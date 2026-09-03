# Panduan Operasional & Troubleshooting

## UMKM Suite v0.7.4 POS UX

**Baseline kelanjutan proyek — operasional, diagnosis, upgrade, dan pemulihan**

Disusun untuk: Multitek Indonesia  
Tanggal dokumen: 3 September 2026  
Status baseline: v0.7.4 dilaporkan sudah dapat digunakan; verifikasi server tetap dilakukan dengan `status.sh` dan `feature_check.sh`.

> Dokumen ini sengaja tidak memuat password, token, API key, nomor WhatsApp pelanggan, atau isi transaksi. Jangan menambahkan rahasia tersebut ke dokumentasi atau log yang dibagikan.

---

## 1. Cara memakai dokumen ini

Dokumen ini adalah titik awal resmi ketika:

- operator menjalankan pemeriksaan rutin;
- chatbot Telegram atau WhatsApp bermasalah;
- fitur AI tidak menjawab dengan benar;
- akan melanjutkan pengembangan setelah v0.7.4;
- akan memasang release baru;
- perlu rollback tanpa merusak data;
- perlu menyerahkan kasus kepada teknisi lain.

Urutan paling aman ketika ada gangguan:

1. Jangan langsung mengulang installer, menghapus container, atau mengubah database.
2. Catat waktu kejadian, pengguna/role yang terdampak, kanal, dan kalimat yang dikirim.
3. Jalankan **Pemeriksaan cepat 5 menit** pada bagian 8.
4. Cocokkan gejala dengan matriks troubleshooting pada bagian 10.
5. Ambil log dengan rentang waktu terbatas dan sembunyikan data pribadi.
6. Lakukan aksi paling kecil: restart satu komponen jika memang diperlukan.
7. Rollback aplikasi hanya bila release baru terbukti menjadi penyebab.

### Tingkat keamanan perintah

| Label | Makna | Contoh |
|---|---|---|
| AMAN — baca saja | Tidak mengubah layanan maupun data | `status.sh`, `feature_check.sh`, `docker logs`, `docker inspect` |
| TERBATAS | Menghentikan/menyalakan satu layanan sesaat | `docker compose restart wa_bridge` |
| PERUBAHAN | Membuat release, image, symlink, atau migrasi | `install.sh --apply` |
| PEMULIHAN | Mengembalikan release/backup; harus berdasarkan bukti | `rollback.sh --apply`, restore database |

> Prinsip utama: kumpulkan bukti terlebih dahulu, lalu ubah komponen paling kecil yang benar-benar bermasalah.

## 2. Baseline sistem yang harus dipertahankan

Baseline ini disusun dari paket v0.7.4 dan riwayat deployment. Saat troubleshooting, bandingkan kondisi aktual dengan tabel berikut.

| Komponen | Baseline v0.7.4 |
|---|---|
| Release aktif Telegram | `/home/umkm-bot/releases/v0.7.4-pos-ux` melalui symlink `/home/umkm-bot/current` |
| Versi Telegram | `0.7.4-pos-ux` atau `v0.7.4-pos-ux` |
| Source WA bridge | `/home/umkm-gowa/bridge_app` |
| Versi WA bridge | `0.7.4-pos-ux` pada `/home/umkm-gowa/BRIDGE_VERSION` |
| Telegram bot | `umkm-bot-bot-1` |
| Reminder worker | `umkm-bot-worker-1` |
| PostgreSQL | `umkm-bot-db-1`, PostgreSQL 17 Alpine |
| GOWA | `umkm-gowa-gowa-1`, image `aldinokemal2104/go-whatsapp-web-multidevice:v9.2.0` |
| WA bridge | `umkm-gowa-wa_bridge-1`, endpoint internal `http://127.0.0.1:8080/health` |
| Webhook GOWA | `http://wa_bridge:8080/webhooks/gowa` |
| Migrasi POS | `007` dan `008` tercatat di `schema_migrations` |
| Isolasi tenant | PostgreSQL RLS + `FORCE ROW LEVEL SECURITY` pada 8 tabel POS |
| AI | Groq, model default `openai/gpt-oss-20b`; fitur dasar tetap berjalan bila AI tidak tersedia |
| State deployment | `/home/umkm-suite-v0.7.4-pos-ux.state` |
| Backup installer | `/home/umkm-suite-backups/v0.7.4-pos-ux-<timestamp>` |
| Paket operator | `/home/msllab/UMKM-Suite-v0.7.4-POS-UX` |

