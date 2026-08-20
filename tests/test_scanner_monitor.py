"""#scanner-monitor: what it prints, and what it refuses to claim.

Two properties matter more than the formatting.

A monitor must not be able to break the thing it monitors -- a Slack
outage during a live order is not a trading failure, so every entry point
swallows everything and returns False.

And it must not invent a winner. With four trading days of data behind
the comparison, "best scanner today" from one candidate is arithmetic
dressed as a finding, so an unmeasured comparison says INSUFFICIENT_SAMPLE.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.notify import monitor  # noqa: E402

CONFIGURED = {monitor.WEBHOOK_ENV: "https://hooks.slack.test/x"}


class TestUnconfiguredIsSilentNotBroken:
    def test_nothing_is_sent_without_a_webhook(self):
        assert monitor.webhook_configured({}) is False
        assert monitor.notify_scan(
            scanner_name="accumulation", session="REGULAR", trading_day="2026-08-20",
            scanned=100, candidates=0, status="SUCCESS", env={}) is False

    def test_an_empty_webhook_counts_as_unset(self):
        assert monitor.webhook_configured({monitor.WEBHOOK_ENV: "   "}) is False

    def test_every_entry_point_survives_a_broken_sender(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("slack down")

        monkeypatch.setattr(monitor, "_send", boom)
        assert monitor.notify_scan(scanner_name="x", session="REGULAR",
                                   trading_day="d", scanned=1, candidates=0,
                                   status="SUCCESS") is False
        assert monitor.notify_buy(strategy="S2", symbol="ABC", session="REGULAR",
                                  qty=1, limit_price=10.0, order_id="1") is False
        assert monitor.notify_fill(strategy="S2", symbol="ABC", qty=1,
                                   average_fill_price=10.0, position_id="p") is False
        assert monitor.notify_sell(strategy="S2", symbol="ABC", reason="VWAP",
                                   qty=1, average_entry=10.0, average_sell=11.0) is False
        assert monitor.notify_tagged(monitor.TAG_RISK, "body") is False
        assert monitor.notify_daily_summary(trading_day="d", rows=[]) is False

    def test_a_non_2xx_response_is_reported_as_not_sent(self, monkeypatch):
        class Response:
            status_code = 500

        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: Response())
        assert monitor._send("hi", env=CONFIGURED) is False


class TestScanMessages:
    def test_zero_candidates_is_still_reported(self):
        """A quiet market is a result, not silence -- and it must be
        distinguishable from a scanner that failed."""
        text = monitor.format_scan(
            scanner_name="accumulation", session="REGULAR", trading_day="2026-08-20",
            scanned=5960, candidates=0, status="SUCCESS")
        assert "[SCANNER S2 VOLUME · REGULAR]" in text
        assert "Candidates: 0" in text
        assert "Status: SUCCESS" in text

    def test_a_failure_reads_differently_from_a_quiet_day(self):
        quiet = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                    trading_day="d", scanned=100, candidates=0,
                                    status="SUCCESS")
        broken = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                     trading_day="d", scanned=0, candidates=None,
                                     status="FAILED_PROVIDER")
        assert "SUCCESS" in quiet and "FAILED_PROVIDER" in broken
        assert quiet != broken

    def test_only_the_top_three_are_listed(self):
        top = [{"symbol": f"S{i}", "score": 90 - i} for i in range(10)]
        text = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                   trading_day="d", scanned=10, candidates=10,
                                   status="SUCCESS", top=top)
        assert "#1 S0" in text and "#3 S2" in text
        assert "#4" not in text
        assert monitor.TOP_N == 3

    def test_the_top_candidate_detail_is_included_when_present(self):
        top = [{"symbol": "ABC", "score": 88.0, "volume_multiple": 3.2,
                "price": 12.5, "vwap": 12.1}]
        text = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                   trading_day="d", scanned=1, candidates=1,
                                   status="SUCCESS", top=top)
        assert "Top candidate: ABC" in text
        assert "volume multiple: 3.20" in text
        assert "VWAP: 12.10" in text

    def test_absent_details_are_omitted_not_faked(self):
        top = [{"symbol": "ABC", "score": 88.0}]
        text = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                   trading_day="d", scanned=1, candidates=1,
                                   status="SUCCESS", top=top)
        assert "VWAP" not in text
        assert "volume multiple" not in text

    @pytest.mark.parametrize("name,tag", [
        ("hma_early_trend", "S1 HMA"), ("accumulation", "S2 VOLUME"),
        ("breakout_ready", "S3 BREAKOUT"), ("premarket_momentum", "S4 PREMARKET"),
        ("gap_pullback", "S5 GAP"), ("orb", "S6 ORB")])
    def test_every_scanner_has_its_tag(self, name, tag):
        assert monitor.scanner_tag(name) == tag

    def test_an_unknown_scanner_is_still_reported(self):
        """Dropping it would hide a scanner that was added and forgotten."""
        assert monitor.scanner_tag("something_new") == "SOMETHING_NEW"


class TestOrderMessages:
    def test_a_buy_reports_accepted_not_filled(self):
        text = monitor.format_buy(strategy="S2", symbol="ABC", session="REGULAR",
                                  qty=1, limit_price=12.34, order_id="0030469882",
                                  rank=2)
        assert "[LIVE BUY · S2]" in text
        assert "Status: ACCEPTED" in text
        assert "Order ID: 0030469882" in text
        assert "Avg fill" not in text, "a buy message must not imply a fill"

    def test_a_fill_carries_the_actual_average(self):
        text = monitor.format_fill(strategy="S1", symbol="TX", qty=1,
                                   average_fill_price=53.68, position_id="s1pos_x")
        assert "[LIVE FILL · S1]" in text
        assert "53.6800" in text
        assert "s1pos_x" in text

    def test_a_sell_without_settled_pnl_says_so(self):
        """Printing a gross number labelled Realized PnL would be a claim
        the ledger cannot support until settlement."""
        text = monitor.format_sell(strategy="S2", symbol="ABC", reason="VWAP_FAIL",
                                   qty=1, average_entry=10.0, average_sell=10.5)
        assert "PENDING_SETTLEMENT" in text

    def test_a_sell_with_pnl_prints_it(self):
        text = monitor.format_sell(strategy="S2", symbol="ABC", reason="VWAP_FAIL",
                                   qty=1, average_entry=10.0, average_sell=10.5,
                                   realized_pnl=0.5, holding_time="2h 15m")
        assert "Realized PnL: 0.50" in text
        assert "Holding time: 2h 15m" in text


class TestDailySummaryRefusesToInventAWinner:
    def test_no_measurements_means_insufficient_sample(self):
        text = monitor.format_daily_summary(trading_day="2026-08-20", rows=[
            {"label": "S1", "candidates": 10},
            {"label": "S2", "candidates": 12},
        ])
        assert text.count(monitor.INSUFFICIENT_SAMPLE) >= 3
        # Candidate counts ARE measured, so that comparison is allowed.
        assert "Most opportunities: S2 (12.00)" in text

    def test_a_winner_needs_a_trade_behind_it(self):
        rows = [{"label": "S1", "candidates": 5, "avg_mfe": 3.0, "trades": 0},
                {"label": "S2", "candidates": 9, "avg_mfe": 1.0, "trades": 2}]
        text = monitor.format_daily_summary(trading_day="d", rows=rows,
                                            minimum_trades_for_winner=1)
        assert "Best scanner today: S2 (1.00)" in text, \
            "S1 has the better MFE but no trade behind it"

    def test_lowest_mae_takes_the_minimum(self):
        rows = [{"label": "S1", "avg_mae": -4.0, "trades": 1},
                {"label": "S2", "avg_mae": -1.0, "trades": 1}]
        text = monitor.format_daily_summary(trading_day="d", rows=rows)
        assert "Lowest MAE: S1 (-4.00)" in text

    def test_every_scanner_appears_even_with_nothing_to_report(self):
        rows = [{"label": f"S{i}", "candidates": 0} for i in range(1, 7)]
        text = monitor.format_daily_summary(trading_day="d", rows=rows)
        for i in range(1, 7):
            assert f"S{i}" in text


class TestItDoesNotDisturbTheAlertChannel:
    def test_it_uses_its_own_webhook(self):
        assert monitor.WEBHOOK_ENV == "SCANNER_MONITOR_SLACK_WEBHOOK_URL"
        assert monitor.WEBHOOK_ENV not in ("SLACK_WEBHOOK_URL",
                                           "SLACK_ALERT_WEBHOOK_URL",
                                           "KIS_LIVE_SLACK_WEBHOOK_URL")

    def test_the_failure_only_alerter_is_untouched(self):
        """Its no-ticker discipline is what makes it worth waking for."""
        source = (REPO_ROOT / "scanners" / "notify" / "slack.py").read_text()
        # Collapsed: the docstring wraps mid-sentence, so a raw substring
        # spanning the line break never matches.
        flat = " ".join(source.split())
        assert "never carries a symbol or a score" in flat
        assert "SCANNER_MONITOR" not in source

    def test_the_monitor_does_not_import_the_alerter(self):
        import ast

        source = (REPO_ROOT / "scanners" / "notify" / "monitor.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [getattr(node, "module", "") or ""] + [a.name for a in node.names]
                for name in names:
                    assert "notify.slack" not in str(name)
                    assert "slack_utils" not in str(name)


class TestRunnerWiring:
    """Every run is reported, including the quiet ones."""

    class Signal:
        def __init__(self, symbol, score, price=None, metrics=None):
            self.symbol, self.scanner_score = symbol, score
            self.signal_price, self.metrics = price, metrics or {}

    class Outcome:
        def __init__(self, name, signals=(), seen=100, failed=False, reason=None):
            self.scanner_name, self.signals = name, list(signals)
            self.symbols_seen, self.failed, self.failure_reason = seen, failed, reason

    class Report:
        def __init__(self, outcomes, profile="daily", trading_day="2026-08-21"):
            self.outcomes, self.profile, self.trading_day = outcomes, profile, trading_day

    def capture(self, monkeypatch):
        sent = []
        monkeypatch.setattr(monitor, "_send", lambda msg, env=None: sent.append(msg) or True)
        return sent

    def test_one_message_per_scanner_including_empty_ones(self, monkeypatch):
        sent = self.capture(monkeypatch)
        report = self.Report([
            self.Outcome("accumulation", [self.Signal("ABC", 90.0)]),
            self.Outcome("orb", []),
        ])
        assert monitor.notify_run(report) == 2
        assert "S2 VOLUME" in sent[0] and "Candidates: 1" in sent[0]
        assert "S6 ORB" in sent[1] and "Candidates: 0" in sent[1]

    def test_a_failed_scanner_says_so(self, monkeypatch):
        sent = self.capture(monkeypatch)
        report = self.Report([self.Outcome("orb", [], failed=True, reason="PROVIDER")])
        monitor.notify_run(report)
        assert "FAILED: PROVIDER" in sent[0]

    def test_candidates_are_ranked_by_score(self, monkeypatch):
        sent = self.capture(monkeypatch)
        signals = [self.Signal("LOW", 10.0), self.Signal("HIGH", 99.0),
                   self.Signal("MID", 50.0)]
        monitor.notify_run(self.Report([self.Outcome("accumulation", signals)]))
        body = sent[0]
        assert body.index("HIGH") < body.index("MID") < body.index("LOW")

    def test_scanner_metrics_reach_the_top_candidate_block(self, monkeypatch):
        sent = self.capture(monkeypatch)
        signal = self.Signal("ABC", 90.0, price=12.5,
                             metrics={"volume_multiple": 3.4, "vwap": 12.1})
        monitor.notify_run(self.Report([self.Outcome("accumulation", [signal])]))
        assert "volume multiple: 3.40" in sent[0]
        assert "VWAP: 12.10" in sent[0]

    def test_the_live_mode_is_reported_per_scanner(self, monkeypatch):
        sent = self.capture(monkeypatch)
        monitor.notify_run(self.Report([
            self.Outcome("hma_early_trend", []), self.Outcome("orb", [])]))
        assert "Live: LIMITED_LIVE" in sent[0], "S1 is the live strategy"
        assert "Live: DISCOVERY_ONLY" in sent[1], "S6 is discovery only"

    def test_a_broken_report_cannot_fail_a_scan(self, monkeypatch):
        self.capture(monkeypatch)
        assert monitor.notify_run(object()) == 0
        assert monitor.notify_run(None) == 0

    def test_the_runner_calls_it_after_the_exit_code_is_decided(self):
        source = (REPO_ROOT / "scanners" / "runner.py").read_text()
        exit_code = source.index("exit_code = 0 if report.status")
        call = source.index("monitor.notify_run(report)")
        returned = source.index("return exit_code")
        assert exit_code < call < returned

    def test_the_runner_guards_the_import(self):
        source = (REPO_ROOT / "scanners" / "runner.py").read_text()
        block = source[source.index("from scanners.notify import monitor"):][:300]
        assert "except Exception" in block
