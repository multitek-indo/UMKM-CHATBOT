#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:---check}"
BOT_ROOT="/home/umkm-bot"
GOWA_ROOT="/home/umkm-gowa"
DB_CONTAINER="umkm-bot-db-1"
BOT_RELEASE="${BOT_ROOT}/releases/v0.7.4-pos-ux"
STATE_FILE="/home/umkm-suite-v0.7.4-pos-ux.state"
PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Perintah tidak tersedia: $1"; }

[[ "${EUID}" -eq 0 ]] || die "Jalankan dengan sudo."
[[ "${MODE}" == "--check" || "${MODE}" == "--apply" ]] || die "Gunakan --check atau --apply."
need docker
need readlink

for path in \
  "${BOT_ROOT}/current/docker-compose.yml" \
  "${GOWA_ROOT}/docker-compose.yml" \
  "${PACKAGE_ROOT}/db/migrations/007_pos.sql" \
  "${PACKAGE_ROOT}/db/migrations/008_owner_wa_staff_ux.sql" \
  "${PACKAGE_ROOT}/bot_release/app/pos.py" \
  "${PACKAGE_ROOT}/bridge_app/pos.py"; do
  [[ -e "${path}" ]] || die "File wajib tidak ditemukan: ${path}"
done

CURRENT_RELEASE="$(readlink -f "${BOT_ROOT}/current")"
CURRENT_VERSION="$(tr -d '\r\n' < "${BOT_ROOT}/current/VERSION")"
case "${CURRENT_VERSION}" in
  0.7.2-pos-ai-ux|v0.7.2-pos-ai-ux) ;;
  *) die "Versi aktif ${CURRENT_VERSION} belum divalidasi untuk upgrade ini." ;;
esac
docker inspect "${DB_CONTAINER}" >/dev/null 2>&1 || die "Container database tidak ditemukan."
docker inspect umkm-bot-bot-1 >/dev/null 2>&1 || die "Container Telegram bot tidak ditemukan."
docker inspect umkm-gowa-gowa-1 >/dev/null 2>&1 || die "Container GOWA tidak ditemukan."
docker inspect umkm-gowa-wa_bridge-1 >/dev/null 2>&1 || die "Container WA bridge tidak ditemukan."
GOWA_CONTAINER_ID="$(docker inspect umkm-gowa-gowa-1 --format '{{.Id}}')"

echo "UMKM Suite v0.7.4 POS UX preflight"
echo "Current release : ${CURRENT_RELEASE}"
echo "Current version : ${CURRENT_VERSION}"
echo "Target release  : ${BOT_RELEASE}"
echo "GOWA image      : $(docker inspect umkm-gowa-gowa-1 --format '{{.Config.Image}}')"
echo "DB              : $(docker exec "${DB_CONTAINER}" psql -X -U umkm_owner -d umkm_bot -Atc 'select version()' | cut -d, -f1)"
echo "Disk free /home : $(df -h /home | awk 'NR==2{print $4}')"

docker exec "${DB_CONTAINER}" pg_isready -U umkm_owner -d umkm_bot >/dev/null
(cd "${BOT_ROOT}/current" && docker compose config --quiet)
(cd "${GOWA_ROOT}" && docker compose config --quiet)

if [[ "${MODE}" == "--check" ]]; then
  echo "PREFLIGHT OK — belum ada perubahan. Jalankan: sudo ./scripts/install.sh --apply"
  exit 0
fi

[[ ! -e "${BOT_RELEASE}" ]] || die "Target release sudah ada: ${BOT_RELEASE}. Gunakan rollback/bersihkan secara manual setelah verifikasi."

STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_ROOT="/home/umkm-suite-backups/v0.7.4-pos-ux-${STAMP}"
BRIDGE_BACKUP="${BACKUP_ROOT}/umkm-gowa"
mkdir -p "${BACKUP_ROOT}" "${BRIDGE_BACKUP}"

echo "[1/8] Backup database..."
docker exec "${DB_CONTAINER}" pg_dump -U umkm_owner -d umkm_bot -Fc > "${BACKUP_ROOT}/umkm_bot.pre-v0.7.4.dump"
test -s "${BACKUP_ROOT}/umkm_bot.pre-v0.7.4.dump" || die "Backup database kosong."

