<p align="center">
  <a href="https://saweria.co/Nusantaraeaters">
    <img src="assets/saweria-qr.png" alt="Dukung Nusantara Bots via Saweria — scan QR ini" width="280">
  </a>
</p>

<h1 align="center">Nusantara Bots</h1>

<p align="center"><b>Tim AI Kamu, Dalam Satu Aplikasi.</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Android-3DDC84?style=flat-square&logo=android&logoColor=white" alt="Android">
  <img src="https://img.shields.io/badge/version-0.10.16-blue?style=flat-square" alt="Version 0.10.16">
  <img src="https://img.shields.io/badge/React%20Native-0.86-61DAFB?style=flat-square&logo=react&logoColor=white" alt="React Native 0.86">
  <img src="https://img.shields.io/badge/TypeScript-3178C6?style=flat-square&logo=typescript&logoColor=white" alt="TypeScript">
</p>

<p align="center">
  Aplikasi AI agent gratis di HP Android. Buat tim bot AI kamu sendiri — ngobrol, kerjakan tugas, akses komputer, semua dari satu aplikasi.
</p>

---

## 💛 Dukung Kami

**Nusantara Bots gratis dan open source.** Kalau aplikasi ini membantumu, dukung pengembangannya seikhlasnya lewat Saweria — berapapun, tanpa minimum yang memberatkan (mulai Rp1.000).

<p align="center">
  <a href="https://saweria.co/Nusantaraeaters">
    <img src="assets/saweria-qr.png" alt="QR Saweria — Dukung Nusantara Bots" width="220">
  </a>
</p>

<p align="center">
  👉 <b><a href="https://saweria.co/Nusantaraeaters">saweria.co/Nusantaraeaters</a></b>
</p>

> "Dukung Nusantara Bots - aplikasi AI agent gratis di HP. Donasi seikhlasnya buat pengembangan aplikasi. Terima kasih!"

Donasi dipakai untuk pengembangan aplikasi. Terima kasih atas dukungannya! 🙏

---

## 📥 Download APK

**Silahkan download APK-nya** di halaman release:

