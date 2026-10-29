# Runner MVP — Bukti Test (29 Okt 2026)

Test dilakukan di PC blackarch-pc, Runner di direktori terpisah
(`/home/liveuser/runner-mvp/`), server live 8780 TIDAK pernah
di-restart, diganti, atau disentuh filenya. Runner hanya klien HTTP
ke server live + router model 3001.

## Keadaan awal (pra-test, terverifikasi)

- Live server health: `{"level": "ok", ...}`
- Team `controlled_by: bot`, `using_bot: Xman`, `browser_open: true`
- Tab CDP: `Analisis channel - YouTube Studio`
  (`.../channel/UCbgUPR603B3SfEe-KIpxaSg/analytics/tab-overview/period-default`)
- Transfer file: `runner_service.py` 28512 byte, sha256 VM = PC
  (`26e71f60…e3`, sebelum perbaikan grace; `c3d877c7…fc` sesudahnya)

## Test 1 — status komputer (tanpa ubah state)

Perintah (dari PC):

```
POST /api/rooms {"name":"runner-test","members":["xm"],"mode":"chat"}
→ {"room": {"id": "runner-test", ...}}

POST /api/messages {"room":"runner-test","character":"xm","role":"user",
  "text":"RUNNER: Cek status komputer tim (browser_open dan
  controlled_by) dan laporkan singkat, tanpa mengubah apa pun.
  Jangan navigasi atau klik."}
→ {"message": {"id": 23, ...}}
```

Hasil (log Runner + `/jobs` + riwayat room):

```
[runner] claim runner-test:23 in room runner-test
[runner] tool computer_status (room runner-test)
[runner] job runner-test:23 -> done (tools: 1)
```

Balasan asisten (id 24) di `GET /api/chat?room=runner-test`:

> **Status Komputer Tim:**
> - **controlled_by:** bot (Xman)
> - **browser_open:** true
> - **URL aktif:** YouTube Studio → Analytics (tab overview)
> - **Layar tersedia:** ya (usia layar ~22,8 menit)
>
> Browser sedang dibuka dan dikendalikan oleh bot. Tidak ada
> perubahan yang dilakukan.

Semua fakta benar vs state live. Model `auto` via router 3001
terbukti bisa `tool_calls` (bukan sekadar jawab teks).

## Test 2 — baca tab browser (list-elements, tanpa klik/navigasi)

Trigger (id 25): `RUNNER: Sebutkan judul halaman dan 5 elemen
pertama dari tab browser tim saat ini (pakai list-elements), tanpa
klik atau navigasi.`

Hasil: job `done (tools: 1)`. Balasan asisten (id 26):

> **Judul Halaman:** Analisis channel - YouTube Studio
>
> **5 Elemen Pertama:**
> 1. **Luaskan menu** (ytcp-icon-button)
> 2. **Penelusuran** (ytcp-icon-button)
> 3. **Bantuan** (ytcp-icon-button)
> 4. **Notifikasi** (ytcp-icon-button)
> 5. **Minta Studio** (button)
>
> Tidak ada klik atau navigasi yang dilakukan.

## Bug yang ketemu saat test + perbaikan

Gejala: Runner pertama (poll 10 dtk, grace 20 dtk) hidup + sehat
tapi tidak pernah mengklaim trigger 23 meski umurnya sudah >100
detik. Penyebab: optimasi poll — room di-skip bila
`message_count` tidak berubah. Trigger yang pertama terlihat saat
umurnya < grace ikut ter-skip selamanya (tidak pernah di-scan
ulang). Perbaikan: set `pending` — room yang punya trigger
menunggu grace selalu di-fetch ulang; trigger yang sudah dijawab
atau diklaim dikeluarkan dari pending; klaim unknown-character
disimpan sebagai `skipped_unknown_character` supaya tidak diproses
berulang. Setelah perbaikan + restart, trigger 23 langsung diklaim
dan selesai dalam ~3 detik.

## Insiden restart (dicatat jujur)

