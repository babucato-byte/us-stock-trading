"""Resolving exit intents whose submission was never confirmed.

The jam
-------
When a SELL submission comes back ambiguously, the exit path marks the
intent SUBMISSION_UNKNOWN, re-latches the position and refuses to retry.
That is right: re-sending an order that may already be live is how a
position gets sold twice. The comment says "reconciliation decides".

Reconciliation never decided -- nothing looked at these rows at all. So
`exitintent_8ec53e6d5a764b22` sat SUBMISSION_UNKNOWN from 2026-08-28
19:54, `reserve()` refused every later exit for RIG, and 3 shares could
not be sold for the rest of the weekend. Every reconciliation pass in
between reported "clean", correctly by its own measure: internal and
broker agreed on the position and no orders were open. The disagreement
was in a ledger nobody read.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reconciliation import exit_intent_resolution as res  # noqa: E402
from s6_live import position_store as ps  # noqa: E402
from state_store import exit_intent_ledger as ledger  # noqa: E402

NOW = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
CLIENT_ID = "s6exit-RIG-d8bd8b42eb88"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _rig(conn, quantity=3):
    pid = ps.record_submission(conn, symbol="RIG", variant="S6-R",
                               entry_session="REGULAR",
                               client_order_id="kislive-RIG-1", now=NOW)
    ps.open_from_fill(conn, pid, quantity=quantity, average_fill_price=5.85,
                      venue="NYSE", now=NOW)
    ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
    return pid


def _stuck_intent(conn, pid, quantity=3):
    intent_id = ledger.reserve(conn, pid, "EMA_STRUCTURE_FAILURE", quantity,
                               CLIENT_ID)
    ledger.mark_submission_unknown(conn, intent_id)
    return intent_id


class _Broker:
    def __init__(self, open_orders=(), fills=()):
        self._open = list(open_orders)
        self._fills = list(fills)

    def get_open_orders(self):
        return self._open

    def get_fills(self, *, start_date=None, end_date=None):
        # The real signature. The first version of this fake took no
        # arguments, which hid that the resolver was calling `get_fills()`
        # bare -- it raised in production and the intent stayed stuck.
        return self._fills


class TestTheRIGCase:
    def test_no_trace_at_the_broker_and_shares_intact_aborts_the_intent(
            self, conn):
        """The submission never reached KIS, so the intent is released
        and the normal runtime can exit the position again."""
        pid = _rig(conn)
        intent_id = _stuck_intent(conn, pid)
        out = res.resolve_unknown_exit_intents(conn, _Broker(), now=NOW)
        assert out[0]["resolved"] is True
        assert out[0]["reason"] == res.RESOLVED_NEVER_SENT
        assert ledger.get_by_id(conn, intent_id)["state"] == ledger.STATE_ABORTED

    def test_the_position_can_be_exited_again_afterwards(self, conn):
        """The point of the whole exercise."""
        pid = _rig(conn)
        _stuck_intent(conn, pid)
        assert ledger.get_active_intent(conn, pid) is not None
        res.resolve_unknown_exit_intents(conn, _Broker(), now=NOW)
        assert ledger.get_active_intent(conn, pid) is None

    def test_it_does_not_touch_the_position_or_its_reason(self, conn):
        """Releasing the intent is not selling, and not relabelling."""
        pid = _rig(conn)
        _stuck_intent(conn, pid)
        res.resolve_unknown_exit_intents(conn, _Broker(), now=NOW)
        row = ps.load(conn, pid)
        assert row["status"] == ps.EXIT_PENDING
        assert row["pending_exit_reason"] == "EMA_STRUCTURE_FAILURE"
        assert row["quantity"] == 3


class TestPositiveEvidenceIsRequired:
    def test_an_order_found_open_at_the_broker_is_marked_submitted(self, conn):
        """It landed after all; the settlement path takes it from here,
        and nothing re-sends it."""
        pid = _rig(conn)
        intent_id = _stuck_intent(conn, pid)
        broker = _Broker(open_orders=[{"client_order_id": CLIENT_ID}])
        out = res.resolve_unknown_exit_intents(conn, broker, now=NOW)
        assert out[0]["reason"] == res.RESOLVED_LANDED
        assert ledger.get_by_id(conn, intent_id)["state"] != ledger.STATE_ABORTED

    def test_a_fill_found_at_the_broker_is_not_aborted(self, conn):
        """Aborting here would free the position for a SECOND sell of
        shares that are already gone."""
        pid = _rig(conn)
        intent_id = _stuck_intent(conn, pid)
        broker = _Broker(fills=[{"ODNO": CLIENT_ID}])
        out = res.resolve_unknown_exit_intents(conn, broker, now=NOW)
        assert out[0]["reason"] == res.RESOLVED_FILLED
        assert ledger.get_by_id(conn, intent_id)["state"] != ledger.STATE_ABORTED

    def test_a_partially_held_position_stays_unknown(self, conn):
        """Shares are missing, so the order we cannot find may well have
        sold them. Guessing re-sends a live order."""
        pid = _rig(conn, quantity=3)
        _stuck_intent(conn, pid, quantity=5)
        out = res.resolve_unknown_exit_intents(conn, _Broker(), now=NOW)
        assert out[0]["resolved"] is False
        assert out[0]["reason"] == res.UNRESOLVED_AMBIGUOUS

    def test_an_unreadable_broker_resolves_nothing(self, conn):
        class _Broken:
            def get_open_orders(self):
                raise RuntimeError("KIS unreachable")

            def get_fills(self, *, start_date=None, end_date=None):
                raise RuntimeError("KIS unreachable")

        pid = _rig(conn)
        intent_id = _stuck_intent(conn, pid)
        out = res.resolve_unknown_exit_intents(conn, _Broken(), now=NOW)
        assert out[0]["resolved"] is False
        assert out[0]["reason"] == res.UNRESOLVED_UNREADABLE
        assert ledger.get_by_id(conn, intent_id)["state"] \
            == ledger.STATE_SUBMISSION_UNKNOWN


class TestItLeavesEverythingElseAlone:
    def test_a_confirmed_intent_is_not_reopened(self, conn):
        pid = _rig(conn)
        intent_id = ledger.reserve(conn, pid, "EMA_STRUCTURE_FAILURE", 3,
                                   CLIENT_ID)
        ledger.mark_submitted(conn, intent_id)
        ledger.mark_confirmed(conn, intent_id, 3)
        assert res.resolve_unknown_exit_intents(conn, _Broker(), now=NOW) == []

    def test_no_intents_at_all_is_not_an_error(self, conn):
        assert res.resolve_unknown_exit_intents(conn, _Broker(), now=NOW) == []

    def test_it_places_no_orders(self):
        source = (REPO_ROOT / "reconciliation"
                  / "exit_intent_resolution.py").read_text()
        for forbidden in ("submit_order", "submit_sell", "submit_buy",
                          "cancel_order", "close_position",
                          "latch_pending_exit"):
            assert forbidden not in source, forbidden

    def test_it_writes_through_the_ledgers_own_transitions(self):
        """§14 forbids a manual status overwrite; the state moves through
        the ledger's API so the change is auditable."""
        source = (REPO_ROOT / "reconciliation"
                  / "exit_intent_resolution.py").read_text()
        assert "UPDATE exit_intents" not in source
        assert "mark_aborted" in source


