"""LIVE_ROLLOUT_MAX_POSITIONS and LIVE_ROLLOUT_MAX_DAILY_ENTRIES are
actually enforced.

The defect
----------
Both limits were read and type-validated by config/live_rollout_config.py
and consumed by nothing. A search of the KIS entry path found
`max_open_positions` and `max_daily_entries` referenced in that config
file and nowhere else -- not in evaluate_buy_gate, not in
kis_live_trading, not in the shadow evaluation. An operator setting
`LIVE_ROLLOUT_MAX_POSITIONS=1` was reading a number that restricted
nothing, and would have discovered that only after ARMED.

What is pinned here
-------------------
* The counts come from durable, authoritative state (KIS positions and
  the pre-transport idempotency ledger), never from caller input.
* An in-flight entry occupies a position slot, so two candidates in one
  pass cannot both be approved against one slot.
* A symbol that is both held and in flight is one slot, not two.
* Every unreadable input fails closed with its own reason code -- there
  is no path that turns an unreadable count into zero.
* The day boundary is the US-Eastern calendar date, and it is the SAME
  value the idempotency ledger records, so the recorder and the counter
  cannot disagree.
* Sells are unaffected.
"""
from datetime import datetime, timedelta, timezone

import pytest

from config.live_rollout_config import LiveRolloutConfig, LiveRolloutConfigError
from execution import entry_limits
from execution.entry_limits import (
    DAILY_ENTRY_STATE_UNKNOWN,
    MAX_DAILY_ENTRIES,
    MAX_OPEN_POSITIONS,
    POSITION_LIMIT_STATE_UNKNOWN,
    EntryLimitState,
    EntryLimitStateUnavailable,
)
from market_hours import us_trading_day
from state_store import db as state_db

NOW = datetime(2026, 8, 7, 17, 30, tzinfo=timezone.utc)  # 13:30 ET, regular session
TODAY = us_trading_day(NOW)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POS.json"))
    from execution import idempotency

    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEM.lock")
    state_db.open_db().close()
    yield


class _Position:
    def __init__(self, symbol, quantity):
        self.symbol = symbol
        self.quantity = quantity


class _Broker:
    def __init__(self, positions=None, raises=None):
        self._positions = positions or []
        self._raises = raises

    def get_positions(self):
        if self._raises is not None:
            raise self._raises
        return self._positions


def _rollout(**overrides):
    kwargs = dict(
        enabled=True, allowed_symbols=frozenset({"AAPL"}), max_quantity_per_order=1,
        max_open_positions=1, max_positions_per_strategy=1, max_daily_entries=1, regular_session_only=True,
        allow_fractional=False, allow_market_order=False, allow_extended_hours=False,
        allow_leverage=False, allow_inverse=False, allow_short=False, allow_margin=False,
        max_price_deviation_percent=0.30,
    )
    kwargs.update(overrides)
    return LiveRolloutConfig(**kwargs)


def _attempt(conn, *, internal_order_id, symbol="AAPL", side="buy", status="CREATED",
             trading_date=TODAY, broker_order_id=None):
    """A row exactly as execution_engine.register() would leave it."""
    stamp = NOW.isoformat()
    conn.execute(
        "INSERT INTO kis_order_idempotency "
        "(internal_order_id, signal_id, symbol, side, trading_date, broker_order_id, "
        "status, created_at, updated_at, requested_quantity, version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (internal_order_id, f"sig-{internal_order_id}", symbol, side, trading_date,
         broker_order_id, status, stamp, stamp, 1),
    )
    conn.commit()


def _collect(broker=None, rollout=None, **kwargs):
    conn = state_db.open_db()
    try:
        return entry_limits.collect(
            broker=broker or _Broker(), conn=conn, rollout=rollout or _rollout(),
            now=NOW, **kwargs)
    finally:
        conn.close()


