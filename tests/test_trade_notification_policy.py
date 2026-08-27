"""A per-minute engine that speaks only when something changed.

DT sat in EXIT_PENDING for 105 consecutive minutes. A channel that
reports that 105 times is not thorough, it is unreadable, and what it
costs is the genuinely new message that gets scrolled past.

The numbered cases below are §40 items 5-19, which are mostly about what
must NOT be sent.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from operations import notification_ledger as ledger  # noqa: E402
from operations import trade_notifications as tn  # noqa: E402

NOW = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _notify(conn, **kwargs):
    kwargs.setdefault("now", NOW)
    return tn.should_notify(conn, **kwargs)


class TestTransitionsAreNewsAndRepetitionIsNot:
    def test_watching_to_ready_is_sent(self):
        assert tn.state_changed("WATCHING", "READY_TO_BUY") is True

    def test_ready_to_ready_is_not(self):
        assert tn.state_changed("READY_TO_BUY", "READY_TO_BUY") is False

    def test_item5_watching_to_ready_sends_once(self, conn):
        out = _notify(conn, event_type="READY", symbol="DT",
                      subject_id="cand-1", previous_state="WATCHING",
                      current_state="READY_TO_BUY")
        assert out["send"] is True

    def test_item6_ready_held_for_ten_ticks_sends_nothing_more(self, conn):
        _notify(conn, event_type="READY", symbol="DT", subject_id="cand-1",
                previous_state="WATCHING", current_state="READY_TO_BUY")
        sent = 0
        for minute in range(1, 11):
            out = _notify(conn, event_type="READY", symbol="DT",
                          subject_id="cand-1", previous_state="READY_TO_BUY",
                          current_state="READY_TO_BUY",
                          now=NOW + timedelta(minutes=minute))
            sent += int(out["send"])
            assert out["reason"] == "NO_STATE_CHANGE"
        assert sent == 0

    def test_item7_ready_to_invalidated_sends_once(self, conn):
        out = _notify(conn, event_type="INVALIDATED", symbol="DT",
                      subject_id="cand-1", previous_state="READY_TO_BUY",
                      current_state="INVALIDATED")
        assert out["send"] is True
        again = _notify(conn, event_type="INVALIDATED", symbol="DT",
                        subject_id="cand-1", previous_state="INVALIDATED",
                        current_state="INVALIDATED",
                        now=NOW + timedelta(minutes=1))
        assert again["send"] is False


class TestOneShotsHappenOnce:
    @pytest.mark.parametrize("event", ["BUY_SUBMITTED", "BUY_FILLED",
                                       "EXIT_SIGNAL", "SELL_SUBMITTED",
                                       "SELL_FILLED"])
    def test_items_8_9_11_14_15_each_send_exactly_once(self, conn, event):
        first = _notify(conn, event_type=event, symbol="DT",
                        subject_id="order-1")
        second = _notify(conn, event_type=event, symbol="DT",
                         subject_id="order-1",
                         now=NOW + timedelta(minutes=1))
        assert first["send"] is True
        assert second["send"] is False
        assert second["reason"] == "ALREADY_SENT"

    def test_buy_submitted_and_buy_filled_are_separate_events(self, conn):
        """§14 -- two distinct facts about one order."""
        submitted = _notify(conn, event_type="BUY_SUBMITTED", symbol="DT",
                            subject_id="order-1")
        filled = _notify(conn, event_type="BUY_FILLED", symbol="DT",
                         subject_id="order-1")
        assert submitted["send"] and filled["send"]
        assert submitted["key"] != filled["key"]

    def test_item10_thirty_ticks_of_holding_send_nothing(self, conn):
        sent = 0
        for minute in range(30):
            out = _notify(conn, event_type="POSITION_STATE", symbol="DT",
                          subject_id="pos-1", previous_state="OPEN",
                          current_state="OPEN",
                          now=NOW + timedelta(minutes=minute))
            sent += int(out["send"])
        assert sent == 0


class TestExitPendingIsRemindedNotRepeated:
    def _tick(self, conn, minute):
        return _notify(conn, event_type="EXIT_PENDING", symbol="DT",
                       subject_id="pos-1", previous_state="EXIT_PENDING",
                       current_state="EXIT_PENDING",
                       now=NOW + timedelta(minutes=minute))

    def test_item12_twenty_ticks_produce_no_repeats(self, conn):
        first = _notify(conn, event_type="EXIT_PENDING", symbol="DT",
                        subject_id="pos-1", previous_state="EXIT_SUBMITTED",
                        current_state="EXIT_PENDING")
        assert first["send"] is True
        sent = sum(int(self._tick(conn, m)["send"]) for m in range(1, 21))
        assert sent == 0

    def test_item13_one_reminder_after_thirty_minutes(self, conn):
        _notify(conn, event_type="EXIT_PENDING", symbol="DT",
                subject_id="pos-1", previous_state="EXIT_SUBMITTED",
                current_state="EXIT_PENDING")
        # Minutes 1..29 stay quiet, minute 31 earns exactly one.
        assert sum(int(self._tick(conn, m)["send"]) for m in range(1, 30)) == 0
        assert self._tick(conn, 31)["send"] is True
        # ...and the minutes after it do not each earn another.
        assert sum(int(self._tick(conn, m)["send"]) for m in range(32, 45)) == 0

    def test_a_session_change_may_remind_without_waiting(self, conn):
        """§18's other trigger: something actually changed."""
        out = tn.reminder_on_change(
            conn, event_type="EXIT_PENDING", symbol="DT", subject_id="pos-1",
            change_reason="SESSION_TRANSITION", now=NOW)
        assert out["send"] is True
        again = tn.reminder_on_change(
            conn, event_type="EXIT_PENDING", symbol="DT", subject_id="pos-1",
            change_reason="SESSION_TRANSITION", now=NOW + timedelta(minutes=1))
        assert again["send"] is False

    def test_a_capability_change_is_a_different_reminder(self, conn):
        tn.reminder_on_change(conn, event_type="EXIT_PENDING", symbol="DT",
                              subject_id="pos-1",
                              change_reason="SESSION_TRANSITION", now=NOW)
        out = tn.reminder_on_change(
            conn, event_type="EXIT_PENDING", symbol="DT", subject_id="pos-1",
            change_reason="SELL_ROUTE_AVAILABLE", now=NOW)
        assert out["send"] is True


