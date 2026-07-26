"""CODEX-026/029/031: live-mode pre-trade gate tests.

Pure unit tests for live_readiness/order_gateway.py (SQLite-backed via
live_readiness/entry_reservation_ledger.py, isolated to tmp_path), plus
integration tests confirming paper_strategy_order.submit_order() and
AlpacaBroker.submit_order() both enforce this gate only for side="buy" +
live mode, and that a blocked order never reaches the broker (HTTP/
session call count assertions).
"""
from datetime import datetime, timedelta, timezone

import pytest

from live_readiness import entry_reservation_ledger as ledger
from live_readiness.order_gateway import (
    MAX_CONCURRENT_LIVE_POSITIONS,
    MAX_DAILY_LIVE_ENTRIES,
    PILOT_TOTAL_BUDGET_KRW,
    LiveEntryContext,
    LiveOrderBlockedError,
    validate_and_size_live_entry,
)
from state_store import db as state_db

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    # CODEX-031's authoritative snapshot lives in SQLite -- every test in
    # this file must be isolated from the real repo-root database, exactly
    # like every other SQLite-touching test file in this suite.
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    live_lock = tmp_path / "LIVE_ENTRY_RESERVATION.lock"
    monkeypatch.setattr(ledger, "_LOCK_FILE", live_lock)
    yield


def _ctx(**overrides):
    defaults = dict(
        symbol="AAPL",
        expected_fill_price_usd=10.0,
        allow_list=["AAPL", "MSFT"],
        available_cash_krw=30_000,
        fx_rate_krw_per_usd=1_350.0,
        fx_rate_as_of=NOW.isoformat(),
        max_order_notional_krw=30_000,
        max_daily_loss_krw=10_000,
        max_position_count=1,
        current_open_position_count=0,
        max_daily_entries=2,
        today_entry_count=0,
        stop_price_usd=9.0,
        now=NOW,
    )
    defaults.update(overrides)
    return LiveEntryContext(**defaults)


def _sized(ctx, order_symbol=None):
    """Most tests aren't exercising CODEX-029's symbol-identity check --
    default order_symbol to ctx.symbol so those call sites read exactly
    as they did before that check was added. Returns just the quantity
    (the approval's other field, reservation_id, is exercised directly by
    the CODEX-031 tests below)."""
    approval = validate_and_size_live_entry(ctx, order_symbol if order_symbol is not None else ctx.symbol)
    return approval.quantity


def _seed_reservation(conn, *, symbol="MSFT", notional_krw=100.0, state=ledger.STATE_COMMITTED,
                       position_id=None, created_at=None):
    """Directly seed a reservation row for tests that need to simulate
    "budget/count already consumed by a prior entry" -- CODEX-031 no
    longer trusts LiveEntryContext.current_open_position_count/
    today_entry_count, so these tests must seed the actual durable ledger
    instead of just setting a context field."""
    reservation_id = ledger.reserve(conn, symbol, notional_krw)
    if state != ledger.STATE_RESERVED:
        conn.execute(
            "UPDATE live_entry_reservations SET state = ?, position_id = ? WHERE reservation_id = ?",
            (state, position_id, reservation_id),
        )
        conn.commit()
    if created_at is not None:
        conn.execute(
            "UPDATE live_entry_reservations SET created_at = ? WHERE reservation_id = ?",
            (created_at, reservation_id),
        )
        conn.commit()
    return reservation_id


def test_valid_entry_returns_positive_quantity():
    qty = _sized(_ctx())
    assert qty > 0


def test_symbol_not_on_allow_list_blocked():
    with pytest.raises(LiveOrderBlockedError, match="allow-list"):
        _sized(_ctx(symbol="TSLA"))


def test_empty_allow_list_blocks_everything():
    with pytest.raises(LiveOrderBlockedError, match="allow-list"):
        _sized(_ctx(allow_list=[]))