# ---------------------------------------------------------------------
# §12 -- the position cap
# ---------------------------------------------------------------------
class TestPositionCap:
    def test_A_no_positions_no_pending_passes(self):
        state = _collect()
        assert state.effective_position_count == 0
        assert state.effective_position_count < state.max_open_positions

    def test_B_one_held_position_fills_the_cap(self):
        state = _collect(broker=_Broker([_Position("MSFT", 3)]))
        assert state.open_position_count == 1
        assert state.effective_position_count >= state.max_open_positions

    def test_C_one_pending_entry_fills_the_cap(self):
        """The race guard: an order that has not filled yet still occupies
        the slot it is going to occupy."""
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", symbol="MSFT", status="SUBMITTING")
        finally:
            conn.close()
        state = _collect()
        assert state.open_position_count == 0
        assert state.pending_entry_count == 1
        assert state.effective_position_count >= state.max_open_positions

    def test_D_a_larger_cap_still_has_room(self):
        state = _collect(broker=_Broker([_Position("MSFT", 3)]),
                         rollout=_rollout(max_open_positions=2))
        assert state.effective_position_count == 1
        assert state.effective_position_count < state.max_open_positions

    def test_E_an_unreadable_position_read_fails_closed(self):
        with pytest.raises(EntryLimitStateUnavailable) as excinfo:
            _collect(broker=_Broker(raises=RuntimeError("KIS down")))
        assert excinfo.value.reason_code == POSITION_LIMIT_STATE_UNKNOWN

    def test_zero_quantity_positions_are_not_open_positions(self):
        state = _collect(broker=_Broker([_Position("MSFT", 0), _Position("AAPL", 2)]))
        assert state.open_position_symbols == frozenset({"AAPL"})

    def test_a_position_row_without_a_symbol_fails_closed(self):
        with pytest.raises(EntryLimitStateUnavailable) as excinfo:
            _collect(broker=_Broker([_Position("", 2)]))
        assert excinfo.value.reason_code == POSITION_LIMIT_STATE_UNKNOWN

    def test_a_non_numeric_quantity_fails_closed(self):
        with pytest.raises(EntryLimitStateUnavailable) as excinfo:
            _collect(broker=_Broker([_Position("AAPL", "2")]))
        assert excinfo.value.reason_code == POSITION_LIMIT_STATE_UNKNOWN

    def test_a_held_symbol_that_is_also_in_flight_is_one_slot(self):
        """Deduplicated by symbol: a filled order became a position, and
        counting both would halve the operator's real capacity."""
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", symbol="AAPL", status="ACCEPTED")
        finally:
            conn.close()
        state = _collect(broker=_Broker([_Position("AAPL", 2)]),
                         rollout=_rollout(max_open_positions=2))
        assert state.open_position_count == 1
        assert state.pending_entry_count == 1
        assert state.effective_position_count == 1

    def test_a_sell_attempt_does_not_occupy_an_entry_slot(self):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="sell-1", symbol="MSFT", side="sell",
                     status="SUBMITTING")
        finally:
            conn.close()
        assert _collect().pending_entry_count == 0

    @pytest.mark.parametrize("status", ["FILLED", "CANCELLED"])
    def test_finished_attempts_release_their_slot(self, status):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", symbol="MSFT", status=status,
                     broker_order_id="kis-1")
        finally:
            conn.close()
        assert _collect().pending_entry_count == 0

    def test_an_attempt_rejected_before_transport_releases_its_slot(self):
        """Nothing reached the broker, so nothing is in flight."""
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", symbol="MSFT", status="REJECTED")
        finally:
            conn.close()
        assert _collect().pending_entry_count == 0

    def test_an_attempt_rejected_BY_the_broker_still_counted_until_terminal(self):
        """It reached the wire; only the pre-transport case is released."""
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", symbol="MSFT", status="REJECTED",
                     broker_order_id="kis-1")
        finally:
            conn.close()
        assert _collect().pending_entry_count == 1