### Komponen yang tidak boleh ikut berubah tanpa rencana khusus

- container, image, pairing, dan volume sesi GOWA;
- token Telegram, API key Groq, kredensial database, dan secret webhook;
- network Docker yang dipakai stack lama;
- data tenant lama dan identitas akun aktif;
- webhook GOWA yang sudah mengarah ke `wa_bridge:8080`.

### Tabel POS utama

`products`, `sales`, `sale_items`, `sale_payments`, `stock_movements`, `stock_opname_sessions`, `stock_opname_items`, dan `tenant_counters`. Undangan karyawan memakai `staff_invites`.

## 3. Gambaran arsitektur

| Jalur | Mekanisme | Data yang disentuh |
|---|---|---|
| Telegram → Bot | aiogram menerima tombol/command, memeriksa identitas dan role | tenant, pengguna, POS, keuangan, pengingat |
| WhatsApp → GOWA → WA bridge | GOWA menerima pesan; bridge melakukan deduplikasi, routing menu/teks, dan konfirmasi | tenant, POS, keuangan, AI, pending action |
| Bot/Bridge → PostgreSQL | Semua query bisnis memakai identitas server dan RLS tenant | produk, penjualan, stok, staf, transaksi |
| Bridge → Groq | Hanya intent/draft dan konteks terbatas; backend tetap memvalidasi | hasil klasifikasi, bukan jalur tulis langsung |

Mekanisme penjualan yang benar:

1. Pengguna menyebut produk, jumlah, dan pembayaran.
2. Parser deterministik mencoba membaca kalimat; AI membantu bila maksud belum jelas.
3. Backend mencocokkan produk hanya di tenant aktif.
4. Sistem menampilkan preview dan sisa stok.
5. Pengguna memilih **Simpan**.
6. Backend membaca ulang role, harga, produk, dan stok.
7. Satu transaksi database membuat invoice, item, pembayaran, stock movement, pemasukan, dan audit log.
8. Bila satu langkah gagal, seluruh transaksi dibatalkan.

## 4. Panduan pengguna sehari-hari

### 4.1 WhatsApp — cara paling mudah

Ketik `MENU`. Nomor disusun ulang sesuai role, sehingga selalu berurutan. Pilih berdasarkan **nama menu** yang tampil; nomor POS milik Pemilik dapat berbeda dari nomor milik Kasir.

Menu tidak mengunci percakapan. Ketika menu tampil, pengguna tetap dapat langsung mengetik:

```text
jual susu 1 cup cash
jual 2 kopi susu dan 1 roti bakar bayar QRIS
catat pemasukan 100 ribu servis
berapa stok kopi susu?
laporan POS bulan ini
edit produk
```

Ketik `0` atau `MENU` untuk membatalkan draft dan kembali ke menu utama.

### 4.2 Penjualan WhatsApp

Contoh satu produk:

```text
jual 1 susu tunai
```

Contoh multi-produk:

```text
jual 2 kopi susu dan 1 roti bakar bayar QRIS
```

Alur normal:

1. Bot mengenali nama/SKU dan jumlah.
2. Bila metode pembayaran belum disebut, bot menanyakannya.
3. Bot menampilkan nama, jumlah, harga, total, dan sisa stok.
4. Pilih **Simpan** hanya setelah preview benar.
5. Jika salah, pilih **Ubah** atau **Batal**.

Kata `dan` dapat memisahkan produk. Bila `dan` memang bagian nama produk, parser hanya memisahkannya jika kedua sisi terbaca sebagai item yang lengkap.

### 4.3 Tambah produk dalam tiga tahap

Pilih **POS & Stok → Tambah Produk**, lalu isi:

1. Nama dan satuan opsional: `Kopi Susu | cup`.
2. Harga jual dan harga modal opsional: `18000 | 9000`.
3. Stok awal dan batas minimum opsional: `20 | 5`.

SKU dibuat otomatis. Default aman: satuan `pcs`, harga modal `0`, dan stok minimum `0`. Periksa preview sebelum menekan **Simpan**.

### 4.4 Edit produk

Ketik `edit produk`, atau pilih **POS & Stok → Edit Produk**. Setelah itu:

1. cari produk memakai nama atau SKU;
2. pilih data yang akan diubah;
3. masukkan nilai baru;
4. periksa preview;
5. konfirmasi penyimpanan.

Nama yang ambigu tidak dipilih otomatis. Pengguna harus memilih produk yang benar.

