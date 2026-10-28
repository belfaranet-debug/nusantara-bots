# server-wiring.patch.md — vault, untuk koordinator

Patch doc: JANGAN edit `app/server.py` dari dokumen ini.
Koordinator yang menempel potongan di bawah ke `server.py`
(builder lain sedang mengedit file itu).

## 0. Prasyarat: aksi approval "vault-release"

`approvals/queue.py` — `APPROVAL_ACTIONS` saat ini TIDAK
memuat `"vault-release"`. Tambahkan (koordinator, satu baris,
di `approvals/queue.py`):

```python
APPROVAL_ACTIONS = ("send", "publish", "merge", "deploy", "delete",
                    "change-production-data", "vault-release")
```

Tanpa ini, `POST /api/approvals/request` dengan
`action="vault-release"` ditolak dan tidak ada secret yang
bisa dirilis. Delete sudah bisa (`"delete"` ada di daftar).

## 1. Impor (di dekat impor `approvals` / `office`)

```python
from vault import VaultError, VaultStore  # noqa: E402
```

## 2. State (di `ServerState.__init__`, dekat `self.approvals`)

```python
# Next-level Phase 2 (i): encrypted credential vault. The key
# comes from GROKBOT_VAULT_KEY; when it is missing the vault
# is unavailable and its endpoints answer 503 (never a silent
# empty vault).
try:
    self.vault = VaultStore()
except VaultError:
    self.vault = None
```

## 3. Endpoint GET baru (di `do_GET`)

```python
elif path == "/api/vault/list":
    # Metadata ONLY — nama, owner, created_at, last_used.
    if state.vault is None:
        self._send_json({"error": "vault belum dikonfigurasi"},
                        status=503)
        return
    self._send_json({"secrets": state.vault.list_secrets()})
```

## 4. Endpoint POST baru (di `do_POST`)

```python
elif path == "/api/vault/put":
    # Body: {name, value, owner?}. "value" di sini adalah secret
    # itu sendiri — satu-satunya endpoint yang menerima nilai
    # kredensial, dan nilainya langsung dienkripsi, tidak pernah
    # di-log. (Field bernama "secret"/"token"/"password" tetap
    # ditolak _read_json; "value" bukan field terlarang.)
    if state.vault is None:
        self._send_json({"error": "vault belum dikonfigurasi"},
                        status=503)
        return
    data = self._allow(obj, ["name", "value", "owner"])
    try:
        meta = state.vault.put_secret(
            data.get("name"), data.get("value"),
            owner=data.get("owner") or "")
    except VaultError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"secret": meta})
elif path == "/api/vault/release":
    # Body: {name, approval_id}. Nilai yang dirilis HANYA boleh
    # dipakai eksekutor sisi-server (mis. pengirim mail) — JANGAN
    # pernah me-render hasilnya di deck dan JANGAN memanggil
    # endpoint ini dari JavaScript deck.
    if state.vault is None:
        self._send_json({"error": "vault belum dikonfigurasi"},
                        status=503)
        return
    data = self._allow(obj, ["name", "approval_id"])
    approval = state.approvals.get(data.get("approval_id") or "")
    if approval is None:
        self._send_json({"error": "unknown approval"}, status=404)
        return
    try:
        value = state.vault.release_secret(data.get("name"), approval)
    except VaultError as e:
        self._send_json({"error": str(e)}, status=403)
        return
    # Consume approval-nya SEKALI — tanpa ini approval yang sama
    # masih berstatus approved (vault menolak replay lewat audit,
    # tapi queue juga harus menandai consumed).
    try:
        state.approvals.consume(
            data["approval_id"], note="vault release %s" % data.get("name"))
    except (KeyError, ValueError):
        pass
    self._send_json({"value": value})
elif path == "/api/vault/delete":
    # Body: {name, approval_id} — hapus juga butuh approval.
    if state.vault is None:
        self._send_json({"error": "vault belum dikonfigurasi"},
                        status=503)
        return
    data = self._allow(obj, ["name", "approval_id"])
    approval = state.approvals.get(data.get("approval_id") or "")
    if approval is None:
        self._send_json({"error": "unknown approval"}, status=404)
        return
    try:
        state.vault.delete_secret(data.get("name"), approval)
    except VaultError as e:
        self._send_json({"error": str(e)}, status=403)
        return
    try:
        state.approvals.consume(
            data["approval_id"], note="vault delete %s" % data.get("name"))
    except (KeyError, ValueError):
        pass
    self._send_json({"deleted": True})
```

**Catatan keamanan untuk koordinator:** `POST /api/vault/release`
mengembalikan plaintext di respons JSON. Itu disengaja —
respons ini untuk kode eksekutor sisi-server, BUKAN untuk
browser. Jangan tambahkan tombol deck yang memanggil endpoint
ini, dan jangan pernah menampilkan `value` di UI. Bila suatu
saat eksekutor butuh secret (mis. token mailbox untuk
`mail_send`), panggil `release_secret` langsung dari kode
Python server, bukan lewat HTTP.

## 5. Verifikasi sesudah ditempel

```bash
python3 -m unittest discover -s vault/tests -v
python3 -m unittest discover -s app/tests -v
# Tanpa GROKBOT_VAULT_KEY: GET /api/vault/list -> 503
# "vault belum dikonfigurasi". Dengan key: put -> list (metadata
# saja) -> request approval vault-release -> release sekali ->
# release kedua dengan approval yang sama -> 403.
```
