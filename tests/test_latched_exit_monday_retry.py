"""A weekend-latched exit sells on Monday, for the reason it latched on.

RIG's situation, exactly
------------------------
On Friday 2026-08-28 at 20:07 UTC, S6's exit runtime decided RIG had
failed its EMA structure. The market was closed to orders, so the
position was latched EXIT_PENDING with that reason stored and no order
sent. It sat over the weekend holding 3 shares.

What has to happen on Monday is that the STORED reason drives the sell.
The alternative -- re-running the exit rules against Monday's opening
data -- sounds more current and is wrong: the decision to leave was
already made on the evidence that existed when it was made, and Monday's
first prints are the noisiest of the session. A position that decided to
exit does not get to re-argue it because time passed.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import exit_runtime, position_store  # noqa: E402

FRIDAY = datetime(2026, 8, 28, 20, 7, 5, tzinfo=timezone.utc)
MONDAY = datetime(2026, 8, 31, 13, 35, tzinfo=timezone.utc)
REASON = "EMA_STRUCTURE_FAILURE"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _latched_rig(conn):
    """RIG as it actually stands: 3 shares, EXIT_PENDING, nothing sent."""
    pid = position_store.record_submission(
        conn, symbol="RIG", variant="S6-R", entry_session="REGULAR",
        client_order_id="kislive-RIG-1", now=FRIDAY - timedelta(hours=4))
    position_store.open_from_fill(conn, pid, quantity=3,
                                  average_fill_price=5.85, venue="NYSE",
                                  now=FRIDAY - timedelta(hours=4))
    position_store.latch_pending_exit(conn, pid, REASON, now=FRIDAY)
    return pid


class _Response:
    """The shape the shared sell path reads: an HTTP-ish status code."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.body = {"output": {"ODNO": "0030999999"}}

    def json(self):
        return self.body


class _Adapter:
    """Records what it was asked to sell. Places nothing.

    The contract is `submit_order(symbol, quantity, side=, client_order_id=)`
    -- the shared exit path S6 routes through, not an S6-specific method.
    """

    def __init__(self, status_code=200):
        self.calls = []
        self.status_code = status_code

    def submit_order(self, symbol, quantity, *, side, client_order_id):
        self.calls.append({"symbol": symbol, "quantity": quantity,
                           "side": side, "client_order_id": client_order_id})
        return _Response(self.status_code)


class TestTheWeekendHold:
    def test_it_stays_exit_pending_with_its_reason(self, conn):
        pid = _latched_rig(conn)
        row = position_store.load(conn, pid)
        assert row["status"] == position_store.EXIT_PENDING
        assert row["pending_exit_reason"] == REASON
        assert not row["exit_submitted"]

    def test_nothing_is_submitted_while_orders_are_not_allowed(self, conn):
        _latched_rig(conn)
        adapter = _Adapter()
        outcomes = exit_runtime.retry_latched_exits(
            conn, broker_adapter=adapter, now=FRIDAY + timedelta(hours=2),
            orders_allowed=False)
        assert outcomes == []
        assert adapter.calls == []

    def test_the_quantity_is_untouched_over_the_weekend(self, conn):
        pid = _latched_rig(conn)
        exit_runtime.retry_latched_exits(
            conn, broker_adapter=_Adapter(), now=FRIDAY + timedelta(days=1),
            orders_allowed=False)
        assert position_store.load(conn, pid)["quantity"] == 3


class TestMondayRetry:
    def test_the_first_sell_capable_moment_submits(self, conn):
        _latched_rig(conn)
        adapter = _Adapter()
        outcomes = exit_runtime.retry_latched_exits(
            conn, broker_adapter=adapter, now=MONDAY, orders_allowed=True)
        assert len(adapter.calls) == 1
        assert outcomes

    def test_it_sells_the_whole_position(self, conn):
        _latched_rig(conn)
        adapter = _Adapter()
        exit_runtime.retry_latched_exits(conn, broker_adapter=adapter,
                                         now=MONDAY, orders_allowed=True)
        assert adapter.calls[0]["quantity"] == 3

    def test_it_sells_the_right_symbol(self, conn):
        _latched_rig(conn)
        adapter = _Adapter()
        exit_runtime.retry_latched_exits(conn, broker_adapter=adapter,
                                         now=MONDAY, orders_allowed=True)
        assert adapter.calls[0]["symbol"] == "RIG"
        assert adapter.calls[0]["side"] == "sell"


class TestTheStoredReasonSurvives:
    """Re-running the exit rules against Monday's opening data sounds
    more current and is wrong: the decision was made on the evidence
    that existed, and Monday's first prints are the noisiest of the
    session."""

    def test_the_retry_does_not_re_evaluate_exit_conditions(self):
        import inspect

        source = inspect.getsource(exit_runtime.retry_latched_exits)
        assert 'row.get("pending_exit_reason")' in source
        for forbidden in ("evaluate_exit", "should_exit", "exit_policy.",
                          "build_features"):
            assert forbidden not in source, forbidden

    def test_the_stored_reason_is_what_gets_submitted(self, conn):
        pid = _latched_rig(conn)
        adapter = _Adapter()
        exit_runtime.retry_latched_exits(conn, broker_adapter=adapter,
                                         now=MONDAY, orders_allowed=True)
        row = position_store.load(conn, pid)
        assert row["exit_reason"] == REASON or \
            row["pending_exit_reason"] == REASON

    def test_a_latched_row_without_a_reason_falls_back_to_session_exit(
            self, conn):
        """It still sells. A missing reason is a bookkeeping gap, not a
        licence to keep holding."""
        import inspect

        source = inspect.getsource(exit_runtime.retry_latched_exits)
        assert '"SESSION_EXIT"' in source


class TestItDoesNotSellTwice:
    def test_a_row_already_submitted_is_skipped(self, conn):
        pid = _latched_rig(conn)
        position_store.mark_exit_submitted(conn, pid, REASON, now=MONDAY)
        adapter = _Adapter()
        exit_runtime.retry_latched_exits(conn, broker_adapter=adapter,
                                         now=MONDAY, orders_allowed=True)
        assert adapter.calls == []

    def test_a_second_retry_in_the_same_window_does_not_resend(self, conn):
        _latched_rig(conn)
        adapter = _Adapter()
        exit_runtime.retry_latched_exits(conn, broker_adapter=adapter,
                                         now=MONDAY, orders_allowed=True)
        exit_runtime.retry_latched_exits(conn, broker_adapter=adapter,
                                         now=MONDAY + timedelta(minutes=1),
                                         orders_allowed=True)
        assert len(adapter.calls) == 1

    def test_an_unrelated_open_position_is_not_sold(self, conn):
        """S1 holds TX. Nothing here may reach it -- and S6's own OPEN
        rows are not EXIT_PENDING either."""
        _latched_rig(conn)
        other = position_store.record_submission(
            conn, symbol="SBS", variant="S6-R", entry_session="REGULAR",
            client_order_id="kislive-SBS-1", now=FRIDAY)
        position_store.open_from_fill(conn, other, quantity=4,
                                      average_fill_price=4.85, venue="NYSE",
                                      now=FRIDAY)
        adapter = _Adapter()
        exit_runtime.retry_latched_exits(conn, broker_adapter=adapter,
                                         now=MONDAY, orders_allowed=True)
        assert [c["symbol"] for c in adapter.calls] == ["RIG"]