### 4.5 Stok dan laporan

- **Atur Stok Fisik** menetapkan jumlah akhir hasil hitung, bukan menambahkan kuantitas.
- **Stok Rendah** menampilkan produk yang telah mencapai batas minimum.
- **Laporan POS** membaca omzet/penjualan tenant pengguna.
- INVENTORY dapat mengelola produk, stok, dan opname, tetapi tidak mencatat pemasukan umum.
- FINANCE dapat mengelola keuangan dan membaca laporan POS, tetapi tidak menjual.

### 4.6 Menambah karyawan tanpa MASTER

Pemilik/Administrator memilih **POS & Stok → Kelola Karyawan → Tambah Karyawan**, mengisi nama dan role, lalu menerima kode seperti:

```text
GABUNG 12345678
```

Karyawan mengirim kode tersebut ke WhatsApp bot. Kode berlaku 30 menit dan hanya dapat dipakai sekali. OWNER dapat mengundang ADMIN, FINANCE, CASHIER, INVENTORY, dan VIEWER. ADMIN tidak dapat membuat ADMIN lain.

### 4.7 Telegram

Jalur utama Telegram adalah tombol **🛒 POS & Stok**. Tombol ditampilkan sesuai role. Command teknis berada di **Fitur Lanjutan** untuk kebutuhan operator.

Perintah `/sale` tidak langsung menyimpan. Contoh:

```text
/sale KOPI:1 | CASH
```

Bot menampilkan preview. Stok dan keuangan baru berubah setelah tombol **Simpan** ditekan.

## 5. Role dan batas akses

| Role | Hak utama | Tidak boleh |
|---|---|---|
| OWNER | Semua data tenant, keuangan, penjualan, produk, stok, opname, staf, nama usaha | Mengakses tenant lain |
| ADMIN | Keuangan, penjualan, produk, stok, opname, staf non-ADMIN | Mengubah nama usaha; membuat ADMIN lain |
| FINANCE | Catat/baca/export keuangan; baca POS | Menjual, mengubah produk/stok, mengelola staf |
| CASHIER | Membuat penjualan dan membaca POS | Mengubah produk/stok, keuangan umum, staf |
| INVENTORY | Produk, stok, opname, baca POS | Membuat penjualan, keuangan umum, staf |
| VIEWER | Membaca keuangan dan POS | Semua tindakan tulis |

Menu yang disembunyikan bukan satu-satunya pengaman. Backend tetap memeriksa role pada saat aksi dijalankan. Semua pencarian produk dan data bisnis tetap dibatasi tenant aktif.

## 6. Fitur AI: kemampuan, batas, dan pemeriksaan

AI berfungsi sebagai penerjemah bahasa pengguna menjadi intent/draft. AI **bukan** pemegang akses database dan tidak boleh mengklaim telah menyimpan data.

### Yang dapat dibantu AI

- pemasukan dan pengeluaran dari bahasa sehari-hari;
- pengingat dengan tanggal/waktu yang cukup jelas;
- ringkasan dan pertanyaan keuangan dari konteks tenant;
- penjualan, cek stok, dan laporan POS;
- pertanyaan bisnis umum secara ringkas.

### Pengaman AI

- intent tulis membutuhkan detail yang cukup dan ambang keyakinan;
- penjualan harus memiliki item, jumlah, dan metode pembayaran yang valid;
- produk dicocokkan kembali secara deterministik di tenant aktif;
- harga dan stok selalu dibaca ulang dari database;
- pemasukan, pengeluaran, pengingat, dan penjualan mengikuti layar konfirmasi;
- permintaan mengakses tenant, prompt, token, secret, atau database lain ditolak;
- konteks bisnis diperlakukan sebagai data pasif, bukan instruksi.

### Bila AI mati

Menu, command, parser penjualan cepat, database, dan transaksi dasar tetap dapat berjalan. Pengguna dapat memakai tombol atau format yang lebih eksplisit. Jadi status AI tidak boleh disamakan dengan status seluruh aplikasi.

### Periksa status AI — AMAN

```bash
sudo docker exec umkm-gowa-wa_bridge-1 \
  python -c 'from bridge_app import ai; print(ai.public_status())'
```

Hasil normal menyerupai:

```text
aktif · groq · openai/gpt-oss-20b
```

Jangan mencetak nilai `GROQ_API_KEY` ke terminal, screenshot, atau tiket dukungan.

## 7. Peta source untuk pengembangan lanjutan

