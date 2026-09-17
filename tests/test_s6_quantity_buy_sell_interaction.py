"""The BUY side and the SELL side of `quantity`, exercised TOGETHER.

Two defects were fixed separately and each is covered by its own suite:

  * the BUY sync re-applied a cumulative fill it had already applied, so
    every tick undid whatever a SELL had taken off (VIAV: repaired to 3,
    back to 4 within 90 seconds)
  * a confirmed partial SELL was applied twice, once by the fill sync and
    again by dead-sell recovery (10 held, 4 sold, ended at 2)

Separately green, they can still be wrong together: they write the same
column from opposite directions, and the BUY fix's correctness depends on
the SELL reduction leaving `entry_filled_quantity` alone while the SELL
fix's correctness depends on the BUY sync not resurrecting what it took.
Everything here drives both paths against one position.

The contract:

    quantity              = shares CURRENTLY HELD
    entry_filled_quantity = cumulative BUY fill ALREADY APPLIED

A BUY sync applies `cumulative - entry_filled_quantity` and nothing else.
A SELL reduction moves `quantity` and never `entry_filled_quantity`.
"""
from datetime import datetime, timezone

import pytest

from s6_live import exit_runtime, position_store
from state_store import exit_intent_ledger as eil
from state_store.db import open_db

NOW = datetime(2026, 9, 17, 9, 11, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s.db"))
    import state_store.db as db
    monkeypatch.setattr(db, "DEFAULT_DB_FILE", tmp_path / "s.db", raising=False)
    with open_db() as c:
        yield c


def _held(conn, pid):
    return int(position_store.load(conn, pid)["quantity"])


def _applied(conn, pid):
    v = position_store.load(conn, pid)["entry_filled_quantity"]
    return None if v is None else int(v)


def _bought(conn, cumulative, symbol="VIAV", price=35.6):
    """A position opened by a BUY that filled `cumulative` shares."""
    pid = position_store.record_submission(
        conn, symbol=symbol, variant="S6-P", entry_session="PREMARKET",
        range_high=36.0, range_low=35.0, entry_volume_expansion=2.0,
        client_order_id=f"c-{symbol}")
    position_store.open_from_fill(
        conn, pid, quantity=cumulative, average_fill_price=price,
        venue="NASDAQ", entry_order_id="o-1", now=NOW)
    return pid


def _buy_sync(conn, pid, cumulative, price=35.6):
    return position_store.apply_fill(
        conn, pid, filled_quantity=cumulative, average_fill_price=price,
        venue="NASDAQ", entry_order_id="o-1", now=NOW)


def _sell(conn, pid, cumulative):
    """Apply a cumulative confirmed SELL through the one owner."""
    intent = exit_runtime._active_intent(conn, pid)
    return exit_runtime.apply_confirmed_exit_fill(
        conn, pid, intent, cumulative, now=NOW)


def _with_exit_intent(conn, pid, qty):
    eil.reserve(conn, pid, "RANGE_REENTRY", qty, f"s6exit-{pid[-6:]}")


# -- A. the VIAV shape ----------------------------------------------------

def test_A_a_partial_sell_survives_a_repeated_buy_sync(conn):
    pid = _bought(conn, 4)
    _with_exit_intent(conn, pid, 4)
    assert (_held(conn, pid), _applied(conn, pid)) == (4, 4)

    _sell(conn, pid, 1)
    assert _held(conn, pid) == 3, "the sold share is off the position"
    assert _applied(conn, pid) == 4, "the BUY record is untouched by a SELL"

    _buy_sync(conn, pid, 4)
    assert _held(conn, pid) == 3, "the BUY sync must not resurrect the share"
    assert _applied(conn, pid) == 4


# -- B. and it keeps surviving -------------------------------------------

def test_B_five_more_buy_syncs_change_nothing(conn):
    pid = _bought(conn, 4)
    _with_exit_intent(conn, pid, 4)
    _sell(conn, pid, 1)

    for _ in range(5):
        _buy_sync(conn, pid, 4)

    assert (_held(conn, pid), _applied(conn, pid)) == (3, 4)


# -- C. the SELL grows, the BUY sync still does nothing -------------------

def test_C_a_growing_sell_then_a_stale_buy_sync(conn):
    pid = _bought(conn, 4)
    _with_exit_intent(conn, pid, 4)

    _sell(conn, pid, 1)
    assert _held(conn, pid) == 3
    _sell(conn, pid, 2)
    assert _held(conn, pid) == 2, "only the increment is applied"

    _buy_sync(conn, pid, 4)
    assert _held(conn, pid) == 2, "never back to 3, never to 4"
    assert _applied(conn, pid) == 4


# -- D. a late BUY fill is real news and must land ------------------------

def test_D_a_late_buy_fill_raises_the_position_by_its_delta(conn):
    """The BUY fix must not become "ignore the broker". A cumulative that
    GROWS is genuine news and its increment belongs on the position."""
    pid = _bought(conn, 3)
    _with_exit_intent(conn, pid, 3)
    _sell(conn, pid, 1)
    assert (_held(conn, pid), _applied(conn, pid)) == (2, 3)

    assert _buy_sync(conn, pid, 4) is True, "a grown cumulative is applied"
    assert _held(conn, pid) == 3, "+1, not +4"
    assert _applied(conn, pid) == 4

    for _ in range(3):
        _buy_sync(conn, pid, 4)
    assert (_held(conn, pid), _applied(conn, pid)) == (3, 4)


# -- E. neither abandon path can double-reduce ---------------------------

def test_E_dead_recovery_and_stale_timeout_cannot_double_reduce(conn):
    """105d73c's guarantee, re-asserted with the BUY column present: the
    reduction owner is the only reducer, and the abandon paths re-applying
    an already-applied cumulative is exactly what produced 10 -> 6 -> 2."""
    pid = _bought(conn, 10)
    _with_exit_intent(conn, pid, 10)

    _sell(conn, pid, 4)
    assert _held(conn, pid) == 6

    # the same cumulative arriving again from either abandon path
    for _ in range(4):
        _sell(conn, pid, 4)
    assert _held(conn, pid) == 6, "never 2"

    _sell(conn, pid, 7)
    assert _held(conn, pid) == 3, "only the delta of 3 applied"
    assert _applied(conn, pid) == 10, "the BUY record is still the BUY record"

    _buy_sync(conn, pid, 10)
    assert _held(conn, pid) == 3, "and the BUY sync does not undo it"


def test_E2_the_reduction_still_has_exactly_one_owner():
    import ast
    import inspect

    callers = set()
    for mod in (exit_runtime,):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for call in ast.walk(node):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "reduce_after_partial_exit"):
                    callers.add(node.name)
    assert callers == {"apply_confirmed_exit_fill"}, sorted(callers)


