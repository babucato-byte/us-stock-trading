"""Stage 3: strategy interface, registry, and the VWAP_MICRO_PULLBACK_MOMENTUM_V1
plugin.

No real network calls, no live 1-minute bar polling -- every bar DataFrame
here is constructed in-process. Nothing under this file touches
order_history.csv / universe.csv / strategy_performance.csv or any kill
switch / credential file.
"""

import pandas as pd
import pytest

from scalping_watchlist.models import UNKNOWN
from strategy.interface import (
    STATE_ENTRY_SIGNAL,
    STATE_NO_SETUP,
    EvaluationResult,
    TradingStrategy,
)
from strategy.plugins.vwap_micro_pullback_v1 import STRATEGY_ID, VWAPMicroPullbackV1
from strategy.registry import (
    StrategyNotActiveError,
    StrategyRegistrationError,
    StrategyRegistry,
)
from strategy.status import (
    ACTIVE,
    BACKTESTED,
    COLLECTED,
    LIMITED_LIVE_APPROVED,
    PAPER_APPROVED,
    PAUSED,
    REJECTED,
    REVIEWED,
    STRUCTURED,
    VALID_STATUSES,
)


# ---------------------------------------------------------------------------
# Helpers: constructed bar fixtures (no network, no real market data)
# ---------------------------------------------------------------------------

def _bar(close, *, open_=None, high=None, low=None, volume=50_000):
    open_ = close - 0.1 if open_ is None else open_
    high = close + 0.2 if high is None else high
    low = close - 0.3 if low is None else low
    return dict(Open=open_, High=high, Low=low, Close=close, Volume=volume)


def _setup_present_bars():
    """Warmup (EMA21 needs >=21 bars) -> rally -> shallow pullback with
    declining volume -> breakout bar with volume re-expansion. Matches the
    VWAP_MICRO_PULLBACK_MOMENTUM_V1 setup end to end."""
    rows = []
    price = 95.0
    for _ in range(15):  # warmup
        price += 0.35
        rows.append(_bar(price, volume=50_000))
    for i in range(13):  # rally: clear run-up, rising volume
        price += 1.0
        rows.append(_bar(price, volume=120_000 + i * 2_000))
    # shallow pullback, declining volume
    rally_end_price = rows[-1]["Close"]
    pullback_prices = [rally_end_price - 0.4, rally_end_price - 0.5, rally_end_price - 0.6]
    for p in pullback_prices:
        rows.append(_bar(p, volume=40_000))
    breakout_close = max(r["Close"] for r in rows) + 0.6
    rows.append(_bar(breakout_close, volume=90_000))
    return pd.DataFrame(rows)


def _no_trend_bars():
    """Flat/declining prices: price stays below (or barely at) VWAP and
    EMA9 never rises above EMA21 -- the trend/momentum filter fails."""
    rows = []
    price = 100.0
    for _ in range(30):
        price -= 0.05
        rows.append(_bar(price, volume=50_000))
    return pd.DataFrame(rows)


def _no_pullback_bars():
    """A strong, uninterrupted rally straight into the "breakout" bar --
    trend/momentum condition passes, but there is no pullback structure at
    all, so NO_PULLBACK_STRUCTURE must fire."""
    rows = []
    price = 95.0
    for _ in range(15):
        price += 0.35
        rows.append(_bar(price, volume=50_000))
    for _ in range(17):
        price += 0.5
        rows.append(_bar(price, volume=90_000))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# StrategyRegistry: validation at registration time
# ---------------------------------------------------------------------------

def test_register_valid_strategy_succeeds():
    registry = StrategyRegistry()
    strategy = VWAPMicroPullbackV1(status=STRUCTURED)

    registry.register(strategy)

    assert registry.get(STRATEGY_ID) is strategy
    assert registry.list_all() == [strategy]


@pytest.mark.parametrize("bad_strategy_id", ["", None, 123, "   "])
def test_construct_rejects_invalid_strategy_id(bad_strategy_id):
    with pytest.raises(ValueError):
        _ConcreteStrategy(strategy_id=bad_strategy_id, version="1.0.0", status=STRUCTURED)