| Lokasi paket | Fungsi |
|---|---|
| `bridge_app/main.py` | Webhook, routing WhatsApp, state percakapan, menu, dan konfirmasi |
| `bridge_app/menu_ux.py` | Keputusan menu pasif vs teks bebas dan helper wizard |
| `bridge_app/pos_parser.py` | Parser penjualan/angka/format POS |
| `bridge_app/product_match.py` | Pencocokan nama/SKU produk per tenant |
| `bridge_app/pos.py` | Layanan bisnis POS untuk WhatsApp |
| `bridge_app/ai.py` | Provider, prompt, normalisasi intent, retry, dan batas AI |
| `bridge_app/db.py` | Identitas WhatsApp, tenant, pending action, dan akses database |
| `bot_release/app/bot.py` | Entry point dan menu Telegram |
| `bot_release/app/pos_handlers.py` | Tombol, wizard, command, preview, dan konfirmasi Telegram |
| `bot_release/app/permissions.py` | Matriks izin server-side |
| `db/migrations/007_pos.sql` | POS, stok, penjualan, pembayaran, RLS, fungsi atomik |
| `db/migrations/008_owner_wa_staff_ux.sql` | OWNER, daftar usaha, undangan staf WhatsApp |
| `scripts/install.sh` | Preflight, backup, release, migrasi, build, feature check |
| `scripts/rollback.sh` | Pemulihan release dan source WA bridge |
| `scripts/feature_check.sh` | Kontrak minimum versi, container, routing, AI, tabel, RLS, migrasi |
| `tests/` | Regression, parser, routing, AI normalize, preview, dan kontrak paket |

Known incident yang wajib dipertahankan sebagai regression test:

> Pada v0.7.3, state menu utama menangkap semua teks setelah `MENU`, sehingga `edit produk`, `jual susu 1 cup cash`, dan `catat pemasukan 100 ribu` dibalas “Pilih angka”. v0.7.4 memperbaikinya dengan hanya menangkap input angka dan melepas teks bebas ke router POS/AI.

## 8. Pemeriksaan cepat 5 menit

Jalankan dari sesi SSH yang stabil. Perintah pada bagian ini bersifat baca saja.

### 8.1 Status dan kontrak fitur

```bash
cd /home/msllab/UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/status.sh
sudo ./scripts/feature_check.sh
```

Hasil target:

- Telegram dan WA bridge menunjukkan `0.7.4-pos-ux`;
- database, bot, worker, GOWA, dan bridge berjalan;
- endpoint bridge sehat;
- routing teks bebas, sinonim penjualan, dan wizard edit produk lulus;
- 8 tabel POS dan RLS FORCE terdeteksi;
- migrasi `007` dan `008` tercatat;
- baris terakhir `FEATURE CHECK LULUS`.

> Jangan memakai `install.sh --check` untuk mengecek instalasi yang sudah aktif. Installer v0.7.4 hanya menerima sumber upgrade v0.7.2. Setelah v0.7.4 aktif, gunakan `status.sh` dan `feature_check.sh`.

### 8.2 Versi dan container

```bash
readlink -f /home/umkm-bot/current
cat /home/umkm-bot/current/VERSION
cat /home/umkm-gowa/BRIDGE_VERSION
sudo docker inspect umkm-gowa-gowa-1 --format '{{.Config.Image}}'
sudo docker ps --filter name=umkm-bot --filter name=umkm-gowa \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

### 8.3 Kesehatan bridge dan database

```bash
sudo docker exec umkm-gowa-wa_bridge-1 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=5).read().decode())"

sudo docker exec umkm-bot-db-1 \
  pg_isready -U umkm_owner -d umkm_bot
```

### 8.4 Migrasi dan RLS

```bash
sudo docker exec umkm-bot-db-1 psql -X -U umkm_owner -d umkm_bot -c \
  "select version,description,applied_at from schema_migrations order by applied_at;"

sudo docker exec umkm-bot-db-1 psql -X -U umkm_owner -d umkm_bot -c \
  "select c.relname,c.relrowsecurity,c.relforcerowsecurity
   from pg_class c join pg_namespace n on n.oid=c.relnamespace
   where n.nspname='public'
     and c.relname in ('products','sales','sale_items','sale_payments',
       'stock_movements','stock_opname_sessions','stock_opname_items','tenant_counters')
   order by c.relname;"
