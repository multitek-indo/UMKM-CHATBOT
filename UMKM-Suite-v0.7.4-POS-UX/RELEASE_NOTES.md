# Release Notes — UMKM Suite v0.7.4 POS UX

Tanggal paket: 2 September 2026

## Diperbaiki

0. Regresi routing v0.7.3 diperbaiki. Menu angka sekarang hanya menangkap input angka; teks bebas dilepas ke router POS/AI dan state menu pasif dibersihkan. Empat kalimat dari laporan pengguna dijadikan regression test.

1. Parser penjualan membedakan kata `dan` sebagai pemisah produk dan sebagai bagian nama produk. Pemisahan hanya dilakukan apabila kedua sisinya dapat dibaca sebagai item lengkap.
2. Menu utama, POS, dan pengaturan akun WhatsApp dibentuk ulang per role dengan nomor berurutan. Pilihan angka dipetakan ke daftar aksi yang tersimpan pada sesi pengguna.
3. Halaman POS Telegram diganti menjadi tombol tindakan. Penjualan, tambah produk, atur stok, daftar produk, stok rendah, dan laporan mempunyai alur bertahap.
4. Command Telegram `/sale` tidak lagi memanggil `create_sale` secara langsung. Command membuat preview non-writing dan baru menyimpan setelah callback konfirmasi.
5. Tambah produk WhatsApp dan Telegram hanya meminta tiga tahap. Satuan, harga modal, dan batas minimum dapat diisi pada tahap yang sama setelah tanda `|`; jika dilewati nilainya memakai default aman.
6. WhatsApp menerima awalan `POS penjualan ...` dan menyediakan wizard `Edit Produk` tanpa format command panjang.

## Keamanan yang dipertahankan

- Pencarian produk tetap dibatasi pada tenant aktif.
- Menu yang disembunyikan bukan pengganti otorisasi; backend tetap memeriksa permission.
- Harga dan stok dibaca ulang dari database saat konfirmasi.
- Penjualan tetap atomik terhadap invoice, item, pembayaran, stok, stock movement, pemasukan, dan audit log.
- AI hanya membentuk intent/draft dan tidak memperoleh jalur tulis tanpa konfirmasi.
- Command lama selain perubahan `/sale` tetap tersedia untuk kompatibilitas.

## Deployment

- Sumber upgrade tervalidasi: `v0.7.2-pos-ai-ux`.
- Tidak ada migrasi database destruktif baru.
- Migrasi `007` dan `008` dijalankan ulang secara idempotent.
- Bot, worker, dan WA bridge dibangun ulang.
- GOWA, pairing, volume sesi, webhook, kredensial, dan API key tidak diubah.

## Verifikasi paket

- Python compile check: lulus.
- Shell syntax check: lulus.
- Unit, parser, routing, service, dan package-contract tests: 55 lulus.
- Feature check container menguji langsung routing menu, sinonim penjualan, wizard edit produk, dan import wizard Telegram.
- Checklist acceptance manual tersedia pada `docs/UX-ACCEPTANCE-v0.7.4.md`.
