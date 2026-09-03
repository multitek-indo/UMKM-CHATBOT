import sys
import types
import unittest


if "httpx" not in sys.modules:
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.RequestError = RuntimeError
    sys.modules["httpx"] = httpx_stub

from app.ai import _normalize  # noqa: E402


class AINormalizeTest(unittest.TestCase):
    def test_partial_sale_keeps_known_product_for_followup(self):
        result = _normalize(
            {
                "intent": "create_sale",
                "confidence": 0.9,
                "sale_items": [{"product_query": "susu", "qty": 1}],
                "payment_method": None,
                "needs_clarification": True,
            },
            {},
            "test-model",
        )
        self.assertTrue(result.needs_clarification)
        self.assertEqual(result.sale_items[0]["product_query"], "susu")
        self.assertIsNone(result.payment_method)

    def test_monthly_pos_period_is_preserved(self):
        result = _normalize(
            {
                "intent": "pos_summary",
                "confidence": 0.9,
                "report_period": "month",
            },
            {},
            "test-model",
        )
        self.assertEqual(result.report_period, "month")


if __name__ == "__main__":
    unittest.main()