```

Semua baris POS harus menunjukkan `relrowsecurity = t` dan `relforcerowsecurity = t`.

## 9. Pengambilan log dan bukti insiden

Gunakan rentang waktu dan jumlah baris terbatas agar log mudah dibaca.

```bash
sudo docker logs --since 30m --tail 200 umkm-gowa-wa_bridge-1
sudo docker logs --since 30m --tail 200 umkm-bot-bot-1
sudo docker logs --since 30m --tail 200 umkm-bot-worker-1
sudo docker logs --since 30m --tail 200 umkm-bot-db-1
```

Mencari error penting:

```bash
sudo docker logs --since 30m umkm-gowa-wa_bridge-1 2>&1 | \
  grep -Ei 'traceback|exception|error|failed|gagal' | tail -100

sudo docker logs --since 30m umkm-bot-bot-1 2>&1 | \
  grep -Ei 'traceback|exception|error|failed|gagal' | tail -100
```

Simpan snapshot diagnosis tanpa mengubah layanan:

```bash
INCIDENT_LOG="/home/msllab/umkm-incident-$(date -u +%Y%m%d-%H%M%S).log"
{
  date -Is
  readlink -f /home/umkm-bot/current
  cat /home/umkm-bot/current/VERSION
  cat /home/umkm-gowa/BRIDGE_VERSION
  sudo docker ps --filter name=umkm-bot --filter name=umkm-gowa
  sudo docker logs --since 30m --tail 200 umkm-gowa-wa_bridge-1 2>&1
  sudo docker logs --since 30m --tail 200 umkm-bot-bot-1 2>&1
} | tee "$INCIDENT_LOG"
echo "Snapshot: $INCIDENT_LOG"
```

Sebelum membagikan log, hapus atau sensor nomor telepon, Telegram ID, kode undangan, isi pesan pelanggan, token, API key, dan detail transaksi pribadi.

## 10. Matriks troubleshooting

| Gejala | Lapisan paling mungkin | Pemeriksaan awal | Aksi aman berikutnya |
|---|---|---|---|
| WhatsApp tidak membalas sama sekali | GOWA, bridge, webhook | `docker ps`, health bridge, log GOWA/bridge | Jika bridge mati, restart bridge saja; jangan recreate GOWA |
| Bot terus berkata “Pilih angka” untuk teks bebas | Routing/menu atau image bridge lama | versi bridge; feature check “Routing MENU ke teks bebas” | Pastikan bridge v0.7.4; rebuild melalui installer/rollback resmi, bukan patch live |
| Telegram tidak menjawab | bot container, token, polling | status container dan log bot | restart service `bot` saja setelah log diambil |
| Pengingat tidak terkirim | worker container atau jadwal | status dan log worker | restart `worker`; jangan restart database |
| AI tidak tersedia | konfigurasi/key/jaringan/provider | `ai.public_status()` dan log bridge | gunakan menu/parser dasar; perbaiki konfigurasi tanpa mencetak key |
| Produk tidak ditemukan | tenant berbeda, produk nonaktif, nama ambigu | daftar produk pada akun yang sama | gunakan SKU atau nama lebih lengkap; cek akun/tenant aktif |
| Penjualan ditolak | stok, role, produk, pembayaran | baca pesan validasi dan preview | perbaiki data input; jangan edit stok dengan SQL |
| Stok berubah tidak sesuai | transaksi lama, stock movement, operasi set stok | cek invoice dan riwayat movement | hentikan koreksi berulang; audit transaksi lalu gunakan fungsi aplikasi |
| Kode `GABUNG` gagal | kedaluwarsa, sudah dipakai, format salah | waktu pembuatan dan role pembuat | buat undangan baru; jangan memasukkan staf lewat SQL |
| Menu role tidak sesuai | role/staf nonaktif atau sesi lama | daftar staf dan role; kirim `MENU` ulang | logout/link ulang bila identitas salah; jangan memberi akses berlebih |
| Database tidak sehat | PostgreSQL/container/storage | `pg_isready`, log DB, ruang disk | jangan restart berulang; amankan backup dan periksa storage |
| Respons ganda | lebih dari satu bridge/webhook atau retry | jumlah container, log message ID | pastikan hanya satu bridge aktif; jangan menambah webhook kedua |
| Bridge `connection refused` setelah deploy | aplikasi belum selesai startup | retry health hingga 60 detik | tunggu dan ulangi health check; jangan langsung rollback |

### 10.1 WhatsApp tidak membalas

```bash
sudo docker inspect umkm-gowa-gowa-1 --format '{{.State.Running}}'
sudo docker inspect umkm-gowa-wa_bridge-1 --format '{{.State.Running}}'
sudo docker logs --since 15m --tail 200 umkm-gowa-gowa-1
sudo docker logs --since 15m --tail 200 umkm-gowa-wa_bridge-1
```

Jika GOWA berjalan tetapi health bridge gagal, restart hanya bridge:

```bash
cd /home/umkm-gowa
sudo docker compose restart wa_bridge
```

Tunggu 10–20 detik, lalu ulangi health check. Jangan menjalankan `docker compose down`, jangan menghapus volume, dan jangan recreate GOWA hanya karena bridge bermasalah.

### 10.2 Regresi “Pilih angka”

```bash
cat /home/umkm-gowa/BRIDGE_VERSION

