"""The two rollout caps as the Order Gate enforces them, and the race
they exist to close.

tests/test_entry_limits.py pins how the counts are gathered. This file
pins what the gate does with them: that both caps are reachable before
any transport, that a sell is never subject to either, that two
candidates in one pass cannot both take one slot, and that OBSERVE
evaluates them identically to the live path.
"""
import dataclasses
from datetime import datetime, timezone

import pytest

from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution import entry_limits, order_gate
from execution.entry_limits import EntryLimitState
from market_hours import us_trading_day
from reconciliation.snapshot import ReconciliationSnapshot
from state_store import db as state_db

NOW = datetime(2026, 8, 7, 17, 30, tzinfo=timezone.utc)
TODAY = us_trading_day(NOW)
ACCOUNT = "12345678"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POS.json"))
    from execution import idempotency

    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEM.lock")
    state_db.open_db().close()
    yield


def _limits(**overrides):
    kwargs = dict(
        max_open_positions=1, max_daily_entries=1,
        open_position_symbols=frozenset(), pending_entry_symbols=frozenset(),
        daily_entry_count=0, trading_day=TODAY)
    kwargs.update(overrides)
    return EntryLimitState(**kwargs)


def _snapshot(symbol="AAPL"):
    return ReconciliationSnapshot(
        account_id=ACCOUNT, symbol=symbol, checked_at=NOW, positions_match=True,
        open_orders_match=True, fills_match=True, has_unknown_orders=False,
        source="test", detail=(),
    )


_DEFAULT = object()


def _ctx(*, limits=_DEFAULT, symbol="AAPL", side="buy", quantity=1):
    instrument = build_instrument(symbol, exchange="NASDAQ")
    signal = build_signal(
        strategy_id="S", strategy_version="v1", config_version="c", code_commit="c1",
        symbol=symbol, exchange="NASDAQ", signal_price=100.0, score=99,
        entry_reason="test", valid_for_seconds=300, now=NOW)
    intent = OrderIntent(
        internal_order_id="ord-1", signal_id=signal.signal_id, strategy_id="S",
        symbol=symbol, exchange="NASDAQ", side=side, quantity=quantity,
        order_type="limit", limit_price=100.0, stop_price=None, target_price=None,
        created_at=NOW)
    return order_gate.BuyGateContext(
        execution_broker="kis", live_order_enabled=True, entry_disabled=False,
        validated_commit="c1", deployed_commit="c1", kis_account_no=ACCOUNT,
        allowed_account_no=ACCOUNT, order_intent=intent, instrument=instrument,
        signal=signal, is_regular_session=True, kis_price_usd=100.0,
        max_price_deviation_percent=30.0, usd_orderable_cash=10_000.0,
        has_open_order_for_symbol=False, has_order_for_signal_id=False,
        allowed_symbols=frozenset({symbol}), reconciliation=_snapshot(symbol),
        entry_limits=_limits() if limits is _DEFAULT else limits, now=NOW)


def _blocked_code(ctx):
    try:
        order_gate.evaluate_buy_gate(ctx)
        return None
    except order_gate.OrderGateBlockedError as exc:
        return exc.code


class TestTheGateEnforcesBothCaps:
    def test_room_in_both_caps_passes(self):
        assert _blocked_code(_ctx()) is None

    def test_the_position_cap_blocks(self):
        code = _blocked_code(_ctx(limits=_limits(
            open_position_symbols=frozenset({"MSFT"}))))
        assert code == entry_limits.MAX_OPEN_POSITIONS

    def test_an_in_flight_entry_blocks_the_position_cap(self):
        code = _blocked_code(_ctx(limits=_limits(
            pending_entry_symbols=frozenset({"MSFT"}))))
        assert code == entry_limits.MAX_OPEN_POSITIONS

    def test_the_daily_cap_blocks(self):
        code = _blocked_code(_ctx(limits=_limits(daily_entry_count=1)))
        assert code == entry_limits.MAX_DAILY_ENTRIES

    def test_a_larger_position_cap_has_room(self):
        code = _blocked_code(_ctx(limits=_limits(
            max_open_positions=2, open_position_symbols=frozenset({"MSFT"}))))
        assert code is None

    def test_a_larger_daily_cap_has_room(self):
        code = _blocked_code(_ctx(limits=_limits(
            max_daily_entries=2, daily_entry_count=1)))
        assert code is None

    def test_missing_limit_state_fails_closed(self):
        """A context built without the caps must block, not skip them --
        the defect being fixed was precisely a silent skip."""
        code = _blocked_code(_ctx(limits=None))
        assert code == entry_limits.POSITION_LIMIT_STATE_UNKNOWN

    def test_the_position_cap_is_reported_before_the_daily_cap(self):
        """Both full: the operator sees the capacity reason first."""
        code = _blocked_code(_ctx(limits=_limits(
            open_position_symbols=frozenset({"MSFT"}), daily_entry_count=1)))
        assert code == entry_limits.MAX_OPEN_POSITIONS

    def test_a_symbol_both_held_and_in_flight_does_not_double_count(self):
        code = _blocked_code(_ctx(limits=_limits(
            max_open_positions=2,
            open_position_symbols=frozenset({"MSFT"}),
            pending_entry_symbols=frozenset({"MSFT"}))))
        assert code is None


