# Cara Melanjutkan Pengembangan

Dokumen ini menjaga agar perubahan fitur tidak langsung merusak versi yang sedang dipakai pengguna.

## Struktur branch

- `main`: hanya versi yang sudah lulus pengujian dan layak dipasang.
- `feature/nama-fitur`: penambahan fitur baru.
- `fix/nama-masalah`: perbaikan bug.
- `docs/nama-dokumen`: perubahan dokumentasi saja.

Jangan mengembangkan fitur langsung pada `main`. Buat branch baru, uji, lalu gabungkan setelah hasilnya jelas.

## Pemeriksaan sebelum digabung

Jalankan dari root source:

```bash
./scripts/test_local.sh
```

Perubahan deployment juga harus diperiksa dengan:

```bash
bash -n scripts/*.sh
```

Sebelum membuat tag:

1. Perbarui `VERSION` dan `bot_release/VERSION` dengan nilai yang sama.
2. Perbarui `RELEASE_NOTES.md` dan `CHANGELOG.md`.
3. Pastikan migrasi database bersifat additive dan idempotent.
4. Pastikan tidak ada `.env`, token, password, dump database, log, atau backup di commit.
5. Jalankan pengujian lokal.
6. Jalankan preflight pada server.
7. Setelah deployment, jalankan `status.sh`, `feature_check.sh`, dan checklist UX.
8. Buat tag hanya pada commit yang benar-benar diuji.

## Pola commit sederhana

```text
feat: tambah fitur diskon penjualan
fix: perbaiki routing menu WhatsApp
test: tambah regression test transaksi multi-produk
docs: perbarui panduan operator
chore: siapkan release v0.7.5
```

Satu commit sebaiknya mewakili satu tujuan yang jelas agar mudah diperiksa dan dibatalkan.

