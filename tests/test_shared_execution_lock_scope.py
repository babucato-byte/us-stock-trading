"""The execution lock covers the decision and the write, not the reads.

LKQ, 2026-09-17: a single BUY held the shared broker-mutation lock for 213.8
seconds. The engine's own reconciliation evidence -- three venue sweeps and
a paged fill history reaching back to the oldest order still believed live --
was collected inside it, four cron ticks were dropped, and the signal being
authorized aged past its 180s budget on the way to the broker.

The reads were never the problem; where the lock sat was. It is now taken by
the ENGINE, after that evidence is gathered and before anything is decided or
written, on both the BUY and the SELL path -- the same lock file, so the two
stay serialised against each other exactly as before.

What that ordering has to get right, and what these tests pin:

  * a RECONCILIATION refusal is a real judgement about the account. It still
    registers a durable attempt, still rejects it with RECONCILIATION_BLOCKED,
    and still consumes the candidate for the day. It needs no lock: writing a
    rejection never touches the broker.

  * a LOCK that is busy, or a snapshot that went stale while waiting for it,
    is infrastructure, not a verdict on the trade. Neither may register
    anything, because the idempotency key matches on
    (signal_id, symbol, side, trading_date) regardless of status -- one
    attempt per candidate per day, by design. Burning it on contention would
    turn a busy moment into a lost trade.
"""
import ast
import inspect
from pathlib import Path

import pytest

from execution import execution_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE = (REPO_ROOT / "execution" / "execution_engine.py").read_text()
KLT = (REPO_ROOT / "kis_live_trading.py").read_text()
S6_EXIT = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
S1_EXIT = (REPO_ROOT / "s1_live" / "exit_runtime.py").read_text()
ADAPTER = (REPO_ROOT / "brokers" / "kis_broker_adapter.py").read_text()


def _engine_index(token):
    i = ENGINE.find(token)
    assert i != -1, f"{token!r} not found in the engine"
    return i


# -- K / L / M: where the lock is, and whose truth it guards ---------------

def test_K_no_deep_fill_sweep_can_happen_while_the_lock_is_held():
    """The reads precede the lock, in source order, in the one function
    that performs both."""
    assert _engine_index("snapshot = _reconcile_now(") < \
        _engine_index("execution_lock.hold(lock_owner)")
    assert _engine_index("execution_lock.hold(lock_owner)") < \
        _engine_index("execution_record = broker.submit_order(")


def test_K2_neither_caller_holds_the_lock_around_the_engine_any_more():
    assert "execution_lock.hold" not in KLT, "the BUY cycle must not hold it"
    # The SELL keeps one fallback hold for adapters that cannot carry the
    # owner -- asserted explicitly below rather than forbidden here.
    assert "lock_owner=_EXEC_LOCK_OWNER_ENTRY" in KLT
    assert "execution_lock_owner=_EXEC_LOCK_OWNER_EXIT" in S6_EXIT


def test_L_the_engine_still_collects_its_own_broker_truth():
    """CODEX-044. The lock moved; the provenance did not."""
    from reconciliation import snapshot as reconciliation_snapshot

    body = inspect.getsource(reconciliation_snapshot.build_snapshot)
    assert "kis_positions = broker.get_positions()" in body
    assert "broker_view" not in inspect.signature(
        reconciliation_snapshot.build_snapshot).parameters


def test_M_no_caller_passes_broker_truth_into_the_engine():
    for name in ("broker_view", "snapshot=", "reconciliation_snapshot="):
        assert f"submit_buy_order(\n" not in KLT or name not in KLT.split(
            "submit_buy_order(")[1][:600], name
    sig = inspect.signature(execution_engine.submit_buy_order).parameters
    assert "broker_view" not in sig and "snapshot" not in sig


# -- A: a reconciliation refusal stays terminal ---------------------------

def test_A_a_reconciliation_refusal_registers_and_rejects_without_the_lock():
    """It must still consume the candidate, and must not need the lock."""
    src = ENGINE
    block = src[src.index("except ExecutionEngineError as exc:"):
                src.index("reconciliation_dirty = not snapshot.is_clean()")]
    assert "_register_and_start(" in block, "the attempt is still registered"
    assert 'event_type="RECONCILIATION_BLOCKED"' in block
    assert "execution_lock" not in block, (
        "recording a refusal never touches the broker and must not take the "
        "broker-mutation lock")
    assert _engine_index('event_type="RECONCILIATION_BLOCKED"') < \
        _engine_index("execution_lock.hold(lock_owner)")


# -- B / C: transient conditions register nothing -------------------------

@pytest.mark.parametrize("token,reason", [
    ("REASON_EXECUTION_LOCK_UNAVAILABLE", "EXECUTION_LOCK_UNAVAILABLE"),
    ("REASON_SNAPSHOT_STALE", "RECONCILIATION_SNAPSHOT_STALE"),
])
def test_B_and_C_transient_refusals_are_named_and_distinct(token, reason):
    assert getattr(execution_engine, token) == reason
    assert getattr(execution_engine, token) != execution_engine.REASON_RECONCILIATION_DIRTY


