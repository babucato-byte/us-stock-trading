"""Stage 4 (roadmap Phase 5): position lifecycle tests.

Every test isolates the position store, order history, order-intent
ledger, and both kill switches to tmp_path via env-var/monkeypatch
overrides -- never touches real operational files. No real network calls
(FakeBroker only), no real Slack (send_slack_alert monkeypatched to a spy).
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import paper_strategy_order as pso
from config import scalping_strategy_v1_config as cfg
from positions import fill_validation, lifecycle, states, store
from strategy.interface import (
    STATE_ENTRY_SIGNAL,
    STATE_NO_SETUP,
    EvaluationResult,
    TradingStrategy,
)
from strategy.registry import StrategyNotActiveError, StrategyRegistry
from strategy.status import ACTIVE, STRUCTURED


TODAY = pso.eastern_now().strftime("%Y-%m-%d")

# CODEX-030: a fixed, unambiguous mid-regular-session moment (Wednesday,
# no holiday, DST in effect) for every check_and_manage()/check_invalidation()
# call below that is not itself testing time-of-day/EOD behavior. Using the
# real wall clock here previously made target/stop/no-action tests fail
# whenever the suite happened to run within EOD_FORCE_CLOSE_MINUTES_BEFORE_
# CLOSE minutes of the real 16:00 ET close (CODEX-030).
MID_SESSION_NOW = datetime(2026, 7, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))


class FakeBrokerResponse:
    def __init__(self, status_code=200, text="OK", dry_run=False, data=None):
        self.status_code = status_code
        self.text = text
        self.dry_run = dry_run
        self.data = data


class FakeBroker:
    """submit_order()'s default behavior (no explicit default_response
    override) synthesizes a realistic *filled* response per call --
    status="filled", filled_qty=the requested qty -- since CODEX-023
    means a bare status_code=200 with no order status is UNKNOWN (fails
    closed to MANUAL_REVIEW), not an implicit fill. Tests that need to
    exercise the accepted/partially-filled/rejected/unknown paths pass an
    explicit default_response or per-(symbol, side) submit_side_effects."""

    def __init__(self, submit_side_effects=None, default_response=None, orders_by_client_id=None):
        self._submit_side_effects = submit_side_effects or {}
        self._default_response = default_response
        self._orders_by_client_id = orders_by_client_id or {}
        self.submit_calls = []

    def submit_order(self, symbol, qty=1, *, side, client_order_id=None):
        self.submit_calls.append((symbol, qty, side, client_order_id))
        effect = self._submit_side_effects.get((symbol, side))
        if isinstance(effect, Exception):
            raise effect
        if effect is not None:
            return effect
        if self._default_response is not None:
            return self._default_response
        return FakeBrokerResponse(status_code=200, text="OK", data={
            "status": "filled", "filled_qty": qty, "filled_avg_price": None,
            "id": f"broker-{client_order_id}",
        })

    def get_order_by_client_order_id(self, client_order_id):
        return self._orders_by_client_id.get(client_order_id)


class FakeStrategy(TradingStrategy):
    """Minimal TradingStrategy double: scripted signal/stop/targets/invalidate,
    no real indicator math -- lifecycle.py must not care how a strategy
    reaches its answer, only that it implements the interface. Subclasses
    TradingStrategy for real (StrategyRegistry.register() rejects anything
    that isn't actually an instance of it)."""

    def __init__(self, signal=True, stop_price=95.0, target_1=105.0, target_2=110.0,
                 invalidate_result=False, status=STRUCTURED):
        super().__init__(strategy_id="FAKE_STRATEGY_V1", version="1.0.0", status=status)
        self._signal = signal
        self._stop_price = stop_price
        self._target_1 = target_1
        self._target_2 = target_2
        self._invalidate_result = invalidate_result

    def evaluate_setup(self, bars, *, symbol, as_of=None):
        return self.generate_entry(bars, symbol=symbol, as_of=as_of)

    def generate_entry(self, bars, *, symbol, as_of=None):
        return EvaluationResult(
            strategy_id=self.strategy_id,
            symbol=symbol,
            evaluated_at="2026-07-25T10:00:00+00:00",
            state=STATE_ENTRY_SIGNAL if self._signal else STATE_NO_SETUP,
            signal=self._signal,
            entry_reason="fake signal" if self._signal else "",
            stop_price=self._stop_price,
            target_1=self._target_1,
            target_2=self._target_2,
        )

    def calculate_stop(self, bars, *, entry_price):
        return self._stop_price

    def calculate_targets(self, *, entry_price, stop_price):
        return {"target_1": self._target_1, "target_2": self._target_2}

    def invalidate(self, bars, *, symbol):
        return self._invalidate_result


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


def _active_registry(strategy):
    registry = StrategyRegistry()
    strategy.status = ACTIVE  # activate() also works but this mirrors register()-time ACTIVE
    registry.register(strategy)
    return registry


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def test_enter_position_creates_filled_pipeline_through_entry_submitted():
    strategy = FakeStrategy()
    registry = _active_registry(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])

    record = lifecycle.enter_position(
        strategy, "AAPL", bars, qty=10, order_date=TODAY,
        broker=broker, registry=registry,
    )

    assert record["state"] == states.ENTRY_SUBMITTED
    assert record["stop_price"] == 95.0
    assert record["target_1_price"] == 105.0
    assert record["target_2_price"] == 110.0
    assert len(broker.submit_calls) == 1
    assert broker.submit_calls[0][2] == "buy"


