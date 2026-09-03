import unittest
from decimal import Decimal

from app.permissions import can_manage_role, has_permission
from app.pos_parser import (
    normalize_sku, parse_human_sale_items, parse_quick_sale_request,
    parse_nonnegative_money, parse_sale_items,
)


class PosParserTest(unittest.TestCase):
    def test_sku_normalization(self):
        self.assertEqual(normalize_sku(" kopi-01 "), "KOPI-01")

    def test_rejects_unsafe_sku(self):
        with self.assertRaises(ValueError):
            normalize_sku("kopi susu")

    def test_money_suffix(self):
        self.assertEqual(parse_nonnegative_money("1,5jt"), 1_500_000)

    def test_sale_items_combine_duplicates(self):
        self.assertEqual(
            parse_sale_items("kopi:2, KOPI:0.5"),
            [{"sku": "KOPI", "qty": Decimal("2.500")}],
        )

    def test_human_sale_name_after_quantity(self):
        self.assertEqual(
            parse_human_sale_items("2 Kopi Susu"),
            [{"product_query": "Kopi Susu", "qty": Decimal("2.000")}],
        )

    def test_human_sale_name_before_quantity_and_unit(self):
        self.assertEqual(
            parse_human_sale_items("Susu 1 cup"),
            [{"product_query": "Susu", "qty": Decimal("1.000")}],
        )

    def test_quick_sale_matches_reported_whatsapp_phrase(self):
        items, payment = parse_quick_sale_request(
            "penjualan pos susu 1 cup pembayaran cash"
        )
        self.assertEqual(
            items,
            [{"product_query": "susu", "qty": Decimal("1.000")}],
        )
        self.assertEqual(payment, "CASH")

    def test_quick_sale_accepts_pos_penjualan_prefix(self):
        items, payment = parse_quick_sale_request(
            "pos penjualan susu 1 cup cash"
        )
        self.assertEqual(
            items,
            [{"product_query": "susu", "qty": Decimal("1.000")}],
        )
        self.assertEqual(payment, "CASH")

    def test_quick_sale_keeps_missing_payment_as_followup(self):
        items, payment = parse_quick_sale_request("penjualan susu 1 cup")
        self.assertEqual(items[0]["product_query"], "susu")
        self.assertIsNone(payment)

    def test_human_sale_splits_indonesian_and_quantity_first(self):
        self.assertEqual(
            parse_human_sale_items("2 Kopi Susu dan 1 Roti Bakar"),
            [
                {"product_query": "Kopi Susu", "qty": Decimal("2.000")},
                {"product_query": "Roti Bakar", "qty": Decimal("1.000")},
            ],
        )

    def test_human_sale_splits_indonesian_and_quantity_last(self):
        self.assertEqual(
            parse_human_sale_items("Kopi Susu 2 dan Roti Bakar 1"),
            [
                {"product_query": "Kopi Susu", "qty": Decimal("2.000")},
                {"product_query": "Roti Bakar", "qty": Decimal("1.000")},
            ],
        )

    def test_human_sale_keeps_and_inside_one_product_name(self):
        self.assertEqual(
            parse_human_sale_items("Roti dan Kopi 1"),
            [{"product_query": "Roti dan Kopi", "qty": Decimal("1.000")}],
        )

    def test_quick_sale_with_and_and_payment(self):
        items, payment = parse_quick_sale_request(
            "jual 2 kopi susu dan 1 roti bakar bayar QRIS"
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(payment, "QRIS")

    def test_permissions(self):
        self.assertTrue(has_permission("CASHIER", "pos.sell"))
        self.assertFalse(has_permission("CASHIER", "finance.read"))
        self.assertTrue(can_manage_role("ADMIN", "CASHIER"))
        self.assertFalse(can_manage_role("ADMIN", "ADMIN"))
        self.assertFalse(can_manage_role("ADMIN", "OWNER"))


if __name__ == "__main__":
    unittest.main()