# -- F. it survives a restart --------------------------------------------

def test_F_sold_shares_do_not_resurrect_across_a_reopen(conn, tmp_path):
    pid = _bought(conn, 4)
    _with_exit_intent(conn, pid, 4)
    _sell(conn, pid, 1)
    assert _held(conn, pid) == 3
    conn.commit()

    with open_db() as reopened:
        assert _held(reopened, pid) == 3
        assert _applied(reopened, pid) == 4, (
            "the applied-BUY marker must be durable, not in-memory -- "
            "a process restart is how VIAV's repair was lost")
        _buy_sync(reopened, pid, 4)
        assert _held(reopened, pid) == 3
        reopened.commit()

    with open_db() as again:
        assert (_held(again, pid), _applied(again, pid)) == (3, 4)


# -- G. a closed position stays closed ------------------------------------

def test_G_a_full_exit_is_not_resurrected_by_a_stale_buy_cumulative(conn):
    pid = _bought(conn, 4)
    _with_exit_intent(conn, pid, 4)
    position_store.close_position(conn, pid, reason="RANGE_REENTRY",
                                  exit_price=36.0, now=NOW)
    row = position_store.load(conn, pid)
    assert row["status"] == position_store.CLOSED

    for _ in range(3):
        _buy_sync(conn, pid, 4)

    row = position_store.load(conn, pid)
    assert row["status"] == position_store.CLOSED, (
        "a stale BUY cumulative must never reopen a closed position")
    assert row["closed_at"] is not None
    assert row["exit_price"] == pytest.approx(36.0)
