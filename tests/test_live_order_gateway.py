"""CODEX-026: live-mode pre-trade gate tests.

Pure unit tests for live_readiness/order_gateway.py (no I/O), plus
integration tests confirming paper_strategy_order.submit_order() enforces
this gate only for side="buy" + live mode, and that a blocked order never
reaches the broker (HTTP/session call count assertions).
"""
from datetime import datetime, timedelta, timezone

import pytest

from live_readiness.order_gateway import LiveEntryContext, LiveOrderBlockedError, validate_and_size_live_entry

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


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
    as they did before that check was added."""
    return validate_and_size_live_entry(ctx, order_symbol if order_symbol is not None else ctx.symbol)


def test_valid_entry_returns_positive_quantity():
    qty = _sized(_ctx())
    assert qty > 0


def test_symbol_not_on_allow_list_blocked():
    with pytest.raises(LiveOrderBlockedError, match="allow-list"):
        _sized(_ctx(symbol="TSLA"))


def test_empty_allow_list_blocks_everything():
    with pytest.raises(LiveOrderBlockedError, match="allow-list"):
        _sized(_ctx(allow_list=[]))


def test_max_position_count_reached_blocked():
    with pytest.raises(LiveOrderBlockedError, match="concurrent positions"):
        _sized(_ctx(current_open_position_count=1, max_position_count=1))


def test_max_daily_entries_reached_blocked():
    with pytest.raises(LiveOrderBlockedError, match="daily entries"):
        _sized(_ctx(today_entry_count=2, max_daily_entries=2))


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
    """A fully valid, matching live entry passes the CODEX-026/029 gate and
    proceeds past it -- proven here by reaching broker_config.py's own
    pre-existing "real live trading is disabled" hard block (an earlier,
    unrelated safety gate this pre-live repository always enforces)
    instead of a 423 from the live-entry-context gate. It never reaches
    the network layer either way, but for a different, correct reason."""
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
    context/payload symbol mismatch is caught by the wrapper's own gate
    before ever reaching AlpacaBroker.submit_order() at all."""
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

    # A single share's notional (30,000.9 KRW) already exceeds a 30,000
    # KRW total budget by 1 KRW -- sizing must reject it, not round down
    # silently to "close enough."
    ctx_over = _ctx(
        symbol="AAPL", available_cash_krw=30_000, max_order_notional_krw=30_000,
        fx_rate_krw_per_usd=1_000.0, expected_fill_price_usd=30.0009, stop_price_usd=None,
    )
    with pytest.raises(LiveOrderBlockedError, match="sizing blocked"):
        _sized(ctx_over)
