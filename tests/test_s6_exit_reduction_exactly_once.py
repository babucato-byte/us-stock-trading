"""A confirmed partial SELL reduces the held quantity EXACTLY ONCE.

The defect, confirmed against the SHA running in production
(e20eef5) on 2026-09-17: a position of 10 whose abandoned SELL filled 4
ended at 2. `sync_sell_fills` applied the fill and reduced 10 -> 6, then
`recover_dead_exits` applied the SAME broker-reported cumulative fill a
second time and reduced 6 -> 2. Four shares were removed twice.

That is the mirror image of the bug the reduction was added for. VIAV was
released for retry with `quantity` ABOVE what KIS held and every retry was
refused; this leaves it BELOW, so a retry asks for fewer shares than the
account actually has and the remainder is silently stranded.

Two independent guarantees are asserted here, and they are separate on
purpose:

  * `recover_dead_exits` must not reduce AT ALL -- its quantity guard
    returns unless the broker holds at least what the row claims, so on
    every path that reaches the release there is nothing to take off.
  * `sync_sell_fills` must be idempotent against a repeated observation
    of the same cumulative fill -- the broker reports a running total,
    not an increment, and the row stays EXIT_SUBMITTED across ticks.
"""
import pytest

from s6_live import exit_runtime, position_store
from state_store import exit_intent_ledger as eil
from tests.test_dead_sell_recovery import (  # noqa: F401 - fixture
    conn, _exit_submitted, _fill, _Broker, _P, NOW)


def _qty(conn, pid):
    return int(position_store.load(conn, pid)["quantity"])


def _submitted_with_intent(conn, qty):
    pid = _exit_submitted(conn, qty=qty)
    eil.reserve(conn, pid, "RANGE_REENTRY", qty, f"s6exit-FLS-{qty}")
    return pid


# -- 1. the confirmed failure --------------------------------------------

def test_1_ten_held_sell_fills_four_dead_recovery_leaves_six(conn):
    pid = _submitted_with_intent(conn, 10)
    fill = lambda row: _fill(filled=4, avg=78.0)  # noqa: E731

    exit_runtime.sync_sell_fills(conn, fills_for=fill, now=NOW)
    assert _qty(conn, pid) == 6, "the fill sync applies the four sold shares"

    out = exit_runtime.recover_dead_exits(
        conn, broker=_Broker([_P("FLS", 6)]), fills_for=fill, now=NOW)

    assert out[0]["status"] == exit_runtime.DEAD_SELL_RELEASED
    assert _qty(conn, pid) == 6, "never 2 -- the same four are not resold"
    assert out[0]["previously_filled"] == 4


# -- 2. the same lifecycle processed again -------------------------------

def test_2_reprocessing_the_same_lifecycle_changes_nothing(conn):
    pid = _submitted_with_intent(conn, 10)
    fill = lambda row: _fill(filled=4, avg=78.0)  # noqa: E731
    exit_runtime.sync_sell_fills(conn, fills_for=fill, now=NOW)
    exit_runtime.recover_dead_exits(
        conn, broker=_Broker([_P("FLS", 6)]), fills_for=fill, now=NOW)

    for _ in range(3):
        exit_runtime.sync_sell_fills(conn, fills_for=fill, now=NOW)
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 6)]), fills_for=fill, now=NOW)

    assert _qty(conn, pid) == 6, "never 2, and never back to 10"


# -- 3. the retry is sized to what is held -------------------------------

def test_3_the_next_retry_is_sized_six(conn):
    pid = _submitted_with_intent(conn, 10)
    fill = lambda row: _fill(filled=4, avg=78.0)  # noqa: E731
    exit_runtime.sync_sell_fills(conn, fills_for=fill, now=NOW)
    out = exit_runtime.recover_dead_exits(
        conn, broker=_Broker([_P("FLS", 6)]), fills_for=fill, now=NOW)

    row = position_store.load(conn, pid)
    assert row["status"] == position_store.EXIT_PENDING
    assert not row["exit_submitted"], "released for retry"
    assert out[0]["retryable_quantity"] == 6
    assert int(row["quantity"]) == 6, "the retry reads six, not two, not ten"


