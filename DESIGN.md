---
name: wormhole-mini
description: Alat kirim-file sekali unduh dan PDF Tools berlatar gelap, satu aksen biru, utilitarian.
colors:
  accent-blue: "#4da3ff"
  accent-blue-hover: "#6db4ff"
  accent-blue-wash: "#2a3a4a"
  accent-blue-solid: "#1f6fd6"
  accent-blue-solid-hover: "#1b66c9"
  danger-coral: "#ff6b6b"
  danger-coral-solid: "#c93434"
  danger-coral-solid-hover: "#bf3030"
  success-mint: "#4dff88"
  warning-amber: "#e0b040"
  night-bg: "#0f0f10"
  surface: "#1a1a1c"
  surface-raised: "#222222"
  text-primary: "#eeeeee"
  text-soft: "#cccccc"
  text-dim: "#aaaaaa"
  text-subtle: "#999999"
  text-muted: "#888888"
  white: "#ffffff"
  border: "#333333"
  border-strong: "#444444"
  border-dashed: "#555555"
  border-subtle: "#2a2a2c"
typography:
  display:
    fontFamily: "sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.2
  body:
    fontFamily: "sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.3
rounded:
  xs: "4px"
  sm: "5px"
  md: "6px"
  lg: "8px"
  xl: "10px"
  2xl: "12px"
  3xl: "16px"
  4xl: "20px"
  full: "50%"
spacing:
  2: "2px"
  4: "4px"
  6: "6px"
  8: "8px"
  10: "10px"
  12: "12px"
  14: "14px"
  16: "16px"
  18: "18px"
  20: "20px"
  24: "24px"
  30: "30px"
  36: "36px"
components:
  button-primary:
    backgroundColor: "{colors.accent-blue-solid}"
    textColor: "{colors.white}"
    rounded: "{rounded.md}"
    padding: "0 16px"
    height: "44px"
  button-danger:
    backgroundColor: "{colors.danger-coral-solid}"
    textColor: "{colors.white}"
    rounded: "{rounded.md}"
    padding: "0 16px"
    height: "44px"
  button-secondary:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "0 16px"
    height: "44px"
  input-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "10px"
  tool-card:
    backgroundColor: "{colors.night-bg}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.xl}"
    padding: "16px 10px"
  tool-card-active:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
  chip-tab:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-soft}"
    rounded: "{rounded.4xl}"
    padding: "7px 16px"
  chip-tab-active:
    backgroundColor: "{colors.accent-blue-solid}"
    textColor: "{colors.white}"
  dropzone:
    backgroundColor: "{colors.night-bg}"
    textColor: "{colors.text-subtle}"
    rounded: "{rounded.2xl}"
    padding: "10px"
---

# Design System: wormhole-mini

## Overview

**Creative North Star: "The Quiet Utility Bench"**

wormhole-mini adalah meja kerja gelap yang tenang: pengguna datang untuk mengirim satu file atau memproses satu PDF, lalu pergi. Latar hampir hitam, satu aksen biru, teks abu terang. Tidak ada hero, ilustrasi, atau gradien. Kontrol berdiri sendiri di tengah kanvas gelap dan bahasanya santai, Indonesia sehari-hari.

Sistem ini berlaku untuk mode Operate: kecepatan membaca dan konsistensi lebih penting daripada ekspresi. Kepribadian datang dari emoji sebagai ikon (🌀 📦 🛠️ 🐛 ❓) dan nada tulisan yang akrab, bukan dari efek visual. Satu-satunya elemen dekoratif adalah marquee biru di atas halaman utama.

Implementasi saat ini masih memakai tipografi bawaan browser dan komponen yang ditulis sendiri-sendiri per halaman; token di `static/css/app.css` sudah ada, tetapi komponen belum dibagi lintas halaman.

**Key Characteristics:**
- Gelap sepenuhnya (`#0f0f10`), tanpa tema terang.
- Satu aksen biru (`#4da3ff`) untuk aksi utama, fokus aktif, dan progres.
- Datar: kedalaman dari garis tipis dan lapisan tonal, bukan bayangan.
- Radius kecil (6px) untuk kontrol, lebih besar (10–12px) untuk wadah.
- Komponen padat, teks 11–14px, cocok untuk alat bantu, kurang untuk sentuhan.

## Colors

Palet monokrom gelap dengan satu aksen biru dan dua warna status. Seluruh nilai ada di `static/css/app.css`.

### Primary
- **Bright Signal Blue** (`#4da3ff`): teks tautan, border aktif/hover, bar progres, dropzone saat drag, ring fokus. Hanya di atas latar gelap (7,3:1); jangan dipakai sebagai latar di bawah teks putih (2,6:1).
- **Solid Action Blue** (`#1f6fd6`, hover `#1b66c9`): latar tombol utama, chip aktif, marquee, badge, tombol putar. Teks putih di atasnya 4,9:1 (hover 5,5:1).
- **Signal Blue Lift** (`#6db4ff`): hover tombol putar thumbnail. Hanya dipakai di satu tempat.
- **Blue Wash** (`#2a3a4a`): latar tombol posisi watermark terpilih.

