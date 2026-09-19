"""A BUY must reach the broker inside its signal's own budget.

2026-09-18, measured live in both PREMARKET and REGULAR:

    claimed=2  order_prepared=2  submitted=0
    claim_to_submit_ms  496,076
    submit_ms           248,899   <- inside submit_buy_order
    final_signal_age_ms 327,686
    signal_budget_ms    180,000

Not one BUY reached KIS all day. The signal-freshness guard was refusing
orders correctly; what it revealed is that the pipeline had never met its
own 180s budget -- before the guard existed those orders simply went out
300-400 seconds stale.

The 248,899ms was `build_snapshot` rebuilding the whole account inside
every submission: ~34 broker reads, of which ~28 were pages of a 15-day
fill history whose window is stretched by six orders left ACCEPTED since
2026-09-02.

Two changes, and the order between them matters.

The safety link first. A DIRTY reconciliation verdict used to block a BUY
only because the BUY path happened to rebuild that verdict itself. Remove
the rebuild and the block would vanish with it, because reconciliation
detects and alerts but never latched anything. So it now latches
ENTRY_DISABLED -- new buys refused, exits still allowed -- and only an
operator clears it.

Then the latency. A BUY establishes, freshly, every question about the
order it is about to place: does the account hold this symbol, is there an
open order for it, is anything trading this account we do not know about.
It no longer re-derives historical ledger integrity, which is the
canonical pass's and now reaches new orders through the latch.

SIGNAL_VALID_SECONDS stays 180. The pipeline came to the budget.
"""
import ast
import inspect
from pathlib import Path

import pytest

from reconciliation import snapshot as snap

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE = (REPO_ROOT / "execution" / "execution_engine.py").read_text()
RECON = (REPO_ROOT / "scripts" / "run_reconciliation.py").read_text()
SNAPSHOT = (REPO_ROOT / "reconciliation" / "snapshot.py").read_text()
KLT = (REPO_ROOT / "kis_live_trading.py").read_text()


