"""S1 <= 1, S6 <= 1, global <= 2 -- the whole matrix.

Two different questions, both pinned here because a per-strategy cap can
fail in two unrelated ways:

  * COUNTING -- does a row in `s6_positions` with status EXIT_SUBMITTED
    still occupy S6's slot? (It must: the shares are held until the exit
    actually fills.)
  * ENFORCING -- given those counts, does the gate refuse the right
    candidate with the right reason code?

The counting half runs against a real database rather than a
hand-built `EntryLimitState`, because the defect this suite is guarding
against is precisely a status that the SQL forgets to include.
"""

from datetime import datetime, timezone

import pytest

from config import strategy_registry
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution import entry_limits, order_gate
from execution.entry_limits import EntryLimitState
from market_hours import us_trading_day
from reconciliation.snapshot import ReconciliationSnapshot
from state_store import db as state_db

NOW = datetime(2026, 8, 25, 15, 30, tzinfo=timezone.utc)
TODAY = us_trading_day(NOW)
ACCOUNT = "12345678"

S1_STRATEGY = "S1_HMA_EARLY_TREND_V1"
S6_STRATEGY = "S6_ORB_BREAKOUT_V1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POS.json"))
    from execution import idempotency

    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEM.lock")
    state_db.open_db().close()
    yield


class _Position:
    def __init__(self, symbol, quantity=1):
        self.symbol = symbol
        self.quantity = quantity


class _Broker:
    """KIS's view: symbols and quantities, never a strategy."""

    def __init__(self, symbols=()):
        self._symbols = list(symbols)

    def get_positions(self):
        return [_Position(s) for s in self._symbols]


class _Rollout:
    def __init__(self, *, max_open_positions=2, max_positions_per_strategy=1,
                 max_daily_entries=1):
        self.max_open_positions = max_open_positions
        self.max_positions_per_strategy = max_positions_per_strategy
        self.max_daily_entries = max_daily_entries


def _insert_s1(conn, symbol, status="OPEN"):
    conn.execute(
        "INSERT INTO s1_positions (position_id, symbol, strategy_id, signal_id, "
        "entry_price, quantity, status, opened_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"s1-{symbol}-{status}", symbol, "hma_early_trend", f"sig-{symbol}",
         10.0, 1, status, NOW.isoformat(), NOW.isoformat()))
    conn.commit()


