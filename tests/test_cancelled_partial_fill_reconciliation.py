"""A CANCELLED order that filled part way is not a contradiction.

An order's STATUS answers what happened to the REMAINDER of it. The fill
QUANTITY answers what actually executed. They are orthogonal, and a
protective SELL that gets some of its shares away before the cancel reaches
the broker ends CANCELLED having filled more than zero. That is the
ordinary shape of the event, not evidence of disagreement.

Reconciliation treated any KIS fill row for a REJECTED/CANCELLED order as a
mismatch without ever asking how much we thought it had filled.

VIAV, 2026-09-17: SELL 4 accepted, 1 share filled, the remaining 3
cancelled. Recorded internally as CANCELLED with confirmed_filled_qty 1 --
which is exactly right. Reconciliation called it ORDER_MISMATCH anyway, and
because reconciliation was not OK the gate refused every replacement SELL
for six hours, against a position that had by then been made perfectly
consistent with the account.

Agreement is still required and only agreement is accepted: no internal
record of a fill, or a quantity that disagrees, still fails closed.
"""
import pytest

from reconciliation import snapshot
from state_store import db as state_db

NOW = "2026-09-17T09:11:00+00:00"
ORDER = "0030692909"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "S.db"))
    c = state_db.open_db()
    yield c
    c.close()


def _order(conn, *, order_id=ORDER, status="CANCELLED", requested=4.0, symbol="VIAV"):
    conn.execute(
        "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, symbol, "
        "side, trading_date, broker_order_id, status, created_at, updated_at, "
        "requested_quantity, version) VALUES (?,?,?,'sell','20260917',?,?,?,?,?,0)",
        (f"s6exit-{symbol}-{order_id}", f"sig-{order_id}", symbol, order_id,
         status, NOW, NOW, requested))
    conn.commit()


def _intent(conn, *, order_id=ORDER, filled=1.0, requested=4.0, state="ABORTED"):
    conn.execute(
        "INSERT INTO exit_intents (intent_id, position_id, reason, requested_qty, "
        "confirmed_filled_qty, state, broker_order_id, client_order_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"intent-{order_id}", "s6pos_x", "RANGE_REENTRY", requested, filled,
         state, order_id, f"coid-{order_id}", NOW, NOW))
    conn.commit()


def _kis_fill(qty, order_id=ORDER, symbol="VIAV"):
    return {"odno": order_id, "pdno": symbol, "ft_ccld_qty": str(qty)}


def _check(conn, fills):
    return snapshot._check_fills(conn, fills, internal_live_ids=set())


# -- 1. the VIAV shape ----------------------------------------------------

def test_1_cancelled_with_an_agreeing_partial_fill_is_accepted(conn):
    _order(conn)
    _intent(conn, filled=1.0)
    ok, detail, dirty = _check(conn, [_kis_fill(1)])
    assert ok, detail
    assert detail == []
    assert dirty == set()


# -- 2. an ordinary cancel with no fill ----------------------------------

def test_2_cancelled_with_no_fill_anywhere_is_valid(conn):
    _order(conn)
    _intent(conn, filled=0.0)
    ok, detail, _ = _check(conn, [])
    assert ok and detail == []


# -- 3 & 4. disagreement still fails closed ------------------------------

def test_3_broker_filled_but_we_recorded_nothing_fails_closed(conn):
    """No durable internal fill evidence must never be read as agreement."""
    _order(conn)  # deliberately no intent row at all
    ok, detail, dirty = _check(conn, [_kis_fill(1)])
    assert not ok
    assert "no internal record of any fill" in detail[0]
    assert dirty == {"VIAV"}


def test_3b_an_intent_recording_zero_against_a_broker_fill_fails_closed(conn):
    _order(conn)
    _intent(conn, filled=0.0)
    ok, detail, dirty = _check(conn, [_kis_fill(1)])
    assert not ok
    assert "filled 0.0" in detail[0]
    assert dirty == {"VIAV"}