echo "[2/8] Siapkan release immutable baru..."
mkdir -p "${BOT_RELEASE}"
cp -a "${CURRENT_RELEASE}/." "${BOT_RELEASE}/"
cp -a "${PACKAGE_ROOT}/bot_release/app/." "${BOT_RELEASE}/app/"
cp -a "${PACKAGE_ROOT}/bot_release/Dockerfile" "${BOT_RELEASE}/Dockerfile"
cp -a "${PACKAGE_ROOT}/bot_release/requirements.txt" "${BOT_RELEASE}/requirements.txt"
cp -a "${PACKAGE_ROOT}/bot_release/VERSION" "${BOT_RELEASE}/VERSION"
cp -a "${PACKAGE_ROOT}/db/migrations/007_pos.sql" "${BOT_RELEASE}/db/migrations/007_pos.sql"
cp -a "${PACKAGE_ROOT}/db/migrations/008_owner_wa_staff_ux.sql" "${BOT_RELEASE}/db/migrations/008_owner_wa_staff_ux.sql"

echo "[3/8] Backup dan overlay WA bridge (GOWA engine tidak disentuh)..."
cp -a "${GOWA_ROOT}/bridge_app" "${BRIDGE_BACKUP}/bridge_app"
cp -a "${GOWA_ROOT}/Dockerfile.bridge" "${BRIDGE_BACKUP}/Dockerfile.bridge"
cp -a "${GOWA_ROOT}/requirements.txt" "${BRIDGE_BACKUP}/requirements.txt"
if [[ -f "${GOWA_ROOT}/BRIDGE_VERSION" ]]; then
  cp -a "${GOWA_ROOT}/BRIDGE_VERSION" "${BRIDGE_BACKUP}/BRIDGE_VERSION"
fi
cp -a "${PACKAGE_ROOT}/bridge_app/." "${GOWA_ROOT}/bridge_app/"
cp -a "${PACKAGE_ROOT}/Dockerfile.bridge" "${GOWA_ROOT}/Dockerfile.bridge"
cp -a "${PACKAGE_ROOT}/requirements.bridge.txt" "${GOWA_ROOT}/requirements.txt"
echo "0.7.4-pos-ux" > "${GOWA_ROOT}/BRIDGE_VERSION"

cat > "${STATE_FILE}" <<EOF
PREVIOUS_RELEASE=${CURRENT_RELEASE}
TARGET_RELEASE=${BOT_RELEASE}
BACKUP_ROOT=${BACKUP_ROOT}
BRIDGE_BACKUP=${BRIDGE_BACKUP}
INSTALLED_AT=${STAMP}
GOWA_CONTAINER_ID=${GOWA_CONTAINER_ID}
EOF

UPGRADE_STARTED=1
on_error() {
  local code=$?
  trap - ERR
  if [[ "${UPGRADE_STARTED}" -eq 1 ]]; then
    echo "Upgrade gagal (exit ${code}); mencoba rollback aplikasi otomatis..." >&2
    "${PACKAGE_ROOT}/scripts/rollback.sh" --apply || echo "ROLLBACK OTOMATIS GAGAL — gunakan backup ${BACKUP_ROOT}" >&2
  fi
  exit "${code}"
}
trap on_error ERR

echo "[4/8] Pastikan migrasi POS v0.7.0 tersedia..."
docker exec -i "${DB_CONTAINER}" psql -X -v ON_ERROR_STOP=1 -U umkm_owner -d umkm_bot < "${PACKAGE_ROOT}/db/migrations/007_pos.sql"

echo "[5/8] Terapkan migrasi OWNER dan WhatsApp UX..."
docker exec -i "${DB_CONTAINER}" psql -X -v ON_ERROR_STOP=1 -U umkm_owner -d umkm_bot < "${PACKAGE_ROOT}/db/migrations/008_owner_wa_staff_ux.sql"

echo "[6/8] Aktifkan Telegram release baru..."
ln -sfn "${BOT_RELEASE}" "${BOT_ROOT}/current"
(
  cd "${BOT_ROOT}/current"
  docker compose up -d --build bot worker
)

echo "[7/8] Rebuild WA bridge saja..."
(
  cd "${GOWA_ROOT}"
  docker compose up -d --build --no-deps wa_bridge
)

echo "[8/8] Feature check dengan startup retry..."
"${PACKAGE_ROOT}/scripts/feature_check.sh"

UPGRADE_STARTED=0
trap - ERR

echo "UPGRADE v0.7.4 BERHASIL"
echo "Backup: ${BACKUP_ROOT}"
echo "GOWA session/volume dan container gowa tidak diubah."
