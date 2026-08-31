"""Where a PAPER strategy's orders go instead of to a broker.

Before this, a non-live strategy was DISCOVERY_ONLY: signals recorded,
nothing else. That answers "did it fire?" and not "would it have made
money?", because a signal is not a trade -- and promoting a scanner on
signal counts alone means finding out with real money.

So PAPER runs the same lifecycle LIVE does: intent, fill, position,
monitoring, exit, realised PnL. Only the engine differs, which is the
point -- promotion should be a mode change, not a rewrite.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution import virtual_execution as ve  # noqa: E402

S1 = "S1_HMA_EARLY_TREND_V1"
S2 = "S2_VOLUME_ACCUMULATION_V1"
NOW = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    return ve.VirtualExecutionEngine()


@pytest.fixture
def env(tmp_path):
    return {"VIRTUAL_EXECUTION_DIR": str(tmp_path)}


def _buy(engine, symbol="NVDA", price=100.0, quantity=2, strategy=S1):
    return engine.submit_buy(strategy_id=strategy, scanner="hma_early_trend",
                             symbol=symbol, quantity=quantity,
                             decision_price=price, session="REGULAR",
                             trading_day="2026-08-31", signal_id="sig-1",
                             now=NOW)


class TestTheFullLifecycle:
    def test_a_buy_opens_a_virtual_position(self, engine):
        out = _buy(engine)
        assert out["accepted"] is True
        position = out["position"]
        assert position["status"] == ve.STATUS_OPEN
        assert position["symbol"] == "NVDA"
        assert position["quantity"] == 2
        assert position["entry_fill_price"] == pytest.approx(100.0)
        assert engine.is_open(S1, "NVDA") is True

    def test_a_sell_closes_it_and_realises_pnl(self, engine):
        _buy(engine, price=100.0, quantity=2)
        out = engine.submit_sell(strategy_id=S1, symbol="NVDA",
                                 decision_price=110.0,
                                 exit_reason="TARGET",
                                 session="REGULAR",
                                 now=NOW + timedelta(hours=1))
        position = out["position"]
        assert position["status"] == ve.STATUS_CLOSED
        assert position["realized_pnl"] == pytest.approx(20.0)
        assert position["realized_pnl_pct"] == pytest.approx(10.0)
        assert position["exit_reason"] == "TARGET"
        assert engine.is_open(S1, "NVDA") is False

    def test_a_loss_is_recorded_as_a_loss(self, engine):
        _buy(engine, price=100.0, quantity=1)
        out = engine.submit_sell(strategy_id=S1, symbol="NVDA",
                                 decision_price=95.0, exit_reason="STOP",
                                 now=NOW)
        assert out["position"]["realized_pnl"] == pytest.approx(-5.0)
        assert out["position"]["realized_pnl_pct"] == pytest.approx(-5.0)

    def test_the_position_is_monitorable_while_open(self, engine):
        _buy(engine)
        assert [p["symbol"] for p in engine.open_positions(S1)] == ["NVDA"]

    def test_every_required_field_is_persisted(self, engine):
        _buy(engine)
        out = engine.submit_sell(strategy_id=S1, symbol="NVDA",
                                 decision_price=110.0, exit_reason="TARGET",
                                 session="AFTER_HOURS", now=NOW)
        position = out["position"]
        for field in ("strategy_id", "scanner", "symbol", "side", "quantity",
                      "entry_at", "entry_session", "entry_decision_price",
                      "entry_fill_price", "exit_at", "exit_session",
                      "exit_reason", "exit_decision_price", "exit_fill_price",
                      "realized_pnl", "realized_pnl_pct", "status",
                      "signal_id", "virtual_position_id", "trading_day"):
            assert field in position, field


class TestTheFillModelRefusesRatherThanInvents:
    """A fabricated fill is worse than a missing one: it produces a
    number that looks like evidence."""

    def test_a_missing_entry_price_is_refused(self, engine):
        out = _buy(engine, price=None)
        assert out["accepted"] is False
        assert out["reason"] == ve.REFUSED_NO_PRICE

    def test_a_zero_or_negative_price_is_refused(self, engine):
        assert _buy(engine, price=0)["reason"] == ve.REFUSED_NO_PRICE
        assert _buy(engine, price=-5)["reason"] == ve.REFUSED_NO_PRICE

    def test_a_missing_exit_price_leaves_the_position_open(self, engine):
        """Closing at an unknown price would invent the one number the
        record exists for."""
        _buy(engine)
        out = engine.submit_sell(strategy_id=S1, symbol="NVDA",
                                 decision_price=None, exit_reason="STOP",
                                 now=NOW)
        assert out["accepted"] is False
        assert engine.is_open(S1, "NVDA") is True

    def test_a_fractional_quantity_is_refused_not_rounded(self, engine):
        """Rounding would silently change what the strategy asked for."""
        out = _buy(engine, quantity=1.5)
        assert out["accepted"] is False
        assert out["reason"] == ve.REFUSED_QUANTITY

    def test_a_zero_quantity_is_refused(self, engine):
        assert _buy(engine, quantity=0)["reason"] == ve.REFUSED_QUANTITY

    def test_whole_shares_only_matches_the_live_rule(self, engine):
        assert _buy(engine, quantity=3)["accepted"] is True

    def test_the_fill_model_is_recorded_on_the_row(self, engine):
        """So a later reader can tell which assumptions produced a
        number."""
        assert _buy(engine)["position"]["fill_model"] == ve.FILL_MODEL

    def test_a_missing_symbol_is_refused(self, engine):
        assert _buy(engine, symbol="")["reason"] == ve.REFUSED_NO_SYMBOL


class TestOneOpenPositionPerStrategyAndSymbol:
    def test_a_second_buy_of_an_open_symbol_is_refused(self, engine):
        """Without this a scanner firing every tick would accumulate a
        position per tick and report a return no account could have."""
        _buy(engine)
        assert _buy(engine)["reason"] == ve.REFUSED_ALREADY_OPEN

    def test_a_different_strategy_may_hold_the_same_symbol(self, engine):
        _buy(engine, strategy=S1)
        assert _buy(engine, strategy=S2)["accepted"] is True

    def test_selling_what_is_not_open_is_refused(self, engine):
        out = engine.submit_sell(strategy_id=S1, symbol="NVDA",
                                 decision_price=10.0, exit_reason="STOP",
                                 now=NOW)
        assert out["reason"] == ve.REFUSED_NOT_OPEN

    def test_it_can_be_bought_again_after_closing(self, engine):
        _buy(engine)
        engine.submit_sell(strategy_id=S1, symbol="NVDA", decision_price=110.0,
                           exit_reason="TARGET", now=NOW)
        assert _buy(engine)["accepted"] is True


class TestItCanNeverReachABroker:
    def test_it_imports_no_broker(self):
        source = (REPO_ROOT / "execution" / "virtual_execution.py").read_text()
        for forbidden in ("from brokers", "import brokers", "KISBroker",
                          "submit_order", "cancel_order", "requests",
                          "get_account", "orderable"):
            assert forbidden not in source, forbidden

    def test_it_is_usable_by_every_paper_scanner(self):
        """One engine, not one per scanner: six slightly different
        virtual fills would make six scanners' results incomparable."""
        engine = ve.VirtualExecutionEngine()
        for scanner in ("hma_early_trend", "accumulation", "breakout_ready",
                        "premarket_momentum", "gap_pullback"):
            out = engine.submit_buy(
                strategy_id=f"strategy::{scanner}", scanner=scanner,
                symbol="AAPL", quantity=1, decision_price=100.0, now=NOW)
            assert out["accepted"] is True, scanner