@pytest.mark.parametrize("bad_version", ["", None, 42, "   "])
def test_construct_rejects_invalid_version(bad_version):
    with pytest.raises(ValueError):
        _ConcreteStrategy(strategy_id="X", version=bad_version, status=STRUCTURED)


@pytest.mark.parametrize("bad_status", ["", None, "NOT_A_REAL_STATUS", 1, "active"])
def test_construct_rejects_invalid_status(bad_status):
    with pytest.raises(ValueError):
        _ConcreteStrategy(strategy_id="X", version="1.0.0", status=bad_status)


def test_register_rejects_duplicate_strategy_id():
    registry = StrategyRegistry()
    registry.register(_ConcreteStrategy(strategy_id="DUP", version="1.0.0", status=STRUCTURED))

    with pytest.raises(StrategyRegistrationError):
        registry.register(_ConcreteStrategy(strategy_id="DUP", version="2.0.0", status=STRUCTURED))


def test_register_rejects_non_strategy_object():
    registry = StrategyRegistry()
    with pytest.raises(StrategyRegistrationError):
        registry.register(object())


# ---------------------------------------------------------------------------
# At most one ACTIVE strategy at a time
# ---------------------------------------------------------------------------

def test_registering_second_active_strategy_is_rejected():
    registry = StrategyRegistry()
    registry.register(_ConcreteStrategy(strategy_id="FIRST", version="1.0.0", status=ACTIVE))

    with pytest.raises(StrategyRegistrationError):
        registry.register(_ConcreteStrategy(strategy_id="SECOND", version="1.0.0", status=ACTIVE))

    # Deterministic policy: the first ACTIVE strategy is untouched, nothing
    # was silently deactivated on its behalf.
    assert registry.get_active_strategy().strategy_id == "FIRST"
    assert registry.get("SECOND") is None


def test_activate_second_strategy_while_one_active_is_rejected():
    registry = StrategyRegistry()
    registry.register(_ConcreteStrategy(strategy_id="FIRST", version="1.0.0", status=ACTIVE))
    registry.register(_ConcreteStrategy(strategy_id="SECOND", version="1.0.0", status=PAPER_APPROVED))

    with pytest.raises(StrategyRegistrationError):
        registry.activate("SECOND")

    assert registry.get_active_strategy().strategy_id == "FIRST"
    assert registry.get("SECOND").status == PAPER_APPROVED


def test_activate_same_already_active_strategy_is_a_noop():
    registry = StrategyRegistry()
    registry.register(_ConcreteStrategy(strategy_id="FIRST", version="1.0.0", status=ACTIVE))

    registry.activate("FIRST")  # must not raise

    assert registry.get_active_strategy().strategy_id == "FIRST"


def test_activate_unknown_strategy_id_raises():
    registry = StrategyRegistry()
    with pytest.raises(StrategyRegistrationError):
        registry.activate("NO_SUCH_STRATEGY")


# ---------------------------------------------------------------------------
# require_active() / select_strategy_for_order(): the order-generation guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "blocked_status",
    [COLLECTED, STRUCTURED, REVIEWED, BACKTESTED, PAUSED, REJECTED],
)
def test_require_active_blocks_non_active_statuses(blocked_status):
    registry = StrategyRegistry()
    registry.register(_ConcreteStrategy(strategy_id="X", version="1.0.0", status=blocked_status))

    with pytest.raises(StrategyNotActiveError):
        registry.require_active("X")


@pytest.mark.parametrize("blocked_status", [PAPER_APPROVED, LIMITED_LIVE_APPROVED])
def test_require_active_blocks_paper_and_limited_live_approved(blocked_status):
    """Roadmap: 'ACTIVE 이전 전략은 주문을 생성할 수 없다' -- PAPER_APPROVED and
    LIMITED_LIVE_APPROVED are both further along than COLLECTED/STRUCTURED/
    etc, but neither is ACTIVE, so both must still be blocked."""
    registry = StrategyRegistry()
    registry.register(_ConcreteStrategy(strategy_id="X", version="1.0.0", status=blocked_status))

    with pytest.raises(StrategyNotActiveError):
        registry.require_active("X")