def test_enter_position_returns_none_when_no_signal():
    strategy = FakeStrategy(signal=False)
    registry = _active_registry(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])

    result = lifecycle.enter_position(
        strategy, "AAPL", bars, qty=10, order_date=TODAY,
        broker=broker, registry=registry,
    )
    assert result is None
    assert broker.submit_calls == []


def test_enter_position_blocks_when_strategy_not_active():
    strategy = FakeStrategy()
    strategy.status = STRUCTURED
    registry = StrategyRegistry()
    registry.register(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])

    with pytest.raises(lifecycle.PositionLifecycleError):
        lifecycle.enter_position(
            strategy, "AAPL", bars, qty=10, order_date=TODAY,
            broker=broker, registry=registry,
        )
    assert broker.submit_calls == []


def test_enter_position_blocked_by_kill_switch_ends_rejected(monkeypatch, tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("halted")
    strategy = FakeStrategy()
    registry = _active_registry(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])

    record = lifecycle.enter_position(
        strategy, "AAPL", bars, qty=10, order_date=TODAY,
        broker=broker, registry=registry,
    )
    assert record["state"] == states.REJECTED
    assert broker.submit_calls == []


def test_enter_position_broker_rejection_ends_rejected():
    strategy = FakeStrategy()
    registry = _active_registry(strategy)
    broker = FakeBroker(default_response=FakeBrokerResponse(status_code=422, text="rejected"))
    bars = pd.DataFrame([{"Close": 100.0}])

    record = lifecycle.enter_position(
        strategy, "AAPL", bars, qty=10, order_date=TODAY,
        broker=broker, registry=registry,
    )
    assert record["state"] == states.REJECTED
    assert record["exit_reason"].startswith("BROKER_REJECTED")


# ---------------------------------------------------------------------------
# Fill tracking
# ---------------------------------------------------------------------------

def _entered_position(qty=10):
    strategy = FakeStrategy()
    registry = _active_registry(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])
    record = lifecycle.enter_position(
        strategy, "AAPL", bars, qty=qty, order_date=TODAY,
        broker=broker, registry=registry,
    )
    return record, broker


def test_partial_fill_transitions_to_partially_filled():
    record, _ = _entered_position(qty=10)
    updated = lifecycle.record_fill(record["position_id"], filled_qty=4, average_fill_price=100.0)
    assert updated["state"] == states.PARTIALLY_FILLED
    assert updated["remaining_qty"] == 4


def test_full_fill_transitions_to_stop_active():
    record, _ = _entered_position(qty=10)
    updated = lifecycle.record_fill(record["position_id"], filled_qty=10, average_fill_price=100.0)
    assert updated["state"] == states.STOP_ACTIVE
    assert updated["remaining_qty"] == 10
    assert updated["average_fill_price"] == 100.0