### Secondary
- **Alert Coral** (`#ff6b6b`): teks error, hover Logout, tombol mengambang laporan bug (emoji, bukan teks).
- **Solid Danger Red** (`#c93434`, hover `#bf3030`): latar tombol bahaya (Hapus, Ban, Kirim Laporan) dan badge banned. Teks putih 5,2:1 (hover 5,7:1).
- **Success Mint** (`#4dff88`) dan **Caution Amber** (`#e0b040`): status sukses dan peringatan di teks; hanya muncul dari JS dan style inline.

### Neutral
- **Night Ground** (`#0f0f10`): latar halaman.
- **Panel Slate** (`#1a1a1c`): input, panel, kartu aktif, tab.
- **Raised Slate** (`#222222`): header panel, latar tombol sekunder, track progres.
- **Paper Grey** (`#eeeeee`): teks utama. Turunan: `#cccccc`, `#aaaaaa`, `#999999`, `#888888` untuk teks sekunder hingga redup.
- **Hairline** (`#333333`), **Edge** (`#444444`), **Dashed Edge** (`#555555`), **Row Line** (`#2a2a2c`): garis pembatas dari paling halus hingga paling tegas.

### Named Rules
**The One Blue Rule.** Biru adalah satu-satunya warna aksi. Jika dua elemen berwarna biru bersaing di satu layar, salah satunya salah.

**The Solid-For-Surfaces Rule.** Warna terang (`accent-blue`, `danger-coral`) hanya untuk teks dan garis di atas gelap; permukaan yang membawa teks putih memakai varian *solid* (>= 4,5:1). Teks putih di biru atau coral terang adalah bug.

**The Status Colors Stay Textual Rule.** Coral, mint, dan amber membawa status; jangan dipakai sebagai dekorasi atau penanda merek.

## Typography

**Display Font:** `sans-serif` (font sistem browser)
**Body Font:** `sans-serif`
**Label/Mono Font:** tidak ada

**Character:** Tanpa pilihan tipografi sengaja; hierarki dibentuk hanya oleh ukuran dan warna abu. Hasilnya netral dan cepat, tetapi tidak punya suara sendiri.

### Hierarchy
- **Display** (bold bawaan `h2`, ±1.5rem, normal): judul halaman ("wormhole-mini", "Login", "PDF Tools").
- **Body** (400, 14px, normal): input, tombol, isi utama.
- **Label** (400, 12–13px, `#999`): label field, catatan, tab, status.
- **Micro** (400, 10–11px, `#888`): label thumbnail, catatan kecil, bilah pengguna.

### Named Rules
**The Small Text Ceiling Rule.** Teks fungsional tidak boleh lebih kecil dari 11px; yang 10px (label thumbnail) adalah pengecualian yang perlu dicatat, bukan pola.

## Layout

Kolom tunggal terpusat. Halaman form/auth: kotak 340px di tengah layar. Halaman utama: dropzone 360px, ditumpuk vertikal. PDF Tools: grid kartu `repeat(auto-fit, minmax(140px, 1fr))` maksimum 600px, form 520px di bawahnya. Admin: kolom 860px yang terpusat (`max-width:860px; margin:0 auto`), isi rata kiri, dengan tab dan tabel. Ritme spasi memakai langkah 4–6–8–10–12–14–16–20px.

Responsif ditangani lewat lebar fleksibel dan `max-width`; tidak ada media query. Meta viewport sudah ada di semua halaman sejak Fase 0.

## Elevation & Depth

Datar secara default. Kedalaman dari garis `1px` dan lapisan tonal (`#0f0f10` → `#1a1a1c` → `#222`). Satu-satunya bayangan: `0 2px 8px rgba(0,0,0,0.4)` pada dua tombol bulat mengambang (bantuan dan laporan bug). Panel yang muncul dari tombol itu memakai border, tanpa bayangan.

### Shadow Vocabulary
- **Floating Action** (`box-shadow: 0 2px 8px var(--color-shadow)`): hanya untuk tombol aksi mengambang 52px.

### Named Rules
**The Flat-By-Default Rule.** Permukaan datar saat diam; satu-satunya bayangan milik elemen yang melayang di atas konten.

## Shapes

Sudut membulat kecil dan seragam: 6px untuk input/tombol, 8px untuk wadah kecil, 10–12px untuk kartu dan dropzone. Pil (16–20px) hanya untuk chip kategori dan tombol halaman edit; lingkaran penuh untuk tombol mengambang dan penanda terpilih. Border tipis `1px`, dropzone `2px` putus-putus.

## Components