sudo docker exec umkm-gowa-wa_bridge-1 python -c \
  "from bridge_app.menu_ux import should_yield_to_free_text as y; \
assert y('menu_main','jual susu 1 cup cash'); \
assert y('menu_main','catat pemasukan 100 ribu'); \
assert y('menu_main','edit produk'); \
assert not y('menu_main','9'); print('routing OK')"
```

Jika versi file sudah v0.7.4 tetapi test gagal, image container mungkin tidak sesuai source. Ambil log dan status; pulihkan melalui installer release atau rollback resmi. Jangan mengedit file di dalam container.

### 10.3 Telegram atau worker berhenti

```bash
sudo docker logs --since 30m --tail 200 umkm-bot-bot-1
sudo docker logs --since 30m --tail 200 umkm-bot-worker-1
```

Restart terarah:

```bash
cd /home/umkm-bot/current
sudo docker compose restart bot
sudo docker compose restart worker
```

Restart tidak membangun ulang image. Bila masalah berasal dari source/release, gunakan prosedur deployment/rollback, bukan restart berulang.

### 10.4 Database atau disk

```bash
df -h /home
sudo docker exec umkm-bot-db-1 pg_isready -U umkm_owner -d umkm_bot
sudo docker logs --since 30m --tail 200 umkm-bot-db-1
```

Jika disk hampir penuh, jangan menjalankan prune dengan volume. Inventarisasi file besar dan backup lama terlebih dahulu, lalu tentukan retensi secara sadar.

### 10.5 Compose tidak valid

```bash
cd /home/umkm-bot/current
sudo docker compose config --quiet

cd /home/umkm-gowa
sudo docker compose config --quiet
```

Tidak ada output berarti konfigurasi valid. Error harus diperbaiki pada file release/paket yang dilacak, bukan dengan perubahan acak pada container.

## 11. Backup manual sebelum perubahan besar

Installer membuat backup otomatis. Untuk perubahan non-installer, buat backup database manual terlebih dahulu:

```bash
BACKUP_DIR="/home/umkm-suite-backups/manual-$(date -u +%Y%m%d-%H%M%S)"
sudo mkdir -p "$BACKUP_DIR"
sudo docker exec umkm-bot-db-1 \
  pg_dump -U umkm_owner -d umkm_bot -Fc | \
  sudo tee "$BACKUP_DIR/umkm_bot.dump" >/dev/null
sudo test -s "$BACKUP_DIR/umkm_bot.dump"
sudo sha256sum "$BACKUP_DIR/umkm_bot.dump" | \
  sudo tee "$BACKUP_DIR/SHA256SUMS"
echo "Backup: $BACKUP_DIR"
```

Periksa katalog dump tanpa memulihkan:

```bash
sudo pg_restore --list "$BACKUP_DIR/umkm_bot.dump" | head -40
```

Restore produksi adalah tindakan destruktif. Pada arsitektur ini, rollback aplikasi tidak menghapus migrasi additive dan biasanya lebih aman daripada restore database. Jangan restore sebelum:

1. membuat backup baru dari kondisi terkini;
2. memastikan file dump dan checksum benar;
3. mencatat release yang aktif;
4. menghentikan penulisan dari bot/bridge;
5. menguji restore pada database terpisah;
6. mendapat persetujuan pemilik sistem.

## 12. Deployment v0.7.4 — referensi historis

Bagian ini menjelaskan pemasangan dari baseline v0.7.2. Jangan jalankan ulang pada server yang sudah aktif v0.7.4.

Verifikasi paket:

```bash
cd /home/msllab
sha256sum UMKM-Suite-v0.7.4-POS-UX.zip
```

Checksum paket yang pernah diserahkan:

```text
613ad5061c7695e32ab7606ab5141905e45ce63f58fa606f3df93a3134c14a2a
```

Preflight dari v0.7.2:

```bash
cd /home/msllab
unzip UMKM-Suite-v0.7.4-POS-UX.zip
cd UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/install.sh --check
```

Jika dan hanya jika `PREFLIGHT OK`:

```bash
set -o pipefail
sudo ./scripts/install.sh --apply 2>&1 | \
  tee /home/msllab/umkm-v0.7.4-install.log
