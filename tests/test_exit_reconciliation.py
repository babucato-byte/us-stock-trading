"""CODEX-023 (accepted-vs-filled separation) and CODEX-024 (durable exit
intent, crash/timeout-safe reconciliation) tests.

Every test isolates the position store, SQLite state store, order
history, and kill switches to tmp_path -- never touches real operational
files. No real network calls (FakeBroker/SequencedBroker only).
"""
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import paper_strategy_order as pso
from positions import fill_validation, lifecycle, order_status, states, store
from state_store import db as state_db
from state_store import exit_intent_ledger as eil
from strategy.interface import STATE_ENTRY_SIGNAL, STATE_NO_SETUP, EvaluationResult, TradingStrategy
from strategy.registry import StrategyRegistry
from strategy.status import ACTIVE

TODAY = pso.eastern_now().strftime("%Y-%m-%d")

# CODEX-030: fixed mid-session moment -- see tests/test_position_lifecycle.py's
# MID_SESSION_NOW for why check_and_manage() must never be called with an
# implicit real-wall-clock `now` from a test.
MID_SESSION_NOW = datetime(2026, 7, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))


class FakeBrokerResponse:
    def __init__(self, status_code=200, text="OK", data=None, dry_run=False):
        self.status_code = status_code
        self.text = text
        self.data = data
        self.dry_run = dry_run


def _filled_response(qty, price=None):
    return FakeBrokerResponse(data={"status": "filled", "filled_qty": qty, "filled_avg_price": price, "id": "b-1"})


def _accepted_response(order_id="b-accepted"):
    return FakeBrokerResponse(data={"status": "accepted", "filled_qty": 0, "filled_avg_price": None, "id": order_id})


def _partial_response(qty, price=None, order_id="b-partial"):
    return FakeBrokerResponse(data={"status": "partially_filled", "filled_qty": qty, "filled_avg_price": price, "id": order_id})


class SequencedBroker:
    """submit_order() returns/raises the next scripted item each call
    (order matters, one item consumed per call). get_order_by_client_order_id
    looks up a caller-supplied dict, where a value that's an Exception
    instance is raised instead of returned."""

    def __init__(self, submit_responses=None, orders_by_client_id=None, positions=None):
        self._submit_responses = list(submit_responses or [])
        self._orders_by_client_id = orders_by_client_id or {}
        self._positions = positions or []
        self.submit_calls = []
        self.lookup_calls = []

    def submit_order(self, symbol, qty=1, *, side, client_order_id=None):
        self.submit_calls.append((symbol, qty, side, client_order_id))
        if not self._submit_responses:
            raise AssertionError("SequencedBroker ran out of scripted submit responses")
        item = self._submit_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get_order_by_client_order_id(self, client_order_id):
        self.lookup_calls.append(client_order_id)
        value = self._orders_by_client_id.get(client_order_id)
        if isinstance(value, Exception):
            raise value
        return value

    def get_positions(self):
        return self._positions


class FakeStrategy(TradingStrategy):
    def __init__(self, stop_price=95.0, target_1=105.0, target_2=110.0, status=ACTIVE):
        super().__init__(strategy_id="FAKE_RECONCILE_STRATEGY", version="1.0.0", status=status)
        self.stop_price = stop_price
        self.target_1 = target_1
        self.target_2 = target_2
        self._fired = False

    def evaluate_setup(self, bars, *, symbol, as_of=None):
        return self.generate_entry(bars, symbol=symbol, as_of=as_of)

    def generate_entry(self, bars, *, symbol, as_of=None):
        signal = not self._fired
        self._fired = True
        return EvaluationResult(
            strategy_id=self.strategy_id, symbol=symbol, evaluated_at="2026-07-26T00:00:00+00:00",
            state=STATE_ENTRY_SIGNAL if signal else STATE_NO_SETUP, signal=signal,
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


def _filled_position(qty=10, broker_submit_responses=None, stop_price=95.0, target_1=105.0, target_2=110.0):
    """Enter + fully fill a position using a SequencedBroker whose first
    call (the entry) always succeeds; remaining scripted responses are for
    the exit(s) under test."""
    strategy = FakeStrategy(stop_price=stop_price, target_1=target_1, target_2=target_2)
    registry = StrategyRegistry()
    strategy.status = ACTIVE
    registry.register(strategy)
    broker = SequencedBroker(submit_responses=[_filled_response(qty)] + list(broker_submit_responses or []))
    bars = pd.DataFrame([{"Close": 100.0}])
    record = lifecycle.enter_position(strategy, "AAPL", bars, qty=qty, order_date=TODAY, broker=broker, registry=registry)
    record = lifecycle.record_fill(record["position_id"], filled_qty=qty, average_fill_price=100.0)
    return record, broker


def _exit_conn(tmp_path=None):
    return state_db.open_db()


# ---------------------------------------------------------------------------
# CODEX-023: accepted != filled
# ---------------------------------------------------------------------------

def test_accepted_sell_leaves_position_exit_submitted_not_closed():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_accepted_response()])
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert updated["state"] == states.EXIT_SUBMITTED
    assert updated["remaining_qty"] == 10  # unchanged -- not filled yet
    assert updated["realized_pnl"] == 0.0


