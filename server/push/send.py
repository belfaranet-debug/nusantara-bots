"""FCM HTTP v1 sender — urllib only, no vendor SDK.

Flow (Google's server-to-server OAuth2):
1. Read the service-account JSON from the path in
   ``GROKBOT_FCM_SERVICE_ACCOUNT``. Fields used: ``project_id``,
   ``private_key``, ``client_email``.
2. Build a JWT (RS256, signed with the service account's private
   key via ``cryptography``) and exchange it at Google's OAuth2
   endpoint for a short-lived access token (cached in memory
   until shortly before expiry).
3. POST the message to
   ``https://fcm.googleapis.com/v1/projects/<project_id>/messages``.

Honest failure, following the ``app/mail_client.py`` spirit:
* No credentials configured -> ``PushNotConfigured`` with the
  plain message "kredensial FCM belum dikonfigurasi". Nothing
  is sent, nothing is faked, nothing is logged as delivered.
* Any network / HTTP / FCM error -> ``PushError`` with a short,
  sanitized message. Service-account key material is never
  included in an error string.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

NOT_CONFIGURED_MESSAGE = "kredensial FCM belum dikonfigurasi"

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SEND_URL = ("https://fcm.googleapis.com/v1/projects/%s/messages")

# Refresh the access token when fewer seconds than this remain.
TOKEN_REFRESH_SKEW_SECONDS = 60
DEFAULT_TIMEOUT = 15


class PushError(Exception):
    """FCM could not be reached or refused the message."""


class PushNotConfigured(Exception):
    """No Firebase service-account credentials are configured."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _scrub(text):
    """Short, log-safe error text (no key-shaped material)."""
    clean = " ".join(str(text or "").split())
    return clean[:200] if clean else "FCM error"


def _load_service_account(path):
    """Parse the service-account JSON. Raises PushNotConfigured."""
    if not path:
        raise PushNotConfigured(NOT_CONFIGURED_MESSAGE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            account = json.load(f)
    except OSError:
        raise PushNotConfigured(
            "kredensial FCM belum dikonfigurasi: file service account "
            "tidak ditemukan di GROKBOT_FCM_SERVICE_ACCOUNT")
    except ValueError:
        raise PushNotConfigured(
            "kredensial FCM belum dikonfigurasi: file service account "
            "bukan JSON yang valid")
    if not isinstance(account, dict):
        raise PushNotConfigured(
            "kredensial FCM belum dikonfigurasi: file service account "
            "tidak berisi objek JSON")
    missing = [k for k in ("project_id", "private_key", "client_email")
               if not account.get(k)]
    if missing:
        raise PushNotConfigured(
            "kredensial FCM belum dikonfigurasi: field hilang di "
            "service account: %s" % ", ".join(missing))
    return account


def _build_jwt(account, now=None):
    """Signed JWT for the OAuth2 service-account flow (RS256).

    Returns the compact JWT string. Raises PushError if the
    private key cannot be loaded (bad key material, never echoed).
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": account["client_email"],
        "sub": account["client_email"],
        "aud": OAUTH_TOKEN_URL,
        "scope": FCM_SCOPE,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = ("%s.%s" % (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
    )).encode("ascii")
    try:
        key = serialization.load_pem_private_key(
            account["private_key"].encode("utf-8"), password=None)
        signature = key.sign(
            signing_input, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise PushError("kunci service account FCM tidak bisa dibaca")
    return "%s.%s" % (signing_input.decode("ascii"), _b64url(signature))


def build_message(token, title, body, data=None) -> dict:
    """Pure FCM HTTP v1 message body for one registration token.

    ``data`` values must be strings (FCM requirement) — anything
    else is stringified. Raises ValueError on a bad token/title.
    """
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token must be a non-empty string")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    clean_data = {}
    for key, value in (data or {}).items():
        clean_data[str(key)] = value if isinstance(value, str) else str(value)
    return {"message": {
        "token": token,
        "notification": {"title": title, "body": body},
        "data": clean_data,
    }}


class FcmSender:
    """One sender bound to one service account.

    ``service_account_path`` defaults to the
    ``GROKBOT_FCM_SERVICE_ACCOUNT`` env var. The account is
    loaded lazily on first send so importing this module never
    fails on a machine without credentials.
    """

    def __init__(self, service_account_path=None):
        self.service_account_path = (
            service_account_path
            or os.environ.get("GROKBOT_FCM_SERVICE_ACCOUNT"))
        self._account = None
        self._access_token = None
        self._access_token_expires_at = 0

    # -- configuration ---------------------------------------------
    def configured(self):
        """(True, "") or (False, reason) — no network involved."""
        if self._account is not None:
            return True, ""
        try:
            _load_service_account(self.service_account_path)
        except PushNotConfigured as e:
            return False, str(e)
        return True, ""

    def _account_or_raise(self):
        if self._account is None:
            self._account = _load_service_account(self.service_account_path)
        return self._account

    # -- transport ---------------------------------------------------
    def _post_json(self, url, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8"))
                message = (detail.get("error", {}).get("message")
                           or detail.get("error_description") or "")
            except Exception:
                message = ""
            raise PushError("FCM HTTP %s%s" % (
                e.code, (": " + _scrub(message)) if message else ""))
        except urllib.error.URLError as e:
            raise PushError("FCM unreachable: %s"
                            % _scrub(getattr(e, "reason", e)))
        except (TimeoutError, OSError) as e:
            raise PushError("FCM unreachable: %s" % _scrub(e))
        except (ValueError, UnicodeDecodeError):
            raise PushError("FCM returned non-JSON output")

    def _access_token(self):
        """Cached OAuth2 access token, refreshed when close to expiry."""
        now = time.time()
        if (self._access_token is not None
                and now < self._access_token_expires_at
                - TOKEN_REFRESH_SKEW_SECONDS):
            return self._access_token
        account = self._account_or_raise()
        jwt = _build_jwt(account)
        reply = self._post_json(OAUTH_TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        })
        token = reply.get("access_token")
        if not token:
            raise PushError("FCM OAuth2 tidak mengembalikan access token")
        expires_in = reply.get("expires_in") or 3600
        self._access_token = token
        self._access_token_expires_at = now + int(expires_in)
        return token

    # -- public API ---------------------------------------------------
    def send_notification(self, token, title, body, data=None) -> dict:
        """Send one notification. Returns {"ok": True, "name": ...}.

        Raises PushNotConfigured when no credentials are set up,
        PushError on any transport / FCM failure, ValueError on a
        bad token/title. Never silently pretends a send happened.
        """
        message = build_message(token, title, body, data)
        account = self._account_or_raise()
        access_token = self._access_token()
        url = FCM_SEND_URL % account["project_id"]
        reply = self._post_json(url, message, headers={
            "Authorization": "Bearer %s" % access_token})
        name = reply.get("name")
        if not name:
            raise PushError("FCM tidak mengembalikan message id")
        return {"ok": True, "name": name}


def send_notification(token, title, body, data=None) -> dict:
    """Module-level convenience: one send with a fresh sender."""
    return FcmSender().send_notification(token, title, body, data)