sudo ./scripts/status.sh
sudo ./scripts/feature_check.sh
```

Installer:

- membuat dump PostgreSQL;
- menyalin release immutable baru;
- mencadangkan source WA bridge;
- menjalankan migrasi `007`/`008` secara idempotent;
- mengaktifkan symlink release;
- membangun ulang bot, worker, dan WA bridge;
- tidak membuat ulang container GOWA;
- menjalankan feature check dengan retry startup bridge.

## 13. Rollback aplikasi v0.7.4

Gunakan bila setelah deployment terbukti ada regresi release dan state file masih tersedia.

```bash
cd /home/msllab/UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/rollback.sh --apply 2>&1 | \
  tee /home/msllab/umkm-v0.7.4-rollback.log
```

Sesudah rollback:

```bash
sudo ./scripts/status.sh
```

Kemudian jalankan feature check milik versi yang kembali aktif, bukan milik v0.7.4.

Rollback melakukan:

- mengembalikan symlink `/home/umkm-bot/current`;
- memulihkan backup source WA bridge;
- membangun ulang bot, worker, dan bridge;
- menyimpan release yang dibatalkan dengan suffix bertanggal;
- mempertahankan tabel/migrasi POS additive agar data dapat dipulihkan.

Rollback memerlukan `/home/umkm-suite-v0.7.4-pos-ux.state`. Jangan mengedit atau menghapus file tersebut sebelum keputusan menerima release atau rollback selesai.

## 14. Prosedur membuat versi berikutnya

Gunakan alur ini untuk v0.7.5 dan seterusnya.

1. **Bekukan baseline.** Catat release aktif, versi bridge, image GOWA, container ID GOWA, migrasi, disk, status AI, dan hasil feature check.
2. **Definisikan acceptance.** Setiap bug pengguna harus memiliki contoh input dan hasil yang diharapkan.
3. **Salin dari release aktif.** Jangan membangun dari paket lama atau release yang pernah di-rollback.
4. **Pertahankan kompatibilitas.** Database, tenant, role, GOWA, sesi, webhook, network, dan data lama harus tetap digunakan.
5. **Migrasi additive/idempotent.** Hindari drop/rename kolom pada jalur upgrade rutin.
6. **Pisahkan parser, AI, dan eksekusi.** AI membuat draft; backend memvalidasi; pengguna mengonfirmasi.
7. **Tambahkan regression test.** Minimal mencakup laporan bug asli, role berbeda, tenant berbeda, dan konfirmasi tanpa penulisan ganda.
8. **Bangun paket immutable.** Sertakan VERSION, release notes, deployment, rollback, feature check, dan checksum.
9. **Preflight dulu.** `--check` harus baca saja dan menolak baseline yang belum divalidasi.
10. **Backup sebelum mutasi.** Dump database dan source bridge harus dibuat serta tidak kosong.
11. **Deploy komponen minimum.** Bot, worker, bridge; GOWA tidak direcreate bila tidak ada perubahan engine.
12. **Verifikasi dua lapis.** Feature check otomatis dan acceptance manual dari sisi pengguna.
13. **Simpan bukti.** Log install, checksum, backup, state file, dan hasil acceptance.

### Snapshot baseline sebelum mulai mengembangkan

```bash
date -Is
readlink -f /home/umkm-bot/current
cat /home/umkm-bot/current/VERSION
cat /home/umkm-gowa/BRIDGE_VERSION
sudo docker inspect umkm-gowa-gowa-1 \
  --format 'ID={{.Id}} IMAGE={{.Config.Image}} STATUS={{.State.Status}}'
