"""Safety and schema tests for the Oracle-only offline export step."""

import json
from datetime import datetime, timedelta, timezone

from validation.export_s6_provider_data import S6ProviderDataExporter, secrets_in_export


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
                 "lowPrice": item["low"], "closePrice": item["close"], "volume": item["volume"]} for item in _bars()]
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
