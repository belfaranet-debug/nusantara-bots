# server-wiring.patch.md — push (FCM), untuk koordinator

Patch doc: JANGAN edit `app/server.py` dari dokumen ini.
Koordinator yang menempel potongan di bawah ke `server.py`
(builder lain sedang mengedit file itu).

## 1. Impor (di dekat impor `approvals` / `office`)

```python
from push import (FcmSender, PushTokenRegistry,  # noqa: E402
                  notify_approval_requested, notify_task_done)
```

## 2. State (di `ServerState.__init__`, dekat `self.approvals`)

```python
# Next-level Phase 2 (h): FCM push tokens + sender.
self.push_tokens = PushTokenRegistry()
self.push_sender = FcmSender()
```

## 3. Endpoint GET baru — sajikan service worker + web config

Di `do_GET`, dekat rute statis (`/static/app.js`):

```python
elif path == "/static/firebase-messaging-sw.js":
    self._send_text((STATIC_DIR / "firebase-messaging-sw.js").read_text(),
                    "application/javascript")
elif path == "/api/push/config":
    # Web config publik + VAPID key, dari env. Bukan rahasia.
    # 503 bila belum dikonfigurasi (bukan config kosong).
    import os as _os, json as _json
    raw = _os.environ.get("GROKBOT_FCM_WEB_CONFIG") or ""
    vapid = _os.environ.get("GROKBOT_FCM_VAPID_KEY") or ""
    try:
        config = _json.loads(raw) if raw else {}
    except ValueError:
        config = {}
    if not isinstance(config, dict) or not config.get("apiKey") or not vapid:
        self._send_json({"error": "push belum dikonfigurasi"}, status=503)
        return
    self._send_json({**config, "vapidKey": vapid})
elif path == "/api/push/tokens":
    # Metadata SAJA — nilai token tidak pernah ke deck.
    self._send_json({"tokens": state.push_tokens.public_list()})
```

## 4. Endpoint POST baru (di `do_POST`)

**PENTING — nama field:** server menolak body yang berisi
field bernama `token` (`REJECTED_FIELDS` di `server.py`).
Karena itu body registrasi memakai **`push_token`**, bukan
`token`. Jangan ganti nama ini.

```python
elif path == "/api/push/register":
    # Body: {character_id, device_label?, platform?, push_token}
    data = self._allow(obj, ["character_id", "device_label",
                             "platform", "push_token"])
    if data.get("character_id") is not None and \
            state.find_character(data["character_id"]) is None:
        self._send_json({"error": "unknown character"}, status=400)
        return
    try:
        record = state.push_tokens.register(
            data.get("push_token"), data.get("character_id"),
            device_label=data.get("device_label") or "",
            platform=data.get("platform") or "web")
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    # Kembalikan metadata saja — nilai token tidak di-echo.
    self._send_json({"token": {k: v for k, v in record.items()
                               if k != "token"}})
elif path == "/api/push/unregister":
    data = self._allow(obj, ["push_token"])
    removed = state.push_tokens.unregister(data.get("push_token") or "")
    self._send_json({"removed": removed})
```

## 5. Hook: approval diminta (di `do_POST`, `/api/approvals/request`)

Sesudah `self._send_json({"approval": record}, status=201)`
— panggil hook SEBELUM `return` tidak dimungkinkan karena
respons sudah terkirim; letakkan pemanggilan tepat sebelum
`_send_json`, best-effort, dan jangan biarkan push menggagalkan
request approval:

```python
try:
    notify_approval_requested(record, registry=state.push_tokens,
                              sender=state.push_sender)
except Exception:
    pass  # push best-effort: approval tetap tercatat
```

## 6. Hook: kartu selesai (di `do_POST`, `/api/kanban/move`)

Sesudah `move_card` berhasil dan sebelum `_send_json`,
hanya bila status baru `done`:

```python
if card is not None and card.get("status") == "done":
    try:
        notify_task_done(card, registry=state.push_tokens,
                         sender=state.push_sender)
    except Exception:
        pass  # push best-effort: card tetap pindah
```

## 7. Client deck

Tempel snippet dari `PUSH-CLIENT.md` ke `app/static/app.js`
(panggil `registerPush(characterId)` sekali sesudah deck
termuat). Jangan ubah struktur deck yang sudah ada — hanya
tambah pemanggilan registrasi.

## 8. Verifikasi sesudah ditempel

```bash
python3 -m unittest discover -s push/tests -v
python3 -m unittest discover -s app/tests -v
# Tanpa kredensial: POST /api/push/register tetap 200 (token
# tersimpan), tapi hook mengembalikan skipped_reason
# "kredensial FCM belum dikonfigurasi" dan tidak ada yang terkirim.
# Dengan kredensial (lihat PUSH-SETUP.md): minta satu approval
# -> notifikasi masuk ke perangkat terdaftar.
```