Restart pertama ke konfigurasi produksi gagal bind: `OSError
[Errno 98] Address already in use` — runner config test masih
hidup karena kill pertama salah sasaran (PID wrapper shell, bukan
PID python runner; pola pgrep ikut cocok dengan command line shell
itu sendiri). Perbaikan: kill memakai PID eksak proses python
(`2565822`, terverifikasi dari `ps` sebagai proses
`python3 runner_service.py --status-port 8791 --poll 10 ...`),
lalu relaunch. Pelajaran: jangan kill runner pakai pola pgrep;
pakai PID eksak dari `ps`.

## Keadaan akhir (pasca-test, terverifikasi)

- Live 8780 health: ok
- Team `controlled_by` tetap `bot`, `using_bot` Xman,
  `browser_open` true, URL tab tidak berubah (masih Analytics)
- Test room dihapus: `DELETE /api/rooms/runner-test` →
  `{"deleted": "runner-test"}`; daftar room tidak lagi memuatnya.
  Room lain (general, meeting-room, testhapus, xm, yt-manager,
  yt-comment-reply, yt-analytics) tidak disentuh.
- Klaim tersimpan: `runner-test:23 -> done`,
  `runner-test:25 -> done` (tidak akan diulang walau restart).
- Runner PRODUKSI sedang jalan (bukan config test):
  start 05:06:40 UTC, poll 15 dtk, grace 90 dtk, status
  `0.0.0.0:8791`, log `/tmp/runner-mvp.log`, klaim
  `/home/liveuser/runner-mvp/runner-claims.json`,
  `model key present: yes` (nilai kunci tidak pernah tercetak).

## Yang TIDAK diuji (sengaja)

- Gate `controlled_by=user`: state kontrol live TIDAK dibalik
  untuk test (berisiko mengganggu sesi Xm yang sedang jalan).
  Gate diuji lewat: (a) pre-check status di kode + dibaca live
  (`controlled_by=bot` saat test tool jalan), (b) backstop server
  409 bila actor salah. Membalik kontrol live hanya untuk test
  tidak sebanding risikonya.
- Notifikasi push + progres live di app: memang tidak ada di MVP
  (lihat RUNNER-DIRECTIONS.md §7).
- Beban poll jangka panjang + room dengan ribuan pesan: belum
  diuji (MVP, 7 room).

## Chief-only (29 Okt 2026, lanjutan — atas perintah Sam)

Perubahan: Runner hanya menjalankan trigger yang datang lewat
Chief (`RUNNER_CHIEF`, default `xm`). Syarat: character pesan
trigger (fallback: member pertama room) harus == chief; trigger
dari room bot lain langsung di-skip + dicatat
`skipped_not_chief` (tanpa menunggu grace), tidak pernah
dijalankan. Deploy: salinan VM = PC (sha256 `868fbc4e…e3`),
restart pakai kill PID eksak (`2578077`, diverifikasi dari `ps`
— bukan pola pgrep), relaunch sama, health baru:
`{"chief_only": true, "chief": "xm", ...}`.

### Tes negatif — trigger dari room sub-bot diabaikan

- Room `chief-test-sub` (members `yt-analytics`), pesan id 27
  character `yt-analytics`: `RUNNER: Jawab tes negatif saja,
  tanpa tool.`
- Log: `[runner 05:23:47] skip trigger chief-test-sub:27: not
  chief (character 'yt-analytics')` — 14 detik setelah posting,
  tanpa menunggu grace. Tidak ada job, tidak ada balasan asisten
  di room itu.

### Tes positif — trigger dari room Chief jalan

- Room `chief-test-chief` (members `xm`), pesan id 28 character
  `xm`: `RUNNER: Jawab singkat: status komputer tim apa
  (controlled_by dan browser_open)? Tanpa navigasi atau klik.`
- Log: `waiting grace (age 14s < 90s)` 05:23:47 → `claim
  chief-test-chief:28` 05:25:17 → `tool computer_status` 05:25:19
  → `job -> done (tools: 1)` 05:25:20.
