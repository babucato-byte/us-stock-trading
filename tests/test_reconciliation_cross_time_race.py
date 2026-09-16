"""A comparison built across a state change is not evidence either way.

`build_snapshot` reads KIS first and this codebase second, and the three
broker reads queue behind a shared 3-second limiter. An order that is
ACCEPTED when KIS is read and CANCELLED when the ledger is read is
absent from `_INTERNAL_LIVE_STATUSES` while still present in the
broker's open-order list, and the comparison reports it as an order
"not tracked internally" -- an order this codebase submitted, tracked
and cancelled correctly.

Confirmed twice on 2026-09-16, both recovering to CLEAN on the next
pass:

    HAL  SELL 0030499393   ACCEPTED -> CANCELLED
    ARQT BUY  0030578574   ACCEPTED -> CANCEL_PENDING -> CANCELLED

The tests that matter most here are the ones proving the guard did NOT
also hide a real disagreement.
"""

from datetime import datetime, timezone

import pytest

from reconciliation import consistency
from reconciliation import snapshot as snap

UTC = timezone.utc
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
ACCOUNT = "12345678-01"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


class _Broker:
    """KIS, read at the instant `build_snapshot` asks.

    `on_read` fires during collection, which is where the real writers
    ran: between the broker answering and the ledger being read.
    """

    class _Config:
        account_no = ACCOUNT

    def __init__(self, *, positions=(), open_orders=(), fills=(), on_read=None):
        self._positions = list(positions)
        self._open_orders = list(open_orders)
        self._fills = list(fills)
        self._on_read = on_read
        self.config = self._Config()

    def get_positions(self):
        return list(self._positions)

    def get_open_orders(self):
        if self._on_read:
            self._on_read()
            self._on_read = None
        return list(self._open_orders)

    def get_fills(self, **kwargs):
        return list(self._fills)


def _record_order(conn, *, internal_id, broker_id, symbol, side, status):
    from execution import idempotency

    idempotency.register(
        conn, internal_order_id=internal_id, signal_id=f"sig-{internal_id}",
        symbol=symbol, side=side, trading_date="2026-09-16",
        requested_quantity=10, strategy_id="S6")
    if broker_id:
        conn.execute(
            "UPDATE kis_order_idempotency SET broker_order_id = ? "
            "WHERE internal_order_id = ?", (broker_id, internal_id))
        conn.commit()
    _force_status(conn, internal_id, status)


def _force_status(conn, internal_id, status):
    """Set the ledger status directly, with the event row the real
    transition would have written -- the marker watches both."""
    conn.execute(
        "UPDATE kis_order_idempotency SET status = ?, version = version + 1 "
        "WHERE internal_order_id = ?", (status, internal_id))
    conn.execute(
        "INSERT INTO order_state_events (internal_order_id, from_state, "
        "to_state, event_type, version, occurred_at) "
        "VALUES (?, ?, ?, 'TRANSITION', 1, ?)",
        (internal_id, None, status, NOW.isoformat()))
    conn.commit()


def _kis_open(order_id, symbol):
    return {"ODNO": order_id, "PDNO": symbol}


def _build(conn, broker, **kwargs):
    return snap.build_snapshot(broker=broker, conn=conn, account_id=ACCOUNT,
                               now=NOW, source="test", **kwargs)


# -- 1. the ordinary case must be untouched ------------------------------

class TestUnchangedInternalState:
    """1. Nothing moved -> the comparison runs exactly as before."""

    def test_a_stable_matching_state_is_clean(self, conn):
        _record_order(conn, internal_id="i1", broker_id="0030499393",
                      symbol="HAL", side="SELL", status="ACCEPTED")
        broker = _Broker(open_orders=[_kis_open("0030499393", "HAL")])
        result = _build(conn, broker)
        assert result.open_orders_match is True
        assert result.is_clean() is True

    def test_the_guard_does_not_fire_when_nothing_writes(self, conn):
        _record_order(conn, internal_id="i1", broker_id="B1", symbol="AAPL",
                      side="BUY", status="ACCEPTED")
        broker = _Broker(open_orders=[_kis_open("B1", "AAPL")])
        for _ in range(3):
            assert _build(conn, broker).is_clean() is True


# -- 2 and 3. the two confirmed incidents --------------------------------

