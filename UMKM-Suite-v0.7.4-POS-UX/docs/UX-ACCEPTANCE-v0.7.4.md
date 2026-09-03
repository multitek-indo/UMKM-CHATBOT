# Checklist UX UMKM Suite v0.7.4

Dokumen ini dipakai setelah deployment untuk memeriksa seluruh perbaikan dari sisi pengguna. Gunakan produk uji dengan stok yang cukup dan batalkan preview apabila tidak ingin mengubah data.

## 0. Regresi MENU tidak memblokir teks bebas

Ketik `MENU`, kemudian uji satu per satu tanpa memilih angka terlebih dahulu:

| Input | Hasil yang diharapkan |
|---|---|
| `jual susu 1 cup cash` | Muncul preview penjualan, bukan peringatan pilih angka |
| `pos penjualan susu` | Bot meminta jumlah produk, bukan peringatan pilih angka |
| `catat pemasukan 100 ribu` | Masuk ke konfirmasi/kelengkapan pemasukan |
| `edit produk` | Wizard edit produk dimulai |

Input angka tetap harus membuka menu yang tampil.

## 1. Transaksi multi-produk dengan kata “dan”

| Input | Hasil yang diharapkan |
|---|---|
| `jual 2 kopi dan 1 roti tunai` | Preview berisi dua item: Kopi × 2 dan Roti × 1 |
| `jual kopi 2 dan roti 1 QRIS` | Preview berisi dua item |
| `jual Roti dan Kopi 1 tunai` | Tetap satu item jika “Roti dan Kopi” adalah nama produk |

Tidak ada stok atau pemasukan yang berubah sebelum pengguna memilih **Simpan**.

## 2. Nomor menu berdasarkan role

Ketik `MENU`, lalu buka **POS & Stok**. Nomor yang tampil harus selalu dimulai dari 1 dan berlanjut tanpa angka yang hilang. Uji minimal role OWNER, CASHIER, INVENTORY, FINANCE, dan VIEWER. Menu yang terlihat dapat berbeda, tetapi urutan angkanya tidak boleh berlubang.

## 3. Telegram tidak memerlukan hafalan command

Tekan **🛒 POS & Stok**. Halaman utama harus menampilkan tombol yang sesuai role:

- Penjualan Baru;
- Daftar Produk;
- Tambah Produk;
- Atur Stok;
- Laporan;
- Stok Rendah;
- Karyawan, bila diizinkan.

Daftar command hanya tampil jika pengguna sengaja membuka **Fitur Lanjutan**.

## 4. `/sale` menggunakan konfirmasi

1. Catat stok produk sebelum uji.
2. Kirim `/sale KOPI:1 | CASH`.
3. Pastikan bot menampilkan preview dengan total dan sisa stok.
4. Periksa bahwa stok belum berubah.
5. Pilih **Batal**; stok harus tetap sama.
6. Ulangi lalu pilih **Simpan**; stok dan pemasukan harus berubah tepat satu kali.

## 5. Tambah produk tiga tahap

Pilih tombol/menu **Tambah Produk** dan isi:

1. `Kopi Susu | cup`
2. `18000 | 9000`
3. `20 | 5`

Preview harus memperlihatkan SKU otomatis, nama, satuan, harga jual, harga modal, stok awal, dan batas minimum. Data baru ditulis setelah **Simpan**.

## Hasil akhir

Acceptance dinyatakan lulus jika seluruh bagian sesuai, permission tetap menolak aksi yang tidak tersedia bagi role, dan feature check server menampilkan `FEATURE CHECK LULUS`.
