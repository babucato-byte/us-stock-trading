"""The SELL takes the execution lock; the evaluation does not.

Evaluating an exit reads bars and computes indicators and mutates
nothing. Submitting one mutates the broker, the intent ledger and the
position row, and that is what must not interleave with a BUY.

Holding the broker lock across the whole evaluation is what starved the
one-minute monitor: measured 2026-09-15, a 215.9s median evaluation
skipped 253 of 318 ticks and left 2280s between evaluations at worst.
"""

import threading
from pathlib import Path

import pytest

from execution import execution_lock
from execution.execution_lock import ExecutionLockUnavailable
from s6_live import exit_runtime, position_store

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Response:
    status_code = 200
    text = "accepted"

    def json(self):
        return {"output": {"ODNO": "0000000001"}}


class _Adapter:
    """Records every broker call, and when it happened relative to a lock."""

    def __init__(self, on_submit=None):
        self.submissions = []
        self._on_submit = on_submit

    def submit_order(self, symbol, quantity, **kwargs):
        if self._on_submit:
            self._on_submit()
        self.submissions.append((symbol, quantity, kwargs))
        return _Response()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


@pytest.fixture
def lock_file(tmp_path, monkeypatch):
    target = tmp_path / "s6_exec.lock"
    monkeypatch.setenv("S6_EXECUTION_LOCK_FILE", str(target))
    return target


def _open_position(conn, symbol="AAPL", quantity=3):
    pid = position_store.record_submission(
        conn, symbol=symbol, variant="S6-R", entry_session="REGULAR",
        range_high=99.5, range_low=99.0, entry_volume_expansion=2.0,
        client_order_id="c1")
    position_store.open_from_fill(
        conn, pid, quantity=quantity, average_fill_price=10.0,
        entry_order_id="o1")
    return pid


class TestEvaluationDoesNotNeedTheExecutionLock:
    """A. The whole point: analysis proceeds while a BUY holds the lock."""

    def test_a_hold_decision_is_reached_while_the_lock_is_held(
            self, conn, lock_file, monkeypatch):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        adapter = _Adapter()

        # A BUY submission holds the execution lock for the whole call.
        with execution_lock.hold("TEST_BUY"):
            outcome = exit_runtime.evaluate_position(
                conn, broker_adapter=adapter, position_id=pid, row=row,
                features=None, current_price=None, session=None,
                orders_allowed=True)

        assert outcome.action == "HELD"
        assert adapter.submissions == [], "a HOLD must never call the broker"


class TestTheSellTakesTheLock:
    """B. And releases it."""

    def test_submission_happens_inside_the_lock(self, conn, lock_file):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        seen = {}

        def _probe():
            # Inside submit_order: the lock must already be unavailable
            # to anyone else.
            with pytest.raises(ExecutionLockUnavailable):
                with execution_lock.hold("TEST_OTHER", timeout_seconds=0):
                    pass
            seen["locked_during_submit"] = True

        adapter = _Adapter(on_submit=_probe)
        outcome = exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid, row=row,
            reason="RANGE_REENTRY")

        assert outcome.action == "SOLD"
        assert seen.get("locked_during_submit") is True
        assert len(adapter.submissions) == 1

    def test_the_lock_is_released_afterwards(self, conn, lock_file):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        exit_runtime._submit_sell_locked(
            conn, broker_adapter=_Adapter(), position_id=pid, row=row,
            reason="RANGE_REENTRY")
        with execution_lock.hold("TEST_AFTER", timeout_seconds=0):
            pass  # must not raise


class TestRevalidationUnderTheLock:
    """C, D, E. The decision was made outside the lock; the row may have
    moved since."""

    def test_a_closed_position_is_not_sold(self, conn, lock_file):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        position_store.close_position(conn, pid, reason="EXTERNAL")
        adapter = _Adapter()

        outcome = exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid, row=row,
            reason="RANGE_REENTRY")

        assert outcome.action == "SELL_BLOCKED"
        assert "closed" in outcome.detail
        assert adapter.submissions == []

    def test_an_already_submitted_exit_is_not_sold_twice(
            self, conn, lock_file):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        position_store.mark_exit_submitted(conn, pid, "RANGE_REENTRY")
        adapter = _Adapter()

        outcome = exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid, row=row,
            reason="RANGE_REENTRY")

        assert outcome.action == "SELL_BLOCKED"
        assert "already submitted" in outcome.detail
        assert adapter.submissions == []

    def test_a_vanished_row_is_not_sold(self, conn, lock_file):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        conn.execute("DELETE FROM s6_positions WHERE position_id = ?", (pid,))
        conn.commit()
        adapter = _Adapter()

        outcome = exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid, row=row,
            reason="RANGE_REENTRY")

        assert outcome.action == "SELL_BLOCKED"
        assert adapter.submissions == []

    def test_the_quantity_sold_is_the_revalidated_one(self, conn, lock_file):
        """Not the quantity the stale decision was made from."""
        pid = _open_position(conn, quantity=3)
        stale = dict(position_store.load(conn, pid))
        stale["quantity"] = 99
        adapter = _Adapter()

        exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid, row=stale,
            reason="RANGE_REENTRY")

        assert adapter.submissions[0][1] == 3


