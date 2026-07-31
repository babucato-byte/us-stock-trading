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
from live_readiness.account_cash import TRUSTED_CASH_USAGE_PERCENT_CEILING, AccountCashSnapshot
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

def test_requesting_100_percent_is_capped_at_trusted_ceiling():
    # CODEX-036: cash_usage_percent is capped by TRUSTED_CASH_USAGE_PERCENT_CEILING
    # (50) regardless of what the caller requests -- 100% must NOT use
    # the full 30,000 balance, only the trusted ceiling's 50% (15,000).
    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=15.0, stop_price_usd=None)
    qty = _sized(ctx)
    assert qty * 15.0 * 1_000.0 <= 15_000
    assert qty * 15.0 * 1_000.0 > 14_000  # close to the trusted 50% (15,000) ceiling, not 30,000


def test_percent_below_trusted_ceiling_uses_requested_percent():
    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=40, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=12.0, stop_price_usd=None)
    qty = _sized(ctx)
    assert qty * 12.0 * 1_000.0 <= 12_000
    assert qty * 12.0 * 1_000.0 > 11_000  # close to the requested 40% (12,000), below the 50% ceiling


# ---------------------------------------------------------------------------
# CODEX-036: available cash and cash_usage_percent must be capped by an
# authoritative source, never taken at face value from the caller.
# ---------------------------------------------------------------------------

def _account_snapshot(cash_krw, *, as_of=None):
    return AccountCashSnapshot(cash_krw=cash_krw, as_of=as_of or NOW, source="broker_account_endpoint")


def test_codex036_repro_inflated_cash_capped_by_real_account_snapshot():
    # Codex's exact reproduction: a real 30,000 KRW account, caller
    # declares 3,000,000/100% -- must NOT approve anywhere close to the
    # ~2,997,000 KRW order the old (uncapped) design allowed.
    ctx = _ctx(available_cash_krw=3_000_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=1.0, fractional_shares_allowed=True,
               min_order_amount_usd=0.01, stop_price_usd=None)
    approval = validate_and_size_live_entry(
        ctx, ctx.symbol, account_cash_snapshot=_account_snapshot(30_000),
    )
    conn = state_db.open_db()
    row = ledger.get_by_id(conn, approval.reservation_id)
    # capped at the trusted ceiling (90%) of the REAL 30,000 balance, not
    # the caller's declared 3,000,000 -- nowhere near 2,997,000 KRW.
    assert row["notional_krw"] <= 27_000


def test_account_snapshot_can_only_lower_never_raise_caller_cash():
    # The opposite direction: a real account with MORE cash than the
    # caller declared must NOT let the caller exceed their own declared
    # figure either -- min() cuts both ways.
    ctx = _ctx(available_cash_krw=1_000, cash_usage_percent=50, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=0.10, fractional_shares_allowed=True,
               min_order_amount_usd=0.01, stop_price_usd=None)
    approval = validate_and_size_live_entry(
        ctx, ctx.symbol, account_cash_snapshot=_account_snapshot(10_000_000),
    )
    conn = state_db.open_db()
    row = ledger.get_by_id(conn, approval.reservation_id)
    assert row["notional_krw"] <= 500  # 1,000 * 50%, not 10,000,000-derived


def test_account_snapshot_wrong_type_blocked():
    ctx = _ctx()
    with pytest.raises(LiveOrderBlockedError, match="AccountCashSnapshot"):
        validate_and_size_live_entry(ctx, ctx.symbol, account_cash_snapshot=3_000_000)


def test_account_snapshot_stale_blocked():
    stale = NOW - timedelta(hours=1)
    ctx = _ctx(max_cash_age_seconds=300)
    with pytest.raises(LiveOrderBlockedError, match="stale"):
        validate_and_size_live_entry(ctx, ctx.symbol, account_cash_snapshot=_account_snapshot(30_000, as_of=stale))


def test_no_snapshot_supplied_uses_caller_cash_unchanged():
    # Backward compatibility: omitting account_cash_snapshot entirely
    # (order_gateway.py's own unit tests, or a caller that hasn't wired
    # snapshot-fetching yet) leaves ctx.available_cash_krw as the only
    # cash input, exactly like before CODEX-036.
    ctx = _ctx(available_cash_krw=30_000, cash_usage_percent=40, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=12.0, stop_price_usd=None)
    qty = _sized(ctx)
    assert qty * 12.0 * 1_000.0 > 11_000


