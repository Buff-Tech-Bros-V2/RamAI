# RamAI — Design System (Poster + Aplikasi)

Dokumen ini menyatukan aturan visual untuk dua deliverable hackathon: poster A4 yang dipajang di depan ballroom, dan dashboard MVP (`apps/dashboard`). Tujuannya supaya keduanya terasa satu produk, bukan dua proyek berbeda.

## 0. Catatan penting: penamaan

Kode (`README.md`, `config/settings.py`, navbar di `base.html`) masih memakai nama **VIRALCAST**, sedangkan PRD (`RamAI_PRD.md`) dan seluruh materi branding memakai **RamAI**. Sebelum poster dicetak, putuskan satu nama dan samakan di:

- `apps/dashboard/templates/dashboard/base.html` (`<title>`, navbar-brand)
- `README.md`
- Poster dan seluruh materi presentasi

Rekomendasi: pakai **RamAI**, karena itu yang sudah dipakai di PRD dan poster.

## 1. Brand identity

- **Positioning**: alat bantu keputusan produksi/replenishment yang serius dan data-driven, bukan produk konsumer yang playful. Kepercayaan (trust) adalah nilai jual utama — agent/LLM tidak pernah mengarang angka.
- **Maskot**: karakter panda bergaya sticker/reaction-meme, outline tebal hitam-putih, aksen merah coral di mulut. Dua ekspresi yang tersedia di `assets/images/`:
  - `IMG_20260917_150202.png` — panda panik, pegang telinga, keringat → dipakai untuk momen "sebelum RamAI" / state cemas (data belum lengkap, keputusan mendesak, tool failure).
  - `IMG_20260917_150228.png` — panda santai, menunjuk percaya diri → dipakai untuk momen "sesudah RamAI" / state rekomendasi siap dan tervalidasi.
  - Aturan pakai: maskot adalah elemen aksen naratif (before/after, ilustrasi state), bukan pengganti data. Jangan gambar ulang gaya maskot ini — pakai asetnya apa adanya. Jangan pakai di kedua ekspresi sekaligus pada elemen yang sama (hindari campur pesan panik+percaya diri).

## 2. Color palette

Dipakai konsisten di poster maupun aplikasi. Palet sengaja dibagi dua peran karena RamAI berdiri di dua dunia sekaligus — **demand forecasting/AI** (dingin, analitik, trust) dan **marketplace/retail** (hangat, urgency, commerce) — supaya siapa pun yang lihat poster langsung membaca kedua kata kunci tema hackathon tanpa perlu teks penjelas. Warna dasar juga dipilih supaya maskot hitam-putih tetap kontras tinggi di atasnya.

| Token | Hex | Peran | Kenapa |
| --- | --- | --- | --- |
| `--ram-navy-900` | `#0F172A` | Latar gelap utama (header poster, navbar app) | Menaungi semua elemen lain; menegaskan ini alat keputusan serius, bukan sekadar toko |
| `--ram-navy-800` | `#1E293B` | Latar gelap sekunder / kartu gelap | Variasi kedalaman di atas navy-900 |
| `--ram-teal-500` | `#06B6D4` | **Sisi forecasting/AI** — garis chart, ikon agent/flow, link, tombol utama | Bahasa universal analytics/data trust (dashboard BI, decision engine) |
| `--ram-teal-400` | `#22D3EE` | Hover/highlight dari teal-500 | — |
| `--ram-orange-500` | `#F97316` | **Sisi marketplace/retail** — badge "surge detected", pull-quote pembeda utama, CTA/tombol approve | Warna konversi/CTA yang dipakai hampir semua marketplace ID (Shopee, Lazada, Tokopedia) — langsung diasosiasikan dengan jual-beli & lonjakan permintaan |
| `--ram-coral-400` | `#F87171` | Risiko — lost sales, residual-stock risk, stockout, error state | Konvensi warning/danger; senada warna mulut maskot |
| `--ram-bg-light` | `#F8FAFC` | Latar konten terang, badan halaman | Netral, print-safe, tidak bentrok dengan teal maupun oranye |
| `--ram-ink-900` | `#0F172A` | Warna teks utama di atas latar terang | — |
| `--ram-muted-500` | `#64748B` | Teks sekunder/caption | — |

Catatan pemakaian:
- Elemen forecast/agent/AI → teal. Elemen demand/lonjakan/retail → oranye. Elemen risiko → coral. Jangan tukar peran ketiganya supaya asosiasi tetap konsisten dari poster ke app.
- Kontras yang wajib dijaga: teks di atas `--ram-navy-900` harus putih/`--ram-bg-light`; jangan taruh `--ram-orange-500` di atas `--ram-teal-500` (kontras rendah) — oranye dan teal masing-masing selalu di atas navy atau off-white, tidak bertumpuk satu sama lain.

## 3. Typography

- **Judul (poster & heading app)**: sans-serif tebal — Poppins, Sora, atau Inter 700/800. Judul poster harus terbaca dari jarak ±2 meter.
- **Body**: Inter / system-ui, regular 400–500.
- **Angka/metrik** (forecast, modal, unit): gunakan font tabular/monospace-friendly atau minimal `font-variant-numeric: tabular-nums` supaya angka di kartu keputusan rapi sejajar.
- Kondisi aplikasi saat ini: masih 100% default Bootstrap (system font stack), belum ada override tipografi. Lihat §5.2 untuk cara menambahkannya tanpa membongkar template.

