"""One S6 position, carried across sessions. §19's properties.

The shape being protected
-------------------------
S6 scans in four sessions but the FAMILY holds at most one position. A
position opened in PREMARKET is the same position in REGULAR: same id,
same entry, same range, same stop. The session changes; nothing the
position was opened on does.

The two halves pull opposite ways, which is why they are tested
together:

    new BUY      refused while ANY S6 position is live, in EVERY variant
    existing     managed in EVERY session, including ones that cannot
                 place a BUY at all

A guard that only did the first would let an account acquire a position
in a session it cannot leave.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_sessions  # noqa: E402
from config import scanner_live_mode as slm  # noqa: E402
from s6_live import (exit_policy, exit_runtime, position_store,  # noqa: E402
                     session_capability, variant_state)

T0 = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def opened(conn, *, session="PREMARKET", variant="S6-P", symbol="AAPL",
           price=100.0, low=99.0, high=99.5):
    pid = position_store.record_submission(
        conn, symbol=symbol, variant=variant, entry_session=session,
        range_high=high, range_low=low, range_minutes=15,
        entry_volume_expansion=2.0, client_order_id=f"s6buy-{symbol}-1",
        now=T0)
    position_store.open_from_fill(conn, pid, quantity=1,
                                  average_fill_price=price, now=T0)
    return pid


# ====================================================================
# Cross-session continuity
# ====================================================================
class TestThePositionSurvivesTheSessionChange:
    def test_entry_variant_and_session_are_never_rewritten(self, conn):
        """The record of WHERE it came from is permanent."""
        pid = opened(conn, session="PREMARKET", variant="S6-P")
        before = position_store.load(conn, pid)

        # Everything a later session does to a held position.
        position_store.observe(conn, pid, price=101.0, now=T0)
        position_store.observe(conn, pid, price=98.0, now=T0)
        position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)

        after = position_store.load(conn, pid)
        assert after["variant"] == before["variant"] == "S6-P"
        assert after["entry_session"] == before["entry_session"] == "PREMARKET"
        assert after["position_id"] == pid

    def test_no_write_path_touches_the_entry_identity(self):
        """Structural: only `record_submission` writes variant or
        entry_session, so a session change cannot rewrite them."""
        import ast

        tree = ast.parse((REPO_ROOT / "s6_live" / "position_store.py")
                         .read_text(encoding="utf-8"))
        writers = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.get_source_segment(
                (REPO_ROOT / "s6_live" / "position_store.py")
                .read_text(encoding="utf-8"), node) or ""
            if "UPDATE" in body and ("variant" in body or "entry_session" in body):
                writers.append(node.name)
        assert writers == [], f"these UPDATE variant/entry_session: {writers}"

    def test_the_original_range_survives_into_the_next_session(self, conn):
        """§6: a new session's ORB must not move an open position's stop."""
        pid = opened(conn, session="PREMARKET", variant="S6-P",
                     low=99.0, high=99.5)
        for price in (101.0, 100.0, 102.0):
            position_store.observe(conn, pid, price=price, now=T0)
        row = position_store.load(conn, pid)
        assert row["range_low"] == 99.0
        assert row["range_high"] == 99.5

        # The stop the policy applies in REGULAR is still PREMARKET's.
        state = position_store.to_state(row)
        detail = exit_policy.decide(state, current_price=100.5,
                                    session="REGULAR", now=T0).detail
        assert detail["range_low"] == 99.0
        assert detail["range_high"] == 99.5

    def test_the_stop_fires_on_the_original_low_not_a_new_one(self, conn):
        pid = opened(conn, session="PREMARKET", variant="S6-P", low=99.0)
        row = position_store.load(conn, pid)
        decision = exit_policy.decide(position_store.to_state(row),
                                      current_price=98.0, session="REGULAR",
                                      now=T0)
        assert decision.sells
        assert decision.reason == exit_policy.REASON_HARD_RISK_CAP
        assert decision.detail["range_low"] == 99.0

    def test_peak_and_trough_carry_across_sessions(self, conn):
        pid = opened(conn, session="PREMARKET", variant="S6-P", price=100.0)
        position_store.observe(conn, pid, price=104.0, now=T0)          # premarket
        position_store.observe(conn, pid, price=96.0,
                               now=T0 + timedelta(hours=3))             # regular
        row = position_store.load(conn, pid)
        assert row["peak_price"] == 104.0
        assert row["trough_price"] == 96.0


# ====================================================================
# Family max = 1, across every variant
# ====================================================================
class TestTheFamilyHoldsOnePosition:
    @pytest.mark.parametrize("status", ["SUBMITTED", "OPEN", "EXIT_PENDING",
                                        "EXIT_SUBMITTED"])
    def test_every_live_status_counts_against_the_limit(self, conn, status):
        pid = position_store.record_submission(
            conn, symbol="AAPL", variant="S6-P", entry_session="PREMARKET",
            range_high=99.5, range_low=99.0, now=T0)
        if status != "SUBMITTED":
            position_store.open_from_fill(conn, pid, quantity=1,
                                          average_fill_price=100.0, now=T0)
        if status in ("EXIT_PENDING", "EXIT_SUBMITTED"):
            position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY",
                                              now=T0)
        if status == "EXIT_SUBMITTED":
            position_store.mark_exit_submitted(conn, pid, "RANGE_REENTRY",
                                               now=T0)
        assert position_store.load(conn, pid)["status"] == status
        assert position_store.open_count(conn) == 1

    def test_the_count_is_family_wide_not_per_variant(self, conn):
        """An S6-P position must block an S6-R buy. The store counts the
        FAMILY, so the limit cannot be evaded by changing variant."""
        opened(conn, session="PREMARKET", variant="S6-P", symbol="AAPL")
        assert position_store.open_count(conn) == 1
        # A second, different-variant row would exceed the family limit.
        # The unique index refuses a duplicate SYMBOL outright; the count
        # is what the entry limit reads for a different symbol.
        position_store.record_submission(
            conn, symbol="MSFT", variant="S6-R", entry_session="REGULAR",
            range_high=99.5, range_low=99.0, now=T0)
        assert position_store.open_count(conn) == 2, (
            "open_count must see BOTH variants -- the rollout limit of 1 is "
            "what refuses the second, and it can only do that if the count "
            "spans the family")

    def test_a_closed_position_stops_counting(self, conn):
        pid = opened(conn)
        position_store.close_position(conn, pid, reason="RANGE_REENTRY",
                                      exit_price=101.0, now=T0)
        assert position_store.open_count(conn) == 0

    def test_a_second_buy_into_the_same_symbol_is_refused_by_storage(self, conn):
        """Not by a gate that might itself be mid-restart."""
        import sqlite3

        opened(conn, symbol="AAPL", variant="S6-P")
        with pytest.raises(sqlite3.IntegrityError):
            position_store.record_submission(
                conn, symbol="AAPL", variant="S6-R", entry_session="REGULAR",
                range_high=99.5, range_low=99.0, now=T0)


# ====================================================================
# Existing position is managed in EVERY session
# ====================================================================
class TestManagementContinuesWhereOrderingCannot:
    def test_exits_are_evaluated_in_a_session_that_cannot_order(self, conn):
        pid = opened(conn, session="PREMARKET", variant="S6-P")
        outcomes = exit_runtime.run_exits(
            conn, broker_adapter=None, features_fn=lambda s: None,
            price_fn=lambda s: 98.0, session="PREMARKET", now=T0,
            orders_allowed=False)
        assert [o["symbol"] for o in outcomes] == ["AAPL"]
        assert outcomes[0]["action"] == exit_runtime.ACTION_LATCHED
        assert position_store.load(conn, pid)["status"] == "EXIT_PENDING"

    def test_a_latched_exit_is_retried_in_the_next_orderable_session(self, conn):
        pid = opened(conn, session="PREMARKET", variant="S6-P")
        exit_runtime.run_exits(
            conn, broker_adapter=None, features_fn=lambda s: None,
            price_fn=lambda s: 98.0, session="PREMARKET", now=T0,
            orders_allowed=False)
        assert position_store.load(conn, pid)["status"] == "EXIT_PENDING"

        sent = []

        class Adapter:
            def submit_order(self, symbol, quantity, side, client_order_id):
                sent.append((symbol, quantity, side))

                class R:
                    status_code, text = 200, "ok"
                return R()

        outcomes = exit_runtime.retry_latched_exits(
            conn, broker_adapter=Adapter(), session="REGULAR",
            now=T0 + timedelta(hours=3), orders_allowed=True)
        assert sent == [("AAPL", 1, "sell")]
        assert outcomes[0]["action"] == exit_runtime.ACTION_SOLD
        assert position_store.load(conn, pid)["status"] == "EXIT_SUBMITTED"

    def test_the_original_exit_reason_is_not_relabelled_by_the_new_session(
            self, conn):
        pid = opened(conn, session="PREMARKET", variant="S6-P")
        position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)
        position_store.latch_pending_exit(conn, pid, "SESSION_EXIT", now=T0)
        assert position_store.load(conn, pid)["pending_exit_reason"] == \
            "RANGE_REENTRY"

    def test_exit_submitted_is_not_resubmitted_after_a_session_change(self, conn):
        pid = opened(conn, session="PREMARKET", variant="S6-P")
        position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)
        position_store.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)

        sent = []

        class Adapter:
            def submit_order(self, *a, **k):
                sent.append(a)
                raise AssertionError("must not submit a second SELL")

        outcomes = exit_runtime.retry_latched_exits(
            conn, broker_adapter=Adapter(), session="REGULAR",
            now=T0 + timedelta(hours=3), orders_allowed=True)
        assert sent == []
        assert outcomes == []

    def test_the_policy_refuses_to_decide_once_a_sell_is_out(self, conn):
        pid = opened(conn, session="PREMARKET", variant="S6-P")
        position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)
        position_store.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)
        row = position_store.load(conn, pid)
        decision = exit_policy.decide(position_store.to_state(row),
                                      current_price=98.0, session="REGULAR",
                                      now=T0)
        assert decision.sells is False
        assert decision.reason == exit_policy.REASON_ALREADY_SUBMITTED

    def test_a_partial_sell_leaves_the_remainder_managed(self, conn):
        pid = opened(conn, session="PREMARKET", variant="S6-P")
        conn.execute("UPDATE s6_positions SET quantity = 3 WHERE position_id = ?",
                     (pid,))
        conn.commit()
        position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)
        position_store.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)

        results = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 1,
                                         "average_fill_price": 101.0},
            now=T0)
        assert results[0]["status"] == "PARTIALLY_SOLD"
        assert results[0]["remaining"] == 2
        row = position_store.load(conn, pid)
        assert row["quantity"] == 2
        assert row["status"] != "CLOSED", "the account still holds shares"

    def test_missing_session_data_is_not_an_exit(self, conn):
        """§6: VWAP/EMA/volume unavailable is NOT_MEASURED, not FAIL."""
        pid = opened(conn, session="PREMARKET", variant="S6-P")
        row = position_store.load(conn, pid)
        decision = exit_policy.decide(position_store.to_state(row),
                                      current_price=None, features=None,
                                      session="REGULAR", now=T0)
        assert decision.sells is False
        assert decision.reason == exit_policy.REASON_INSUFFICIENT_DATA


# ====================================================================
# §7 / §17: route capability, derived and fail-closed
# ====================================================================
class TestRouteCapabilityIsDerivedNotAssumed:
    def test_extended_hours_sessions_are_blocked_by_the_rollout_control(self):
        for session in ("PREMARKET", "AFTER_HOURS"):
            cap = session_capability.capability(session)
            assert cap.buy.status == session_capability.BLOCKED
            assert cap.buy.reason == \
                session_capability.REASON_EXTENDED_HOURS_FORBIDDEN
            assert cap.order_capable is False

    def test_overnight_is_blocked_while_the_rollout_is_regular_only(self):
        cap = session_capability.capability("OVERNIGHT_DAYTIME")
        assert cap.order_capable is False
        assert cap.buy.reason == session_capability.REASON_REGULAR_ONLY

    def test_the_fill_query_verdict_is_session_independent(self):
        """An order id is an order id: the same inquiry answers for a
        PREMARKET entry sold in REGULAR, so the verdict is the runtime's
        rather than the session's."""
        verdicts = {session_capability.capability(s).fill_query.status
                    for s in ("REGULAR", "OVERNIGHT_DAYTIME", "PREMARKET",
                              "AFTER_HOURS")}
        assert len(verdicts) == 1

    def test_order_capable_requires_all_three_routes(self):
        """A BUY that cannot be sold, or a fill that cannot be read back,
        is not a tradeable session."""
        from dataclasses import replace

        cap = session_capability.capability("REGULAR")
        assert cap.buy.verified and cap.sell.verified
        assert cap.fill_query.verified
        assert cap.order_capable is True

        unreadable = session_capability.RouteVerdict(
            session_capability.NOT_VERIFIED,
            session_capability.REASON_FILL_QUERY_UNWIRED, "")
        assert replace(cap, fill_query=unreadable).order_capable is False
        assert replace(cap, sell=unreadable).order_capable is False

    def test_an_unknown_session_fails_closed(self):
        cap = session_capability.capability("NOT_A_SESSION")
        assert cap.order_capable is False


