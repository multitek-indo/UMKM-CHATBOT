from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackageContractTest(unittest.TestCase):
    def test_required_files(self):
        for relative in (
            "db/migrations/007_pos.sql",
            "db/migrations/008_owner_wa_staff_ux.sql",
            "bot_release/app/pos.py",
            "bot_release/app/product_match.py",
            "bot_release/app/pos_handlers.py",
            "bridge_app/pos.py",
            "bridge_app/menu_ux.py",
            "scripts/install.sh",
            "scripts/rollback.sh",
            "scripts/feature_check.sh",
            "docs/UX-ACCEPTANCE-v0.7.4.md",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_migration_is_additive_and_tenant_safe(self):
        sql = (ROOT / "db/migrations/007_pos.sql").read_text()
        self.assertNotIn("DROP TABLE", sql.upper())
        self.assertGreaterEqual(sql.upper().count("FORCE ROW LEVEL SECURITY"), 8)
        for table in ("products", "sales", "sale_items", "sale_payments", "stock_movements"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)

    def test_installer_does_not_restart_gowa(self):
        script = (ROOT / "scripts/install.sh").read_text()
        self.assertIn("--no-deps wa_bridge", script)
        self.assertNotIn("up -d --build gowa", script)
        self.assertIn("008_owner_wa_staff_ux.sql", script)

    def test_staff_invites_are_function_only_and_tenant_bound(self):
        sql = (ROOT / "db/migrations/008_owner_wa_staff_ux.sql").read_text()
        upper = sql.upper()
        self.assertIn("CREATE TABLE IF NOT EXISTS STAFF_INVITES", upper)
        self.assertIn("SECURITY DEFINER", upper)
        self.assertIn("APP_CREATE_STAFF_INVITE", upper)
        self.assertIn("APP_CLAIM_STAFF_INVITE", upper)
        self.assertIn("REVOKE ALL ON TABLE STAFF_INVITES FROM UMKM_APP", upper)
        self.assertIn("TM.ROLE='OWNER'", upper)

    def test_whatsapp_has_guided_owner_and_staff_ux(self):
        source = (ROOT / "bridge_app/main.py").read_text()
        for label in (
            "Penjualan Baru", "Tambah Produk", "Atur Stok",
            "Kelola Karyawan", "Tambah Karyawan", "GABUNG {code}",
        ):
            self.assertIn(label, source)
        self.assertLess(source.index("join_match ="), source.index('if access["access_status"] != "active"'))

    def test_health_check_waits_for_bridge_readiness(self):
        script = (ROOT / "scripts/feature_check.sh").read_text()
        self.assertIn("check_retry", script)
        self.assertIn("sleep 5", script)

    def test_release_version_and_migration_copy(self):
        self.assertEqual((ROOT / "VERSION").read_text().strip(), "0.7.4-pos-ux")
        self.assertEqual((ROOT / "bot_release/VERSION").read_text().strip(), "0.7.4-pos-ux")
        self.assertEqual(
            (ROOT / "db/migrations/008_owner_wa_staff_ux.sql").read_bytes(),
            (ROOT / "bot_release/db/migrations/008_owner_wa_staff_ux.sql").read_bytes(),
        )

    def test_ai_pos_requires_confirmation_paths(self):
        ai = (ROOT / "bot_release/app/ai.py").read_text()
        telegram = (ROOT / "bot_release/app/bot.py").read_text()
        whatsapp = (ROOT / "bridge_app/main.py").read_text()
        for intent in ("create_sale", "stock_query", "pos_summary"):
            self.assertIn(f'"{intent}"', ai)
        self.assertIn("AIConfirmSale", telegram)
        self.assertIn('"pos_sale"', whatsapp)

    def test_ai_ux_keeps_sale_context_and_resolves_names(self):
        ai = (ROOT / "bridge_app/ai.py").read_text()
        db = (ROOT / "bridge_app/db.py").read_text()
        pos = (ROOT / "bridge_app/pos.py").read_text()
        whatsapp = (ROOT / "bridge_app/main.py").read_text()
        self.assertIn("product_catalog", db)
        self.assertIn("product_query", ai)
        self.assertIn("prepare_sale_preview", pos)
        for state in ("sale_payment_needed", "sale_product_choice", "sale_product_replace"):
            self.assertIn(state, whatsapp)
        self.assertIn("TOTAL:", whatsapp)
        self.assertIn("Sisa stok", whatsapp)
        self.assertIn("parse_quick_sale_request", whatsapp)
        payment_state = whatsapp.split('if action_type == "sale_payment_needed":', 1)[1]
        self.assertIn("quick_sale", payment_state[:800])

    def test_ai_pos_period_is_not_forced_to_today(self):
        ai = (ROOT / "bridge_app/ai.py").read_text()
        whatsapp = (ROOT / "bridge_app/main.py").read_text()
        self.assertIn("report_period", ai)
        self.assertIn('result.report_period == "month"', whatsapp)

    def test_whatsapp_role_menus_are_dynamic_and_sequential(self):
        source = (ROOT / "bridge_app/main.py").read_text()
        self.assertIn("MAIN_MENU_CATALOG", source)
        self.assertIn("POS_MENU_CATALOG", source)
        self.assertIn("BUSINESS_MENU_CATALOG", source)
        self.assertIn("enumerate(items, 1)", source)
        self.assertIn('"menu_main", {"actions":', source)
        self.assertIn("selected_menu_action(payload, raw)", source)

    def test_telegram_pos_uses_buttons_and_guided_wizards(self):
        source = (ROOT / "bot_release/app/pos_handlers.py").read_text()
        for label in (
            "Penjualan Baru", "Daftar Produk", "Tambah Produk", "Atur Stok",
            "POSWizard.sale_items", "POSWizard.product_name", "POSWizard.stock_product",
        ):
            self.assertIn(label, source)
        self.assertIn('("🧾 Penjualan Baru", "posux:sale")', source)
        self.assertIn('("➕ Tambah Produk", "posux:product_add")', source)

    def test_telegram_sale_command_previews_before_writing(self):
        source = (ROOT / "bot_release/app/pos_handlers.py").read_text()
        start = source.index('@router.message(Command("sale"))')
        end = source.index('@router.message(Command("pos_report"))', start)
        handler = source[start:end]
        self.assertIn("prepare_telegram_sale", handler)
        self.assertNotIn("create_sale", handler)
        self.assertIn('F.data == "posux:sale:save"', source)

    def test_quick_product_flow_has_three_required_steps(self):
        source = (ROOT / "bridge_app/main.py").read_text()
        self.assertIn("TAMBAH PRODUK — 1/3", source)
        self.assertIn("TAMBAH PRODUK — 2/3", source)
        self.assertIn("TAMBAH PRODUK — 3/3", source)
        self.assertIn('"cost_price": 0', source)
        self.assertIn('"min_stock": "0"', source)

    def test_passive_menu_cannot_block_free_text(self):
        source = (ROOT / "bridge_app/main.py").read_text()
        helper = (ROOT / "bridge_app/menu_ux.py").read_text()
        self.assertIn("should_yield_to_free_text(action_type, raw)", source)
        self.assertIn("await db.clear_pending_action(jid)\n        return False", source)
        for state in ("menu_main", "menu_pos", "menu_business", "menu_staff"):
            self.assertIn(f'"{state}"', helper)

    def test_whatsapp_has_guided_product_edit(self):
        source = (ROOT / "bridge_app/main.py").read_text()
        self.assertIn('(\"product_edit\", \"✏️ Edit Produk\"', source)
        self.assertIn('"guided_pos_product_edit_pick"', source)
        self.assertIn('"guided_pos_product_edit_field"', source)
        self.assertIn('"guided_pos_product_edit_value"', source)
        self.assertIn('"pos_product_update"', source)
        self.assertIn("await pos.update_product(", source)

    def test_deploy_checks_the_exact_menu_regression(self):
        script = (ROOT / "scripts/feature_check.sh").read_text()
        self.assertIn("Routing MENU ke teks bebas sehat", script)
        self.assertIn("jual susu 1 cup cash", script)
        self.assertIn("catat pemasukan 100 ribu", script)


if __name__ == "__main__":
    unittest.main()
