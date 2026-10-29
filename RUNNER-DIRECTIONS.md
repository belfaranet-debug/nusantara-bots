# Runner Server-Side — Directions (MVP)

Dokumen kerja (bukan deliverable user). Bahasa: Indonesia.
Tujuan: bot Nusantara Bots tetap jalan walau Sam keluar dari app HP —
seperti Muse yang jalan di server, bukan di HP.

## 1. Masalahnya

Loop agen saat ini jalan **di app HP** (ChatScreen + `byo.ts` +
`botTools`/`browserTools`). App yang manggil model API, app yang
mengeksekusi tool lewat server. Begitu Android mem-background app,
soket putus, turn mati dengan "Model API request failed (network
error)". Server (8780) sendiri tidak pernah mati — yang mati adalah
turn di HP.

Bukti dari investigasi (29 Okt 2026):

- Server **tidak pernah** memanggil model beneran. `POST /api/chat`
  itu jalur mock. Jalur BYO adalah `POST /api/messages` ("persist
  path"): app sudah memanggil model sendiri, lalu hanya menyimpan
  pesan jadi (user/assistant) ke server.
- Riwayat hidup di server (`data/chat.json`, `ChatStore`): rooms +
  messages (`id` urut, `room`, `character`, `role`, `text`,
  `created_at`, `reply_to`). App mengambilnya lewat
  `GET /api/chat?room=<id>` tiap buka room, dan `GET /api/rooms`
  untuk daftar room.
- Alur kirim app (ChatScreen): `persistMessage(user)` **dulu**,
  baru loop model, lalu `persistMessage(assistant)` di akhir. Jadi
  pesan user yang dikirim lalu app di-background **sudah ada** di
  server — Runner bisa melihatnya tanpa perubahan APK.
- Tool komputer lewat HTTP server: `POST /api/computer/open`,
  `click`, `click-viewport`, `type`, `scroll`, `list-elements`,
  `GET /api/computer/status`, `GET /api/computer/screen`. Gate:
  `actor=bot` hanya jalan kalau `controlled_by=bot`; kalau user
  yang megang, server menolak 409. Runner **tidak boleh** merebut
  kontrol (tidak pernah memanggil take-control/release-control).
- Tidak ada runner di server sebelumnya: `pulse.py` = komposer draft
  mingguan, `triage.py` = klasifikasi mail, `mock_model.py` = balasan
  mock. Runner ini komponen baru.
- `list-elements` adalah "mata" text-only: max 30 elemen
  (tag/label/href/x/y viewport) + url + title halaman aktif. Klik
  elemen pakai `click-viewport` (koordinat viewport, tanpa offset
  chrome/DPR — ini yang benar; klik layar mentah rawan meleset).

## 2. Apa itu Runner

Service Python mandiri (stdlib saja, satu file:
`server/runner_service.py`) yang jalan **di PC**, terpisah dari
server live 8780. Ia murni klien HTTP:

- ke server live (baca rooms/chat, tulis pesan, pakai tool komputer),
- ke model (OpenAI-compatible `POST /v1/chat/completions` ke router
  Sam AI 3001, model `auto`).

Runner TIDAK mengimpor kode server live, TIDAK menyentuh file
`data/`/`characters/`/`devices/`, TIDAK mengubah server 8780, dan
TIDAK menyimpan kredensial apa pun. Kunci model dibaca saat runtime
dari environment (`SAMAI_API_KEY`) atau dari file env
(`~/.hermes/.env`, seperti pola `key_env` Hermes) — nilainya tidak
pernah ditulis ke log, file, atau laporan.

## 3. Alur turn (MVP)

```
Sam kirim "RUNNER: <tugas>" di chat Chief (Xm) — perintah dari
room bot lain diabaikan (Chief-only, 29 Okt 2026)
  → app persist pesan user ke server (POST /api/messages)
  → Sam tutup app (loop app mati — tidak masalah)
  → Runner poll GET /api/rooms + GET /api/chat tiap 15 dtk
  → ketemu pesan trigger tanpa balasan asisten sesudahnya
  → tunggu GRACE 90 dtk (memberi kesempatan loop app menjawab
    kalau app ternyata masih terbuka)
  → cek ulang: kalau sudah ada balasan asisten, lewati (no-op)
  → klaim job (file klaim di direktori runner, bukan data user)
  → kalau tugas diawali "@<sub-bot>": jalankan turn sebagai
    sub-bot itu, posting hasil ke room sub-bot + salinan ke
    room Chief (delegasi, 29 Okt 2026); tanpa "@<target>":
    hasil hanya di room Chief
  → cek GET /api/computer/status (scope team):
      kalau controlled_by != bot → lewati tool, jawab dari teks
  → loop model+tool server-side (max 8 ronde):
      model → tool_calls → eksekusi via /api/computer/* → hasil
      → model lagi … sampai jawaban teks final
  → cek ulang balasan (hindari duplikat balapan): kalau ada balasan
    asisten baru sesudah trigger, BUANG hasil (jangan posting)
  → POST /api/messages {room, character, role: assistant, text}
  → Sam buka room lagi → balasan sudah tampil (app fetch /api/chat)
```

Karakter balasan = `character` pesan trigger (app selalu mengisi
bot yang diajak chat); fallback = member pertama room. Room yang
diproses hanya `mode=chat` (room meeting dilewati karena circuit
breaker 40 pesan). Pesan trigger yang sudah diklaim tidak pernah
dijalankan ulang, termasuk setelah runner restart.

## 4. Trigger (tanpa perubahan APK)

- Prefix: `RUNNER:` di awal pesan user (case-insensitive, spasi
  sesudah prefix opsional). Contoh: `RUNNER: Baca Analytics channel
  Jejak Hikmah dan laporkan subscriber + 3 Shorts teratas.`
- **Chief-only (29 Okt 2026, atas perintah Sam):** trigger hanya
  berlaku bila character pesan (fallback: member pertama room)
  == chief (default `xm`). Trigger dari room bot lain langsung
  di-skip + dicatat `skipped_not_chief` (tanpa menunggu grace),
  tidak pernah dijalankan. Rantai komando: Sam → Chief (Xm) →
  sub-bot; Runner hanya mengeksekusi perintah yang datang lewat
  Chief.
- **Delegasi (29 Okt 2026):** tugas Chief boleh diawali
  `@<sub-bot>` (id atau nama bot, mis. `@yt-analytics`). Turn
  jalan dengan catatan peran sub-bot itu; hasil diposting ke
  room sub-bot (diawali "Perintah dari Chief: ...") + salinan
  terusan ke room Chief. Target tak dikenal → pesan error ke
  room Chief (daftar sub-bot valid), model tidak dijalankan.
  Room sub-bot dicari lewat `GET /api/rooms` (id == target, atau
  members == [target]); kalau tidak ada, hasil hanya di room
  Chief + catatan. Pesan hasil ber-role assistant, jadi tidak
  pernah menjadi trigger baru (tidak ada loop).
- Syarat klaim: (a) role=user, (b) teks diawali prefix,
  (c) tidak ada pesan assistant sesudahnya di room itu,
  (d) umur pesan ≥ grace (default 90 dtk) — diukur dari
  `created_at`, fallback dari pertama kali runner melihat pesan itu.
- Poll memakai `message_count` dari daftar room untuk melewati
  room yang tidak berubah — KECUALI room yang punya trigger yang
  sedang menunggu grace (pending set). Tanpa pengecualian ini,
  trigger yang pertama terlihat saat umurnya < grace tidak pernah
  di-scan ulang (bug yang ketemu saat test, sudah diperbaiki;
  lihat `server/runner-mvp-test.md`).
- Scope komputer: `team` (default, bisa diubah via env). Runner
  tidak pernah memanggil take-control/release-control/login-vault.
- Tool MVP: `computer_status`, `computer_open(url)`,
  `computer_list_elements`, `computer_click_viewport(x, y)`,
  `computer_type(text)`, `computer_scroll(direction, amount)`.
  Tool `login` (vault) sengaja TIDAK disediakan — Runner tidak
  pernah login pakai kredensial atas nama Sam.

## 5. Konfigurasi (env)

| Variabel | Default | Arti |
|---|---|---|
| `RUNNER_SERVER` | `http://127.0.0.1:8780` | Server live |
| `RUNNER_SERVER_TOKEN` | (kosong) | Header `X-Grokbot-Token` bila server kelak butuh token |
| `RUNNER_MODEL_BASE_URL` | `http://127.0.0.1:3001/v1` | Router Sam AI |
| `RUNNER_MODEL` | `auto` | Model router |
| `RUNNER_ENV_FILE` | `~/.hermes/.env` | File env tempat kunci dibaca |
| `RUNNER_STATUS_PORT` | `8791` | Port status (0.0.0.0, hanya baca) |
| `RUNNER_POLL_SECONDS` | `15` | Interval poll |
| `RUNNER_GRACE_SECONDS` | `90` | Grace sebelum klaim |
| `RUNNER_TRIGGER` | `RUNNER:` | Prefix trigger |
| `RUNNER_CHIEF` | `xm` | Satu-satunya character yang perintahnya dijalankan (Chief-only) |
| `RUNNER_SCOPE` | `team` | Scope komputer |
| `RUNNER_MAX_ROUNDS` | `8` | Max ronde model+tool |
| `RUNNER_CLAIM_FILE` | `./runner-claims.json` | File klaim job |

Status: `GET /health` (ok + uptime + config tanpa secret),
`GET /jobs` (job terakhir: room, cuplikan tugas, status, waktu).
Log: stdout (arahkan ke file saat nohup). Log tidak pernah berisi
kunci, dan teks tugas dipotong max 120 karakter.

## 6. Reboot PC

Runner TIDAK auto-start. Setelah reboot PC, Runner mati sampai
dijalankan lagi (server 8780, websockify 6081, dan Chromium :100
juga mati saat reboot — itu pekerjaan rumah terpisah). Start:

```
cd /home/liveuser/runner-mvp
setsid nohup python3 runner_service.py > /tmp/runner-mvp.log 2>&1 < /dev/null &
```

Stop (PID eksak dari `ps`, JANGAN pola pkill — pola pgrep ikut
cocok dengan command line shell wrapper dan pernah salah kill):

```
PID=$(ps aux | awk '$11 == "python3" && $12 == "runner_service.py" {print $2}')
kill $PID
```
Service systemd permanen = follow-up, butuh persetujuan Sam (belum
dibuat di MVP ini).

## 7. Batasan MVP (jujur)

1. **Tanpa notifikasi push.** Sam tahu job selesai hanya saat
   membuka room. Notifikasi butuh perubahan app / push infra.
2. **Tanpa progres live di app.** Indikator "thinking" abu-abu itu
   lokal app (hanya mencerminkan loop app sendiri); progres Runner
   tidak tampil di app sampai balasan final terposting.
3. **Race double-run tidak 100% hilang.** Kalau app terbuka DAN
   loop app lambat (> grace) DAN Runner mengklaim, keduanya bisa
   menjawab. Mitigasi: grace + klaim + cek-ulang-sebelum-posting.
   Pola pakai yang aman: kirim trigger, lalu tutup app.
4. **Trigger prefix kasar.** `RUNNER:` terlihat di chat dan harus
   diketik manual. Tombol rapi butuh perubahan app (lihat §8).
5. **Model `auto` gratis bisa lemah tool-calling.** Kalau model
   tidak mengembalikan `tool_calls`, Runner menjawab dari teks
   (tanpa tool) dan melaporkannya — bukan diam-diam gagal. Model
   spesifik bisa dipilih via `RUNNER_MODEL` bila terbukti lebih baik.
6. **Hanya scope team + room mode chat.** Scope private dan room
   meeting tidak diproses MVP.
7. **Tidak ada antrean prioritas / paralel.** Job jalan satu per
   satu, berurutan. Job gagal dicatat + diposting sebagai pesan
   kegagalan singkat (bukan diam).
8. **Konteks terbatas.** Runner mengirim max 12 pesan terakhir room
   sebagai konteks (tiap pesan dipotong 1000 karakter) + system
   prompt. Riwayat sangat panjang tidak penuh terkirim.

## 8. Integrasi penuh (perlu perubahan app — spesifikasi, BUKAN dibangun di MVP)

Agar UX-nya setara Muse (tanpa prefix, ada progres + notifikasi),
perubahan app minimal yang dibutuhkan:

1. **ChatScreen — mode kirim server-job.** Tambah aksi kirim
   (mis. long-press tombol kirim → "Jalankan di server") yang:
   POST pesan user seperti biasa, lalu **melewati loop lokal**
   (tidak memanggil model dari HP) dan menandai room sedang
   menunggu job server.
2. **Poller hasil.** Saat room dalam mode tunggu-job, app poll
   `GET /api/chat?room=` berkala; saat balasan asisten baru muncul,
   tampilkan + (opsional) notifikasi lokal.
3. **Opsional (lebih rapi): endpoint job khusus**
   `POST /api/runner/jobs {room, character, task}` + status job di
   Runner (`GET /jobs`), supaya app tidak perlu mengandalkan
   prefix teks dan grace period. Ini butuh perubahan server juga
   (endpoint baru di server.py) — dilakukan hanya bila Sam
   menyetujui setelah MVP terbukti.

Tanpa ketiga itu, MVP tetap jalan penuh lewat prefix `RUNNER:`.

## 9. Cara pakai (untuk Sam)

1. Pastikan Runner jalan di PC (cek dari PC: `curl
   http://127.0.0.1:8791/health` — harus `ok`; dari HP pakai
   alamat PC kamu).
2. Di chat Chief (Xm) — BUKAN di chat sub-bot — kirim:
   `RUNNER: <tugas lo>`. Untuk sub-bot tertentu, awali tugas
   dengan `@id-bot`, mis. `RUNNER: @yt-analytics baca Analytics
   channel dan laporkan subscriber + 3 Shorts teratas.` (hasil
   lengkap di room sub-bot, salinan di room Chief).
3. Tutup app. Bot kerja di server.
4. Buka lagi room itu nanti — balasan Runner sudah ada.
5. Kalau job gagal, balasan Runner akan bilang gagalnya kenapa
   (bukan diam). Kalau kontrol komputer sedang Sam pegang, Runner
   tidak memakai tool dan menjawab dari teks saja.

## 10. File

- `server/runner_service.py` — service Runner (stdlib saja).
- `server/runner-mvp-test.md` — bukti test 29 Okt 2026 (termasuk
  bug grace + insiden restart).
- Deploy PC (bukan repo live): `/home/liveuser/runner-mvp/`
  (salinan file service + `runner-claims.json` + log
  `/tmp/runner-mvp.log`).
