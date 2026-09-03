import unittest

from app.product_match import choose_product_match, normalize_product_query


def product(sku, name, barcode=None):
    return {"sku": sku, "name": name, "barcode": barcode}


class ProductMatchTest(unittest.TestCase):
    def test_normalizes_human_text(self):
        self.assertEqual(normalize_product_query("  Kopi-Susu! "), "kopi susu")

    def test_exact_name_resolves_without_sku(self):
        selected, candidates = choose_product_match(
            "susu",
            [product("MINUM-01", "Susu"), product("KOPI", "Kopi Hitam")],
        )
        self.assertEqual(selected["sku"], "MINUM-01")
        self.assertEqual(candidates, [])

    def test_typo_resolves_when_unique(self):
        selected, _ = choose_product_match(
            "susuu",
            [product("SUSU", "Susu"), product("ROTI", "Roti Bakar")],
        )
        self.assertEqual(selected["sku"], "SUSU")

    def test_ambiguous_name_requires_user_choice(self):
        selected, candidates = choose_product_match(
            "susu",
            [product("SUSU-UHT", "Susu UHT"), product("SUSU-SEGAR", "Susu Segar")],
        )
        self.assertIsNone(selected)
        self.assertEqual(len(candidates), 2)


if __name__ == "__main__":
    unittest.main()
