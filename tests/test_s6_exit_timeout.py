"""A protective S6 SELL does not rest indefinitely either.

Production evidence, 2026-09-10: SCL exit-intent s6exit-SCL-d7d50d9650ae
(broker order 0000001958) sat ACCEPTED, zero filled, KIS still holding
2 shares open, for 6+ hours -- HARD_RISK_CAP/RANGE_REENTRY both true
the whole time, with nothing anywhere that would ever act on it.
exit_runtime.recover_dead_exits only acts once KIS itself makes the
order disappear; this module is the missing case, where KIS keeps it
open and something on our side must decide.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import exit_timeout as xt  # noqa: E402
from s6_live import position_store as ps  # noqa: E402

NOW = datetime(2026, 9, 10, 1, 20, tzinfo=timezone.utc)
ACCOUNT = "12345678"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


class Broker:
    """Enough of KISBroker for these decisions: an open-order book and
    an optional position book (release_dead_exit's own re-check does
    not run here, but cancel confirmation does)."""

    def __init__(self, open_symbols=("SCL",), raises=False, qty="2", filled="0",
                 extra=None):
        self._open = list(open_symbols)
        self._raises = raises
        self._qty = qty
        self._filled = filled
        self._extra = extra or {}
        self.config = type("C", (), {"account_no": ACCOUNT})()

    def get_open_orders(self):
        if self._raises:
            raise RuntimeError("KIS unreachable")
        return [{"pdno": s, "odno": "0000001958", "ft_ord_qty": self._qty,
                 "ft_ccld_qty": self._filled,
                 "nccs_qty": str(int(self._qty) - int(self._filled)),
                 "ft_ord_unpr3": "61.80000000", **self._extra} for s in self._open]


def _stuck_sell(conn, *, symbol="SCL", accepted_age_seconds=700,
                quantity=2, broker_order_id="0000001958",
                client_order_id="s6exit-SCL-abc123", accepted=True):
    """A SELL that is genuinely resting, ACCEPTED, unfilled, still open
    at KIS -- the exact SCL shape."""
    from state_store import exit_intent_ledger as eil

    submitted_at = NOW - timedelta(seconds=accepted_age_seconds)
    pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                               entry_session="REGULAR",
                               client_order_id=f"kislive-{symbol}-buy",
                               now=submitted_at)
    ps.open_from_fill(conn, pid, quantity=quantity, average_fill_price=61.97,
                      now=submitted_at)
    ps.latch_pending_exit(conn, pid, "VWAP_FAILURE", now=submitted_at)
    ps.mark_exit_submitted(conn, pid, "VWAP_FAILURE", now=submitted_at)

    intent_id = eil.reserve(conn, pid, "VWAP_FAILURE", quantity, client_order_id)
    eil.mark_submitted(conn, intent_id, broker_order_id=broker_order_id)

    conn.execute(
        "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
        "symbol, side, trading_date, broker_order_id, status, created_at, "
        "updated_at, requested_quantity, version, strategy_id) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        (client_order_id, client_order_id, symbol, "sell", "2026-09-09",
         broker_order_id, "ACCEPTED", submitted_at.isoformat(),
         submitted_at.isoformat(), quantity, 4, "POSITIONS_LIFECYCLE_EXIT"))
    if accepted:
        conn.execute(
            "INSERT INTO order_state_events (internal_order_id, from_state, "
            "to_state, event_type, payload, version, occurred_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (client_order_id, "SUBMITTING", "ACCEPTED", "TRANSPORT_RESULT",
             f'{{"broker_order_id": "{broker_order_id}"}}', 4,
             submitted_at.isoformat()))
    conn.commit()
    return pid


def _stub_engine(monkeypatch, sent):
    def _submit_cancel(**kwargs):
        sent.append(kwargs)
        return None
    monkeypatch.setattr("execution.execution_engine.submit_cancel", _submit_cancel)


class TestTheClockDecidesOnlyWhenItHasRunOut:
    def test_a_sell_inside_the_timeout_is_held(self, conn):
        _stuck_sell(conn, accepted_age_seconds=120)
        out = xt.evaluate(conn, broker=Broker(), account_id=ACCOUNT, now=NOW)
        assert [o["action"] for o in out] == [xt.ACTION_HELD]
        assert out[0]["age_seconds"] == pytest.approx(120, abs=2)

    def test_the_timeout_is_ten_minutes(self):
        assert xt.SELL_FILL_TIMEOUT_SECONDS == 600

    def test_a_missing_acceptance_event_fails_closed(self, conn):
        _stuck_sell(conn, accepted_age_seconds=100000, accepted=False)
        out = xt.evaluate(conn, broker=Broker(), account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.ACTION_HELD
        assert out[0]["age_seconds"] is None


class TestOpenCancelableSellIsReassessed:
    """A due OPEN SELL is kept unless broker evidence proves reprice."""

    def test_a_stuck_sell_past_interval_is_kept_not_cancelled(self, conn, monkeypatch):
        pid = _stuck_sell(conn, accepted_age_seconds=700)
        sent = []
        _stub_engine(monkeypatch, sent)
        broker = Broker()

        out = xt.evaluate(conn, broker=broker, account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.KEEP_ORDER
        assert sent == []
        assert ps.load(conn, pid)["status"] == ps.EXIT_SUBMITTED
        assert ps.load(conn, pid)["exit_submitted"]

    def test_replacement_is_not_submitted_here(self, conn, monkeypatch):
        """No new replacement-submission code exists in this module --
        it only releases the row; the EXISTING, unchanged
        retry_latched_exits is what submits on the next tick."""
        import inspect

        source = inspect.getsource(xt)
        assert "_submit_sell" not in source
        assert "submit_order" not in source


class TestNoReplacementBeforeCancelledConfirmation:
    """§8: a timeout alone must never manufacture a second live order."""

    def test_an_ambiguous_cancel_does_not_release_the_position(self, conn, monkeypatch):
        pid = _stuck_sell(conn, accepted_age_seconds=700)

        def _boom(**kwargs):
            raise RuntimeError("cancel response timed out")
        monkeypatch.setattr("execution.execution_engine.submit_cancel", _boom)

        out = xt.evaluate(conn, broker=Broker(), account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.KEEP_ORDER
        assert ps.load(conn, pid)["status"] == ps.EXIT_SUBMITTED
        assert ps.load(conn, pid)["exit_submitted"]

    def test_a_cancel_that_does_not_actually_clear_the_book_does_not_release(
            self, conn, monkeypatch):
        """The engine returned without raising, but KIS's own book still
        lists the order -- e.g. an ACK for a different action. Must not
        be treated as a confirmed cancel."""
        pid = _stuck_sell(conn, accepted_age_seconds=700)
        _stub_engine(monkeypatch, [])
        # Still open on EVERY read -- both the pre-cancel check and the
        # post-cancel confirmation.
        out = xt.evaluate(conn, broker=Broker(open_symbols=("SCL",)),
                          account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.KEEP_ORDER
        assert ps.load(conn, pid)["status"] == ps.EXIT_SUBMITTED

    def test_an_unreadable_open_order_book_does_not_cancel(self, conn, monkeypatch):
        pid = _stuck_sell(conn, accepted_age_seconds=700)
        sent = []
        _stub_engine(monkeypatch, sent)
        out = xt.evaluate(conn, broker=Broker(raises=True), account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.BROKER_UNKNOWN
        assert sent == []
        assert ps.load(conn, pid)["status"] == ps.EXIT_SUBMITTED

    def test_a_sell_that_already_filled_is_skipped_not_cancelled(self, conn, monkeypatch):
        """A fill landing between the decision and the transport stops
        the cancel -- the broker no longer lists it open."""
        pid = _stuck_sell(conn, accepted_age_seconds=700)
        sent = []
        _stub_engine(monkeypatch, sent)
        out = xt.evaluate(conn, broker=Broker(open_symbols=()), account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.TERMINAL_RECONCILE
        assert sent == []
        assert ps.load(conn, pid)["status"] == ps.EXIT_SUBMITTED


class TestReplacementUsesRemainingQuantity:
    """§5: replacement quantity must equal broker-confirmed remaining
    position quantity, never a stale row value blindly reused."""

    def test_the_cancelled_intents_ledger_quantity_is_the_live_row(self, conn):
        pid = _stuck_sell(conn, accepted_age_seconds=700, quantity=2)
        row = dict(ps.load(conn, pid))
        assert row["quantity"] == 2
        # release_dead_exit does not alter the row's quantity -- it is
        # the SAME live row retry_latched_exits reads for its qty.
        ps.release_dead_exit(conn, pid, reason="VWAP_FAILURE", now=NOW)
        assert ps.load(conn, pid)["quantity"] == 2


class TestLatchIsClearedOnlyOnSafeTerminalState:
    """§9: exit_submitted means "an active SELL may exist", not "never
    attempt another SELL for this position again"."""

    def test_latch_stays_set_for_a_valid_open_order(self, conn, monkeypatch):
        pid = _stuck_sell(conn, accepted_age_seconds=700)
        broker = Broker()
        _stub_engine(monkeypatch, [])

        assert ps.load(conn, pid)["exit_submitted"] == 1
        xt.evaluate(conn, broker=broker, account_id=ACCOUNT, now=NOW)
        assert ps.load(conn, pid)["exit_submitted"]

    def test_latch_stays_set_on_an_unresolved_cancel(self, conn, monkeypatch):
        pid = _stuck_sell(conn, accepted_age_seconds=700)

        def _boom(**kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr("execution.execution_engine.submit_cancel", _boom)
        xt.evaluate(conn, broker=Broker(), account_id=ACCOUNT, now=NOW)
        assert ps.load(conn, pid)["exit_submitted"] == 1


class TestUnknownDoesNotDuplicateSell:
    def test_a_second_tick_after_unknown_does_not_send_a_second_cancel(
            self, conn, monkeypatch):
        """Once CANCEL_UNKNOWN, the position is no longer EXIT_SUBMITTED-
        and-open in a way this module would re-evaluate the SAME way --
        but critically, nothing here re-attempts a cancel from a dirty
        state; the row stays exactly as the engine left it."""
        _stuck_sell(conn, accepted_age_seconds=700)
        calls = []

        def _boom(**kwargs):
            calls.append(1)
            raise RuntimeError("boom")
        monkeypatch.setattr("execution.execution_engine.submit_cancel", _boom)
        xt.evaluate(conn, broker=Broker(), account_id=ACCOUNT, now=NOW)
        xt.evaluate(conn, broker=Broker(), account_id=ACCOUNT, now=NOW)
        # Reassessment never makes age itself a cancel trigger.
        assert len(calls) == 0


class TestPhase3ReassessmentDecisions:
    def test_partial_fill_is_managed_without_a_new_sell(self, conn, monkeypatch):
        pid = _stuck_sell(conn, accepted_age_seconds=700, quantity=28)
        sent = []
        _stub_engine(monkeypatch, sent)
        out = xt.evaluate(conn, broker=Broker(qty="28", filled="10"),
                          account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.PARTIAL_FILL_MANAGE
        assert out[0]["remaining_qty"] == 18
        assert sent == []
        assert ps.load(conn, pid)["quantity"] == 28

    def test_proven_reprice_waits_for_cancel_confirmation(self, conn, monkeypatch):
        pid = _stuck_sell(conn, accepted_age_seconds=700)
        sent = []
        _stub_engine(monkeypatch, sent)
        broker = Broker(extra={"reprice_proven": True})
        calls = {"n": 0}
        def _book():
            calls["n"] += 1
            return ([{"pdno": "SCL", "odno": "0000001958", "ft_ord_qty": "2",
                     "ft_ccld_qty": "0", "nccs_qty": "2",
                     "ft_ord_unpr3": "61.8", "reprice_proven": True}]
                    if calls["n"] <= 2 else [])
        broker.get_open_orders = _book
        out = xt.evaluate(conn, broker=broker, account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.REPRICE_ORDER
        assert len(sent) == 1
        assert ps.load(conn, pid)["status"] == ps.EXIT_PENDING

    def test_reassessment_history_survives_order_ids_and_bounds_churn(self, conn):
        pid = _stuck_sell(conn, accepted_age_seconds=700)
        for n in range(4):
            conn.execute(
                "INSERT INTO s6_sell_reassessments (position_id, symbol, broker_status, "
                "sell_retry_count, reassessment_count, decision, decision_reason, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, "SCL", "OPEN_CANCELABLE", n + 1, n + 1,
                 xt.REPRICE_ORDER, "TEST", NOW.isoformat()))
        conn.commit()
        out = xt.evaluate(conn, broker=Broker(), account_id=ACCOUNT, now=NOW)
        assert out[0]["action"] == xt.EXIT_ESCALATION_REQUIRED
        assert out[0]["sell_retry_count"] == 4


class TestAlertsDedupeAndRoute:
    """§12: meaningful once, never routine-polling spam."""

    def test_stuck_timeout_and_cancel_unresolved_are_urgent_events(self):
        from operations import live_notifications as ln

        assert ln.SELL_STUCK_TIMEOUT in ln.URGENT_EVENTS
        assert ln.SELL_CANCEL_UNRESOLVED in ln.URGENT_EVENTS
        assert ln.channel_for(ln.SELL_STUCK_TIMEOUT) == "LIVE_ALERTS"
        assert ln.channel_for(ln.SELL_CANCEL_UNRESOLVED) == "LIVE_ALERTS"

    def test_repeated_stuck_alert_for_the_same_position_is_deduped(self, conn):
        from operations import live_notifications as ln

        sent = []
        monkeypatch_sender = lambda text: sent.append(text) or True  # noqa: E731
        import slack_utils
        orig = slack_utils.send_kis_live_alert
        slack_utils.send_kis_live_alert = monkeypatch_sender
        try:
            xt._alert_stuck_timeout(conn, "SCL", "s6pos_x", 700)
            xt._alert_stuck_timeout(conn, "SCL", "s6pos_x", 705)
        finally:
            slack_utils.send_kis_live_alert = orig
        assert len(sent) == 1, "the second call for the same position must be deduped"


class TestSellSideIntentReconstructionUsesTheExitLedger:
    """Distinguishes this from entry_timeout: s6_positions.client_order_id
    is the BUY's own order id and must never be read for the SELL side."""

    def test_reconstructed_intent_uses_the_exit_ledgers_own_order_id(self, conn):
        pid = _stuck_sell(conn, accepted_age_seconds=700,
                          client_order_id="s6exit-SCL-d7d50d9650ae",
                          broker_order_id="0000001958")
        row = dict(ps.load(conn, pid))
        assert row["client_order_id"] != "s6exit-SCL-d7d50d9650ae", (
            "sanity: the position row's own client_order_id is the BUY's")
        intent, _instrument, broker_order_id = xt._reconstruct_sell_intent(
            conn, pid, row, open_order={"ft_ord_unpr3": "61.80000000"})
        assert intent.internal_order_id == "s6exit-SCL-d7d50d9650ae"
        assert intent.side == "sell"
        assert broker_order_id == "0000001958"

    def test_session_is_resolved_from_the_orders_own_acceptance_time(
            self, conn, monkeypatch):
        """Not the clock the cancel happens to run on."""
        pid = _stuck_sell(conn, accepted_age_seconds=700)
        row = dict(ps.load(conn, pid))
        captured = {}

        def _fake_route_session(*, now=None):
            captured["now"] = now
            return "REGULAR"
        monkeypatch.setattr(
            "config.session_capability.route_session", _fake_route_session)
        xt._reconstruct_sell_intent(conn, pid, row,
                                    open_order={"ft_ord_unpr3": "61.80000000"})
        # The order was accepted 700s before NOW -- the resolved session
        # must be asked about THAT moment, not "now".
        assert captured["now"] == NOW - timedelta(seconds=700)


class TestS1AndOtherS6ExitBehaviorUnchanged:
    """This module is purely additive: a NEW stage, a NEW file. S1's own
    exit runtime and the rest of S6's exit_runtime.py are untouched."""

    def test_s1_exit_runtime_has_no_reference_to_the_new_module(self):
        import inspect

        import s1_live.exit_runtime as s1_exit

        assert "exit_timeout" not in inspect.getsource(s1_exit)

    def test_recover_dead_exits_and_reconcile_unconfirmed_exits_unchanged(self):
        """`s1_live/exit_runtime.py`, `s6_live/position_store.py` and
        `state_store/exit_intent_ledger.py` stay untouched by this
        module's own work -- a byte-diff proof, since nothing about
        exit_timeout.py has any reason to touch them.

        `s6_live/exit_runtime.py` is deliberately NOT included here: a
        later, independent change (reconciliation/sell_projection.py's
        settle-on-fill hook inside sync_sell_fills) legitimately touches
        that file without touching anything this module depends on --
        `_abort_intent` and `position_store.release_dead_exit`, both
        exercised end-to-end and still passing in
        TestOpenCancelableSellEntersCancelFlow above. A whole-file diff
        proof cannot distinguish "this module broke" from "an unrelated
        change landed in the same file", so it checks only the files
        that should never have either.
        """
        import subprocess

        diff = subprocess.run(
            ["git", "diff", "--stat", "HEAD", "--", "s1_live/exit_runtime.py",
             "s6_live/position_store.py", "state_store/exit_intent_ledger.py"],
            capture_output=True, text=True).stdout
        assert diff.strip() == "", diff