class TestReconciliationReportsIt:
    def test_a_stuck_intent_is_counted_separately_from_orders(self):
        """Folding it into a total would let "clean" keep being printed
        over a jammed exit -- which is what happened for three days."""
        source = (REPO_ROOT / "scripts" / "run_reconciliation.py").read_text()
        assert "exit_intents_stuck" in source
        assert "exit_intents_resolved" in source

    def test_the_pass_calls_the_resolver(self):
        source = (REPO_ROOT / "scripts" / "run_reconciliation.py").read_text()
        assert "resolve_unknown_exit_intents" in source


class TestItAsksTheBrokerTheWayTheBrokerExpects:
    """The first version called `broker.get_fills()` bare. `get_fills`
    requires start_date and end_date, so it raised on every real pass:
    the intent stayed stuck with BROKER_UNREADABLE -- failing closed,
    which is correct and completely useless. A fake with the wrong
    signature hid it, so the signature is now checked against the real
    broker."""

    def test_the_resolver_reads_fills_through_the_shared_window(self):
        source = (REPO_ROOT / "reconciliation"
                  / "exit_intent_resolution.py").read_text()
        assert "fill_window.read_fills" in source
        assert "broker.get_fills()" not in source

    def test_the_real_broker_requires_a_date_window(self):
        import inspect

        from brokers.kis_broker import KISBroker

        parameters = inspect.signature(KISBroker.get_fills).parameters
        assert "start_date" in parameters
        assert "end_date" in parameters
        assert parameters["start_date"].kind == inspect.Parameter.KEYWORD_ONLY

    def test_both_reconciliation_readers_use_the_same_window(self):
        """So the lookback cannot drift back to today-only in one of
        them and silently stop finding older fills."""
        source = (REPO_ROOT / "scripts" / "run_reconciliation.py").read_text()
        resolver = (REPO_ROOT / "reconciliation"
                    / "exit_intent_resolution.py").read_text()
        assert "fill_window.read_fills" in source
        assert "fill_window.read_fills" in resolver
