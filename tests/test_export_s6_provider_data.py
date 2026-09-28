"""Safety and schema tests for the Oracle-only offline export step."""

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from validation.export_s6_provider_data import (
    S6ProviderDataExporter,
    _KISReadOnlyFacade,
    _normalize_completed_bars,
    secrets_in_export,
)


NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)


def _bars():
    return [{"at": NOW - timedelta(minutes=2), "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 12},
            {"at": NOW, "open": 11, "high": 12, "low": 10, "close": 11.5, "volume": 99}]


class KIS:
    def __init__(self): self.calls = []
    def bars(self, symbol): self.calls.append(symbol); return _bars()


class Toss:
    def __init__(self): self.auth_calls = 0; self.calls = []
    def authenticate(self): self.auth_calls += 1
    def candles_1m(self, symbol, count):
        self.calls.append((symbol, count))
        rows = [{"timestamp": item["at"].isoformat(), "openPrice": item["open"], "highPrice": item["high"],
                 "lowPrice": item["low"], "closePrice": item["close"], "volume": item["volume"], "currency": "USD"} for item in _bars()]
        return type("Result", (), {"data": {"result": {"candles": rows}}})()


def test_export_has_only_whitelisted_completed_market_data(tmp_path):
    exporter = S6ProviderDataExporter(KIS(), Toss(), candle_count=120)
    path = exporter.write(tmp_path, symbols=["AAPL"], now=NOW)
    payload = json.loads(path.read_text())
    assert payload["metadata"]["execution_calls"] == 0
    assert payload["metadata"]["toss_volume_source"] == "REST_CANDLE"
    assert len(payload["bars"]["AAPL"]["KIS"]) == 1
    assert len(payload["bars"]["AAPL"]["TOSS"]) == 1
    assert set(payload["bars"]["AAPL"]["KIS"][0]) == {"timestamp", "open", "high", "low", "close", "volume"}
    assert not secrets_in_export(payload)


def test_toss_failure_does_not_prevent_kis_export():
    class BrokenToss(Toss):
        def authenticate(self): raise RuntimeError("token=never-exported")
    payload = S6ProviderDataExporter(KIS(), BrokenToss()).collect(["AAPL"], now=NOW)
    assert payload["bars"]["AAPL"]["KIS"]
    assert payload["bars"]["AAPL"]["TOSS"] == []
    assert payload["errors"]["TOSS"] == {"RuntimeError": 1}
    assert "never-exported" not in json.dumps(payload)


def test_forbidden_structural_terms_fail_the_export_check():
    assert secrets_in_export({"token": "redacted"})
    assert secrets_in_export({"metadata": {"secret": "redacted"}})
    assert not secrets_in_export({"bars": {"AAPL": {"KIS": []}}})


def test_exporter_has_no_order_methods():
    assert not any(name in S6ProviderDataExporter.__dict__ for name in ("submit_order", "cancel_order", "modify_order"))


def test_exporter_has_no_parity_shadow_pandas_or_scanner_imports():
    source = Path("validation/export_s6_provider_data.py").read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = ("validation.s6_provider_parity", "validation.broker_shadow_compare", "brokers.toss_shadow_adapter", "pandas")
    assert not any(name in imported or name.startswith("scanners.") for name in forbidden)
    assert not any(name.startswith("scanners.") for name in imported)


def test_kis_facade_exposes_minute_bars_only():
    assert set(_KISReadOnlyFacade.__dict__) >= {"__init__", "bars"}
    assert not any(name in _KISReadOnlyFacade.__dict__ for name in ("quote", "buying_power", "account", "submit_order", "cancel_order", "modify_order"))


def test_toss_only_authenticates_once_and_reads_candles():
    kis, toss = KIS(), Toss()
    S6ProviderDataExporter(kis, toss, candle_count=120).collect(["AAPL", "NVDA"], now=NOW)
    assert toss.auth_calls == 1
    assert toss.calls == [("AAPL", 120), ("NVDA", 120)]


def test_429_is_counted_without_serializing_error_message():
    class RateLimitedToss(Toss):
        def candles_1m(self, symbol, count):
            exc = RuntimeError("secret response status=429")
            exc.status = 429
            raise exc
    payload = S6ProviderDataExporter(KIS(), RateLimitedToss()).collect(["AAPL"], now=NOW)
    assert payload["rate_limited_429"] == 1
    assert payload["errors"]["TOSS"] == {"RuntimeError": 1}
    assert "secret" not in json.dumps(payload)


def test_timestamp_is_normalized_to_utc_minute():
    plus_nine = timezone(timedelta(hours=9))
    rows = [{"timestamp": (NOW - timedelta(minutes=1)).astimezone(plus_nine), "open": 1, "high": 2, "low": 1,
             "close": 2, "volume": 3}]
    assert _normalize_completed_bars(rows, now=NOW)[0]["timestamp"] == "2026-09-29T14:59:00+00:00"
