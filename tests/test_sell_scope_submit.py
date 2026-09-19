"""A SELL does not read the fill history to send one order.

The measurement, on HAYW's exit of 2026-09-18:

    positions_read_ms      18,783.8
    open_orders_read_ms    20,516.0
    fills_read_ms         170,554.x     <- removed here
    intent 19:36:43.826 -> submit 19:41:16.720 = 272.9s

and the history bought nothing: the snapshot was CLEAN, so
`sell_safe_evidence` -- the only reader of what the fills produce -- was
never consulted. There is no PROTECTIVE_EXIT line in that day's log.

What the fill history actually feeds is one clause of one rule: the
fill-derived half of `order_dirty_symbols`, read by rule 6. These tests
pin what is LEFT, because that is the claim worth disproving -- every
other protection a SELL has reads the positions and the open orders,
which are still collected inline:

    BROKER_REPORTS_FLAT        the broker says zero          rule 8
    QTY_EXCEEDS_CONFIRMED      min(local, broker) cap        rule 9
    BROKER_QTY_UNCONFIRMED     two reads disagree            rule 7
    SELL_ALREADY_PENDING       open order / other intent     rule 4
    SUBMISSION_UNKNOWN_*       an UNKNOWN order              rule 5

and DEAD_SELL recovery never touched this snapshot at all: it asks
`kis_fill_inquiry` about one order at a time, from the exit monitor.
"""
import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest

from execution import sell_safe_evidence as sse
from reconciliation import snapshot as snap
from reconciliation.snapshot import ReconciliationSnapshot

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE = (REPO_ROOT / "execution" / "execution_engine.py").read_text()
NOW = datetime(2026, 9, 18, 19, 36, tzinfo=timezone.utc)
ACCOUNT = "12345678"


def _executable(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.dump(tree)


def _snapshot(**overrides):
    kwargs = dict(
        account_id=ACCOUNT, symbol="AAPL", checked_at=NOW, positions_match=False,
        open_orders_match=True, fills_match=True, has_unknown_orders=False,
        source="test", detail=("position mismatch for ZZZ: internal=0 KIS=3",),
        kis_position_quantities=(("AAPL", 2), ("ZZZ", 3)),
        position_mismatch_symbols=frozenset({"ZZZ"}),
    )
    kwargs.update(overrides)
    return ReconciliationSnapshot(**kwargs)


def _evidence(**overrides):
    kwargs = dict(local=sse.LocalPositionEvidence(
                      position_id="s6pos_1", status="OPEN", remaining_quantity=2,
                      entry_price=100.0, exit_submitted=False),
                  broker_position_read_ok=True, broker_position_quantity=2,
                  broker_open_order_for_symbol=False, unknown_orders_for_symbol=0,
                  other_active_exit_intents=0, collected_at=NOW)
    kwargs.update(overrides)
    return sse.SellSafeEvidence(**kwargs)


def _judge(snapshot=None, evidence=None, quantity=2, symbol="AAPL"):
    return sse.evaluate_protective_exit(
        snapshot=snapshot if snapshot is not None else _snapshot(),
        symbol=symbol, quantity=quantity,
        evidence=evidence if evidence is not None else _evidence(),
        now=NOW, account_id=ACCOUNT)


# -- 1. the SELL asks for the submit scope -------------------------------

def test_1_the_sell_now_uses_the_submit_scope():
    call = ENGINE[ENGINE.index("snapshot = _reconcile_now("):]
    call = call[:call.index("reconciliation_dirty")]
    assert "SCOPE_SUBMIT" in call
    assert "SCOPE_FULL" not in call, (
        "the scope no longer branches on side; a SELL reads what a BUY reads")
    assert 'side_label == "sell"' in call, (
        "TCN-02A is unchanged: only a SELL defers a dirty snapshot to the gate")
    assert "defer_dirty_to_gate" in call


def test_1b_the_fill_sweep_is_behind_the_full_guard_and_is_not_reached():
    """The property, not the spelling."""
    body = inspect.getsource(snap.build_snapshot)
    assert body.index("if scope == SCOPE_FULL:") < body.index("fill_window.read_fills(")


def test_1c_a_sell_snapshot_never_calls_get_fills(tmp_path, monkeypatch, caplog):
    """Behavioural: a broker whose fill history raises is never asked."""
    import logging
    import tempfile

    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    class Position:
        def __init__(self, symbol, quantity):
            self.symbol, self.quantity = symbol, quantity

    class Broker:
        def __init__(self):
            self.asked = []

        def get_positions(self):
            self.asked.append("positions")
            return [Position("AAPL", 2)]

        def get_open_orders(self):
            self.asked.append("open_orders")
            return []

        def get_fills(self, **_kwargs):
            self.asked.append("fills")
            raise AssertionError("the submit scope must never read the fill history")

    broker = Broker()
    with open_db() as conn, caplog.at_level(logging.INFO, logger="reconciliation.snapshot"):
        result = snap.build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT, symbol="AAPL",
            now=NOW, source="test", scope=snap.SCOPE_SUBMIT)
    assert result is not None
    assert broker.asked == ["positions", "open_orders"], broker.asked

    line = [r.getMessage() for r in caplog.records
            if "RECONCILIATION_SNAPSHOT_TIMING" in r.getMessage()]
    assert line, "the timing line must still be emitted"
    assert "scope=submit" in line[-1]
    assert "fills_collected=False" in line[-1]
    assert "fills_read_ms=0.0" in line[-1]


# -- 2-4. the quantity protections, which read the POSITIONS -------------

