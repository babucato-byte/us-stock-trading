"""Releasing an order row that provably never reached the broker.

The failure this covers
-----------------------
An S1 BUY for CMBT was written CREATED at 15:33 and advanced to
VALIDATING; nothing moved it again. Four hours later that row still held
a global position slot -- `2 of 2 slot(s) in use (1 held, 1 in flight)`
-- and blocked every subsequent entry, S6's included, against an order
that was never sent. KIS had no open order, no position and no fill for
the symbol, and the row's broker_order_id was NULL.

The command already existed for the SUBMITTING case. SUBMITTING is the
genuinely ambiguous one: the engine advances to it immediately BEFORE
the transport call, so it can mean "may be in flight". CREATED and
VALIDATING are strictly earlier than that boundary and cannot mean that
-- but they are held to the same evidence anyway, because the whole
value of this command is that it refuses on doubt.

Nothing here places an order. The broker is a fake that raises if asked
to trade.
"""

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution import order_repository  # noqa: E402

TOOL = REPO_ROOT / "scripts" / "release_pre_transport_orphan.py"
OID = "kislive-CMBT-310c570a452e"
NOW = "2026-08-26T15:33:03.333590+00:00"


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("STATE_STORE_DB_FILE", path)
    from state_store.db import open_db

    with open_db() as conn:
        yield conn


def _order(conn, *, status="VALIDATING", broker_order_id=None, symbol="CMBT"):
    conn.execute(
        "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, symbol, "
        "side, trading_date, broker_order_id, status, created_at, updated_at, "
        "requested_quantity, version, strategy_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (OID, "sig-1", symbol, "buy", "2026-08-26", broker_order_id, status,
         NOW, NOW, 1.0, 1, "S1_HMA_EARLY_TREND_V1"))
    order_repository.append_creation_event(conn, order_id=OID, state="CREATED")
    conn.execute(
        "INSERT INTO order_state_events (internal_order_id, from_state, to_state, "
        "event_type, payload, version, occurred_at) VALUES (?,?,?,?,NULL,1,?)",
        (OID, "CREATED", status, "VALIDATION_STARTED", NOW))
    conn.commit()


class FakeBroker:
    """Read-only. Asking it to trade is a test failure."""

    def __init__(self, open_orders=(), positions=(), fills=(), raises=None):
        self._open, self._pos, self._fills, self._raises = (
            list(open_orders), list(positions), list(fills), raises)

    def _maybe_raise(self):
        if self._raises:
            raise self._raises

    def get_open_orders(self):
        self._maybe_raise(); return self._open

    def get_positions(self):
        self._maybe_raise(); return self._pos

    def get_fills(self, start_date=None, end_date=None):
        self._maybe_raise(); return self._fills

    def submit_order(self, *a, **k):
        raise AssertionError("the release command must never place an order")


class _Pos:
    def __init__(self, symbol, quantity):
        self.symbol, self.quantity = symbol, quantity


def _run(db, broker, argv):
    """Drive main() with the fake broker patched in."""
    import scripts.release_pre_transport_orphan as tool

    old_argv, old_broker = sys.argv, tool.KISBroker
    sys.argv = ["release_pre_transport_orphan.py"] + argv
    tool.KISBroker = lambda *a, **k: broker
    try:
        return tool.main()
    finally:
        sys.argv, tool.KISBroker = old_argv, old_broker


def _status(conn):
    return conn.execute(
        "SELECT status FROM kis_order_idempotency WHERE internal_order_id=?",
        (OID,)).fetchone()[0]