def test_4_internal_greater_than_broker_fails_closed(conn):
    _order(conn)
    _intent(conn, filled=2.0)
    ok, detail, dirty = _check(conn, [_kis_fill(1)])
    assert not ok
    assert "filled 2.0" in detail[0]
    assert dirty == {"VIAV"}


def test_4b_broker_greater_than_internal_fails_closed(conn):
    _order(conn)
    _intent(conn, filled=1.0)
    ok, detail, _ = _check(conn, [_kis_fill(3)])
    assert not ok


# -- 5 & 6. untouched behaviour ------------------------------------------

def test_5_a_filled_order_is_not_a_dead_order_and_is_unaffected(conn):
    _order(conn, status="FILLED", requested=4.0)
    _intent(conn, filled=4.0, state="CONFIRMED")
    ok, detail, _ = _check(conn, [_kis_fill(4)])
    assert ok and detail == []


def test_6_an_accepted_order_with_a_partial_fill_is_unaffected(conn):
    _order(conn, status="ACCEPTED")
    ok, detail, _ = _check(conn, [_kis_fill(1)])
    assert ok and detail == []


def test_6b_overfill_against_the_request_still_reported(conn):
    """The other arm of this function must keep working."""
    _order(conn, status="ACCEPTED", requested=1.0)
    ok, detail, dirty = _check(conn, [_kis_fill(5)])
    assert not ok
    assert "exceeds the internally requested quantity" in detail[0]
    assert dirty == {"VIAV"}


def test_a_rejected_order_with_an_agreeing_fill_is_also_accepted(conn):
    """The rule is about status-vs-quantity, not about CANCELLED alone."""
    _order(conn, status="REJECTED")
    _intent(conn, filled=1.0)
    ok, detail, _ = _check(conn, [_kis_fill(1)])
    assert ok and detail == []


# -- 8. nothing is mutated ------------------------------------------------

def test_8_reconciliation_mutates_no_history(conn):
    _order(conn)
    _intent(conn, filled=1.0)
    before_order = dict(conn.execute(
        "SELECT status, requested_quantity FROM kis_order_idempotency "
        "WHERE broker_order_id=?", (ORDER,)).fetchone())
    before_intent = dict(conn.execute(
        "SELECT state, confirmed_filled_qty FROM exit_intents "
        "WHERE broker_order_id=?", (ORDER,)).fetchone())

    _check(conn, [_kis_fill(1)])

    assert dict(conn.execute(
        "SELECT status, requested_quantity FROM kis_order_idempotency "
        "WHERE broker_order_id=?", (ORDER,)).fetchone()) == before_order
    assert dict(conn.execute(
        "SELECT state, confirmed_filled_qty FROM exit_intents "
        "WHERE broker_order_id=?", (ORDER,)).fetchone()) == before_intent
    assert before_order["status"] == "CANCELLED", (
        "the terminal status is the truth and must not be rewritten to FILLED")


# -- 9. reconciliation places no orders -----------------------------------

def test_9_the_check_never_reaches_a_broker():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(snapshot._check_fills))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    forbidden = {"submit_buy_order", "submit_sell_order", "cancel_order",
                 "place_order", "submit"}
    assert not (called & forbidden), sorted(called & forbidden)


def test_the_rule_is_not_symbol_specific():
    """No VIAV, no order id, no symbol list anywhere in the branch."""
    import inspect

    source = inspect.getsource(snapshot._check_fills)
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))
    assert "VIAV" not in code
    assert ORDER not in code


def test_unreadable_internal_evidence_reports_rather_than_accepts(conn, monkeypatch):
    """If the ledger cannot be read, nothing is silently accepted."""
    from state_store import exit_intent_ledger as eil

    _order(conn)
    _intent(conn, filled=1.0)
    monkeypatch.setattr(eil, "confirmed_fill_by_broker_order_id",
                        lambda c: (_ for _ in ()).throw(RuntimeError("ledger down")))
    ok, detail, _ = _check(conn, [_kis_fill(1)])
    assert not ok, "unreadable evidence is not agreement"