### Buttons
Satu sistem di `static/css/app.css`: kelas `.btn` + satu varian warna (`.btn-primary`, `.btn-secondary`, `.btn-danger`) + modifier opsional (`.btn-block` lebar penuh, `.btn-sm` 12px, `.btn-lg` 15px, `.btn-icon` 44×44). Tombol ikon polos memakai `.icon-btn`. Jangan menulis gaya tombol baru per halaman.
- **Shape:** sudut 6px, tinggi minimum 44px (`--touch-min`) di semua ukuran; ukuran kecil hanya mengecilkan huruf dan padding horizontal.
- **Primary:** latar `#1f6fd6`, teks putih, 14px. Hover `#1b66c9`.
- **Danger:** latar `#c93434`, teks putih. Hover `#bf3030`. Untuk aksi destruktif, selalu lewat dialog konfirmasi.
- **Secondary:** latar `#222`, border `#444`, teks terang. Hover: border biru.
- **Focus:** global `:focus-visible` outline 2px `#4da3ff`, offset 2px.
- **Disabled:** opasitas 0.5, kursor `not-allowed`.
- Elemen yang bukan `<button>` tetapi bisa diklik (kartu alat, chip, dropzone) wajib `role="button"` + `tabindex="0"`; `static/js/a11y.js` menambahkan respons Enter dan Spasi.

### Chips
- **Style:** pil 20px, tinggi 44px, latar `#1a1a1c`, border `#444`, teks `#ccc`, 13px (kategori PDF Tools). Aktif: latar Solid Action Blue, teks putih.

### Cards / Containers
- **Tool Card** (`role=button`): border `#333`, radius 10px, padding 16×10, ikon emoji 26px dan label 13px. Niatnya ikon di atas label, tetapi `.tool-card{display:flex}` (dari filter kategori) membuat ikon di kiri dan label di kanan; ini bug yang diketahui, bukan gaya. Hover/aktif: border biru, aktif juga latar `#1a1a1c`.
- **Panel melayang (bantuan/bug):** latar `#1a1a1c`, border `#333`, radius 12px, header `#222`.
- **Stat (admin):** latar `#1a1a1c`, border `#2a2a2c`, radius 8px.

### Inputs / Fields
- **Style:** latar `#1a1a1c`, border `#444`, radius 6px, teks `#eee`, 13–14px, label 12px `#999` di atasnya.
- **Focus:** ring global 2px biru (`:focus-visible`). Tinggi minimum 44px; di layar <= 480px teks input 16px agar iOS tidak zoom. Placeholder `#9a9a9a`.
- **Error / Disabled:** error ditampilkan sebagai teks status coral di bawah form, bukan di field.

### Navigation
- **Tab (admin):** kotak 6px, border `#333`; aktif: border dan teks biru.
- **Navigasi antarhalaman:** tautan teks biru atau abu (`← Kembali`, `🛠️ Buka PDF Tools →`); tidak ada bar navigasi.

### Dropzone (komponen khas)
Kotak putus-putus `2px #555`, teks tengah `#999`; saat drag border dan teks jadi biru. Radius berbeda per tempat: 12px di halaman utama (`#drop`, 360×200) dan 10px di dropzone alat PDF (`#merge-zone` dan sejenisnya).

## Do's and Don'ts

### Do:
- **Do** pakai token `var(--color-*)`, `var(--radius-*)`, `var(--space-*)` dari `static/css/app.css`.
- **Do** pertahankan satu aksen biru untuk aksi utama dan state aktif.
- **Do** tampilkan status operasi di teks status dengan warna coral/mint/amber dan kalimat Indonesia santai.
- **Do** jaga kontras teks sekunder minimal AA di atas `#0f0f10`.

### Don't:
- **Don't** menambah bayangan atau gradien di permukaan diam.
- **Don't** menambah warna aksi kedua di samping biru.
- **Don't** menulis ulang komponen yang sama (tombol, input, kartu) per halaman dengan nilai berbeda; pakai satu definisi.
- **Don't** memakai teks di bawah 11px untuk informasi yang diperlukan untuk bertindak.

### Do (ditambahkan setelah polish batch 1):
- **Do** beri target sentuh minimal 44×44px pada semua kontrol, termasuk ikon.
- **Do** minta konfirmasi lewat `<dialog class="dlg">` untuk aksi yang tidak bisa dibatalkan (Hapus, Ban, Unban), dengan nama target di isi pesan.
- **Do** pakai `min-height:100dvh` dan `max-width` + `width:100%` untuk wadah; `box-sizing:border-box` sudah global.

### Don't (ditambahkan setelah polish batch 1):
- **Don't** menulis `height:100vh` atau lebar piksel tetap (mis. `width:360px`) untuk wadah konten.
- **Don't** memakai `<div>`/`<span>` sebagai tombol tanpa `role`, `tabindex`, dan label.