class TestVariantStates:
    def test_blocked_route_is_its_own_state_not_discovery(self):
        states = variant_state.evaluate()
        for variant in ("S6-P", "S6-A", "S6-O"):
            assert states[variant].mode == variant_state.BLOCKED_ORDER_ROUTE

    def test_regular_is_discovery_only_today(self):
        assert variant_state.evaluate()["S6-R"].mode == \
            variant_state.DISCOVERY_ONLY

    def test_a_closed_market_supplies_no_observation(self):
        states = variant_state.evaluate(observations={})
        for state in states.values():
            for name in variant_state.OBSERVATION_CHECKS:
                key = f"{variant_state.PREFIX[state.variant]}_{name}"
                assert state.checks[key] == variant_state.NOT_MEASURED

    def test_observations_are_per_variant_and_never_shared(self):
        """An OVERNIGHT tick is not evidence about REGULAR."""
        states = variant_state.evaluate(
            observations={"overnight_market_tick_verified": True})
        assert states["S6-O"].checks["overnight_market_tick_verified"] == \
            variant_state.PASS
        assert states["S6-R"].checks["regular_market_tick_verified"] == \
            variant_state.NOT_MEASURED

    def test_a_failed_observation_is_not_merely_unmeasured(self):
        states = variant_state.evaluate(
            observations={"regular_market_tick_verified": False})
        assert states["S6-R"].checks["regular_market_tick_verified"] == \
            variant_state.FAIL

    def test_ready_requires_every_check(self):
        """All six -> READY. Any one missing -> not READY."""
        full = {"regular_market_tick_verified": True,
                "regular_candidate_freshness_verified": True,
                "regular_common_stock_dry_run_verified": True}
        state = variant_state.evaluate(observations=full)["S6-R"]
        assert state.mode == variant_state.READY_FOR_LIMITED_LIVE
        assert state.blocking == []

        for dropped in list(full):
            partial = {k: v for k, v in full.items() if k != dropped}
            weaker = variant_state.evaluate(observations=partial)["S6-R"]
            assert weaker.mode == variant_state.DISCOVERY_ONLY
            assert dropped in weaker.blocking

    def test_ready_is_not_permission_to_trade(self):
        """READY means a human MAY promote. It must not itself order."""
        full = {"regular_market_tick_verified": True,
                "regular_candidate_freshness_verified": True,
                "regular_common_stock_dry_run_verified": True}
        state = variant_state.evaluate(observations=full)["S6-R"]
        assert state.mode == variant_state.READY_FOR_LIMITED_LIVE
        assert state.may_order is False
        assert slm.SCANNER_LIVE_MODE["orb"] == slm.MODE_DISCOVERY_ONLY

    def test_it_promotes_nothing(self):
        import ast

        text = (REPO_ROOT / "s6_live" / "variant_state.py").read_text()
        tree = ast.parse(text)
        calls = {n.func.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "submit_order" not in calls
        # It reads the live-mode table and never assigns into it.
        assert "SCANNER_LIVE_MODE[" not in text.replace(
            "scanner_live_mode.SCANNER_LIVE_MODE", "")

    def test_the_table_renders_every_variant(self):
        table = variant_state.format_table(variant_state.evaluate())
        for variant in ("S6-O", "S6-P", "S6-R", "S6-A"):
            assert variant in table


class TestS1IsUnaffected:
    def test_no_s6_module_here_touches_s1_state(self):
        for name in ("session_capability.py", "variant_state.py"):
            text = (REPO_ROOT / "s6_live" / name).read_text()
            assert "s1_positions" not in text
            assert "s1_live.store" not in text

    def test_the_live_modes_are_unchanged(self):
        assert slm.SCANNER_LIVE_MODE["hma_early_trend"] == slm.MODE_LIMITED_LIVE
        assert slm.SCANNER_LIVE_MODE["accumulation"] == slm.MODE_DISCOVERY_ONLY
        assert slm.SCANNER_LIVE_MODE["orb"] == slm.MODE_DISCOVERY_ONLY

    def test_s6_live_sessions_is_unchanged(self):
        assert s6_sessions.LIVE_SESSIONS == frozenset({"REGULAR"})