def test_missing_fx_rate_blocked():
    with pytest.raises(LiveOrderBlockedError, match="FX rate"):
        _sized(_ctx(fx_rate_krw_per_usd=None))


def test_zero_or_negative_fx_rate_blocked():
    with pytest.raises(LiveOrderBlockedError, match="FX rate"):
        _sized(_ctx(fx_rate_krw_per_usd=0))
    with pytest.raises(LiveOrderBlockedError, match="FX rate"):
        _sized(_ctx(fx_rate_krw_per_usd=-100))


def test_nan_fx_rate_blocked():
    with pytest.raises(LiveOrderBlockedError):
        _sized(_ctx(fx_rate_krw_per_usd=float("nan")))


def test_missing_fx_timestamp_blocked():
    with pytest.raises(LiveOrderBlockedError, match="timestamp"):
        _sized(_ctx(fx_rate_as_of=None))


def test_stale_fx_rate_blocked():
    stale = (NOW - timedelta(hours=1)).isoformat()
    with pytest.raises(LiveOrderBlockedError, match="stale"):
        _sized(_ctx(fx_rate_as_of=stale, max_fx_rate_age_seconds=300))


def test_future_fx_timestamp_blocked():
    future = (NOW + timedelta(hours=1)).isoformat()
    with pytest.raises(LiveOrderBlockedError, match="stale"):
        _sized(_ctx(fx_rate_as_of=future))


def test_naive_fx_timestamp_blocked():
    with pytest.raises(LiveOrderBlockedError, match="timezone-aware"):
        _sized(_ctx(fx_rate_as_of="2026-07-26T12:00:00"))


def test_no_available_cash_blocked():
    with pytest.raises(LiveOrderBlockedError, match="available cash"):
        _sized(_ctx(available_cash_krw=0))


def test_price_rise_making_order_unaffordable_blocked():
    # a price spike between signal and submission can make even the
    # capped budget unable to afford a single share -- sizing itself
    # reports this as INSUFFICIENT_FUNDS.
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(_ctx(
            expected_fill_price_usd=1000.0, max_order_notional_krw=5_000, available_cash_krw=30_000,
            stop_price_usd=None,
        ))


def test_max_order_notional_below_available_cash_still_enforced():
    # budget is capped at max_order_notional_krw even though more cash exists
    ctx = _ctx(
        available_cash_krw=1_000_000, max_order_notional_krw=13_500, expected_fill_price_usd=5.0,
        stop_price_usd=None,
    )
    qty = _sized(ctx)
    assert qty * 5.0 <= 13_500 / 1_350.0 + 1e-6


def test_stop_loss_risk_exceeding_daily_loss_cap_blocked():
    with pytest.raises(LiveOrderBlockedError, match="risk"):
        _sized(_ctx(
            expected_fill_price_usd=10.0, stop_price_usd=1.0, max_daily_loss_krw=1.0,
        ))


def test_stop_price_not_below_entry_price_blocked():
    with pytest.raises(LiveOrderBlockedError, match="risk"):
        _sized(_ctx(stop_price_usd=10.0, expected_fill_price_usd=10.0))


def test_no_stop_price_skips_risk_check():
    qty = _sized(_ctx(stop_price_usd=None))
    assert qty > 0


def test_insufficient_funds_propagates_as_blocked():
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(_ctx(available_cash_krw=1, expected_fill_price_usd=1000.0))


def test_fractional_disallowed_by_default():
    ctx = _ctx()
    assert ctx.fractional_shares_allowed is False
    qty = _sized(ctx)
    assert isinstance(qty, int)


# ---------------------------------------------------------------------------
# CODEX-031: authoritative (not caller-trusted) 30,000 KRW ceiling, daily
# entry count, and concurrent-position count -- computed from the durable
# entry_reservation_ledger, never from LiveEntryContext's own count/limit
# fields (which the caller can no longer use to unlock more than the
# trusted ceilings below).
# ---------------------------------------------------------------------------