class TestTheConfirmedIncidents:

    def test_2_hal_sell_accepted_to_cancelled_is_not_a_mismatch(self, conn):
        """HAL SELL 0030499393: KIS OPEN observed, then ACCEPTED ->
        CANCELLED before the ledger was read."""
        _record_order(conn, internal_id="i1", broker_id="0030499393",
                      symbol="HAL", side="SELL", status="ACCEPTED")

        broker = _Broker(
            open_orders=[_kis_open("0030499393", "HAL")],
            on_read=lambda: _force_status(conn, "i1", "CANCELLED"))

        with pytest.raises(snap.ReconciliationSnapshotStale) as caught:
            _build(conn, broker)
        assert caught.value.reason_code == consistency.STALE_REASON_CODE
        assert "not tracked internally" not in str(caught.value)

    def test_3_arqt_buy_through_cancel_pending_is_not_a_mismatch(self, conn):
        """ARQT BUY 0030578574: ACCEPTED -> CANCEL_PENDING -> CANCELLED."""
        _record_order(conn, internal_id="i2", broker_id="0030578574",
                      symbol="ARQT", side="BUY", status="ACCEPTED")

        def _during_collection():
            _force_status(conn, "i2", "CANCEL_PENDING")
            _force_status(conn, "i2", "CANCELLED")

        broker = _Broker(open_orders=[_kis_open("0030578574", "ARQT")],
                         on_read=_during_collection)

        with pytest.raises(snap.ReconciliationSnapshotStale):
            _build(conn, broker)

    def test_without_the_race_the_same_order_reports_the_real_state(self, conn):
        """The control: cancelled BEFORE collection is a genuine
        disagreement with a broker that still lists it open, and must
        still be reported."""
        _record_order(conn, internal_id="i3", broker_id="0030499393",
                      symbol="HAL", side="SELL", status="CANCELLED")
        broker = _Broker(open_orders=[_kis_open("0030499393", "HAL")])
        result = _build(conn, broker)
        assert result.open_orders_match is False
        assert any("not tracked internally" in line for line in result.detail)


# -- 4 and 5. real mismatches must survive the fix ------------------------

class TestRealMismatchesStillReported:

    def test_4_a_genuinely_unknown_broker_order_is_still_a_mismatch(self, conn):
        """An order nobody here ever submitted -- a manual order, another
        process. The whole point of the check; the race fix must not
        hide it."""
        broker = _Broker(open_orders=[_kis_open("9999999999", "TSLA")])
        result = _build(conn, broker)
        assert result.open_orders_match is False
        assert result.is_clean() is False
        assert any("9999999999" in line for line in result.detail)
        assert "TSLA" in result.order_dirty_symbols

    def test_4b_an_internally_live_order_kis_does_not_know_is_reported(self, conn):
        _record_order(conn, internal_id="i4", broker_id="B4", symbol="NVDA",
                      side="BUY", status="ACCEPTED")
        broker = _Broker(open_orders=[], fills=[])
        result = _build(conn, broker)
        assert result.open_orders_match is False
        assert any("B4" in line for line in result.detail)

    def test_5_a_real_position_mismatch_is_still_reported(self, conn):
        class _Pos:
            def __init__(self, symbol, quantity):
                self.symbol = symbol
                self.quantity = quantity

        broker = _Broker(positions=[_Pos("MSFT", 50)])
        result = _build(conn, broker, internal_positions=[])
        assert result.positions_match is False
        assert result.is_clean() is False


# -- 6. a stale snapshot can never publish CLEAN --------------------------

class TestStaleCannotPublishClean:

    def test_6_no_snapshot_object_exists_to_call_clean_on(self, conn):
        """The guard raises instead of returning, so there is no object a
        caller could read `is_clean()` from -- which is what makes an
        accidental CLEAN unrepresentable rather than merely avoided."""
        _record_order(conn, internal_id="i5", broker_id="B5", symbol="HAL",
                      side="SELL", status="ACCEPTED")
        broker = _Broker(open_orders=[_kis_open("B5", "HAL")],
                         on_read=lambda: _force_status(conn, "i5", "CANCELLED"))
        with pytest.raises(snap.ReconciliationSnapshotStale):
            _build(conn, broker)

    def test_the_stale_error_fails_every_existing_caller_closed(self):
        """A subclass of the unavailable error, so callers written before
        this existed still block rather than proceed."""
        assert issubclass(snap.ReconciliationSnapshotStale,
                          snap.ReconciliationUnavailableError)

    def test_the_periodic_pass_records_nothing_and_says_why(self, conn,
                                                            monkeypatch):
        """A3: no record written, so the clean timestamp the order gates
        read is not refreshed by a pass that reached no verdict."""
        import scripts.run_reconciliation as runner

        _record_order(conn, internal_id="i6", broker_id="B6", symbol="HAL",
                      side="SELL", status="ACCEPTED")
        broker = _Broker(open_orders=[_kis_open("B6", "HAL")],
                         on_read=lambda: _force_status(conn, "i6", "CANCELLED"))

        written = []
        monkeypatch.setattr(runner.reconciliation_state, "record_result",
                            lambda **kw: written.append(kw))
        monkeypatch.setattr(runner, "resolve_unknown_orders",
                            lambda *a, **k: [])
        monkeypatch.setattr(runner, "settle_live_orders", lambda *a, **k: [])
        monkeypatch.setattr(runner.exit_intent_resolution,
                            "resolve_unknown_exit_intents", lambda *a, **k: [])

        result = runner.run_once(broker=broker, conn=conn, now=NOW,
                                 account_id=ACCOUNT)

        assert result["status"] == "snapshot_stale"
        assert result["snapshot"] is None
        assert result["reason_code"] == consistency.STALE_REASON_CODE
        assert written == [], "a pass with no verdict must record nothing"


