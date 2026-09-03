# Keamanan Repository

Repository disarankan berstatus **private** karena source memuat arsitektur operasional sistem bisnis, walaupun paket ini tidak memuat kredensial produksi.

## Jangan pernah diunggah

- `.env` aktif;
- token Telegram;
- API key Groq;
- password PostgreSQL atau GOWA;
- webhook secret;
- dump database dan data pelanggan;
- log yang memuat nomor telepon, chat, atau informasi transaksi;
- direktori sesi/pairing WhatsApp;
- backup server.

Gunakan `.env.example` hanya sebagai daftar nama konfigurasi. Nilai sebenarnya tetap berada di server atau secret manager.

Jika kredensial tidak sengaja masuk ke Git, jangan hanya menghapus filenya dari commit terbaru. Segera nonaktifkan/rotasi kredensial tersebut, batasi akses repository, lalu bersihkan riwayat Git dengan prosedur yang diawasi.

Laporan kerentanan sebaiknya dikirim secara privat kepada pemilik repository, bukan ditulis pada issue publik.