# -- 4. the stale-timeout path reduces at most once ----------------------

def test_4_the_timeout_path_never_reapplies_an_applied_fill(conn):
    """The cancel path has no broker fill lookup: all it has is the
    intent's own `confirmed_filled_qty`, which is written ONLY alongside
    the reduction it describes. Re-applying it subtracts shares the fill
    sync already took off."""
    pid = _submitted_with_intent(conn, 4)
    exit_runtime.sync_sell_fills(
        conn, fills_for=lambda row: _fill(filled=1, avg=35.6), now=NOW)
    assert _qty(conn, pid) == 3

    intent = exit_runtime._active_intent(conn, pid)
    assert exit_runtime.filled_before_abort(intent) == 1, (
        "progress records what was applied")

    for _ in range(3):
        exit_runtime.apply_confirmed_exit_fill(
            conn, pid, exit_runtime._active_intent(conn, pid),
            exit_runtime.filled_before_abort(intent), now=NOW)

    assert _qty(conn, pid) == 3, "never 2, never 1"


# -- 5. the fill-sync path reduces exactly once --------------------------

def test_5_sync_sell_fills_is_idempotent_across_ticks(conn):
    """The row stays EXIT_SUBMITTED after a partial, so the next tick sees
    the same order again. `filled_quantity` is the broker's CUMULATIVE
    total -- subtracting it every tick walked the position to zero on its
    own, with no recovery path involved."""
    pid = _submitted_with_intent(conn, 10)
    fill = lambda row: _fill(filled=4, avg=78.0)  # noqa: E731

    for _ in range(5):
        exit_runtime.sync_sell_fills(conn, fills_for=fill, now=NOW)

    assert _qty(conn, pid) == 6


def test_5b_a_growing_cumulative_fill_applies_only_its_increment(conn):
    pid = _submitted_with_intent(conn, 10)
    for cumulative, expected in ((2, 8), (2, 8), (5, 5), (5, 5), (7, 3)):
        exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row, c=cumulative: _fill(filled=c, avg=78.0),
            now=NOW)
        assert _qty(conn, pid) == expected, f"cumulative {cumulative}"


def test_5c_a_regressing_cumulative_never_returns_shares(conn):
    pid = _submitted_with_intent(conn, 10)
    exit_runtime.sync_sell_fills(
        conn, fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
    exit_runtime.sync_sell_fills(
        conn, fills_for=lambda row: _fill(filled=1, avg=78.0), now=NOW)
    assert _qty(conn, pid) == 6, "a bad read must not invent stock"


# -- 6. a full fill closes rather than leaving a zero-share row ----------

def test_6_a_full_fill_closes_and_leaves_no_zero_share_row(conn):
    pid = _submitted_with_intent(conn, 10)
    exit_runtime.sync_sell_fills(
        conn, fills_for=lambda row: _fill(filled=10, avg=78.0), now=NOW)

    row = position_store.load(conn, pid)
    assert row["status"] == position_store.CLOSED
    assert int(row["quantity"]) >= 1, (
        "s6_positions CHECK forbids a held row below one share")
    assert row["exit_price"] == pytest.approx(78.0)


def test_6b_a_partial_then_the_rest_closes(conn):
    pid = _submitted_with_intent(conn, 10)
    exit_runtime.sync_sell_fills(
        conn, fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
    assert _qty(conn, pid) == 6
    exit_runtime.sync_sell_fills(
        conn, fills_for=lambda row: _fill(filled=10, avg=78.0), now=NOW)
    assert position_store.load(conn, pid)["status"] == position_store.CLOSED


# -- the owner itself -----------------------------------------------------

def test_the_reduction_has_exactly_one_owner():
    """Three paths used to reduce independently. Only the owner may now."""
    import ast
    import inspect

    src = inspect.getsource(exit_runtime)
    tree = ast.parse(src)
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "reduce_after_partial_exit"):
                callers.add(node.name)
    assert callers == {"apply_confirmed_exit_fill"}, (
        f"only the owner may reduce; found {sorted(callers)}")
