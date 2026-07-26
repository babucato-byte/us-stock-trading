"""CODEX-026/029/031/034: live-mode pre-trade gate tests.

Pure unit tests for live_readiness/order_gateway.py (SQLite-backed via
live_readiness/entry_reservation_ledger.py, isolated to tmp_path), plus
integration tests confirming paper_strategy_order.submit_order() and
AlpacaBroker.submit_order() both enforce this gate only for side="buy" +
live mode, and that a blocked order never reaches the broker (HTTP/
session call count assertions).

CODEX-034 replaced the fixed 30,000 KRW pilot ceiling with a
percent-of-current-balance model (cash_usage_percent) and added durable
ambiguous-failure handling (SUBMISSION_UNKNOWN) -- see
live_readiness/order_gateway.py's module docstring for the full formula.
"""
from datetime import datetime, timedelta, timezone

import pytest

from live_readiness import entry_reservation_ledger as ledger
from live_readiness.order_gateway import (
    MAX_CONCURRENT_LIVE_POSITIONS,
    MAX_DAILY_LIVE_ENTRIES,
    LiveEntryContext,
    LiveOrderBlockedError,
    validate_and_size_live_entry,
)
from state_store import db as state_db

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    # CODEX-031/034's authoritative snapshot lives in SQLite -- every test
    # in this file must be isolated from the real repo-root database.
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setattr(ledger, "_LOCK_FILE", tmp_path / "LIVE_ENTRY_RESERVATION.lock")
    yield


def _ctx(**overrides):
    defaults = dict(
        symbol="AAPL",
        expected_fill_price_usd=10.0,
        allow_list=["AAPL", "MSFT"],
        available_cash_krw=30_000,
        cash_usage_percent=100,
        cash_as_of=NOW.isoformat(),
        fx_rate_krw_per_usd=1_350.0,
        fx_rate_as_of=NOW.isoformat(),
        max_position_count=1,
        max_daily_entries=2,
        stop_price_usd=9.0,
        now=NOW,
    )
    defaults.update(overrides)
    return LiveEntryContext(**defaults)


def _sized(ctx, order_symbol=None, *, release=True):
    """Most tests aren't exercising CODEX-029's symbol-identity check --
    default order_symbol to ctx.symbol so those call sites read exactly
    as they did before that check was added. Returns just the quantity.

    By default the reservation this creates is immediately released again,
    since these are pure sizing/validation tests, not lifecycle tests --
    without this, a second _sized() call in the same test would spuriously
    trip the trusted MAX_CONCURRENT_LIVE_POSITIONS=1 ceiling."""
    approval = validate_and_size_live_entry(ctx, order_symbol if order_symbol is not None else ctx.symbol)
    if release:
        conn = state_db.open_db()
        ledger.mark_released(conn, approval.reservation_id)
    return approval.quantity


_seed_counter = [0]


def _seed_reservation(conn, *, symbol="MSFT", notional_krw=100.0, state=ledger.STATE_COMMITTED,
                       position_id=None, created_at=None, client_order_id=None):
    """Directly seed a reservation row for tests that need to simulate
    "cash already consumed by a prior entry"."""
    _seed_counter[0] += 1
    client_order_id = client_order_id or f"seed-{symbol}-{_seed_counter[0]}"
    reservation_id = ledger.reserve(conn, symbol, notional_krw, client_order_id)
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


# ---------------------------------------------------------------------------
# Basic gate checks (symbol, allow-list, FX) -- unchanged by CODEX-034.
# ---------------------------------------------------------------------------

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


def test_nan_fx_rate_blocked():
    with pytest.raises(LiveOrderBlockedError):
        _sized(_ctx(fx_rate_krw_per_usd=float("nan")))


def test_stale_fx_rate_blocked():
    stale = (NOW - timedelta(hours=1)).isoformat()
    with pytest.raises(LiveOrderBlockedError, match="stale"):
        _sized(_ctx(fx_rate_as_of=stale, max_fx_rate_age_seconds=300))


# ---------------------------------------------------------------------------
# cash_usage_percent validation: 1-100 only, reject 0/negative/>100/None/
# string/NaN/Infinity/bool.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_value", [0, -1, 101, 1000, None, "50", float("nan"), float("inf"),
                                        float("-inf"), True, False])