# ---------------------------------------------------------------------
# §13 -- the daily-entry cap
# ---------------------------------------------------------------------
class TestDailyEntryCap:
    def test_A_no_entries_today_passes(self):
        state = _collect()
        assert state.daily_entry_count == 0
        assert state.daily_entry_count < state.max_daily_entries

    def test_B_one_entry_today_fills_the_cap(self):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", status="FILLED", broker_order_id="k1")
        finally:
            conn.close()
        state = _collect()
        assert state.daily_entry_count == 1
        assert state.daily_entry_count >= state.max_daily_entries

    def test_C_a_larger_cap_still_has_room(self):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", status="FILLED", broker_order_id="k1")
        finally:
            conn.close()
        state = _collect(rollout=_rollout(max_daily_entries=2))
        assert state.daily_entry_count == 1
        assert state.daily_entry_count < state.max_daily_entries

    def test_D_a_previous_trading_days_entry_does_not_count(self):
        yesterday = us_trading_day(NOW - timedelta(days=1))
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", status="FILLED",
                     trading_date=yesterday, broker_order_id="k1")
        finally:
            conn.close()
        assert _collect().daily_entry_count == 0

    def test_E_a_korean_date_rollover_does_not_reset_the_count(self):
        """23:00 UTC on Aug 7 is Aug 8 in Korea and in UTC, but still the
        Aug 7 US trading day. The count must not reset there."""
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", status="FILLED", broker_order_id="k1")
        finally:
            conn.close()
        later = datetime(2026, 8, 7, 23, 0, tzinfo=timezone.utc)  # 19:00 ET, same US day
        assert us_trading_day(later) == TODAY
        conn = state_db.open_db()
        try:
            state = entry_limits.collect(
                broker=_Broker(), conn=conn, rollout=_rollout(), now=later)
        finally:
            conn.close()
        assert state.trading_day == TODAY
        assert state.daily_entry_count == 1

    def test_the_next_us_trading_day_starts_a_fresh_count(self):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", status="FILLED", broker_order_id="k1")
        finally:
            conn.close()
        tomorrow = NOW + timedelta(days=1)
        conn = state_db.open_db()
        try:
            state = entry_limits.collect(
                broker=_Broker(), conn=conn, rollout=_rollout(), now=tomorrow)
        finally:
            conn.close()
        assert state.trading_day != TODAY
        assert state.daily_entry_count == 0

    def test_F_an_unreadable_ledger_fails_closed(self):
        class _Broken:
            def execute(self, *args, **kwargs):
                raise RuntimeError("database is locked")

        with pytest.raises(EntryLimitStateUnavailable) as excinfo:
            entry_limits.collect(broker=_Broker(), conn=_Broken(),
                                 rollout=_rollout(), now=NOW)
        assert excinfo.value.reason_code in (POSITION_LIMIT_STATE_UNKNOWN,
                                             DAILY_ENTRY_STATE_UNKNOWN)

    def test_a_pre_transport_rejection_does_not_consume_the_day(self):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", status="REJECTED")
        finally:
            conn.close()
        assert _collect().daily_entry_count == 0

    def test_the_attempt_being_evaluated_does_not_count_itself(self):
        """The live path registers its row BEFORE the gate runs. Without
        the exclusion the first candidate of the day would block itself."""
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-me", status="CREATED")
        finally:
            conn.close()
        assert _collect().daily_entry_count == 1
        assert _collect(exclude_internal_order_id="ord-me").daily_entry_count == 0


# ---------------------------------------------------------------------
# §16 -- UNKNOWN keeps its slot
# ---------------------------------------------------------------------
class TestUnknownOrders:
    def test_an_unknown_attempt_keeps_its_daily_slot(self):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", status="UNKNOWN")
        finally:
            conn.close()
        assert _collect().daily_entry_count == 1

    def test_an_unknown_attempt_keeps_its_position_slot(self):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-1", symbol="MSFT", status="UNKNOWN")
        finally:
            conn.close()
        state = _collect()
        assert state.pending_entry_count == 1
        assert state.effective_position_count >= state.max_open_positions

    def test_an_unknown_attempt_blocks_a_DIFFERENT_symbol(self):
        """The specific hazard: candidate B must not enter while A's
        outcome is unresolved."""
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-A", symbol="AAAA", status="UNKNOWN")
        finally:
            conn.close()
        state = _collect()
        assert "AAAA" in state.pending_entry_symbols
        assert state.effective_position_count >= state.max_open_positions


