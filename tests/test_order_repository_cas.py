"""CODEX-047: durable state machine + compare-and-set enforcement.

These tests exercise the DATABASE layer, not the pure transition graph
(tests/test_order_state_machine.py already covers that). The distinction
is the whole point of this finding: the pure graph was already correct,
and the persistence layer ignored it.
"""
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from execution import idempotency, order_repository
from execution.order_repository import OrderRepositoryError, OrderStateConflictError
from execution.order_state_machine import OrderStateTransitionError
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    yield


def _conn():
    return state_db.open_db()


def _register(conn, order_id="ord-1", **overrides):
    kwargs = dict(
        internal_order_id=order_id, signal_id=f"sig-{order_id}", symbol="AAPL", side="buy",
        trading_date="2026-07-29", requested_quantity=1,
    )
    kwargs.update(overrides)
    idempotency.register(conn, **kwargs)
    return order_repository.load(conn, kwargs["internal_order_id"])


class TestRegistration:
    def test_new_order_starts_at_version_zero_with_a_creation_event(self):
        conn = _conn()
        record = _register(conn)
        assert record.state == "CREATED"
        assert record.version == 0
        events = order_repository.load_events(conn, "ord-1")
        assert [e["to_state"] for e in events] == ["CREATED"]
        assert events[0]["event_type"] == "ORDER_CREATED"


class TestCompareAndSet:
    def test_legal_transition_succeeds_and_bumps_version(self):
        conn = _conn()
        record = _register(conn)
        updated = order_repository.advance(conn, record, "VALIDATING", event_type="T", now=NOW)
        assert updated.state == "VALIDATING"
        assert updated.version == 1

    def test_illegal_transition_rejected_before_any_db_write(self):
        conn = _conn()
        record = _register(conn)
        with pytest.raises(OrderStateTransitionError):
            order_repository.advance(conn, record, "FILLED", event_type="T", now=NOW)
        assert order_repository.load(conn, "ord-1").state == "CREATED"
        # No event row was written for the rejected attempt.
        assert len(order_repository.load_events(conn, "ord-1")) == 1

    def test_expected_state_mismatch_fails(self):
        conn = _conn()
        _register(conn)
        with pytest.raises(OrderStateConflictError):
            order_repository.compare_and_set_state(
                conn, order_id="ord-1", expected_state="APPROVED", next_state="SUBMITTING",
                event_type="T", expected_version=0, now=NOW,
            )

    def test_expected_version_mismatch_fails(self):
        conn = _conn()
        record = _register(conn)
        order_repository.advance(conn, record, "VALIDATING", event_type="T", now=NOW)
        # `record` is now stale: state moved on and version is 1, not 0.
        with pytest.raises(OrderStateConflictError):
            order_repository.compare_and_set_state(
                conn, order_id="ord-1", expected_state="VALIDATING", next_state="APPROVED",
                event_type="T", expected_version=0, now=NOW,
            )

    def test_unknown_order_id_fails(self):
        conn = _conn()
        with pytest.raises(OrderStateConflictError):
            order_repository.compare_and_set_state(
                conn, order_id="does-not-exist", expected_state="CREATED",
                next_state="VALIDATING", event_type="T", expected_version=0, now=NOW,
            )

    def test_broker_order_id_is_written_atomically_with_the_state(self):
        conn = _conn()
        record = _register(conn)
        for state in ("VALIDATING", "APPROVED", "SUBMITTING"):
            record = order_repository.advance(conn, record, state, event_type="T", now=NOW)
        record = order_repository.advance(
            conn, record, "ACCEPTED", event_type="T", broker_order_id="kis-1", now=NOW,
        )
        assert record.broker_order_id == "kis-1"
        assert record.state == "ACCEPTED"


