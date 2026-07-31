import pytest

from execution import idempotency
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


class TestUpdateStatus:
    def test_update_status_and_broker_order_id(self):
        conn = _conn()
        _register(conn)
        idempotency.update_status(conn, "ord-1", "ACCEPTED", broker_order_id="kis-123")
        row = idempotency.find_existing(
            conn, internal_order_id="ord-1", signal_id="sig-1", symbol="AAPL",
            side="buy", trading_date="2026-07-29",
        )
        assert row["status"] == "ACCEPTED"
        assert row["broker_order_id"] == "kis-123"


class TestHasUnknownOrder:
    def test_no_rows_returns_false(self):
        conn = _conn()
        assert idempotency.has_unknown_order(conn, symbol="AAPL", side="buy") is False

    def test_unknown_status_returns_true_for_matching_symbol_and_side(self):
        conn = _conn()
        _register(conn)
        idempotency.update_status(conn, "ord-1", "UNKNOWN")
        assert idempotency.has_unknown_order(conn, symbol="AAPL", side="buy") is True

    def test_unknown_on_other_symbol_does_not_block(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", symbol="AAPL")
        idempotency.update_status(conn, "ord-1", "UNKNOWN")
        assert idempotency.has_unknown_order(conn, symbol="MSFT", side="buy") is False

    def test_unknown_on_other_side_does_not_block(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", symbol="AAPL", side="buy")
        idempotency.update_status(conn, "ord-1", "UNKNOWN")
        assert idempotency.has_unknown_order(conn, symbol="AAPL", side="sell") is False

    def test_accepted_status_does_not_block(self):
        conn = _conn()
        _register(conn)
        idempotency.update_status(conn, "ord-1", "ACCEPTED")
        assert idempotency.has_unknown_order(conn, symbol="AAPL", side="buy") is False


class TestListUnknownOrders:
    def test_returns_only_unknown_rows(self):
        conn = _conn()
        _register(conn, internal_order_id="ord-1", symbol="AAPL")
        _register(conn, internal_order_id="ord-2", symbol="MSFT")
        idempotency.update_status(conn, "ord-1", "UNKNOWN", broker_order_id="kis-1")
        idempotency.update_status(conn, "ord-2", "ACCEPTED", broker_order_id="kis-2")
        rows = idempotency.list_unknown_orders(conn)
        assert [r["internal_order_id"] for r in rows] == ["ord-1"]
        assert rows[0]["broker_order_id"] == "kis-1"


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