def test_new_sell_leaves_position_exit_submitted_remaining_qty_unchanged():
    record, broker = _filled_position(qty=10, broker_submit_responses=[
        FakeBrokerResponse(data={"status": "new", "filled_qty": 0, "filled_avg_price": None, "id": "b-new"})
    ])
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert updated["state"] == states.EXIT_SUBMITTED
    assert updated["remaining_qty"] == 10


def test_partially_filled_sell_reduces_only_confirmed_quantity():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_partial_response(4, price=94.0)])
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert updated["state"] == states.EXIT_SUBMITTED  # intent not complete yet
    assert updated["remaining_qty"] == 6
    assert updated["realized_pnl"] == pytest.approx(4 * (94.0 - 100.0))


def test_filled_sell_closes_when_remaining_reaches_zero():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_filled_response(10, price=94.0)])
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert updated["state"] == states.CLOSED
    assert updated["remaining_qty"] == 0
    assert updated["realized_pnl"] == pytest.approx(10 * (94.0 - 100.0))


def test_accepted_then_later_filled_event_closes_normally():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_accepted_response(order_id="b-x")])
    pending = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert pending["state"] == states.EXIT_SUBMITTED

    # The broker later reports the same order as filled.
    broker._orders_by_client_id[pending["client_order_id"]] = {
        "status": "filled", "filled_qty": 10, "filled_avg_price": 93.5, "id": "b-x",
    }
    resolved = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert resolved["state"] == states.CLOSED
    assert resolved["remaining_qty"] == 0
    assert resolved["realized_pnl"] == pytest.approx(10 * (93.5 - 100.0))


def test_repeated_accepted_events_do_not_double_reduce_quantity():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_accepted_response(order_id="b-y")])
    first = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    broker._orders_by_client_id[first["client_order_id"]] = {
        "status": "accepted", "filled_qty": 0, "filled_avg_price": None, "id": "b-y",
    }
    second = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    third = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert second["remaining_qty"] == 10
    assert third["remaining_qty"] == 10
    assert third["realized_pnl"] == 0.0


def test_repeated_filled_events_do_not_double_apply_pnl():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_accepted_response(order_id="b-z")])
    lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    broker._orders_by_client_id["exit-will-not-match"] = None  # noop, keyed correctly below
    pending = store.load_position(record["position_id"])
    broker._orders_by_client_id[pending["client_order_id"]] = {
        "status": "filled", "filled_qty": 10, "filled_avg_price": 93.0, "id": "b-z",
    }
    once = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert once["state"] == states.CLOSED
    assert once["realized_pnl"] == pytest.approx(10 * (93.0 - 100.0))

    # Intent is now CONFIRMED (terminal) and position CLOSED (not
    # exit-reachable) -- a repeat reconciliation attempt must find nothing
    # active to act on and leave everything unchanged.
    twice = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert twice is None
    unchanged = store.load_position(record["position_id"])
    assert unchanged["realized_pnl"] == pytest.approx(10 * (93.0 - 100.0))


def test_partial_fill_quantity_regression_rejected():
    conn = state_db.open_db()
    record, broker = _filled_position(qty=10, broker_submit_responses=[_partial_response(6, price=94.0)])
    lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    pending = store.load_position(record["position_id"])
    intent = eil.get_active_intent(conn, record["position_id"])
    assert intent["confirmed_filled_qty"] == 6

    # A later (buggy/out-of-order) event reporting a *smaller* cumulative
    # fill must be rejected, not silently accepted as a regression.
    with pytest.raises(fill_validation.InvalidFillError):
        lifecycle._apply_exit_fill_progress(
            conn, dict(pending), intent, order_status.FILL_STATE_PARTIALLY_FILLED,
            confirmed_qty=3, confirmed_price=94.0, reason="STOP_LOSS", on_fully_filled_state=states.CLOSED,
        )