def test_invalid_cash_usage_percent_blocked(bad_value):
    with pytest.raises(LiveOrderBlockedError, match="cash_usage_percent"):
        _sized(_ctx(cash_usage_percent=bad_value))


@pytest.mark.parametrize("good_value", [1, 50, 90, 100, 0.5, 99.9])
def test_valid_cash_usage_percent_accepted(good_value):
    # A large balance + cheap fractional-eligible share so even the
    # smallest tested percent (0.5%) still clears the sizing engine's own
    # minimum-order-amount floor -- this test is only about
    # cash_usage_percent itself being accepted, not about sizing math.
    ctx = _ctx(cash_usage_percent=good_value, available_cash_krw=500_000,
               expected_fill_price_usd=1.0, fx_rate_krw_per_usd=1_000.0,
               fractional_shares_allowed=True, stop_price_usd=None)
    qty = _sized(ctx)
    assert qty > 0


# ---------------------------------------------------------------------------
# Core formula: max_allocatable_cash = available_cash * percent/100.
# ---------------------------------------------------------------------------

def test_100_percent_uses_full_balance():
    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=30.0, stop_price_usd=None)
    qty = _sized(ctx)
    assert qty * 30.0 * 1_000.0 <= 30_000
    assert qty * 30.0 * 1_000.0 > 29_000  # close to the full 30,000 ceiling


def test_90_percent_caps_at_90_percent_of_balance():
    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=90, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=27.0, stop_price_usd=None)
    qty = _sized(ctx)
    assert qty * 27.0 * 1_000.0 <= 27_000
    assert qty * 27.0 * 1_000.0 > 26_000  # close to the 90% (27,000) ceiling, not the full 30,000


def test_no_fixed_30000_ceiling_larger_balance_allows_larger_order():
    """CODEX-034's explicit requirement: 30,000 KRW must not be baked in
    as a permanent system ceiling -- a larger real balance must allow a
    correspondingly larger order."""
    ctx = _ctx(available_cash_krw=300_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=250.0, stop_price_usd=None)
    qty = _sized(ctx)
    notional = qty * 250.0 * 1_000.0
    assert notional > 30_000  # would have been impossible under the old fixed ceiling
    assert notional <= 300_000


def test_balance_change_is_reflected_immediately():
    ctx_small = _ctx(available_cash_krw=10_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0,
                      expected_fill_price_usd=5.0, stop_price_usd=None)
    qty_small = _sized(ctx_small)

    ctx_large = _ctx(available_cash_krw=100_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0,
                      expected_fill_price_usd=5.0, stop_price_usd=None)
    qty_large = _sized(ctx_large)
    assert qty_large > qty_small


# ---------------------------------------------------------------------------
# Cash freshness/validity: missing/stale/negative/NaN/Infinity all blocked.
# ---------------------------------------------------------------------------

def test_missing_cash_lookup_blocked():
    with pytest.raises(LiveOrderBlockedError, match="available cash"):
        _sized(_ctx(available_cash_krw=None))


def test_zero_cash_blocked():
    with pytest.raises(LiveOrderBlockedError, match="available cash"):
        _sized(_ctx(available_cash_krw=0))


def test_negative_cash_blocked():
    with pytest.raises(LiveOrderBlockedError, match="available cash"):
        _sized(_ctx(available_cash_krw=-100))


def test_nan_cash_blocked():
    with pytest.raises(LiveOrderBlockedError, match="available cash"):
        _sized(_ctx(available_cash_krw=float("nan")))


def test_infinite_cash_blocked():
    with pytest.raises(LiveOrderBlockedError, match="available cash"):
        _sized(_ctx(available_cash_krw=float("inf")))


def test_missing_cash_timestamp_blocked():
    with pytest.raises(LiveOrderBlockedError, match="timestamp"):
        _sized(_ctx(cash_as_of=None))


def test_stale_cash_blocked():
    stale = (NOW - timedelta(hours=1)).isoformat()
    with pytest.raises(LiveOrderBlockedError, match="stale"):
        _sized(_ctx(cash_as_of=stale, max_cash_age_seconds=300))


def test_naive_cash_timestamp_blocked():
    with pytest.raises(LiveOrderBlockedError, match="timezone-aware"):
        _sized(_ctx(cash_as_of="2026-07-26T12:00:00"))


# ---------------------------------------------------------------------------
# Deductions: pending/unknown/open-position exposure all reduce
# available_for_new_order.
# ---------------------------------------------------------------------------