def test_record_fill_wrong_state_raises():
    record, _ = _entered_position(qty=10)
    lifecycle.record_fill(record["position_id"], filled_qty=10, average_fill_price=100.0)
    with pytest.raises(lifecycle.PositionLifecycleError):
        lifecycle.record_fill(record["position_id"], filled_qty=10, average_fill_price=100.0)


# ---------------------------------------------------------------------------
# CODEX-027: record_fill() rejects invalid/regressing fills
# ---------------------------------------------------------------------------

def test_record_fill_negative_qty_rejected_and_position_unchanged():
    record, _ = _entered_position(qty=10)
    with pytest.raises(fill_validation.InvalidFillError):
        lifecycle.record_fill(record["position_id"], filled_qty=-3, average_fill_price=100.0)
    unchanged = store.load_position(record["position_id"])
    assert unchanged["state"] == states.ENTRY_SUBMITTED
    assert unchanged["filled_qty"] == 0


def test_record_fill_nan_qty_rejected():
    record, _ = _entered_position(qty=10)
    with pytest.raises(fill_validation.InvalidFillError):
        lifecycle.record_fill(record["position_id"], filled_qty=float("nan"), average_fill_price=100.0)


def test_record_fill_qty_exceeding_requested_rejected():
    record, _ = _entered_position(qty=10)
    with pytest.raises(fill_validation.InvalidFillError):
        lifecycle.record_fill(record["position_id"], filled_qty=15, average_fill_price=100.0)


def test_record_fill_negative_price_rejected():
    record, _ = _entered_position(qty=10)
    with pytest.raises(fill_validation.InvalidFillError):
        lifecycle.record_fill(record["position_id"], filled_qty=5, average_fill_price=-1.0)


def test_record_fill_regression_rejected():
    record, _ = _entered_position(qty=10)
    lifecycle.record_fill(record["position_id"], filled_qty=6, average_fill_price=100.0)
    with pytest.raises(fill_validation.InvalidFillError):
        lifecycle.record_fill(record["position_id"], filled_qty=3, average_fill_price=100.0)
    unchanged = store.load_position(record["position_id"])
    assert unchanged["filled_qty"] == 6  # the earlier valid fill was not clobbered


def test_record_fill_duplicate_same_cumulative_event_is_idempotent_noop():
    record, _ = _entered_position(qty=10)
    first = lifecycle.record_fill(record["position_id"], filled_qty=6, average_fill_price=100.0)
    second = lifecycle.record_fill(record["position_id"], filled_qty=6, average_fill_price=100.0)
    assert first["state_history"] == second["state_history"]  # no new history entry appended
    assert second["filled_qty"] == 6


# ---------------------------------------------------------------------------
# Target-1 partial exit / target-2 full exit / stop-loss
# ---------------------------------------------------------------------------

def _filled_position(qty=10, stop_price=95.0, target_1=105.0, target_2=110.0):
    strategy = FakeStrategy(stop_price=stop_price, target_1=target_1, target_2=target_2)
    registry = _active_registry(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])
    record = lifecycle.enter_position(
        strategy, "AAPL", bars, qty=qty, order_date=TODAY,
        broker=broker, registry=registry,
    )
    record = lifecycle.record_fill(record["position_id"], filled_qty=qty, average_fill_price=100.0)
    return record, broker


def test_target_1_hit_submits_50_percent_partial_exit():
    record, broker = _filled_position(qty=10)
    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=105.0, now=MID_SESSION_NOW, broker=broker,
    )

    assert updated["state"] == states.PARTIAL_EXITED
    assert updated["remaining_qty"] == 5
    assert updated["realized_pnl"] == pytest.approx(5 * (105.0 - 100.0))
    sell_calls = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(sell_calls) == 1
    assert sell_calls[0][1] == 5


def test_target_2_after_partial_exit_closes_remaining():
    record, broker = _filled_position(qty=10)
    record = lifecycle.check_and_manage(
        record["position_id"], current_price=105.0, now=MID_SESSION_NOW, broker=broker,
    )
    assert record["state"] == states.PARTIAL_EXITED

    # move to breakeven trailing stop first (deliberate simple policy)
    record = lifecycle.check_and_manage(
        record["position_id"], current_price=106.0, now=MID_SESSION_NOW, broker=broker,
    )
    assert record["state"] == states.TRAILING

    record = lifecycle.check_and_manage(
        record["position_id"], current_price=110.0, now=MID_SESSION_NOW, broker=broker,
    )
    assert record["state"] == states.CLOSED
    assert record["remaining_qty"] == 0
    assert record["exit_reason"] == "TARGET_2"


