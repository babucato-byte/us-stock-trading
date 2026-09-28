import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from brokers.toss_shadow_adapter import TossShadowAdapter, TossShadowExecutionBlocked
from validation.toss_openapi import client as toss_client
from validation.broker_shadow_compare import (
    KIS_ENVIRONMENT_NOT_LOADED, TOSS_CANDLE_SCHEMA_UNVERIFIED, TOSS_QUOTE_SCHEMA_UNVERIFIED,
    BrokerShadowComparison, _KISReadOnlyFacade, candle_schema_verified, classify_difference,
    compare_completed_bars, final_verdict, format_kis_environment, format_schema_probe,
    latency_summary, missing_kis_environment, normalize_toss_buying_power, parse_toss_candles,
    parse_toss_quote_rows, price_difference, provider_error_summary, quote_schema_verified,
    schema_probe,
)


NOW = datetime(2026, 9, 28, 16, 20, 30, tzinfo=timezone.utc)
KST = timezone(timedelta(hours=9))


def toss_candle(minute_utc, close="100.00", tz=KST):
    stamp = minute_utc.astimezone(tz).isoformat(timespec="milliseconds")
    return {"timestamp": stamp, "openPrice": close, "highPrice": close, "lowPrice": close, "closePrice": close, "volume": "10", "currency": "USD"}


def kis_bar(minute_utc, close=100.0):
    at = minute_utc.astimezone(ZoneInfo("America/New_York"))  # kis_minute_chart returns Eastern-aware
    return {"at": at, "open": close, "high": close, "low": close, "close": close, "volume": 10.0}


class Toss:
    def __init__(self, fail=False, candles=None): self.fail, self.calls, self.candles, self.authenticated = fail, 0, candles or [], 0
    def authenticate(self): self.authenticated += 1
    def get_prices(self, symbols):
        self.calls += 1
        if self.fail: raise RuntimeError("token secret")
        return type("R", (), {"latency_ms": 3, "data": {"result": [{"symbol": s, "lastPrice": "100.10", "currency": "USD", "timestamp": "t"} for s in symbols]}})()
    def get_buying_power(self): return type("R", (), {"latency_ms": 2, "data": {}})()
    def get_1m_candles(self, _symbol): return type("R", (), {"data": {"result": {"candles": self.candles, "nextBefore": "x"}}})()
    def submit_order(self, *a, **k): raise TossShadowExecutionBlocked("TOSS_SHADOW_EXECUTION_BLOCKED")
    cancel_order = modify_order = submit_order


class KIS:
    def __init__(self, fail=False, bars=None): self.fail, self.order_calls, self._bars = fail, 0, bars or []
    def quote(self, _symbol):
        if self.fail: raise RuntimeError("credential")
        return 100.0
    def buying_power(self): return None
    def bars(self, _symbol): return self._bars
    def submit_order(self, *a, **k): self.order_calls += 1


class _Response:
    def __init__(self, body): self._body, self.status = json.dumps(body).encode(), 200
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _fake_toss_server(requests):
    """Records (method, path) of every request that would have left the process."""
    def urlopen(request, timeout=None):
        path = request.full_url.split("openapi.tossinvest.com", 1)[1].split("?", 1)[0]
        requests.append((request.get_method(), path))
        bodies = {
            "/oauth2/token": {"access_token": "tok"},
            "/api/v1/accounts": {"result": [{"accountSeq": 1, "accountNo": "12345678", "accountType": "X"}]},
            "/api/v1/prices": {"result": [{"symbol": "AAPL", "lastPrice": "100.00", "currency": "USD", "timestamp": "t"}]},
            "/api/v1/candles": {"result": {"candles": [toss_candle(NOW - timedelta(minutes=1))], "nextBefore": "x"}},
            "/api/v1/buying-power": {"result": {"cashBuyingPower": "250.50", "currency": "USD"}},
        }
        return _Response(bodies[path])
    return urlopen