def test_no_fixed_30000_ceiling_larger_balance_allows_larger_order():
    """CODEX-034's explicit requirement: 30,000 KRW must not be baked in
    as a permanent system ceiling -- a larger real balance must allow a
    correspondingly larger order. Uses cash_usage_percent=50 (at, not
    above, the CODEX-036 trusted ceiling) so this test isolates the
    balance-scaling behavior from the ceiling-capping behavior."""
    ctx = _ctx(available_cash_krw=300_000, cash_usage_percent=50, fx_rate_krw_per_usd=1_000.0,
               expected_fill_price_usd=100.0, stop_price_usd=None)
    qty = _sized(ctx)
    notional = qty * 100.0 * 1_000.0
    assert notional > 30_000  # would have been impossible under the old fixed ceiling
    assert notional <= 300_000 * 0.5


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
    # CODEX-036: cash_usage_percent=100 is capped to the trusted 50%
    # ceiling, so available_cash_krw is doubled here to keep the same
    # 30,000 KRW effective allocatable cash this test's math depends on.
    ctx = _ctx(available_cash_krw=60_000, cash_usage_percent=50, expected_fill_price_usd=1_000.0,
               fx_rate_krw_per_usd=1_000.0, stop_price_usd=None, max_position_count=5)
    # 30,000 - 20,000 pending = 10,000 available; 1 share costs 1,000,000 KRW -> unaffordable.
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(ctx)