def test_pending_reservation_deducted(monkeypatch):
    # MAX_CONCURRENT_LIVE_POSITIONS=1 is authoritative and would otherwise
    # mask the cash-deduction check behind a "max concurrent positions"
    # block as soon as any other reservation exists -- raise it here so
    # this test isolates the cash math specifically.
    monkeypatch.setattr("live_readiness.order_gateway.MAX_CONCURRENT_LIVE_POSITIONS", 5)
    conn = state_db.open_db()
    _seed_reservation(conn, notional_krw=20_000, state=ledger.STATE_RESERVED)
    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=100, expected_fill_price_usd=1_000.0,
               fx_rate_krw_per_usd=1_000.0, stop_price_usd=None, max_position_count=5)
    # 30,000 - 20,000 pending = 10,000 available; 1 share costs 1,000,000 KRW -> unaffordable.
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(ctx)


def test_unknown_submission_reservation_deducted(monkeypatch):
    monkeypatch.setattr("live_readiness.order_gateway.MAX_CONCURRENT_LIVE_POSITIONS", 5)
    conn = state_db.open_db()
    _seed_reservation(conn, notional_krw=20_000, state=ledger.STATE_SUBMISSION_UNKNOWN)
    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=100, expected_fill_price_usd=1_000.0,
               fx_rate_krw_per_usd=1_000.0, stop_price_usd=None, max_position_count=5)
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(ctx)


def test_open_position_cost_deducted(monkeypatch):
    from positions import states, store as position_store

    monkeypatch.setattr("live_readiness.order_gateway.MAX_CONCURRENT_LIVE_POSITIONS", 5)

    record = position_store.create_position("S", "1.0", "MSFT", "coid-open", 1)
    with position_store.locked_position(record["position_id"]) as locked:
        states.validate_transition(locked["state"], states.ARMED)
        locked["state"] = states.ARMED  # non-terminal -- still "open"
        locked["state_history"].append({"state": states.ARMED, "at": "t", "reason": "test"})

    conn = state_db.open_db()
    _seed_reservation(conn, notional_krw=20_000, state=ledger.STATE_COMMITTED, position_id=record["position_id"])

    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=100, expected_fill_price_usd=1_000.0,
               fx_rate_krw_per_usd=1_000.0, stop_price_usd=None, max_position_count=5)
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(ctx)


def test_closed_position_cost_not_deducted():
    from positions import states, store as position_store

    record = position_store.create_position("S", "1.0", "MSFT", "coid-closed", 1)
    with position_store.locked_position(record["position_id"]) as locked:
        states.validate_transition(locked["state"], states.REJECTED)
        locked["state"] = states.REJECTED  # terminal -- position closed
        locked["state_history"].append({"state": states.REJECTED, "at": "t", "reason": "test"})

    conn = state_db.open_db()
    _seed_reservation(conn, notional_krw=20_000, state=ledger.STATE_COMMITTED, position_id=record["position_id"])

    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=100, expected_fill_price_usd=5.0,
               fx_rate_krw_per_usd=1_000.0, stop_price_usd=None)
    qty = _sized(ctx)  # not blocked -- closed position's cost is excluded
    assert qty > 0


# ---------------------------------------------------------------------------
# Final quantity: actual_qty = min(balance_based_qty, risk_based_qty,
# strategy_max_qty) -- resize down, don't just reject.
# ---------------------------------------------------------------------------

def test_risk_cap_resizes_quantity_down_instead_of_rejecting():
    # Balance affords 3 shares ($30 budget / $10 = 3); a tight per-trade
    # risk cap should shrink the quantity, not reject the whole order.
    ctx = _ctx(available_cash_krw=40_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=9.0,  # $1 risk/share
               max_risk_per_trade_krw=1_350.0)  # exactly 1 share's worth of risk
    qty = _sized(ctx)
    assert qty == 1


def test_risk_cap_looser_than_balance_does_not_reduce_quantity():
    ctx = _ctx(available_cash_krw=13_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=9.0,
               max_risk_per_trade_krw=1_000_000.0)  # far looser than the balance ceiling
    qty = _sized(ctx)
    assert qty == 1  # balance-based ceiling still binds, unaffected by the loose risk cap


