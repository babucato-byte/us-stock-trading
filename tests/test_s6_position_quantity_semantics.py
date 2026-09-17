"""`quantity` means shares CURRENTLY HELD. Nothing may re-assert the entry.

VIAV, 2026-09-17. Bought 4, sold 1, broker held 3. The position was
corrected to 3 and the very next buy-fill sync put it back to 4, because
`sync_buy_fills` compared the broker's CUMULATIVE fill (4) against
`quantity` (3) and overwrote it. Seven SELLs were then refused by the
order gate for "internal=4 KIS=3".

The previous fix reduced the quantity correctly and was undone within
ninety seconds, because its tests exercised the SELL side alone. These
tests exercise the INTERACTION -- the case that was missing.
"""

from datetime import datetime, timezone

import pytest

from s6_live import position_store

UTC = timezone.utc
NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "state.db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _submitted(conn, symbol="VIAV"):
    return position_store.record_submission(
        conn, symbol=symbol, variant="S6-P", entry_session="PREMARKET",
        range_high=36.0, range_low=35.0, entry_volume_expansion=2.0,
        client_order_id=f"c-{symbol}")


def _held(conn, pid):
    return int(position_store.load(conn, pid)["quantity"])


def _applied(conn, pid):
    return position_store.load(conn, pid)["entry_filled_quantity"]


def _buy_sync(conn, pid, cumulative, price=35.6):
    """What `exit_runtime.sync_buy_fills` does with a broker fill."""
    return position_store.apply_fill(
        conn, pid, filled_quantity=cumulative, average_fill_price=price,
        venue="NASDAQ", entry_order_id="o-1", now=NOW)


# -- the contract ---------------------------------------------------------

class TestTheContract:

    def test_the_schema_carries_the_buy_side_separately(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)
        assert _held(conn, pid) == 4
        assert _applied(conn, pid) == 4

    def test_opening_sets_both(self, conn):
        pid = _submitted(conn)
        position_store.open_from_fill(conn, pid, quantity=4,
                                      average_fill_price=35.6,
                                      entry_order_id="o-1")
        assert _held(conn, pid) == 4 and _applied(conn, pid) == 4

    def test_a_sell_reduction_never_touches_the_buy_record(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        assert _held(conn, pid) == 3
        assert _applied(conn, pid) == 4, "the BUY still filled 4"


# -- 5. the VIAV reproduction --------------------------------------------

class TestVIAVReproduction:
    """The exact production sequence, which used to end at 4."""

    def test_5_a_partial_sell_survives_the_buy_sync(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)                  # BUY cumulative 4
        assert _held(conn, pid) == 4

        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        assert _held(conn, pid) == 3             # SELL 1 of 4

        # the broker still reports the same cumulative BUY fill, forever
        assert _buy_sync(conn, pid, 4) is False
        assert _held(conn, pid) == 3, "the sync must not resurrect the sold share"

    def test_5b_repeated_syncs_are_idempotent(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        observed = []
        for _ in range(5):
            _buy_sync(conn, pid, 4)
            observed.append(_held(conn, pid))
        assert observed == [3, 3, 3, 3, 3], observed

    def test_5c_the_old_behaviour_is_gone(self, conn):
        """Pinned as arithmetic: cumulative > held no longer overwrites."""
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        held, applied = _held(conn, pid), int(_applied(conn, pid))
        assert applied > held, "cumulative exceeds held -- the old trigger"
        _buy_sync(conn, pid, applied)
        assert _held(conn, pid) == held


# -- 6. a late BUY fill ---------------------------------------------------

class TestLateBuyFill:

    def test_6_only_the_delta_is_applied(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 3)                  # originally confirmed 3
        assert _held(conn, pid) == 3
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        assert _held(conn, pid) == 2             # SELL 1

        assert _buy_sync(conn, pid, 4) is True   # a fourth share fills late
        assert _held(conn, pid) == 3, "2 + delta 1, not 4"
        assert _applied(conn, pid) == 4

    def test_6b_and_the_next_sync_changes_nothing(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 3)
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        _buy_sync(conn, pid, 4)
        for _ in range(3):
            _buy_sync(conn, pid, 4)
        assert _held(conn, pid) == 3

    def test_a_late_fill_still_re_averages_the_price(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 3, price=10.0)
        _buy_sync(conn, pid, 4, price=12.0)
        assert position_store.load(conn, pid)["entry_price"] == 12.0


# -- 7. several sells -----------------------------------------------------

class TestMultipleSells:

    def test_7_no_sold_share_is_ever_resurrected(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)

        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        assert _held(conn, pid) == 3
        _buy_sync(conn, pid, 4)
        assert _held(conn, pid) == 3

        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        assert _held(conn, pid) == 2
        _buy_sync(conn, pid, 4)
        assert _held(conn, pid) == 2


# -- 8. a full exit -------------------------------------------------------

class TestFullExit:

    def test_8_a_closed_position_is_not_resurrected(self, conn):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)
        position_store.close_position(conn, pid, reason="EMA_STRUCTURE_FAILURE",
                                      exit_price=36.0, now=NOW)
        assert position_store.load(conn, pid)["status"] == position_store.CLOSED

        assert _buy_sync(conn, pid, 4) is False, "CLOSED is not a held status"
        assert position_store.load(conn, pid)["status"] == position_store.CLOSED

    def test_8b_the_reducer_still_refuses_to_empty_a_held_row(self, conn):
        """No zero-share held row -- the table's CHECK and the close path
        own that case."""
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)
        assert position_store.reduce_after_partial_exit(
            conn, pid, sold=4, now=NOW) == 4
        assert _held(conn, pid) == 4


