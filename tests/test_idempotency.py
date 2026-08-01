import pytest

from execution import idempotency, order_repository
from state_store import db as state_db


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    yield


def _conn():
    return state_db.open_db()


def _register(conn, **overrides):
    kwargs = dict(
        internal_order_id="ord-1", signal_id="sig-1", symbol="AAPL", side="buy",
        trading_date="2026-07-29",
    )
    kwargs.update(overrides)
    idempotency.register(conn, **kwargs)
    return kwargs


# CODEX-047: there is no update_status() any more -- a status is only
# reachable by walking the real state machine through the compare-and-set
# repository, which is exactly the property these tests should rely on.
_PATH_TO = {
    "VALIDATING": ["VALIDATING"],
    "APPROVED": ["VALIDATING", "APPROVED"],
    "SUBMITTING": ["VALIDATING", "APPROVED", "SUBMITTING"],
    "ACCEPTED": ["VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED"],
    "UNKNOWN": ["VALIDATING", "APPROVED", "SUBMITTING", "UNKNOWN"],
}


def _drive_to(conn, order_id, target, *, broker_order_id=None):
    record = order_repository.load(conn, order_id)
    steps = _PATH_TO[target]
    for index, state in enumerate(steps):
        is_last = index == len(steps) - 1
        record = order_repository.advance(
            conn, record, state, event_type="TEST",
            broker_order_id=broker_order_id if is_last else None,
        )
    return record


class TestRegister:
    def test_first_registration_succeeds(self):
        conn = _conn()
        _register(conn)
        row = idempotency.find_existing(
            conn, internal_order_id="ord-1", signal_id="sig-1", symbol="AAPL",
            side="buy", trading_date="2026-07-29",
        )
        assert row is not None
        assert row["status"] == "CREATED"

    def test_duplicate_internal_order_id_blocked(self):
        conn = _conn()
        _register(conn)
        with pytest.raises(idempotency.DuplicateOrderAttemptError):
            _register(conn)

    def test_duplicate_business_key_different_internal_id_blocked(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1")
        with pytest.raises(idempotency.DuplicateOrderAttemptError):
            _register(conn, internal_order_id="ord-2")  # same signal/symbol/side/date

    def test_different_trading_date_allowed(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", trading_date="2026-07-29")
        _register(conn, internal_order_id="ord-2", trading_date="2026-07-30")  # should not raise

    def test_different_side_allowed(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", side="buy")
        _register(conn, internal_order_id="ord-2", side="sell")  # should not raise


class TestNoBareStatusWriteApiExists:
    def test_update_status_is_gone(self):
        # CODEX-047: a "set this status" API cannot check the current
        # state, the expected version, or write the paired state event,
        # so it must not exist at all.
        assert not hasattr(idempotency, "update_status")


class TestDurableStateWrites:
    def test_state_and_broker_order_id_are_persisted_through_cas(self):
        conn = _conn()
        _register(conn)
        _drive_to(conn, "ord-1", "ACCEPTED", broker_order_id="kis-123")
        row = idempotency.find_existing(
            conn, internal_order_id="ord-1", signal_id="sig-1", symbol="AAPL",
            side="buy", trading_date="2026-07-29",
        )
        assert row["status"] == "ACCEPTED"
        assert row["broker_order_id"] == "kis-123"


class TestHasUnknownOrder:
    def test_no_rows_returns_false(self):
        conn = _conn()
        assert idempotency.has_unknown_order(conn) is False

    def test_unknown_status_returns_true(self):
        conn = _conn()
        _register(conn)
        _drive_to(conn, "ord-1", "UNKNOWN")
        assert idempotency.has_unknown_order(conn) is True

    def test_unknown_on_another_symbol_still_blocks(self):
        # CODEX-044: the previous (symbol, side)-scoped query let a new
        # order through while a DIFFERENT symbol's order sat unresolved.
        # An UNKNOWN order anywhere means this codebase does not know the
        # account's real exposure, so it blocks account-wide.
        conn = _conn()
        _register(conn, internal_order_id="ord-1", symbol="AAPL")
        _drive_to(conn, "ord-1", "UNKNOWN")
        assert idempotency.has_unknown_order(conn) is True

    def test_unknown_on_the_opposite_side_still_blocks(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", symbol="AAPL", side="buy")
        _drive_to(conn, "ord-1", "UNKNOWN")
        assert idempotency.has_unknown_order(conn) is True

    def test_accepted_status_does_not_block(self):
        conn = _conn()
        _register(conn)
        _drive_to(conn, "ord-1", "ACCEPTED")
        assert idempotency.has_unknown_order(conn) is False


class TestListUnknownOrders:
    def test_returns_only_unknown_rows(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", symbol="AAPL")
        _register(conn, internal_order_id="ord-2", symbol="MSFT", signal_id="sig-2")
        _drive_to(conn, "ord-1", "UNKNOWN", broker_order_id="kis-1")
        _drive_to(conn, "ord-2", "ACCEPTED", broker_order_id="kis-2")
        rows = idempotency.list_unknown_orders(conn)
        assert [r["internal_order_id"] for r in rows] == ["ord-1"]
        assert rows[0]["broker_order_id"] == "kis-1"


class TestOrderListingsForReconciliation:
    def test_list_orders_by_status(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", symbol="AAPL")
        _register(conn, internal_order_id="ord-2", symbol="MSFT", signal_id="sig-2")
        _drive_to(conn, "ord-1", "ACCEPTED", broker_order_id="kis-1")
        rows = idempotency.list_orders_by_status(conn, ("ACCEPTED",))
        assert [r["internal_order_id"] for r in rows] == ["ord-1"]

    def test_list_orders_with_broker_id_skips_rows_without_one(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", symbol="AAPL")
        _register(conn, internal_order_id="ord-2", symbol="MSFT", signal_id="sig-2")
        _drive_to(conn, "ord-1", "ACCEPTED", broker_order_id="kis-1")
        rows = idempotency.list_orders_with_broker_id(conn)
        assert [r["broker_order_id"] for r in rows] == ["kis-1"]


class TestSingleRunLock:
    def test_lock_is_reentrant_across_sequential_uses(self):
        with idempotency.single_run_lock(timeout=1.0):
            pass
        with idempotency.single_run_lock(timeout=1.0):
            pass  # should not deadlock/raise on second sequential use

    def test_concurrent_holder_blocks_second_acquire(self):
        with idempotency.single_run_lock(timeout=1.0):
            with pytest.raises(idempotency.IdempotencyError):
                with idempotency.single_run_lock(timeout=0.2):
                    pass