def test_B_lock_unavailable_raises_before_anything_is_registered():
    src = ENGINE
    lock_fail = src.index("REASON_EXECUTION_LOCK_UNAVAILABLE,")
    register = src.index("record = _register_and_start(", lock_fail - 4000)
    assert lock_fail < src.index("record = _register_and_start(\n            conn,"), (
        "a busy lock must not consume the candidate's one attempt for the day")
    assert register >= 0


def test_C_a_stale_snapshot_raises_before_anything_is_registered():
    src = ENGINE
    stale = src.index("REASON_SNAPSHOT_STALE,")
    assert stale < src.index("record = _register_and_start(\n            conn,"), (
        "waiting for a lock is not a verdict on the candidate")


def test_C2_staleness_is_judged_against_the_callers_clock_plus_the_wait():
    """A caller passes a fixed `now`; judging its snapshot against
    wall-clock time compares two clocks and refuses every order."""
    assert "waited = max(0.0, time.monotonic() - evidence_gathered_at)" in ENGINE
    assert "now=current + timedelta(seconds=waited)" in ENGINE


# -- D / F: the happy path, and the final signal check --------------------

def test_D_the_locked_section_runs_in_the_required_order():
    order = [
        "execution_lock.hold(lock_owner)",
        # The LIVE registration (8-space indent), not the one on the
        # reconciliation-refusal path, which deliberately runs earlier and
        # without the lock.
        "record = _register_and_start(\n            conn,",
        "blocked = pre_submit_check()",
        "_build_gate_context, gate_fn",
        "if final_check is not None:",
        '"SUBMITTING"',
        "execution_record = broker.submit_order(",
    ]
    positions = [_engine_index(token) for token in order]
    assert positions == sorted(positions), list(zip(order, positions))


def test_F_the_final_signal_check_is_preserved_and_still_precedes_submitting():
    assert execution_engine.REASON_SIGNAL_EXPIRED_BEFORE_SUBMIT == \
        "SIGNAL_EXPIRED_BEFORE_BROKER_SUBMIT"
    assert _engine_index("if final_check is not None:") < _engine_index('"SUBMITTING"')
    assert "final_check=_final_signal_check" in KLT


# -- E: registration stays the duplicate guard ----------------------------

def test_E_registration_still_re_checks_under_the_single_run_lock():
    """A racing tick that got here first must still be refused, not
    duplicated -- the lookup happens at registration time, inside the lock,
    and the table's UNIQUE constraints are the real guarantee."""
    assert _engine_index("idempotency.single_run_lock()") < \
        _engine_index("record = _register_and_start(\n            conn,")
    helper = inspect.getsource(execution_engine._register_and_start)
    assert "idempotency.register(" in helper
    assert "DuplicateOrderAttemptError" in helper
    assert "REASON_DUPLICATE" in helper


# -- G / H / I / J: the SELL side -----------------------------------------

def test_G_the_sell_hands_the_lock_down_instead_of_wrapping_the_engine():
    assert "execution_lock_owner=_EXEC_LOCK_OWNER_EXIT" in S6_EXIT
    assert "pre_submit_check=_still_sellable" in S6_EXIT
    assert "execution_lock_owner" in S1_EXIT
    assert "lock_owner=execution_lock_owner" in ADAPTER


def test_G2_an_adapter_that_cannot_carry_the_lock_keeps_the_old_shape():
    """Fail-closed, not fail-open. Handing the re-check to an adapter that
    drops it would REMOVE a refusal -- a closed position would be sold --
    so the probe falls back to the behaviour it replaces, not to none."""
    assert "if not _accepts_execution_lock(broker_adapter):" in S6_EXIT
    fallback = S6_EXIT[S6_EXIT.index("if not _accepts_execution_lock(broker_adapter):"):]
    fallback = fallback[:fallback.index("fresh = position_store.load(conn, position_id) or row")]
    assert "execution_lock.hold(_EXEC_LOCK_OWNER_EXIT)" in fallback
    assert "_sell_still_valid(fresh)" in fallback
    assert "_ACTION_LATCHED" in fallback


def test_H_duplicate_sell_protection_is_untouched():
    assert "exit_intent_ledger.reserve(" in S1_EXIT
    assert "DuplicateExitIntentError" in S1_EXIT
    assert "active exit intent already exists" in S1_EXIT


def test_H2_a_blocked_submission_still_aborts_the_intent_and_latches():
    """Which is what makes a busy lock transient for the SELL too: the
    adapter answers 423, the intent is aborted, the position is latched,
    and `retry_latched_exits` tries again next tick."""
    assert "exit_intent_ledger.mark_aborted(conn, intent_id)" in S1_EXIT
    assert "store.latch_pending_exit(conn, position_id, reason, now=now)" in S1_EXIT


def test_I_the_exactly_once_reduction_is_untouched():
    from s6_live import exit_runtime

    owners = set()
    tree = ast.parse(S6_EXIT)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "reduce_after_partial_exit"):
                owners.add(node.name)
    assert owners == {"apply_confirmed_exit_fill"}, sorted(owners)
    assert hasattr(exit_runtime, "filled_before_abort")


def test_J_sell_reconciliation_behaviour_is_unchanged():
    """A dirty SELL snapshot is still carried to the gate (TCN-02A), and is
    still not re-judged by the new under-lock freshness check."""
    assert 'defer_dirty_to_gate=(side_label == "sell")' in ENGINE
    assert 'if not (reconciliation_dirty and side_label == "sell"):' in ENGINE