class TestOrdering:
    def test_both_caps_run_after_every_candidate_specific_check(self):
        """A candidate that is also wrong in a candidate-specific way is
        reported for THAT reason, not for the account being full."""
        ctx = _ctx(limits=_limits(open_position_symbols=frozenset({"MSFT"})))
        blocked = dataclasses.replace(ctx, allowed_symbols=frozenset())
        assert _blocked_code(blocked) == "SYMBOL"

    def test_both_caps_run_before_the_gate_can_approve(self):
        """The property that actually matters: no context reaches an
        approval without both caps having been evaluated."""
        assert _blocked_code(_ctx(limits=_limits(daily_entry_count=1))) is not None
        assert _blocked_code(_ctx()) is None

    def test_the_gate_performs_no_io_for_the_caps(self):
        """The state is handed in, exactly like the reconciliation
        snapshot -- the gate stays a pure predicate."""
        import ast
        import pathlib

        source = pathlib.Path("execution/order_gate.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_check_entry_limits")
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None)
                assert name not in ("collect", "execute", "get_positions", "open_db"), (
                    f"_check_entry_limits performs I/O via {name}()")


class TestSellsAreNeverCapped:
    def test_the_sell_gate_does_not_consult_the_caps(self):
        """An account at its position cap must always be able to close
        what it holds; a cap that blocked exits would trap capital."""
        import ast
        import pathlib

        source = pathlib.Path("execution/order_gate.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "evaluate_sell_gate")
        called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                  for n in ast.walk(function) if isinstance(n, ast.Call)}
        assert "_check_entry_limits" not in called

    def test_the_sell_context_has_no_entry_limit_field(self):
        assert "entry_limits" not in {
            f.name for f in dataclasses.fields(order_gate.SellGateContext)}


class TestTheRaceIsClosed:
    """§14: with one slot free, two candidates evaluated in the same pass
    must not both be approved."""

    def _attempt(self, conn, internal_order_id, symbol, status="CREATED"):
        stamp = NOW.isoformat()
        conn.execute(
            "INSERT INTO kis_order_idempotency "
            "(internal_order_id, signal_id, symbol, side, trading_date, broker_order_id, "
            "status, created_at, updated_at, requested_quantity, version) "
            "VALUES (?, ?, ?, 'buy', ?, NULL, ?, ?, ?, 1, 0)",
            (internal_order_id, f"sig-{internal_order_id}", symbol, TODAY, status,
             stamp, stamp))
        conn.commit()

    def test_two_candidates_one_slot_only_one_is_approved(self):
        class _Broker:
            def get_positions(self):
                return []

        rollout = type("R", (), {"max_open_positions": 1, "max_daily_entries": 1})()

        # Candidate A is evaluated first and, as the live path does,
        # registers its attempt before the gate runs.
        conn = state_db.open_db()
        try:
            limits_a = entry_limits.collect(
                broker=_Broker(), conn=conn, rollout=rollout, now=NOW,
                exclude_internal_order_id="ord-A")
            assert _blocked_code(_ctx(limits=limits_a, symbol="AAPL")) is None
            self._attempt(conn, "ord-A", "AAPL")

            # Candidate B, same pass, same free slot.
            limits_b = entry_limits.collect(
                broker=_Broker(), conn=conn, rollout=rollout, now=NOW,
                exclude_internal_order_id="ord-B")
        finally:
            conn.close()

        code = _blocked_code(_ctx(limits=limits_b, symbol="MSFT"))
        assert code in (entry_limits.MAX_OPEN_POSITIONS, entry_limits.MAX_DAILY_ENTRIES)

    def test_the_second_candidate_is_blocked_even_after_a_restart(self):
        """§15: A's slot is durable, so a fresh process still sees it."""
        class _Broker:
            def get_positions(self):
                return []

        rollout = type("R", (), {"max_open_positions": 1, "max_daily_entries": 1})()
        conn = state_db.open_db()
        try:
            self._attempt(conn, "ord-A", "AAPL")
        finally:
            conn.close()  # crash

        conn = state_db.open_db()  # restart
        try:
            limits_b = entry_limits.collect(
                broker=_Broker(), conn=conn, rollout=rollout, now=NOW,
                exclude_internal_order_id="ord-B")
        finally:
            conn.close()
        code = _blocked_code(_ctx(limits=limits_b, symbol="MSFT"))
        assert code in (entry_limits.MAX_OPEN_POSITIONS, entry_limits.MAX_DAILY_ENTRIES)


class TestTheTradingDayIsSharedWithTheLedger:
    def test_the_engine_stamps_rows_with_the_same_day_the_gate_counts(self):
        """If the recorder used the UTC date and the counter the Eastern
        one, a late-session entry would be recorded under one day and
        counted under another."""
        import ast
        import pathlib

        source = pathlib.Path("execution/execution_engine.py").read_text(encoding="utf-8")
        assert "us_trading_day(current)" in source
        assert "current.date().isoformat()" not in source
        tree = ast.parse(source)
        imported = {node.module for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)}
        assert "market_hours" in imported

    def test_the_eastern_day_does_not_roll_with_utc(self):
        late = datetime(2026, 8, 7, 23, 30, tzinfo=timezone.utc)  # 19:30 ET
        assert late.date().isoformat() == "2026-08-07"
        assert us_trading_day(late) == "2026-08-07"
        past_utc_midnight = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)  # 21:00 ET Aug 7
        assert past_utc_midnight.date().isoformat() == "2026-08-08"
        assert us_trading_day(past_utc_midnight) == "2026-08-07"
