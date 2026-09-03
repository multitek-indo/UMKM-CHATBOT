# Tag dan Release UMKM Suite

## Perbedaan istilah

- **Commit** adalah satu titik perubahan source.
- **Tag** adalah penanda permanen yang menunjuk ke satu commit tertentu.
- **Release** adalah halaman rilis yang memakai tag dan dapat berisi deskripsi serta file installer.

Tag tidak menjadi tempat menyimpan file. Source harus sudah ada dalam commit; ZIP installer, checksum, dan dokumen operator dilampirkan sebagai aset Release.

## Tag yang dibuat sekarang

| Kolom | Nilai |
|---|---|
| Tag | `v0.7.4` |
| Target | branch `main` setelah source v0.7.4 diunggah |
| Judul Release | `UMKM Suite v0.7.4 POS UX` |
| Status | Stable / Latest |
| Pesan annotated tag | `UMKM Suite v0.7.4 POS UX — stable release with guided POS flows and WhatsApp routing fix` |

Nilai internal aplikasi tetap `0.7.4-pos-ux`. Tag dipendekkan menjadi `v0.7.4` agar riwayat versi mudah dibaca.

## Deskripsi tag historis

Gunakan deskripsi berikut hanya setelah source setiap versi dimasukkan sebagai commit yang terpisah dan tag diarahkan ke commit yang tepat.

| Tag | Deskripsi pendek | Status |
|---|---|---|
| `v0.7.0` | Fondasi POS multi-channel, inventory, transaksi atomik, RBAC, dan RLS tenant. | Historical |
| `v0.7.1` | Menu WhatsApp bertahap dan pengelolaan karyawan mandiri melalui kode undangan tenant. | Historical |
| `v0.7.2` | Peningkatan AI POS, resolver nama produk, klarifikasi aman, dan preview transaksi. | Stable fallback |
| `v0.7.3` | UX menu/wizard awal; ditarik karena regresi routing teks bebas setelah `MENU`. | Withdrawn; jangan deploy |
| `v0.7.4` | Memperbaiki regresi v0.7.3 serta menyempurnakan menu, Telegram, preview sale, tambah/edit produk. | Current stable |

Jangan membuat seluruh tag lama pada commit v0.7.4. Tindakan itu membuat sejarah palsu karena semua tag akan menunjuk ke source yang sama.

## Pola versi berikutnya

- `v0.7.5`: perbaikan bug atau peningkatan kecil yang kompatibel.
- `v0.8.0`: fitur baru yang cukup besar tetapi tetap satu keluarga sistem.
- `v1.0.0`: kontrak aplikasi dianggap matang dan stabil untuk penggunaan produksi yang lebih luas.

Tag kandidat uji dapat memakai bentuk `v0.7.5-rc.1`. Tandai sebagai pre-release dan jangan jadikan Latest.