class TestUnknownHandling:
    def _to_unknown(self, conn):
        record = _register(conn)
        for state in ("VALIDATING", "APPROVED", "SUBMITTING", "UNKNOWN"):
            record = order_repository.advance(conn, record, state, event_type="T", now=NOW)
        return record

    def test_lost_response_persists_unknown(self):
        conn = _conn()
        self._to_unknown(conn)
        assert order_repository.load(conn, "ord-1").state == "UNKNOWN"

    def test_unknown_cannot_transition_back_to_submitting(self):
        conn = _conn()
        record = self._to_unknown(conn)
        with pytest.raises(OrderStateTransitionError):
            order_repository.advance(conn, record, "SUBMITTING", event_type="T", now=NOW)

    def test_unknown_can_only_be_left_via_reconciliation(self):
        conn = _conn()
        record = self._to_unknown(conn)
        with pytest.raises(OrderStateTransitionError):
            order_repository.advance(conn, record, "FILLED", event_type="T", now=NOW)
        resolved = order_repository.advance(
            conn, record, "FILLED", event_type="UNKNOWN_RECONCILED",
            via_reconciliation=True, now=NOW,
        )
        assert resolved.state == "FILLED"

    def test_reconciliation_cannot_resolve_to_submitting(self):
        conn = _conn()
        record = self._to_unknown(conn)
        with pytest.raises(OrderStateTransitionError):
            order_repository.advance(
                conn, record, "SUBMITTING", event_type="X", via_reconciliation=True, now=NOW,
            )

    def test_unknown_survives_a_restart(self):
        conn = _conn()
        self._to_unknown(conn)
        conn.close()
        # A brand-new connection == a restarted process reading the same
        # durable file.
        fresh = _conn()
        assert order_repository.load(fresh, "ord-1").state == "UNKNOWN"
        assert idempotency.has_unknown_order(fresh) is True


class TestPartialFills:
    def test_partially_filled_to_filled(self):
        conn = _conn()
        record = _register(conn)
        for state in ("VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED"):
            record = order_repository.advance(conn, record, state, event_type="T", now=NOW)
        record = order_repository.advance(conn, record, "FILLED", event_type="T", now=NOW)
        assert record.state == "FILLED"

    def test_repeated_partial_fills_each_bump_the_version(self):
        conn = _conn()
        record = _register(conn)
        for state in ("VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED"):
            record = order_repository.advance(conn, record, state, event_type="T", now=NOW)
        first_version = record.version
        record = order_repository.advance(conn, record, "PARTIALLY_FILLED", event_type="T", now=NOW)
        assert record.version == first_version + 1


class _FailEventInsertConnection:
    """Thin proxy that lets the UPDATE through and fails the paired
    order_state_events INSERT -- the only way to observe that the two
    writes really are one transaction (sqlite3.Connection.execute cannot
    be monkeypatched directly)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO order_state_events" in sql:
            raise sqlite3.OperationalError("simulated event-write failure")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestStateAndEventAreOneTransaction:
    def test_every_transition_writes_exactly_one_event(self):
        conn = _conn()
        record = _register(conn)
        for state in ("VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED"):
            record = order_repository.advance(conn, record, state, event_type="T", now=NOW)
        events = order_repository.load_events(conn, "ord-1")
        assert [e["to_state"] for e in events] == [
            "CREATED", "VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED",
        ]
        assert [e["version"] for e in events] == [0, 1, 2, 3, 4]

    def test_event_write_failure_rolls_back_the_state_change(self):
        conn = _conn()
        record = _register(conn)
        failing = _FailEventInsertConnection(conn)
        with pytest.raises(sqlite3.OperationalError):
            order_repository.advance(failing, record, "VALIDATING", event_type="T", now=NOW)
        # BOTH must be rolled back: the state is untouched and no event
        # row exists for the failed transition.
        assert order_repository.load(conn, "ord-1").state == "CREATED"
        assert len(order_repository.load_events(conn, "ord-1")) == 1

    def test_caller_owned_transaction_is_refused(self):
        conn = _conn()
        record = _register(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(OrderRepositoryError, match="idle connection"):
                order_repository.advance(conn, record, "VALIDATING", event_type="T", now=NOW)
        finally:
            conn.rollback()


class TestConcurrentCompareAndSet:
    def test_two_concurrent_writers_only_one_wins(self):
        """Both threads read the same (state, version) and both try to
        advance it. Exactly one may succeed -- the other must get a
        conflict, not silently overwrite the winner."""
        setup = _conn()
        record = _register(setup)
        setup.close()

        barrier = threading.Barrier(2)
        outcomes = []

        def _attempt(next_state):
            conn = _conn()
            try:
                barrier.wait(timeout=5)
                order_repository.compare_and_set_state(
                    conn, order_id=record.internal_order_id, expected_state="CREATED",
                    next_state=next_state, event_type="RACE", expected_version=0, now=NOW,
                )
                outcomes.append(("ok", next_state))
            except OrderStateConflictError:
                outcomes.append(("conflict", next_state))
            finally:
                conn.close()

        threads = [
            threading.Thread(target=_attempt, args=("VALIDATING",)),
            threading.Thread(target=_attempt, args=("REJECTED",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        statuses = sorted(status for status, _ in outcomes)
        assert statuses == ["conflict", "ok"], outcomes

        final = _conn()
        try:
            assert order_repository.load(final, record.internal_order_id).version == 1
            # Exactly one transition event beyond the creation event.
            assert len(order_repository.load_events(final, record.internal_order_id)) == 2
        finally:
            final.close()
