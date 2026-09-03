import sys
import types
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal

import app


fake_db = types.ModuleType("app.db")
sys.modules["app.db"] = fake_db
app.db = fake_db

from app import pos  # noqa: E402
from app.pos_parser import parse_quick_sale_request  # noqa: E402


PRODUCTS = [
    {
        "id": "p1", "sku": "MINUM-01", "barcode": None, "name": "Susu",
        "unit": "cup", "sell_price": Decimal("10000"), "cost_price": Decimal("5000"),
        "stock_on_hand": Decimal("20"), "min_stock": Decimal("5"), "is_active": True,
    },
    {
        "id": "p2", "sku": "ROTI", "barcode": None, "name": "Roti Bakar",
        "unit": "pcs", "sell_price": Decimal("15000"), "cost_price": Decimal("7000"),
        "stock_on_hand": Decimal("10"), "min_stock": Decimal("2"), "is_active": True,
    },
]


class FakeConn:
    async def fetch(self, query, tenant_id):
        if "WHERE tenant_id=$1" not in query:
            raise AssertionError("product query is not tenant-bound")
        if tenant_id != "tenant-a":
            raise AssertionError("wrong tenant context")
        return PRODUCTS


@asynccontextmanager
async def tenant_tx(identity):
    if identity != "user-a":
        raise AssertionError("wrong identity")
    yield FakeConn(), {
        "tenant_id": "tenant-a", "user_id": "user-a", "role": "CASHIER",
        "business_name": "Warung Uji",
    }


fake_db.tenant_tx = tenant_tx


class SalePreviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_reported_whatsapp_sentence_reaches_safe_preview(self):
        items, payment = parse_quick_sale_request(
            "penjualan pos susu 1 cup pembayaran cash"
        )
        _, preview = await pos.prepare_sale_preview("user-a", items, payment)
        self.assertEqual(preview["items"][0]["sku"], "MINUM-01")
        self.assertEqual(preview["total"], 10000)
        self.assertEqual(preview["payment_method"], "CASH")

    async def test_pos_penjualan_sentence_reaches_same_safe_preview(self):
        items, payment = parse_quick_sale_request(
            "pos penjualan susu 1 cup cash"
        )
        _, preview = await pos.prepare_sale_preview("user-a", items, payment)
        self.assertEqual(preview["items"][0]["sku"], "MINUM-01")
        self.assertEqual(preview["total"], 10000)
        self.assertEqual(preview["payment_method"], "CASH")

    async def test_name_resolves_and_total_is_shown_before_write(self):
        ctx, preview = await pos.prepare_sale_preview(
            "user-a", [{"product_query": "susu", "qty": "1"}], "cash"
        )
        self.assertEqual(ctx["tenant_id"], "tenant-a")
        self.assertEqual(preview["items"][0]["sku"], "MINUM-01")
        self.assertEqual(preview["items"][0]["name"], "Susu")
        self.assertEqual(preview["items"][0]["stock_after"], "19.000")
        self.assertEqual(preview["total"], 10000)
        self.assertEqual(preview["payment_method"], "CASH")

    async def test_payment_can_remain_pending_without_losing_items(self):
        _, preview = await pos.prepare_sale_preview(
            "user-a", [{"product_query": "susu", "qty": "1"}], None
        )
        self.assertEqual(preview["items"][0]["name"], "Susu")
        self.assertIsNone(preview["payment_method"])


if __name__ == "__main__":
    unittest.main()
