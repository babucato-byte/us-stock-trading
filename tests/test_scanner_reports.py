"""Weekly and monthly reports, and the analysis export.

Sections 15, 16 and 22.

The property worth the most here is section 14's separation: the monthly
report must NOT compute a profit factor from signal returns. Those
numbers would look entirely reasonable and would describe a strategy
nobody ran, which is the single most misleading thing this report could
contain -- and the failure would be invisible, because a profit factor
of 1.6 printed next to a scanner's name reads as a fact.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.analytics import export as export_module  # noqa: E402
from scanners.analytics import monthly_report, weekly_report  # noqa: E402
from scanners.base import result_store  # noqa: E402
from scanners.base.models import ScannerSignal  # noqa: E402

START = "2026-08-01"
END = "2026-08-31"
DAY = "2026-08-12"


def seed(scanner_name="hma_early_trend", symbol="TEST", *, day=DAY, score=80.0,
         returns=None, metrics=None, version="hma_early_trend_v1.0",
         provider="yfinance", run_id="20260812_DAILY_aaaaaa"):
    signal = ScannerSignal(
        timestamp=f"{day}T14:00:00+00:00",
        trading_day=day,
        symbol=symbol,
        scanner_name=scanner_name,
        scanner_version=version,
        scanner_score=score,
        signal_price=100.0,
        market_data_provider=provider,
        scanner_run_id=run_id,
        source_timeframe="1d",
        extension_hma200_pct=(metrics or {}).get("extension", 3.0),
        metrics={"config_fingerprint": (metrics or {}).get("fingerprint", "aaa111")},
    )
    result_store.write_signals([signal], trading_day=day)
    if returns:
        payload = dict(returns)
        payload["signal_id"] = signal.signal_id
        result_store.write_performance([payload], trading_day=day)
    return signal


class TestWeeklyReport:
    def test_reports_per_scanner_and_version(self):
        seed("hma_early_trend", returns={"return_1d": 2.0, "return_5d": 5.0})
        seed("accumulation", version="accumulation_v1.0",
             returns={"return_1d": -1.0, "return_5d": 1.0})
        report = weekly_report.build(START, END)
        names = {item["scanner_name"] for item in report["scanners"]}
        assert names == {"hma_early_trend", "accumulation"}

    def test_states_horizon_maturity(self):
        """Averages over four matured signals out of ninety are the most
        misread number in any weekly report."""
        seed("hma_early_trend", symbol="AAA", returns={"return_1d": 2.0})
        seed("hma_early_trend", symbol="BBB", returns={"return_1d": 3.0, "return_5d": 9.0})
        report = weekly_report.build(START, END)
        maturity = report["scanners"][0]["maturity"]
        assert maturity["return_1d"]["n"] == 2
        assert maturity["return_5d"]["n"] == 1
        assert maturity["return_5d"]["pct_of_signals"] == pytest.approx(50.0)

    def test_hit_rate_defaults_to_the_1_day_horizon(self):
        """Within one week the 5-day horizon has matured for at most the
        first day or two of signals, so a 5-day hit rate would describe
        Monday rather than the week."""
        assert weekly_report.build(START, END)["hit_horizon"] == "return_1d"

    def test_splits_and_warns_when_the_config_fingerprint_changed_mid_week(self):
        """Section 11: parameters stay frozen through month 1.

        Two fingerprints means they did not. The rows must be SPLIT into
        two experiments -- not merged with a warning attached, because a
        blended average is wrong whether or not anyone reads the note --
        and the split must also be announced, since two table rows for
        one scanner otherwise reads as a display quirk.
        """
        seed("hma_early_trend", symbol="AAA", metrics={"fingerprint": "aaa111"})
        seed("hma_early_trend", symbol="BBB", metrics={"fingerprint": "bbb222"})
        report = weekly_report.build(START, END)

        assert len(report["scanners"]) == 2, "merged two parameter sets into one average"
        assert {item["config_fingerprint"] for item in report["scanners"]} == {
            "aaa111", "bbb222"}
        assert all(item["signal_count"] == 1 for item in report["scanners"])

        splits = report["experiment_splits"]
        assert len(splits) == 1
        assert splits[0]["causes"] == ["config_fingerprint"]
        text = weekly_report.format_report(report)
        assert "WARNING" in text
        assert "without a version bump" in text

    def test_splits_when_the_data_provider_changed_mid_week(self):
        """Section 12: the same scanner over two vendors' bars is two
        experiments. Merging them would measure the vendor gap and
        report it as scanner performance."""
        seed("hma_early_trend", symbol="AAA", provider="yfinance",
             returns={"return_1d": 5.0})
        seed("hma_early_trend", symbol="BBB", provider="alpaca",
             returns={"return_1d": -5.0})
        report = weekly_report.build(START, END)

        assert len(report["scanners"]) == 2
        assert {item["market_data_provider"] for item in report["scanners"]} == {
            "yfinance", "alpaca"}
        splits = report["experiment_splits"]
        assert len(splits) == 1
        assert splits[0]["causes"] == ["market_data_provider"]
        assert "market data provider changed" in weekly_report.format_report(report)

    def test_one_provider_and_one_fingerprint_produces_no_warning(self):
        """What a clean month 1 looks like."""
        seed("hma_early_trend", symbol="AAA")
        seed("hma_early_trend", symbol="BBB")
        report = weekly_report.build(START, END)
        assert len(report["scanners"]) == 1
        assert report["experiment_splits"] == []
        assert "WARNING" not in weekly_report.format_report(report)

    def test_reports_quartiles_alongside_the_mean(self):
        """Section 30: one +100% signal moves a mean and not a median."""
        for index, value in enumerate([1.0, 2.0, 3.0, 100.0]):
            seed("hma_early_trend", symbol=f"S{index}",
                 returns={"return_1d": value, "mfe_5d": value, "mae_5d": -1.0})
        item = weekly_report.build(START, END)["scanners"][0]
        assert item["median_return_1d"] == pytest.approx(2.5)
        assert item["avg_return_1d"] > 25
        assert item["p25_return_1d"] is not None
        assert item["p75_return_1d"] is not None
        assert item["p25_return_1d"] < item["p75_return_1d"]
        assert item["max_return_1d"] == pytest.approx(100.0)
        assert "p25" in weekly_report.format_report(
            weekly_report.build(START, END))

    def test_formats_and_writes(self):
        seed(returns={"return_1d": 2.0})
        report = weekly_report.build(START, END)
        text = weekly_report.format_report(report)
        assert "Scanner weekly report" in text
        path = Path(weekly_report.write(report))
        assert json.loads(path.read_text())["report"] == "weekly"

    def test_an_empty_window_is_a_valid_empty_report(self):
        report = weekly_report.build("1999-01-01", "1999-01-07")
        assert report["total_signals"] == 0
        assert report["scanners"] == []
        weekly_report.format_report(report)


class TestMonthlyReport:
    def test_reports_the_section_16_scanner_metrics(self):
        seed(returns={"return_5d": 6.0, "mfe_5d": 9.0, "mae_5d": -3.0})
        item = monthly_report.build(START, END)["scanners"][0]
        for field in ("signal_count", "hit_rate", "avg_return_5d", "median_return_5d",
                      "avg_mfe", "median_mfe", "avg_mae", "median_mae", "mfe_mae_ratio"):
            assert field in item, field
        assert item["mfe_mae_ratio"] == pytest.approx(3.0)

    def test_trading_metrics_are_not_applicable_without_an_entry_engine(self):
        """Section 14/16/30: deriving a profit factor from signal returns
        would report a result for a strategy that was never run."""
        seed(returns={"return_5d": 6.0})
        trading = monthly_report.build(START, END)["trading"]
        assert trading["applicable"] is False
        assert "never run" in trading["reason"]
        assert "profit_factor" not in trading

    def test_trading_metrics_compute_from_realised_trades(self):
        report = monthly_report.build(START, END, trades=[
            {"pnl": 100.0}, {"pnl": -50.0}, {"pnl": 200.0}, {"pnl": -25.0}])
        trading = report["trading"]
        assert trading["applicable"] is True
        assert trading["trade_count"] == 4
        assert trading["profit_factor"] == pytest.approx(300.0 / 75.0)
        assert trading["win_rate"] == pytest.approx(50.0)
        assert trading["average_win"] == pytest.approx(150.0)
        assert trading["average_loss"] == pytest.approx(-37.5)

    def test_max_drawdown_follows_the_cumulative_curve(self):
        trading = monthly_report.compute_trading_metrics(
            [{"pnl": 100.0}, {"pnl": -60.0}, {"pnl": -20.0}, {"pnl": 10.0}])
        assert trading["max_drawdown"] == pytest.approx(-80.0)

    def test_profit_factor_is_null_rather_than_infinite(self):
        trading = monthly_report.compute_trading_metrics([{"pnl": 10.0}, {"pnl": 5.0}])
        assert trading["profit_factor"] is None

    def test_extension_buckets_answer_the_section_22_question(self):
        """"Do stretched names do worse?" is answered from the recorded
        extension, which section 8 says to store without filtering on."""
        seed(symbol="TIGHT", metrics={"extension": 2.0},
             returns={"return_5d": 8.0, "mfe_5d": 9.0, "mae_5d": -1.0})
        seed(symbol="STRETCHED", metrics={"extension": 55.0},
             returns={"return_5d": -6.0, "mfe_5d": 1.0, "mae_5d": -9.0})
        profile = monthly_report.build(START, END)["scanners"][0]["extension"]
        buckets = {item["extension_bucket"]: item for item in profile}
        assert "<5%" in buckets
        assert ">=40%" in buckets
        assert buckets["<5%"]["avg_return_5d"] > buckets[">=40%"]["avg_return_5d"]

    def test_flags_and_splits_a_mid_month_parameter_change(self):
        seed(symbol="AAA", metrics={"fingerprint": "aaa111"})
        seed(symbol="BBB", metrics={"fingerprint": "bbb222"})
        report = monthly_report.build(START, END)
        assert len(report["scanners"]) == 2
        assert all(item["parameters_stable"] is False for item in report["scanners"])
        assert len(report["experiment_splits"]) == 1
        assert "WARNING" in monthly_report.format_report(report)

    def test_flags_and_splits_a_mid_month_provider_change(self):
        """Section 12."""
        seed(symbol="AAA", provider="yfinance")
        seed(symbol="BBB", provider="polygon")
        report = monthly_report.build(START, END)
        providers = {item["market_data_provider"] for item in report["scanners"]}
        assert providers == {"yfinance", "polygon"}
        assert report["experiment_splits"][0]["causes"] == ["market_data_provider"]

    def test_reports_the_distribution_block(self):
        """Section 30."""
        for index, value in enumerate([1.0, 2.0, 3.0, 80.0]):
            seed(symbol=f"S{index}",
                 returns={"return_5d": value, "mfe_5d": value, "mae_5d": -2.0})
        report = monthly_report.build(START, END)
        item = report["scanners"][0]
        assert item["p25_return_5d"] is not None
        assert item["p75_return_5d"] is not None
        assert item["median_return_5d"] < item["avg_return_5d"]
        assert "DISTRIBUTION" in monthly_report.format_report(report)

    def test_includes_the_intersection_analysis(self):
        seed("hma_early_trend", symbol="NVDA", returns={"return_5d": 9.0})
        seed("accumulation", symbol="NVDA", version="accumulation_v1.0",
             returns={"return_5d": 8.0})
        report = monthly_report.build(START, END)
        assert report["intersections"]["symbol_day_count"] == 1
        assert report["intersections"]["combinations"][0]["confirmation_count"] == 2

    def test_month_bounds_handle_december(self):
        assert monthly_report.month_bounds(2026, 12) == ("2026-12-01", "2026-12-31")
        assert monthly_report.month_bounds(2026, 2) == ("2026-02-01", "2026-02-28")

    def test_formats_and_writes(self):
        seed(returns={"return_5d": 6.0})
        report = monthly_report.build(START, END)
        text = monthly_report.format_report(report)
        assert "SCANNER PERFORMANCE" in text
        assert "TRADING PERFORMANCE" in text
        path = Path(monthly_report.write(report))
        assert json.loads(path.read_text())["report"] == "monthly"


class TestExport:
    def test_csv_carries_signal_and_performance_columns(self):
        seed(returns={"return_1d": 2.0, "return_5d": 5.0, "mfe_5d": 7.0, "mae_5d": -1.0})
        frame = export_module.build_dataset(START, END)
        for column in ("symbol", "scanner_name", "signal_price", "return_5d",
                       "mfe_5d", "mae_5d"):
            assert column in frame.columns, column

    def test_carries_the_confirmation_count(self):
        """Section 22 lists "are multi-scanner symbols better?" as a
        question to answer; the table has to carry the answer per row."""
        seed("hma_early_trend", symbol="NVDA")
        seed("accumulation", symbol="NVDA", version="accumulation_v1.0")
        frame = export_module.build_dataset(START, END)
        assert set(frame["confirmation_count"]) == {2}
        assert all("|" in value for value in frame["scanners_agreeing"])

    def test_nulls_stay_empty_in_the_csv_not_zero(self):
        """Anything consuming this must be able to tell "not measured
        yet" from "went nowhere"."""
        seed(returns={"return_1d": 2.0})
        path = export_module.to_csv(START, END)
        text = Path(path).read_text()
        header = text.splitlines()[0].split(",")
        row = text.splitlines()[1].split(",")
        assert row[header.index("return_5d")] == ""

    def test_leading_columns_come_first(self):
        seed(returns={"return_1d": 2.0})
        frame = export_module.build_dataset(START, END)
        assert list(frame.columns)[:5] == [
            "trading_day", "timestamp", "symbol", "scanner_name", "scanner_version"]

    def test_json_export_states_the_one_directional_rule(self):
        """Section 22: an analysis may propose a change, never apply one."""
        seed(returns={"return_1d": 2.0})
        payload = json.loads(Path(export_module.to_json(START, END)).read_text())
        assert "must never write scanner settings back" in payload["note"]
        assert payload["row_count"] == 1

    def test_an_empty_window_exports_nothing_rather_than_an_empty_file(self):
        assert export_module.to_csv("1999-01-01", "1999-01-31") is None
        assert export_module.to_json("1999-01-01", "1999-01-31") is None
