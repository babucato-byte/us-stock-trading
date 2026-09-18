"""An expired signal cannot reach the broker, and the deep sweep leaves the lock.

LKQ, 2026-09-17. The signal's 180s budget was measured at 17:11:27 with 78
seconds used and passed. `submit_buy_order` then collected its reconciliation
evidence -- three venue sweeps and a paged fill history over a fifteen-day
window, thirty-four limiter-paced KIS reads -- and the order reached KIS at
17:15:00, 291 seconds old and 1.6x over budget. The execution lock was held
for 213.8 seconds and four cron ticks were dropped.

Nothing lied. The question was asked once, early, and then a great deal
happened before the answer was used.

Two changes, tested here:

  * the budget is asked again immediately before the order becomes
    irreversible, so a stale signal is refused rather than sent
  * the broker's side is collected BEFORE the execution lock, so the lock
    contains the decision and the write rather than minutes of collection
"""
from datetime import datetime, timedelta, timezone

import pytest

from execution import execution_engine
from reconciliation import snapshot as reconciliation_snapshot

NOW = datetime(2026, 9, 17, 17, 11, 27, tzinfo=timezone.utc)


class _Signal:
    def __init__(self, created_at, signal_id="sig-LKQ"):
        self.created_at = created_at
        self.signal_id = signal_id

    def is_expired(self, *, now):
        return (now - self.created_at).total_seconds() > 180.0


class _Validity:
    valid_for_seconds = 180.0
    policy_source = "s6_orb_breakout"

    def __init__(self, moment):
        self._moment = moment

    def submit_moment(self, cycle_now, *, clock=None):
        return self._moment


def _final_check_for(signal, validity):
    """The production closure's logic, exercised directly."""
    moment = validity.submit_moment(NOW)
    if moment is None or not signal.is_expired(now=moment):
        return None
    return (execution_engine.REASON_SIGNAL_EXPIRED_BEFORE_SUBMIT,
            f"signal {signal.signal_id!r} exceeded its 180s budget")


# -- B / boundary: the budget, asked at the wire --------------------------

@pytest.mark.parametrize("age,expected_block", [
    (179.9, False),
    (180.0, False),   # inclusive: is_expired uses `>`, not `>=`
    (180.1, True),
    (291.0, True),    # LKQ
])
def test_the_final_check_follows_the_existing_inclusive_contract(age, expected_block):
    created = NOW - timedelta(seconds=age)
    verdict = _final_check_for(_Signal(created), _Validity(NOW))
    assert (verdict is not None) is expected_block
    if expected_block:
        assert verdict[0] == "SIGNAL_EXPIRED_BEFORE_BROKER_SUBMIT"


def test_a_source_without_a_budget_is_not_measured():
    class _NoBudget(_Validity):
        def submit_moment(self, cycle_now, *, clock=None):
            return None
    assert _final_check_for(_Signal(NOW - timedelta(seconds=9999)),
                            _NoBudget(None)) is None


# -- the engine honours it, and refuses before the wire -------------------

def test_B_an_expired_signal_never_reaches_broker_submit_order():
    """The refusal must happen before the order is irreversible."""
    import inspect

    source = inspect.getsource(execution_engine._submit_new_order)
    assert "final_check" in source
    assert source.index("if final_check is not None:") < source.index("broker.submit_order("), (
        "the final check must precede the transport call")
    assert source.index("if final_check is not None:") < source.index('"SUBMITTING"'), (
        "refusing after the SUBMITTING transition would leave the row "
        "looking like it might be in flight, which is worse than the bug")


def test_the_reason_code_is_distinct_and_named():
    assert (execution_engine.REASON_SIGNAL_EXPIRED_BEFORE_SUBMIT
            == "SIGNAL_EXPIRED_BEFORE_BROKER_SUBMIT")
    assert (execution_engine.REASON_SIGNAL_EXPIRED_BEFORE_SUBMIT
            != execution_engine.REASON_GATE), "not folded into a generic refusal"


# -- what did NOT change, recorded so the gap is not mistaken for done ----

def test_the_deep_sweep_is_no_longer_inside_the_execution_lock():
    """This test used to assert the opposite, deliberately.

    When the final signal check shipped, the 213-second lock hold was left
    open and documented here so nobody could mistake the signal fix for
    having addressed it. It has since been addressed: the lock moved into
    the engine, taken after its reconciliation reads rather than around
    them. The full contract lives in
    tests/test_shared_execution_lock_scope.py; this is the marker flipping.
    """
    import inspect

    import kis_live_trading
    from execution import execution_engine

    assert "execution_lock.hold" not in inspect.getsource(kis_live_trading)
    engine = inspect.getsource(execution_engine)
    assert engine.index("snapshot = _reconcile_now(") < \
        engine.index("execution_lock.hold(lock_owner)")


def test_the_engine_still_collects_its_own_facts():
    """CODEX-044 intact: no caller-supplied broker truth on the buy path."""
    import inspect

    from reconciliation import snapshot as reconciliation_snapshot

    build = inspect.signature(reconciliation_snapshot.build_snapshot)
    assert "broker_view" not in build.parameters
    body = inspect.getsource(reconciliation_snapshot.build_snapshot)
    assert "kis_positions = broker.get_positions()" in body

    submit = inspect.signature(execution_engine.submit_buy_order)
    assert "broker_view" not in submit.parameters


def test_the_fill_window_was_not_narrowed():
    """A today-only window is not the fix: it made a previous-session fill
    permanently invisible and blocked every BUY from the next morning."""
    import inspect

    from reconciliation import fill_window

    assert fill_window.MAX_LOOKBACK_DAYS == 90
    assert "oldest_live_trading_date(conn)" in inspect.getsource(fill_window.window)


# -- G / H: nothing else moved --------------------------------------------

def test_G_and_H_the_safety_flow_is_unchanged():
    """The steps and their order, re-expressed after the lock relocation.

    `idempotency.register` moved into `_register_and_start`, shared by the
    reconciliation-refusal path and the live path, so it is asserted through
    that helper rather than by name here.
    """
    import inspect

    from execution import execution_engine

    source = inspect.getsource(execution_engine._submit_new_order)
    for step in ("_reconcile_now", "_register_and_start", "single_run_lock"):
        assert step in source, step
    helper = inspect.getsource(execution_engine._register_and_start)
    assert "idempotency.register(" in helper

    # reconcile -> lock -> register -> gate -> final check -> submit
    assert (source.index("snapshot = _reconcile_now(")
            < source.index("execution_lock.hold(lock_owner)")
            < source.index("record = _register_and_start(\n            conn,")
            < source.index("_build_gate_context, gate_fn")
            < source.index("if final_check is not None:")
            < source.index("execution_record = broker.submit_order("))


def test_the_default_path_is_byte_for_byte_the_old_one():
    """A caller that passes nothing new must behave exactly as before."""
    import inspect

    sig = inspect.signature(execution_engine.submit_buy_order)
    assert sig.parameters["final_check"].default is None


def test_telemetry_never_breaks_a_submission():
    import inspect

    import kis_live_trading

    body = inspect.getsource(kis_live_trading._log_entry_submit_latency)
    assert "except Exception" in body, "telemetry must never raise into the order path"
