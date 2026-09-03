#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${1:-}" == "--apply" ]] || { echo "Dry-run: gunakan sudo ./scripts/rollback.sh --apply"; exit 0; }
[[ "${EUID}" -eq 0 ]] || { echo "ERROR: Jalankan dengan sudo." >&2; exit 1; }

STATE_FILE="/home/umkm-suite-v0.7.4-pos-ux.state"
[[ -f "${STATE_FILE}" ]] || { echo "ERROR: State file tidak ditemukan: ${STATE_FILE}" >&2; exit 1; }
# shellcheck disable=SC1090
source "${STATE_FILE}"
[[ -d "${PREVIOUS_RELEASE}" ]] || { echo "ERROR: Release lama tidak ditemukan." >&2; exit 1; }
[[ -d "${BRIDGE_BACKUP}/bridge_app" ]] || { echo "ERROR: Backup bridge tidak ditemukan." >&2; exit 1; }

ln -sfn "${PREVIOUS_RELEASE}" /home/umkm-bot/current
ROLLBACK_STAGING="/home/umkm-gowa/bridge_app.rollback-staging.$(date -u +%Y%m%d-%H%M%S)"
mv /home/umkm-gowa/bridge_app "${ROLLBACK_STAGING}"
cp -a "${BRIDGE_BACKUP}/bridge_app" /home/umkm-gowa/bridge_app
cp -a "${BRIDGE_BACKUP}/Dockerfile.bridge" /home/umkm-gowa/Dockerfile.bridge
cp -a "${BRIDGE_BACKUP}/requirements.txt" /home/umkm-gowa/requirements.txt
if [[ -f "${BRIDGE_BACKUP}/BRIDGE_VERSION" ]]; then
  cp -a "${BRIDGE_BACKUP}/BRIDGE_VERSION" /home/umkm-gowa/BRIDGE_VERSION
elif [[ -f /home/umkm-gowa/BRIDGE_VERSION ]]; then
  mv /home/umkm-gowa/BRIDGE_VERSION "${ROLLBACK_STAGING}.BRIDGE_VERSION"
fi

(
  cd /home/umkm-bot/current
  docker compose up -d --build bot worker
)
(
  cd /home/umkm-gowa
  docker compose up -d --build --no-deps wa_bridge
)

if [[ "${TARGET_RELEASE}" != "${PREVIOUS_RELEASE}" && -d "${TARGET_RELEASE}" ]]; then
  FAILED_RELEASE="${TARGET_RELEASE}.rolled-back.$(date -u +%Y%m%d-%H%M%S)"
  mv "${TARGET_RELEASE}" "${FAILED_RELEASE}"
  echo "Release yang dibatalkan disimpan: ${FAILED_RELEASE}"
fi

echo "ROLLBACK APLIKASI SELESAI"
echo "Release aktif: $(readlink -f /home/umkm-bot/current)"
echo "Catatan: schema POS additive tidak dihapus agar data tetap dapat dipulihkan."
echo "Backup database penuh: ${BACKUP_ROOT}/umkm_bot.pre-v0.7.4.dump"