def test_risk_cap_reduced_to_zero_shares_blocked():
    ctx = _ctx(available_cash_krw=40_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=9.0,
               max_risk_per_trade_krw=1.0)  # far too tight for even 1 share
    with pytest.raises(LiveOrderBlockedError, match="no affordable quantity"):
        _sized(ctx)


def test_strategy_max_quantity_resizes_quantity_down():
    ctx = _ctx(available_cash_krw=40_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=None, strategy_max_quantity=1)
    qty = _sized(ctx)
    assert qty == 1  # balance alone would afford 3


def test_strategy_max_quantity_looser_than_balance_does_not_increase_quantity():
    ctx = _ctx(available_cash_krw=13_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=None, strategy_max_quantity=100)
    qty = _sized(ctx)
    assert qty == 1


def test_strategy_max_quantity_zero_blocked():
    ctx = _ctx(available_cash_krw=40_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=None, strategy_max_quantity=0)
    with pytest.raises(LiveOrderBlockedError, match="no affordable quantity"):
        _sized(ctx)


def test_reservation_notional_reflects_resized_quantity_not_balance_based():
    conn = state_db.open_db()
    ctx = _ctx(available_cash_krw=40_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=None, strategy_max_quantity=1)
    approval = validate_and_size_live_entry(ctx, ctx.symbol)
    row = ledger.get_by_id(conn, approval.reservation_id)
    # 1 share at $10 * 1,350 KRW/$ = 13,500 KRW -- NOT the ~40,500 KRW the
    # unconstrained balance-based sizing would have reserved.
    assert row["notional_krw"] == pytest.approx(13_500.0)


def test_cash_lookup_failure_blocks_new_order():
    with pytest.raises(LiveOrderBlockedError):
        _sized(_ctx(available_cash_krw=None))


# ---------------------------------------------------------------------------
# Concurrent position/daily entry ceilings -- unchanged mechanism from
# CODEX-031, still authoritative (ledger-derived, caller can't loosen).
# ---------------------------------------------------------------------------

def test_authoritative_open_position_count_blocks_at_trusted_ceiling():
    conn = state_db.open_db()
    _seed_reservation(conn, symbol="MSFT", state=ledger.STATE_COMMITTED)
    ctx = _ctx(max_position_count=5)  # caller tries to loosen -- trusted ceiling still wins
    with pytest.raises(LiveOrderBlockedError, match="concurrent positions"):
        _sized(ctx)


def test_authoritative_daily_entry_count_blocks_at_trusted_ceiling():
    conn = state_db.open_db()
    for _ in range(MAX_DAILY_LIVE_ENTRIES):
        _seed_reservation(conn, symbol="MSFT", state=ledger.STATE_RELEASED)
    ctx = _ctx(max_daily_entries=99)
    with pytest.raises(LiveOrderBlockedError, match="daily entries"):
        _sized(ctx)


# ---------------------------------------------------------------------------
# CODEX-034: reservation reserve/commit/mark_submission_unknown/release
# lifecycle and reconciliation.
# ---------------------------------------------------------------------------

def test_reserve_requires_client_order_id():
    conn = state_db.open_db()
    with pytest.raises(ledger.EntryReservationError):
        ledger.reserve(conn, "AAPL", 1000.0, None)


def test_reservation_lifecycle_reserved_committed():
    conn = state_db.open_db()
    reservation_id = ledger.reserve(conn, "AAPL", 1000.0, "coid-1")
    assert ledger.get_by_id(conn, reservation_id)["state"] == ledger.STATE_RESERVED
    ledger.mark_committed(conn, reservation_id, position_id="pos-1")
    row = ledger.get_by_id(conn, reservation_id)
    assert row["state"] == ledger.STATE_COMMITTED
    assert row["position_id"] == "pos-1"


def test_reservation_lifecycle_reserved_submission_unknown_stays_counted():
    conn = state_db.open_db()
    reservation_id = ledger.reserve(conn, "AAPL", 1000.0, "coid-2")
    ledger.mark_submission_unknown(conn, reservation_id)
    row = ledger.get_by_id(conn, reservation_id)
    assert row["state"] == ledger.STATE_SUBMISSION_UNKNOWN
    snapshot = ledger.build_snapshot(conn)
    assert snapshot.unknown_submission_reservations_krw >= 1000.0


def test_cannot_transition_out_of_released_terminal_state():
    conn = state_db.open_db()
    reservation_id = ledger.reserve(conn, "AAPL", 1000.0, "coid-3")
    ledger.mark_released(conn, reservation_id)
    with pytest.raises(ledger.EntryReservationError):
        ledger.mark_committed(conn, reservation_id)