sudo docker ps --filter name=umkm-bot --filter name=umkm-gowa
df -h /home
```

### Pengujian minimum versi berikutnya

| Kelompok | Uji wajib |
|---|---|
| Package | VERSION, file wajib, shell syntax, Python compile |
| Parser | satu produk, multi-produk dengan koma/`dan`, satuan, pembayaran |
| Routing | menu angka, teks bebas sesudah `MENU`, pembatalan state |
| Produk | tambah tiga tahap, edit, nama ambigu, tenant isolation |
| Penjualan | preview, ubah, batal, simpan tepat sekali, stok tidak cukup |
| Role | OWNER, ADMIN, FINANCE, CASHIER, INVENTORY, VIEWER |
| AI | intent, detail kurang, provider mati, prompt injection, tanpa write langsung |
| Database | migrasi idempotent, transaksi atomik, RLS FORCE |
| Deployment | preflight read-only, backup, retry health, rollback |

### Kriteria siap produksi

- semua test lokal lulus;
- source dapat dikompilasi dan shell syntax valid;
- ZIP lolos uji integritas dan checksum dicatat;
- tidak ada secret tertanam di source;
- upgrade dari versi aktif diuji, bukan hanya fresh install;
- GOWA dan sesi tidak direcreate;
- feature check lulus;
- acceptance pengguna lulus;
- rollback diuji atau setidaknya diverifikasi secara struktural.

## 15. Larangan operasional

Jangan menjalankan perintah berikut sebagai cara cepat memperbaiki masalah:

```text
docker compose down -v
docker system prune -a --volumes
rm -rf /home/umkm-bot
rm -rf /home/umkm-gowa
```

Jangan pula:

- menghapus volume database atau sesi WhatsApp;
- mengedit/DELETE/UPDATE data bisnis langsung tanpa konteks tenant;
- menambahkan instance GOWA, bridge, atau webhook kedua;
- menyalin token/API key ke chat atau dokumen;
- mengedit source di dalam container;
- mengulang `install.sh --apply` setelah error tanpa membaca log;
- menganggap container `running` berarti endpoint aplikasi sudah siap;
- memulihkan database hanya untuk membatalkan perubahan source aplikasi.

## 16. Formulir serah-terima insiden

Salin format berikut ketika meminta bantuan:

```text
Waktu kejadian (zona waktu):
Versi aktif Telegram:
Versi WA bridge:
Kanal: WhatsApp / Telegram / keduanya
Pengguna dan role (tanpa nomor lengkap):
Tenant/usaha (samarkan bila perlu):
Kalimat/tombol yang digunakan:
Hasil yang diharapkan:
Hasil aktual:
Apakah data sempat berubah:
Error pertama pada log:
Hasil status.sh:
Hasil feature_check.sh:
Path log instalasi/insiden:
Release terakhir yang diketahui baik:
Path backup/state file:
Tindakan yang sudah dilakukan:
```

Lampirkan hanya potongan log di sekitar waktu kejadian. Jangan lampirkan `.env`, token, API key, password database, pairing data, atau data pelanggan mentah.

## 17. Checklist bulanan

| Pemeriksaan | Lulus bila |
|---|---|
| Release | Symlink dan VERSION sesuai release yang disetujui |
| Container | bot, worker, DB, GOWA, bridge berjalan |
| Feature check | Baris terakhir `FEATURE CHECK LULUS` |
| AI | status diketahui; kegagalan AI tidak memblokir menu dasar |
| Database | `pg_isready` sukses; migrasi tidak berubah tak terencana |
| RLS | 8 tabel POS tetap `ENABLE` dan `FORCE` |
| Disk | Ruang `/home` cukup untuk backup dan image baru |
| Backup | Dump terbaru ada, tidak kosong, checksum tercatat |
| UX | `MENU`, teks bebas, preview penjualan, produk, staf diuji singkat |
| Log | Tidak ada error berulang atau respons ganda |

## 18. Ringkasan satu halaman

Jika sistem bermasalah, jalankan:

```bash
cd /home/msllab/UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/status.sh
sudo ./scripts/feature_check.sh
sudo docker logs --since 30m --tail 200 umkm-gowa-wa_bridge-1
sudo docker logs --since 30m --tail 200 umkm-bot-bot-1
```

Keputusan cepat:

- **Feature check lulus, satu input gagal:** tambah regression test pada parser/routing.
- **AI gagal:** gunakan menu dasar; periksa provider tanpa restart seluruh stack.
- **Bridge/GOWA:** bila GOWA sehat, restart bridge. **Bot/DB:** bila DB sehat, restart bot.
- **Regresi release:** rollback aplikasi dengan state file; jangan restore DB otomatis.
- **DB/disk:** hentikan perubahan, backup, dan eskalasi dengan log.
