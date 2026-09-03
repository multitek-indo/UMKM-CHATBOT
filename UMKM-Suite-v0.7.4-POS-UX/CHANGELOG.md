# Changelog

Semua perubahan penting UMKM Suite dicatat di sini. Source repository awal dimulai dari versi stabil `v0.7.4`; entri sebelumnya merupakan ringkasan sejarah paket yang telah dibuat.

## v0.7.4 — Stable

- Memperbaiki regresi v0.7.3 yang membuat teks bebas setelah `MENU` dianggap sebagai pilihan angka.
- Mendukung transaksi multi-produk dengan kata “dan”.
- Membuat nomor menu WhatsApp berurutan sesuai role.
- Menambahkan tombol dan wizard POS di Telegram.
- Mengubah `/sale` menjadi preview dan konfirmasi sebelum penyimpanan.
- Memangkas tambah produk menjadi tiga tahap dan menambahkan wizard edit produk.
- Lulus 55 pengujian lokal pada paket release.

## v0.7.3 — Withdrawn / Jangan dipasang

- Memperkenalkan UX menu dan wizard baru.
- Ditarik karena state menu pasif menangkap semua teks bebas setelah `MENU`, sehingga perintah POS, keuangan, dan edit produk tidak diteruskan ke router yang benar.
- Masalah tersebut diperbaiki di `v0.7.4` dan dijadikan regression test.

## v0.7.2 — Stable fallback historis

- Menambahkan resolver nama produk dan UX AI yang tidak mengharuskan SKU.
- Menjaga draft penjualan saat pengguna melengkapi metode pembayaran.
- Menambahkan penanganan produk ambigu/tidak ditemukan serta preview harga, total, dan sisa stok.
- Lulus 28 pengujian pada paket release.

## v0.7.1 — Historis

- Menambahkan menu angka dan wizard WhatsApp.
- Menambahkan undangan karyawan satu kali pakai tanpa persetujuan MASTER.
- Memperbaiki health check WA bridge dan rollback recoverable.
- Lulus 13 pengujian pada paket release.

## v0.7.0 — Historis

- Menambahkan fondasi POS, inventory, stock opname, penjualan atomik, role server-side, dan RLS tenant.
- Menambahkan installer dengan preflight, backup, feature check, dan rollback.
- Lulus 8 pengujian pada paket release.