class TestPerformanceRecord:
    def test_closed_trades_are_summarised(self, env):
        engine = ve.VirtualExecutionEngine()
        for symbol, exit_price in (("AAA", 110.0), ("BBB", 90.0)):
            engine.submit_buy(strategy_id=S1, scanner="s", symbol=symbol,
                              quantity=1, decision_price=100.0, now=NOW)
            out = engine.submit_sell(strategy_id=S1, symbol=symbol,
                                     decision_price=exit_price,
                                     exit_reason="X", now=NOW)
            ve.record(out["position"], trading_day="D", env=env)
        summary = ve.performance("D", strategy_id=S1, env=env)
        assert summary["closed_trades"] == 2
        assert summary["realized_pnl"] == pytest.approx(0.0)
        assert summary["win_rate"] == pytest.approx(0.5)

    def test_open_positions_are_counted_never_valued(self, env):
        """Marking to market would mix realised and unrealised into the
        one figure people then quote."""
        engine = ve.VirtualExecutionEngine()
        out = _buy(engine)
        ve.record(out["position"], trading_day="D", env=env)
        summary = ve.performance("D", strategy_id=S1, env=env)
        assert summary["still_open"] == 1
        assert summary["closed_trades"] == 0
        assert summary["realized_pnl"] is None

    def test_an_unconfigured_root_writes_nothing(self, engine):
        out = _buy(engine)
        assert ve.record(out["position"], trading_day="D", env={}) is False
        assert ve.read("D", env={}) == []

    def test_a_corrupt_line_does_not_lose_the_others(self, env):
        engine = ve.VirtualExecutionEngine()
        out = _buy(engine)
        ve.record(out["position"], trading_day="D", env=env)
        with open(ve.log_path("D", env=env), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        assert len(ve.read("D", env=env)) == 1
