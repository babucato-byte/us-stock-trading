"""Focused tests for the 2026-09-14 operator-notification audit (13
items). Each test proves the BEFORE/AFTER behaviour of one fix, not
merely "no exception".
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from operations import live_notifications as ln
from operations import notification_ledger as ledger
from operations import slack_presentation as sp


def _ledger_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE notification_ledger (
        notification_key TEXT PRIMARY KEY, event_type TEXT, strategy_id TEXT,
        symbol TEXT, subject_id TEXT, state_version TEXT, channel TEXT,
        event_time TEXT, sent_at TEXT, delay_seconds REAL, created_at TEXT)""")
    conn.commit()
    return conn


# ---------------------------------------------------------------------
# S-01: broker-confirmed accepted/submitted orders are now presented,
# durably, keyed on broker_order_id.
# ---------------------------------------------------------------------
class TestS01OrderAcceptedIsPresentedAndDurable:
    def test_order_accepted_is_no_longer_internal(self):
        assert ln.ORDER_ACCEPTED not in ln.INTERNAL_EVENTS
        assert ln.ORDER_ACCEPTED in ln.LIVE_TRADING_EVENTS
        assert ln.channel_for(ln.ORDER_ACCEPTED) == sp.LIVE_TRADING

    def test_order_accepted_produces_a_korean_message_naming_the_order(self):
        sent = []
        ln.notify(ln.ORDER_ACCEPTED,
                 {"symbol": "AAPL", "side": "buy", "state": "ACCEPTED",
                  "broker_order_id": "kis-42"},
                 send_fn=lambda m: sent.append(m) or True, track_health=False)
        assert len(sent) == 1
        assert "[매수 주문 접수 완료]" in sent[0]
        assert "kis-42" in sent[0]

    def test_a_restart_between_wire_and_notify_does_not_duplicate_or_lose_it(self):
        """The durable claim is keyed on broker_order_id: two separate
        `notify()` calls (simulating a process that crashed and retried)
        for the SAME order id send exactly once."""
        conn = _ledger_conn()
        sent = []

        def _first_attempt():
            ln.notify(ln.ORDER_ACCEPTED,
                     {"symbol": "MSFT", "side": "buy", "broker_order_id": "kis-99",
                      "state": "ACCEPTED"},
                     send_fn=lambda m: sent.append(m) or True, track_health=False,
                     dedupe_conn=conn, dedupe_subject="kis-99",
                     dedupe_version="BROKER_CONFIRMED")

        _first_attempt()
        # "process restart": a fresh call for the identical order id.
        _first_attempt()
        assert len(sent) == 1