def test_unrecognized_broker_status_fails_closed_to_manual_review():
    record, broker = _filled_position(qty=10, broker_submit_responses=[
        FakeBrokerResponse(data={"status": "some_new_status_alpaca_might_add", "filled_qty": None, "filled_avg_price": None, "id": "b-w"})
    ])
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert updated["state"] == states.MANUAL_REVIEW
    assert updated["remaining_qty"] == 10  # never touched


def test_no_path_turns_an_accepted_order_directly_into_closed():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_accepted_response()])
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert updated["state"] != states.CLOSED
    assert updated["state"] == states.EXIT_SUBMITTED


# ---------------------------------------------------------------------------
# CODEX-024: durable exit intent, crash/timeout-safe reconciliation
# ---------------------------------------------------------------------------

def test_timeout_then_direct_retry_submits_broker_sell_exactly_once():
    record, broker = _filled_position(qty=10, broker_submit_responses=[TimeoutError("simulated timeout")])
    lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)  # times out, no crash propagates
    pending = store.load_position(record["position_id"])
    assert pending["state"] == states.EXIT_SUBMITTED  # not reverted to STOP_ACTIVE

    # Direct retry of the same low-level exit function ("call twice" per
    # CODEX-024's literal reproduction) must not submit a second order.
    lifecycle._force_full_exit(record["position_id"], "AAPL", TODAY, broker, "STOP_LOSS", store.LOCK_TIMEOUT_SECONDS)
    sell_calls = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(sell_calls) == 1  # only the original attempt


def test_concurrent_exit_attempts_submit_broker_sell_exactly_once():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_filled_response(10, price=94.0)])
    state_db.open_db()  # pre-warm: ensure the SQLite schema exists before concurrent access
    position_id = record["position_id"]
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=2)
            lifecycle.check_and_manage(position_id, current_price=94.0, now=MID_SESSION_NOW, broker=broker)
        except Exception as exc:  # pragma: no cover
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


def test_stop_and_target_triggered_simultaneously_submit_sell_exactly_once():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_filled_response(10, price=94.0)])
    state_db.open_db()  # pre-warm: ensure the SQLite schema exists before concurrent access
    position_id = record["position_id"]
    barrier = threading.Barrier(2)
    errors = []

    def stop_worker():
        try:
            barrier.wait(timeout=2)
            lifecycle._force_full_exit(position_id, "AAPL", TODAY, broker, "STOP_LOSS", store.LOCK_TIMEOUT_SECONDS)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def target_worker():
        try:
            barrier.wait(timeout=2)
            lifecycle._force_full_exit(position_id, "AAPL", TODAY, broker, "TARGET_2", store.LOCK_TIMEOUT_SECONDS)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=stop_worker), threading.Thread(target=target_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)

    assert not errors
    sell_calls = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(sell_calls) == 1


def test_exit_intent_reservation_failure_prevents_broker_call(monkeypatch):
    record, broker = _filled_position(qty=10, broker_submit_responses=[_filled_response(10, price=94.0)])

    def _broken_reserve(*args, **kwargs):
        raise RuntimeError("simulated durable-storage failure")

    monkeypatch.setattr(eil, "reserve", _broken_reserve)
    lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    sell_calls = [c for c in broker.submit_calls if c[2] == "sell"]
    assert sell_calls == []
    unchanged = store.load_position(record["position_id"])
    assert unchanged["state"] == states.STOP_ACTIVE  # nothing durable happened, so nothing changed


def test_restart_recovery_reconciles_pending_exit_via_client_order_id():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_accepted_response(order_id="b-restart")])
    pending = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert pending["state"] == states.EXIT_SUBMITTED

    broker._orders_by_client_id[pending["client_order_id"]] = {
        "status": "filled", "filled_qty": 10, "filled_avg_price": 93.0, "id": "b-restart",
    }
    result = lifecycle.recover_on_restart(broker=broker)
    assert result.status == lifecycle.RECOVERY_STATUS_OK
    recovered = [p for p in result.positions if p["position_id"] == record["position_id"]][0]
    assert recovered["state"] == states.CLOSED


