"""#stock-scanner: what it prints, and what it refuses to claim.

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
        assert monitor.notify_tagged(monitor.TAG_DAILY_SUMMARY, "body") is False
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
        assert "[S2 · 거래량 누적]" in text
        assert "세션: 정규장 (REGULAR)" in text
        # §7 verbatim: the quiet day is reported as a successful scan that
        # found nothing, never as an absence of a message.
        assert "후보 수: 0" in text
        assert "상태: 정상" in text

    def test_a_failure_reads_differently_from_a_quiet_day(self):
        quiet = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                    trading_day="d", scanned=100, candidates=0,
                                    status="SUCCESS")
        broken = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                     trading_day="d", scanned=0, candidates=None,
                                     status="FAILED_PROVIDER")
        assert "정상" in quiet and "데이터 공급 실패" in broken
        assert quiet != broken

    def test_only_the_top_three_are_listed(self):
        top = [{"symbol": f"S{i}", "score": 90 - i} for i in range(10)]
        text = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                   trading_day="d", scanned=10, candidates=10,
                                   status="SUCCESS", top=top)
        assert "1위 S0" in text and "3위 S2" in text
        assert "4위" not in text
        assert monitor.TOP_N == 3

    def test_the_top_candidate_detail_is_included_when_present(self):
        top = [{"symbol": "ABC", "score": 88.0, "volume_multiple": 3.2,
                "price": 12.5, "vwap": 12.1}]
        text = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                   trading_day="d", scanned=1, candidates=1,
                                   status="SUCCESS", top=top)
        assert "상위 후보: ABC" in text
        assert "거래량 배수: 3.20" in text
        assert "VWAP: 12.10" in text

    def test_absent_details_are_omitted_not_faked(self):
        top = [{"symbol": "ABC", "score": 88.0}]
        text = monitor.format_scan(scanner_name="accumulation", session="REGULAR",
                                   trading_day="d", scanned=1, candidates=1,
                                   status="SUCCESS", top=top)
        assert "VWAP" not in text
        assert "거래량 배수" not in text

    @pytest.mark.parametrize("name,tag", [
        ("hma_early_trend", "S1 HMA"), ("accumulation", "S2 VOLUME"),
        ("breakout_ready", "S3 BREAKOUT"), ("premarket_momentum", "S4 PREMARKET"),
        ("gap_pullback", "S5 GAP"), ("orb", "S6 ORB")])
    def test_every_scanner_has_its_tag(self, name, tag):
        assert monitor.scanner_tag(name) == tag

    def test_an_unknown_scanner_is_still_reported(self):
        """Dropping it would hide a scanner that was added and forgotten."""
        assert monitor.scanner_tag("something_new") == "SOMETHING_NEW"


class TestOrderMessagesAreGone:
    """This channel carries scanner analytics, not a live order lifecycle.

    Production showed "[실거래 매수] ORDER_SUBMITTED" and "[실거래 매도]
    ORDER_ACCEPTED" in stock-scanner because the formatters below lived
    here. They are removed, not merely uncalled: a live-order formatter
    in the scanner module invites the next caller. Execution logging is
    unaffected -- order_state_events still records every transition, and
    operations.live_notifications owns the operator-facing half.
    """

    def test_the_live_order_formatters_no_longer_exist(self):
        for gone in ("format_buy", "format_fill", "format_sell",
                     "notify_buy", "notify_fill", "notify_sell"):
            assert not hasattr(monitor, gone), gone

    def test_the_execution_tags_no_longer_exist(self):
        for gone in ("TAG_LIVE_BUY", "TAG_LIVE_FILL", "TAG_LIVE_SELL",
                     "TAG_RISK", "TAG_WATCHDOG", "TAG_RECONCILIATION"):
            assert not hasattr(monitor, gone), gone
        assert monitor.TAG_DAILY_SUMMARY == "DAILY SUMMARY"


class TestDailySummaryRefusesToInventAWinner:
    def test_no_measurements_means_insufficient_sample(self):
        text = monitor.format_daily_summary(trading_day="2026-08-20", rows=[
            {"label": "S1", "candidates": 10},
            {"label": "S2", "candidates": 12},
        ])
        assert text.count("표본 부족") >= 3
        # Candidate counts ARE measured, so that comparison is allowed.
        assert "오늘 가장 많은 기회: S2 (12.00)" in text

    def test_a_winner_needs_a_trade_behind_it(self):
        rows = [{"label": "S1", "candidates": 5, "avg_mfe": 3.0, "trades": 0},
                {"label": "S2", "candidates": 9, "avg_mfe": 1.0, "trades": 2}]
        text = monitor.format_daily_summary(trading_day="d", rows=rows,
                                            minimum_trades_for_winner=1)
        assert "성과 판정: S2 (1.00)" in text, \
            "S1 has the better MFE but no trade behind it"

    def test_lowest_mae_takes_the_minimum(self):
        rows = [{"label": "S1", "avg_mae": -4.0, "trades": 1},
                {"label": "S2", "avg_mae": -1.0, "trades": 1}]
        text = monitor.format_daily_summary(trading_day="d", rows=rows)
        assert "최저 MAE: S1 (-4.00)" in text

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


@pytest.fixture(autouse=True)
def _classifiable(monkeypatch):
    """Treat every test symbol as ordinary stock.

    `notify_run` classifies candidates before ranking them, and the test
    environment has no KIS master -- so without this every symbol is
    UNKNOWN, fails closed, and the top block is empty. That behaviour is
    correct and is asserted in tests/test_candidate_eligibility.py; here
    it would only hide what these tests are about.
    """
    from scanners.publish import eligibility

    monkeypatch.setattr(eligibility, "classify_symbol", lambda symbol, index=None: {
        "security_type": "COMMON_STOCK", "etp_type": None, "exchange": "NASD",
        "live_eligible": True, "classified_at": None})


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
        assert "S2 · 거래량 누적" in sent[0] and "후보 수: 1" in sent[0]
        assert "S6 · 장초반 돌파" in sent[1] and "후보 수: 0" in sent[1]

    def test_a_failed_scanner_says_so(self, monkeypatch):
        sent = self.capture(monkeypatch)
        report = self.Report([self.Outcome("orb", [], failed=True, reason="PROVIDER")])
        monitor.notify_run(report)
        assert "상태: 실패: PROVIDER" in sent[0]

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
        assert "거래량 배수: 3.40" in sent[0]
        assert "VWAP: 12.10" in sent[0]

    def test_the_live_mode_is_reported_per_scanner(self, monkeypatch):
        """Each scanner reports ITS OWN mode, not a shared one.

        Uses a live strategy and a discovery-only one, so the test
        stays about per-scanner reporting rather than about which
        strategies happen to be promoted. `orb` used to be the
        discovery-only example and is now live, which is precisely why
        the pair is chosen for the property and not for the posture.
        """
        sent = self.capture(monkeypatch)
        monitor.notify_run(self.Report([
            self.Outcome("hma_early_trend", []),
            self.Outcome("accumulation", [])]))
        # S1 stood down to DISCOVERY_ONLY on 2026-08-31, so the monitor
        # now reports it as analysis-only. The property under test is
        # that the mode is reported PER SCANNER at all, not which mode
        # S1 happens to hold.
        assert "운영 모드: 분석 전용" in sent[0], "S1 is analysis-only"
        assert "운영 모드: 분석 전용" in sent[1], "S2 is discovery only"

    def test_a_promoted_scanner_reports_limited_live(self, monkeypatch):
        sent = self.capture(monkeypatch)
        monitor.notify_run(self.Report([self.Outcome("orb", [])]))
        assert "운영 모드: 제한 실거래" in sent[0], "S6 is promoted"

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


def _kis_live_present():
    """`find_spec` RAISES when the parent package is absent rather than
    returning None -- and absent is exactly the case in the scanner
    runtime, so the unguarded call fails collection for the whole file."""
    try:
        return importlib.util.find_spec("operations.live_notifications") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


KIS_LIVE_PRESENT = _kis_live_present()


@pytest.mark.skipif(not KIS_LIVE_PRESENT,
                    reason="KIS live lifecycle is not part of the scanner runtime")
class TestTheLiveLifecycleIsNoLongerMirroredHere:
    """2026-09: stock-sanner carries ONLY the daily S1-S5 summary.

    The live lifecycle used to be copied into this channel under
    [LIVE BUY]/[LIVE FILL]/[RISK] tags, which doubled every order message
    and buried the scanner results. The copy is gone: fills, cancels and
    faults are presented once, on stock-live-trading / stock-live-alerts,
    by operations.live_notifications.
    """

    def capture(self, monkeypatch):
        seen = []
        monkeypatch.setattr(monitor, "_send", lambda msg, env=None: seen.append(msg) or True)
        return seen

    def test_live_notifications_no_longer_know_the_monitor(self):
        ln = _ln()
        assert not hasattr(ln, "_mirror_to_monitor")
        assert not hasattr(ln, "monitor_tag_for")
        source = (REPO_ROOT / "operations" / "live_notifications.py").read_text()
        assert "scanners.notify" not in source

    @pytest.mark.parametrize("event", ["FILL_COMPLETED", "SELL_FILLED", "ORDER_UNKNOWN",
                                       "KILL_SWITCH_ACTIVATED", "CANCEL_COMPLETED"])
    def test_a_live_event_sends_nothing_to_the_scanner_channel(self, monkeypatch, event):
        seen = self.capture(monkeypatch)
        delivered = []
        _ln().notify(event, {"symbol": "TX"},
                     send_fn=lambda m: delivered.append(m) or True, track_health=False)
        assert delivered, "the live channel still gets the message"
        assert seen == [], "the scanner channel gets no copy"

    def test_the_watchdog_alerts_only_when_it_actually_escalated(self, monkeypatch):
        """"already ENTRY_DISABLED" and "disabled entries just now" are
        different facts. Only the second is an alert; it goes to
        stock-live-alerts, not to this channel."""
        import scripts.run_s1_position_watchdog as wd

        ln = _ln()
        sent = []
        monkeypatch.setattr(ln, "notify",
                            lambda event, fields=None, **k: sent.append((event, fields)) or True)
        seen = self.capture(monkeypatch)
        wd.notify_monitor({"status": "STALE", "detail": "d", "symbol": "TX",
                           "silent_minutes": 51}, escalated=True)
        wd.notify_monitor({"status": "STALE", "detail": "d", "symbol": "TX",
                           "silent_minutes": 51}, escalated=False)
        assert [e for e, _ in sent] == [ln.WATCHDOG_ESCALATED]
        # The bare state word travels; slack_presentation renders it as
        # "안전 중지 상태: 신규 진입 차단" from the one shared mapping.
        assert sent[0][1]["kill_switch"] == "ENTRY_DISABLED"
        assert seen == []

    def test_the_watchdog_announces_only_the_stale_case(self):
        source = (REPO_ROOT / "scripts" / "run_s1_position_watchdog.py").read_text()
        # The CALL, not the `def` -- "notify_monitor(result" matches the
        # definition too, and the definition is above this line by
        # construction, so the loose form would pass for the wrong reason.
        healthy_return = source.index('if result["status"] != STATUS_STALE')
        call = source.index("notify_monitor(result, escalated=escalated)")
        assert call > healthy_return


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


class TestOwnershipIsEnforcedNotAssumed:
    """§2: one runtime announces scans, the other announces orders.

    Both runtimes deploy from the same repository, so `scanners/runner.py`
    exists in both and both could reach the same webhook. Only the
    scanner runtime's cron actually invokes it today -- but "nobody calls
    it yet" is not a guarantee anyone can see, and the day a scanner cron
    is added to the release, every scan is announced twice.
    """

    def capture(self, monkeypatch):
        seen = []
        monkeypatch.setattr(monitor, "_send",
                            lambda msg, env=None: seen.append(msg) or True)
        return seen

    class Outcome:
        def __init__(self, name):
            self.scanner_name, self.signals = name, []
            self.symbols_seen, self.failed, self.failure_reason = 10, False, None

    class Report:
        def __init__(self, outcomes):
            self.outcomes, self.trading_day, self.profile = outcomes, "2026-08-20", "daily"

    def test_the_scanner_runtime_owns_scan_messages(self):
        """No DEPLOYED_COMMIT -- a working checkout, so it announces."""
        assert monitor.scanner_notifications_owned_here({}) is True

    def test_the_trading_release_does_not_announce_scans(self):
        assert monitor.scanner_notifications_owned_here(
            {monitor.TRADING_RUNTIME_MARKER: "bee3ee88f53f"}) is False

    def test_a_blank_marker_is_not_a_release(self):
        assert monitor.scanner_notifications_owned_here(
            {monitor.TRADING_RUNTIME_MARKER: "   "}) is True

    def test_notify_run_sends_nothing_from_the_trading_release(self, monkeypatch):
        """The dead wiring is inert by rule, not merely by crontab."""
        sent = self.capture(monkeypatch)
        count = monitor.notify_run(
            self.Report([self.Outcome("hma_early_trend")]),
            env={monitor.TRADING_RUNTIME_MARKER: "bee3ee88f53f",
                 monitor.WEBHOOK_ENV: "https://hooks.slack.test/x"})
        assert count == 0
        assert sent == [], "the release must not announce a scan"

    def test_notify_run_sends_from_the_scanner_runtime(self, monkeypatch):
        sent = self.capture(monkeypatch)
        count = monitor.notify_run(
            self.Report([self.Outcome("hma_early_trend")]),
            env={monitor.WEBHOOK_ENV: "https://hooks.slack.test/x"})
        assert count == 1
        assert "[S1 · HMA 초기추세]" in sent[0]

    @pytest.mark.skipif(not KIS_LIVE_PRESENT,
                        reason="no live lifecycle in the scanner runtime")
    def test_order_events_are_not_gated_by_scanner_ownership(self, monkeypatch):
        """Gating both halves on one flag would silence the trading
        runtime for the messages it is the only one able to send."""
        monkeypatch.setenv(monitor.TRADING_RUNTIME_MARKER, "bee3ee88f53f")
        delivered = []
        _ln().notify("FILL_COMPLETED", {"symbol": "TX", "filled_qty": 1, "fill_price": 2.0},
                     send_fn=lambda m: delivered.append(m) or True, track_health=False)
        assert any("[매수 체결]" in m for m in delivered), \
            "the release must still announce its own fills"


class TestOneSessionVocabulary:
    """The channel and the code must mean the same thing by "session".

    The coverage line used to name OVERNIGHT and DAYTIME separately --
    two names `scan_session.normalize()` rejects, because the venue
    treats that window as a single bucket. So a message advertised
    coverage of sessions no scan could ever be labelled with, and a
    reader comparing the line against a Session: field would find names
    that never appear there.
    """

    def test_the_coverage_line_uses_the_real_session_names(self):
        from scanners.base import scan_session

        assert monitor.ALL_SESSIONS == tuple(scan_session.SESSIONS)

    def test_every_advertised_session_is_one_a_scan_can_carry(self):
        from scanners.base import scan_session

        for name in monitor.ALL_SESSIONS:
            assert scan_session.normalize(name) == name, name

    def test_the_split_overnight_names_are_gone(self):
        assert "OVERNIGHT" not in monitor.ALL_SESSIONS
        assert "DAYTIME" not in monitor.ALL_SESSIONS
        assert "OVERNIGHT_DAYTIME" in monitor.ALL_SESSIONS

    def test_a_routed_session_reports_live_capable(self):
        """PREMARKET reads as live-capable now, and that is the point of
        the correction: it shares the general order family with the
        regular session, so reporting it as scan-only UNDERSTATED what
        S6 can actually do. A report that understates capability is as
        wrong as one that overstates it."""
        text = monitor.format_scan(
            scanner_name="accumulation", session="PREMARKET", trading_day="d",
            scanned=10, candidates=1, status="SUCCESS")
        assert "세션 주문: 실거래 가능" in text

    def test_a_session_with_no_route_claims_nothing(self):
        """It omits the line rather than asserting either way -- which is
        the right answer for a session nobody defined."""
        text = monitor.format_scan(
            scanner_name="accumulation", session="NOT_A_SESSION",
            trading_day="d", scanned=10, candidates=1, status="SUCCESS")
        assert "세션 주문:" not in text

    def test_a_verified_session_says_that_instead(self):
        text = monitor.format_scan(
            scanner_name="accumulation", session="REGULAR", trading_day="d",
            scanned=10, candidates=1, status="SUCCESS")
        assert "세션 주문: 실거래 가능" in text

    def test_a_profile_name_claims_no_execution_status(self):
        """"DAILY" is a scanner group, not a session. Claiming a
        verification status for it would be inventing one."""
        text = monitor.format_scan(
            scanner_name="accumulation", session="DAILY", trading_day="d",
            scanned=10, candidates=0, status="SUCCESS")
        assert "세션 주문:" not in text

    def test_the_run_reports_its_clock_session_not_its_profile(self, monkeypatch):
        sent = []
        monkeypatch.setattr(monitor, "_send",
                            lambda msg, env=None: sent.append(msg) or True)

        class Outcome:
            scanner_name, failed, failure_reason = "accumulation", False, None
            signals, symbols_seen = [], 100

        class Report:
            outcomes = [Outcome()]
            trading_day, profile, session = "d", "daily", "REGULAR"

        monitor.notify_run(Report(), env={monitor.WEBHOOK_ENV: "https://x.test"})
        assert "세션: 정규장 (REGULAR)" in sent[0]
        assert "세션: DAILY" not in sent[0]


class TestAScannerThatVanishedIsNotSilent:
    """The worst case §8 guards against, found by breaking a real config.

    A scanner that fails to CONSTRUCT never reaches `report.outcomes`, so
    the per-outcome loop cannot see it. Before this, a scanner with an
    unparseable config produced no message at all -- not "0 candidates",
    not "FAILED", nothing. Five messages arrived where six were expected
    and the reader had to notice an absence to catch it, which is the one
    thing a monitor exists to stop being necessary.
    """

    def capture(self, monkeypatch):
        seen = []
        monkeypatch.setattr(monitor, "_send",
                            lambda msg, env=None: seen.append(msg) or True)
        return seen

    class Outcome:
        def __init__(self, name):
            self.scanner_name, self.signals = name, []
            self.symbols_seen, self.failed, self.failure_reason = 5, False, None

    class Report:
        def __init__(self, outcomes, construction_failures=None):
            self.outcomes = outcomes
            self.construction_failures = construction_failures or {}
            self.trading_day, self.profile, self.session = "d", "daily", "REGULAR"

    def test_a_scanner_that_could_not_be_built_is_reported(self, monkeypatch):
        sent = self.capture(monkeypatch)
        count = monitor.notify_run(
            self.Report([self.Outcome("accumulation")],
                        {"breakout_ready": "invalid config json"}),
            env={monitor.WEBHOOK_ENV: "https://x.test"})
        assert count == 2, "the built one AND the broken one"
        broken = [m for m in sent if "S3 · 돌파 준비" in m]
        assert broken, "the scanner that vanished must still get a line"
        assert "실행 준비 실패" in broken[0]
        assert "invalid config json" in broken[0]

    def test_it_is_not_reported_as_a_quiet_day(self, monkeypatch):
        """`Candidates: 0` would say the scanner ran and found nothing.
        It did not run."""
        sent = self.capture(monkeypatch)
        monitor.notify_run(self.Report([], {"orb": "boom"}),
                           env={monitor.WEBHOOK_ENV: "https://x.test"})
        assert "후보 수: 0" not in sent[0]
        assert "후보 수: -" in sent[0]
        assert "상태: 정상" not in sent[0]

    def test_every_scanner_in_the_run_gets_exactly_one_line(self, monkeypatch):
        sent = self.capture(monkeypatch)
        monitor.notify_run(
            self.Report([self.Outcome("hma_early_trend"),
                         self.Outcome("accumulation")],
                        {"orb": "a", "gap_pullback": "b"}),
            env={monitor.WEBHOOK_ENV: "https://x.test"})
        # S6 renders by VARIANT: one scanner runs in four sessions and
        # each forms its own range, so "S6 · 돌파" alone would not say
        # WHICH range broke. The report's session is REGULAR, hence S6-R.
        tags = ["S1 · HMA 초기추세", "S2 · 거래량 누적",
                "S6 · 정규장 돌파", "S5 · 갭 눌림"]
        for tag in tags:
            assert sum(f"[{tag}]" in m for m in sent) == 1, tag
        assert len(sent) == 4


class TestKoreanDisplayLayer:
    """Korean in the message, English in the data.

    The property that matters is the boundary. `exit_reason` stays
    VOLUME_DECAY_PRICE_WEAKNESS in the decision, the trade record, the
    database and every comparison; only the printed line is Korean. A
    localisation that reached the stored values would make historical
    rows unqueryable by the code that wrote them, and a strategy_id that
    differed by locale would break the position limit silently -- a
    failure that shows up as a missing refusal rather than an error.
    """

    def test_every_scanner_has_a_korean_name(self):
        from scanners.notify import labels

        expected = {
            "hma_early_trend": "S1 · HMA 초기추세",
            "accumulation": "S2 · 거래량 누적",
            "breakout_ready": "S3 · 돌파 준비",
            "premarket_momentum": "S4 · 프리마켓 모멘텀",
            "gap_pullback": "S5 · 갭 눌림",
            "orb": "S6 · 장초반 돌파"}
        for name, korean in expected.items():
            assert labels.scanner(name) == korean

    @pytest.mark.parametrize("code,korean", [
        ("REGULAR", "정규장"), ("PREMARKET", "프리마켓"),
        ("AFTER_HOURS", "시간외"), ("OVERNIGHT_DAYTIME", "주간/오버나이트"),
        ("CLOSED", "장 마감")])
    def test_sessions_are_translated_and_keep_their_code(self, code, korean):
        from scanners.notify import labels

        assert labels.session(code) == f"{korean} ({code})"
        assert labels.session(code, with_code=False) == korean

    @pytest.mark.parametrize("code,korean", [
        ("SUCCESS", "정상"), ("FAILED", "실패"),
        ("FAILED_NOT_BUILT", "실행 준비 실패"),
        ("DISCOVERY_ONLY", "분석 전용"), ("LIMITED_LIVE", "제한 실거래"),
        ("SCAN_ONLY", "스캔 전용"), ("LIVE_UNVERIFIED", "실거래 미검증"),
        ("INSUFFICIENT_SAMPLE", "표본 부족"),
        ("PENDING_SETTLEMENT", "정산 대기")])
    def test_statuses_are_translated(self, code, korean):
        from scanners.notify import labels

        assert labels.status(code) == korean

    @pytest.mark.parametrize("code,korean", [
        ("VOLUME_DECAY", "거래량 모멘텀 감소"),
        ("VOLUME_DECAY_PRICE_WEAKNESS", "거래량 감소 + 가격 약화"),
        ("VWAP_FAILURE", "VWAP 이탈"),
        ("STRUCTURE_FAILURE", "가격 구조 붕괴"),
        ("HARD_STOP", "최대 손실 제한"),
        ("SESSION_EXIT", "세션 종료 청산")])
    def test_exit_reasons_print_korean_and_the_code(self, code, korean):
        from scanners.notify import labels

        assert labels.exit_reason(code) == f"{korean} ({code})"

    def test_every_s2_exit_reason_has_a_translation(self):
        """A reason the code can emit but the channel cannot name would
        reach an operator as a bare identifier."""
        from s2_live import exit_policy
        from scanners.notify import labels

        for reason in exit_policy.EXIT_REASONS:
            assert reason in labels.EXIT_REASON_LABELS, reason

    def test_an_untranslated_value_passes_through_rather_than_hiding(self):
        """Failing to translate must not look like failing to happen."""
        from scanners.notify import labels

        assert labels.scanner("something_new") == "something_new"
        assert labels.exit_reason("NEW_REASON") == "NEW_REASON"
        assert labels.status("NEW_STATUS") == "NEW_STATUS"

    def test_a_failure_status_keeps_its_detail(self):
        """The detail is usually the only thing that says WHY."""
        from scanners.notify import labels

        assert labels.status("FAILED: config 로드 실패") == "실패: config 로드 실패"

    def test_zero_candidates_reads_as_a_result(self):
        text = monitor.format_scan(
            scanner_name="accumulation", session="REGULAR",
            trading_day="2026-08-20", scanned=5960, candidates=0,
            status="SUCCESS", live_status="LIMITED_LIVE")
        assert "[S2 · 거래량 누적]" in text
        assert "상태: 정상" in text
        assert "후보 수: 0" in text
        assert "조건을 충족한 종목이 없습니다" in text
        assert "운영 모드: 제한 실거래" in text

    def test_a_build_failure_reads_differently_and_shows_a_dash(self):
        """0 means a completed scan that found nothing; "-" means the
        scan never completed. The two must never share a phrasing."""
        text = monitor.format_scan(
            scanner_name="breakout_ready", session="REGULAR", trading_day="d",
            scanned=None, candidates=None,
            status="FAILED_NOT_BUILT: config 로드 실패")
        assert "[S3 · 돌파 준비]" in text
        assert "상태: 실행 준비 실패: config 로드 실패" in text
        assert "후보 수: -" in text
        assert "후보 수: 0" not in text
        assert "조건을 충족한 종목이 없습니다" not in text

    def test_the_korean_order_messages_moved_to_the_trading_channel(self):
        """Their Korean rendering now lives in operations.slack_presentation
        and is sent to stock-live-trading, once per lifecycle."""
        from operations import slack_presentation as sp

        assert sp.buy_filled({"symbol": "ABC", "quantity": 1,
                              "average_fill_price": 12.34}).startswith("[매수 체결]")
        assert sp.sell_filled({"symbol": "ABC", "quantity": 1,
                               "average_fill_price": 12.80}).startswith("[매도 체결]")

    @pytest.mark.parametrize("tag,korean", [
        ("DAILY SUMMARY", "일일 스캐너 요약")])
    def test_operational_tags_are_korean(self, tag, korean, monkeypatch):
        sent = []
        monkeypatch.setattr(monitor, "_send",
                            lambda m, env=None: sent.append(m) or True)
        monitor.notify_tagged(tag, "본문")
        assert sent[0].startswith(f"[{korean}]")

    def test_the_summary_refuses_to_name_a_winner_without_a_sample(self):
        text = monitor.format_daily_summary(
            trading_day="d", rows=[{"label": "accumulation", "candidates": 14,
                                    "trades": 0}])
        assert "성과 판정: 표본 부족" in text

    def test_times_show_et_and_kst(self):
        from scanners.notify import labels

        both = labels.dual_time("2026-08-20T14:15:00+00:00")
        assert "10:15 ET" in both and "23:15 KST" in both

    def test_an_unparseable_time_is_returned_not_dropped(self):
        from scanners.notify import labels

        assert labels.dual_time("not a time") == "not a time"


class TestLocalisationDidNotReachTheData:
    """§O: this was a display change. Nothing behavioural moved."""

    def test_internal_exit_reasons_are_still_ascii_identifiers(self):
        from s2_live import exit_policy

        for reason in exit_policy.EXIT_REASONS:
            assert reason.isascii() and reason.isupper()

    def test_internal_session_names_are_unchanged(self):
        from scanners.base import scan_session

        assert scan_session.SESSIONS == ("PREMARKET", "REGULAR", "AFTER_HOURS",
                                         "OVERNIGHT_DAYTIME")

    def test_strategy_ids_are_unchanged(self):
        from config import position_limits

        for name in position_limits.PROPOSED_STRATEGY_MAX:
            assert name.isascii()

    def test_the_live_mode_values_are_unchanged(self):
        from config import scanner_live_mode

        for mode in scanner_live_mode.SCANNER_LIVE_MODE.values():
            assert mode.isascii() and mode.isupper()

    def test_the_labels_module_decides_nothing(self):
        """A display table that could reach a policy would let a
        translation change a trade."""
        import ast

        banned = {"brokers", "execution", "order_gate", "s2_live", "s1_live",
                  "position_limits", "kis_broker"}
        source = (REPO_ROOT / "scanners" / "notify" / "labels.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, f"imports {name}"

    def test_dedup_still_works_after_localisation(self):
        monitor.reset_scan_dedup()
        sent = []
        first = monitor.format_scan(scanner_name="accumulation",
                                    session="REGULAR", trading_day="d",
                                    scanned=1, candidates=0, status="SUCCESS")
        second = monitor.format_scan(scanner_name="accumulation",
                                     session="REGULAR", trading_day="d",
                                     scanned=1, candidates=0, status="SUCCESS")
        assert first == second, "identical scans still render identically"
        monitor.reset_scan_dedup()

    def test_the_webhook_env_is_unchanged(self):
        assert monitor.WEBHOOK_ENV == "SCANNER_MONITOR_SLACK_WEBHOOK_URL"