def test_caller_claiming_inflated_budget_is_still_capped_at_30000():
    """CODEX-031's exact reproduction: a caller reporting a 3,000,000 KRW
    budget must never be approved for anything beyond the trusted 30,000
    KRW pilot ceiling."""
    ctx = _ctx(
        available_cash_krw=3_000_000, max_order_notional_krw=3_000_000, max_daily_loss_krw=3_000_000,
        expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None,
    )
    approval = validate_and_size_live_entry(ctx, "AAPL")
    notional_krw = approval.quantity * 10.0 * 1_350.0
    assert notional_krw <= PILOT_TOTAL_BUDGET_KRW


def test_caller_claiming_inflated_position_and_entry_counts_is_ignored():
    """current_open_position_count/today_entry_count are no longer read
    for the gating decision at all -- a caller claiming 0 (to hide real
    exposure) must not bypass the authoritative ledger-derived count."""
    conn = state_db.open_db()
    _seed_reservation(conn, symbol="MSFT", state=ledger.STATE_COMMITTED)  # 1 already "open"
    ctx = _ctx(current_open_position_count=0, max_position_count=1)  # caller lies: claims 0 open
    with pytest.raises(LiveOrderBlockedError, match="concurrent positions"):
        _sized(ctx)


def test_authoritative_open_position_count_blocks_at_trusted_ceiling():
    conn = state_db.open_db()
    _seed_reservation(conn, symbol="MSFT", state=ledger.STATE_COMMITTED)
    # Caller tries to loosen the cap to 5 -- trusted MAX_CONCURRENT_LIVE_POSITIONS still wins.
    ctx = _ctx(max_position_count=5)
    with pytest.raises(LiveOrderBlockedError, match="concurrent positions"):
        _sized(ctx)


def test_authoritative_daily_entry_count_blocks_at_trusted_ceiling():
    conn = state_db.open_db()
    for _ in range(MAX_DAILY_LIVE_ENTRIES):
        _seed_reservation(conn, symbol="MSFT", state=ledger.STATE_RELEASED)  # even released attempts count
    # Caller tries to loosen the cap to 99 -- trusted MAX_DAILY_LIVE_ENTRIES still wins.
    ctx = _ctx(max_daily_entries=99)
    with pytest.raises(LiveOrderBlockedError, match="daily entries"):
        _sized(ctx)


def test_reserved_and_committed_notional_both_count_toward_budget():
    """Seed a COMMITTED reservation linked to an already-terminal position
    (so it doesn't also trip the MAX_CONCURRENT_LIVE_POSITIONS=1 ceiling
    before the budget check is ever reached) and confirm its notional
    still counts toward the cumulative 30,000 KRW ceiling."""
    from positions import states, store as position_store

    record = position_store.create_position("S", "1.0", "MSFT", "coid-budget", 1)
    with position_store.locked_position(record["position_id"]) as locked:
        states.validate_transition(locked["state"], states.REJECTED)
        locked["state"] = states.REJECTED
        locked["state_history"].append({"state": states.REJECTED, "at": "t", "reason": "test"})

    conn = state_db.open_db()
    _seed_reservation(conn, notional_krw=20_000, state=ledger.STATE_COMMITTED, position_id=record["position_id"])

    ctx = _ctx(expected_fill_price_usd=1_000.0, fx_rate_krw_per_usd=1_000.0, stop_price_usd=None)
    # Only 10,000 KRW of the 30,000 ceiling remains -- 1000 USD/share * 1000 FX = 1,000,000 KRW/share, unaffordable.
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(ctx)