def test_broker_confirms_no_such_order_marks_reconciliation_required_no_resubmit():
    record, broker = _filled_position(qty=10, broker_submit_responses=[TimeoutError("timeout")])
    lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    pending = store.load_position(record["position_id"])
    # broker has never heard of this client_order_id (not registered in orders_by_client_id -> None)
    resolved = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert resolved["state"] == states.EXIT_SUBMITTED  # unchanged, not resubmitted
    sell_calls = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(sell_calls) == 1  # still just the original attempt

    conn = state_db.open_db()
    intent = eil.get_by_id(conn, eil.get_active_intent(conn, record["position_id"])["intent_id"]) \
        if eil.get_active_intent(conn, record["position_id"]) else None
    # the intent must exist and be flagged for reconciliation, not silently dropped
    all_intents = conn.execute("SELECT * FROM exit_intents WHERE position_id = ?", (record["position_id"],)).fetchall()
    assert any(dict(r)["state"] == eil.STATE_RECONCILIATION_REQUIRED for r in all_intents)


def test_broker_lookup_failure_during_reconciliation_does_not_resubmit():
    record, broker = _filled_position(qty=10, broker_submit_responses=[TimeoutError("timeout")])
    lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    pending = store.load_position(record["position_id"])
    broker._orders_by_client_id[pending["client_order_id"]] = ConnectionError("broker unreachable")

    resolved = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert resolved["state"] == states.EXIT_SUBMITTED
    sell_calls = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(sell_calls) == 1  # no resubmission attempted


def test_partially_filled_exit_only_manages_remaining_quantity():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_partial_response(4, price=94.0)])
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert updated["remaining_qty"] == 6
    assert updated["state"] == states.EXIT_SUBMITTED  # still tracking the rest of this exit's intent


def test_full_fill_transitions_to_closed_with_zero_remaining():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_filled_response(10, price=94.0)])
    updated = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert updated["state"] == states.CLOSED
    assert updated["remaining_qty"] == 0


def test_stale_reserved_intent_is_never_auto_resubmitted(monkeypatch):
    """Simulate a crash between eil.reserve() succeeding and the broker
    call ever being attempted: the intent is durably RESERVED but the
    broker never actually saw it. A later call must reconcile (find
    "no such order") rather than blindly submitting."""
    record, broker = _filled_position(qty=10)
    conn = state_db.open_db()
    intent_id = eil.reserve(conn, record["position_id"], "STOP_LOSS", 10, "exit-stale-coid")
    with store.locked_position(record["position_id"]) as locked:
        states.validate_transition(locked["state"], states.EXIT_SUBMITTED)
        locked["state"] = states.EXIT_SUBMITTED
        locked["client_order_id"] = "exit-stale-coid"
        locked["state_history"].append({"state": states.EXIT_SUBMITTED, "at": "t", "reason": "simulated crash before broker call"})

    resolved = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert resolved["state"] == states.EXIT_SUBMITTED  # not silently closed or reverted
    sell_calls = [c for c in broker.submit_calls if c[2] == "sell"]
    assert sell_calls == []  # never resubmitted
    updated_intent = eil.get_by_id(conn, intent_id)
    assert updated_intent["state"] == eil.STATE_RECONCILIATION_REQUIRED


# ---------------------------------------------------------------------------
# CODEX-028: SQLite is canonical for exit fill progress -- these pin down
# the exact reproduction scenario from the finding (partial 4, then a
# later cumulative-10 event, must reach remaining=0/CLOSED with the FULL
# 10-share PnL, never losing track of the first 4 shares' worth) and a
# few store-level guarantees at the lifecycle-call level.
# ---------------------------------------------------------------------------

def test_partial_4_then_cumulative_10_reaches_remaining_0_closed_full_pnl():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_partial_response(4, price=94.0)])
    pending = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert pending["state"] == states.EXIT_SUBMITTED
    assert pending["remaining_qty"] == 6
    assert pending["realized_pnl"] == pytest.approx(4 * (94.0 - 100.0))

    # The broker later reports the SAME order fully filled at cumulative 10
    # (not a second order) -- exactly CODEX-028's reproduction step 3/4.
    broker._orders_by_client_id[pending["client_order_id"]] = {
        "status": "filled", "filled_qty": 10, "filled_avg_price": 94.0, "id": "b-partial",
    }
    resolved = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert resolved["state"] == states.CLOSED
    assert resolved["remaining_qty"] == 0
    # Full 10-share PnL, not just the delta (6) applied by the second event.
    assert resolved["realized_pnl"] == pytest.approx(10 * (94.0 - 100.0))