def test_stop_loss_before_target_1_closes_full_position():
    record, broker = _filled_position(qty=10)
    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker,
    )

    assert updated["state"] == states.CLOSED
    assert updated["exit_reason"] == "STOP_LOSS"
    assert updated["remaining_qty"] == 0
    assert updated["realized_pnl"] == pytest.approx(10 * (95.0 - 100.0))


def test_check_and_manage_no_action_when_price_between_stop_and_target_1():
    record, broker = _filled_position(qty=10)
    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=100.0, now=MID_SESSION_NOW, broker=broker,
    )
    assert updated["state"] == states.STOP_ACTIVE
    assert [c for c in broker.submit_calls if c[2] == "sell"] == []


# ---------------------------------------------------------------------------
# CODEX-046: independent live-rollout exit flags
# ---------------------------------------------------------------------------

def test_partial_profit_disabled_takes_no_action_at_target_1():
    record, broker = _filled_position(qty=10)
    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=105.0, now=MID_SESSION_NOW, broker=broker,
        enable_partial_profit=False,
    )
    assert updated["state"] == states.STOP_ACTIVE
    assert [c for c in broker.submit_calls if c[2] == "sell"] == []


def test_partial_profit_disabled_but_past_target_2_takes_full_exit():
    record, broker = _filled_position(qty=10)
    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=110.0, now=MID_SESSION_NOW, broker=broker,
        enable_partial_profit=False,
    )
    assert updated["state"] == states.CLOSED
    assert updated["exit_reason"] == "TARGET_2"
    assert updated["remaining_qty"] == 0


def test_stop_loss_still_active_when_all_other_flags_disabled():
    record, broker = _filled_position(qty=10)
    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker,
        enable_partial_profit=False, enable_trailing_stop=False,
        enable_time_stop=False, enable_eod_exit=False,
    )
    assert updated["state"] == states.CLOSED
    assert updated["exit_reason"] == "STOP_LOSS"


def test_trailing_stop_disabled_stays_partial_exited_without_moving_to_breakeven():
    record, broker = _filled_position(qty=10)
    record = lifecycle.check_and_manage(
        record["position_id"], current_price=105.0, now=MID_SESSION_NOW, broker=broker,
    )
    assert record["state"] == states.PARTIAL_EXITED
    record = lifecycle.check_and_manage(
        record["position_id"], current_price=106.0, now=MID_SESSION_NOW, broker=broker,
        enable_trailing_stop=False,
    )
    assert record["state"] == states.PARTIAL_EXITED  # never moved to TRAILING
    assert record["stop_price"] == pytest.approx(95.0)  # unchanged, not moved to breakeven


def test_time_stop_disabled_does_not_force_exit():
    record, broker = _filled_position(qty=10)
    with store.locked_position(record["position_id"]) as locked:
        locked["entry_time"] = MID_SESSION_NOW.isoformat()
    held_past_max = MID_SESSION_NOW + timedelta(minutes=cfg.MAX_POSITION_HOLD_MINUTES + 1)

    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=100.0, now=held_past_max, broker=broker,
        enable_time_stop=False,
    )
    assert updated["state"] == states.STOP_ACTIVE
    assert [c for c in broker.submit_calls if c[2] == "sell"] == []


def test_eod_exit_disabled_does_not_force_exit():
    record, broker = _filled_position(qty=10)
    from market_hours import combine_eastern, MARKET_REGULAR_END
    near_close = combine_eastern(MID_SESSION_NOW.date(), MARKET_REGULAR_END) - timedelta(minutes=1)

    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=100.0, now=near_close, broker=broker,
        enable_eod_exit=False,
    )
    assert updated["state"] == states.STOP_ACTIVE
    assert [c for c in broker.submit_calls if c[2] == "sell"] == []


# ---------------------------------------------------------------------------
# Time-stop / EOD forced close
# ---------------------------------------------------------------------------

