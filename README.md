# WhatsApp Project Hub, Explorer & Loader

Sistem manajemen, pencarian, dan visualisasi obrolan WhatsApp multi-proyek terintegrasi dengan **Supabase Postgres (Database & RLS)** dan **Google Drive (Penyimpanan Media)**.

---

## 🚀 Fitur Utama

Aplikasi web memiliki **Main Headbar** dengan 3 tab navigasi:

1. **📊 Dashboard (In Progress)**
   - Indikator status pengembangan fitur analitik.
   - Ringkasan KPI: Total proyek aktif, total grup terhubung, jumlah pesan, dan status penyimpanan cloud.
   - Rencana fitur roadmap: visualisasi tren chat, kontributor teraktif, dan rangkuman AI.

2. **🔍 Explorer**
   - Penelusuran pesan dan berkas media obrolan WhatsApp secara real-time.
   - Filter lengkap: Proyek, Grup WhatsApp, Pengirim, Rentang Tanggal, Jenis Media (Foto, Dokumen, Video, Audio), dan Pesan Sistem.
   - Pratinjau thumbnail gambar langsung dari Google Drive.
   - Tombol *Lompat ke Konteks* (`konteks ↗`) untuk melihat pesan dalam alur percakapan aslinya.
   - Pencarian instan teks obrolan berkecepatan tinggi dengan penyorotan kata (*highlighting*).

3. **📥 Loader**
   - **Pemilih Folder / File Ekspor:** Memilih folder ekspor obrolan WhatsApp dari komputer (`_chat.txt` beserta media lampirannya).
   - **Tombol Update Chat:** Memulai proses update ke proyek yang dipilih.
   - **Asisten Perintah Terminal:** Menghasilkan perintah CLI otomatis siap salin untuk mengunggah media bergiga-byte langsung ke Google Drive.
   - **Riwayat Log Pengunggahan (Real-time dari Supabase):** Menampilkan kapan saja data diunggah, nama grup, berkas sumber, **jumlah pesan yang terupdate (`msg_new`)**, total pesan di berkas, rentang tanggal percakapan, dan status peringatan.

---

## 📁 Struktur Direktori

```text
├── web/                          # Aplikasi web statis (yang di-deploy ke Vercel)
│   ├── index.html                # Dashboard, Explorer, Loader
│   ├── config.js                 # Konfigurasi Supabase untuk web client (URL + kunci publishable)
│   └── config.example.js         # Template konfigurasi web
├── scripts/                      # CLI Python (dijalankan dari root proyek)
│   ├── wa_parser.py              # Parser chat WhatsApp (Android/iOS, multi-format)
│   ├── wa_loader.py              # Loader data ke database Supabase & Google Drive
│   └── wa_drive.py               # Otentikasi & upload Google Drive API
├── supabase/
│   └── migrations/               # Jalankan berurutan di SQL Editor Supabase
│       ├── 001_init.sql          # Skema database & aturan RLS
│       ├── 002_seq_and_bucket.sql
│       └── 003_storage_policy.sql
├── tests/                        # Unit test (unittest)
│   ├── test_wa_parser.py
│   └── test_wa_loader.py
├── data/                         # Hasil parse lokal (chat.jsonl) - tidak masuk git
├── .env.example                  # Contoh environment variable untuk CLI loader
├── requirements.txt              # Dependensi Python untuk parser & loader
└── vercel.json                   # Konfigurasi deployment (outputDirectory: web)
```

---

## 🛠️ Menjalankan CLI & Tes

Semua perintah dijalankan dari **root proyek** (bukan dari dalam `scripts/`), karena `.env`, `google_client.json`, dan `google_token.json` dibaca dari folder kerja.

```bash
pip install -r requirements.txt
cp .env.example .env            # lalu isi kredensial

python scripts/wa_parser.py "<folder-ekspor>" --out data/chat.jsonl
python scripts/wa_loader.py load data/chat.jsonl --project "<NAMA-PROYEK>" --media-dir "<folder-ekspor>" --drive

python -m unittest discover -s tests
```

---

## 💻 Menjalankan di Lokal (Port 8000)

1. Jalankan server lokal dari root proyek:
   ```bash
   python -m http.server 8000 --directory web
   ```
2. Buka browser di [http://localhost:8000](http://localhost:8000).
3. Masuk dengan email yang sudah diundang pada Supabase Auth Anda.

---

## 📤 Cara Mengunggah Proyek ke GitHub

Ikuti langkah-langkah berikut di terminal untuk mengunggah ke GitHub:

1. **Inisialisasi Git & Tambahkan Berkas:**
   ```bash
   git init
   git add .
   git commit -m "feat: tambahkan headbar dengan Dashboard, Explorer, dan Loader serta konfigurasi Vercel"
   ```

2. **Buat Repositori Baru di GitHub:**
   - Buka [github.com/new](https://github.com/new).
   - Beri nama repository (misalnya: `wa-dashboard`).
   - Pilih *Private* atau *Public*.
   - **Jangan** centang *Add a README file* (karena sudah ada).
   - Klik **Create repository**.

3. **Hubungkan Remote & Push ke GitHub:**
   ```bash
   git branch -M main
   git remote add origin https://github.com/<USERNAME-ANDA>/wa-dashboard.git
   git push -u origin main
   ```

> **Catatan Keamanan:** File `.gitignore` sudah otomatis melindungi `.env`, `google_client.json`, `google_token.json`, dan file `*.jsonl`/chat agar kredensial dan obrolan pribadi tidak terunggah ke publik.

---

## 🌐 Cara Deploy ke Vercel

1. Buka [Vercel Dashboard](https://vercel.com/dashboard) dan klik **Add New...** > **Project**.
2. Pilih repositori GitHub Anda (`wa-dashboard`).
3. Pada bagian **Framework Preset**, biarkan **Other**. Folder output (`web`) sudah diatur di `vercel.json`.
4. Klik tombol **Deploy**.
5. Tunggu proses deployment selesai hingga Anda mendapatkan domain (misalnya: `https://wa-dashboard-xyz.vercel.app`).

### ⚠️ Langkah Wajib di Supabase setelah Deploy:
Agar login OTP di Vercel berfungsi:
1. Buka [Supabase Dashboard](https://supabase.com/dashboard) > Proyek Anda.
2. Masuk ke menu **Authentication** > **URL Configuration**.
3. Di bagian **Redirect URLs**, tambahkan URL domain Vercel Anda:
   - `https://<nama-aplikasi-anda>.vercel.app`
   - `https://<nama-aplikasi-anda>.vercel.app/`
4. Klik **Save**.