def test_reconcile_by_client_order_id_no_reservation_returns_none():
    conn = state_db.open_db()
    assert ledger.reconcile_by_client_order_id(conn, "does-not-exist", broker=None) is None


def test_reconcile_by_client_order_id_broker_reports_accepted_commits():
    conn = state_db.open_db()
    reservation_id = ledger.reserve(conn, "AAPL", 1000.0, "coid-4")
    ledger.mark_submission_unknown(conn, reservation_id)

    class _Broker:
        def get_order_by_client_order_id(self, coid):
            return {"status": "accepted", "id": "broker-order-1"}

    result = ledger.reconcile_by_client_order_id(conn, "coid-4", _Broker())
    assert result["state"] == ledger.STATE_COMMITTED


def test_reconcile_by_client_order_id_broker_reports_rejected_releases():
    conn = state_db.open_db()
    reservation_id = ledger.reserve(conn, "AAPL", 1000.0, "coid-5")
    ledger.mark_submission_unknown(conn, reservation_id)

    class _Broker:
        def get_order_by_client_order_id(self, coid):
            return {"status": "rejected", "id": "broker-order-2"}

    result = ledger.reconcile_by_client_order_id(conn, "coid-5", _Broker())
    assert result["state"] == ledger.STATE_RELEASED


def test_reconcile_by_client_order_id_broker_unknown_stays_unknown():
    conn = state_db.open_db()
    reservation_id = ledger.reserve(conn, "AAPL", 1000.0, "coid-6")
    ledger.mark_submission_unknown(conn, reservation_id)

    class _Broker:
        def get_order_by_client_order_id(self, coid):
            return None

    result = ledger.reconcile_by_client_order_id(conn, "coid-6", _Broker())
    assert result["state"] == ledger.STATE_SUBMISSION_UNKNOWN


def test_reconcile_by_client_order_id_lookup_failure_stays_unknown():
    conn = state_db.open_db()
    reservation_id = ledger.reserve(conn, "AAPL", 1000.0, "coid-7")
    ledger.mark_submission_unknown(conn, reservation_id)

    class _Broker:
        def get_order_by_client_order_id(self, coid):
            raise ConnectionError("network down")

    result = ledger.reconcile_by_client_order_id(conn, "coid-7", _Broker())
    assert result["state"] == ledger.STATE_SUBMISSION_UNKNOWN


# ---------------------------------------------------------------------------
# Concurrency: same-balance concurrent orders must not double-spend.
# ---------------------------------------------------------------------------

def test_concurrent_double_entry_atomic_reservation_blocks_second():
    import threading

    state_db.open_db()  # pre-warm schema before threads
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
# broker's network session.
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
    broker = AlpacaBroker(
        config=BrokerConfig(trading_mode="live", enable_real_trading=False, live_dry_run=True,
                             api_key="key", secret_key="secret"),
        session=_NetworkForbiddenSession(),
    )
    response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-3", side="sell")
    assert response.status_code != 423 or response.data.get("blocked_reason") is None


def test_paper_mode_buy_unaffected_by_missing_live_entry_context():
    session = _NetworkForbiddenSession()
    broker = AlpacaBroker(
        config=BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret"),
        session=session,
    )
    with pytest.raises(RuntimeError, match="Credential revalidation failed"):
        pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-4", side="buy")


def test_broker_double_without_config_attribute_unaffected():
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
# CODEX-029: symbol-identity lock.
# ---------------------------------------------------------------------------

def test_context_symbol_mismatched_with_order_symbol_blocked():
    with pytest.raises(LiveOrderBlockedError, match="does not match"):
        _sized(_ctx(symbol="AAPL"), order_symbol="TSLA")


def test_case_mutation_between_context_and_order_symbol_blocked():
    with pytest.raises(LiveOrderBlockedError, match="does not match"):
        _sized(_ctx(symbol="AAPL"), order_symbol="aapl")


def test_whitespace_mutation_between_context_and_order_symbol_blocked():
    with pytest.raises(LiveOrderBlockedError, match="does not match"):
        _sized(_ctx(symbol="AAPL"), order_symbol=" AAPL")