# ---------------------------------------------------------------------
# §15 -- durable across a crash
# ---------------------------------------------------------------------
class TestCrashPersistence:
    def test_a_slot_taken_before_a_crash_is_still_taken_after_restart(self):
        """The reservation is the idempotency row, written before any
        network call. A fresh process (new connection) must see it."""
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-A", symbol="AAAA", status="CREATED")
        finally:
            conn.close()  # simulate the process dying here

        # Restart: a brand-new connection, nothing carried in memory.
        state = _collect()
        assert state.pending_entry_count == 1
        assert state.daily_entry_count == 1
        assert state.effective_position_count >= state.max_open_positions


# ---------------------------------------------------------------------
# §17 -- configuration validation
# ---------------------------------------------------------------------
class TestConfigValidation:
    @pytest.mark.parametrize("field", ["max_open_positions", "max_daily_entries"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_limits_are_rejected_by_the_config(self, field, value):
        with pytest.raises(LiveRolloutConfigError):
            _rollout(**{field: value}).validate()

    @pytest.mark.parametrize("field", ["max_open_positions", "max_daily_entries"])
    @pytest.mark.parametrize("value", [True, False, 1.0, "1", None, [1]])
    def test_polluted_types_are_rejected_by_the_config(self, field, value):
        with pytest.raises(LiveRolloutConfigError):
            _rollout(**{field: value}).validate()

    @pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1", None])
    def test_the_collector_refuses_a_bad_limit_even_if_config_is_bypassed(self, value):
        """bool is an int subclass; True must never read as a cap of 1."""
        class _Rollout:
            max_open_positions = value
            max_daily_entries = 1

        with pytest.raises(EntryLimitStateUnavailable) as excinfo:
            _collect(rollout=_Rollout())
        assert excinfo.value.reason_code == POSITION_LIMIT_STATE_UNKNOWN


# ---------------------------------------------------------------------
# Observability (§19)
# ---------------------------------------------------------------------
class TestAuditPayload:
    def test_it_reports_every_number_the_operator_needs(self):
        state = _collect(broker=_Broker([_Position("MSFT", 3)]))
        payload = state.as_audit_payload()
        assert set(payload) == {
            "max_open_positions", "open_positions", "pending_entries",
            "effective_positions", "max_daily_entries", "daily_entries",
            "trading_day",
            # Added with the per-strategy cap. An operator reading this
            # payload to decide whether an entry is possible needs to
            # know WHOSE slots are in use, not only how many.
            "max_positions_per_strategy", "strategy_positions", "unattributed"}
        assert payload["trading_day"] == TODAY

    def test_the_payload_carries_no_identifiers(self):
        conn = state_db.open_db()
        try:
            _attempt(conn, internal_order_id="ord-secret", broker_order_id="kis-secret")
        finally:
            conn.close()
        text = repr(_collect().as_audit_payload())
        assert "ord-secret" not in text
        assert "kis-secret" not in text


class TestStateArithmetic:
    def _state(self, **overrides):
        kwargs = dict(
            max_open_positions=1, max_positions_per_strategy=1, max_daily_entries=1,
            open_position_symbols=frozenset(), pending_entry_symbols=frozenset(),
            daily_entry_count=0, trading_day=TODAY)
        kwargs.update(overrides)
        return EntryLimitState(**kwargs)

    def test_effective_is_a_union_not_a_sum(self):
        state = self._state(open_position_symbols=frozenset({"A", "B"}),
                            pending_entry_symbols=frozenset({"B", "C"}))
        assert state.open_position_count == 2
        assert state.pending_entry_count == 2
        assert state.effective_position_count == 3  # not 4

    def test_reason_codes_are_distinct(self):
        assert len({MAX_OPEN_POSITIONS, MAX_DAILY_ENTRIES,
                    POSITION_LIMIT_STATE_UNKNOWN, DAILY_ENTRY_STATE_UNKNOWN}) == 4