class TestThePreTransportStatesAreReleasable:
    @pytest.mark.parametrize("state", ["VALIDATING", "CREATED", "SUBMITTING"])
    def test_a_clean_row_is_released_to_REJECTED(self, db, state):
        _order(db, status=state)
        assert _run(db, FakeBroker(), [OID, "--confirm"]) == 0
        assert _status(db) == "REJECTED"

    def test_the_exact_CMBT_case(self, db):
        """VALIDATING, NULL broker id, KIS shows nothing."""
        _order(db, status="VALIDATING")
        assert _run(db, FakeBroker(), [OID, "--confirm"]) == 0
        assert _status(db) == "REJECTED"

    def test_a_dry_run_changes_nothing(self, db):
        _order(db, status="VALIDATING")
        assert _run(db, FakeBroker(), [OID]) == 0
        assert _status(db) == "VALIDATING"

    def test_the_release_goes_through_the_state_machine(self, db):
        """Not raw SQL -- the event history has to show the transition."""
        _order(db, status="VALIDATING")
        _run(db, FakeBroker(), [OID, "--confirm"])
        events = db.execute(
            "SELECT from_state,to_state,event_type FROM order_state_events "
            "WHERE internal_order_id=? ORDER BY version", (OID,)).fetchall()
        assert ("VALIDATING", "REJECTED", "TRANSPORT_NOT_ATTEMPTED") == tuple(events[-1])

    def test_the_broker_order_id_stays_null(self, db):
        """That NULL is what tells entry_limits the order never landed."""
        _order(db, status="VALIDATING")
        _run(db, FakeBroker(), [OID, "--confirm"])
        assert db.execute("SELECT broker_order_id FROM kis_order_idempotency "
                          "WHERE internal_order_id=?", (OID,)).fetchone()[0] is None


class TestItRefusesWhateverItCannotProve:
    def test_a_present_broker_order_id_is_a_refusal(self, db):
        """The order reached KIS; releasing it would declare a possibly
        live order dead."""
        _order(db, status="VALIDATING", broker_order_id="0030999999")
        assert _run(db, FakeBroker(), [OID, "--confirm"]) == 1
        assert _status(db) == "VALIDATING"

    def test_a_terminal_state_is_not_releasable(self, db):
        _order(db, status="FILLED")
        assert _run(db, FakeBroker(), [OID, "--confirm"]) == 1
        assert _status(db) == "FILLED"

    def test_an_open_order_at_KIS_is_a_refusal(self, db):
        _order(db, status="VALIDATING")
        broker = FakeBroker(open_orders=[{"pdno": "CMBT", "odno": "1"}])
        assert _run(db, broker, [OID, "--confirm"]) == 1
        assert _status(db) == "VALIDATING"

    def test_a_position_at_KIS_is_a_refusal(self, db):
        _order(db, status="VALIDATING")
        assert _run(db, FakeBroker(positions=[_Pos("CMBT", 1)]),
                    [OID, "--confirm"]) == 1
        assert _status(db) == "VALIDATING"

    def test_a_fill_at_KIS_is_a_refusal(self, db):
        _order(db, status="VALIDATING")
        broker = FakeBroker(fills=[{"pdno": "CMBT", "ft_ccld_qty": "1"}])
        assert _run(db, broker, [OID, "--confirm"]) == 1
        assert _status(db) == "VALIDATING"

    def test_an_unreadable_broker_is_a_refusal(self, db):
        """Unreadable is not absent -- the distinction this command
        exists to make."""
        _order(db, status="VALIDATING")
        broker = FakeBroker(raises=RuntimeError("KIS unreachable"))
        assert _run(db, broker, [OID, "--confirm"]) == 1
        assert _status(db) == "VALIDATING"

    def test_an_unknown_order_id_is_a_refusal(self, db):
        assert _run(db, FakeBroker(), ["kislive-NOPE-1", "--confirm"]) == 1

    def test_another_symbols_trace_does_not_block_it(self, db):
        """The evidence must be scoped to this symbol."""
        _order(db, status="VALIDATING")
        broker = FakeBroker(open_orders=[{"pdno": "TX", "odno": "9"}],
                            positions=[_Pos("TX", 1)],
                            fills=[{"pdno": "TX", "ft_ccld_qty": "1"}])
        assert _run(db, broker, [OID, "--confirm"]) == 0
        assert _status(db) == "REJECTED"
