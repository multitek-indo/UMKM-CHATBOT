# Panduan WhatsApp UMKM Suite v0.7.4

## Menu sesuai peran

Ketik `MENU`. Bot hanya menampilkan fungsi yang boleh digunakan oleh peran tersebut dan menomorinya selalu dari `1` secara berurutan. Karena itu, pilih berdasarkan nama menu yang tampil—nomor POS milik Pemilik dapat berbeda dari nomor POS milik Kasir.

Menu tidak mengunci percakapan. Saat menu sedang tampil, pengguna tetap boleh langsung mengetik kalimat seperti `jual susu 1 cup cash`, `catat pemasukan 100 ribu`, atau `edit produk`.

## Penjualan dengan bahasa biasa

Koma dan kata `dan` sama-sama dapat memisahkan produk:

```text
jual susu 1 cup tunai
jual 2 kopi susu dan 1 roti bakar bayar QRIS
jual kopi susu 2, roti bakar 1 transfer
```

Kata `dan` di dalam satu nama tidak dipecah apabila kedua sisinya bukan item lengkap. Contoh `Roti dan Kopi 1` tetap dibaca sebagai satu produk bernama “Roti dan Kopi”.

Bot menyimpan draft, meminta pembayaran jika belum ada, lalu menampilkan nama, jumlah, harga, total, dan sisa stok. Pilih `1` untuk menyimpan, `2` untuk mengubah, atau `0` untuk membatalkan.

## Menambahkan produk dalam tiga tahap

Pilih **POS & Stok → Tambah Produk**, lalu isi:

1. Nama dan satuan opsional, misalnya `Kopi Susu | cup`.
2. Harga jual dan harga modal opsional, misalnya `18000 | 9000` atau cukup `18000`.
3. Stok awal dan batas minimum opsional, misalnya `20 | 5` atau cukup `20`.

SKU dibuat otomatis. Jika satuan tidak ditulis, sistem memakai `pcs`. Harga modal dan batas stok minimum yang dilewati dimulai dari `0` dan tetap dapat disesuaikan melalui fitur lanjutan.

## Mengedit produk

Ketik `edit produk` kapan saja, atau pilih **POS & Stok → Edit Produk**. Ketik nama/SKU, pilih data yang ingin diubah, masukkan nilai baru, lalu periksa konfirmasi. Sistem hanya menyimpan setelah pengguna memilih `1`.

## Menambahkan karyawan

Pilih **POS & Stok → Kelola Karyawan → Tambah Karyawan**. Isi nama dan peran. Kode undangan delapan digit berlaku 30 menit dan satu kali pakai. Karyawan cukup mengirim `GABUNG 12345678` ke WhatsApp bot; MASTER tidak diperlukan.

## Keamanan

- Produk dicari hanya di dalam usaha pengguna.
- Nama ambigu tidak dipilih otomatis.
- Role dan stok diperiksa ulang ketika tombol Simpan ditekan.
- Harga berasal dari database, bukan dari AI atau teks pengguna.
- `0` atau `MENU` membatalkan draft dan kembali ke menu utama.
