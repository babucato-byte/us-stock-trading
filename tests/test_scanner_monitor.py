"""#scanner-monitor: what it prints, and what it refuses to claim.

Two properties matter more than the formatting.

A monitor must not be able to break the thing it monitors -- a Slack
outage during a live order is not a trading failure, so every entry point
swallows everything and returns False.

And it must not invent a winner. With four trading days of data behind
the comparison, "best scanner today" from one candidate is arithmetic
dressed as a finding, so an unmeasured comparison says INSUFFICIENT_SAMPLE.
"""

import importlib.util
import os
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
        assert "[S2 VOLUME]" in text
        assert "Session: REGULAR" in text
        # §7 verbatim: the quiet day is reported as a successful scan that
        # found nothing, never as an absence of a message.
        assert "Candidates: 0" in text
        assert "Scanner: SUCCESS" in text

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
        """It may share the transport. It may not share the CHANNEL.

        `slack_utils.send_to_webhook` is a socket with a timeout -- reusing
        it is the point, so there is one place where the transport lives.
        What must never be reused is anything that decides WHERE a message
        lands: `send_slack_alert` and `send_slack_message` resolve the
        alert and report webhooks internally, so importing either would
        put tickers in the channel whose whole discipline is that it
        carries none.
        """
        import ast

        banned = {"send_slack_alert", "send_slack_message",
                  "send_kis_live_alert", "send_kis_live_message"}
        source = (REPO_ROOT / "scanners" / "notify" / "monitor.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = str(getattr(node, "module", "") or "")
                assert "notify.slack" not in module
                for alias in node.names:
                    assert "notify.slack" not in alias.name
                    assert alias.name not in banned, f"imports {alias.name}"
        # And no alert/report webhook name appears anywhere in the file.
        for env_name in ("SLACK_ALERT_WEBHOOK_URL", "KIS_LIVE_SLACK"):
            assert env_name not in source

    def test_the_monitor_sends_through_the_shared_transport(self):
        """§2: reuse the existing sender rather than a second requests.post.

        Two copies of the outbound call means two timeouts, two status
        rules, and one of them silently not getting the next fix.
        """
        source = (REPO_ROOT / "scanners" / "notify" / "monitor.py").read_text()
        assert "send_to_webhook" in source
        assert "requests.post" not in source

    def test_the_shared_transport_resolves_no_webhook_of_its_own(self):
        """`send_to_webhook` takes the URL from its caller.

        If it fell back to an env webhook, an unset monitor URL would
        reroute scanner tickers into whichever channel it defaulted to.
        """
        import inspect

        import slack_utils

        assert "SCANNER_MONITOR_SLACK_WEBHOOK_URL" not in inspect.getsource(
            slack_utils.send_to_webhook)
        assert list(inspect.signature(
            slack_utils.send_to_webhook).parameters) == ["webhook_url", "message"]


@pytest.fixture(autouse=True)
def _fresh_dedup():
    """Each test starts with nothing sent.

    Production does not need this -- a scan is one cron process -- but the
    suppression is module state, and without a reset one test's message
    silences the next test's identical one.
    """
    monitor.reset_scan_dedup()
    yield
    monitor.reset_scan_dedup()


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
        assert "Mode: LIMITED_LIVE" in sent[0], "S1 is the live strategy"
        assert "Mode: DISCOVERY_ONLY" in sent[1], "S6 is discovery only"

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


#: The scanner runtime and the KIS live runtime are separate deployments:
#: the scanners run from a working checkout, the live trading from an
#: immutable release. The monitor MODULE belongs to both, but the live
#: lifecycle it mirrors only exists in the release, so these tests skip
#: where there is no lifecycle to mirror rather than failing on it.
#: Scoped to this class, NOT the module. A module-level importorskip
#: would take the scanner-message tests with it, and those are exactly
#: the ones that must run in the scanner runtime.
def _ln():
    from operations import live_notifications
    return live_notifications


KIS_LIVE_PRESENT = importlib.util.find_spec("operations.live_notifications") is not None


@pytest.mark.skipif(not KIS_LIVE_PRESENT,
                    reason="KIS live lifecycle is not part of the scanner runtime")
class TestOperationalEventsReachTheSameChannel:
    """§6: the live lifecycle is mirrored into #scanner-monitor.

    Mirrored, not rerouted. The KIS live channels are where a real order
    is announced and they keep receiving exactly what they received
    before; the monitor gets a copy so one channel answers "what did the
    system do today" without an operator reading three.
    """

    def capture(self, monkeypatch):
        seen = []
        monkeypatch.setattr(monitor, "_send", lambda msg, env=None: seen.append(msg) or True)
        return seen

    @pytest.mark.parametrize("event,tag", [
        ("FILL_COMPLETED", "LIVE FILL"),
        ("PARTIAL_FILL", "LIVE FILL"),
        ("SELL_FILLED", "LIVE SELL"),
        ("SELL_SUBMITTED", "LIVE SELL"),
        ("EXIT_TRIGGERED", "LIVE SELL"),
        ("RECONCILIATION_MISMATCH", "RECONCILIATION"),
        ("POSITION_MISMATCH", "RECONCILIATION"),
        ("ORDER_REJECTED", "RISK"),
        ("ORDER_UNKNOWN", "RISK"),
        ("KILL_SWITCH_ACTIVATED", "RISK"),
        ("HALT_ACTIVATED", "RISK"),
        ("KIS_API_FAILURE", "RISK"),
        ("DAILY_SUMMARY", "DAILY SUMMARY"),
    ])
    def test_each_event_carries_its_tag(self, event, tag):
        assert _ln().monitor_tag_for(event) == tag

    def test_a_submit_is_tagged_by_side_not_by_event_name(self):
        """ORDER_SUBMITTED carries both directions. Filing a sell under
        [LIVE BUY] would make the channel lie about a real order."""
        assert _ln().monitor_tag_for("ORDER_SUBMITTED", {"side": "buy"}) == "LIVE BUY"
        assert _ln().monitor_tag_for("ORDER_SUBMITTED", {"side": "sell"}) == "LIVE SELL"
        assert _ln().monitor_tag_for("ORDER_ACCEPTED", {"side": "SELL"}) == "LIVE SELL"

    def test_routine_intermediate_events_are_not_mirrored(self):
        """§11: summary over commentary. These stay on the KIS channel."""
        for event in ("MARKET_START", "BUY_CANDIDATE_SELECTED",
                      "LIVE_ORDER_PREPARED", "ORDER_PENDING",
                      "CANCEL_REQUESTED", "CANCEL_COMPLETED"):
            assert _ln().monitor_tag_for(event) is None, event

    def test_an_unknown_event_mirrors_nowhere(self):
        assert _ln().monitor_tag_for("SOMETHING_NEW") is None

    def test_the_mirror_cannot_break_the_kis_notification(self, monkeypatch):
        """The order path must survive a monitor that throws."""
        def boom(*a, **k):
            raise RuntimeError("monitor down")

        monkeypatch.setattr(monitor, "notify_tagged", boom)
        delivered = []
        assert _ln().notify("FILL_COMPLETED", {"symbol": "TX"},
                         send_fn=lambda m: delivered.append(m) or True,
                         track_health=False) is True
        assert delivered, "the KIS message still went out"

    def test_the_mirror_is_sent_even_when_the_kis_send_fails(self, monkeypatch):
        """If the primary webhook is down the monitor line is the only
        record there is -- gating it on the primary would lose it."""
        seen = self.capture(monkeypatch)
        assert _ln().notify("FILL_COMPLETED", {"symbol": "TX"},
                         send_fn=lambda m: False, track_health=False) is False
        assert any("LIVE FILL" in m and "TX" in m for m in seen)

    def test_the_watchdog_announces_only_the_stale_case(self):
        source = (REPO_ROOT / "scripts" / "run_s1_position_watchdog.py").read_text()
        # The CALL, not the `def` -- "notify_monitor(result" matches the
        # definition too, and the definition is above this line by
        # construction, so the loose form would pass for the wrong reason.
        healthy_return = source.index('if result["status"] != STATUS_STALE')
        assert source.index("notify_monitor(result, escalated=escalated)") > healthy_return

    def test_the_watchdog_states_whether_it_actually_escalated(self):
        """"already ENTRY_DISABLED" and "disabled entries just now" are
        different facts; one message for both hides a repeating fault."""
        import scripts.run_s1_position_watchdog as wd

        sent = []
        original = monitor.notify_tagged
        try:
            monitor.notify_tagged = lambda tag, body, **k: sent.append((tag, body)) or True
            wd.notify_monitor({"status": "STALE", "detail": "d", "symbol": "TX",
                               "silent_minutes": 51}, escalated=True)
            wd.notify_monitor({"status": "STALE", "detail": "d", "symbol": "TX",
                               "silent_minutes": 51}, escalated=False)
        finally:
            monitor.notify_tagged = original
        assert sent[0][0] == monitor.TAG_WATCHDOG
        assert "escalated now" in sent[0][1]
        assert "unchanged" in sent[1][1]
        assert "Exits remain permitted." in sent[0][1]


class TestTheWebhookIsFoundUnderCron:
    """The bug this class exists for: configured, deployed, and silent.

    The webhook lives in `.env`. `slack_utils` is what loads that file, at
    import. The monitor read `os.environ` and returned early when the key
    was missing -- which under cron is the state BEFORE anything has
    imported `slack_utils`. Every message was dropped, with no error and
    no log, while the key sat correctly in the file.
    """

    def test_the_env_file_is_loaded_before_the_key_is_read(self, monkeypatch):
        """A key that only appears once `slack_utils` is imported must
        still be found -- that is exactly the cron ordering."""
        import sys

        monkeypatch.delitem(sys.modules, "slack_utils", raising=False)
        monkeypatch.delenv(monitor.WEBHOOK_ENV, raising=False)

        loaded = {}

        class FakeSlackUtils:
            def __init__(self):
                # Stands in for load_dotenv() running at import time.
                os.environ[monitor.WEBHOOK_ENV] = "https://hooks.slack.test/late"
                loaded["yes"] = True

        monkeypatch.setitem(sys.modules, "slack_utils", FakeSlackUtils())
        try:
            assert monitor.webhook_configured() is True
            assert loaded == {"yes": True}
        finally:
            os.environ.pop(monitor.WEBHOOK_ENV, None)

    def test_an_explicit_mapping_is_used_as_given(self):
        """A caller that passes a mapping means that mapping. Preloading
        for it would let the real environment leak into a test."""
        assert monitor.webhook_configured({}) is False
        assert monitor.webhook_configured(
            {monitor.WEBHOOK_ENV: "https://hooks.slack.test/x"}) is True

    def test_a_missing_slack_utils_is_not_fatal(self, monkeypatch):
        """The process environment alone is still a valid answer."""
        import builtins
        import sys

        monkeypatch.delitem(sys.modules, "slack_utils", raising=False)
        real_import = builtins.__import__

        def refuse(name, *a, **k):
            if name == "slack_utils":
                raise ImportError("no dotenv here")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", refuse)
        monkeypatch.setenv(monitor.WEBHOOK_ENV, "https://hooks.slack.test/x")
        assert monitor.webhook_configured() is True
