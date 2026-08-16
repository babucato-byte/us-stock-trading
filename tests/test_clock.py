"""CODEX-030: Clock protocol tests -- ProductionClock/FrozenClock
themselves, plus check_and_manage()'s EOD/session boundary behavior driven
entirely by injected clocks/`now` rather than the real wall clock."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import paper_strategy_order as pso
from clock import DEFAULT_CLOCK, Clock, FrozenClock, ProductionClock
from config import scalping_strategy_v1_config as cfg
from market_hours import EASTERN, MARKET_REGULAR_END, combine_eastern
from positions import lifecycle, states, store
from strategy.interface import STATE_ENTRY_SIGNAL, STATE_NO_SETUP, EvaluationResult, TradingStrategy
from strategy.registry import StrategyRegistry
from strategy.status import ACTIVE

TODAY = pso.eastern_now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Clock protocol itself
# ---------------------------------------------------------------------------

def test_production_clock_returns_tz_aware_now():
    clock = ProductionClock()
    assert clock.now_utc().tzinfo is not None
    assert clock.now_eastern().tzinfo is not None
    assert clock.market_date() == clock.now_eastern().date()


def test_default_clock_is_a_production_clock():
    assert isinstance(DEFAULT_CLOCK, ProductionClock)


def test_frozen_clock_returns_fixed_value_regardless_of_real_time():
    fixed = datetime(2026, 7, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    clock = FrozenClock(now_eastern=fixed)
    assert clock.now_eastern() == fixed
    assert clock.now_eastern() == clock.now_eastern()  # repeated calls: identical
    assert clock.market_date() == fixed.date()


def test_frozen_clock_from_utc_converts_to_eastern():
    fixed_utc = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)  # 11:00 ET (DST, UTC-4)
    clock = FrozenClock(now_utc=fixed_utc)
    assert clock.now_eastern().hour == 11
    assert clock.now_eastern().tzinfo is not None


def test_frozen_clock_rejects_naive_datetime():
    with pytest.raises(ValueError):
        FrozenClock(now_eastern=datetime(2026, 7, 15, 11, 0))


def test_frozen_clock_requires_at_least_one_value():
    with pytest.raises(ValueError):
        FrozenClock()


def test_clock_abstract_methods_raise_not_implemented():
    clock = Clock()
    with pytest.raises(NotImplementedError):
        clock.now_utc()
    with pytest.raises(NotImplementedError):
        clock.now_eastern()


# ---------------------------------------------------------------------------
# check_and_manage(): EOD/session boundary via injected now/clock
# ---------------------------------------------------------------------------

class FakeBrokerResponse:
    def __init__(self, status_code=200, text="OK", data=None, dry_run=False):
        self.status_code = status_code
        self.text = text
        self.data = data
        self.dry_run = dry_run


class FakeBroker:
    def __init__(self):
        self.submit_calls = []

    def submit_order(self, symbol, qty=1, *, side, client_order_id=None):
        self.submit_calls.append((symbol, qty, side, client_order_id))
        return FakeBrokerResponse(data={
            "status": "filled", "filled_qty": qty, "filled_avg_price": None,
            "id": f"broker-{client_order_id}",
        })

    def get_order_by_client_order_id(self, client_order_id):
        return None


class FakeStrategy(TradingStrategy):
    def __init__(self, stop_price=95.0, target_1=105.0, target_2=110.0):
        super().__init__(strategy_id="CLOCK_TEST_STRATEGY", version="1.0.0", status=ACTIVE)
        self.stop_price = stop_price
        self.target_1 = target_1
        self.target_2 = target_2

    def evaluate_setup(self, bars, *, symbol, as_of=None):
        return self.generate_entry(bars, symbol=symbol, as_of=as_of)

    def generate_entry(self, bars, *, symbol, as_of=None):
        return EvaluationResult(
            strategy_id=self.strategy_id, symbol=symbol, evaluated_at="2026-07-15T15:00:00+00:00",
            state=STATE_ENTRY_SIGNAL, signal=True,
            stop_price=self.stop_price, target_1=self.target_1, target_2=self.target_2,
        )

    def calculate_stop(self, bars, *, entry_price):
        return self.stop_price

    def calculate_targets(self, *, entry_price, stop_price):
        return {"target_1": self.target_1, "target_2": self.target_2}

    def invalidate(self, bars, *, symbol):
        return False


@pytest.fixture(autouse=True)
def _isolate_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL_SWITCH"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH_STATE.json"))
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()
    yield


def _open_position(qty=10, stop_price=95.0, target_1=105.0, target_2=110.0):
    strategy = FakeStrategy(stop_price=stop_price, target_1=target_1, target_2=target_2)
    registry = StrategyRegistry()
    registry.register(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])
    record = lifecycle.enter_position(
        strategy, "AAPL", bars, qty=qty, order_date=TODAY, broker=broker, registry=registry,
    )
    record = lifecycle.record_fill(record["position_id"], filled_qty=qty, average_fill_price=100.0)
    return record, broker


# A fixed, unambiguous Wednesday with DST in effect and no US market holiday.
SESSION_DATE = datetime(2026, 7, 15).date()


def _et(hour, minute=0):
    return datetime(SESSION_DATE.year, SESSION_DATE.month, SESSION_DATE.day, hour, minute, tzinfo=EASTERN)


def _pin_entry_time_shortly_before(position_id, now):
    """Every point-in-time test below uses a `now` that may be far from
    whatever real moment enter_position() actually ran at (SESSION_DATE is
    a fixed 2026-07-15, not "today") -- without pinning entry_time to
    something close to that `now`, held_minutes could appear enormous (or
    deeply negative) and TIME_STOP (checked before STOP_LOSS/EOD) could
    fire instead of the scenario under test, or mask it entirely."""
    with store.locked_position(position_id) as locked:
        locked["entry_time"] = (now - timedelta(minutes=1)).isoformat()


def test_regular_session_time_triggers_price_based_exit_not_eod():
    record, broker = _open_position()
    _pin_entry_time_shortly_before(record["position_id"], _et(11, 0))
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=_et(11, 0), broker=broker)
    assert updated["exit_reason"] == "STOP_LOSS"


def test_just_before_eod_cutoff_does_not_force_close():
    record, broker = _open_position()
    cutoff = combine_eastern(SESSION_DATE, MARKET_REGULAR_END) - timedelta(minutes=cfg.EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE)
    just_before = cutoff - timedelta(minutes=1)
    _pin_entry_time_shortly_before(record["position_id"], just_before)
    updated = lifecycle.check_and_manage(record["position_id"], current_price=100.0, now=just_before, broker=broker)
    assert updated["state"] == states.STOP_ACTIVE  # no action -- price is between stop and target_1


def test_eod_cutoff_exactly_forces_close():
    record, broker = _open_position()
    cutoff = combine_eastern(SESSION_DATE, MARKET_REGULAR_END) - timedelta(minutes=cfg.EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE)
    _pin_entry_time_shortly_before(record["position_id"], cutoff)
    updated = lifecycle.check_and_manage(record["position_id"], current_price=100.0, now=cutoff, broker=broker)
    assert updated["exit_reason"] == "EOD_FORCED_CLOSE"


def test_after_eod_cutoff_forces_close():
    record, broker = _open_position()
    cutoff = combine_eastern(SESSION_DATE, MARKET_REGULAR_END) - timedelta(minutes=cfg.EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE)
    after = cutoff + timedelta(minutes=1)
    _pin_entry_time_shortly_before(record["position_id"], after)
    updated = lifecycle.check_and_manage(record["position_id"], current_price=100.0, now=after, broker=broker)
    assert updated["exit_reason"] == "EOD_FORCED_CLOSE"


def test_premarket_time_does_not_trigger_eod():
    record, broker = _open_position()
    _pin_entry_time_shortly_before(record["position_id"], _et(6, 0))
    updated = lifecycle.check_and_manage(record["position_id"], current_price=100.0, now=_et(6, 0), broker=broker)
    assert updated["state"] == states.STOP_ACTIVE  # premarket hour, well before any EOD cutoff


def test_holiday_date_eod_cutoff_still_computed_from_supplied_now_not_real_calendar():
    """check_and_manage() itself has no holiday gate (that belongs to entry-side
    session gating) -- an explicitly injected `now` on a market holiday still
    resolves deterministically off the supplied date, never off whether today
    happens to be a holiday in the real calendar."""
    record, broker = _open_position()
    independence_day = datetime(2026, 7, 4, 10, 0, tzinfo=EASTERN)  # US market holiday
    _pin_entry_time_shortly_before(record["position_id"], independence_day)
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=independence_day, broker=broker)
    assert updated["exit_reason"] == "STOP_LOSS"  # deterministic off the injected `now`, not a holiday gate


def test_dst_spring_forward_boundary_uses_correct_utc_offset():
    # 2026-03-08 02:00 ET does not exist (US DST begins 2026-03-08); pick a
    # moment shortly after the spring-forward transition and confirm the
    # UTC offset used for the EOD cutoff computation is the DST offset (-4).
    record, broker = _open_position()
    after_spring_forward = datetime(2026, 3, 8, 11, 0, tzinfo=EASTERN)
    assert after_spring_forward.utcoffset() == timedelta(hours=-4)
    _pin_entry_time_shortly_before(record["position_id"], after_spring_forward)
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=after_spring_forward, broker=broker)
    assert updated["exit_reason"] == "STOP_LOSS"


def test_dst_fall_back_boundary_uses_correct_utc_offset():
    record, broker = _open_position()
    after_fall_back = datetime(2026, 11, 2, 11, 0, tzinfo=EASTERN)  # US DST ends 2026-11-01
    assert after_fall_back.utcoffset() == timedelta(hours=-5)
    _pin_entry_time_shortly_before(record["position_id"], after_fall_back)
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=after_fall_back, broker=broker)
    assert updated["exit_reason"] == "STOP_LOSS"


def test_utc_date_can_differ_from_eastern_date_at_session_boundary():
    # 2026-07-16 02:00 UTC is 2026-07-15 22:00 ET -- UTC date is one day
    # ahead of the Eastern "market date" this moment belongs to.
    late_et = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
    clock = FrozenClock(now_utc=late_et)
    assert clock.now_utc().date() == datetime(2026, 7, 16).date()
    assert clock.market_date() == datetime(2026, 7, 15).date()


def test_repeated_check_and_manage_calls_with_same_now_are_idempotent():
    record, broker = _open_position()
    fixed = _et(11, 0)
    _pin_entry_time_shortly_before(record["position_id"], fixed)
    first = lifecycle.check_and_manage(record["position_id"], current_price=100.0, now=fixed, broker=broker)
    second = lifecycle.check_and_manage(record["position_id"], current_price=100.0, now=fixed, broker=broker)
    assert first["state"] == second["state"] == states.STOP_ACTIVE
    assert [c for c in broker.submit_calls if c[2] == "sell"] == []


def test_check_and_manage_result_independent_of_real_system_clock(monkeypatch):
    """Patch market_hours.eastern_now (what DEFAULT_CLOCK.now_eastern would
    otherwise reach) to an EOD-window value, and confirm an explicit `now`
    still wins -- proving the result depends only on the injected clock/now,
    never on whatever the real system clock reads."""
    import market_hours

    def _poisoned_eastern_now(now=None):
        return combine_eastern(SESSION_DATE, MARKET_REGULAR_END) - timedelta(minutes=1)

    monkeypatch.setattr(market_hours, "eastern_now", _poisoned_eastern_now)
    record, broker = _open_position()
    _pin_entry_time_shortly_before(record["position_id"], _et(11, 0))
    updated = lifecycle.check_and_manage(record["position_id"], current_price=100.0, now=_et(11, 0), broker=broker)
    assert updated["state"] == states.STOP_ACTIVE  # not EOD_FORCED_CLOSE, despite the poisoned wall clock


def test_check_and_manage_rejects_naive_now():
    record, broker = _open_position()
    naive = datetime(2026, 7, 15, 11, 0)
    with pytest.raises(lifecycle.PositionLifecycleError):
        lifecycle.check_and_manage(record["position_id"], current_price=100.0, now=naive, broker=broker)


def test_check_and_manage_uses_injected_clock_when_now_omitted():
    record, broker = _open_position()
    _pin_entry_time_shortly_before(record["position_id"], _et(11, 0))
    frozen = FrozenClock(now_eastern=_et(11, 0))
    updated = lifecycle.check_and_manage(record["position_id"], current_price=100.0, clock=frozen, broker=broker)
    assert updated["state"] == states.STOP_ACTIVE


# ---------------------------------------------------------------------------
# CODEX-030's originally-reported 4 failures: pinned down explicitly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("current_price,expected_state,expected_reason", [
    (105.0, states.PARTIAL_EXITED, None),
    (94.0, states.CLOSED, "STOP_LOSS"),
    (100.0, states.STOP_ACTIVE, None),
])
def test_target_stop_no_action_are_stable_regardless_of_wall_clock(current_price, expected_state, expected_reason):
    """The 4 CODEX-030 failures were target/stop/no-action tests turning
    into EOD_FORCED_CLOSE depending on when the suite ran. Pinned here with
    an explicit mid-session `now` covering the exact three scenarios that
    failed (target hit, stop hit, no action)."""
    record, broker = _open_position()
    _pin_entry_time_shortly_before(record["position_id"], _et(11, 0))
    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=current_price, now=_et(11, 0), broker=broker,
    )
    assert updated["state"] == expected_state
    if expected_reason:
        assert updated["exit_reason"] == expected_reason
