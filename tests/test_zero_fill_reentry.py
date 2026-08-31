"""A BUY that never filled is not a completed trade.

What happened
-------------
On 2026-08-31 an S6 BUY for RBLX (broker order 0030152653) was ACCEPTED,
filled nothing, hit its TTL and was cancelled. The position row closed
with quantity NULL, entry_price NULL and exit_reason
BUY_FILL_TTL_EXPIRED -- and same-day re-entry then refused RBLX for the
rest of the day, for a trade that never happened.

Why the reason list was not the fix
-----------------------------------
BUY_FILL_TTL_EXPIRED is also raised for a PARTIALLY filled order that
`entry_timeout` cancels. Those shares are real. Exempting the reason
would have let a symbol the strategy genuinely holds be bought again the
same day -- loosening the protection this block exists to provide.

So the discriminator is whether shares ever changed hands, not what the
closure was called.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution import reentry_policy as rp  # noqa: E402

S6 = "S6_ORB_BREAKOUT_V1"
DAY = "2026-08-31"
CLOSED_AT = "2026-08-31T14:15:08.653024+00:00"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _closed_row(conn, *, symbol, quantity, reason, entry_price=None):
    """A CLOSED row built the way production builds one.

    `quantity=None` reproduces the zero-fill case exactly: a submission
    that was recorded and then closed without ever opening, which is
    what an accepted-but-unfilled BUY leaves behind.
    """
    from s6_live import position_store as ps

    now = datetime(2026, 8, 31, 14, 15, 8, tzinfo=timezone.utc)
    pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                               entry_session="REGULAR",
                               client_order_id=f"kislive-{symbol}-1", now=now)
    if quantity:
        ps.open_from_fill(conn, pid, quantity=quantity,
                          average_fill_price=entry_price or 10.0,
                          venue="NYSE", now=now)
    ps.close_position(conn, pid, reason=reason, exit_price=entry_price,
                      now=now)
    return pid


class TestTheRBLXCase:
    def test_a_zero_fill_cancel_does_not_block_reentry(self, conn):
        """The exact row RBLX left behind."""
        _closed_row(conn, symbol="RBLX", quantity=None,
                    reason="BUY_FILL_TTL_EXPIRED")
        assert "RBLX" not in rp.blocked_symbols(
            conn, strategy_id=S6, trading_day=DAY)

    def test_it_is_not_counted_as_an_exit_either(self, conn):
        _closed_row(conn, symbol="RBLX", quantity=None,
                    reason="BUY_FILL_TTL_EXPIRED")
        assert rp.exits_today(conn, strategy_id=S6, trading_day=DAY) == {}

    def test_a_zero_quantity_is_treated_the_same_as_null(self, conn):
        _closed_row(conn, symbol="RBLX", quantity=0,
                    reason="BUY_FILL_TTL_EXPIRED")
        assert "RBLX" not in rp.blocked_symbols(
            conn, strategy_id=S6, trading_day=DAY)


class TestRealTradesAreStillBlocked:
    """The protection must not be loosened by any of this."""

    def test_a_filled_position_that_exited_still_blocks(self, conn):
        _closed_row(conn, symbol="OWL", quantity=1, entry_price=12.13,
                    reason="RANGE_REENTRY")
        assert "OWL" in rp.blocked_symbols(
            conn, strategy_id=S6, trading_day=DAY)

    def test_a_PARTIALLY_filled_ttl_cancel_still_blocks(self, conn):
        """The case that makes exempting the reason wrong: those shares
        were real, so the symbol must not be re-entered today."""
        _closed_row(conn, symbol="PART", quantity=2, entry_price=5.0,
                    reason="BUY_FILL_TTL_EXPIRED")
        assert "PART" in rp.blocked_symbols(
            conn, strategy_id=S6, trading_day=DAY)

    def test_a_session_exit_still_blocks(self, conn):
        _closed_row(conn, symbol="SBS", quantity=4, entry_price=4.84,
                    reason="SESSION_EXIT")
        assert "SBS" in rp.blocked_symbols(
            conn, strategy_id=S6, trading_day=DAY)

    def test_the_existing_non_trade_reasons_still_apply(self, conn):
        """Unchanged: an ownership repair was never this strategy's
        trade even though shares existed."""
        _closed_row(conn, symbol="DT", quantity=1, entry_price=50.79,
                    reason="RELEASED_WRONGLY_ATTRIBUTED")
        assert "DT" not in rp.blocked_symbols(
            conn, strategy_id=S6, trading_day=DAY)


class TestAmbiguityStillBlocks:
    """The block exists to refuse. A value that cannot be read must not
    become permission."""

    def test_an_unreadable_quantity_counts_as_held(self):
        assert rp._held_shares({"quantity": "not-a-number"}) is True

    def test_a_missing_column_counts_as_held(self):
        class _Row:
            def keys(self):
                raise KeyError("quantity")

            def __getitem__(self, key):
                raise KeyError(key)

        assert rp._held_shares(_Row()) is True

    def test_null_and_zero_are_the_only_not_held_answers(self):
        assert rp._held_shares({"quantity": None}) is False
        assert rp._held_shares({"quantity": 0}) is False
        assert rp._held_shares({"quantity": 1}) is True