def test_empty_order_symbol_blocked():
    with pytest.raises(LiveOrderBlockedError, match="empty"):
        _sized(_ctx(symbol="AAPL"), order_symbol="")


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
    """A fully valid, matching live entry passes the gate and proceeds
    past it -- proven here by reaching broker_config.py's own
    pre-existing "real live trading is disabled" hard block instead of a
    423 from the live-entry-context gate."""
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
    broker = _live_broker()
    ctx = _ctx(symbol="AAPL")
    response = pso.submit_order("TSLA", qty=1, broker=broker, client_order_id="c-6", side="buy",
                                 live_entry_context=ctx)
    assert response.status_code == 423
    assert "does not match" in response.data["blocked_reason"]
    assert broker.session.requests == []


def test_whole_balance_exceeding_order_blocked_zero_network_calls():
    """A caller-inflated context (e.g. cash_usage_percent=100 but a real
    balance too small for even one share) is blocked before the broker is
    ever called."""
    broker = _live_broker()
    ctx = _ctx(symbol="AAPL", available_cash_krw=1, cash_usage_percent=100,
               expected_fill_price_usd=1000.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    response = broker.submit_order("AAPL", qty=1, side="buy", client_order_id="c-direct-8",
                                    live_entry_context=ctx)
    assert response.status_code == 423
    assert broker.session.requests == []


# ---------------------------------------------------------------------------
# CODEX-034: ambiguous broker failure (timeout/connection loss) marks the
# reservation SUBMISSION_UNKNOWN, not RELEASED, and it keeps counting
# toward allocatable cash -- so a naive retry of a similarly-sized order
# is blocked (HTTP 0 additional calls) instead of reaching the broker a
# second time.
# ---------------------------------------------------------------------------

class _TimeoutThenOkSession:
    """First .request() call raises a requests.exceptions.Timeout (no
    response ever received -- the canonical "ambiguous" failure). A
    second call (if ever reached) would raise AssertionError, since a
    correctly-blocked retry must never get that far."""

    def __init__(self):
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            import requests
            raise requests.exceptions.Timeout("simulated broker response loss")
        raise AssertionError("retry should have been blocked by the still-active reservation, not reach the broker")


def test_ambiguous_broker_failure_marks_submission_unknown_not_released(monkeypatch):
    from broker.broker_config import BrokerConfig as _BC

    session = _TimeoutThenOkSession()
    broker = AlpacaBroker(
        config=_BC(trading_mode="live", enable_real_trading=True, live_dry_run=False,
                   api_key="key", secret_key="secret"),
        session=session,
    )
    # bypass the "real live trading is disabled" hard pre-live block so we
    # can reach the actual network call path in this isolated test. Frozen
    # dataclass instances reject attribute assignment, but a class-level
    # monkeypatch (auto-restored) still applies to every instance -- this
    # also covers validate_order_allowed_now()'s own fresh
    # BrokerConfig.from_env() instance, since it's the same class.
    monkeypatch.setattr(_BC, "validate_order_allowed", lambda self: True)
    monkeypatch.setattr(_BC, "validate_for_request", lambda self: None)
    # _validate_current_credentials_match_captured() re-reads
    # ALPACA_API_KEY/ALPACA_SECRET_KEY from the environment and requires
    # them to match the config's captured "key"/"secret" -- set them so
    # that pre-network gate doesn't itself raise (a real, non-ambiguous
    # failure) before ever reaching the simulated network timeout.
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("TRADING_MODE", "live")

    ctx = _ctx(symbol="AAPL", available_cash_krw=27_000, cash_usage_percent=100,
               expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    with pytest.raises(Exception):
        broker.submit_order("AAPL", qty=1, side="buy", client_order_id=None, live_entry_context=ctx)

    conn = state_db.open_db()
    rows = conn.execute("SELECT state FROM live_entry_reservations").fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == ledger.STATE_SUBMISSION_UNKNOWN  # not RELEASED

    # Retry with a similarly-sized order: the SUBMISSION_UNKNOWN
    # reservation still consumes the full 27,000 KRW allocatable
    # cash, so this must be blocked BEFORE any second broker call.
    ctx_retry = _ctx(symbol="AAPL", available_cash_krw=27_000, cash_usage_percent=100,
                      expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    response = broker.submit_order("AAPL", qty=1, side="buy", client_order_id=None,
                                    live_entry_context=ctx_retry)
    assert response.status_code == 423
    assert session.calls == 1  # exactly one broker submit call total, never a second
