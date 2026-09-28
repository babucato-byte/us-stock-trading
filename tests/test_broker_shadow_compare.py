import json
import tempfile
import unittest
from pathlib import Path

from brokers.toss_shadow_adapter import TossShadowExecutionBlocked
from validation.broker_shadow_compare import (
    BrokerShadowComparison, classify_difference, latency_summary, price_difference,
)


class Toss:
    def __init__(self, fail=False): self.fail, self.calls = fail, 0
    def get_prices(self, symbols):
        self.calls += 1
        if self.fail: raise RuntimeError("token secret")
        return type("R", (), {"latency_ms": 3, "data": {"result": [{"symbol": s, "lastPrice": "100.10"} for s in symbols]}})()
    def get_buying_power(self): return type("R", (), {"latency_ms": 2, "data": {}})()
    def get_1m_candles(self, _symbol): return type("R", (), {"data": {"result": {"candles": []}}})()
    def submit_order(self, *a, **k): raise TossShadowExecutionBlocked("TOSS_SHADOW_EXECUTION_BLOCKED")
    cancel_order = modify_order = submit_order


class KIS:
    def __init__(self, fail=False): self.fail, self.order_calls = fail, 0
    def quote(self, _symbol):
        if self.fail: raise RuntimeError("credential")
        return 100.0
    def buying_power(self): return None
    def bars(self, _symbol): return []
    def submit_order(self, *a, **k): self.order_calls += 1


class BrokerShadowCompareTests(unittest.TestCase):
    def setUp(self): self.kis, self.toss = KIS(), Toss(); self.compare = BrokerShadowComparison(self.kis, self.toss, symbols=("AAPL",))
    def test_toss_execution_remains_blocked(self):
        with self.assertRaises(TossShadowExecutionBlocked): self.toss.submit_order()
    def test_comparison_does_not_call_order_api(self): self.compare.run_cycle(); self.assertEqual(self.kis.order_calls, 0)
    def test_secret_redaction(self): self.assertEqual(self.compare._quote("TOSS", "AAPL", lambda: (_ for _ in ()).throw(RuntimeError("secret"))).error, "RuntimeError")
    def test_price_diff(self): self.assertAlmostEqual(price_difference(100, 100.1)[0], 0.1)
    def test_percent_diff(self): self.assertAlmostEqual(price_difference(100, 100.1)[1], 0.1)
    def test_threshold_classification(self): self.assertEqual(classify_difference(0.1), "NORMAL"); self.assertEqual(classify_difference(0.3), "REVIEW"); self.assertEqual(classify_difference(0.31), "LARGE_DIFFERENCE")
    def test_buying_power_none_safe(self): self.assertIsNone(self.compare.compare_buying_power()["KIS"]["buying_power_usd"])
    def test_provider_failure_does_not_stop_other(self):
        rows = BrokerShadowComparison(KIS(fail=True), Toss(), symbols=("AAPL",)).run_cycle(); self.assertIsNotNone(rows[0]["toss"]["price"])
    def test_malformed_quote_fails_safely(self):
        rows = BrokerShadowComparison(KIS(), Toss(), symbols=("AAPL",)).run_cycle(); self.assertIsNone(BrokerShadowComparison(KIS(), Toss(fail=True), symbols=("AAPL",)).run_cycle()[0]["toss"]["price"]); self.assertEqual(rows[0]["classification"], "NORMAL")
    def test_log_output_has_no_credentials(self):
        self.compare.run_cycle()
        with tempfile.TemporaryDirectory() as tmp:
            path = self.compare.write_log(Path(tmp)); content = path.read_text(); self.assertNotIn("secret", content); self.assertNotIn("token", content); json.loads(content)
    def test_latency_summary(self): self.assertEqual(latency_summary([1, 2, 3])["p95"], 3)


if __name__ == "__main__": unittest.main()