- Balasan asisten (id 29), fakta benar vs state live:
  `controlled_by: bot`, `browser_open: ya (true)` — URL YouTube
  Studio Analytics.

### Bersih-bersih + live tidak tersentuh

- Kedua room test dihapus (`{"deleted": ...}`); daftar room
  kembali ke 7 awal (general, meeting-room, testhapus, xm,
  yt-manager, yt-comment-reply, yt-analytics).
- Live 8780 health ok, `controlled_by` tetap `bot` (Xman),
  `browser_open` true, tab tidak berubah.

## Delegasi @sub-bot (29 Okt 2026, lanjutan — atas perintah Sam)

Perubahan: tugas Chief boleh diawali `@<sub-bot>` (id atau nama).
Turn jalan dengan catatan peran sub-bot; hasil diposting ke room
sub-bot (diawali "Perintah dari Chief: ...") + salinan terusan ke
room Chief. Target tak dikenal → pesan error ke room Chief, model
tidak dijalankan. Deploy: salinan VM = PC (sha256 `66d1d540…59`),
restart pakai kill PID eksak (`2634687`), health baru:
`{"chief_only": true, "chief": "xm", "delegation": true, ...}`.
Unit parse diuji lokal (6 kasus: id, nama tanpa strip, @xm =
tugas Chief biasa, target tak dikenal, @ tanpa tugas, tugas biasa
— semua benar).

### Tes D1 — delegasi jalan, hasil dobel-posting

- Character tes `tes-sub` dibuat (focus day-to-day) + room
  `tes-sub` (members `tes-sub`) + room Chief `chief-d1`.
- Trigger (id 27, character xm): `RUNNER: @tes-sub Jawab singkat:
  status komputer tim apa (controlled_by dan browser_open)?
  Tanpa navigasi atau klik.`
- Log: `waiting grace` 05:35:58 → `claim chief-d1:27` 05:37:29 →
  `tool computer_status` 05:37:30 → `job -> done (tools: 1
  delegated to tes-sub)` 05:37:32.
- Room `tes-sub` menerima asisten (id 30, character tes-sub):
  "Perintah dari Chief: ..." + fakta benar (controlled_by bot
  Xman, browser_open true, URL Studio Analytics). Room `chief-d1`
  menerima asisten (id 31, character xm): "Diteruskan ke tes-sub
  — hasil lengkap juga ada di room tes-sub." + hasil penuh.

### Tes D2 — target tak dikenal ditolak tanpa model

- Trigger (id 28, character xm): `RUNNER: @botngawur tes`.
- Log: `claim chief-d2:28` 05:37:32 → `job -> failed (tools: 0)`
  — tidak ada pemanggilan tool/model. Room `chief-d2` menerima
  asisten (id 32): "Runner: target @botngawur tidak dikenal.
  Sub-bot yang bisa diperintah: @tes-sub, @yt-analytics,
  @yt-comment-reply, @yt-manager. ..."

### Tes D3 — regresi tugas Chief biasa

- Trigger (id 29, character xm): `RUNNER: Jawab ok saja.`
- Log: `claim chief-d3:29` 05:37:32 → `job -> done (tools: 0)`.
  Room `chief-d3` menerima asisten (id 33): "Ok." — tanpa
  delegasi, tanpa posting ke room lain.

### Bersih-bersih + live tidak tersentuh

- 4 room test dihapus + character `tes-sub` dihapus
  (`with_memory=false`, `messages_purged: 0` — room sudah
  dihapus duluan); daftar room kembali ke 7 awal, daftar
  character kembali ke 4 awal (xm, yt-analytics, yt-comment-reply,
  yt-manager).
- Live 8780 health ok, `controlled_by` tetap `bot` (Xman),
  `browser_open` true sepanjang tes.

## Cara stop (bila Sam tidak mau Runner jalan)

```
PID=$(ps aux | awk '$11 == "python3" && $12 == "runner_service.py" {print $2}')
kill $PID
```

(Ambil PID dari `ps`, bukan pola pgrep — lihat insiden restart
di atas.)