## 4. Poster A4 — spesifikasi

Lihat riwayat prompt yang sudah dibuat untuk Claude Design (isi lengkap sudah dikirim ke user sebelumnya). Ringkasan spesifikasi tetap:

- Ukuran: A4 210×297mm, portrait, print-ready, kontras tinggi untuk pencahayaan ballroom.
- Struktur: Header (nama produk + tagline + badge tim/challenge) → Latar Belakang Masalah (dengan maskot panik) → Konsep/Solusi (dengan maskot percaya diri, pull-quote pembeda utama) → Cara Kerja (diagram alur 6 langkah, ikon line-art) → 3 Kartu Keputusan (Commit Sekarang / Bertahap / Tunggu) → Footer (badge "Synthetic Demo Data" + "Working MVP Prototype" + tech stack).
- Diagram konsep dipakai untuk merepresentasikan produk — **bukan** screenshot dashboard asli, karena UI masih minimal dan belum representatif secara visual.
- Warna & tipografi: ikuti §2 dan §3 di atas persis.

## 5. Aplikasi (dashboard MVP)

### 5.1 Kondisi saat ini

Stack: Django templates + Bootstrap 5.3.3 via CDN, tanpa CSS kustom apa pun (`base.html` hanya `bg-light` + navbar `bg-dark` default Bootstrap). Semua warna (badge warning/info/danger/primary, card border) masih warna default Bootstrap, belum brand RamAI.

Komponen yang sudah ada:
- Navbar gelap + badge "Synthetic Demo Data" (FR-U06 — sudah terpenuhi)
- 4 stat card (stok, operation mode, deadline, surge persistence)
- Tabel actual order & forecast quantile
- 3 kartu alternatif keputusan (`_action_card.html`), kartu rekomendasi diberi border+badge primary
- Form what-if (kapasitas, modal, deadline) + tombol approve
- Alert untuk warning/fallback state

### 5.2 Rencana styling ringan (cocok untuk sisa waktu hackathon)

Jangan bongkar total ke custom CSS framework. Cukup override CSS variable Bootstrap di `base.html` supaya seluruh komponen (`.btn-primary`, `.badge`, `.border-primary`, dst) otomatis ikut warna brand tanpa mengubah markup:

```html
<style>
  :root {
    --bs-primary: #06B6D4;      /* ram-teal-500, ganti tombol & border rekomendasi */
    --bs-primary-rgb: 6,182,212;
    --bs-dark: #0F172A;         /* ram-navy-900, navbar */
    --bs-warning: #F97316;      /* ram-orange-500, badge surge/synthetic data */
    --bs-danger: #F87171;       /* ram-coral-400, stockout/risk */
    --bs-body-bg: #F8FAFC;      /* ram-bg-light */
    --bs-body-font-family: 'Inter', system-ui, sans-serif;
  }
</style>
```

Tambahkan `<link>` Google Fonts Inter di `<head>` jika ingin tipografi ikut berubah. Perubahan ini murah (tidak menyentuh logic Django) tapi langsung membuat app terasa satu brand dengan poster.

### 5.3 Pemetaan state ke warna (PRD §15.2)

| State | Warna/badge |
| --- | --- |
| Normal | Netral (`--ram-ink-900` / abu-abu) |
| Surge detected | `--ram-orange-500` (badge/alert warning) |
| Content data missing | Info biru netral (alert info, sudah ada) |
| Low confidence | Outline oranye pada badge confidence |
| Stockout censoring detected | `--ram-coral-400` (badge danger, sudah dipakai di tabel) |
| Infeasible constraints | `--ram-coral-400` (alert danger) |
| Tool failure | `--ram-coral-400` + opsional maskot panik kecil sebagai ilustrasi |
| Recomputing | `--ram-teal-500` (spinner/progress) |
| Awaiting approval | `--ram-teal-500` (tombol primary, sudah sesuai) |

### 5.4 Maskot di aplikasi (opsional, prioritas rendah)

Boleh dipakai sebagai ilustrasi kecil di empty/error state saja (mis. "Belum ada data SKU" atau "tool failure") memakai panda panik, dan di halaman konfirmasi setelah approve plan memakai panda percaya diri. Ini P2 — jangan dikerjakan sebelum P0/P1 functional requirement di PRD selesai.

## 6. Konsistensi lintas deliverable

- Badge "Synthetic Demo Data" wajib muncul di poster maupun app dengan warna yang sama (`--ram-orange-500` pada latar gelap, atau `text-bg-warning` di Bootstrap — sudah cocok).
- Istilah aksi keputusan harus identik: "Commit Sekarang", "Commit Bertahap", "Tunggu Dulu" — sudah konsisten antara `_action_card.html` dan draft prompt poster, pertahankan.
- Nama tim "BuffTechBros" dan label challenge tampil di footer poster dan boleh ditambahkan di footer app jika sempat.
