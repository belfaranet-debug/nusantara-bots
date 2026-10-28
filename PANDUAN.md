# Panduan Pengguna Baru — Nusantara Bots

Panduan singkat buat kamu yang baru install Nusantara Bots. Ikuti nomor 1-2-3, selesai.

## Yang kamu butuhkan

- HP Android + APK Nusantara Bots (download dari [Releases](https://github.com/sam666-byte/nusantara-bots/releases))
- API key dari provider AI (buat ngobrol sama bot) — lihat Langkah 2
- (Opsional) Server pendamping di PC — lihat Langkah 3

---

## Langkah 1 — Install APK

1. Download APK dari halaman [Releases](https://github.com/sam666-byte/nusantara-bots/releases):
   - `nusantara-bots-0.10.16-arm64.apk` — untuk HP Android (kebanyakan HP).
   - `nusantara-bots-0.10.16-x86_64.apk` — untuk emulator.
2. Install APK di HP, lalu buka aplikasinya.

---

## Langkah 2 — Masukkan API key model (biar bot bisa jawab)

Tanpa ini, bot tidak bisa ngobrol sama AI.

1. Buka aplikasi → **Settings → Pilih model**.
2. Pilih preset: **Agnes**, **Poolside Laguna**, **Sam AI**, atau **Big Pickle**.
3. Masukkan **API key** kamu dari provider itu.
4. Selesai — bot sudah bisa ngobrol.

**Dapat API key dari mana?** Daftar di dashboard provider yang kamu pilih (misalnya Agnes atau Poolside), lalu copy API key-nya. Key disimpan hanya di HP kamu (SecureStore) dan tidak pernah dikirim ke mana-mana selain ke provider yang kamu pilih.

---

## Langkah 3 (opsional) — Sambungkan ke server di PC

Langkah ini **opsional**. Kamu hanya butuh server kalau mau fitur bot, rooms, memory, MCP, dan Team Computer. Kalau cuma mau chat model langsung (BYO), Langkah 1 + 2 saja sudah cukup.

1. Deploy server pendamping di PC — kodenya ada di folder [`server/`](server/README.md) di repo ini. Clone repo, masuk ke folder `server/`, lalu jalankan (lihat [server/README.md](server/README.md) untuk cara lengkap):
   ```bash
   git clone https://github.com/sam666-byte/nusantara-bots.git
   cd nusantara-bots/server
   GROKBOT_APP_HOST=0.0.0.0 GROKBOT_APP_OPEN_LAN=1 GROKBOT_APP_TOKEN_FILE=/path/to/token python3 -u app/server.py
   ```
2. Copy **access token server** — token yang kamu set waktu deploy (dari file token atau env var).
3. Di aplikasi → pengaturan server → isi **Server URL** + **Access Token** → simpan.

---

## Kalau error `401 - Simpan akses token dahulu`

Artinya **access token server belum disimpan di aplikasi** (atau salah).

1. Copy access token dari server kamu (file token / env var waktu deploy).
2. Di aplikasi → pengaturan server → paste token itu di kolom access token → simpan.
3. Coba lagi — 401-nya hilang.

---

## Dua kunci, jangan tertukar

| | Token server | API key model (BYOK) |
|---|---|---|
| **Buat apa** | Izin memakai server di PC kamu | Izin memakai AI (Agnes/Poolside/dll) |
| **Didapat dari** | Waktu deploy server | Dashboard provider AI |
| **Diisi di** | Pengaturan server aplikasi | Settings → Pilih model |
| **Kalau belum diisi** | Error 401 | Bot tidak bisa ngobrol |

Keduanya harus diisi di tempat yang berbeda. Kalau bingung yang mana, lihat tabel di atas.