def test_2_a_broker_that_reports_flat_refuses_the_sell():
    verdict = _judge(snapshot=_snapshot(kis_position_quantities=(("AAPL", 0),)),
                     evidence=_evidence(broker_position_quantity=0))
    assert not verdict.permitted
    assert verdict.reason_code == sse.BROKER_REPORTS_FLAT


def test_3_a_quantity_above_what_both_sides_confirm_is_refused():
    verdict = _judge(quantity=5)
    assert not verdict.permitted
    assert verdict.reason_code == sse.QTY_EXCEEDS_CONFIRMED
    assert verdict.max_quantity == 2, "the refusal carries the sellable cap"


def test_4_two_broker_reads_that_disagree_refuse_the_sell():
    """The position is moving; neither number is evidence."""
    verdict = _judge(snapshot=_snapshot(kis_position_quantities=(("AAPL", 2),)),
                     evidence=_evidence(broker_position_quantity=1))
    assert not verdict.permitted
    assert verdict.reason_code == sse.BROKER_QTY_UNCONFIRMED


# -- 5-7. the duplicate-sell protections, which read the OPEN ORDERS -----

def test_5_an_open_sell_at_the_broker_refuses_a_second_one():
    verdict = _judge(evidence=_evidence(broker_open_order_for_symbol=True))
    assert not verdict.permitted
    assert verdict.reason_code == sse.SELL_ALREADY_PENDING


def test_6_another_active_exit_intent_refuses_the_sell():
    verdict = _judge(evidence=_evidence(other_active_exit_intents=1))
    assert not verdict.permitted
    assert verdict.reason_code == sse.SELL_ALREADY_PENDING


def test_7_an_unknown_order_for_the_symbol_refuses_the_sell():
    verdict = _judge(evidence=_evidence(unknown_orders_for_symbol=1))
    assert not verdict.permitted
    assert verdict.reason_code == sse.SUBMISSION_UNKNOWN_FOR_SYMBOL


def test_7b_the_snapshots_own_unknown_symbol_set_also_refuses():
    verdict = _judge(snapshot=_snapshot(
        has_unknown_orders=True, unknown_order_symbols=frozenset({"AAPL"})))
    assert not verdict.permitted
    assert verdict.reason_code == sse.SUBMISSION_UNKNOWN_FOR_SYMBOL


# -- 8-9. the engine's own guarantees ------------------------------------

def test_8_the_single_run_lock_still_wraps_registration():
    """Registration happens under the lock, and the lock is taken before
    anything is registered -- unchanged by the scope."""
    block = ENGINE[ENGINE.index("execution_critical.enter_context(idempotency"):]
    block = block[:block.index("pre_submit_check")]
    assert "idempotency.single_run_lock()" in block
    assert "_register_and_start(" in block
    assert block.index("single_run_lock()") < block.index("_register_and_start(")


def test_9_a_dirty_sell_is_still_deferred_to_the_gate():
    """TCN-02A survives the scope change: the deferral is keyed on side,
    not on how much of the account was read."""
    code = _executable(ENGINE)
    assert "defer_dirty_to_gate" in ENGINE
    reconcile = ENGINE[ENGINE.index("def _reconcile_now("):]
    reconcile = reconcile[:reconcile.index("\ndef ")]
    assert "if defer_dirty_to_gate:" in reconcile
    assert "sell_needs_evidence()" in reconcile
    assert "return snapshot" in reconcile
    # and the gate still refuses a dirty sell that fails the evidence
    assert "sell_safe_evidence" in (
        REPO_ROOT / "execution" / "order_gate.py").read_text()


# -- 10. DEAD_SELL recovery was never a reader of this snapshot ----------

def test_10_dead_sell_recovery_does_not_read_the_pre_submit_snapshot():
    runtime = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
    body = runtime[runtime.index("def recover_dead_exits("):]
    body = body[:body.index("\ndef ")]
    # Comments stripped so the guard reads the code, not the prose.
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    for forbidden in ("build_snapshot", "fill_window", "read_fills",
                      "order_dirty_symbols", "SCOPE_FULL", "SCOPE_SUBMIT"):
        assert forbidden not in code, forbidden
    # what it DOES use: the broker's positions and a per-order inquiry
    assert "get_positions" in body
    assert "fills_for" in body

    supplier = (REPO_ROOT / "scripts" / "run_s6_runtime.py").read_text()
    lookup = supplier[supplier.index("def _fill_lookup("):]
    lookup = lookup[:lookup.index("\ndef ")]
    assert "kis_fill_inquiry.inquire(" in lookup
    assert "broker_order_id=order_id" in lookup, (
        "recovery asks about ONE order, not a window")


def test_10b_every_other_reader_of_the_paged_window_is_the_canonical_pass():
    """The window is still read -- three times, all of them by the
    reconciliation cron. What changed is that the SUBMIT path no longer
    waits for it. `execution/` and `s6_live/` must never read it."""
    import subprocess

    found = sorted(subprocess.run(
        ["grep", "-rln", "fill_window.read_fills", "--include=*.py",
         "execution", "reconciliation", "s6_live", "scripts"],
        cwd=str(REPO_ROOT), capture_output=True, text=True).stdout.split())
    assert found == ["reconciliation/exit_intent_resolution.py",
                     "reconciliation/snapshot.py",
                     "scripts/run_reconciliation.py"], found
    # none of them sits on the submit path
    for name in ("execution/execution_engine.py", "s6_live/exit_runtime.py"):
        assert "fill_window" not in (REPO_ROOT / name).read_text(), name
    # and exit_intent_resolution is reached only from the cron entrypoint
    cron = (REPO_ROOT / "scripts" / "run_reconciliation.py").read_text()
    assert "exit_intent_resolution.resolve_unknown_exit_intents(" in cron
