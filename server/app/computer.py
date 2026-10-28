"""Team Computer sessions, server-side (0.10.0, bagian F).

One session per scope (``team`` | ``private``): who is using the
browser, who controls it (bot or user), whether the browser is
open, and whether a live screen is available. Session state is
persisted to a JSON file (mode 0600) so a server restart does
not forget who was using the computer.

Honest screen (the lesson from the black/empty screenshot):
``capture_screen`` returns the REAL desktop image taken with
``scrot`` on display :100 — or ``available: False`` + a reason
when there is nothing real to show (display down, no browser
window, scrot failed). It NEVER returns a black/blank image
pretending to be live. A blank white page IS a real screenshot
of a blank browser tab, and is returned as such.

Control flow (who may click/type):
* ``controlled_by == "user"``  -> only the app user may act.
* ``controlled_by == "bot"``   -> only a bot tool call may act.
* Anything else gets an honest 409-style refusal
  (``ControlLocked``); the bot is paused while the user holds
  control, and vice versa.

How clicks/typing reach the browser:
* The browser bridge exposes ref-based tools only
  (``browser_click`` / ``browser_type`` take an accessibility
  ``ref`` from ``browser_snapshot`` — there is NO coordinate
  click). A tap on the app screen arrives as normalized x,y,
  so clicks go through ``xdotool mousemove + click`` on :100
  (pixels, no shell, no bridge tab needed).
* Typing goes through ``xdotool type`` to the focused window
  (same: argv list, no shell). Passwords NEVER go through this
  path — the login flow types them via the bridge's
  ``browser_type`` (ref-based, value in the JSON-RPC body,
  never in a process command line visible via ``ps``).
* Scroll goes through ``xdotool`` mouse wheel (button 4/5) at
  the given point; zoom through ``xdotool key`` Ctrl+plus /
  Ctrl+minus / Ctrl+0 to the focused browser window. Both are
  control-checked like click/type (0.10.7).
* Browser navigation / snapshot / login DO use the bridge
  (127.0.0.1:19885). When no tab is connected the bridge
  answers "No connection to browser extension" — that is
  reported honestly as "browser belum terhubung", never faked.

Dependencies on this machine (all verified present): Xvfb :100,
``scrot``, ``xdotool``, ``xwininfo``, bridge on 127.0.0.1:19885.
All helpers are module-level functions so tests can monkeypatch
them (``_run_cmd``, ``_bridge_call``, ``_x_socket_exists``,
``_browser_window_present``, ``_run_scrot``).
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_FILE = _REPO_ROOT / "computer-state.json"

DISPLAY = ":100"
SCREEN_WIDTH = 1600
SCREEN_HEIGHT = 900
BRIDGE_URL = "http://127.0.0.1:19885/mcp"
CDP_LIST_URL = "http://127.0.0.1:9222/json/list"
CDP_TIMEOUT = 10
SCOPES = ("team", "private")

# Login URLs for services the login flow knows. Unknown services
# are refused honestly instead of guessed — extend this map when
# a new service is added (never guess a login URL, never open an
# empty tab for an unknown service).
SERVICE_URLS = {
    "youtube-studio": "https://studio.youtube.com",
    "youtube": "https://www.youtube.com/",
    "gmail": "https://mail.google.com",
    "google": "https://accounts.google.com/",
    "github": "https://github.com/login",
}

# Opened when /api/computer/open is called without a URL: a real
# start page, never an empty tab (an empty tab was the old bug —
# the browser opened to about:blank and the login then failed
# silently somewhere else).
DEFAULT_OPEN_URL = "https://duckduckgo.com"

_NO_TAB_TEXT = "No connection to browser extension"


class ComputerError(Exception):
    """A computer operation failed honestly (message is user-facing).

    ``status`` is the HTTP status the endpoint should answer
    with: 400 validation, 409 control held by the other side,
    502 browser/bridge down, 500 command failed.
    """

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class BridgeError(ComputerError):
    """The browser bridge call failed."""


class BridgeNoTab(BridgeError):
    """The bridge is up but no browser tab is connected."""

    def __init__(self, message=None):
        super().__init__(
            message or "browser belum terhubung: buka browser di PC "
                      "lalu klik ikon ekstensi browser dan tekan Connect",
            status=502)


class ControlLocked(ComputerError):
    """The other side holds control (HTTP 409 at the endpoint)."""

    def __init__(self, message):
        super().__init__(message, status=409)


class ScreenUnavailable(ComputerError):
    """No real screen image is available; .reason says why."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _now():
    return time.time()


def _utcnow_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_scope(scope):
    if scope not in SCOPES:
        raise ComputerError(
            "scope harus team atau private, bukan %r" % (scope,))
    return scope


# -- injectable low-level helpers (monkeypatched in tests) ---------
def _run_cmd(argv, env=None, timeout=30):
    """Run a command (argv list, no shell). Returns CompletedProcess."""
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(argv, capture_output=True, text=True,
                          env=run_env, timeout=timeout)


_BRIDGE_SESSION = {"id": None}