# -- the consistency domain itself ---------------------------------------

class TestInvalidationScope:
    """A2. Small enough not to be useless, wide enough to be safe."""

    def test_an_order_status_transition_invalidates(self, conn):
        before = consistency.capture(conn)
        _record_order(conn, internal_id="i7", broker_id="B7", symbol="AAPL",
                      side="BUY", status="ACCEPTED")
        assert consistency.is_stale(before, consistency.capture(conn))

    def test_a_broker_order_id_mapping_invalidates(self, conn):
        """A2 names this explicitly: the comparison JOINS on the broker
        id, so an id learned late changes which orders match."""
        from execution import idempotency

        idempotency.register(
            conn, internal_order_id="i8", signal_id="s8", symbol="AAPL",
            side="BUY", trading_date="2026-09-16", requested_quantity=1,
            strategy_id="S6")
        before = consistency.capture(conn)
        conn.execute("UPDATE kis_order_idempotency SET broker_order_id = ? "
                     "WHERE internal_order_id = ?", ("LATE-ID", "i8"))
        conn.commit()
        assert consistency.is_stale(before, consistency.capture(conn))

    def test_an_exit_intent_transition_invalidates(self, conn):
        before = consistency.capture(conn)
        conn.execute(
            "INSERT INTO exit_intents (intent_id, position_id, "
            "client_order_id, reason, requested_qty, state, created_at, "
            "updated_at) VALUES ('e1','p1','c1','STOP',1,'RESERVED',?,?)",
            (NOW.isoformat(), NOW.isoformat()))
        conn.commit()
        assert consistency.is_stale(before, consistency.capture(conn))

    def test_an_s6_position_transition_invalidates(self, conn):
        from s6_live import position_store

        before = consistency.capture(conn)
        position_store.record_submission(
            conn, symbol="AAPL", variant="S6-R", entry_session="REGULAR",
            range_high=1.0, range_low=0.5, entry_volume_expansion=2.0,
            client_order_id="c9")
        assert consistency.is_stale(before, consistency.capture(conn))

    def test_an_irrelevant_write_does_not_invalidate(self, conn):
        """A2: the domain must be the SMALLEST safe one. A scan writing
        its own bookkeeping cannot make a broker comparison wrong, and
        invalidating on it would leave a busy account unverifiable."""
        before = consistency.capture(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS unrelated_scratch (x TEXT)")
        conn.execute("INSERT INTO unrelated_scratch (x) VALUES ('noise')")
        conn.commit()
        assert not consistency.is_stale(before, consistency.capture(conn))

    def test_a_missing_marker_is_treated_as_stale(self):
        """Fail-closed: a marker that could not be taken cannot prove the
        state held still, and must not read as 'nothing changed'."""
        assert consistency.is_stale(None, None) is True
        marker = consistency.ConsistencyMarker(digests=(("orders", "a"),))
        assert consistency.is_stale(marker, None) is True
        assert consistency.is_stale(None, marker) is True

    def test_capture_returns_none_rather_than_raising(self):
        assert consistency.capture(None) is None

    def test_the_change_description_names_the_domain(self, conn):
        before = consistency.capture(conn)
        _record_order(conn, internal_id="i10", broker_id="B10", symbol="X",
                      side="BUY", status="ACCEPTED")
        text = consistency.describe_change(before, consistency.capture(conn))
        assert "orders" in text or "order_events" in text
