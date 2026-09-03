#!/usr/bin/env bash
set -Eeuo pipefail

echo "Release Telegram : $(readlink -f /home/umkm-bot/current)"
echo "Versi Telegram   : $(cat /home/umkm-bot/current/VERSION 2>/dev/null || echo '-')"
echo "Versi WA bridge  : $(cat /home/umkm-gowa/BRIDGE_VERSION 2>/dev/null || echo 'legacy')"
echo "Image GOWA       : $(docker inspect umkm-gowa-gowa-1 --format '{{.Config.Image}}' 2>/dev/null || echo '-')"
echo "Status AI        : $(docker exec umkm-gowa-wa_bridge-1 python -c 'from bridge_app import ai; print(ai.public_status())' 2>/dev/null || echo 'tidak dapat diperiksa')"
echo
docker ps --filter name=umkm-bot --filter name=umkm-gowa --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
echo
docker exec umkm-bot-db-1 psql -X -U umkm_owner -d umkm_bot -c "select version,description,applied_at from schema_migrations order by applied_at desc limit 5" 2>/dev/null || true