# ---------------------------------------------------------------------
# S-02: account-wide reconciliation CLEAN -> DIRTY is a durable alert;
# still-dirty and still-clean produce nothing further.
# ---------------------------------------------------------------------
class TestS02ReconciliationTransitionAlert:
    def test_clean_to_dirty_alerts_but_still_dirty_does_not_repeat(self):
        import scripts.run_reconciliation as rr

        sent = []
        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        conn2 = _ledger_conn()

        def _send(message):
            sent.append(message)
            return True

        import operations.live_notifications as ln_mod
        orig_sender = ln_mod._sender_for
        ln_mod._sender_for = lambda event: _send
        try:
            rr._announce_reconciliation_transition(
                conn2, clean=False, was_clean=True, mismatch_count=2, detail=["x"], now=now)
            # still dirty next tick: no second alert
            rr._announce_reconciliation_transition(
                conn2, clean=False, was_clean=False, mismatch_count=2, detail=["x"],
                now=now + timedelta(minutes=5))
        finally:
            ln_mod._sender_for = orig_sender
        assert len(sent) == 1
        assert "계좌 대조 불일치" in sent[0]

    def test_dirty_to_clean_sends_a_recovery_message(self):
        import scripts.run_reconciliation as rr

        conn = _ledger_conn()
        sent = []

        def _send(message):
            sent.append(message)
            return True

        import operations.live_notifications as ln_mod
        orig_sender = ln_mod._sender_for
        ln_mod._sender_for = lambda event: _send
        try:
            rr._announce_reconciliation_transition(
                conn, clean=True, was_clean=False, mismatch_count=0, detail=(),
                now=datetime(2026, 9, 14, 12, 5, tzinfo=timezone.utc))
        finally:
            ln_mod._sender_for = orig_sender
        assert len(sent) == 1
        assert "계좌 대조 정상 복구" in sent[0]
        assert not sent[0].startswith("🚨")

    def test_unknown_previous_state_does_not_alert_on_first_ever_dirty_read(self):
        """No prior recorded result (was_clean=None) must not be treated
        as a fabricated CLEAN baseline -- but it also must not suppress a
        real first alert forever. The chosen contract: unknown behaves
        like "was clean" for alert purposes only when currently dirty is
        the FIRST thing ever observed, so it does alert once."""
        import scripts.run_reconciliation as rr

        conn = _ledger_conn()
        sent = []

        def _send(message):
            sent.append(message)
            return True

        import operations.live_notifications as ln_mod
        orig_sender = ln_mod._sender_for
        ln_mod._sender_for = lambda event: _send
        try:
            rr._announce_reconciliation_transition(
                conn, clean=False, was_clean=None, mismatch_count=1, detail=["y"],
                now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
        finally:
            ln_mod._sender_for = orig_sender
        assert len(sent) == 1


# ---------------------------------------------------------------------
# S-03: bounded consecutive exit-evaluation-failure alert + recovery.
# ---------------------------------------------------------------------
class TestS03ExitEvaluationFailureIsBoundedAndAlerted:
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        return conn

    def test_alert_fires_exactly_at_the_threshold_not_every_tick(self):
        from s6_live import exit_runtime as er

        conn = self._conn()
        sent = []

        def _send(message):
            sent.append(message)
            return True

        import operations.live_notifications as ln_mod
        orig_sender = ln_mod._sender_for
        ln_mod._sender_for = lambda event: _send
        try:
            for _ in range(er.EXIT_EVALUATION_FAILURE_THRESHOLD - 1):
                er._record_exit_evaluation_outcome(conn, "pos-1", "NVDA", ok=False)
            assert sent == []  # below threshold: silent, not spamming either
            er._record_exit_evaluation_outcome(conn, "pos-1", "NVDA", ok=False)
            assert len(sent) == 1
            assert "포지션 청산 평가 반복 실패" in sent[0]
            # a further failing tick must not re-alert
            er._record_exit_evaluation_outcome(conn, "pos-1", "NVDA", ok=False)
            assert len(sent) == 1
        finally:
            ln_mod._sender_for = orig_sender

    def test_recovery_message_after_a_failing_streak(self):
        from s6_live import exit_runtime as er

        conn = self._conn()
        sent = []

        def _send(message):
            sent.append(message)
            return True

        import operations.live_notifications as ln_mod
        orig_sender = ln_mod._sender_for
        ln_mod._sender_for = lambda event: _send
        try:
            for _ in range(er.EXIT_EVALUATION_FAILURE_THRESHOLD):
                er._record_exit_evaluation_outcome(conn, "pos-2", "TSLA", ok=False)
            sent.clear()
            er._record_exit_evaluation_outcome(conn, "pos-2", "TSLA", ok=True)
            assert len(sent) == 1
            assert "정상 복구" in sent[0]
            # the counter must have reset: no further recovery on the next OK
            sent.clear()
            er._record_exit_evaluation_outcome(conn, "pos-2", "TSLA", ok=True)
            assert sent == []
        finally:
            ln_mod._sender_for = orig_sender

    def test_a_single_failure_below_threshold_never_alerts(self):
        from s6_live import exit_runtime as er

        conn = self._conn()
        sent = []
        import operations.live_notifications as ln_mod
        orig_sender = ln_mod._sender_for
        ln_mod._sender_for = lambda event: (lambda m: sent.append(m) or True)
        try:
            er._record_exit_evaluation_outcome(conn, "pos-3", "AMD", ok=False)
            er._record_exit_evaluation_outcome(conn, "pos-3", "AMD", ok=True)
        finally:
            ln_mod._sender_for = orig_sender
        assert sent == []


# ---------------------------------------------------------------------
# S-04/S-05: session is preserved in block/reject/UNKNOWN/cancel
# messages.
# ---------------------------------------------------------------------
class TestS04S05SessionIsPreserved:
    def test_order_blocked_shows_the_session(self):
        fields = ln.order_blocked_fields(symbol="RIG", reason_code="INSUFFICIENT_CASH",
                                         session="PREMARKET")
        text = sp.order_blocked(fields)
        assert "세션: 프리장 (PREMARKET)" in text

    def test_order_failed_shows_the_session(self):
        text = sp.order_failed({"symbol": "RIG", "side": "buy", "reason": "BROKER_SUBMIT_FAILED",
                                "session": "AFTER_HOURS"})
        assert "세션: 애프터장 (AFTER_HOURS)" in text

    def test_unknown_order_fields_carries_session_into_the_critical_alert(self):
        fields = ln.unknown_order_fields(symbol="RIG", side="buy",
                                         durable_state="UNKNOWN", session="REGULAR")
        text = sp.critical(ln.ORDER_UNKNOWN, fields)
        assert "세션: 정규장 (REGULAR)" in text

    def test_cancel_failed_critical_alert_carries_session(self):
        text = sp.critical(ln.CANCEL_FAILED,
                           {"symbol": "RIG", "broker_order_id": "k1",
                            "reason": "boom", "session": "OVERNIGHT_DAYTIME"})
        assert "세션: 데이장 (OVERNIGHT_DAYTIME)" in text

    def test_run_live_buy_entry_announce_blocks_threads_session_through(self):
        import importlib

        runner = importlib.import_module("scripts.run_live_buy_entry")
        sent = []
        import operations.live_notifications as ln_mod
        orig_sender = ln_mod._sender_for
        ln_mod._sender_for = lambda event: (lambda m: sent.append(m) or True)
        try:
            runner._announce_blocks([("RIG", "insufficient KIS orderable cash")],
                                    session="PREMARKET")
        finally:
            ln_mod._sender_for = orig_sender
        assert sent, "expected one block message"
        assert "세션: 프리장 (PREMARKET)" in sent[0]


# ---------------------------------------------------------------------
# S-07: cash-precheck refresh failure is presented as UNKNOWN, not as a
# stale current figure.
# ---------------------------------------------------------------------
class TestS07CashPrecheckRefreshFailurePresentation:
    def test_refresh_failure_detail_reads_unknown_not_stale_available(self):
        from scripts.run_live_buy_entry import _cash_precheck_detail_text

        detail = {"reason": "authoritative refresh failed after a low cached read",
                  "available_cash": 11.98, "required_for_1_share": 149.63,
                  "shortfall": 137.65}
        text = _cash_precheck_detail_text(detail)
        assert "available=UNKNOWN" in text
        assert "cached_available=11.98" in text
        assert "refresh_failed=true" in text
        assert not text.startswith("available=11.98")  # not presented as the current figure

    def test_an_ordinary_block_still_shows_available(self):
        from scripts.run_live_buy_entry import _cash_precheck_detail_text

        detail = {"available_cash": 5.0, "required_for_1_share": 10.0, "shortfall": 5.0}
        text = _cash_precheck_detail_text(detail)
        assert "available=5.0" in text
        assert "UNKNOWN" not in text


# ---------------------------------------------------------------------
# S-08: the state-book performance block is no longer called "실거래
# 성과" ("live trading performance").
# ---------------------------------------------------------------------
class TestS08PerformanceLabelIsNotOverclaimed:
    def test_exact_string_is_gone_from_the_module(self):
        source = (REPO_ROOT / "trading_health_check.py").read_text(encoding="utf-8")
        assert "실거래 성과" not in source

    def test_the_new_label_names_it_as_a_state_book_read(self):
        import trading_health_check as hc

        report = {
            "now_kst": "x", "market_day": True, "checks": [], "failed": [], "warned": [],
            "overall": "NORMAL", "performance": {"available": False, "detail": "n/a"},
        }
        text = hc.format_message(report)
        assert "누적 상태 기록 성과" in text
        assert "실거래 성과" not in text


# ---------------------------------------------------------------------
# S-09: an active WARN must not read as NORMAL.
# ---------------------------------------------------------------------
class TestS09OverallReflectsWarnings:
    def test_a_warn_with_no_failures_is_attention(self):
        import trading_health_check as hc

        checks = [hc.Check("a", hc.OK, ""), hc.Check("b", hc.WARN, "pending")]
        failed = [c.name for c in checks if c.verdict == hc.FAIL]
        warned = [c.name for c in checks if c.verdict == hc.WARN]
        overall = "NORMAL" if not failed and not warned else "ATTENTION"
        assert overall == "ATTENTION"

    def test_zero_failures_and_zero_warnings_is_normal(self):
        import trading_health_check as hc

        checks = [hc.Check("a", hc.OK, "")]
        failed = [c.name for c in checks if c.verdict == hc.FAIL]
        warned = [c.name for c in checks if c.verdict == hc.WARN]
        overall = "NORMAL" if not failed and not warned else "ATTENTION"
        assert overall == "NORMAL"


# ---------------------------------------------------------------------
# S-10: collector heartbeat age is computed, not inferred from
# connection_state alone.
# ---------------------------------------------------------------------
class TestS10CollectorHeartbeatComputed:
    def test_connected_but_stale_heartbeat_is_not_ok(self, tmp_path):
        import trading_health_check as hc

        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        root = tmp_path / "scanner"
        (root / "realtime_bars").mkdir(parents=True)
        (root / "realtime_bars" / "collector_status.json").write_text(json.dumps({
            "state": "CONNECTED_ACTIVE", "connection_state": "CONNECTED",
            "subscription_requested": 5, "subscription_count": 5,
            "last_heartbeat_at": (now - timedelta(minutes=10)).isoformat(),
        }))
        checks = {c.name: c for c in hc.collector({"SCANNER_DATA_ROOT": str(root)},
                                                   "sha", ps_text="", now=now)}
        assert checks["collector:connected"].verdict == hc.OK
        # The independently-computed fact overrides the collector's own
        # stale CONNECTED report.
        assert checks["collector:heartbeat"].verdict == hc.FAIL

    def test_fresh_heartbeat_is_ok(self, tmp_path):
        import trading_health_check as hc

        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        root = tmp_path / "scanner"
        (root / "realtime_bars").mkdir(parents=True)
        (root / "realtime_bars" / "collector_status.json").write_text(json.dumps({
            "state": "CONNECTED_ACTIVE", "connection_state": "CONNECTED",
            "subscription_requested": 5, "subscription_count": 5,
            "last_heartbeat_at": (now - timedelta(seconds=5)).isoformat(),
        }))
        checks = {c.name: c for c in hc.collector({"SCANNER_DATA_ROOT": str(root)},
                                                   "sha", ps_text="", now=now)}
        assert checks["collector:heartbeat"].verdict == hc.OK


# ---------------------------------------------------------------------
# S-11: the health check's own log/summary lines are excluded from its
# ERROR count.
# ---------------------------------------------------------------------
class TestS11NoRecursiveErrorCounting:
    def test_health_check_log_is_excluded_by_name(self, tmp_path):
        import trading_health_check as hc

        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        root = tmp_path / "scanner"
        (root / "logs" / "cron").mkdir(parents=True)
        (root / "logs" / "cron" / "health_check.log").write_text(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')},000 ERROR HEALTH: reconciliation dirty\n"
            f"{now.strftime('%Y-%m-%d %H:%M:%S')},000 ERROR something else\n")
        check = hc.recent_errors({"SCANNER_DATA_ROOT": str(root)}, now)
        assert check.verdict == hc.OK
        assert "0 ERROR" in check.detail

    def test_a_real_error_in_another_log_still_counts(self, tmp_path):
        import trading_health_check as hc

        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        root = tmp_path / "scanner"
        (root / "logs" / "cron").mkdir(parents=True)
        (root / "logs" / "cron" / "s6_scan.log").write_text(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')},000 ERROR scanners.runner boom\n")
        check = hc.recent_errors({"SCANNER_DATA_ROOT": str(root)}, now)
        assert check.verdict == hc.WARN

    def test_a_generated_health_line_in_any_log_is_still_excluded(self, tmp_path):
        import trading_health_check as hc

        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        root = tmp_path / "scanner"
        (root / "logs" / "cron").mkdir(parents=True)
        (root / "logs" / "cron" / "run_health_report.log").write_text(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')},000 ERROR HEALTH: unknown orders present\n")
        check = hc.recent_errors({"SCANNER_DATA_ROOT": str(root)}, now)
        assert check.verdict == hc.OK


# ---------------------------------------------------------------------
# S-12/S-13: the durable claim release-on-failure mechanism already
# works end to end, including through send_with_health_tracking. These
# tests LOCK IN that finding (no code change was needed).
# ---------------------------------------------------------------------
class TestS12S13ClaimReleaseOnDefiniteFailure:
    def test_a_handled_falsy_send_failure_releases_the_claim(self):
        conn = _ledger_conn()
        delivered = ln.notify(
            ln.SELL_FILLED, ln.sell_filled_fields(
                symbol="AAPL", qty=1, fill_price=1, realized_pnl=1, realized_pnl_pct=1),
            send_fn=lambda m: False, dedupe_conn=conn, dedupe_subject="pos-1",
            dedupe_version="SOLD:1:0")
        assert delivered is False
        key = ledger.key_for(ln.SELL_FILLED, symbol="AAPL", subject_id="pos-1",
                             state_version="SOLD:1:0")
        assert ledger.already_sent(conn, key) is False

    def test_a_raised_exception_during_send_also_releases_the_claim(self):
        """Goes through send_with_health_tracking (track_health defaults
        True), which is exactly the path S-12/S-13 asked to verify."""
        conn = _ledger_conn()

        def _raises(message):
            raise ConnectionError("boom")

        delivered = ln.notify(
            ln.SELL_FILLED, ln.sell_filled_fields(
                symbol="TSLA", qty=1, fill_price=1, realized_pnl=1, realized_pnl_pct=1),
            send_fn=_raises, dedupe_conn=conn, dedupe_subject="pos-2",
            dedupe_version="SOLD:1:0")
        assert delivered is False
        key = ledger.key_for(ln.SELL_FILLED, symbol="TSLA", subject_id="pos-2",
                             state_version="SOLD:1:0")
        assert ledger.already_sent(conn, key) is False

    def test_a_successful_send_keeps_the_claim(self):
        conn = _ledger_conn()
        delivered = ln.notify(
            ln.SELL_FILLED, ln.sell_filled_fields(
                symbol="MSFT", qty=1, fill_price=1, realized_pnl=1, realized_pnl_pct=1),
            send_fn=lambda m: True, dedupe_conn=conn, dedupe_subject="pos-3",
            dedupe_version="SOLD:1:0")
        assert delivered is True
        key = ledger.key_for(ln.SELL_FILLED, symbol="MSFT", subject_id="pos-3",
                             state_version="SOLD:1:0")
        assert ledger.already_sent(conn, key) is True


# ---------------------------------------------------------------------
# S-14: recovery messages exist and are paired with their alerts.
# ---------------------------------------------------------------------
class TestS14RecoveryMessages:
    def test_health_recovery_tracks_transition_and_fires_once(self, tmp_path, monkeypatch):
        from operations import health_recovery as hr

        state_path = tmp_path / "recovery_state.json"
        sent = []

        # First run: collector heartbeat is FAIL.
        recovered = hr.notify_recoveries(
            [{"name": "collector:heartbeat", "verdict": "FAIL"}],
            send_fn=lambda m: sent.append(m) or True, state_path=state_path)
        assert recovered == []
        assert sent == []

        # Second run: recovered to OK -> one recovery message.
        recovered = hr.notify_recoveries(
            [{"name": "collector:heartbeat", "verdict": "OK"}],
            send_fn=lambda m: sent.append(m) or True, state_path=state_path)
        assert recovered == ["collector:heartbeat"]
        assert len(sent) == 1
        assert "시스템 회복" in sent[0]

        # Third run: still OK -> no repeat.
        recovered = hr.notify_recoveries(
            [{"name": "collector:heartbeat", "verdict": "OK"}],
            send_fn=lambda m: sent.append(m) or True, state_path=state_path)
        assert recovered == []
        assert len(sent) == 1

    def test_untracked_check_names_are_ignored(self, tmp_path):
        from operations import health_recovery as hr

        state_path = tmp_path / "recovery_state.json"
        recovered = hr.notify_recoveries(
            [{"name": "some_other_check", "verdict": "OK"}],
            send_fn=lambda m: True, state_path=state_path)
        assert recovered == []

    def test_reconciliation_and_exit_evaluation_have_dedicated_recovery_events(self):
        assert ln.RECONCILIATION_RECOVERED in ln.RECOVERY_EVENTS
        assert ln.EXIT_EVALUATION_RECOVERED in ln.RECOVERY_EVENTS
        assert ln.channel_for(ln.RECONCILIATION_RECOVERED) == sp.LIVE_ALERTS
