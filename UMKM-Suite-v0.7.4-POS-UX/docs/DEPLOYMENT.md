# Deployment UMKM Suite v0.7.4 POS UX

## Ruang lingkup

| Komponen | Tindakan |
|---|---|
| Release Telegram | Membuat `/home/umkm-bot/releases/v0.7.4-pos-ux` |
| Bot dan worker | Build ulang dari release baru |
| WA bridge | Backup, overlay source, lalu build `--no-deps` |
| PostgreSQL | Backup penuh; migrasi 007/008 dijalankan idempotent |
| GOWA engine dan sesi | Tidak diubah atau direstart |
| Webhook | Tetap `http://wa_bridge:8080/webhooks/gowa` |

## Prasyarat

- Versi aktif harus `0.7.2-pos-ai-ux` atau `v0.7.2-pos-ai-ux`.
- Container bot, worker, database, GOWA, dan WA bridge dari baseline tersedia.
- Compose bot dan GOWA valid.

## Preflight

```bash
cd /home/msllab/UMKM-Suite-v0.7.4-POS-UX
sudo ./scripts/install.sh --check
```

Mode ini hanya membaca konfigurasi. Jangan menjalankan `--apply` apabila versi aktif atau container berbeda dari baseline.

## Instalasi

```bash
set -o pipefail
sudo ./scripts/install.sh --apply 2>&1 | tee /home/msllab/umkm-v0.7.4-install.log
```

Installer membuat backup, release immutable, overlay bridge, rebuild aplikasi, dan feature check. Kegagalan setelah perubahan dimulai memicu rollback aplikasi otomatis.

## Verifikasi

```bash
sudo ./scripts/status.sh
sudo ./scripts/feature_check.sh
sudo docker logs --tail 100 umkm-bot-bot-1
sudo docker logs --tail 100 umkm-gowa-wa_bridge-1
```

Uji acceptance kelima perbaikan mengikuti bagian **Uji penerimaan ringkas** pada `README.md`.

## Rollback

```bash
sudo ./scripts/rollback.sh --apply
```

Rollback memerlukan `/home/umkm-suite-v0.7.4-pos-ux.state`. Release yang dibatalkan disimpan dengan nama bertanggal. Skema dan data POS tidak dihapus. Backup penuh berada pada direktori yang dicetak installer.
