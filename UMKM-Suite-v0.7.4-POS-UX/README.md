# UMKM Suite v0.7.4 POS UX

Upgrade aman dari `v0.7.2-pos-ai-ux` yang menyederhanakan operasional POS di WhatsApp dan Telegram. Database, tenant, role, AI, GOWA, sesi WhatsApp, webhook, network, token, dan data lama tetap digunakan.

Versi ini memperbaiki regresi v0.7.3: setelah `MENU` tampil, kalimat bebas tidak lagi dianggap sebagai pilihan angka. Perintah seperti `jual susu 1 cup cash`, `catat pemasukan 100 ribu`, dan `edit produk` kembali diteruskan ke POS/AI.

## Perbaikan utama

- Penjualan multi-produk menerima kata `dan`, misalnya `jual 2 kopi dan 1 roti tunai`.
- Nomor menu WhatsApp selalu berurutan untuk setiap role; angka mengarah ke aksi yang ditampilkan pada sesi tersebut.
- Telegram memakai tombol dan wizard untuk penjualan, produk, stok, serta laporan. Command lanjutan tetap tersedia, tetapi tidak lagi menjadi halaman utama.
- `/sale` Telegram membuat preview dan menunggu tombol **Simpan**; tidak langsung mengubah stok atau keuangan.
- Tambah produk dipangkas menjadi tiga tahap: nama/satuan, harga jual/modal, dan stok awal/minimum. Kolom setelah `|` bersifat opsional dan SKU dibuat otomatis.
- Edit produk tersedia sebagai wizard WhatsApp; pengguna memilih produk, memilih data yang diubah, lalu mengonfirmasi sebelum penyimpanan.
- Semua pemeriksaan role, tenant, stok, harga, dan transaksi atomik tetap dilakukan server-side.

## Preflight dan instalasi

```bash
cd /home/msllab
unzip UMKM-Suite-v0.7.4-POS-UX.zip
cd UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/install.sh --check
```

Jika hasil terakhir `PREFLIGHT OK`, terapkan hanya setelah log preflight diperiksa:

```bash
set -o pipefail
sudo ./scripts/install.sh --apply 2>&1 | tee /home/msllab/umkm-v0.7.4-install.log
sudo ./scripts/status.sh
sudo ./scripts/feature_check.sh
```

Installer membuat dump PostgreSQL dan backup source bridge. Hanya container `bot`, `worker`, dan `wa_bridge` yang dibangun ulang. Container GOWA dan sesi WhatsApp tidak disentuh.

## Uji penerimaan ringkas

1. WhatsApp: ketik `MENU` dengan role CASHIER dan pastikan nomor tampil `1, 2, 3...` tanpa lubang.
2. WhatsApp: ketik `jual 2 kopi dan 1 roti bayar QRIS`; pastikan dua item tampil pada preview.
3. WhatsApp: pilih **Tambah Produk**; pastikan hanya ada tiga tahap wajib sebelum konfirmasi.
4. Telegram: tekan **🛒 POS & Stok**; pastikan muncul tombol, bukan daftar command.
5. Telegram: jalankan `/sale KOPI:1 | CASH`; pastikan stok belum berubah sampai **Simpan** ditekan.

Panduan lengkap berada di `docs/PANDUAN-WHATSAPP.md`, `docs/UX-ACCEPTANCE-v0.7.4.md`, `docs/DEPLOYMENT.md`, dan `RELEASE_NOTES.md`.

## Rollback

```bash
cd /home/msllab/UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/rollback.sh --apply
```

Rollback mengaktifkan release sebelumnya dan memulihkan source WA bridge dari backup. Skema dan data POS tidak dihapus.