class BrokerShadowCompareTests(unittest.TestCase):
    def setUp(self): self.kis, self.toss = KIS(), Toss(); self.compare = BrokerShadowComparison(self.kis, self.toss, symbols=("AAPL",))

    # --- existing behaviour -------------------------------------------------
    def test_toss_execution_remains_blocked(self):
        with self.assertRaises(TossShadowExecutionBlocked): self.toss.submit_order()
    def test_comparison_does_not_call_order_api(self): self.compare.run_cycle(); self.assertEqual(self.kis.order_calls, 0)
    def test_secret_redaction(self): self.assertEqual(self.compare._quote("TOSS", "AAPL", lambda: (_ for _ in ()).throw(RuntimeError("secret"))).error, "RuntimeError")
    def test_price_diff(self): self.assertAlmostEqual(price_difference(100, 100.1)[0], 0.1)
    def test_percent_diff(self): self.assertAlmostEqual(price_difference(100, 100.1)[1], 0.1)
    def test_threshold_classification(self): self.assertEqual(classify_difference(0.1), "NORMAL"); self.assertEqual(classify_difference(0.3), "REVIEW"); self.assertEqual(classify_difference(0.31), "LARGE_DIFFERENCE"); self.assertEqual(classify_difference(None), "NOT_COMPARABLE")
    def test_buying_power_none_safe(self): self.assertIsNone(self.compare.compare_buying_power()["KIS"]["buying_power_usd"])
    def test_provider_failure_does_not_stop_other(self):
        rows = BrokerShadowComparison(KIS(fail=True), Toss(), symbols=("AAPL",)).run_cycle()
        self.assertIsNotNone(rows[0]["toss"]["price"]); self.assertEqual(rows[0]["kis"]["error"], "RuntimeError"); self.assertEqual(rows[0]["classification"], "NOT_COMPARABLE")
        rows = BrokerShadowComparison(KIS(), Toss(fail=True), symbols=("AAPL",)).run_cycle()
        self.assertEqual(rows[0]["kis"]["price"], 100.0); self.assertIsNone(rows[0]["toss"]["price"])
    def test_malformed_quote_fails_safely(self):
        rows = self.compare.run_cycle(); self.assertEqual(rows[0]["classification"], "NORMAL")
        self.assertIsNone(BrokerShadowComparison(KIS(), Toss(fail=True), symbols=("AAPL",)).run_cycle()[0]["toss"]["price"])
    def test_log_output_has_no_credentials(self):
        BrokerShadowComparison(KIS(fail=True), Toss(fail=True), symbols=("AAPL",)).run_cycle()
        self.compare.run_cycle()
        with tempfile.TemporaryDirectory() as tmp:
            content = self.compare.write_log(Path(tmp)).read_text(); self.assertNotIn("secret", content); self.assertNotIn("token", content); json.loads(content)
    def test_latency_summary(self): self.assertEqual(latency_summary([1, 2, 3])["p95"], 3); self.assertIsNone(latency_summary([])["median"])

    # 1. nested provider error summary
    def test_nested_provider_error_summary(self):
        records = [{"kis": {"error": "KIS_ENVIRONMENT_NOT_LOADED"}, "toss": {"error": "ValidationError"}}, {"kis": {"error": "KIS_ENVIRONMENT_NOT_LOADED"}, "toss": {"error": None}}]
        summary = provider_error_summary(records)
        self.assertEqual(summary["KIS"], {"KIS_ENVIRONMENT_NOT_LOADED": 2}); self.assertEqual(summary["TOSS"], {"ValidationError": 1})
    def test_error_summary_from_real_cycle_is_type_name_only(self):
        compare = BrokerShadowComparison(KIS(fail=True), Toss(fail=True), symbols=("AAPL", "NVDA"))
        compare.run_cycle()
        self.assertEqual(provider_error_summary(compare.records), {"KIS": {"RuntimeError": 2}, "TOSS": {"RuntimeError": 2}})

    # 2/3/4/5. schema probes print keys, never values or credentials
    def test_quote_schema_probe_is_key_only(self):
        data = {"result": [{"symbol": "AAPL", "lastPrice": "987.65", "currency": "USD", "timestamp": "2026-09-28T16:20:00.000+00:00"}]}
        probe = schema_probe(data)
        self.assertEqual(probe, {"top_level_keys": ["result"], "payload_type": "list", "row_keys": ["currency", "lastPrice", "symbol", "timestamp"]})
        rendered = format_schema_probe("TOSS_QUOTE_SCHEMA", probe, verified=quote_schema_verified(probe))
        for value in ("AAPL", "987.65", "USD", "2026"): self.assertNotIn(value, rendered)
        self.assertIn("verified: YES", rendered)
    def test_candle_schema_probe_is_key_only(self):
        data = {"result": {"candles": [toss_candle(NOW, close="555.55")], "nextBefore": "CURSOR_VALUE"}}
        probe = schema_probe(data, candle=True)
        self.assertEqual(probe["payload_keys"], ["candles", "nextBefore"])
        self.assertEqual(probe["row_keys"], ["closePrice", "currency", "highPrice", "lowPrice", "openPrice", "timestamp", "volume"])
        rendered = format_schema_probe("TOSS_CANDLE_SCHEMA", probe, verified=candle_schema_verified(probe))
        for value in ("555.55", "CURSOR_VALUE", "2026", "USD"): self.assertNotIn(value, rendered)
        self.assertIn("verified: YES", rendered)
    def test_schema_probe_never_prints_credentials_token_or_account(self):
        data = {"access_token": "TOKEN_VALUE", "result": [{"accountNo": "12345678", "client_secret": "SECRET_VALUE", "Authorization": "Bearer XYZ", "12345678": "by-account"}]}
        rendered = format_schema_probe("TOSS_QUOTE_SCHEMA", schema_probe(data))
        for value in ("TOKEN_VALUE", "12345678", "SECRET_VALUE", "Bearer", "XYZ", "by-account"): self.assertNotIn(value, rendered)
        self.assertIn("<NON_IDENTIFIER_KEY>", rendered)
    def test_schema_probe_error_is_type_name_only(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            from validation.broker_shadow_compare import _probe
            _, verified = _probe("TOSS_QUOTE_SCHEMA", lambda: (_ for _ in ()).throw(RuntimeError("Bearer TOKEN_VALUE")), candle=False)
        self.assertFalse(verified); self.assertIn("RuntimeError", buffer.getvalue()); self.assertNotIn("TOKEN_VALUE", buffer.getvalue())

    # 6. verified quote mapping only
    def test_toss_quote_parser_uses_verified_keys(self):
        self.assertEqual(parse_toss_quote_rows({"result": [{"symbol": "aapl", "lastPrice": "100.10"}]}), {"AAPL": 100.10})
    def test_toss_quote_parser_rejects_guessed_aliases(self):
        for row in ({"symbol": "AAPL", "price": "1"}, {"code": "AAPL", "lastPrice": "1"}, {"symbol": "AAPL", "lastPrice": "nan"}):
            with self.assertRaises(TOSS_QUOTE_SCHEMA_UNVERIFIED): parse_toss_quote_rows({"result": [row]})
        with self.assertRaises(TOSS_QUOTE_SCHEMA_UNVERIFIED): parse_toss_quote_rows({"result": []})
        self.assertFalse(quote_schema_verified(schema_probe({"result": [{"symbol": "AAPL", "price": "1"}]})))
    def test_symbol_absent_from_batch_is_not_comparable(self):
        toss = Toss(); toss.get_prices = lambda s: type("R", (), {"latency_ms": 3, "data": {"result": [{"symbol": "AAPL", "lastPrice": "100"}]}})()
        rows = BrokerShadowComparison(KIS(), toss, symbols=("AAPL", "NVDA")).run_cycle()
        self.assertEqual(rows[1]["toss"]["error"], "TOSS_QUOTE_SYMBOL_MISSING"); self.assertEqual(rows[1]["classification"], "NOT_COMPARABLE")

    # 7. verified candle mapping only
    def test_toss_candle_parser_uses_verified_keys(self):
        bars = parse_toss_candles({"result": {"candles": [toss_candle(NOW, close="2")]}})
        self.assertEqual(set(bars[0]), {"at", "open", "high", "low", "close", "volume"}); self.assertEqual(bars[0]["close"], "2")
    def test_toss_candle_parser_rejects_guessed_aliases(self):
        with self.assertRaises(TOSS_CANDLE_SCHEMA_UNVERIFIED): parse_toss_candles({"result": {"candles": [{"timestamp": "t", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}]}})
        with self.assertRaises(TOSS_CANDLE_SCHEMA_UNVERIFIED): parse_toss_candles({"result": {"bars": [toss_candle(NOW)]}})
        with self.assertRaises(TOSS_CANDLE_SCHEMA_UNVERIFIED): parse_toss_candles({"result": {"candles": []}})
    def test_bar_comparison_skips_forming_bar_and_aligns_minutes(self):
        last, forming = NOW.replace(second=0) - timedelta(minutes=1), NOW.replace(second=0)
        # The forming minute disagrees wildly and must be ignored; the completed one matches.
        result = compare_completed_bars([kis_bar(last), kis_bar(forming, 50.0)], parse_toss_candles({"result": {"candles": [toss_candle(last), toss_candle(forming, "999")]}}), NOW)
        self.assertEqual(result["classification"], "MATCH"); self.assertEqual(result["minute_utc"], last.isoformat())
    def test_bar_comparison_uses_common_minute_when_one_side_lags(self):
        m1, m2 = NOW.replace(second=0) - timedelta(minutes=2), NOW.replace(second=0) - timedelta(minutes=1)
        result = compare_completed_bars([kis_bar(m1)], parse_toss_candles({"result": {"candles": [toss_candle(m1), toss_candle(m2, "150")]}}), NOW)
        self.assertEqual(result["minute_utc"], m1.isoformat()); self.assertEqual(result["classification"], "MATCH")
    def test_bar_classification_thresholds(self):
        m = NOW.replace(second=0) - timedelta(minutes=1)
        toss = lambda close: parse_toss_candles({"result": {"candles": [toss_candle(m, close)]}})
        self.assertEqual(compare_completed_bars([kis_bar(m)], toss("100.20"), NOW)["classification"], "MINOR_DIFF")
        self.assertEqual(compare_completed_bars([kis_bar(m)], toss("101.00"), NOW)["classification"], "MAJOR_DIFF")
        self.assertEqual(compare_completed_bars([], toss("100"), NOW)["reason"], "KIS_NO_COMPLETED_BAR")
    def test_compare_bars_records_provider_errors_without_stopping(self):
        m = NOW.replace(second=0) - timedelta(minutes=1)
        result = BrokerShadowComparison(KIS(bars=[kis_bar(m)]), Toss(candles=[toss_candle(m)]), symbols=("AAPL",)).compare_bars(NOW)
        self.assertEqual(result[0]["classification"], "MATCH")
        result = BrokerShadowComparison(KIS(fail=True, bars=[kis_bar(m)]), Toss(candles=[{"bad": 1}]), symbols=("AAPL",)).compare_bars(NOW)
        self.assertEqual(result[0]["toss_error"], "TOSS_CANDLE_SCHEMA_UNVERIFIED"); self.assertEqual(result[0]["classification"], "NOT_COMPARABLE")

    # buying power: verified keys only
    def test_toss_buying_power_normalized_from_verified_keys(self):
        self.assertEqual(normalize_toss_buying_power({"result": {"cashBuyingPower": "250.50", "currency": "USD"}}), {"currency": "USD", "buying_power_usd": 250.5, "raw_available": True})
        self.assertEqual(normalize_toss_buying_power({"result": {"cashBuyingPower": "1000", "currency": "KRW"}})["buying_power_usd"], "UNKNOWN")
        self.assertEqual(normalize_toss_buying_power({"result": {"amount": "1"}}), {"currency": "UNKNOWN", "buying_power_usd": "UNKNOWN", "raw_available": True})

    # 8. missing KIS env
    def test_missing_kis_environment_classified_explicitly(self):
        self.assertEqual(missing_kis_environment({}), ("KIS_ENV", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRODUCT_CD", "KIS_ACCOUNT_READ_ENABLED"))
        self.assertIn("KIS_ENVIRONMENT_NOT_LOADED", format_kis_environment({}))
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(KIS_ENVIRONMENT_NOT_LOADED): _KISReadOnlyFacade()
    def test_kis_environment_prints_presence_never_values(self):
        env = {name: "VALUE_%s_XYZ" % i for i, name in enumerate(("KIS_ENV", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRODUCT_CD", "KIS_ACCOUNT_READ_ENABLED"))}
        rendered = format_kis_environment(env)
        self.assertEqual(rendered.count("PRESENT"), 6); self.assertNotIn("VALUE_", rendered); self.assertNotIn("NOT_LOADED", rendered)
        self.assertIn("KIS_APP_SECRET: MISSING", format_kis_environment({**env, "KIS_APP_SECRET": " "}))

    # 10. success-only latency
    def test_latency_counts_successes_only(self):
        compare = BrokerShadowComparison(KIS(fail=True), Toss(), symbols=("AAPL", "NVDA"))
        compare.run_cycle(); compare.run_cycle()
        self.assertEqual(compare.latency["KIS"], {"success": [], "failed": 4})
        self.assertEqual(compare.latency["TOSS"], {"success": [3, 3], "failed": 0})  # one sample per batch request, not per symbol
        self.assertIsNone(compare.records[0]["kis"]["latency_ms"])
        compare = BrokerShadowComparison(KIS(), Toss(fail=True), symbols=("AAPL",)); compare.run_cycle()
        self.assertEqual(compare.latency["TOSS"]["failed"], 1); self.assertEqual(len(compare.latency["KIS"]["success"]), 1)
        self.assertIsNone(compare.records[0]["toss"]["latency_ms"])
    def test_rate_limit_counted_without_printing_message(self):
        toss = Toss(); toss.get_prices = lambda s: (_ for _ in ()).throw(toss_client.ValidationError("HTTP_ERROR status=429"))
        compare = BrokerShadowComparison(KIS(), toss, symbols=("AAPL",)); compare.run_cycle()
        self.assertEqual(compare.rate_limited["TOSS"], 1); self.assertEqual(compare.records[0]["toss"]["error"], "ValidationError")

    # FINAL is computed, never asserted
    def test_final_verdict_requires_every_criterion(self):
        m = NOW.replace(second=0) - timedelta(minutes=1)
        compare = BrokerShadowComparison(KIS(bars=[kis_bar(m)]), Toss(candles=[toss_candle(m)]), symbols=("AAPL",)); compare.run_cycle()
        bars = compare.compare_bars(NOW)
        self.assertEqual(final_verdict(kis_env_loaded=True, schema_verified=True, comparison=compare, bars=bars), ("TOSS_SHADOW_COMPARISON_PASS", []))
        verdict, reasons = final_verdict(kis_env_loaded=False, schema_verified=True, comparison=compare, bars=bars)
        self.assertEqual(verdict, "TOSS_SHADOW_COMPARISON_INCOMPLETE"); self.assertIn("KIS_ENVIRONMENT_NOT_LOADED", reasons)
        failed = BrokerShadowComparison(KIS(fail=True), Toss(), symbols=("AAPL",)); failed.run_cycle()
        _, reasons = final_verdict(kis_env_loaded=True, schema_verified=True, comparison=failed, bars=failed.compare_bars(NOW))
        self.assertIn("KIS_NO_SUCCESSFUL_QUOTE", reasons); self.assertIn("AAPL_PRICE_NOT_COMPARABLE", reasons); self.assertIn("AAPL_BAR_NOT_COMPARABLE", reasons)

    # 11/12/13. execution blocked on the real adapter; OCO/OTO do not exist at all
    def test_real_adapter_blocks_every_mutation(self):
        adapter = TossShadowAdapter(client=mock.Mock(spec=toss_client.TossReadOnlyClient))
        for method in (adapter.submit_order, adapter.cancel_order, adapter.modify_order):
            with self.assertRaises(TossShadowExecutionBlocked): method("AAPL", side="BUY", quantity=1)
        for name in ("place_order", "submit_oco_order", "submit_oto_order", "oco", "oto", "sell"):
            self.assertFalse(hasattr(adapter, name))
        for name in ("submit_order", "cancel_order", "modify_order", "place_order", "sell"):
            self.assertFalse(hasattr(_KISReadOnlyFacade, name))

    # 14. no mutating network call; and prices only after authentication
    def test_full_run_sends_only_allowed_reads_and_authenticates_first(self):
        requests = []
        with mock.patch.object(toss_client, "urlopen", _fake_toss_server(requests)):
            adapter = TossShadowAdapter(client=toss_client.TossReadOnlyClient("id", "secret"))
            compare = BrokerShadowComparison(KIS(), adapter, symbols=("AAPL",))
            compare.authenticate(); compare.run_cycle(); compare.compare_bars(NOW); power = compare.compare_buying_power()
            for method in (adapter.submit_order, adapter.cancel_order, adapter.modify_order):
                with self.assertRaises(TossShadowExecutionBlocked): method()
        self.assertEqual(requests[0], ("POST", "/oauth2/token"))
        self.assertTrue(set(requests) <= toss_client.ALLOWED_REQUESTS)
        self.assertEqual([r for r in requests if r[0] != "GET"], [("POST", "/oauth2/token")])
        self.assertEqual(compare.records[0]["classification"], "NORMAL"); self.assertIsNone(compare.authentication_error)
        self.assertEqual(power["TOSS"]["buying_power_usd"], 250.5)
    def test_unauthenticated_price_read_is_the_original_failure(self):
        requests = []
        with mock.patch.object(toss_client, "urlopen", _fake_toss_server(requests)):
            compare = BrokerShadowComparison(KIS(), TossShadowAdapter(client=toss_client.TossReadOnlyClient("id", "secret")), symbols=("AAPL",))
            rows = compare.run_cycle()  # no authenticate(): reproduces the 60/60 Oracle errors
        self.assertEqual(rows[0]["toss"]["error"], "ValidationError"); self.assertEqual(requests, [])


if __name__ == "__main__": unittest.main()