def test_released_reservation_does_not_count_toward_budget():
    conn = state_db.open_db()
    _seed_reservation(conn, notional_krw=29_999, state=ledger.STATE_RELEASED)
    ctx = _ctx(
        available_cash_krw=1, max_order_notional_krw=1, expected_fill_price_usd=10.0,
        fx_rate_krw_per_usd=1_350.0, stop_price_usd=None,
    )
    # Released reservation doesn't consume budget; this still blocks, but for
    # the unrelated reason that available_cash_krw itself is 1 KRW.
    with pytest.raises(LiveOrderBlockedError, match="no budget|sizing blocked"):
        _sized(ctx)
    # Confirm the budget-exhaustion message specifically is NOT what blocked it.
    try:
        _sized(ctx)
    except LiveOrderBlockedError as exc:
        assert "pilot budget exhausted" not in str(exc)


def test_committed_reservation_for_closed_position_frees_position_count_not_budget():
    """CODEX-031 design: the 30,000 KRW ceiling is a cumulative,
    never-decreasing pilot total, but the concurrent-position count must
    reflect currently-open positions only -- a committed reservation
    whose linked position has since closed no longer counts toward
    MAX_CONCURRENT_LIVE_POSITIONS, but its notional still counts toward
    the lifetime 30,000 KRW ceiling."""
    from positions import states, store as position_store

    record = position_store.create_position("S", "1.0", "MSFT", "coid-x", 1)
    with position_store.locked_position(record["position_id"]) as locked:
        states.validate_transition(locked["state"], states.REJECTED)
        locked["state"] = states.REJECTED  # terminal
        locked["state_history"].append({"state": states.REJECTED, "at": "t", "reason": "test"})

    conn = state_db.open_db()
    _seed_reservation(conn, notional_krw=100.0, state=ledger.STATE_COMMITTED, position_id=record["position_id"])

    # Confirm the position-count exclusion BEFORE attempting a new entry
    # (a successful validate_and_size_live_entry() call below makes its
    # own new reservation, which would itself then count).
    snapshot_before = ledger.build_snapshot(conn)
    assert snapshot_before.active_notional_krw >= 100.0  # budget still counted cumulatively
    assert snapshot_before.active_position_count == 0  # freed -- funded position is terminal

    # Position count is free again -- a second entry should NOT be
    # blocked by the concurrent-position cap.
    qty = _sized(_ctx(max_position_count=1))
    assert qty > 0


def test_reserve_and_commit_and_release_lifecycle():
    conn = state_db.open_db()
    reservation_id = ledger.reserve(conn, "AAPL", 1000.0)
    row = conn.execute(
        "SELECT state FROM live_entry_reservations WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()
    assert row["state"] == ledger.STATE_RESERVED

    ledger.mark_committed(conn, reservation_id, position_id="pos_123")
    row = conn.execute(
        "SELECT state, position_id FROM live_entry_reservations WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()
    assert row["state"] == ledger.STATE_COMMITTED
    assert row["position_id"] == "pos_123"

    ledger.mark_released(conn, reservation_id)
    row = conn.execute(
        "SELECT state FROM live_entry_reservations WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()
    assert row["state"] == ledger.STATE_RELEASED


def test_successful_validate_and_size_creates_a_reservation():
    conn = state_db.open_db()
    approval = validate_and_size_live_entry(_ctx(), "AAPL", conn=conn)
    row = conn.execute(
        "SELECT state, notional_krw FROM live_entry_reservations WHERE reservation_id = ?",
        (approval.reservation_id,),
    ).fetchone()
    assert row["state"] == ledger.STATE_RESERVED
    assert row["notional_krw"] > 0


def test_concurrent_double_entry_atomic_reservation_blocks_second():
    """Two "simultaneous" entry attempts against a 1-position ceiling: the
    first reserves and commits an open position; by the time the second
    reads the snapshot (necessarily after the first releases the lock),
    it must see the first's reservation and be blocked -- proving the
    snapshot-then-reserve sequence is atomic under reservation_lock()."""
    import threading

    conn_setup = state_db.open_db()  # pre-warm schema before threads
    results = []
    errors = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait(timeout=2)
            try:
                approval = validate_and_size_live_entry(_ctx(symbol="AAPL", max_position_count=1), "AAPL")
                conn = state_db.open_db()
                ledger.mark_committed(conn, approval.reservation_id, position_id=f"pos-{approval.reservation_id}")
                conn.close()
                results.append("approved")
            except LiveOrderBlockedError:
                results.append("blocked")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)

    assert not errors
    assert sorted(results) == ["approved", "blocked"]


# ---------------------------------------------------------------------------
# Integration: paper_strategy_order.submit_order() actually enforces this
# gate for side="buy" + live mode, and a blocked order never reaches the
# broker's network session (real AlpacaBroker + a session double whose
# .request() raises if ever called -- proves zero HTTP calls, not just
# zero calls to a mock broker).
# ---------------------------------------------------------------------------

import paper_strategy_order as pso
from broker import AlpacaBroker, BrokerConfig


class _NetworkForbiddenSession:
    def __init__(self):
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        raise AssertionError("No network call should ever be made for a blocked live entry")


def _live_broker():
    return AlpacaBroker(
        config=BrokerConfig(trading_mode="live", enable_real_trading=True, live_dry_run=False,
                             api_key="key", secret_key="secret"),
        session=_NetworkForbiddenSession(),
    )


@pytest.fixture(autouse=True)
def _isolate_kill_switches(tmp_path, monkeypatch):
    # Both kill switches must default to allowing entries for these tests
    # to exercise the CODEX-026 gate specifically, not get blocked earlier
    # for an unrelated reason.
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL_SWITCH"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH_STATE.json"))
    yield