def test_require_active_passes_for_active_strategy():
    registry = StrategyRegistry()
    strategy = _ConcreteStrategy(strategy_id="X", version="1.0.0", status=ACTIVE)
    registry.register(strategy)

    assert registry.require_active("X") is strategy
    assert registry.select_strategy_for_order("X") is strategy


def test_require_active_unknown_strategy_id_raises():
    registry = StrategyRegistry()
    with pytest.raises(StrategyNotActiveError):
        registry.require_active("NO_SUCH_STRATEGY")


# ---------------------------------------------------------------------------
# Registry lookup / listing
# ---------------------------------------------------------------------------

def test_get_active_strategy_returns_none_when_nothing_active():
    registry = StrategyRegistry()
    registry.register(_ConcreteStrategy(strategy_id="X", version="1.0.0", status=STRUCTURED))

    assert registry.get_active_strategy() is None


def test_get_active_strategy_returns_the_active_one():
    registry = StrategyRegistry()
    registry.register(_ConcreteStrategy(strategy_id="A", version="1.0.0", status=STRUCTURED))
    active = _ConcreteStrategy(strategy_id="B", version="1.0.0", status=ACTIVE)
    registry.register(active)
    registry.register(_ConcreteStrategy(strategy_id="C", version="1.0.0", status=REJECTED))

    assert registry.get_active_strategy() is active


def test_list_all_and_get_by_id():
    registry = StrategyRegistry()
    a = _ConcreteStrategy(strategy_id="A", version="1.0.0", status=STRUCTURED)
    b = _ConcreteStrategy(strategy_id="B", version="1.0.0", status=REVIEWED)
    registry.register(a)
    registry.register(b)

    assert registry.get("A") is a
    assert registry.get("B") is b
    assert registry.get("NOPE") is None
    assert set(registry.list_all()) == {a, b}


# ---------------------------------------------------------------------------
# VWAP_MICRO_PULLBACK_MOMENTUM_V1 plugin
# ---------------------------------------------------------------------------

def test_vwap_plugin_signals_entry_when_setup_present():
    strategy = VWAPMicroPullbackV1()
    bars = _setup_present_bars()

    result = strategy.evaluate_setup(bars, symbol="TEST")

    assert result.state == STATE_ENTRY_SIGNAL
    assert result.signal is True
    assert result.rejection_reasons == ""


def test_vwap_plugin_no_signal_when_vwap_ema_condition_fails():
    strategy = VWAPMicroPullbackV1()
    bars = _no_trend_bars()

    result = strategy.evaluate_setup(bars, symbol="TEST")

    assert result.state == STATE_NO_SETUP
    assert result.signal is False
    assert "PRICE_NOT_ABOVE_VWAP" in result.rejection_reasons
    assert "EMA9_NOT_ABOVE_EMA21" in result.rejection_reasons


def test_vwap_plugin_no_signal_when_no_pullback():
    strategy = VWAPMicroPullbackV1()
    bars = _no_pullback_bars()

    result = strategy.evaluate_setup(bars, symbol="TEST")

    assert result.state == STATE_NO_SETUP
    assert result.signal is False
    assert "NO_PULLBACK_STRUCTURE" in result.rejection_reasons


def test_vwap_plugin_no_signal_with_insufficient_bars():
    strategy = VWAPMicroPullbackV1()
    bars = pd.DataFrame([_bar(100.0 + i) for i in range(3)])

    result = strategy.evaluate_setup(bars, symbol="TEST")

    assert result.state == STATE_NO_SETUP
    assert result.signal is False
    assert "INSUFFICIENT_BARS" in result.rejection_reasons


def test_vwap_plugin_generate_entry_populates_stop_and_targets():
    strategy = VWAPMicroPullbackV1()
    bars = _setup_present_bars()

    result = strategy.generate_entry(bars, symbol="TEST")

    assert result.signal is True
    assert isinstance(result.entry_price, float)
    assert result.stop_price < result.entry_price  # stop below entry for a long
    assert result.target_1 > result.entry_price     # targets above entry
    assert result.target_2 > result.target_1
    expected_risk = result.entry_price - result.stop_price
    assert result.risk_per_share == pytest.approx(expected_risk)
    # 1R math: target_1 sits exactly one risk_per_share above entry.
    assert result.target_1 == pytest.approx(result.entry_price + expected_risk)