def test_delta_sequencing_4_3_3_applies_incremental_pnl_correctly():
    """partial 4 -> partial 7 (delta 3) -> full 10 (delta 3): each step
    must apply only its own delta's PnL, and the running total after all
    three must equal exactly the full 10-share PnL at the final price."""
    record, broker = _filled_position(qty=10, broker_submit_responses=[_partial_response(4, price=90.0)])
    step1 = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert step1["remaining_qty"] == 6
    assert step1["realized_pnl"] == pytest.approx(4 * (90.0 - 100.0))

    broker._orders_by_client_id[step1["client_order_id"]] = {
        "status": "partially_filled", "filled_qty": 7, "filled_avg_price": 91.0, "id": "b-partial",
    }
    step2 = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert step2["remaining_qty"] == 3
    assert step2["realized_pnl"] == pytest.approx(4 * (90.0 - 100.0) + 3 * (91.0 - 100.0))

    broker._orders_by_client_id[step1["client_order_id"]] = {
        "status": "filled", "filled_qty": 10, "filled_avg_price": 92.0, "id": "b-partial",
    }
    step3 = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert step3["state"] == states.CLOSED
    assert step3["remaining_qty"] == 0
    assert step3["realized_pnl"] == pytest.approx(
        4 * (90.0 - 100.0) + 3 * (91.0 - 100.0) + 3 * (92.0 - 100.0)
    )


def test_out_of_order_cumulative_regression_after_full_fill_blocked():
    """An event reporting cumulative 6 arriving AFTER cumulative 10 has
    already been confirmed must be rejected outright, not silently
    reapplied or allowed to move the (already-terminal) intent backward."""
    record, broker = _filled_position(qty=10, broker_submit_responses=[_filled_response(10, price=94.0)])
    closed = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert closed["state"] == states.CLOSED

    conn = state_db.open_db()
    intent = eil.get_by_client_order_id(conn, closed["client_order_id"])
    assert intent["state"] == eil.STATE_CONFIRMED
    with pytest.raises(eil.ExitIntentError):
        eil.update_progress(conn, intent["intent_id"], confirmed_filled_qty=6)  # terminal -- cannot move at all


def test_json_projection_corrupted_mid_exit_flow_does_not_block_reconciliation(tmp_path):
    """SQLite (not the JSON projection) drives every decision in the exit
    flow -- corrupting POSITION_STORE.json between two reconciliation
    calls must not affect the outcome of the second one at all."""
    record, broker = _filled_position(qty=10, broker_submit_responses=[_partial_response(4, price=94.0)])
    pending = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    assert pending["remaining_qty"] == 6

    store_path = store._resolve_store_path()
    store_path.write_text("{not valid json at all, mid-flow corruption")

    broker._orders_by_client_id[pending["client_order_id"]] = {
        "status": "filled", "filled_qty": 10, "filled_avg_price": 94.0, "id": "b-partial",
    }
    resolved = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert resolved["state"] == states.CLOSED
    assert resolved["remaining_qty"] == 0
    assert resolved["realized_pnl"] == pytest.approx(10 * (94.0 - 100.0))


def test_repeated_reconciliation_with_unchanged_broker_state_is_idempotent():
    record, broker = _filled_position(qty=10, broker_submit_responses=[_partial_response(4, price=94.0)])
    pending = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    broker._orders_by_client_id[pending["client_order_id"]] = {
        "status": "partially_filled", "filled_qty": 4, "filled_avg_price": 94.0, "id": "b-partial",
    }
    first = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    second = lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
    assert first["remaining_qty"] == second["remaining_qty"] == 6
    assert first["realized_pnl"] == second["realized_pnl"] == pytest.approx(4 * (94.0 - 100.0))


def test_concurrent_reconciliation_calls_reach_same_final_state():
    import threading

    record, broker = _filled_position(qty=10, broker_submit_responses=[_partial_response(4, price=94.0)])
    pending = lifecycle.check_and_manage(record["position_id"], current_price=94.0, now=MID_SESSION_NOW, broker=broker)
    broker._orders_by_client_id[pending["client_order_id"]] = {
        "status": "filled", "filled_qty": 10, "filled_avg_price": 94.0, "id": "b-partial",
    }
    state_db.open_db()  # pre-warm before spawning threads (SQLite first-use race)
    barrier = threading.Barrier(3)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=2)
            lifecycle.reconcile_pending_exit(record["position_id"], broker=broker)
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)

    assert not errors
    final = store.load_position(record["position_id"])
    assert final["state"] == states.CLOSED
    assert final["remaining_qty"] == 0
    assert final["realized_pnl"] == pytest.approx(10 * (94.0 - 100.0))