def test_live_buy_without_context_blocked_zero_network_calls():
    broker = _live_broker()
    response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-1", side="buy")
    assert response.status_code == 423
    assert response.data["blocked_reason"] == "MISSING_LIVE_ENTRY_CONTEXT"
    assert broker.session.requests == []


def test_live_buy_symbol_not_allowed_blocked_zero_network_calls():
    broker = _live_broker()
    ctx = _ctx(symbol="AAPL", allow_list=["MSFT"])
    response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-2", side="buy",
                                 live_entry_context=ctx)
    assert response.status_code == 423
    assert "allow-list" in response.data["blocked_reason"]
    assert broker.session.requests == []


def test_live_sell_never_gated_by_live_entry_context():
    # Exits are never subject to this gate, even in live mode -- an
    # existing position must always be closeable.
    broker = AlpacaBroker(
        config=BrokerConfig(trading_mode="live", enable_real_trading=False, live_dry_run=True,
                             api_key="key", secret_key="secret"),
        session=_NetworkForbiddenSession(),
    )
    response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-3", side="sell")
    # live_dry_run short-circuits before any real network call regardless;
    # what matters here is that no LiveOrderBlockedError/423 was raised
    # for the missing live_entry_context on a sell.
    assert response.status_code != 423 or response.data.get("blocked_reason") is None


def test_paper_mode_buy_unaffected_by_missing_live_entry_context():
    session = _NetworkForbiddenSession()
    broker = AlpacaBroker(
        config=BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret"),
        session=session,
    )
    # Paper mode must reach the real submission path unmodified by the
    # CODEX-026 gate -- it proceeds straight past it into the broker's own
    # existing credential-revalidation gate (which fails here only because
    # this test process has no real env credentials configured, proving we
    # got well past the live-entry-context check, not blocked by it).
    with pytest.raises(RuntimeError, match="Credential revalidation failed"):
        pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-4", side="buy")