def test_vwap_plugin_generate_entry_returns_no_signal_result_when_no_setup():
    strategy = VWAPMicroPullbackV1()
    bars = _no_trend_bars()

    result = strategy.generate_entry(bars, symbol="TEST")

    assert result.signal is False
    assert result.entry_price == UNKNOWN  # left at sentinel, nothing computed


def test_vwap_plugin_calculate_stop_and_targets_sanity():
    strategy = VWAPMicroPullbackV1()

    targets = strategy.calculate_targets(entry_price=100.0, stop_price=99.0)

    assert targets["risk_per_share"] == pytest.approx(1.0)
    assert targets["target_1"] == pytest.approx(101.0)  # 1R
    assert targets["target_2"] > targets["target_1"]
    assert 0.0 < targets["partial_exit_fraction_at_target_1"] <= 1.0


def test_vwap_plugin_calculate_targets_rejects_stop_above_entry():
    strategy = VWAPMicroPullbackV1()
    with pytest.raises(ValueError):
        strategy.calculate_targets(entry_price=100.0, stop_price=100.0)


def test_vwap_plugin_manage_position_is_still_a_stage4_stub():
    # manage_position() is not overridden by the VWAP plugin -- position
    # management is positions/lifecycle.py's job (Stage 4), driven by the
    # already-computed stop/target prices captured at entry time, not by
    # a per-strategy manage_position() dispatch.
    strategy = VWAPMicroPullbackV1()
    with pytest.raises(NotImplementedError):
        strategy.manage_position(position_state={}, latest_bar={})


def test_vwap_plugin_invalidate_is_implemented_in_stage4():
    # invalidate() IS overridden (Stage 4, roadmap Phase 5): a position's
    # entry thesis (price above VWAP) is invalidated once the latest bar
    # closes back below VWAP. Note the real signature -- (bars, *, symbol)
    # -- intentionally differs from the base class's NotImplementedError
    # placeholder signature (evaluation, reason); see the docstring on
    # VWAPMicroPullbackV1.invalidate() for why.
    strategy = VWAPMicroPullbackV1()

    # A steady climb: the latest close stays above VWAP -- not invalidated.
    healthy = pd.DataFrame([_bar(c, volume=50_000) for c in [100, 101, 102, 103, 104]])
    assert strategy.invalidate(healthy, symbol="AAPL") is False

    # A sharp drop that closes the latest bar below VWAP: invalidated.
    broken = pd.DataFrame([_bar(c, volume=50_000) for c in [100, 101, 102, 103, 90]])
    assert strategy.invalidate(broken, symbol="AAPL") is True


# ---------------------------------------------------------------------------
# End-to-end: registry + guard + plugin, matching how Stage 4 will use this
# ---------------------------------------------------------------------------

def test_only_active_registered_vwap_strategy_may_produce_an_order():
    registry = StrategyRegistry()
    strategy = VWAPMicroPullbackV1(status=BACKTESTED)
    registry.register(strategy)

    with pytest.raises(StrategyNotActiveError):
        registry.select_strategy_for_order(STRATEGY_ID)

    registry.activate(STRATEGY_ID)
    assert registry.select_strategy_for_order(STRATEGY_ID) is strategy


# ---------------------------------------------------------------------------
# Minimal concrete TradingStrategy for interface/registry-only tests, so
# these tests don't depend on the VWAP plugin's actual setup logic.
# ---------------------------------------------------------------------------

class _ConcreteStrategy(TradingStrategy):
    def evaluate_setup(self, bars, *, symbol, as_of=None):
        return EvaluationResult(
            strategy_id=self.strategy_id,
            symbol=symbol,
            evaluated_at=as_of or "2026-01-01T00:00:00+00:00",
            state=STATE_NO_SETUP,
            signal=False,
        )

    def generate_entry(self, bars, *, symbol, as_of=None):
        return self.evaluate_setup(bars, symbol=symbol, as_of=as_of)

    def calculate_stop(self, bars, *, entry_price):
        return entry_price - 1.0

    def calculate_targets(self, *, entry_price, stop_price):
        risk = entry_price - stop_price
        return {"risk_per_share": risk, "target_1": entry_price + risk, "target_2": entry_price + 2 * risk}
