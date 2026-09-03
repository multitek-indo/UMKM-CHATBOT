# Baseline Inspeksi & Kesimpulan Upgrade UMKM Suite

Status baseline: terverifikasi dari snapshot konfigurasi dan source aktif yang dikirim pada 2 September 2026. Dokumen ini tidak menyimpan token, password, API key, webhook secret, atau nilai kredensial lain.

## Ringkasan eksekutif

Sistem aktif terdiri dari dua stack Docker yang berbagi network: `umkm-bot` untuk Telegram, worker, dan PostgreSQL; `umkm-gowa` untuk GOWA engine dan WA bridge. Arsitektur sudah memiliki fondasi multi-tenant yang baik melalui `tenant_id` dan RLS, antrean WhatsApp, deduplikasi pesan, HMAC webhook, audit log, serta mekanisme MASTER support-login.

Dua kekurangan utama versi 0.6 adalah: role tersimpan tetapi belum ditegakkan pada handler/backend; dan user yang dibuat MASTER selalu menjadi OWNER bisnis sendiri, sehingga belum ada alur staf di bawah satu administrator/tenant. Belum ada tabel atau logika POS/inventory.

Kesimpulan: upgrade paling aman adalah memperluas stack aktif, bukan memasang chatbot atau GOWA kedua. POS harus menulis sale, item, payment, stock movement, saldo stok, dan transaksi keuangan di dalam satu transaksi PostgreSQL.

## Inventaris versi dan kapasitas

| Area | Baseline terverifikasi |
|---|---|
| OS | Ubuntu 24.04.4 LTS |
| Docker | Engine 29.7.2 |
| Docker Compose | v5.5.0 |
| CPU/RAM | 4 vCPU / sekitar 3,8 GB RAM |
| Disk bebas | sekitar 73 GB |
| Release Telegram aktif | `/home/umkm-bot/releases/v0.6.0` melalui `/home/umkm-bot/current` |
| GOWA stack | v0.6.1 |
| GOWA image | `aldinokemal2104/go-whatsapp-web-multidevice:v9.2.0` |
| PostgreSQL | 17.11 Alpine; database `umkm_bot`; ukuran sekitar 8,9 MB |
| AI | Groq; model `openai/gpt-oss-20b` |

## Topologi dan jalur data

1. Telegram menerima update dengan polling pada container `umkm-bot-bot-1`.
2. WhatsApp menerima event pada GOWA, lalu webhook menuju `http://wa_bridge:8080/webhooks/gowa`.
3. WA bridge memvalidasi HMAC, memasukkan event ke `whatsapp_inbox_events`, lalu worker internal memproses dengan deduplikasi dan retry.
4. Telegram bot dan WA bridge menggunakan role aplikasi database yang sama pada PostgreSQL.
5. Keduanya mengatur `app.tenant_id` per transaksi sehingga policy RLS membatasi baris tenant.
6. Reminder Telegram diproses `umkm-bot-worker-1`; reminder WhatsApp diproses worker pada WA bridge.

Network GOWA menunjuk external network `umkm-bot_backend`. GOWA tidak mempublikasikan API ke internet; port 3000 dibind ke loopback host. Volume `gowa_storage` menyimpan sesi/perangkat WhatsApp.

## Container yang relevan

| Container | Fungsi | Tindakan pada v0.7.0 |
|---|---|---|
| `umkm-bot-db-1` | PostgreSQL | Migrasi additive; container tidak diganti |
| `umkm-bot-bot-1` | Telegram | Rebuild dari release baru |
| `umkm-bot-worker-1` | Reminder Telegram | Rebuild dari release baru |
| `umkm-gowa-gowa-1` | WhatsApp engine/session | Tidak diubah dan tidak direstart |
| `umkm-gowa-wa_bridge-1` | Webhook + chatbot WA | Rebuild saja dengan `--no-deps` |

Stack `lab-bot`, `msl-lab`, Portainer, Cloudflared, dan container lain berada di luar ruang lingkup upgrade.

## Baseline database v0.6

Tabel: `users`, `tenants`, `tenant_members`, `transactions`, `reminders`, `audit_logs`, `ai_usage_logs`, `support_sessions`, `channel_link_codes`, `whatsapp_identities`, `whatsapp_inbox_events`, dan `whatsapp_pending_actions`.

RLS aktif dan dipaksa pada `transactions`, `reminders`, `audit_logs`, dan `ai_usage_logs`. Role tersimpan: `OWNER`, `ADMIN`, `FINANCE`, `CASHIER`, `VIEWER`. Fungsi security-definer tersedia untuk admin MASTER, support-login, dan worker reminder.

## Temuan keamanan/fungsional

| Prioritas | Temuan | Dampak | Keputusan v0.7.0 |
|---|---|---|---|
| Tinggi | Role dibaca ke context tetapi tidak dibandingkan sebelum aksi | Role rendah dapat mengakses fungsi yang tidak semestinya | Permission matrix server-side dan gating channel |
| Tinggi | User buatan MASTER menjadi OWNER tenant baru | Tidak ada staf di bawah satu usaha | Command staf tenant untuk OWNER/ADMIN |
| Tinggi | Belum ada POS/inventory | Penjualan tidak memengaruhi stok | Schema dan service POS atomik |
| Sedang | Hanya empat tabel lama memakai RLS | Tabel POS baru berisiko bocor jika tidak konsisten | RLS `FORCE` pada semua tabel POS |
| Positif | HMAC, Basic Auth, antrean dan dedup WA sudah tersedia | Integrasi dapat diperluas tanpa webhook baru | Gunakan bridge aktif |
| Positif | Release bot memakai symlink | Rollback source cepat | Release baru immutable + rollback symlink |