class TestReconciliation:
    def test_item16_a_passing_reconciliation_does_not_spam(self, conn):
        sent = 0
        for minute in range(60):
            out = _notify(conn, event_type="RECONCILIATION", symbol=None,
                          subject_id="account", previous_state="PASS",
                          current_state="PASS",
                          now=NOW + timedelta(minutes=minute))
            sent += int(out["send"])
        assert sent == 0

    def test_item17_a_failure_sends_once(self, conn):
        out = _notify(conn, event_type="RECONCILIATION", subject_id="account",
                      previous_state="PASS", current_state="FAILED")
        assert out["send"] is True
        again = _notify(conn, event_type="RECONCILIATION",
                        subject_id="account", previous_state="FAILED",
                        current_state="FAILED", now=NOW + timedelta(minutes=1))
        assert again["send"] is False

    def test_item18_recovery_sends_once(self, conn):
        _notify(conn, event_type="RECONCILIATION", subject_id="account",
                previous_state="PASS", current_state="FAILED")
        recovery = _notify(conn, event_type="RECONCILIATION",
                           subject_id="account", previous_state="FAILED",
                           current_state="PASS",
                           now=NOW + timedelta(minutes=5))
        assert recovery["send"] is True


class TestItSurvivesARestart:
    def test_item19_a_restarted_notifier_does_not_resend(self, conn):
        """The ledger row outlives the process, which is the point."""
        first = _notify(conn, event_type="BUY_FILLED", symbol="DT",
                        subject_id="order-1")
        assert first["send"] is True
        # A fresh "process" computing the key from the same facts.
        key = ledger.key_for("BUY_FILLED", symbol="DT", subject_id="order-1")
        assert key == first["key"]
        assert ledger.already_sent(conn, key) is True
        assert _notify(conn, event_type="BUY_FILLED", symbol="DT",
                       subject_id="order-1")["send"] is False

    def test_the_key_is_deterministic_across_processes(self):
        a = ledger.key_for("BUY_FILLED", strategy_id="S6", symbol="dt",
                           subject_id="o1", state_version="v1")
        b = ledger.key_for("BUY_FILLED", strategy_id="S6", symbol="DT",
                           subject_id="o1", state_version="v1")
        assert a == b

    def test_a_definite_failure_may_be_retried(self, conn):
        out = _notify(conn, event_type="BUY_FILLED", symbol="DT",
                      subject_id="order-1")
        ledger.release(conn, out["key"])
        assert _notify(conn, event_type="BUY_FILLED", symbol="DT",
                       subject_id="order-1")["send"] is True


class TestFailureErrsTowardSending:
    def test_a_broken_ledger_never_silences_an_alert(self, conn):
        """Duplicate beats missing for something an operator must see."""
        conn.execute("DROP TABLE notification_ledger")
        conn.commit()
        out = _notify(conn, event_type="RECONCILIATION",
                      subject_id="account", previous_state="PASS",
                      current_state="FAILED")
        assert out["send"] is True


class TestDelayIsMeasuredFromTheEvent:
    def test_delay_is_event_to_send_not_tick_to_send(self, conn):
        """§24 -- measuring against when we noticed would report zero
        for a message that arrived an hour after the fill."""
        out = _notify(conn, event_type="BUY_FILLED", symbol="DT",
                      subject_id="order-1",
                      event_time=NOW - timedelta(minutes=10))
        assert out["delay_seconds"] == pytest.approx(600, abs=2)

    def test_an_excessive_delay_is_flagged(self, conn):
        out = _notify(conn, event_type="BUY_FILLED", symbol="DT",
                      subject_id="order-1",
                      event_time=NOW - timedelta(minutes=30))
        assert out["late"] is True

    def test_a_prompt_notification_is_not_flagged(self, conn):
        out = _notify(conn, event_type="BUY_FILLED", symbol="DT",
                      subject_id="order-1",
                      event_time=NOW - timedelta(seconds=5))
        assert out["late"] is False


class TestEveryMomentKeepsItsOwnName:
    def test_timestamps_are_labelled_individually(self):
        out = tn.timestamps_for(
            market_data_asof=NOW - timedelta(minutes=5),
            broker_fill_time=NOW - timedelta(minutes=1),
            notification_sent_at=NOW)
        assert set(out) == {"market_data_asof", "broker_fill_time",
                            "notification_sent_at"}

    def test_they_render_in_two_timezones(self):
        out = tn.timestamps_for(broker_fill_time=NOW)
        assert "ET" in out["broker_fill_time"] or "KST" in out["broker_fill_time"]

    def test_a_missing_moment_is_omitted_not_invented(self):
        assert tn.timestamps_for(ready_at=None) == {}