def test_unknown_submission_reservation_deducted(monkeypatch):
    monkeypatch.setattr("live_readiness.order_gateway.MAX_CONCURRENT_LIVE_POSITIONS", 5)
    conn = state_db.open_db()
    _seed_reservation(conn, notional_krw=20_000, state=ledger.STATE_SUBMISSION_UNKNOWN)
    ctx = _ctx(available_cash_krw=60_000, cash_usage_percent=50, expected_fill_price_usd=1_000.0,
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

    ctx = _ctx(available_cash_krw=60_000, cash_usage_percent=50, expected_fill_price_usd=1_000.0,
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

# ---------------------------------------------------------------------------
# CODEX-037: NaN/Infinity/bool/string/negative/zero optional caps must be
# rejected up front (fail-closed), never silently ignored ("fail-open").
# ---------------------------------------------------------------------------

_INVALID_OPTIONAL_CAP_VALUES = [float("nan"), float("inf"), float("-inf"), True, False, "10", -1, 0]


@pytest.mark.parametrize("bad_value", _INVALID_OPTIONAL_CAP_VALUES)
def test_invalid_max_order_notional_krw_blocked(bad_value):
    ctx = _ctx(max_order_notional_krw=bad_value)
    with pytest.raises(LiveOrderBlockedError, match="max_order_notional_krw"):
        _sized(ctx)


@pytest.mark.parametrize("bad_value", _INVALID_OPTIONAL_CAP_VALUES)
def test_invalid_max_daily_loss_krw_blocked(bad_value):
    ctx = _ctx(max_daily_loss_krw=bad_value)
    with pytest.raises(LiveOrderBlockedError, match="max_daily_loss_krw"):
        _sized(ctx)


@pytest.mark.parametrize("bad_value", _INVALID_OPTIONAL_CAP_VALUES)
def test_invalid_max_risk_per_trade_krw_blocked(bad_value):
    ctx = _ctx(max_risk_per_trade_krw=bad_value)
    with pytest.raises(LiveOrderBlockedError, match="max_risk_per_trade_krw"):
        _sized(ctx)


@pytest.mark.parametrize("bad_value", _INVALID_OPTIONAL_CAP_VALUES)
def test_invalid_strategy_max_quantity_blocked(bad_value):
    ctx = _ctx(strategy_max_quantity=bad_value)
    with pytest.raises(LiveOrderBlockedError, match="strategy_max_quantity"):
        _sized(ctx)


@pytest.mark.parametrize("bad_value", _INVALID_OPTIONAL_CAP_VALUES)
def test_invalid_stop_price_usd_blocked(bad_value):
    ctx = _ctx(stop_price_usd=bad_value)
    with pytest.raises(LiveOrderBlockedError, match="stop_price_usd"):
        _sized(ctx)


def test_codex037_repro_fractional_nan_risk_cap_blocked_zero_reservations():
    # Codex's exact reproduction: fractional entry + NaN risk cap used to
    # silently approve a ~$3 order (qty 0.2222...) instead of blocking.
    conn = state_db.open_db()
    ctx = _ctx(available_cash_krw=13_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=9.0, fractional_shares_allowed=True,
               max_risk_per_trade_krw=float("nan"))
    with pytest.raises(LiveOrderBlockedError, match="max_risk_per_trade_krw"):
        _sized(ctx)
    assert conn.execute("SELECT COUNT(*) AS n FROM live_entry_reservations").fetchone()["n"] == 0


def test_codex037_repro_fractional_nan_strategy_cap_blocked_zero_reservations():
    conn = state_db.open_db()
    ctx = _ctx(available_cash_krw=13_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=None, fractional_shares_allowed=True,
               strategy_max_quantity=float("nan"))
    with pytest.raises(LiveOrderBlockedError, match="strategy_max_quantity"):
        _sized(ctx)
    assert conn.execute("SELECT COUNT(*) AS n FROM live_entry_reservations").fetchone()["n"] == 0


def test_codex037_repro_whole_share_nan_order_notional_cap_blocked_zero_reservations():
    conn = state_db.open_db()
    ctx = _ctx(available_cash_krw=40_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=None, fractional_shares_allowed=False,
               max_order_notional_krw=float("nan"))
    with pytest.raises(LiveOrderBlockedError, match="max_order_notional_krw"):
        _sized(ctx)
    assert conn.execute("SELECT COUNT(*) AS n FROM live_entry_reservations").fetchone()["n"] == 0


def test_risk_cap_resizes_quantity_down_instead_of_rejecting():
    # Balance affords 3 shares ($30 budget / $10 = 3); a tight per-trade
    # risk cap should shrink the quantity, not reject the whole order.
    ctx = _ctx(available_cash_krw=40_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=9.0,  # $1 risk/share
               max_risk_per_trade_krw=1_350.0)  # exactly 1 share's worth of risk
    qty = _sized(ctx)
    assert qty == 1


def test_risk_cap_looser_than_balance_does_not_reduce_quantity():
    # CODEX-036: available_cash_krw doubled to 27,000 so the trusted 50%
    # ceiling still leaves 13,500 KRW effective budget (1 share at $10 *
    # 1,350 KRW/$), matching this test's original intent.
    ctx = _ctx(available_cash_krw=27_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
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
    ctx = _ctx(available_cash_krw=27_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=None, strategy_max_quantity=100)
    qty = _sized(ctx)
    assert qty == 1


def test_strategy_max_quantity_zero_blocked():
    # CODEX-037: strategy_max_quantity=0 is now caught by the upfront
    # "must be positive" optional-cap validation, before sizing even runs.
    ctx = _ctx(available_cash_krw=40_500, cash_usage_percent=100, fx_rate_krw_per_usd=1_350.0,
               expected_fill_price_usd=10.0, stop_price_usd=None, strategy_max_quantity=0)
    with pytest.raises(LiveOrderBlockedError, match="strategy_max_quantity must be positive"):
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
    # KIS migration: this file tests the legacy CODEX-026+ Alpaca-KRW
    # live-entry-context gate specifically (allow-list/FX/symbol-match/
    # budget checks), not the new Alpaca-order-disabled gate --
    # alpaca_order_enabled=True keeps these tests exercising exactly
    # what they were designed to test.
    return AlpacaBroker(
        config=BrokerConfig(trading_mode="live", enable_real_trading=True, live_dry_run=False,
                             api_key="key", secret_key="secret", alpaca_order_enabled=True,
                             execution_broker="alpaca"),
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
                             api_key="key", secret_key="secret", alpaca_order_enabled=True,
                             execution_broker="alpaca"),
        session=_NetworkForbiddenSession(),
    )
    response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-3", side="sell")
    assert response.status_code != 423 or response.data.get("blocked_reason") is None


def test_paper_mode_buy_unaffected_by_missing_live_entry_context():
    session = _NetworkForbiddenSession()
    broker = AlpacaBroker(
        config=BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret",
                             alpaca_paper_order_enabled=True, execution_broker="alpaca"),
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
        self.order_calls = 0

    def request(self, *args, **kwargs):
        self.order_calls += 1
        if self.order_calls == 1:
            import requests
            raise requests.exceptions.Timeout("simulated broker response loss")
        raise AssertionError("retry should have been blocked by the still-active reservation, not reach the broker")


class _HTTPErrorThenBlockedSession:
    """First .request() call raises requests.exceptions.HTTPError with a
    REAL requests.Response attached (status_code + JSON body), simulating
    an upstream/gateway/rejection response that WAS received. A second
    call (if ever reached) fails the test, since a correctly-blocked
    retry must never get that far."""

    def __init__(self, status_code, body=None, *, malformed_body=False):
        self.order_calls = 0
        self.status_code = status_code
        self.body = body if body is not None else {"message": "simulated response"}
        self.malformed_body = malformed_body

    def request(self, *args, **kwargs):
        self.order_calls += 1
        if self.order_calls == 1:
            import json
            import requests
            resp = requests.Response()
            resp.status_code = self.status_code
            resp._content = b"not json {{{" if self.malformed_body else json.dumps(self.body).encode()
            raise requests.exceptions.HTTPError(response=resp)
        raise AssertionError("retry should have been blocked, not reach the broker")


def _live_broker_for_fault_injection(session, monkeypatch):
    from broker.broker_config import BrokerConfig as _BC

    broker = AlpacaBroker(
        config=_BC(trading_mode="live", enable_real_trading=True, live_dry_run=False,
                   api_key="key", secret_key="secret", alpaca_order_enabled=True,
                   execution_broker="alpaca"),
        session=session,
    )
    monkeypatch.setattr(_BC, "validate_order_allowed", lambda self: True)
    monkeypatch.setattr(_BC, "validate_for_request", lambda self: None)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("TRADING_MODE", "live")
    return broker


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 502, 503, 504])
def test_ambiguous_http_status_marks_submission_unknown_not_released(monkeypatch, status_code):
    session = _HTTPErrorThenBlockedSession(status_code)
    broker = _live_broker_for_fault_injection(session, monkeypatch)

    ctx = _ctx(symbol="AAPL", available_cash_krw=27_000, cash_usage_percent=100,
               expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    with pytest.raises(Exception):
        broker.submit_order("AAPL", qty=1, side="buy", client_order_id=None, live_entry_context=ctx)

    conn = state_db.open_db()
    rows = conn.execute("SELECT state FROM live_entry_reservations").fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == ledger.STATE_SUBMISSION_UNKNOWN  # not RELEASED

    ctx_retry = _ctx(symbol="AAPL", available_cash_krw=27_000, cash_usage_percent=100,
                      expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    response = broker.submit_order("AAPL", qty=1, side="buy", client_order_id=None,
                                    live_entry_context=ctx_retry)
    assert response.status_code == 423
    assert session.order_calls == 1  # exactly one broker submit call total, never a second


def test_ambiguous_unrecognized_http_status_marks_submission_unknown(monkeypatch):
    # A status code this classifier has never seen before must default to
    # ambiguous (fail-closed allowlist), not be silently trusted.
    session = _HTTPErrorThenBlockedSession(418)
    broker = _live_broker_for_fault_injection(session, monkeypatch)
    ctx = _ctx(symbol="AAPL", available_cash_krw=27_000, cash_usage_percent=100,
               expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    with pytest.raises(Exception):
        broker.submit_order("AAPL", qty=1, side="buy", client_order_id=None, live_entry_context=ctx)
    conn = state_db.open_db()
    rows = conn.execute("SELECT state FROM live_entry_reservations").fetchall()
    assert rows[0]["state"] == ledger.STATE_SUBMISSION_UNKNOWN


def test_definitive_status_with_unparseable_body_stays_ambiguous(monkeypatch):
    # 422 is on the definitive allowlist, but a body that doesn't parse as
    # JSON can't be confirmed as Alpaca's actual rejection contract.
    session = _HTTPErrorThenBlockedSession(422, malformed_body=True)
    broker = _live_broker_for_fault_injection(session, monkeypatch)
    ctx = _ctx(symbol="AAPL", available_cash_krw=27_000, cash_usage_percent=100,
               expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    with pytest.raises(Exception):
        broker.submit_order("AAPL", qty=1, side="buy", client_order_id=None, live_entry_context=ctx)
    conn = state_db.open_db()
    rows = conn.execute("SELECT state FROM live_entry_reservations").fetchall()
    assert rows[0]["state"] == ledger.STATE_SUBMISSION_UNKNOWN


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 410, 422])
def test_definitive_rejection_status_with_json_body_releases(monkeypatch, status_code):
    session = _HTTPErrorThenBlockedSession(status_code, body={"code": 40010001, "message": "rejected"})
    broker = _live_broker_for_fault_injection(session, monkeypatch)
    ctx = _ctx(symbol="AAPL", available_cash_krw=27_000, cash_usage_percent=100,
               expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    with pytest.raises(Exception):
        broker.submit_order("AAPL", qty=1, side="buy", client_order_id=None, live_entry_context=ctx)
    conn = state_db.open_db()
    rows = conn.execute("SELECT state FROM live_entry_reservations").fetchall()
    assert rows[0]["state"] == ledger.STATE_RELEASED  # a genuine definitive rejection frees the cash

    # Because it was safely released, a fresh retry MAY reach the broker
    # again (this is correct -- the first attempt is confirmed dead).
    ctx_retry = _ctx(symbol="AAPL", available_cash_krw=27_000, cash_usage_percent=100,
                      expected_fill_price_usd=10.0, fx_rate_krw_per_usd=1_350.0, stop_price_usd=None)
    with pytest.raises(Exception):
        broker.submit_order("AAPL", qty=1, side="buy", client_order_id=None, live_entry_context=ctx_retry)
    assert session.order_calls == 2


def test_ambiguous_broker_failure_marks_submission_unknown_not_released(monkeypatch):
    from broker.broker_config import BrokerConfig as _BC

    session = _TimeoutThenOkSession()
    broker = AlpacaBroker(
        config=_BC(trading_mode="live", enable_real_trading=True, live_dry_run=False,
                   api_key="key", secret_key="secret", alpaca_order_enabled=True,
                   execution_broker="alpaca"),
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
    assert session.order_calls == 1  # exactly one broker submit call total, never a second
