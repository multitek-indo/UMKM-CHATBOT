#!/usr/bin/env bash
set -Eeuo pipefail

DB_CONTAINER="umkm-bot-db-1"
fail=0
check() { if eval "$2"; then echo "[OK] $1"; else echo "[FAIL] $1" >&2; fail=1; fi; }
check_retry() {
  local label="$1" command="$2" attempt
  for attempt in $(seq 1 12); do
    if eval "${command}" >/dev/null 2>&1; then
      echo "[OK] ${label}"
      return 0
    fi
    [[ "${attempt}" -lt 12 ]] && sleep 5
  done
  echo "[FAIL] ${label}" >&2
  fail=1
  return 0
}

check "Telegram release v0.7.4-pos-ux" "test \"\$(cat /home/umkm-bot/current/VERSION)\" = v0.7.4-pos-ux -o \"\$(cat /home/umkm-bot/current/VERSION)\" = 0.7.4-pos-ux"
check "Database healthy" "docker exec ${DB_CONTAINER} pg_isready -U umkm_owner -d umkm_bot >/dev/null"
check "Telegram bot running" "test \"\$(docker inspect umkm-bot-bot-1 --format '{{.State.Running}}')\" = true"
check "Reminder worker running" "test \"\$(docker inspect umkm-bot-worker-1 --format '{{.State.Running}}')\" = true"
check "GOWA healthy/running" "test \"\$(docker inspect umkm-gowa-gowa-1 --format '{{.State.Running}}')\" = true"
check "WA bridge running" "test \"\$(docker inspect umkm-gowa-wa_bridge-1 --format '{{.State.Running}}')\" = true"
check_retry "WA bridge health endpoint" "docker exec umkm-gowa-wa_bridge-1 python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5).read()\""
check "Resolver nama produk aktif" "docker exec umkm-gowa-wa_bridge-1 test -f /app/bridge_app/product_match.py"
check "Telegram POS wizard aktif" "docker exec umkm-bot-bot-1 grep -q 'class POSWizard' /app/app/pos_handlers.py"
check "Menu WhatsApp dinamis aktif" "docker exec umkm-gowa-wa_bridge-1 grep -q 'MAIN_MENU_CATALOG' /app/bridge_app/main.py"
check "Routing MENU ke teks bebas sehat" "docker exec umkm-gowa-wa_bridge-1 python -c \"from bridge_app.menu_ux import should_yield_to_free_text as y; assert y('menu_main','jual susu 1 cup cash'); assert y('menu_main','catat pemasukan 100 ribu'); assert y('menu_main','edit produk'); assert not y('menu_main','9')\""
check "Sinonim POS penjualan aktif" "docker exec umkm-gowa-wa_bridge-1 python -c \"from bridge_app.pos_parser import parse_quick_sale_request as p; items,pay=p('pos penjualan susu 1 cup cash'); assert items and pay == 'CASH'\""
check "Wizard edit produk aktif" "docker exec umkm-gowa-wa_bridge-1 python -c \"from bridge_app.menu_ux import parse_product_edit_request as p; assert p('edit produk') == (True, None); assert p('pos edit produk susu') == (True, 'susu')\" && docker exec umkm-gowa-wa_bridge-1 grep -q 'guided_pos_product_edit_value' /app/bridge_app/main.py"
check "Telegram POS wizard dapat diimpor" "docker exec umkm-bot-bot-1 python -c \"from app.pos_handlers import POSWizard; assert POSWizard\""

if docker exec umkm-gowa-wa_bridge-1 python -c "from bridge_app import ai; raise SystemExit(0 if ai.is_configured() else 1)" >/dev/null 2>&1; then
  echo "[OK] Konfigurasi Asisten AI tersedia"
else
  echo "[WARN] Asisten AI belum dikonfigurasi; menu dasar tetap berfungsi" >&2
fi

if [[ -f /home/umkm-suite-v0.7.4-pos-ux.state ]]; then
  expected_gowa_id="$(sed -n 's/^GOWA_CONTAINER_ID=//p' /home/umkm-suite-v0.7.4-pos-ux.state)"
  current_gowa_id="$(docker inspect umkm-gowa-gowa-1 --format '{{.Id}}')"
  check "Container GOWA tidak dibuat ulang" "test -n '${expected_gowa_id}' -a '${current_gowa_id}' = '${expected_gowa_id}'"
fi

table_count="$(docker exec "${DB_CONTAINER}" psql -X -U umkm_owner -d umkm_bot -Atc "select count(*) from information_schema.tables where table_schema='public' and table_name in ('products','sales','sale_items','sale_payments','stock_movements','stock_opname_sessions','stock_opname_items','tenant_counters')")"
check "8 tabel POS tersedia" "test '${table_count}' = 8"

forced_count="$(docker exec "${DB_CONTAINER}" psql -X -U umkm_owner -d umkm_bot -Atc "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relname in ('products','sales','sale_items','sale_payments','stock_movements','stock_opname_sessions','stock_opname_items','tenant_counters') and c.relrowsecurity and c.relforcerowsecurity")"
check "RLS FORCE aktif pada 8 tabel POS" "test '${forced_count}' = 8"

migration="$(docker exec "${DB_CONTAINER}" psql -X -U umkm_owner -d umkm_bot -Atc "select count(*) from schema_migrations where version='007'")"
check "Migration 007 tercatat" "test '${migration}' = 1"

invite_table="$(docker exec "${DB_CONTAINER}" psql -X -U umkm_owner -d umkm_bot -Atc "select count(*) from information_schema.tables where table_schema='public' and table_name='staff_invites'")"
check "Tabel undangan karyawan tersedia" "test '${invite_table}' = 1"

invite_functions="$(docker exec "${DB_CONTAINER}" psql -X -U umkm_owner -d umkm_bot -Atc "select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname in ('app_create_staff_invite','app_claim_staff_invite')")"
check "2 fungsi undangan karyawan tersedia" "test '${invite_functions}' = 2"

migration_008="$(docker exec "${DB_CONTAINER}" psql -X -U umkm_owner -d umkm_bot -Atc "select count(*) from schema_migrations where version='008'")"
check "Migration 008 tercatat" "test '${migration_008}' = 1"

if [[ "${fail}" -ne 0 ]]; then
  echo "FEATURE CHECK GAGAL" >&2
  exit 1
fi
echo "FEATURE CHECK LULUS"