Pencarian source tidak menemukan secret hardcoded pada berkas aplikasi yang diinspeksi. Secret tetap harus berada di `.env` aktif dan tidak dicopy ke laporan/paket.

## Baseline v0.7.0 POS

Migration `007_pos.sql` menambahkan `products`, `tenant_counters`, `sales`, `sale_items`, `sale_payments`, `stock_movements`, `stock_opname_sessions`, dan `stock_opname_items`. Tabel `transactions` memperoleh `reference_type`, `reference_id`, dan `source_channel` untuk menghubungkan pembukuan dengan sale.

Role dan kemampuan:

| Role | Keuangan | POS | Produk/Stok/Opname | Staf |
|---|---|---|---|---|
| OWNER | Baca/tulis/export | Jual, lihat, void | Penuh | ADMIN dan role di bawahnya |
| ADMIN | Baca/tulis/export | Jual, lihat, void | Penuh | FINANCE/CASHIER/INVENTORY/VIEWER |
| FINANCE | Baca/tulis/export | Lihat laporan POS | Tidak | Tidak |
| CASHIER | Tidak melihat keuangan umum | Jual dan lihat produk | Tidak | Tidak |
| INVENTORY | Tidak | Lihat produk | Produk, stok, opname | Tidak |
| VIEWER | Baca | Lihat | Tidak | Tidak |

Alur sale: validasi role → lock produk → cek stok → nomor invoice → insert sale/items/payment → update stok → stock movements → insert pemasukan `transactions` → audit → commit. Bila satu langkah gagal, seluruh perubahan rollback.

Layer AI v0.7.0 menambahkan intent `create_sale`, `stock_query`, dan `pos_summary`. Hasil AI tidak dipercaya langsung: SKU, jumlah, role, tenant, harga, dan stok divalidasi backend; sale baru ditulis setelah pengguna menekan konfirmasi.

## Path penting

| Path | Arti |
|---|---|
| `/home/umkm-bot/current` | Symlink release Telegram aktif |
| `/home/umkm-bot/releases/` | Release immutable Telegram |
| `/home/umkm-bot/shared/.env` | Secret/config bersama; jangan ditampilkan |
| `/home/umkm-gowa/docker-compose.yml` | Compose GOWA + WA bridge |
| `/home/umkm-gowa/bridge_app` | Source bridge aktif |
| `/home/umkm-gowa/.env` | Secret/config GOWA; jangan ditampilkan |
| `/home/umkm-suite-backups/` | Backup upgrade v0.7.0 |

## Checklist inspeksi delta untuk upgrade berikutnya

Tidak perlu mengulang inspeksi penuh jika baseline ini masih cocok. Jalankan hanya pemeriksaan delta berikut dan bandingkan hasilnya:

```bash
date -Is
lsb_release -ds
docker version --format '{{.Server.Version}}'
docker compose version
readlink -f /home/umkm-bot/current
cat /home/umkm-bot/current/VERSION
cat /home/umkm-gowa/VERSION
cat /home/umkm-gowa/BRIDGE_VERSION 2>/dev/null || true
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}'
docker network inspect umkm-bot_backend --format '{{json .Containers}}'
docker volume inspect umkm-gowa_gowa_storage --format '{{.Mountpoint}}'
docker exec umkm-bot-db-1 psql -X -U umkm_owner -d umkm_bot -c '\dt+'
docker exec umkm-bot-db-1 psql -X -U umkm_owner -d umkm_bot -c "select version,description,applied_at from schema_migrations order by applied_at"
docker exec umkm-bot-db-1 psql -X -U umkm_owner -d umkm_bot -c "select relname,relrowsecurity,relforcerowsecurity from pg_class where relnamespace='public'::regnamespace order by relname"
docker inspect umkm-gowa-gowa-1 --format '{{.Id}} {{.Config.Image}} {{.State.Status}}'
docker inspect umkm-gowa-wa_bridge-1 --format '{{.Id}} {{.Config.Image}} {{.State.Status}}'
```

Untuk compose/env, selalu sensor secret sebelum berbagi:

```bash
sudo sed -E 's/^([[:space:]]*[^#]*(TOKEN|SECRET|PASSWORD|PASS|API_KEY|AUTH|DATABASE_URL)[^:=]*[=:]).*/\1 [REDACTED]/I' /home/umkm-bot/current/docker-compose.yml
sudo sed -E 's/^([[:space:]]*[^#]*(TOKEN|SECRET|PASSWORD|PASS|API_KEY|AUTH|DATABASE_URL)[^:=]*[=:]).*/\1 [REDACTED]/I' /home/umkm-gowa/docker-compose.yml
```

Inspeksi penuh baru diperlukan jika path release, nama container/stack/network, engine GOWA, major PostgreSQL, kontrak webhook, model penyimpanan tenant, atau mekanisme autentikasi berubah.

## Kesimpulan final

Fondasi v0.6 layak dipertahankan. Upgrade v0.7.0 harus dilakukan in-place secara additive dengan backup dan rollback source, sambil membiarkan GOWA engine serta session volume tetap hidup. Baseline ini menjadi sumber kebenaran awal; upgrade berikutnya cukup melakukan inspeksi delta dan mencatat perubahan versi, schema migration, container image, serta kontrak integrasi.