def _bridge_call(name, arguments, req_id=1):
    """One tools/call to the browser bridge. Returns the result dict.

    Raises BridgeNoTab when the bridge reports no connected tab
    (one retry after 3s, same as the browse path — the extension
    reconnects to a fresh bridge process after an idle gap).
    Raises BridgeError on transport failure or a tool error.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}
                      ).encode()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if _BRIDGE_SESSION["id"]:
        headers["Mcp-Session-Id"] = _BRIDGE_SESSION["id"]

    def _once():
        req = urllib.request.Request(BRIDGE_URL, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                _BRIDGE_SESSION["id"] = sid
            return json.loads(resp.read().decode("utf-8", "replace"))

    try:
        res = _once()
    except Exception as e:
        _BRIDGE_SESSION["id"] = None
        raise BridgeError("bridge tidak terjangkau: %s" % e, status=502)
    text = _bridge_text(res)
    if _NO_TAB_TEXT in text:
        time.sleep(3)
        try:
            res = _once()
        except Exception as e:
            _BRIDGE_SESSION["id"] = None
            raise BridgeError("bridge tidak terjangkau: %s" % e, status=502)
        text = _bridge_text(res)
        if _NO_TAB_TEXT in text:
            raise BridgeNoTab()
    if isinstance(res.get("result", {}).get("isError"), bool) \
            and res["result"]["isError"]:
        raise BridgeError("bridge menolak %s: %s" % (name, text[:300]),
                          status=502)
    return res.get("result", {})


def _bridge_text(res):
    """Joined text blocks of a bridge tools/call result."""
    content = res.get("result", {}).get("content", []) if isinstance(res, dict) else []
    out = ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            out += block.get("text", "") + "\n"
    return out


# -- CDP (Chrome DevTools Protocol, 0.10.8) -----------------------
# Grounding visual berbasis TEKS untuk model text-only: model tidak
# bisa melihat screenshot, jadi koordinat klik harus datang dari
# DOM (getBoundingClientRect), bukan dari tebakan layout. CDP
# berjalan di 127.0.0.1:9222 (Chromium :100). Semua helper ini
# module-level supaya test bisa monkeypatch (seperti _run_cmd /
# _bridge_call).
def _cdp_targets():
    """Daftar tab Chrome via CDP /json/list."""
    req = urllib.request.Request(CDP_LIST_URL, method="GET")
    with urllib.request.urlopen(req, timeout=CDP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _cdp_pick_target(targets, url_hint):
    """Pilih tab halaman yang sedang dilihat (bukan chrome://,
    bukan tab blank MCP). Urutan: URL persis, lalu awalan URL
    sesi, lalu origin sama, lalu tab halaman pertama yang layak.
    """
    from urllib.parse import urlparse
    pages = [t for t in (targets or [])
             if t.get("type") == "page"
             and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    hint = (url_hint or "").strip()
    if hint:
        for t in pages:
            if t.get("url") == hint:
                return t
        for t in pages:
            if t.get("url", "").startswith(hint):
                return t
        try:
            hint_host = urlparse(hint).netloc
        except Exception:
            hint_host = ""
        if hint_host:
            for t in pages:
                try:
                    if urlparse(t.get("url", "")).netloc == hint_host:
                        return t
                except Exception:
                    continue
    for t in pages:
        u = t.get("url", "")
        if u.startswith("chrome://") or "browsermcp.io/blank" in u:
            continue
        return t
    return pages[0]


def _cdp_call(ws, method, params, req_id):
    """Satu pemanggilan CDP; lewati event sampai jawaban id cocok."""
    ws.send(json.dumps({"id": req_id, "method": method,
                        "params": params}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == req_id:
            return msg
        # event (mis. Runtime.executionContextCreated) — lewati.


def _cdp_evaluate(expression, url_hint=None):
    """Jalankan JS di tab aktif via CDP. Kembalikan nilai JS-nya.

    Raises ComputerError(502) bila CDP/tab tidak terjangkau atau
    JS-nya throw — dilaporkan jujur, tidak pernah dipalsukan.
    """
    try:
        targets = _cdp_targets()
    except Exception as e:
        raise ComputerError("CDP browser tidak terjangkau: %s" % e,
                            status=502)
    target = _cdp_pick_target(targets, url_hint)
    if not target:
        raise ComputerError("tidak ada tab browser aktif (CDP kosong)",
                            status=502)
    try:
        import websocket  # websocket-client, terpasang di PC
    except ImportError as e:
        raise ComputerError(
            "modul websocket-client belum terpasang: %s" % e, status=500)
    try:
        # suppress_origin: Chrome menolak WebSocket CDP bila ada
        # header Origin http://127.0.0.1:9222 (403) — tanpa Origin
        # (seperti klien ws Node) diterima. Jangan restart Chrome
        # dengan --remote-allow-origins hanya untuk ini.
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=CDP_TIMEOUT,
            suppress_origin=True)
    except Exception as e:
        raise ComputerError("CDP tidak bisa tersambung ke tab: %s" % e,
                            status=502)
    try:
        _cdp_call(ws, "Runtime.enable", {}, 1)
        msg = _cdp_call(ws, "Runtime.evaluate",
                        {"expression": expression,
                         "returnByValue": True}, 2)
    except Exception as e:
        raise ComputerError("CDP gagal mengevaluasi JS: %s" % e,
                            status=502)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    res = msg.get("result", {}) if isinstance(msg, dict) else {}
    if res.get("exceptionDetails"):
        detail = res["exceptionDetails"]
        text = ""
        try:
            text = detail["exception"]["description"]
        except Exception:
            text = str(detail)[:200]
        raise ComputerError("JS di halaman gagal: %s" % text[:200],
                            status=502)
    return res.get("result", {}).get("value")


def _cdp_navigate(url, url_hint=None):
    """Navigasi tab aktif ke URL via CDP Page.navigate.

    Fallback open_browser bila bridge (ekstensi Browser MCP) mati
    / nonaktif — ekstensi ini rawan mati ~50 mnt idle dan pernah
    ter-disable sendiri di chrome://extensions, sementara CDP di
    9222 tetap hidup. Mengembalikan URL tujuan.
    """
    try:
        targets = _cdp_targets()
    except Exception as e:
        raise ComputerError("CDP browser tidak terjangkau: %s" % e,
                            status=502)
    target = _cdp_pick_target(targets, url_hint)
    if not target:
        raise ComputerError("tidak ada tab browser aktif (CDP kosong)",
                            status=502)
    try:
        import websocket  # websocket-client, terpasang di PC
    except ImportError as e:
        raise ComputerError(
            "modul websocket-client belum terpasang: %s" % e, status=500)
    try:
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=CDP_TIMEOUT,
            suppress_origin=True)
    except Exception as e:
        raise ComputerError("CDP tidak bisa tersambung ke tab: %s" % e,
                            status=502)
    try:
        msg = _cdp_call(ws, "Page.navigate", {"url": url}, 1)
    except Exception as e:
        raise ComputerError("CDP gagal navigasi: %s" % e, status=502)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    res = msg.get("result", {}) if isinstance(msg, dict) else {}
    if res.get("errorText"):
        raise ComputerError("navigasi CDP gagal: %s"
                            % str(res["errorText"])[:200], status=502)
    # Aktifkan (focus) tab ini — xdotool type/click mengetik ke tab
    # AKTIF, bukan ke tab sesi. Tanpa ini, sesudah klik membuka tab
    # baru, ketikan berikutnya nyasar ke tab lama yang masih aktif.
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:9222/json/activate/%s"
            % target["id"], method="GET")
        with urllib.request.urlopen(req, timeout=CDP_TIMEOUT) as resp:
            resp.read()
    except Exception:
        pass
    return url


def _cdp_click(x, y, url_hint=None):
    """Klik di titik (x, y) VIEWPORT-normalized (0..1) via CDP
    Input.dispatchMouseEvent (ruang CSS px viewport — sama persis
    dengan getBoundingClientRect yang dipakai list_elements).

    Dipakai browser_click model (0.10.8). Lebih andal daripada
    xdotool: tidak butuh offset chrome / devicePixelRatio (di sini
    DPR=1.25 membuat konversi layar meleset ~60px di Y). Klik
    xdotool di /api/computer/click tetap ada untuk tap Team
    Computer (koordinat layar dari mirror).
    """
    try:
        targets = _cdp_targets()
    except Exception as e:
        raise ComputerError("CDP browser tidak terjangkau: %s" % e,
                            status=502)
    target = _cdp_pick_target(targets, url_hint)
    if not target:
        raise ComputerError("tidak ada tab browser aktif (CDP kosong)",
                            status=502)
    try:
        import websocket  # websocket-client, terpasang di PC
    except ImportError as e:
        raise ComputerError(
            "modul websocket-client belum terpasang: %s" % e, status=500)
    # Ukuran viewport (CSS px) — untuk mengubah 0..1 jadi px.
    size = _cdp_evaluate(
        "({w: window.innerWidth, h: window.innerHeight})", url_hint)
    try:
        vw = float(size["w"])
        vh = float(size["h"])
    except (KeyError, TypeError, ValueError):
        raise ComputerError("ukuran viewport browser tidak terbaca",
                            status=502)
    px = max(0, min(vw - 1, round(float(x) * vw)))
    py = max(0, min(vh - 1, round(float(y) * vh)))
    # Aktifkan tab target dulu — klik CDP masuk ke tab ini, dan
    # ketikan xdotool berikutnya harus ke tab yang sama.
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:9222/json/activate/%s"
            % target["id"], method="GET")
        with urllib.request.urlopen(req, timeout=CDP_TIMEOUT) as resp:
            resp.read()
    except Exception:
        pass
    try:
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=CDP_TIMEOUT,
            suppress_origin=True)
    except Exception as e:
        raise ComputerError("CDP tidak bisa tersambung ke tab: %s" % e,
                            status=502)
    try:
        _cdp_call(ws, "Input.dispatchMouseEvent",
                  {"type": "mousePressed", "x": px, "y": py,
                   "button": "left", "clickCount": 1}, 1)
        _cdp_call(ws, "Input.dispatchMouseEvent",
                  {"type": "mouseReleased", "x": px, "y": py,
                   "button": "left", "clickCount": 1}, 2)
    except Exception as e:
        raise ComputerError("CDP gagal klik: %s" % e, status=502)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return {"x": px, "y": py, "viewport": [int(vw), int(vh)]}


# JS pengumpul elemen klik (0.10.8): semua <a>/<button>/[role]/
# input submit yang TERLIHAT di viewport, dengan koordinat PUSAT
# elemen dalam ruang LAYAR (0..1, sama seperti browser_click).
# Koreksi chrome: viewport mulai di bawah tab bar + address bar +
# banner debug (outerHeight-innerHeight); kiri ~0 (maximized).
# Kembalikan {url, title, width, height, count, elements}.
_LIST_ELEMENTS_JS = r"""
(() => {
  const els = [...document.querySelectorAll('a, button, [role="button"], input[type="submit"], input[type="button"], summary')];
  const out = [];
  let belowFold = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > window.innerHeight || r.right < 0 || r.left > window.innerWidth) { if (r.top > window.innerHeight) belowFold++; continue; }
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const href = el.tagName === 'A' ? (el.getAttribute('href') || '').slice(0, 120) : '';
    // Koordinat VIEWPORT (0..1 dari innerWidth/innerHeight, CSS px).
    // browser_click memakai koordinat ini via CDP Input.dispatchMouseEvent
    // (viewport space) — BUKAN koordinat layar. Alasan: konversi ke
    // koordinat layar butuh offset chrome + devicePixelRatio (di sini
    // DPR=1.25) yang rapuh; CDP click memakai ruang yang sama persis
    // dengan getBoundingClientRect, jadi tidak bisa meleset.
    out.push({tag: el.tagName.toLowerCase(), label, href, x: Math.round(cx / window.innerWidth * 1000) / 1000, y: Math.round(cy / window.innerHeight * 1000) / 1000});
    if (out.length >= 30) break;
  }
  out.sort((a, b) => (a.y - b.y) || (a.x - b.x));
  return {url: location.href, title: document.title, width: window.innerWidth, height: window.innerHeight, count: out.length, below_fold: belowFold, elements: out};
})()
"""


def _x_socket_exists():
    return os.path.exists("/tmp/.X11-unix/X100")


def _browser_window_present():
    """True when a real browser window is mapped on :100.

    Uses xwininfo on the root window and looks for a Chromium /
    Firefox window of a plausible size. A failure to run xwininfo
    is treated as "unknown" (True) so a missing helper never
    blocks a real screenshot — scrot is the ground truth below.
    """
    try:
        proc = _run_cmd(["xwininfo", "-root", "-tree"],
                        env={"DISPLAY": DISPLAY}, timeout=10)
    except Exception:
        return True
    if proc.returncode != 0:
        return True
    for line in (proc.stdout or "").splitlines():
        low = line.lower()
        if "chromium" not in low and "firefox" not in low:
            continue
        m = re.search(r"(\d+)x(\d+)\+", line)
        if m and int(m.group(1)) >= 200 and int(m.group(2)) >= 150:
            return True
    return False


def _run_scrot(path):
    """Capture :100 to ``path`` (PNG). Returns CompletedProcess."""
    return _run_cmd(["scrot", "-q", "85", str(path)],
                    env={"DISPLAY": DISPLAY}, timeout=15)


def _png_size(data):
    """(width, height) from PNG bytes, or (None, None)."""
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None, None
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    except Exception:
        return None, None


# -- persistent session state --------------------------------------
class ComputerStore:
    """Per-scope sessions, persisted to JSON (mode 0600)."""

    def __init__(self, path=None):
        self.path = Path(path or os.environ.get(
            "GROKBOT_COMPUTER_STATE_FILE", str(DEFAULT_STATE_FILE)))
        directory = self.path.parent
        if str(directory):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        if not self.path.exists():
            return {"sessions": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(
                    data.get("sessions"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"sessions": {}}

    def _save_locked(self):
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _session_locked(self, scope):
        sessions = self._data.setdefault("sessions", {})
        sess = sessions.get(scope)
        if not isinstance(sess, dict):
            sess = {
                "scope": scope,
                "using_bot": None,
                "controlled_by": "bot",
                "browser_open": False,
                "url": None,
                "screen_available": False,
                "screen_reason": "Belum ada screenshot.",
                "screen_age_seconds": None,
                "last_capture_at": None,
                "last_width": None,
                "last_height": None,
                "updated_at": _utcnow_iso(),
            }
            sessions[scope] = sess
        return sess

    def get_status(self, scope):
        """Public status dict for one scope (no live probing)."""
        _validate_scope(scope)
        with self._lock:
            sess = self._session_locked(scope)
            age = None
            if sess.get("last_capture_at") is not None:
                age = max(0.0, _now() - sess["last_capture_at"])
            return {
                "scope": scope,
                "using_bot": sess.get("using_bot"),
                "controlled_by": sess.get("controlled_by", "bot"),
                "browser_open": bool(sess.get("browser_open")),
                "url": sess.get("url"),
                "screen_available": bool(sess.get("screen_available")),
                "screen_reason": sess.get("screen_reason"),
                "screen_age_seconds": age,
            }

    def update(self, scope, **fields):
        """Set session fields + updated_at, persist, return status."""
        _validate_scope(scope)
        with self._lock:
            sess = self._session_locked(scope)
            sess.update(fields)
            sess["updated_at"] = _utcnow_iso()
            self._save_locked()
            age = None
            if sess.get("last_capture_at") is not None:
                age = max(0.0, _now() - sess["last_capture_at"])
            return {
                "scope": scope,
                "using_bot": sess.get("using_bot"),
                "controlled_by": sess.get("controlled_by", "bot"),
                "browser_open": bool(sess.get("browser_open")),
                "url": sess.get("url"),
                "screen_available": bool(sess.get("screen_available")),
                "screen_reason": sess.get("screen_reason"),
                "screen_age_seconds": age,
            }


# Module-level default store (endpoints use these helpers).
_DEFAULT_STORE = None
_DEFAULT_STORE_LOCK = threading.Lock()


def _store():
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = ComputerStore()
        return _DEFAULT_STORE


def get_status(scope, store=None):
    return (store or _store()).get_status(scope)


def take_control(scope, store=None):
    """User takes over: bot tool calls are refused until release."""
    st = store or _store()
    return st.update(scope, controlled_by="user")


def release_control(scope, store=None):
    """Hand control back to the bot."""
    st = store or _store()
    return st.update(scope, controlled_by="bot")


def _check_control(scope, actor, store=None):
    """Raise ControlLocked when ``actor`` does not hold control."""
    st = store or _store()
    status = st.get_status(scope)
    holder = status["controlled_by"]
    if actor not in ("user", "bot"):
        raise ComputerError("actor harus user atau bot")
    if holder != actor:
        who = "pengguna" if holder == "user" else "bot"
        raise ControlLocked(
            "komputer %s sedang dikendalikan %s — ambil alih dulu "
            "lewat Take control" % (scope, who))
    return status


# -- screen ---------------------------------------------------------
def capture_screen(scope, store=None):
    """Fresh screenshot of :100, or available=False + honest reason.

    Returns the app's screen shape: {available, reason,
    image_b64, width, height, age_seconds}. image_b64 is set ONLY
    when available is True. The temp file is removed afterwards;
    the image bytes are never written to a persistent file.
    """
    _validate_scope(scope)
    st = store or _store()
    try:
        result = _capture_screen_inner()
    except ScreenUnavailable as e:
        st.update(scope, screen_available=False, screen_reason=e.reason)
        return {"available": False, "reason": e.reason,
                "image_b64": None, "width": None, "height": None,
                "age_seconds": None}
    st.update(scope, screen_available=True, screen_reason=None,
              last_capture_at=_now(), last_width=result["width"],
              last_height=result["height"])
    result["age_seconds"] = 0.0
    return result


def _capture_screen_inner():
    if not _x_socket_exists():
        raise ScreenUnavailable(
            "display :100 tidak jalan (Xvfb mati) — layar tidak bisa diambil")
    if not _browser_window_present():
        raise ScreenUnavailable(
            "browser belum berjalan di display :100 — buka browser dulu")
    import tempfile
    import uuid

    # scrot 2.0.0 MENOLAK menimpa file yang sudah ada (rc tetap 0, ia
    # menulis ke <nama>_000.png) — path tujuan TIDAK BOLEH pre-created.
    # mkstemp membuat file kosong dulu -> server membaca file yang
    # tetap 0 byte ("screenshot kosong"). Pakai nama uuid yang belum ada.
    tmp = None
    try:
        tmp = os.path.join(tempfile.gettempdir(),
                           "grokbot-screen-%s.png" % uuid.uuid4().hex)
        try:
            proc = _run_scrot(tmp)
        except Exception as e:
            raise ScreenUnavailable("scrot gagal dijalankan: %s" % e)
        if proc.returncode != 0:
            raise ScreenUnavailable(
                "scrot gagal (kode %s): %s"
                % (proc.returncode, (proc.stderr or "").strip()[:200]))
        try:
            with open(tmp, "rb") as f:
                data = f.read()
        except OSError as e:
            raise ScreenUnavailable("screenshot tidak bisa dibaca: %s" % e)
        if not data:
            raise ScreenUnavailable("screenshot kosong (0 byte)")
        width, height = _png_size(data)
        return {"available": True, "reason": None,
                "image_b64": base64.b64encode(data).decode("ascii"),
                "width": width, "height": height, "age_seconds": 0.0}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# -- browser control -------------------------------------------------
def open_browser(scope, url=None, bot_id=None, bot_name=None, store=None):
    """Open the browser in this scope, straight to the target URL.

    Scope H (auto-open): a URL is ALWAYS navigated to — the one
    given, or DEFAULT_OPEN_URL when none is given. The browser is
    never opened to an empty tab. Failures are specific, never a
    generic "tool operations failed":
    * no browser window on :100 -> "browser belum berjalan ..."
    * bridge up but no tab connected -> fallback CDP Page.navigate
      (0.10.8; ekstensi Browser MCP rawan mati/nonaktif, CDP 9222
      tetap hidup) — hanya bila CDP pun gagal, error "browser
      belum terhubung ..."
    * bad URL -> "url harus diawali http:// atau https://"
    """
    _validate_scope(scope)
    st = store or _store()
    url = (url or DEFAULT_OPEN_URL).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ComputerError("url tidak valid: harus diawali http:// "
                            "atau https://", status=400)
    if not _x_socket_exists():
        raise ComputerError(
            "browser belum berjalan di display :100 — buka browser "
            "di PC dulu, lalu hubungkan tab lewat ekstensi browser",
            status=502)
    if not _browser_window_present():
        raise ComputerError(
            "browser belum berjalan di display :100 — buka browser "
            "di PC dulu, lalu hubungkan tab lewat ekstensi browser",
            status=502)
    # Jalur utama: bridge (ekstensi Browser MCP). Bila bridge mati
    # / ekstensi nonaktif (rawan ~50 mnt idle; pernah ter-disable
    # sendiri), fallback ke CDP Page.navigate — CDP di 9222 tetap
    # hidup dan navigasi tidak butuh ekstensi. 0.10.8.
    try:
        _bridge_call("browser_navigate", {"url": url}, req_id=201)
    except (BridgeNoTab, BridgeError):
        with st._lock:
            hint = st._session_locked(scope).get("url") or ""
        _cdp_navigate(url, hint)
    using_bot = None
    if bot_id:
        using_bot = {"id": bot_id, "name": bot_name or bot_id}
    old = st.get_status(scope)
    st.update(scope, browser_open=True, url=url,
              using_bot=using_bot if bot_id else old["using_bot"])
    status = st.get_status(scope)
    return {"ok": True, "scope": scope, "browser_open": True,
            "url": status["url"], "using_bot": status["using_bot"]}


def click(scope, x, y, actor="user", store=None):
    """Tap at normalized (x, y) — via xdotool on :100.

    The bridge has no coordinate click (ref-based only), so the
    tap goes to the real desktop at that point. Control-checked.
    """
    _validate_scope(scope)
    try:
        x = float(x)
        y = float(y)
    except (TypeError, ValueError):
        raise ComputerError("x dan y harus angka 0..1")
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise ComputerError("x dan y harus di antara 0 dan 1")
    status = _check_control(scope, actor, store)
    st = store or _store()
    with st._lock:
        sess = st._session_locked(scope)
        width = sess.get("last_width") or SCREEN_WIDTH
        height = sess.get("last_height") or SCREEN_HEIGHT
    px = max(0, min(width - 1, round(x * width)))
    py = max(0, min(height - 1, round(y * height)))
    try:
        proc = _run_cmd(["xdotool", "mousemove", str(px), str(py),
                         "click", "1"],
                        env={"DISPLAY": DISPLAY}, timeout=15)
    except Exception as e:
        raise ComputerError("klik gagal dijalankan: %s" % e, status=500)
    if proc.returncode != 0:
        raise ComputerError(
            "klik gagal (kode %s): %s"
            % (proc.returncode, (proc.stderr or "").strip()[:200]),
            status=500)
    return {"ok": True, "scope": scope, "x": x, "y": y, "px": px, "py": py}


def click_viewport(scope, x, y, actor="user", store=None):
    """Klik di titik (x, y) VIEWPORT-normalized (0..1) via CDP.

    Dipakai model (browser_click, 0.10.8): koordinat (x, y) diambil
    dari browser_list_elements (ruang viewport). Control-checked
    seperti click. Mengembalikan titik CSS px + ukuran viewport.
    """
    _validate_scope(scope)
    try:
        x = float(x)
        y = float(y)
    except (TypeError, ValueError):
        raise ComputerError("x dan y harus angka 0..1")
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise ComputerError("x dan y harus di antara 0 dan 1")
    _check_control(scope, actor, store)
    st = store or _store()
    with st._lock:
        hint = st._session_locked(scope).get("url") or ""
    # Catat tab yang sudah ada — link target="_blank" membuka tab
    # BARU; sesudah klik, sesi diarahkan ke tab baru itu supaya
    # list_elements berikutnya membaca halaman yang benar.
    try:
        before = {t.get("id") for t in _cdp_targets()
                  if t.get("type") == "page"}
    except Exception:
        before = set()
    res = _cdp_click(x, y, hint)
    new_url = None
    try:
        import time as _time
        _time.sleep(1.0)
        after = [t for t in _cdp_targets()
                 if t.get("type") == "page"
                 and t.get("id") not in before]
        # Tab baru yang bukan chrome:// / blank MCP.
        fresh = [t for t in after
                 if not (t.get("url", "").startswith("chrome://")
                         or "browsermcp.io/blank" in t.get("url", ""))]
        if fresh:
            new_url = fresh[0].get("url") or ""
    except Exception:
        new_url = None
    if new_url:
        st.update(scope, url=new_url)
    return {"ok": True, "scope": scope, "x": x, "y": y,
            "coord_space": "viewport",
            "px": res["x"], "py": res["y"],
            "viewport": res["viewport"],
            "url": new_url or hint}


def type_text(scope, text, actor="user", store=None):
    """Type into the focused element — via xdotool on :100.

    Control-checked. NEVER used for passwords (the login flow
    types those via the bridge's ref-based browser_type).
    """
    _validate_scope(scope)
    if not isinstance(text, str) or not text:
        raise ComputerError("text tidak boleh kosong")
    if len(text) > 4000:
        raise ComputerError("text terlalu panjang (maks 4000 karakter)")
    _check_control(scope, actor, store)
    try:
        proc = _run_cmd(["xdotool", "type", "--clearmodifiers", "--", text],
                        env={"DISPLAY": DISPLAY}, timeout=30)
    except Exception as e:
        raise ComputerError("ketik gagal dijalankan: %s" % e, status=500)
    if proc.returncode != 0:
        raise ComputerError(
            "ketik gagal (kode %s): %s"
            % (proc.returncode, (proc.stderr or "").strip()[:200]),
            status=500)
    return {"ok": True, "scope": scope}


def scroll(scope, direction="down", amount=3, x=None, y=None, actor="user", store=None):
    """Scroll the page — via xdotool mouse wheel on :100.

    The bridge has no scroll tool, so the wheel goes to the real
    desktop at (x, y) (default: screen center). Control-checked.
    ``direction``: "down" | "up". ``amount``: wheel notches (1..10).
    """
    _validate_scope(scope)
    direction = (direction or "down").strip().lower()
    if direction not in ("down", "up"):
        raise ComputerError("direction harus 'down' atau 'up'")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise ComputerError("amount harus angka 1..10")
    if not (1 <= amount <= 10):
        raise ComputerError("amount harus 1..10")
    if x is None:
        x = 0.5
    if y is None:
        y = 0.5
    try:
        x = float(x)
        y = float(y)
    except (TypeError, ValueError):
        raise ComputerError("x dan y harus angka 0..1")
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise ComputerError("x dan y harus di antara 0 dan 1")
    status = _check_control(scope, actor, store)
    st = store or _store()
    with st._lock:
        sess = st._session_locked(scope)
        width = sess.get("last_width") or SCREEN_WIDTH
        height = sess.get("last_height") or SCREEN_HEIGHT
    px = max(0, min(width - 1, round(x * width)))
    py = max(0, min(height - 1, round(y * height)))
    # xdotool wheel: button 4 = up, 5 = down.
    button = "5" if direction == "down" else "4"
    try:
        proc = _run_cmd(["xdotool", "mousemove", str(px), str(py),
                         "click", "--repeat", str(amount),
                         "--delay", "120", button],
                        env={"DISPLAY": DISPLAY}, timeout=15)
    except Exception as e:
        raise ComputerError("scroll gagal dijalankan: %s" % e, status=500)
    if proc.returncode != 0:
        raise ComputerError(
            "scroll gagal (kode %s): %s"
            % (proc.returncode, (proc.stderr or "").strip()[:200]),
            status=500)
    return {"ok": True, "scope": scope, "direction": direction,
            "amount": amount, "px": px, "py": py}


def zoom(scope, direction="in", actor="user", store=None):
    """Zoom the browser page — via xdotool Ctrl+plus/minus/0 on :100.

    The bridge has no zoom tool, so the keys go to the focused
    window (the browser). Control-checked. ``direction``:
    "in" | "out" | "reset".
    """
    _validate_scope(scope)
    direction = (direction or "in").strip().lower()
    keys = {"in": "ctrl+plus", "out": "ctrl+minus", "reset": "ctrl+0"}
    if direction not in keys:
        raise ComputerError("direction harus 'in', 'out', atau 'reset'")
    _check_control(scope, actor, store)
    try:
        proc = _run_cmd(["xdotool", "key", keys[direction]],
                        env={"DISPLAY": DISPLAY}, timeout=15)
    except Exception as e:
        raise ComputerError("zoom gagal dijalankan: %s" % e, status=500)
    if proc.returncode != 0:
        raise ComputerError(
            "zoom gagal (kode %s): %s"
            % (proc.returncode, (proc.stderr or "").strip()[:200]),
            status=500)
    return {"ok": True, "scope": scope, "direction": direction}


def list_elements(scope, actor="user", store=None):
    """Daftar elemen yang bisa diklik di tab aktif + koordinat
    layar (0..1) — grounding visual berbasis TEKS (0.10.8).

    Model DeepSeek V3 text-only tidak bisa melihat screenshot,
    jadi ia TIDAK BOLEH menebak koordinat dari ingatan layout
    (itu penyebab klik meleset 2x di 0.10.7). Ia memanggil tool
    ini dulu, memilih elemen dari daftar, lalu browser_click ke
    (x, y) elemen itu. Control-checked seperti click.
    """
    _validate_scope(scope)
    _check_control(scope, actor, store)
    st = store or _store()
    with st._lock:
        sess = st._session_locked(scope)
        url_hint = sess.get("url") or ""
    value = _cdp_evaluate(_LIST_ELEMENTS_JS, url_hint)
    if not isinstance(value, dict) \
            or not isinstance(value.get("elements"), list):
        raise ComputerError(
            "daftar elemen browser tidak terbaca (CDP kosong)",
            status=502)
    elements = []
    for el in value["elements"][:30]:
        try:
            x = float(el["x"])
            y = float(el["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            continue
        elements.append({
            "tag": str(el.get("tag", ""))[:20],
            "label": str(el.get("label", ""))[:80],
            "href": str(el.get("href", ""))[:120],
            "x": round(x, 3),
            "y": round(y, 3),
        })
    return {"ok": True, "scope": scope,
            "url": value.get("url") or url_hint,
            "title": str(value.get("title", ""))[:120],
            "width": int(value.get("width") or 0),
            "height": int(value.get("height") or 0),
            "coord_space": "viewport",
            "count": len(elements),
            "below_fold": int(value.get("below_fold") or 0),
            "elements": elements}


# -- login ------------------------------------------------------------
_REF_RE = re.compile(r"\[ref=([^\]]+)\]")


def _snapshot_lines():
    try:
        result = _bridge_call("browser_snapshot", {}, req_id=202)
    except BridgeNoTab:
        raise
    except BridgeError as e:
        raise ComputerError(str(e), status=502)
    return _bridge_text({"result": result}).splitlines()


def _find_ref(lines, kinds, labels):
    """First (ref, line) whose line matches a kind + a label.

    ``kinds``: snapshot element kinds (textbox, button, ...).
    ``labels``: case-insensitive substrings of the visible label.
    """
    for line in lines:
        low = line.lower()
        if not any(k in low for k in kinds):
            continue
        if labels and not any(lbl.lower() in low for lbl in labels):
            continue
        m = _REF_RE.search(line)
        if m:
            return m.group(1), line.strip()
    return None, None


def _type_into(ref, element, text, submit=False):
    try:
        _bridge_call("browser_type",
                     {"element": element, "ref": ref,
                      "text": text, "submit": submit}, req_id=203)
    except BridgeNoTab:
        raise
    except BridgeError as e:
        raise ComputerError("gagal mengetik ke %s: %s" % (element, e),
                            status=502)


def _click_ref(ref, element):
    try:
        _bridge_call("browser_click",
                     {"element": element, "ref": ref}, req_id=204)
    except BridgeNoTab:
        raise
    except BridgeError as e:
        raise ComputerError("gagal klik %s: %s" % (element, e), status=502)


def login(service, vault, url=None, scope="team", store=None):
    """Log in to one service using its stored vault credential.

    Scope H (auto-open): the credential is checked FIRST — a
    missing credential or an unknown service fails WITHOUT
    opening the browser at all (never an empty tab + silent
    failure). Otherwise the browser is navigated STRAIGHT to
    the service URL and the credential is typed in through the
    bridge. The values NEVER enter the response, the log, or
    the chat. Returns {ok, service, url} or {ok: False, error}
    with an honest reason at each step; unknown service also
    carries status 400 for the endpoint. Success is judged by
    "no password field / no error text left on the page" — it
    is NOT proof of a session. A 2FA/captcha page is NOT a
    failure: it returns {ok: True, needs_user_action: "2fa",
    message} so the caller can wait for the user to confirm on
    their phone and then continue the session itself.
    """
    _validate_scope(scope)
    if vault is None:
        return {"ok": False, "service": service,
                "error": "vault belum dikonfigurasi — simpan kredensial "
                         "dulu setelah kunci vault disiapkan"}
    try:
        stored = bool(vault.has(service))
    except Exception:
        stored = False
    if not stored:
        # Checked BEFORE any browser is opened: never open an
        # empty tab and then fail silently.
        return {"ok": False, "service": service,
                "error": "kredensial %s belum tersimpan di vault" % service}
    url = (url or SERVICE_URLS.get(service) or "").strip()
    if not url:
        # Unknown service: HTTP 400 at the endpoint (the handler
        # reads result["status"]). Never an empty tab.
        return {"ok": False, "service": service, "status": 400,
                "error": "service %s tidak dikenal — belum ada URL "
                         "login untuk layanan ini" % service}
    try:
        creds = vault.get(service)
    except KeyError:
        return {"ok": False, "service": service,
                "error": "kredensial %s belum tersimpan" % service}
    except Exception:
        return {"ok": False, "service": service,
                "error": "kredensial %s tidak bisa dibaca (kunci vault "
                         "salah atau data rusak)" % service}
    username = creds["username"]
    password = creds["password"]
    try:
        _bridge_call("browser_navigate", {"url": url}, req_id=205)
    except BridgeNoTab as e:
        return {"ok": False, "service": service, "url": url,
                "error": str(e)}
    except BridgeError as e:
        return {"ok": False, "service": service, "url": url,
                "error": str(e)}
    time.sleep(3)
    try:
        lines = _snapshot_lines()
        user_ref, _ = _find_ref(
            lines, ("textbox",),
            ("email", "username", "user", "phone", "telepon",
             "nama pengguna", "email or phone"))
        if not user_ref:
            return {"ok": False, "service": service, "url": url,
                    "error": "kolom username tidak ditemukan di halaman "
                             "login — halaman mungkin berubah atau butuh "
                             "klik manual"}
        _type_into(user_ref, "username field", username)
        time.sleep(1)
        lines = _snapshot_lines()
        pass_ref, _ = _find_ref(
            lines, ("textbox",), ("password", "kata sandi", "sandi"))
        if not pass_ref:
            # Multi-step login (email dulu, lalu Next): advance once.
            next_ref, _ = _find_ref(
                lines, ("button",),
                ("next", "berikutnya", "continue", "lanjutkan"))
            if not next_ref:
                return {"ok": False, "service": service, "url": url,
                        "error": "kolom password tidak ditemukan setelah "
                                 "username — halaman login mungkin berubah"}
            _click_ref(next_ref, "Next button")
            time.sleep(3)
            lines = _snapshot_lines()
            pass_ref, _ = _find_ref(
                lines, ("textbox",), ("password", "kata sandi", "sandi"))
            if not pass_ref:
                return {"ok": False, "service": service, "url": url,
                        "error": "kolom password tidak ditemukan setelah "
                                 "tombol Next — halaman login mungkin "
                                 "berubah atau meminta verifikasi"}
        _type_into(pass_ref, "password field", password)
        time.sleep(1)
        lines = _snapshot_lines()
        submit_ref, _ = _find_ref(
            lines, ("button",),
            ("sign in", "masuk", "login", "next", "berikutnya"))
        if submit_ref:
            _click_ref(submit_ref, "Sign in button")
        else:
            try:
                _bridge_call("browser_press_key", {"key": "Enter"},
                             req_id=206)
            except BridgeError as e:
                return {"ok": False, "service": service, "url": url,
                        "error": str(e)}
        time.sleep(3)
        lines = _snapshot_lines()
        page = "\n".join(lines).lower()
        if "captcha" in page or "verifikasi" in page \
                or "2-step" in page or "2fa" in page:
            # Not a failure: the service is waiting for the user
            # to confirm on their phone. Structured signal so the
            # caller can tell "wait for user" apart from "failed"
            # without string-matching the message.
            return {"ok": True, "service": service, "url": url,
                    "needs_user_action": "2fa",
                    "message": "Google meminta verifikasi 2 langkah — "
                               "pengguna perlu konfirmasi di HP-nya."}
        if "wrong password" in page or "password salah" in page \
                or "incorrect" in page or "couldn't find" in page:
            return {"ok": False, "service": service, "url": url,
                    "error": "login ditolak oleh layanan — periksa "
                             "kredensial yang tersimpan"}
        pass_ref, _ = _find_ref(
            lines, ("textbox",), ("password", "kata sandi", "sandi"))
        if pass_ref:
            return {"ok": False, "service": service, "url": url,
                    "error": "login belum berhasil — kolom password "
                             "masih tampil (kredensial mungkin salah)"}
    except BridgeNoTab as e:
        return {"ok": False, "service": service, "url": url,
                "error": str(e)}
    except ComputerError as e:
        return {"ok": False, "service": service, "url": url,
                "error": str(e)}
    finally:
        # Drop the plaintext references as soon as possible.
        username = ""
        password = ""
    st = store or _store()
    st.update(scope, browser_open=True, url=url)
    return {"ok": True, "service": service, "url": url}