def _insert_s6(conn, symbol, status="OPEN"):
    entry_price = None if status == "SUBMITTED" else 10.0
    quantity = None if status == "SUBMITTED" else 1
    conn.execute(
        "INSERT INTO s6_positions (position_id, strategy_id, symbol, quantity, "
        "entry_price, status, submitted_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"s6-{symbol}-{status}", S6_STRATEGY, symbol, quantity, entry_price,
         status, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
    conn.commit()


def _collect(broker_symbols=(), rollout=None):
    conn = state_db.open_db()
    try:
        return entry_limits.collect(
            broker=_Broker(broker_symbols), conn=conn,
            rollout=rollout or _Rollout(), now=NOW)
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------

class TestEveryLiveStatusHoldsItsSlot:
    """SUBMITTED / OPEN / EXIT_PENDING / EXIT_SUBMITTED all count.

    The two ends are the ones worth stating. SUBMITTED is an order whose
    fill is not yet confirmed -- exactly the case a cap exists to stop
    being doubled. EXIT_SUBMITTED still holds shares: a slot released
    when an exit was merely SENT would let a replacement be bought
    against a position that still exists.
    """

    @pytest.mark.parametrize("status", sorted(strategy_registry.HOLDING_STATUSES))
    def test_s6_status_occupies_the_slot(self, status):
        conn = state_db.open_db()
        try:
            _insert_s6(conn, "LGN", status)
        finally:
            conn.close()
        limits = _collect(broker_symbols=["LGN"] if status != "SUBMITTED" else [])
        assert limits.strategy_effective_count(strategy_registry.SLOT_S6) == 1

    @pytest.mark.parametrize("status", ["OPEN", "EXIT_PENDING", "EXIT_SUBMITTED"])
    def test_s1_status_occupies_the_slot(self, status):
        conn = state_db.open_db()
        try:
            _insert_s1(conn, "TX", status)
        finally:
            conn.close()
        limits = _collect(broker_symbols=["TX"])
        assert limits.strategy_effective_count(strategy_registry.SLOT_S1) == 1

    def test_closed_releases_the_slot(self):
        conn = state_db.open_db()
        try:
            _insert_s6(conn, "LGN", "CLOSED")
        finally:
            conn.close()
        limits = _collect()
        assert limits.strategy_effective_count(strategy_registry.SLOT_S6) == 0


class TestAttribution:
    def test_s1_and_s6_are_counted_separately(self):
        conn = state_db.open_db()
        try:
            _insert_s1(conn, "TX")
            _insert_s6(conn, "LGN")
        finally:
            conn.close()
        limits = _collect(broker_symbols=["TX", "LGN"])
        assert limits.strategy_effective_count(strategy_registry.SLOT_S1) == 1
        assert limits.strategy_effective_count(strategy_registry.SLOT_S6) == 1
        assert limits.effective_position_count == 2

    def test_a_broker_position_no_store_claims_counts_against_everyone(self):
        """The fail-closed direction.

        A held symbol nobody can attribute might belong to any strategy.
        Counting it against none would free capacity that is really in
        use -- the one outcome a cap must never produce.
        """
        limits = _collect(broker_symbols=["MYSTERY"])
        assert limits.unattributed_symbols == frozenset({"MYSTERY"})
        for slot in strategy_registry.LIVE_SLOTS:
            assert limits.strategy_effective_count(slot) == 1


# ---------------------------------------------------------------------
# Enforcing
# ---------------------------------------------------------------------

def _limits(**overrides):
    kwargs = dict(
        max_open_positions=2, max_daily_entries=1,
        open_position_symbols=frozenset(), pending_entry_symbols=frozenset(),
        daily_entry_count=0, trading_day=TODAY,
        max_positions_per_strategy=1, strategy_symbols={},
        unattributed_symbols=frozenset())
    kwargs.update(overrides)
    return EntryLimitState(**kwargs)


def _state(*, s1=(), s6=()):
    """The account as the caps see it: who holds what."""
    held = frozenset(s1) | frozenset(s6)
    return _limits(
        open_position_symbols=held,
        strategy_symbols={
            strategy_registry.SLOT_S1: frozenset(s1),
            strategy_registry.SLOT_S6: frozenset(s6),
            strategy_registry.SLOT_S2: frozenset(),
        })


def _ctx(strategy_id, *, limits, symbol="NEW"):
    instrument = build_instrument(symbol, exchange="NASDAQ")
    signal = build_signal(
        strategy_id=strategy_id, strategy_version="v1", config_version="c",
        code_commit="c1", symbol=symbol, exchange="NASDAQ", signal_price=100.0,
        score=99, entry_reason="test", valid_for_seconds=300, now=NOW)
    intent = OrderIntent(
        internal_order_id="ord-1", signal_id=signal.signal_id,
        strategy_id=strategy_id, symbol=symbol, exchange="NASDAQ", side="buy",
        quantity=1, order_type="limit", limit_price=100.0, stop_price=None,
        target_price=None, created_at=NOW)
    return order_gate.BuyGateContext(
        execution_broker="kis", live_order_enabled=True, entry_disabled=False,
        validated_commit="c1", deployed_commit="c1", kis_account_no=ACCOUNT,
        allowed_account_no=ACCOUNT, order_intent=intent, instrument=instrument,
        signal=signal, is_regular_session=True, kis_price_usd=100.0,
        max_price_deviation_percent=30.0, usd_orderable_cash=10_000.0,
        has_open_order_for_symbol=False, has_order_for_signal_id=False,
        allowed_symbols=frozenset({symbol}),
        reconciliation=ReconciliationSnapshot(
            account_id=ACCOUNT, symbol=symbol, checked_at=NOW,
            positions_match=True, open_orders_match=True, fills_match=True,
            has_unknown_orders=False, source="test", detail=()),
        entry_limits=limits, now=NOW)


def _blocked_code(ctx):
    try:
        order_gate.evaluate_buy_gate(ctx)
        return None
    except order_gate.OrderGateBlockedError as exc:
        return exc.code


class TestTheAuthorisedMatrix:
    """S1 max 1, S6 max 1, global max 2 -- every cell."""

    def test_empty_account_admits_either_strategy(self):
        assert _blocked_code(_ctx(S1_STRATEGY, limits=_state())) is None
        assert _blocked_code(_ctx(S6_STRATEGY, limits=_state())) is None

    def test_s1_holding_one_admits_s6_and_refuses_s1(self):
        state = _state(s1=["TX"])
        assert _blocked_code(_ctx(S6_STRATEGY, limits=state)) is None
        assert _blocked_code(_ctx(S1_STRATEGY, limits=state)) == \
            entry_limits.MAX_STRATEGY_POSITIONS

    def test_s6_holding_one_admits_s1_and_refuses_s6(self):
        state = _state(s6=["LGN"])
        assert _blocked_code(_ctx(S1_STRATEGY, limits=state)) is None
        assert _blocked_code(_ctx(S6_STRATEGY, limits=state)) == \
            entry_limits.MAX_STRATEGY_POSITIONS

    def test_one_each_refuses_everything_on_the_global_cap(self):
        """Both strategies full AND the account full.

        The global cap is reported rather than the per-strategy one,
        because it is checked first and is the broader fact: the account
        has no room for anybody, not merely none for this strategy.
        """
        state = _state(s1=["TX"], s6=["LGN"])
        assert state.effective_position_count == 2
        for strategy in (S1_STRATEGY, S6_STRATEGY):
            assert _blocked_code(_ctx(strategy, limits=state)) == \
                entry_limits.MAX_OPEN_POSITIONS

    def test_a_third_position_is_refused_even_with_a_free_strategy_slot(self):
        """S2 has an empty slot; the account does not have a free one."""
        state = _state(s1=["TX"], s6=["LGN"])
        assert _blocked_code(
            _ctx("S2_VOLUME_ACCUMULATION_V1", limits=state)) == \
            entry_limits.MAX_OPEN_POSITIONS


class TestTheCapCannotBeSkipped:
    def test_an_unknown_strategy_is_refused(self):
        """No name, no slot, no cap. Blocking is the only honest answer:
        'unknown strategy' must not be the one input that skips a
        limit."""
        assert _blocked_code(_ctx("NOT_A_STRATEGY", limits=_state())) == \
            entry_limits.STRATEGY_ATTRIBUTION_UNKNOWN

    def test_intent_and_signal_must_agree(self):
        """Checked on one, recorded from the other -- so they must
        match, or a future count attributes this order to a strategy
        that was never capped for it."""
        ctx = _ctx(S6_STRATEGY, limits=_state())
        import dataclasses

        ctx = dataclasses.replace(
            ctx, order_intent=dataclasses.replace(
                ctx.order_intent, strategy_id=S1_STRATEGY))
        assert _blocked_code(ctx) == entry_limits.STRATEGY_ATTRIBUTION_UNKNOWN

    def test_an_unattributed_holding_blocks_every_strategy(self):
        state = _limits(
            open_position_symbols=frozenset({"MYSTERY"}),
            unattributed_symbols=frozenset({"MYSTERY"}),
            strategy_symbols={slot: frozenset()
                              for slot in strategy_registry.LIVE_SLOTS})
        assert _blocked_code(_ctx(S6_STRATEGY, limits=state)) == \
            entry_limits.MAX_STRATEGY_POSITIONS


class TestConfigRefusesAContradiction:
    def test_per_strategy_above_global_is_rejected(self):
        from config.live_rollout_config import (
            LiveRolloutConfig, LiveRolloutConfigError,
        )

        config = LiveRolloutConfig.from_env(env={
            "LIVE_ROLLOUT_MAX_POSITIONS": "1",
            "LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY": "2",
        })
        with pytest.raises(LiveRolloutConfigError) as excinfo:
            config.validate()
        assert "exceeds" in str(excinfo.value)

    def test_the_authorised_posture_validates(self):
        from config.live_rollout_config import LiveRolloutConfig

        config = LiveRolloutConfig.from_env(env={
            "LIVE_ROLLOUT_MAX_POSITIONS": "2",
            "LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY": "1",
            "LIVE_ROLLOUT_MAX_QUANTITY": "1",
            "LIVE_ROLLOUT_MAX_DAILY_ENTRIES": "1",
        })
        assert config.validate() is True
        assert config.max_open_positions == 2
        assert config.max_positions_per_strategy == 1