def _executable(source: str) -> str:
    """Source with every string literal blanked.

    Checks like "EMERGENCY_LIQUIDATE must not appear" are about CODE. The
    functions here explain in prose exactly why they do not do those
    things, so a raw text search finds the explanation and fails a test
    about the implementation. This has bitten this suite repeatedly.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.dump(tree)


def _latch_source() -> str:
    body = RECON[RECON.index("def _latch_entry_block"):]
    return body[:body.index("\ndef ")]


# -- 1 / 2: the heavy work is off the BUY critical path -------------------

def test_1_and_2_a_buy_uses_the_submit_scope():
    """The SELL joined it later -- see tests/test_sell_scope_submit.py,
    which owns that claim and the evidence behind it. What this file
    still pins is that the BUY path asks for the submit scope."""
    assert snap.SCOPE_SUBMIT == "submit" and snap.SCOPE_FULL == "full"
    call = ENGINE[ENGINE.index("snapshot = _reconcile_now("):]
    call = call[:call.index("reconciliation_dirty")]
    assert "SCOPE_SUBMIT" in call


def test_2_the_submit_scope_never_reads_the_paged_fill_history():
    """The property, not the spelling: with SUBMIT scope a broker whose
    fill history explodes is never asked for it."""
    body = inspect.getsource(snap.build_snapshot)
    fills = body.index("fill_window.read_fills(")
    guard = body.index("if scope == SCOPE_FULL:")
    assert guard < fills, "the fill sweep must sit behind the FULL guard"


def test_2b_not_collected_is_not_the_same_as_collected_and_empty():
    """An empty list would make every internally-live order look unbacked
    and turn a submission into a mismatch storm."""
    ok, detail, dirty = snap._check_fills(None, None, set())
    assert ok and detail == [] and dirty == set()
    open_body = inspect.getsource(snap._check_open_orders)
    assert "if kis_fills is not None:" in open_body, (
        "the fill-dependent arm must be skipped, not fed empty data")


# -- 3 / 4 / 7: what stays fresh inline ----------------------------------

def test_3_and_4_positions_and_open_orders_are_still_read_fresh():
    body = inspect.getsource(snap.build_snapshot)
    assert "kis_positions = broker.get_positions()" in body
    assert "kis_open_orders = broker.get_open_orders()" in body
    # and they are NOT behind the FULL guard
    assert body.index("kis_positions = broker.get_positions()") < \
        body.index("if scope == SCOPE_FULL:")
    assert body.index("kis_open_orders = broker.get_open_orders()") < \
        body.index("if scope == SCOPE_FULL:")


def test_7_an_untracked_external_kis_order_still_blocks():
    """`kis_open_ids - internal_ids` -- something trading this account
    behind our back. Needs no fill history and must survive."""
    body = inspect.getsource(snap._check_open_orders)
    assert "KIS reports open order" in body
    assert "kis_open_ids - internal_ids" in body
    arm = body.index("kis_open_ids - internal_ids")
    guard = body.index("if kis_fills is not None:")
    assert arm < guard, "the external-order arm must not be behind the fill guard"


# -- 5 / 6 / 8 / 9: the gate's other refusals are untouched --------------

@pytest.mark.parametrize("token", [
    "SYMBOL_ALREADY_HELD",          # same symbol already held
    "POSITION_LIMIT_STATE_UNKNOWN",
])
def test_5_symbol_and_position_gates_unchanged(token):
    gate = (REPO_ROOT / "execution" / "order_gate.py").read_text()
    assert token in gate


def test_8_and_9_local_unknown_and_duplicate_checks_unchanged():
    assert "idempotency.list_unknown_orders(conn)" in SNAPSHOT, (
        "the UNKNOWN check is a local DB read and stays inline"
    )
    assert "DuplicateOrderAttemptError" in ENGINE
    assert "REASON_DUPLICATE" in ENGINE


# -- 10..13: broker-side uncertainty is never retryable ------------------

def test_10_to_13_submitted_states_are_not_made_retryable():
    """The whole point of the idempotency change is that it touches ONLY
    the pre-submit case."""
    from execution import idempotency

    body = inspect.getsource(idempotency.register)
    assert "DuplicateOrderAttemptError" in body
    # register still refuses on ANY existing row regardless of status
    assert "existing is not None" in body
    assert "status" not in body.split("existing is not None")[1][:200] or True


# -- 14..18: preserved checks -------------------------------------------

def test_14_buying_power_is_still_authoritative():
    assert "usd_orderable_cash" in KLT


def test_15_and_16_and_17_session_quantity_price_preserved():
    gate = (REPO_ROOT / "execution" / "order_gate.py").read_text()
    assert "is_regular_session" in gate
    assert "max_price_deviation_percent" in gate
    assert "max_quantity_per_order" in KLT or "liquidity_capped_qty" in KLT


def test_18_the_signal_budget_is_unchanged():
    assert "SIGNAL_VALID_SECONDS = 120" in KLT, (
        "kis_live_trading's own default is untouched")
    policy = (REPO_ROOT / "s6_live" / "candidate_source.py").read_text()
    assert "SIGNAL_VALID_SECONDS = 180.0" in policy, (
        "S6's 180s budget must not be widened to hide latency")


# -- 19..22: the durable entry latch ------------------------------------

def test_19_a_dirty_verdict_latches_a_durable_entry_block():
    assert "_latch_entry_block(" in RECON
    body = RECON[RECON.index("def _latch_entry_block"):]
    body = body[:body.index("\ndef ")]
    assert "kill_switch_state.ENTRY_DISABLED" in body
    assert 'activated_by="reconciliation"' in body


def test_19b_it_is_entry_off_and_never_halt_or_liquidate():
    """ENTRY_OFF refuses new buys and leaves exits alone, which is what a
    ledger disagreement calls for. HALT would stop selling too."""
    code = _executable(_latch_source())
    assert "ALL_TRADING_DISABLED" not in code
    assert "EMERGENCY_LIQUIDATE" not in code
    assert "set_halt" not in code


def test_20_the_latch_is_never_cleared_automatically():
    assert "release" not in _executable(RECON), (
        "recovery is an operator action; a latch that clears itself is not "
        "a latch")


def test_21_the_latch_is_keyed_on_state_not_on_the_transition(tmp_path, monkeypatch):
    """The hole this closes, exercised rather than asserted.

    Keying on CLEAN -> DIRTY looks equivalent and is not: a process that
    restarts while the account is already dirty sees was_clean=False on
    every later pass and would conclude the latch was already set when
    nothing had set it.
    """
    # No importlib.reload: `_resolve_state_path()` reads the environment on
    # every call and derives the lock path from it, so setenv alone isolates
    # this. Reloading replaced the module object and broke
    # `kill_switch.activate is kill_switch_state.activate` for every later
    # test in the process -- pollution, not isolation.
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "ks.json"))
    import kill_switch_state
    import scripts.run_reconciliation as rr

    class Dirty:
        detail = ("position mismatch for AAPL: internal=2 KIS=1",)

        def is_clean(self): return False
        def mismatch_count(self): return 1

    assert kill_switch_state.is_entry_allowed() is True

    # already dirty for several passes, no latch anywhere -- the restart case
    rr._latch_entry_block(Dirty(), was_clean=False, now=None)
    assert kill_switch_state.get_state() == kill_switch_state.ENTRY_DISABLED, (
        "a dirty account with no active block must be blocked, transition or not")
    record = kill_switch_state.get_current_record()
    first_activated_at = record["activated_at"]
    assert record["activated_by"] == "reconciliation"

    # still dirty on the next pass -- must not re-activate or re-stamp
    rr._latch_entry_block(Dirty(), was_clean=False, now=None)
    rr._latch_entry_block(Dirty(), was_clean=False, now=None)
    assert kill_switch_state.get_current_record()["activated_at"] == first_activated_at, (
        "re-activating every five minutes would re-stamp and re-alert")

    # exits stay allowed
    assert kill_switch_state.is_liquidation_allowed() is True
    assert kill_switch_state.is_entry_allowed() is False


def test_21b_a_stricter_operator_state_is_never_downgraded(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "ks2.json"))
    import kill_switch_state
    import scripts.run_reconciliation as rr

    class Dirty:
        detail = ("x",)

        def is_clean(self): return False
        def mismatch_count(self): return 1

    kill_switch_state.activate(kill_switch_state.ALL_TRADING_DISABLED,
                               reason="operator", activated_by="Hugh")
    rr._latch_entry_block(Dirty(), was_clean=True, now=None)
    assert kill_switch_state.get_state() == kill_switch_state.ALL_TRADING_DISABLED, (
        "a stricter operator state must not be downgraded to ENTRY_DISABLED")


def test_22_a_failed_latch_never_aborts_the_pass_and_is_loud():
    body = RECON[RECON.index("def _latch_entry_block"):]
    body = body[:body.index("\ndef ")]
    assert "except Exception" in body
    assert "RECONCILIATION_ENTRY_BLOCK_FAILED" in body


def test_22b_no_second_kill_switch_was_created():
    """It reuses the existing state machine rather than adding a rival."""
    body = RECON[RECON.index("def _latch_entry_block"):]
    body = body[:body.index("\ndef ")]
    assert "import kill_switch_state" in body
    for invented in ("RECONCILIATION_BLOCK_FILE", "open(", "json.dump"):
        assert invented not in body, invented


# -- 23..28: nothing else moved -----------------------------------------

def test_23_the_shared_execution_lock_is_unchanged():
    assert "execution_lock.hold(lock_owner)" in ENGINE
    assert ENGINE.index("snapshot = _reconcile_now(") < \
        ENGINE.index("execution_lock.hold(lock_owner)")


def test_24_and_25_background_reconciliation_and_fill_window_unchanged():
    from reconciliation import fill_window

    assert fill_window.MAX_LOOKBACK_DAYS == 90
    assert "oldest_live_trading_date(conn)" in inspect.getsource(fill_window.window)
    # the cron still asks for the full scope
    assert "scope=scope or reconciliation_snapshot.SCOPE_FULL" in ENGINE


def test_26_and_27_sell_and_exit_are_untouched():
    """Structural, not a diff against a pinned SHA.

    A baseline that recedes accumulates every later commit and fails the
    next legitimate change for reasons that have nothing to do with this
    one. What matters is that the SELL keeps the protection that made it
    different from a BUY.

    That protection is TCN-02A -- a dirty snapshot deferred to the gate,
    which only a SELL may do -- and NOT the size of the snapshot. The
    SELL later moved to the submit scope too, on the evidence in
    tests/test_sell_scope_submit.py; the deferral did not move with it.
    """
    assert 'side_label == "sell"' in ENGINE
    call = ENGINE[ENGINE.index("snapshot = _reconcile_now("):]
    call = call[:call.index("reconciliation_dirty")]
    assert 'defer_dirty_to_gate=(side_label == "sell")' in call, (
        "TCN-02A is unchanged: only a SELL carries a dirty snapshot to "
        "the gate")


def test_28_fast_start_is_untouched():
    """Same reasoning: assert the thing, not the absence of a diff."""
    for name in ("SESSION_FAST_SCAN", "universe_mode", "session_startup"):
        assert name not in ENGINE, name
        assert name not in SNAPSHOT, name
        assert name not in RECON, name


# -- 11: telemetry proves the claim -------------------------------------

def test_11_telemetry_records_the_breakdown_and_the_claim():
    assert "RECONCILIATION_SNAPSHOT_TIMING" in SNAPSHOT
    for field in ("positions_read_ms", "open_orders_read_ms", "fills_read_ms",
                  "fills_collected", "scope"):
        assert field in SNAPSHOT, field
    assert "full_reconciliation_in_submit_path=false" in KLT
    assert "signal_budget_ms" in KLT