# -- restart safety and legacy rows --------------------------------------

class TestDurability:

    def test_the_delta_is_durable_across_connections(self, conn, tmp_path):
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        from state_store.db import open_db

        with open_db() as fresh:
            assert int(position_store.load(fresh, pid)["quantity"]) == 3
            assert int(position_store.load(fresh, pid)["entry_filled_quantity"]) == 4
            assert position_store.apply_fill(
                fresh, pid, filled_quantity=4, average_fill_price=35.6,
                now=NOW) is False
            assert int(position_store.load(fresh, pid)["quantity"]) == 3

    def test_a_pre_migration_row_falls_back_to_quantity(self, conn):
        """A row written before migration 28 has no BUY record; its
        `quantity` IS the cumulative fill, so the first delta is zero."""
        pid = _submitted(conn)
        _buy_sync(conn, pid, 4)
        conn.execute("UPDATE s6_positions SET entry_filled_quantity = NULL "
                     "WHERE position_id = ?", (pid,))
        conn.commit()
        assert _applied(conn, pid) is None
        assert _buy_sync(conn, pid, 4) is False
        assert _held(conn, pid) == 4

    def test_the_migration_backfills_existing_rows(self):
        from state_store.migrations import MIGRATIONS

        entry = next(m for m in MIGRATIONS if m[0] == 28)
        sql = " ".join(entry[2]).upper()
        assert "ADD COLUMN ENTRY_FILLED_QUANTITY" in sql
        assert "UPDATE S6_POSITIONS SET ENTRY_FILLED_QUANTITY = QUANTITY" in sql


# -- 9, 10. what must not have moved --------------------------------------

class TestNothingElseChanged:

    def test_9_the_oversell_precheck_is_untouched(self):
        from s6_live import exit_runtime

        assert exit_runtime._sell_still_valid(
            {"status": "OPEN", "quantity": 0}).startswith("no quantity")

    def test_9b_no_broker_gate_or_reconciliation_file_changed(self):
        import subprocess

        changed = subprocess.run(
            ["git", "diff", "--name-only",
             "e20eef596cb396a10f83a493d0543b738e384e1c", "HEAD"],
            capture_output=True, text=True).stdout.split()
        for path in ("execution/execution_engine.py", "brokers/kis_broker.py",
                     "reconciliation/snapshot.py", "brokers/kis_rate_limiter.py"):
            assert path not in changed, path

    def test_10_the_momentum_adapter_fix_is_preserved(self):
        import inspect

        from scripts import run_s6_runtime

        body = inspect.getsource(run_s6_runtime._observe_momentum)
        assert "for position_id, row in rows" in body, (
            "the tuple unpack that fixed the live TypeError must stay")
        assert 'row["position_id"]' not in body

    def test_the_sell_reduction_path_is_unchanged(self):
        import inspect

        from s6_live import exit_timeout

        assert "reduce_after_partial_exit" in inspect.getsource(exit_timeout)