def test_time_stop_forces_full_exit():
    # CODEX-030: entry_time is pinned to MID_SESSION_NOW (rather than the
    # real entry_time enter_position() recorded at whatever moment the
    # test happened to run) so that "61 minutes later" lands at a fixed,
    # unambiguous point well inside the same regular session and well
    # before the EOD cutoff -- otherwise this test's pass/fail would
    # depend on what wall-clock time the suite ran at.
    record, broker = _filled_position(qty=10)
    with store.locked_position(record["position_id"]) as locked:
        locked["entry_time"] = MID_SESSION_NOW.isoformat()
    held_past_max = MID_SESSION_NOW + timedelta(minutes=cfg.MAX_POSITION_HOLD_MINUTES + 1)

    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=100.0, now=held_past_max, broker=broker,
    )
    assert updated["state"] == states.CLOSED
    assert updated["exit_reason"] == "TIME_STOP"


def test_eod_forces_full_exit_regardless_of_price():
    record, broker = _filled_position(qty=10)
    from market_hours import combine_eastern, MARKET_REGULAR_END
    near_close = combine_eastern(MID_SESSION_NOW.date(), MARKET_REGULAR_END) - timedelta(minutes=1)

    updated = lifecycle.check_and_manage(
        record["position_id"], current_price=100.0, now=near_close, broker=broker,
    )
    assert updated["state"] == states.CLOSED
    assert updated["exit_reason"] == "EOD_FORCED_CLOSE"


# ---------------------------------------------------------------------------
# Strategy invalidation
# ---------------------------------------------------------------------------

def test_invalidation_forces_full_exit():
    strategy = FakeStrategy(invalidate_result=True)
    registry = _active_registry(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])
    record = lifecycle.enter_position(
        strategy, "AAPL", bars, qty=10, order_date=TODAY, broker=broker, registry=registry,
    )
    record = lifecycle.record_fill(record["position_id"], filled_qty=10, average_fill_price=100.0)

    updated = lifecycle.check_invalidation(
        record["position_id"], strategy, bars, now=MID_SESSION_NOW, broker=broker,
    )
    assert updated["state"] == states.CLOSED
    assert updated["exit_reason"] == "STRATEGY_INVALIDATION"


def test_invalidation_noop_when_strategy_says_still_valid():
    record, broker = _filled_position(qty=10)
    strategy = FakeStrategy(invalidate_result=False)
    bars = pd.DataFrame([{"Close": 100.0}])

    updated = lifecycle.check_invalidation(
        record["position_id"], strategy, bars, now=MID_SESSION_NOW, broker=broker,
    )
    assert updated["state"] == states.STOP_ACTIVE
    assert [c for c in broker.submit_calls if c[2] == "sell"] == []


# ---------------------------------------------------------------------------
# Duplicate-exit prevention under concurrency
# ---------------------------------------------------------------------------

def test_concurrent_stop_loss_checks_only_submit_one_exit_order():
    import threading

    from state_store import db as state_db

    record, broker = _filled_position(qty=10)
    state_db.open_db()  # pre-warm: ensure the SQLite schema exists before concurrent access
    position_id = record["position_id"]
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=2)
            lifecycle.check_and_manage(position_id, current_price=90.0, now=MID_SESSION_NOW, broker=broker)
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)

    assert not errors
    sell_calls = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(sell_calls) == 1
    final = store.load_position(position_id)
    assert final["state"] == states.CLOSED
    assert final["remaining_qty"] == 0


# ---------------------------------------------------------------------------
# PnL
# ---------------------------------------------------------------------------

def test_compute_unrealized_pnl():
    record, _ = _filled_position(qty=10)
    assert lifecycle.compute_unrealized_pnl(record, current_price=103.0) == pytest.approx(30.0)
    assert lifecycle.compute_unrealized_pnl(record, current_price=97.0) == pytest.approx(-30.0)


def test_compute_unrealized_pnl_zero_when_no_remaining_qty():
    record, broker = _filled_position(qty=10)
    closed = lifecycle.check_and_manage(
        record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker,
    )
    assert lifecycle.compute_unrealized_pnl(closed, current_price=200.0) == 0.0


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

