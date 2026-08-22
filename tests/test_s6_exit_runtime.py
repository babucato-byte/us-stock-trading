"""S6's SELL goes through the shared path, and its fills through generic sync.

The decision is S6's; the submission is S1's `_submit_sell` with S6's
store passed in -- the same function S2 uses. A third copy would be a
third idea of what is safe, and three diverge faster than two.

Fill synchronisation is deliberately not gated by exit ownership.
Conflating them is what cost S1 its bookkeeping once: an exit guard
excluded a strategy wholesale and took fill sync with it, so the
position stopped being counted while still being held.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_hours import EASTERN  # noqa: E402
from s6_live import exit_runtime as er  # noqa: E402
from s6_live import position_store as ps  # noqa: E402

T0 = datetime(2026, 8, 21, 12, 0, tzinfo=EASTERN)
NEAR_CLOSE = datetime(2026, 8, 21, 15, 50, tzinfo=EASTERN)


class Features:
    def __init__(self, price=101.0, vwap=100.0, ema9=100.5, ema21=100.0,
                 volume_expansion=2.0):
        self.price, self.vwap = price, vwap
        self.ema9, self.ema21 = ema9, ema21
        self.volume_expansion = volume_expansion


class Adapter:
    def __init__(self, status=200, raises=False):
        self.calls, self._status, self._raises = [], status, raises

    def submit_order(self, symbol, quantity, *, side, client_order_id=None):
        self.calls.append({"symbol": symbol, "quantity": quantity,
                           "side": side, "client_order_id": client_order_id})
        if self._raises:
            raise RuntimeError("connection lost mid-submit")
        return type("R", (), {"status_code": self._status, "text": "ok"})()


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def opened(conn, symbol="ABC", **kw):
    pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                               entry_session="REGULAR", range_high=99.5,
                               range_low=99.0, entry_volume_expansion=2.0,
                               now=T0, **kw)
    ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                      venue="NASD", now=T0)
    return pid


class TestTheSellUsesTheSharedPath:
    def test_it_imports_the_shared_submitter(self):
        source = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        assert "from s1_live.exit_runtime import ExitOutcome, _submit_sell" \
            in source

    def test_it_defines_no_broker_call_of_its_own(self):
        import ast

        source = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for seg in name.split("."):
                        assert seg not in {"brokers", "kis_broker",
                                           "kis_broker_adapter"}, name

    def test_a_range_reentry_sells(self, conn):
        opened(conn)
        adapter = Adapter()
        outcomes = er.run_exits(
            conn, broker_adapter=adapter,
            features_fn=lambda s: Features(price=99.4),
            price_fn=lambda s: 99.4, session="REGULAR", now=T0)
        assert len(adapter.calls) == 1
        assert adapter.calls[0]["side"] == "sell"
        assert adapter.calls[0]["client_order_id"].startswith("s6exit-")
        assert outcomes[0]["reason"] == "RANGE_REENTRY"

    def test_a_healthy_position_is_held(self, conn):
        opened(conn)
        adapter = Adapter()
        outcomes = er.run_exits(
            conn, broker_adapter=adapter, features_fn=lambda s: Features(),
            price_fn=lambda s: 101.0, session="REGULAR", now=T0)
        assert adapter.calls == []
        assert outcomes[0]["action"] == er.ACTION_HELD

    def test_one_position_cannot_produce_two_sells(self, conn):
        opened(conn)
        adapter = Adapter()
        for _ in range(3):
            er.run_exits(conn, broker_adapter=adapter,
                         features_fn=lambda s: Features(price=99.4),
                         price_fn=lambda s: 99.4, session="REGULAR", now=T0)
        assert len(adapter.calls) == 1, "exit_submitted is one-way"

    def test_an_ambiguous_submit_latches_rather_than_retrying(self, conn):
        """The behaviour learned on S1: never auto-retry an ambiguous
        send, and never clear the trigger."""
        opened(conn)
        adapter = Adapter(raises=True)
        outcomes = er.run_exits(
            conn, broker_adapter=adapter,
            features_fn=lambda s: Features(price=99.4),
            price_fn=lambda s: 99.4, session="REGULAR", now=T0)
        # The shared submitter's own vocabulary, not this module's --
        # reusing the function means reusing what it reports.
        assert outcomes[0]["action"] == "SELL_BLOCKED"
        assert "submission unknown" in outcomes[0]["detail"]
        row = ps.load_by_symbol(conn, "ABC")
        assert row["status"] == ps.EXIT_PENDING
        assert row["exit_submitted"] == 0


class TestSessionExitAndRetry:
    def test_a_healthy_position_exits_at_the_boundary(self, conn):
        opened(conn)
        adapter = Adapter()
        outcomes = er.run_exits(
            conn, broker_adapter=adapter, features_fn=lambda s: Features(),
            price_fn=lambda s: 101.0, session="REGULAR", now=NEAR_CLOSE)
        assert outcomes[0]["reason"] == "SESSION_EXIT"
        assert len(adapter.calls) == 1

    def test_an_unorderable_session_latches(self, conn):
        opened(conn)
        adapter = Adapter()
        outcomes = er.run_exits(
            conn, broker_adapter=adapter,
            features_fn=lambda s: Features(price=99.4),
            price_fn=lambda s: 99.4, session="PREMARKET", now=T0,
            orders_allowed=False)
        assert adapter.calls == []
        assert outcomes[0]["action"] == er.ACTION_LATCHED
        assert ps.load_by_symbol(conn, "ABC")["status"] == ps.EXIT_PENDING

    def test_a_latched_exit_is_retried_in_the_next_window(self, conn):
        """The condition already fired and the position is already
        leaving; waiting for it to re-trigger would hold it longer."""
        opened(conn)
        er.run_exits(conn, broker_adapter=Adapter(),
                     features_fn=lambda s: Features(price=99.4),
                     price_fn=lambda s: 99.4, session="PREMARKET", now=T0,
                     orders_allowed=False)
        adapter = Adapter()
        outcomes = er.retry_latched_exits(conn, broker_adapter=adapter,
                                          session="REGULAR", now=T0)
        assert len(adapter.calls) == 1
        assert outcomes[0]["reason"] == "RANGE_REENTRY"

    def test_the_retry_does_nothing_when_orders_are_still_barred(self, conn):
        opened(conn)
        er.run_exits(conn, broker_adapter=Adapter(),
                     features_fn=lambda s: Features(price=99.4),
                     price_fn=lambda s: 99.4, session="PREMARKET", now=T0,
                     orders_allowed=False)
        adapter = Adapter()
        assert er.retry_latched_exits(conn, broker_adapter=adapter,
                                      orders_allowed=False) == []
        assert adapter.calls == []

    def test_an_already_submitted_exit_is_not_retried(self, conn):
        opened(conn)
        er.run_exits(conn, broker_adapter=Adapter(),
                     features_fn=lambda s: Features(price=99.4),
                     price_fn=lambda s: 99.4, session="REGULAR", now=T0)
        adapter = Adapter()
        er.retry_latched_exits(conn, broker_adapter=adapter,
                               session="REGULAR", now=T0)
        assert adapter.calls == []


class TestBuyFillSync:
    def submitted(self, conn, symbol="ABC"):
        return ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                    range_high=99.5, range_low=99.0, now=T0)

    def test_a_fill_promotes_submitted_to_open(self, conn):
        pid = self.submitted(conn)
        result = er.sync_buy_fills(conn, fills_for=lambda row: {
            "filled_quantity": 1, "average_fill_price": 100.85,
            "venue": "AMEX", "order_id": "o1"}, now=T0)
        assert result[0]["status"] == "OPENED"
        row = ps.load(conn, pid)
        assert row["status"] == ps.OPEN and row["entry_price"] == 100.85

    def test_the_same_fill_twice_is_a_no_op(self, conn):
        self.submitted(conn)
        fill = {"filled_quantity": 1, "average_fill_price": 100.0}
        er.sync_buy_fills(conn, fills_for=lambda row: fill, now=T0)
        again = er.sync_buy_fills(conn, fills_for=lambda row: fill, now=T0)
        assert again == [], "the row is no longer SUBMITTED"

    def test_no_answer_leaves_it_unconfirmed(self, conn):
        """Different from "reported as unfilled" -- only the latter is
        safe to abandon."""
        self.submitted(conn)
        result = er.sync_buy_fills(conn, fills_for=lambda row: None, now=T0)
        assert result[0]["status"] == "STILL_UNCONFIRMED"
        assert ps.load_unconfirmed(conn)

    def test_a_terminal_unfilled_order_is_abandoned(self, conn):
        """A row that can never resolve is indistinguishable from one
        still in flight, and the position limit counts both."""
        pid = self.submitted(conn)
        result = er.sync_buy_fills(conn, fills_for=lambda row: {
            "filled_quantity": 0, "terminal": True}, now=T0)
        assert result[0]["status"] == "ABANDONED"
        assert ps.load(conn, pid)["status"] == ps.CLOSED
        assert ps.open_count(conn) == 0

    def test_one_lookup_failure_does_not_cost_the_others(self, conn):
        self.submitted(conn, "GOOD")
        self.submitted(conn, "BAD")

        def lookup(row):
            if row["symbol"] == "BAD":
                raise RuntimeError("broker unavailable")
            return {"filled_quantity": 1, "average_fill_price": 100.0}

        results = er.sync_buy_fills(conn, fills_for=lookup, now=T0)
        by_symbol = {r["symbol"]: r for r in results}
        assert by_symbol["GOOD"]["status"] == "OPENED"
        assert "error" in by_symbol["BAD"]

    def test_fill_sync_is_not_gated_by_exit_ownership(self):
        """Conflating them is what cost S1 its bookkeeping once."""
        source = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        assert "EXIT_MANAGED_ELSEWHERE" not in source
        assert "not exit ownership" in source


class TestSellFillSync:
    def sold(self, conn, quantity=1):
        pid = ps.record_submission(conn, symbol="ABC", variant="S6-R",
                                   range_high=99.5, range_low=99.0, now=T0)
        ps.open_from_fill(conn, pid, quantity=quantity,
                          average_fill_price=100.0, now=T0)
        ps.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)
        return pid

    def test_a_full_sell_closes_the_position(self, conn):
        pid = self.sold(conn)
        result = er.sync_sell_fills(conn, fills_for=lambda row: {
            "filled_quantity": 1}, now=T0)
        assert result[0]["status"] == "CLOSED"
        assert ps.load(conn, pid)["status"] == ps.CLOSED
        assert ps.holdings(conn) == []

    def test_a_partial_sell_keeps_the_remainder_managed(self, conn):
        """Closing on a partial would orphan shares the broker still
        holds: the position vanishes while the account keeps the risk."""
        pid = self.sold(conn, quantity=3)
        result = er.sync_sell_fills(conn, fills_for=lambda row: {
            "filled_quantity": 1}, now=T0)
        assert result[0]["status"] == "PARTIALLY_SOLD"
        assert result[0]["remaining"] == 2
        row = ps.load(conn, pid)
        assert row["status"] != ps.CLOSED and row["quantity"] == 2
        assert ps.holdings(conn) == [("ABC", None, 2)]

    def test_awaiting_a_fill_leaves_the_position_open(self, conn):
        self.sold(conn)
        result = er.sync_sell_fills(conn, fills_for=lambda row: None, now=T0)
        assert result[0]["status"] == "AWAITING_SELL_FILL"

    def test_a_position_without_a_submitted_exit_is_skipped(self, conn):
        pid = ps.record_submission(conn, symbol="ABC", range_high=99.5,
                                   range_low=99.0, now=T0)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          now=T0)
        assert er.sync_sell_fills(conn, fills_for=lambda row: {
            "filled_quantity": 1}, now=T0) == []


class TestRestartRecovery:
    def test_a_submitted_buy_survives_a_restart(self, conn):
        pid = ps.record_submission(conn, symbol="ABC", range_high=99.5,
                                   range_low=99.0, now=T0)
        # A restart reads the store, not memory.
        assert [r["position_id"] for r in ps.load_unconfirmed(conn)] == [pid]
        assert ps.open_count(conn) == 1, "still blocks a duplicate BUY"

    def test_an_exit_pending_position_is_recovered_and_retried(self, conn):
        pid = opened(conn)
        ps.latch_pending_exit(conn, pid, "SESSION_EXIT", now=T0)
        adapter = Adapter()
        outcomes = er.retry_latched_exits(conn, broker_adapter=adapter,
                                          session="REGULAR", now=T0)
        assert len(adapter.calls) == 1
        assert outcomes[0]["reason"] == "SESSION_EXIT"

    def test_a_submitted_exit_is_not_resent_after_a_restart(self, conn):
        pid = opened(conn)
        ps.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)
        adapter = Adapter()
        er.retry_latched_exits(conn, broker_adapter=adapter,
                               session="REGULAR", now=T0)
        er.run_exits(conn, broker_adapter=adapter,
                     features_fn=lambda s: Features(price=99.4),
                     price_fn=lambda s: 99.4, session="REGULAR", now=T0)
        assert adapter.calls == [], "exit_submitted survives the restart"

    def test_exit_management_continues_after_a_restart(self, conn):
        opened(conn)
        adapter = Adapter()
        outcomes = er.run_exits(
            conn, broker_adapter=adapter,
            features_fn=lambda s: Features(price=98.9),
            price_fn=lambda s: 98.9, session="REGULAR", now=T0)
        assert outcomes[0]["reason"] == "HARD_RISK_CAP"


class TestExitsAreNotGatedByEntryRisk:
    def test_no_risk_control_is_imported(self):
        import ast

        banned = {"position_limits", "kill_switch_state", "order_gate",
                  "allocator", "internal_holdings"}
        source = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for seg in name.split("."):
                        assert seg not in banned, name
