"""Push notifications (FCM) for the own build — Phase 2 (h).

Server-side plumbing only. Nothing here can send anything until
the owner provides Firebase credentials (see PUSH-SETUP.md): without
them every send fails cleanly with "kredensial FCM belum
dikonfigurasi" and nothing is delivered silently.

Pieces:
* ``tokens`` — registry of per-character / per-device FCM tokens
  (JSON file, atomic writes under a lock, same pattern as
  ``office/kanban.py`` and ``computers/registry.py``).
* ``send`` — FCM HTTP v1 sender over urllib (service-account
  JWT -> OAuth2 access token -> messages endpoint).
* ``hooks`` — pure payload builders + fan-out: approval requested
  and task done. Honest no-ops when there are no tokens or no
  credentials.

No keys of any kind live in this package. The service-account
JSON is read from the path in ``GROKBOT_FCM_SERVICE_ACCOUNT``;
the VAPID key lives in the browser only (see PUSH-CLIENT.md).
"""

from .hooks import notify_approval_requested, notify_task_done
from .send import FcmSender, PushError, PushNotConfigured, send_notification
from .tokens import PLATFORMS, PushTokenRegistry

__all__ = [
    "PLATFORMS",
    "FcmSender",
    "PushError",
    "PushNotConfigured",
    "PushTokenRegistry",
    "notify_approval_requested",
    "notify_task_done",
    "send_notification",
]