def test_broker_double_without_config_attribute_unaffected():
    """Regression: getattr(broker.config, "is_live_mode", False) evaluates
    `broker.config` eagerly and raised AttributeError for any test double
    lacking a .config attribute entirely (most FakeBroker doubles used
    throughout this test suite) -- getattr's default only protects the
    *named* attribute lookup, not an attribute chain. Fixed by resolving
    `broker.config` itself via getattr(broker, "config", None) first."""

    class _NoConfigBroker:
        def __init__(self):
            self.submit_calls = []

        def submit_order(self, symbol, qty=1, *, side, client_order_id=None):
            self.submit_calls.append((symbol, qty, side, client_order_id))
            class _Resp:
                status_code = 200
                text = "OK"
                data = {"status": "filled", "filled_qty": qty, "filled_avg_price": 10.0, "id": "x"}
                dry_run = False
            return _Resp()

    broker = _NoConfigBroker()
    response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-5", side="buy")
    assert response.status_code == 200
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# CODEX-029: symbol-identity lock between the approved LiveEntryContext and
# the actual order submitted -- both the pure gateway function and the
# real network boundary (AlpacaBroker.submit_order() itself, closing
# CODEX-026's "direct broker call bypasses the gate" residual risk).
# ---------------------------------------------------------------------------

def test_context_symbol_mismatched_with_order_symbol_blocked():
    with pytest.raises(LiveOrderBlockedError, match="does not match"):
        _sized(_ctx(symbol="AAPL"), order_symbol="TSLA")


def test_case_mutation_between_context_and_order_symbol_blocked():
    # Deliberately NOT normalized -- a case mutation is itself treated as
    # a mismatch, never silently equated to the allow-list-style match.
    with pytest.raises(LiveOrderBlockedError, match="does not match"):
        _sized(_ctx(symbol="AAPL"), order_symbol="aapl")


def test_whitespace_mutation_between_context_and_order_symbol_blocked():
    with pytest.raises(LiveOrderBlockedError, match="does not match"):
        _sized(_ctx(symbol="AAPL"), order_symbol=" AAPL")


def test_empty_order_symbol_blocked():
    with pytest.raises(LiveOrderBlockedError, match="empty"):
        _sized(_ctx(symbol="AAPL"), order_symbol="")


def test_none_order_symbol_blocked():
    # Call the real function directly (not the _sized() convenience
    # wrapper, which treats None as "default to ctx.symbol") to exercise
    # an explicit None order_symbol.
    with pytest.raises(LiveOrderBlockedError, match="empty"):
        validate_and_size_live_entry(_ctx(symbol="AAPL"), None)


def test_empty_context_symbol_blocked():
    with pytest.raises(LiveOrderBlockedError, match="empty"):
        validate_and_size_live_entry(_ctx(symbol=""), "AAPL")


def test_matching_symbols_case_and_whitespace_exact_still_pass():
    qty = _sized(_ctx(symbol="AAPL"), order_symbol="AAPL")
    assert qty > 0


# --- integration: real AlpacaBroker, direct call bypassing the wrapper ---

def test_direct_broker_call_context_aapl_payload_tsla_blocked_zero_network_calls():
    broker = _live_broker()
    ctx = _ctx(symbol="AAPL")
    response = broker.submit_order("TSLA", qty=999999, side="buy", client_order_id="c-direct-1",
                                    live_entry_context=ctx)
    assert response.status_code == 423
    assert "does not match" in response.data["blocked_reason"]
    assert broker.session.requests == []


def test_direct_broker_call_signal_aapl_command_tsla_blocked():
    broker = _live_broker()
    ctx = _ctx(symbol="AAPL", allow_list=["AAPL", "TSLA"])  # even if TSLA is separately allow-listed
    response = broker.submit_order("TSLA", qty=1, side="buy", client_order_id="c-direct-2",
                                    live_entry_context=ctx)
    assert response.status_code == 423
    assert broker.session.requests == []


def test_direct_broker_call_without_context_blocked_zero_network_calls():
    broker = _live_broker()
    response = broker.submit_order("AAPL", qty=1, side="buy", client_order_id="c-direct-3")
    assert response.status_code == 423
    assert response.data["blocked_reason"] == "MISSING_LIVE_ENTRY_CONTEXT"
    assert broker.session.requests == []