👉 **[Download APK v0.10.16](https://github.com/sam666-byte/nusantara-bots/releases)**

- **`nusantara-bots-0.10.16-arm64.apk`** — untuk HP Android (ARM64, kebanyakan HP).
- **`nusantara-bots-0.10.16-x86_64.apk`** — untuk emulator (x86_64).

Install APK ke HP, lalu buka **Settings → Pilih model** dan masukkan API key kamu (BYOK).

---

## ✨ Fitur

- **🤖 Tim bot AI** — Buat banyak bot dengan karakter dan tugas masing-masing. Ngobrol dengan mereka dalam satu aplikasi.
- **🔀 Pilih model sekali ketuk** — Ganti model aktif untuk semua bot dari Settings. Preset bawaan: **Agnes**, **Poolside Laguna**, **Sam AI**, dan **Big Pickle**. Tambah provider sendiri juga bisa.
- **🔑 BYOK (Bring Your Own Key)** — Pakai API key kamu sendiri (lihat penjelasan di bawah).
- **🧰 MCP (Model Context Protocol)** — Bot bisa memakai tools: baca/tulis file, browsing, dan puluhan tools lain.
- **🖥️ Team Computer** — Lihat dan kendalikan desktop PC dari HP lewat VNC di dalam aplikasi.
- **💾 Chat tersimpan** — Riwayat chat bot tersimpan dan tidak hilang saat keluar-masuk aplikasi.
- **🛠️ Skill Creator** — Buat skill sendiri untuk bot kamu.
- **⏱️ Timer & routines** — Bot bisa diingatkan dan dijadwalkan.

## 🔑 BYOK — Bring Your Own Key

**Nusantara Bots tidak menyertakan API key apapun.** Kamu memakai API key milikmu sendiri dari provider AI yang kamu pilih. Begini cara kerjanya:

1. **Daftar ke provider AI** pilihanmu (misalnya Agnes, Poolside, atau provider lain yang kompatibel dengan API OpenAI).
2. **Dapatkan API key** dari dashboard provider tersebut.
3. **Di aplikasi**, buka **Settings → Pilih model**, pilih preset (Agnes / Poolside Laguna / Sam AI / Big Pickle) atau tambah provider kustom dengan base URL + API key kamu.
4. **Selesai.** Semua bot langsung memakai model dan key tersebut.

**Keamanan key kamu:**

- Key disimpan **hanya di HP kamu**, di SecureStore (penyimpanan terenkripsi perangkat).
- Key **tidak pernah** dikirim ke server aplikasi dan **tidak pernah** masuk ke chat, log, atau riwayat.
- Key **tidak pernah** ada di repo ini — repo ini 100% bersih dari API key, token, dan kredensial apapun.
- Kalau ganti HP atau hapus aplikasi, key ikut terhapus — kamu tinggal masukkan lagi.

> 💡 **Tanpa key sendiri, aplikasi tidak bisa ngobrol dengan model AI.** Itu disengaja: key adalah milikmu, bukan milik aplikasi.

## 🚀 Cara Setup

### Yang dibutuhkan

- Node.js 18+ dan npm
- JDK 17 dan Android SDK (untuk build APK)
- HP Android (atau emulator) untuk menjalankan aplikasi
- (Opsional) Server pendamping Nusantara Bots di PC kamu, untuk fitur bot, rooms, memory, MCP, dan Team Computer. Server ini komponen terpisah — aplikasi bisa tetap dipakai untuk chat model langsung (BYO) tanpa server.

### Build APK

```bash
# 1. Clone repo ini
git clone https://github.com/<username>/nusantara-bots.git
cd nusantara-bots

# 2. Install dependencies
npm install

# 3. Build APK debug
cd android
./gradlew assembleDebug

# APK ada di: android/app/build/outputs/apk/debug/app-debug.apk
```

Untuk build release, buat keystore sendiri dan konfigurasikan signing di `android/app/build.gradle` (jangan pernah commit keystore ke repo).

### Jalankan di HP

1. Install APK ke HP Android.
2. Buka aplikasi → **Settings → Server URL** — isi alamat server pendamping kamu (kalau ada), contoh: `http://your-server:8780`. Kalau tidak ada server, kosongkan saja untuk chat model langsung.
3. Buka **Settings → Pilih model** — pilih preset dan masukkan API key kamu (BYOK).
4. Buat bot pertama dan mulai ngobrol.

### Access token server (kalau pakai server pendamping)

Kalau server pendamping di-deploy supaya bisa diakses dari HP (bind LAN), server **wajib pakai access token**. Ini pengaman supaya hanya HP kamu yang bisa memakai server itu.

**Di server** — set token lewat file atau env var saat menjalankan server:

```bash
GROKBOT_APP_HOST=0.0.0.0 GROKBOT_APP_OPEN_LAN=1 GROKBOT_APP_TOKEN_FILE=/path/to/token python3 -u app/server.py
```

atau:

```bash
GROKBOT_APP_HOST=0.0.0.0 GROKBOT_APP_OPEN_LAN=1 GROKBOT_APP_TOKEN=<token-rahasia> python3 -u app/server.py
```

Token tersimpan di luar repo (file `chmod 600` atau env var) dan tidak pernah di-print di log — buka file-nya langsung untuk melihat isinya.

**Di aplikasi** — buka pengaturan server, isi kolom access token dengan token dari server, lalu simpan. Aplikasi mengirim token itu lewat header `X-Grokbot-Token` di setiap request.

**Kalau muncul `401 - Simpan akses token dahulu`:** artinya token belum disimpan di aplikasi (atau salah). Masukkan token server yang benar di pengaturan server, simpan, lalu coba lagi.

> 💡 Token akses server ini **beda** dengan API key model (BYOK). Token server = izin memakai server pendamping kamu. API key model = izin memakai model AI. Kamu butuh keduanya kalau pakai server + chat model.

### Tes

```bash
npm test        # jest — 575 tes
npx tsc --noEmit  # type check
```

## 🏗️ Arsitektur

- **React Native 0.86 + React Navigation** — aplikasi Android (build manual via Gradle, bukan Expo).
- **`src/api/`** — client HTTP ke server pendamping + client model (OpenAI-compatible).
- **`src/api/modelProfiles.ts`** — preset model (Agnes, Poolside, Sam AI, Big Pickle).
- **`src/api/providers.ts`** — daftar provider & model.
- **`src/api/client.ts`** — client server (bot, rooms, chat, memory, vault).
- **`src/components/` & `src/screens/`** — UI aplikasi.
- **`android/`** — kode native Android + Gradle (manual, bukan hasil generate Expo).

## 🤝 Kontribusi

Kontribusi dipersilakan! Silakan buka issue atau pull request. Pastikan `npm test` dan `npx tsc --noEmit` lolos sebelum mengirim PR.

## 📄 Lisensi

MIT License — lihat file [LICENSE](LICENSE) (Copyright 2026 Samir Jamal).

---

<p align="center">
  Dibuat dengan ❤️ untuk pengguna AI agent di Indonesia.<br>
  <a href="https://saweria.co/Nusantaraeaters">💛 Dukung via Saweria</a>
</p>
