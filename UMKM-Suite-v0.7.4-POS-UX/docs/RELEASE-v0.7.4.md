# UMKM Suite v0.7.4 POS UX

**Status:** Stable  
**Upgrade dari:** v0.7.2-pos-ai-ux  
**Jangan gunakan:** v0.7.3-pos-ux karena telah ditarik  

## Ringkasan

Rilis ini memulihkan routing teks bebas WhatsApp setelah menu dibuka dan menyelesaikan peningkatan kemudahan penggunaan POS di WhatsApp serta Telegram. Seluruh pemeriksaan role, tenant, stok, harga, konfirmasi, dan transaksi atomik tetap dilakukan pada backend.

## Perubahan utama

- Teks seperti `jual susu 1 cup cash`, `catat pemasukan 100 ribu`, dan `edit produk` kembali bekerja setelah pengguna mengetik `MENU`.
- Penjualan multi-produk menerima kata `dan`, misalnya `jual 2 kopi dan 1 roti QRIS`.
- Nomor menu WhatsApp selalu berurutan sesuai role pengguna.
- Telegram menggunakan tombol dan wizard sehingga pengguna tidak perlu menghafal command.
- `/sale` hanya membuat preview; stok dan keuangan berubah setelah pengguna memilih **Simpan**.
- Tambah produk menjadi tiga tahap dengan SKU otomatis.
- Edit produk tersedia melalui wizard dan konfirmasi.
- Perintah lama yang masih aman dipertahankan untuk kompatibilitas.

## Keamanan dan data

- AI hanya menerjemahkan bahasa pengguna menjadi intent/draft; AI tidak menulis database langsung.
- Produk dibatasi pada tenant aktif dan permission diperiksa server-side.
- Harga dan stok dibaca ulang saat konfirmasi.
- Penjualan tetap atomik terhadap invoice, item, pembayaran, stok, pergerakan stok, pemasukan, dan audit log.
- Tidak ada migrasi destruktif baru.
- GOWA, pairing, volume sesi, webhook, token, dan API key tidak diganti.

## Verifikasi

- Python compile check: lulus.
- Shell syntax check: lulus.
- Unit, parser, routing, service, dan package-contract tests: **55 lulus**.
- Checklist UX tersedia pada `docs/UX-ACCEPTANCE-v0.7.4.md`.

## Aset Release

Lampirkan file berikut pada Release GitHub:

1. `UMKM-Suite-v0.7.4-POS-UX.zip` — paket deployment.
2. `SHA256SUMS-v0.7.4.txt` — checksum integritas.
3. `Panduan-Operasional-Troubleshooting-UMKM-Suite-v0.7.4.docx` — panduan operator dan troubleshooting.

## Upgrade

```bash
cd /home/msllab
unzip UMKM-Suite-v0.7.4-POS-UX.zip
cd UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/install.sh --check
```

Lanjutkan `--apply` hanya jika preflight berakhir dengan `PREFLIGHT OK` dan sumber aktif adalah `v0.7.2-pos-ai-ux`.

```bash
set -o pipefail
sudo ./scripts/install.sh --apply 2>&1 | tee /home/msllab/umkm-v0.7.4-install.log
sudo ./scripts/status.sh
sudo ./scripts/feature_check.sh
```

## Rollback

```bash
cd /home/msllab/UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/rollback.sh --apply
```

Rollback aplikasi mempertahankan skema POS additive agar data tetap dapat dipulihkan.

