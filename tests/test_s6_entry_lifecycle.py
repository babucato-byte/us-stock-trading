"""The S6 entry that was never wired, end to end.

What was missing
----------------
Every stage of S6's live position lifecycle existed and was tested except
the one that starts it. `record_submission` and `open_from_fill` had no
production caller, and `s6_positions` had never held a row -- so the exit
runtime, the every-minute monitor, the fill sync and the exit policy were
all reading a store nothing wrote.

Two consequences, and the second is the one that is easy to miss:

  * a held S6 position had no exit servicing at all;
  * `strategy_registry.POSITION_TABLES` maps S6 to `s6_positions` and
    `entry_limits._held_symbols_by_slot` reads it to decide WHOSE a
    position is. A position the store does not know about is
    `unattributed`, and unattributed symbols count against EVERY slot --
    so one unrecorded S6 position would have blocked new entries
    account-wide.

These tests pin the wiring, the idempotency the store already had, and
the session behaviour the exit half depends on.
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import session_capability as sc  # noqa: E402
from market_hours import EASTERN  # noqa: E402
from s6_live import entry_lifecycle as el  # noqa: E402
from s6_live import exit_runtime as er  # noqa: E402
from s6_live import position_store as ps  # noqa: E402

S6 = "S6_ORB_BREAKOUT_V1"
S1 = "S1_HMA_EARLY_TREND_V1"

# Inside OVERNIGHT_DAYTIME, ahead of a Wednesday session.
DAYTIME = datetime(2026, 8, 25, 23, 30, tzinfo=EASTERN)
REGULAR = datetime(2026, 8, 26, 12, 0, tzinfo=EASTERN)

CANDIDATE = {
    "symbol": "SLF", "variant": "S6-O", "session": "OVERNIGHT_DAYTIME",
    "range_high": 79.535, "range_low": 79.01, "range_minutes": 15,
    "vwap": 79.287, "ema9": 79.923, "ema21": 79.879,
    "volume_expansion": 14.89,
}


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _record(conn, *, symbol="SLF", session="OVERNIGHT_DAYTIME",
            client_order_id="kislive-SLF-abc123", row=CANDIDATE, now=DAYTIME):
    return el.record_entry_submission(
        conn, symbol=symbol, session=session, client_order_id=client_order_id,
        candidate_row=row, now=now)


class TestTheEntryReachesTheCanonicalStore:
    def test_a_sent_buy_becomes_a_submitted_row(self, conn):
        pid = _record(conn)
        row = ps.load(conn, pid)
        assert row["status"] == ps.SUBMITTED
        assert row["symbol"] == "SLF"
        assert row["strategy_id"] == S6

    def test_a_submitted_row_holds_no_shares_yet(self, conn):
        """It records that a BUY was SENT, not that it filled. Treating a
        submission as a position is how a rejected order becomes a
        phantom holding."""
        row = ps.load(conn, _record(conn))
        assert not row["quantity"]
        assert not row["entry_price"]

    def test_the_orb_measurements_come_from_the_candidate(self, conn):
        """The position carries the range it actually broke out of,
        rather than one re-derived later against different bars."""
        row = ps.load(conn, _record(conn))
        assert row["range_high"] == pytest.approx(79.535)
        assert row["range_low"] == pytest.approx(79.01)
        assert row["range_minutes"] == 15
        assert row["entry_vwap"] == pytest.approx(79.287)
        assert row["entry_volume_expansion"] == pytest.approx(14.89)

    def test_a_missing_candidate_leaves_the_range_unmeasured(self, conn):
        """NULL, not a default. `exit_policy` can say "not measured"; a
        fabricated range would silently move the structural stop."""
        row = ps.load(conn, _record(conn, row=None))
        assert row["range_high"] is None
        assert row["range_low"] is None
        assert row["status"] == ps.SUBMITTED

    def test_the_session_and_variant_are_recorded(self, conn):
        row = ps.load(conn, _record(conn))
        assert row["entry_session"] == "OVERNIGHT_DAYTIME"
        assert row["variant"] == "S6-O"

    def test_a_daytime_buy_produces_a_daytime_position(self, conn):
        """The variant follows the session, so a daytime entry is not
        later measured against the regular session's opening range.

        Two different symbols on purpose: the store permits only one
        non-closed position per symbol, which the next test pins."""
        day = ps.load(conn, _record(conn, symbol="SLF",
                                    session="OVERNIGHT_DAYTIME"))
        reg = ps.load(conn, _record(conn, symbol="AAPL", session="REGULAR",
                                    client_order_id="kislive-AAPL-def456"))
        assert day["variant"] == "S6-O"
        assert reg["variant"] == "S6-R"

    def test_one_live_position_per_symbol(self, conn):
        """A partial unique index (`WHERE status != 'CLOSED'`) is the
        last line of defence against a duplicate entry: two SUBMITTED
        rows for one symbol would each hold the slot and each expect a
        fill, and reconciliation could not say which held the shares."""
        import sqlite3

        _record(conn, symbol="SLF")
        with pytest.raises(sqlite3.IntegrityError):
            _record(conn, symbol="SLF", client_order_id="kislive-SLF-second")

    def test_a_closed_position_does_not_block_re_entry(self, conn):
        """The index is deliberately partial. A symbol traded and exited
        must be tradable again, or one entry per symbol per lifetime."""
        pid = _record(conn, symbol="SLF")
        er.sync_buy_fills(conn, fills_for=self_fills(), now=DAYTIME)
        ps.close_position(conn, pid, reason="STOP", exit_price=81.0,
                          now=DAYTIME)
        again = _record(conn, symbol="SLF",
                        client_order_id="kislive-SLF-second")
        assert ps.load(conn, again)["status"] == ps.SUBMITTED


class TestOnlyS6GoesToThisStore:
    def test_is_s6_accepts_every_spelling(self):
        for alias in ("orb", "S6_ORB_BREAKOUT_V1", "S6"):
            assert el.is_s6(alias) is True

    def test_other_strategies_are_not_s6(self):
        for alias in (S1, "hma_early_trend", "accumulation", None, "", "who"):
            assert el.is_s6(alias) is False

    def test_s6_is_not_also_written_to_the_general_lifecycle(self):
        """One position with two exit engines is worse than either: the
        `positions` lifecycle runs its own stop/target/time/EOD exits
        while S6 has its own policy. `positions` is also absent from
        POSITION_TABLES, so a row there answers nobody's question about
        attribution."""
        import inspect

        import kis_live_trading

        source = inspect.getsource(kis_live_trading.run_live_buy_entry_cycle)
        assert "s6_entry_lifecycle.is_s6(" in source
        assert "record_entry_submission" in source
        # The general call must be on the ELSE branch, not unconditional.
        assert "else:" in source
        assert "create_kis_position_after_buy" in source

    def test_the_bootstrap_uses_the_same_lifecycle(self):
        """The bootstrap is a SMALLER first order, not a SEPARATE
        lifecycle. Writing to `positions` sent it to a store S6's exit
        runtime does not read, serviced by a cron gated to 09..15 ET."""
        import inspect

        from live_pilot import bootstrap

        source = inspect.getsource(bootstrap.run_bootstrap_buy)
        assert "entry_lifecycle" in source
        assert "record_entry_submission" in source


class TestTheStoreSettlesFillsIdempotently:
    def _fills(self, **overrides):
        fill = {"filled_quantity": 1, "average_fill_price": 79.60,
                "venue": "NASD", "order_id": "KIS-1"}
        fill.update(overrides)
        return lambda _row: fill

    def test_a_fill_opens_the_position(self, conn):
        pid = _record(conn)
        er.sync_buy_fills(conn, fills_for=self._fills(), now=DAYTIME)
        row = ps.load(conn, pid)
        assert row["status"] == ps.OPEN
        assert row["quantity"] == 1
        assert row["entry_price"] == pytest.approx(79.60)

    def test_the_same_fill_seen_twice_is_a_no_op(self, conn):
        """Cumulative quantity is what is applied, so re-reading a
        completed fill cannot double the position."""
        pid = _record(conn)
        for _ in range(3):
            er.sync_buy_fills(conn, fills_for=self._fills(), now=DAYTIME)
        row = ps.load(conn, pid)
        assert row["quantity"] == 1
        assert row["status"] == ps.OPEN

    def test_a_partial_fill_opens_and_then_completes(self, conn):
        """A BUY that fills in two parts reaches OPEN on the first fill
        and must keep synchronising afterwards -- scanning only SUBMITTED
        rows would leave the position permanently short of what the
        account holds."""
        pid = _record(conn)
        er.sync_buy_fills(conn, fills_for=self._fills(filled_quantity=1),
                          now=DAYTIME)
        assert ps.load(conn, pid)["quantity"] == 1
        er.sync_buy_fills(conn, fills_for=self._fills(filled_quantity=3),
                          now=DAYTIME)
        row = ps.load(conn, pid)
        assert row["quantity"] == 3
        assert row["status"] == ps.OPEN

    def test_a_stale_smaller_fill_never_shrinks_the_position(self, conn):
        pid = _record(conn)
        er.sync_buy_fills(conn, fills_for=self._fills(filled_quantity=3),
                          now=DAYTIME)
        er.sync_buy_fills(conn, fills_for=self._fills(filled_quantity=1),
                          now=DAYTIME)
        assert ps.load(conn, pid)["quantity"] == 3

    def test_an_unusable_fill_price_is_refused_not_stored(self, conn):
        """Every later decision -- the structural stop above all -- is
        measured from the entry, and a position whose entry is wrong is
        worse than no position because it looks correct."""
        pid = _record(conn)
        with pytest.raises(ps.S6PositionError):
            ps.open_from_fill(conn, pid, quantity=1, average_fill_price=0)
        assert ps.load(conn, pid)["status"] == ps.SUBMITTED


class TestAmbiguityAndRestart:
    def test_an_unanswered_lookup_leaves_the_row_submitted(self, conn):
        """"No answer yet" is not "never filled". The row stays, so the
        share cannot be held at KIS with nothing internal aware of it."""
        pid = _record(conn)
        out = er.sync_buy_fills(conn, fills_for=lambda _r: None, now=DAYTIME)
        assert ps.load(conn, pid)["status"] == ps.SUBMITTED
        assert out and out[0]["status"] == "STILL_UNCONFIRMED"

    def test_only_a_positively_unfilled_order_is_abandoned(self, conn):
        pid = _record(conn)
        er.sync_buy_fills(
            conn, fills_for=lambda _r: {"filled_quantity": 0, "terminal": True},
            now=DAYTIME)
        assert ps.load(conn, pid)["status"] != ps.SUBMITTED

    def test_a_restart_settles_the_row_from_broker_evidence(self, conn):
        """The row survives the process that wrote it, which is the whole
        point of writing it before the answer is known."""
        pid = _record(conn)
        assert [r["position_id"] for r in ps.load_unconfirmed(conn)] == [pid]
        er.sync_buy_fills(conn, fills_for=self_fills(), now=DAYTIME)
        assert ps.load(conn, pid)["status"] == ps.OPEN

    def test_the_ambiguous_path_records_without_resending(self):
        """An ambiguous BUY is never re-sent. The row is what lets
        reconciliation settle it instead."""
        import inspect

        import kis_live_trading

        source = inspect.getsource(kis_live_trading.run_live_buy_entry_cycle)
        ambiguous = source.split("except KISAmbiguousResponseError")[1]
        assert "record_entry_submission" in ambiguous
        assert "submit_buy_order" not in ambiguous


def self_fills():
    return lambda _row: {"filled_quantity": 1, "average_fill_price": 79.60,
                         "venue": "NASD", "order_id": "KIS-1"}


class TestTheExitHalfFollowsTheSession:
    def test_daytime_supports_both_sides(self):
        cap = sc.capability_for("OVERNIGHT_DAYTIME", now=DAYTIME)
        assert cap.entry_supported is True
        assert cap.exit_supported is True

    def test_the_daytime_sell_route_is_the_daytime_tr(self):
        cap = sc.capability_for("OVERNIGHT_DAYTIME", now=DAYTIME)
        assert cap.order_route_sell[1] == "TTTS6037U"
        assert cap.order_route_sell[0].endswith("/daytime-order")

    def test_the_daytime_cancel_route_is_the_daytime_tr(self):
        from brokers import kis_broker as kb

        assert kb.cancel_route_for("OVERNIGHT_DAYTIME", "live")[1] == "TTTS6038U"

    def test_an_unsupported_session_latches_instead_of_selling(self, conn):
        """Latched, never dropped. A session that cannot place orders is
        a reason to wait, not a reason to forget the position should be
        leaving."""
        pid = _record(conn)
        er.sync_buy_fills(conn, fills_for=self_fills(), now=DAYTIME)

        class _Adapter:
            def __init__(self):
                self.calls = []

            def submit_order(self, *a, **k):
                self.calls.append((a, k))
                return type("R", (), {"status_code": 200, "text": "ok"})()

        adapter = _Adapter()
        er.run_exits(conn, broker_adapter=adapter,
                     features_fn=lambda _s: None, price_fn=lambda _s: 60.0,
                     session="AFTER_HOURS", now=DAYTIME, orders_allowed=False)
        assert adapter.calls == []
        assert ps.load(conn, pid)["status"] in (ps.EXIT_PENDING, ps.OPEN)

    def test_a_latched_exit_is_retried_when_the_session_can_order(self, conn):
        """CODEX §7: a SELL that could not be sent is retried in the next
        execution window rather than waiting for the exit condition to
        re-trigger -- the condition already fired."""
        pid = _record(conn)
        er.sync_buy_fills(conn, fills_for=self_fills(), now=DAYTIME)
        ps.latch_pending_exit(conn, pid, "STOP", now=DAYTIME)
        assert ps.load(conn, pid)["status"] == ps.EXIT_PENDING

        class _Adapter:
            def __init__(self):
                self.calls = []

            def submit_order(self, symbol, quantity, *, side, client_order_id=None):
                self.calls.append(side)
                return type("R", (), {"status_code": 200, "text": "ok"})()

        blocked = _Adapter()
        assert er.retry_latched_exits(conn, broker_adapter=blocked,
                                      session="AFTER_HOURS", now=DAYTIME,
                                      orders_allowed=False) == []
        assert blocked.calls == []

        allowed = _Adapter()
        er.retry_latched_exits(conn, broker_adapter=allowed,
                               session="OVERNIGHT_DAYTIME", now=DAYTIME,
                               orders_allowed=True)
        assert allowed.calls == ["sell"]


class TestTheSellCompletesTheLifecycle:
    def _open(self, conn):
        pid = _record(conn)
        er.sync_buy_fills(conn, fills_for=self_fills(), now=DAYTIME)
        return pid

    def test_a_sell_fill_closes_the_position(self, conn):
        pid = self._open(conn)
        ps.mark_exit_submitted(conn, pid, "STOP", now=DAYTIME)
        er.sync_sell_fills(
            conn,
            fills_for=lambda _r: {"filled_quantity": 1,
                                  "average_fill_price": 81.00},
            now=DAYTIME + timedelta(minutes=5))
        row = ps.load(conn, pid)
        assert row["status"] == ps.CLOSED

    def test_the_close_records_an_exit_price(self, conn):
        """Realized PnL is entry vs exit, so a close with no exit price
        is a closed position nobody can score."""
        pid = self._open(conn)
        ps.close_position(conn, pid, reason="STOP", exit_price=81.00,
                          now=DAYTIME)
        row = ps.load(conn, pid)
        assert row["status"] == ps.CLOSED
        assert row["exit_price"] == pytest.approx(81.00)
        assert row["entry_price"] == pytest.approx(79.60)
        # The realized move the ledger is scored on.
        assert row["exit_price"] - row["entry_price"] == pytest.approx(1.40)


class TestTheCapsStayHonest:
    def test_s6_is_attributable_once_it_is_recorded(self, conn):
        """The reason this matters beyond exits: an unattributed symbol
        counts against EVERY slot, so one unrecorded S6 position would
        have blocked new entries account-wide."""
        from config import strategy_registry

        assert strategy_registry.POSITION_TABLES[strategy_registry.SLOT_S6] \
            == "s6_positions"
        _record(conn)
        # A SUBMITTED row is not yet a HOLDING: the cap asks "could
        # another position appear" and reconciliation asks "what do we
        # hold", and an unfilled order is yes to the first, no to the
        # second.
        assert ps.holdings(conn) == []
        assert ps.open_count(conn) == 1

        er.sync_buy_fills(conn, fills_for=self_fills(), now=DAYTIME)
        assert [(sym, qty) for sym, _venue, qty in ps.holdings(conn)] \
            == [("SLF", 1)]

    def test_s1_holding_a_position_does_not_consume_s6s_slot(self):
        """Per-strategy caps exist precisely so one strategy holding a
        position does not consume another's capacity."""
        from config import position_limits

        assert position_limits.PROPOSED_STRATEGY_MAX["S1_HMA_EARLY_TREND_V1"] == 1
        assert position_limits.PROPOSED_STRATEGY_MAX["S6_ORB_BREAKOUT_V1"] == 1
        assert position_limits.PROPOSED_GLOBAL_MAX >= 2
        decision = position_limits.check_entry(S6, {S1: 1})
        assert decision.allowed is True

    def test_the_readiness_check_blocks_only_on_the_trading_slot(self):
        import pathlib

        source = pathlib.Path("scripts/final_pre_live_check.sh").read_text()
        assert "trading_slot = strategy_registry.slot_for" in source
        assert "INFO::STRATEGY_SLOT_" in source


class TestS1KeepsItsExitAndLosesOnlyItsEntry:
    def test_s1_cannot_open_but_can_close(self):
        from config import strategy_entry_policy as sep

        assert sep.entry_enabled(S1) is False
        assert sep.exit_enabled(S1) is True

    def test_s6_can_do_both(self):
        from config import strategy_entry_policy as sep

        assert sep.entry_enabled(S6) is True
        assert sep.exit_enabled(S6) is True

    def test_the_sell_gate_never_consults_entry_permission(self):
        """TX keeps its exit. `evaluate_sell_gate` reads only
        `live_order_enabled` -- not the entry policy, not the live-mode
        table, not the rollout flag."""
        import inspect

        from execution import order_gate

        source = inspect.getsource(order_gate.evaluate_sell_gate)
        assert "entry_disabled" not in source
        assert "strategy_entry_policy" not in source
        assert "live_order_enabled" in source