class TestAtMostOneBrokerSell:
    """F. Two exit attempts for one position."""

    def test_a_second_attempt_does_not_reach_the_broker(
            self, conn, lock_file):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        adapter = _Adapter()

        first = exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid, row=row,
            reason="RANGE_REENTRY")
        second = exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid, row=row,
            reason="RANGE_REENTRY")

        assert first.action == "SOLD"
        assert second.action == "SELL_BLOCKED"
        assert len(adapter.submissions) == 1, "exactly one broker SELL"


class TestLockUnavailable:
    """I. Contention is never a reason to sell, and never terminal."""

    def test_no_broker_call_when_the_lock_cannot_be_taken(
            self, conn, lock_file, monkeypatch):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        adapter = _Adapter()
        monkeypatch.setenv("S6_EXECUTION_LOCK_ACQUIRE_TIMEOUT_SECONDS", "0")

        with execution_lock.hold("TEST_BUY"):
            outcome = exit_runtime._submit_sell_locked(
                conn, broker_adapter=adapter, position_id=pid, row=row,
                reason="RANGE_REENTRY")

        assert adapter.submissions == [], "never submit without the lock"
        assert outcome.action == "EXIT_PENDING_LATCHED"
        assert "execution lock unavailable" in outcome.detail

    def test_the_exit_stays_eligible_for_the_next_tick(
            self, conn, lock_file, monkeypatch):
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        monkeypatch.setenv("S6_EXECUTION_LOCK_ACQUIRE_TIMEOUT_SECONDS", "0")

        with execution_lock.hold("TEST_BUY"):
            exit_runtime._submit_sell_locked(
                conn, broker_adapter=_Adapter(), position_id=pid, row=row,
                reason="RANGE_REENTRY")

        after = position_store.load(conn, pid)
        assert after["status"] == position_store.EXIT_PENDING
        assert not after["exit_submitted"], (
            "contention must not look like a submitted exit")
        assert after["pending_exit_reason"] == "RANGE_REENTRY"

        # And the retry path sends it once the lock is free.
        adapter = _Adapter()
        retried = exit_runtime.retry_latched_exits(
            conn, broker_adapter=adapter, orders_allowed=True)
        assert len(adapter.submissions) == 1
        assert retried[0]["action"] == "SOLD"


class TestSerialisedAgainstBuy:
    """G. The SELL and the BUY still take the SAME file."""

    def test_the_sell_uses_the_same_lock_path_as_entry(self, lock_file):
        import kis_live_trading

        assert execution_lock.lock_path() == str(lock_file)
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "lock_owner=_EXEC_LOCK_OWNER_ENTRY" in source, (
            "the entry hands the lock to the engine, which takes it after "
            "its reconciliation reads rather than around them")
        exit_source = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        assert "execution_lock.hold(_EXEC_LOCK_OWNER_EXIT)" in exit_source

    def test_a_concurrent_sell_and_buy_do_not_overlap(self, conn, lock_file):
        """Held by a thread standing in for the BUY submission."""
        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        overlapped = []
        buy_holding = threading.Event()
        release = threading.Event()

        def _buy():
            with execution_lock.hold("TEST_BUY"):
                buy_holding.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=_buy)
        thread.start()
        try:
            buy_holding.wait(timeout=5)

            def _probe():
                overlapped.append(buy_holding.is_set() and not release.is_set())

            adapter = _Adapter(on_submit=_probe)
            import os
            os.environ["S6_EXECUTION_LOCK_ACQUIRE_TIMEOUT_SECONDS"] = "0"
            outcome = exit_runtime._submit_sell_locked(
                conn, broker_adapter=adapter, position_id=pid, row=row,
                reason="RANGE_REENTRY")
            os.environ.pop("S6_EXECUTION_LOCK_ACQUIRE_TIMEOUT_SECONDS", None)
        finally:
            release.set()
            thread.join(timeout=5)

        assert adapter.submissions == [], (
            "the SELL must not submit while the BUY holds the lock")
        assert outcome.action == "EXIT_PENDING_LATCHED"


class TestIdempotencySemanticsIntact:
    """J. The ledger is still the authority; this only pre-checks."""

    def test_the_intent_ledger_still_reserves(self, conn, lock_file):
        from state_store import exit_intent_ledger as eil

        pid = _open_position(conn)
        row = position_store.load(conn, pid)
        exit_runtime._submit_sell_locked(
            conn, broker_adapter=_Adapter(), position_id=pid, row=row,
            reason="RANGE_REENTRY")

        rows = list(conn.execute(
            "SELECT state, client_order_id FROM exit_intents "
            "WHERE position_id = ?", (pid,)))
        assert len(rows) == 1, "one reservation, one SELL"
        assert rows[0][0] == "SUBMITTED"

    def test_the_precheck_defers_to_the_ledger(self):
        """`_sell_still_valid` is a readable early refusal, not a second
        state machine -- reserve() remains what makes a duplicate SELL
        impossible."""
        source = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        assert "_submit_sell(" in source, "still the shared submitter"
        assert "exit_intent_ledger" not in source.split(
            "def _sell_still_valid")[1].split("def ")[0], (
            "the pre-check must not reimplement the ledger")