def test_recover_on_restart_marks_unreconcilable_position_recovery_required():
    record, broker = _filled_position(qty=10)
    # No broker passed => reconciliation is inconclusive by construction.
    result = lifecycle.recover_on_restart(broker=None)
    assert result.status == lifecycle.RECOVERY_STATUS_OK
    assert len(result.positions) == 1
    assert result.positions[0]["state"] == states.RECOVERY_REQUIRED


def test_recover_on_restart_confirms_when_broker_lookup_succeeds():
    record, broker = _filled_position(qty=10)
    broker._orders_by_client_id[record["client_order_id"]] = {"status": "filled"}

    result = lifecycle.recover_on_restart(broker=broker)
    assert len(result.positions) == 1
    assert result.positions[0]["state"] == states.STOP_ACTIVE  # unchanged, confirmed


def test_recover_on_restart_leaves_already_recovery_required_untouched():
    record, broker = _filled_position(qty=10)
    with store.locked_position(record["position_id"]) as locked:
        states.validate_transition(locked["state"], states.RECOVERY_REQUIRED)
        locked["state"] = states.RECOVERY_REQUIRED
        locked["state_history"].append({"state": states.RECOVERY_REQUIRED, "at": "t", "reason": "prior crash"})

    result = lifecycle.recover_on_restart(broker=broker)
    assert result.positions[0]["state"] == states.RECOVERY_REQUIRED


def test_recover_on_restart_skips_terminal_positions():
    record, broker = _filled_position(qty=10)
    lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)  # closes it

    result = lifecycle.recover_on_restart(broker=broker)
    assert result.positions == []
    assert result.status == lifecycle.RECOVERY_STATUS_OK


# ---------------------------------------------------------------------------
# CODEX-025: corrupted store fails closed on restart, not silently empty
# CODEX-028: since SQLite is now canonical, these corrupt the SQLite
# database file (STATE_STORE_DB_FILE) -- corrupting POSITION_STORE.json
# alone no longer means anything (it's a regenerable projection, see
# tests/test_position_store.py's
# test_corrupted_json_projection_alone_is_not_store_corruption).
# ---------------------------------------------------------------------------

def _corrupt_state_db(tmp_path):
    db_path = tmp_path / "TEST_STATE.db"
    db_path.write_bytes(b"not a sqlite database, just garbage bytes" * 50)


def test_recover_on_restart_store_unavailable_on_corrupted_file(tmp_path, monkeypatch):
    _corrupt_state_db(tmp_path)

    result = lifecycle.recover_on_restart(broker=None)
    assert result.status == lifecycle.RECOVERY_STATUS_STORE_UNAVAILABLE
    assert result.positions == []
    assert result.reason is not None


def test_recover_on_restart_store_unavailable_escalates_kill_switch(tmp_path, monkeypatch):
    import kill_switch_state
    _corrupt_state_db(tmp_path)

    lifecycle.recover_on_restart(broker=None)
    assert kill_switch_state.get_state() == kill_switch_state.MANUAL_REVIEW


def test_recover_on_restart_store_unavailable_fetches_broker_positions_best_effort(tmp_path):
    strategy = FakeStrategy()
    registry = _active_registry(strategy)
    broker = FakeBroker()
    broker._positions = [{"symbol": "AAPL", "qty": 5}]
    bars = pd.DataFrame([{"Close": 100.0}])
    lifecycle.enter_position(strategy, "AAPL", bars, qty=10, order_date=TODAY, broker=broker, registry=registry)

    _corrupt_state_db(tmp_path)

    def _get_positions():
        return [{"symbol": "AAPL", "qty": 5}]
    broker.get_positions = _get_positions

    result = lifecycle.recover_on_restart(broker=broker)
    assert result.status == lifecycle.RECOVERY_STATUS_STORE_UNAVAILABLE
    assert result.broker_positions == [{"symbol": "AAPL", "qty": 5}]


def test_new_entry_refused_when_store_corrupted(tmp_path, monkeypatch):
    _corrupt_state_db(tmp_path)

    strategy = FakeStrategy()
    registry = _active_registry(strategy)
    broker = FakeBroker()
    bars = pd.DataFrame([{"Close": 100.0}])
    with pytest.raises(store.PositionStoreError):
        lifecycle.enter_position(strategy, "AAPL", bars, qty=10, order_date=TODAY, broker=broker, registry=registry)
    assert broker.submit_calls == []
