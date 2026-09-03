import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]


class DummyFastAPI:
    def __init__(self, *args, **kwargs):
        pass

    def on_event(self, *args, **kwargs):
        return lambda function: function

    def get(self, *args, **kwargs):
        return lambda function: function

    def post(self, *args, **kwargs):
        return lambda function: function


class DummyHTTPException(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)


class FakeDatabase(types.ModuleType):
    def __init__(self):
        super().__init__("bridge_app.db")
        self.pending = None
        self.clears = 0

    async def ensure_whatsapp_user(self, *args, **kwargs):
        return {
            "access_status": "active",
            "deleted_at": None,
            "active_tenant_id": "tenant-a",
        }

    async def get_pending_action(self, jid):
        return self.pending

    async def clear_pending_action(self, jid):
        self.pending = None
        self.clears += 1

    async def set_pending_action(self, jid, action_type, payload, **kwargs):
        self.pending = {"action_type": action_type, "payload": payload}

    async def get_whatsapp_access(self, jid):
        return {
            "role": "OWNER",
            "business_name": "Warung Uji",
            "phone_number": "6281",
        }


def load_main_module():
    os.environ.update({
        "GOWA_BASIC_AUTH_USER": "test",
        "GOWA_BASIC_AUTH_PASSWORD": "test",
        "GOWA_WEBHOOK_SECRET": "test",
        "BOT_TOKEN": "test",
        "MASTER_TELEGRAM_ID": "1",
    })

    fake_fastapi = types.ModuleType("fastapi")
    fake_fastapi.FastAPI = DummyFastAPI
    fake_fastapi.HTTPException = DummyHTTPException
    fake_fastapi.Request = type("Request", (), {})
    sys.modules["fastapi"] = fake_fastapi

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.BasicAuth = object
    fake_httpx.AsyncClient = object
    sys.modules["httpx"] = fake_httpx

    database = FakeDatabase()
    sys.modules["bridge_app.db"] = database
    for name in ("ai", "exporter", "pos"):
        module = types.ModuleType(f"bridge_app.{name}")
        sys.modules[f"bridge_app.{name}"] = module
        setattr(sys.modules["bridge_app"], name, module)
    setattr(sys.modules["bridge_app"], "db", database)

    spec = importlib.util.spec_from_file_location(
        "bridge_app._flow_test_main", ROOT / "bridge_app/main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.gowa_send_text = AsyncMock()
    return module, database


class WhatsAppFlowTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main, cls.database = load_main_module()

    def setUp(self):
        self.database.pending = {
            "action_type": "menu_main",
            "payload": {"actions": ["income", "pos"]},
        }
        self.database.clears = 0
        self.main.gowa_send_text.reset_mock()

    async def test_menu_then_sale_sentence_reaches_ai_router(self):
        self.main.handle_ai = AsyncMock()
        await self.main.process_message("user@s.whatsapp.net", {"body": "jual susu 1 cup cash"})
        self.main.handle_ai.assert_awaited_once_with(
            "user@s.whatsapp.net", "jual susu 1 cup cash"
        )
        self.assertGreaterEqual(self.database.clears, 1)

    async def test_menu_then_finance_sentence_reaches_ai_router(self):
        self.main.handle_ai = AsyncMock()
        await self.main.process_message(
            "user@s.whatsapp.net", {"body": "catat pemasukan 100 ribu"}
        )
        self.main.handle_ai.assert_awaited_once_with(
            "user@s.whatsapp.net", "catat pemasukan 100 ribu"
        )

    async def test_menu_then_edit_product_starts_wizard(self):
        self.main.begin_product_edit = AsyncMock()
        await self.main.process_message("user@s.whatsapp.net", {"body": "edit produk"})
        self.main.begin_product_edit.assert_awaited_once_with(
            "user@s.whatsapp.net", None
        )

    async def test_incomplete_pos_penjualan_asks_for_quantity(self):
        await self.main.process_message(
            "user@s.whatsapp.net", {"body": "pos penjualan susu"}
        )
        self.assertEqual(self.database.pending["action_type"], "sale_items_needed")
        sent = self.main.gowa_send_text.await_args.args[1]
        self.assertIn("produk dan jumlah", sent)

    async def test_zero_can_clear_product_cost_in_edit_wizard(self):
        self.database.pending = {
            "action_type": "guided_pos_product_edit_value",
            "payload": {
                "sku": "SUSU",
                "name": "Susu",
                "sell_price": 10000,
                "cost_price": 5000,
                "unit": "cup",
                "min_stock": "5",
                "edit_field": "cost_price",
            },
        }
        handled = await self.main.handle_menu_state("user@s.whatsapp.net", "0")
        self.assertTrue(handled)
        self.assertEqual(self.database.pending["action_type"], "pos_product_update")
        self.assertEqual(self.database.pending["payload"]["cost_price"], 0)


if __name__ == "__main__":
    unittest.main()