def test_direct_broker_call_symbol_not_on_allow_list_blocked_zero_network_calls():
    broker = _live_broker()
    ctx = _ctx(symbol="AAPL", allow_list=["MSFT"])
    response = broker.submit_order("AAPL", qty=1, side="buy", client_order_id="c-direct-4",
                                    live_entry_context=ctx)
    assert response.status_code == 423
    assert "allow-list" in response.data["blocked_reason"]
    assert broker.session.requests == []


def test_direct_broker_call_valid_all_match_reaches_network_boundary():
    """A fully valid, matching live entry passes the CODEX-026/029/031
    gate and proceeds past it -- proven here by reaching broker_config.py's
    own pre-existing "real live trading is disabled" hard block (an
    earlier, unrelated safety gate this pre-live repository always
    enforces) instead of a 423 from the live-entry-context gate. It never
    reaches the network layer either way, but for a different, correct
    reason."""
    broker = _live_broker()
    ctx = _ctx(symbol="AAPL")
    with pytest.raises(RuntimeError, match="Real live trading is disabled"):
        broker.submit_order("AAPL", qty=1, side="buy", client_order_id="c-direct-5", live_entry_context=ctx)
    assert broker.session.requests == []


def test_direct_broker_call_sell_never_gated_by_live_entry_context():
    broker = _live_broker()
    with pytest.raises(RuntimeError, match="Real live trading is disabled"):
        broker.submit_order("AAPL", qty=1, side="sell", client_order_id="c-direct-6")
    assert broker.session.requests == []


def test_stale_live_entry_context_blocked_at_direct_broker_boundary():
    broker = _live_broker()
    stale_fx = (NOW - timedelta(hours=1)).isoformat()
    ctx = _ctx(symbol="AAPL", fx_rate_as_of=stale_fx)
    response = broker.submit_order("AAPL", qty=1, side="buy", client_order_id="c-direct-7",
                                    live_entry_context=ctx)
    assert response.status_code == 423
    assert "stale" in response.data["blocked_reason"]
    assert broker.session.requests == []


def test_wrapper_passes_symbol_mismatch_through_to_broker_boundary_too():
    """End-to-end via the paper_strategy_order.submit_order() wrapper: a
    context/payload symbol mismatch is caught before ever reaching
    AlpacaBroker.submit_order()'s network layer at all. Since `broker`
    here IS an AlpacaBroker, the wrapper defers entirely to the broker's
    own gate (CODEX-031: single reservation point)."""
    broker = _live_broker()
    ctx = _ctx(symbol="AAPL")
    response = pso.submit_order("TSLA", qty=1, broker=broker, client_order_id="c-6", side="buy",
                                 live_entry_context=ctx)
    assert response.status_code == 423
    assert "does not match" in response.data["blocked_reason"]
    assert broker.session.requests == []


def test_budget_boundary_30000_exactly_allowed_30001_blocked():
    ctx_ok = _ctx(
        symbol="AAPL", available_cash_krw=30_000, max_order_notional_krw=30_000,
        fx_rate_krw_per_usd=1_000.0, expected_fill_price_usd=30.0, stop_price_usd=None,
    )
    qty = _sized(ctx_ok)
    assert qty * 30.0 * 1_000.0 <= 30_000


def test_budget_boundary_30001_notional_blocked_even_with_generous_caller_context():
    # A single share's notional (30,000.9 KRW) already exceeds the
    # authoritative 30,000 KRW total budget by ~1 KRW -- sizing must
    # reject it, not round down silently to "close enough," and the
    # caller's own (already-generous) max_order_notional_krw/
    # available_cash_krw cannot loosen the trusted PILOT_TOTAL_BUDGET_KRW
    # ceiling this is ultimately capped against.
    ctx_over = _ctx(
        symbol="AAPL", available_cash_krw=30_000, max_order_notional_krw=30_000,
        fx_rate_krw_per_usd=1_000.0, expected_fill_price_usd=30.0009, stop_price_usd=None,
    )
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(ctx_over)
