import unittest

from bridge_app.menu_ux import (
    numbered_choice,
    parse_product_edit_request,
    selected_menu_action,
    should_yield_to_free_text,
)


class MenuRoutingTest(unittest.TestCase):
    def test_main_menu_releases_reported_sale_sentence(self):
        self.assertTrue(
            should_yield_to_free_text("menu_main", "jual susu 1 cup cash")
        )

    def test_main_menu_releases_reported_pos_sentence(self):
        self.assertTrue(
            should_yield_to_free_text("menu_main", "pos penjualan susu")
        )

    def test_main_menu_releases_reported_finance_sentence(self):
        self.assertTrue(
            should_yield_to_free_text("menu_main", "catat pemasukan 100 ribu")
        )

    def test_main_menu_releases_reported_product_edit_sentence(self):
        self.assertTrue(
            should_yield_to_free_text("menu_main", "edit produk")
        )

    def test_pos_menu_releases_free_text_too(self):
        self.assertTrue(
            should_yield_to_free_text("menu_pos", "jual susu 1 cup tunai")
        )

    def test_numeric_choice_stays_in_menu(self):
        self.assertFalse(should_yield_to_free_text("menu_main", "9"))
        self.assertFalse(should_yield_to_free_text("menu_main", "9️⃣"))
        self.assertEqual(numbered_choice("9️⃣"), "9")

    def test_guided_state_remains_strict(self):
        self.assertFalse(
            should_yield_to_free_text("guided_pos_product_quick_name", "Susu")
        )

    def test_visible_action_mapping_uses_payload_order(self):
        payload = {"actions": ["sale", "products", "product_add"]}
        self.assertEqual(selected_menu_action(payload, "2"), "products")
        self.assertIsNone(selected_menu_action(payload, "4"))

    def test_product_edit_phrase_is_deterministic(self):
        self.assertEqual(parse_product_edit_request("edit produk"), (True, None))
        self.assertEqual(
            parse_product_edit_request("ubah produk susu"), (True, "susu")
        )
        self.assertEqual(
            parse_product_edit_request("POS edit produk KOPI"), (True, "KOPI")
        )
        self.assertEqual(parse_product_edit_request("produk susu"), (False, None))


if __name__ == "__main__":
    unittest.main()
