from datetime import datetime, timedelta, timezone

import pytest

from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution.order_gate import (
    BuyGateContext,
    CancelGateContext,
    OrderGateBlockedError,
    SellGateContext,
    evaluate_buy_gate,
    evaluate_cancel_gate,
    evaluate_sell_gate,
)

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


def _instrument(**overrides):
    kwargs = dict(exchange="NASDAQ")
    kwargs.update(overrides)
    return build_instrument("AAPL", **kwargs)


def _signal(**overrides):
    kwargs = dict(
        strategy_id="strat-1", strategy_version="v1", config_version="cfg-1",
        code_commit="abc123", symbol="AAPL", exchange="NASDAQ", signal_price=100.0,
        score=90.0, entry_reason="breakout", valid_for_seconds=300, now=NOW,
    )
    kwargs.update(overrides)
    return build_signal(**kwargs)


def _order_intent(**overrides):
    kwargs = dict(
        internal_order_id="ord-1", signal_id="sig-1", strategy_id="strat-1",
        symbol="AAPL", exchange="NASDAQ", side="buy", quantity=1, order_type="limit",
        limit_price=100.0, stop_price=95.0, target_price=110.0, created_at=NOW,
    )
    kwargs.update(overrides)
    return OrderIntent(**kwargs)


def _buy_ctx(**overrides):
    kwargs = dict(
        execution_broker="kis", live_order_enabled=True, entry_disabled=False,
        validated_commit="c1", deployed_commit="c1", kis_account_no="12345678",
        allowed_account_no="12345678", order_intent=_order_intent(), instrument=_instrument(),
        signal=_signal(), is_regular_session=True, kis_price_usd=100.1,
        max_price_deviation_percent=0.30, usd_orderable_cash=1000.0,
        has_open_order_for_symbol=False, has_order_for_signal_id=False,
        allowed_symbols=frozenset({"AAPL"}), reconciliation_ok=True, has_unknown_order=False,
        now=NOW,
    )
    kwargs.update(overrides)
    return BuyGateContext(**kwargs)


class TestEvaluateBuyGate:
    def test_passes_valid_context(self):
        assert evaluate_buy_gate(_buy_ctx()) is True

    def test_non_kis_broker_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="execution broker"):
            evaluate_buy_gate(_buy_ctx(execution_broker="alpaca"))

    def test_live_order_disabled_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="live order flag"):
            evaluate_buy_gate(_buy_ctx(live_order_enabled=False))

    def test_entry_disabled_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="ENTRY_DISABLED"):
            evaluate_buy_gate(_buy_ctx(entry_disabled=True))

    def test_commit_mismatch_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="commit"):
            evaluate_buy_gate(_buy_ctx(validated_commit="c1", deployed_commit="c2"))

    def test_account_mismatch_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="allowed account"):
            evaluate_buy_gate(_buy_ctx(kis_account_no="99999999"))

    def test_non_integer_quantity_blocked(self):
        # OrderIntent itself already rejects non-int quantities at
        # construction (domain/order_intent.py) -- this test proves the
        # gate has its own independent belt-and-suspenders check via a
        # duck-typed object bypassing that construction-time validation.
        class _FakeIntent:
            quantity = 1.5
            order_type = "limit"
            symbol = "AAPL"
        with pytest.raises(OrderGateBlockedError, match="integer"):
            evaluate_buy_gate(_buy_ctx(order_intent=_FakeIntent()))

    def test_market_order_type_blocked(self):
        class _FakeIntent:
            quantity = 1
            order_type = "market"
            symbol = "AAPL"
        with pytest.raises(OrderGateBlockedError, match="limit orders"):
            evaluate_buy_gate(_buy_ctx(order_intent=_FakeIntent()))

    def test_outside_regular_session_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="regular trading session"):
            evaluate_buy_gate(_buy_ctx(is_regular_session=False))

    def test_expired_signal_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="expired"):
            evaluate_buy_gate(_buy_ctx(now=NOW + timedelta(minutes=10)))

    def test_invalid_kis_price_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="KIS price is invalid"):
            evaluate_buy_gate(_buy_ctx(kis_price_usd=-1.0))

    def test_price_deviation_exceeded_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="deviates"):
            evaluate_buy_gate(_buy_ctx(kis_price_usd=101.0))  # 1% > 0.30% limit

    def test_price_deviation_within_limit_passes(self):
        assert evaluate_buy_gate(_buy_ctx(kis_price_usd=100.29)) is True

    def test_insufficient_cash_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="insufficient KIS orderable cash"):
            evaluate_buy_gate(_buy_ctx(usd_orderable_cash=50.0))

    def test_existing_open_order_for_symbol_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="open .* order already exists"):
            evaluate_buy_gate(_buy_ctx(has_open_order_for_symbol=True))

    def test_existing_order_for_signal_id_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="signal_id"):
            evaluate_buy_gate(_buy_ctx(has_order_for_signal_id=True))

    def test_symbol_not_in_allow_list_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="allowed-symbols"):
            evaluate_buy_gate(_buy_ctx(allowed_symbols=frozenset({"MSFT"})))

    def test_leveraged_instrument_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="order-eligible"):
            evaluate_buy_gate(_buy_ctx(instrument=_instrument(leveraged=True)))

    def test_inverse_instrument_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="order-eligible"):
            evaluate_buy_gate(_buy_ctx(instrument=_instrument(inverse=True)))

    def test_otc_instrument_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="order-eligible"):
            evaluate_buy_gate(_buy_ctx(instrument=_instrument(otc=True)))

    def test_reconciliation_not_ok_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="[Rr]econciliation"):
            evaluate_buy_gate(_buy_ctx(reconciliation_ok=False))

    def test_unknown_order_exists_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="UNKNOWN"):
            evaluate_buy_gate(_buy_ctx(has_unknown_order=True))


def _sell_ctx(**overrides):
    kwargs = dict(
        execution_broker="kis", live_order_enabled=True,
        order_intent=_order_intent(side="sell", quantity=1),
        instrument=_instrument(), kis_position_quantity=5, position_source="kis",
        has_existing_sell_order_for_symbol=False, reconciliation_ok=True, has_unknown_order=False,
    )
    kwargs.update(overrides)
    return SellGateContext(**kwargs)


class TestEvaluateSellGate:
    def test_passes_valid_context(self):
        assert evaluate_sell_gate(_sell_ctx()) is True

    def test_non_kis_broker_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="execution broker"):
            evaluate_sell_gate(_sell_ctx(execution_broker="alpaca"))

    def test_live_order_disabled_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="live order flag"):
            evaluate_sell_gate(_sell_ctx(live_order_enabled=False))

    def test_non_kis_position_source_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="position source"):
            evaluate_sell_gate(_sell_ctx(position_source="alpaca"))

    def test_zero_position_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="no KIS position"):
            evaluate_sell_gate(_sell_ctx(kis_position_quantity=0))

    def test_sell_quantity_exceeds_position_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="exceeds actual KIS position"):
            evaluate_sell_gate(_sell_ctx(kis_position_quantity=1, order_intent=_order_intent(side="sell", quantity=2)))

    def test_existing_sell_order_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="duplicate liquidation"):
            evaluate_sell_gate(_sell_ctx(has_existing_sell_order_for_symbol=True))

    def test_reconciliation_not_ok_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="[Rr]econciliation"):
            evaluate_sell_gate(_sell_ctx(reconciliation_ok=False))

    def test_unknown_order_exists_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="UNKNOWN"):
            evaluate_sell_gate(_sell_ctx(has_unknown_order=True))

    def test_sell_full_position_exactly_passes(self):
        assert evaluate_sell_gate(
            _sell_ctx(kis_position_quantity=1, order_intent=_order_intent(side="sell", quantity=1))
        ) is True


def _cancel_ctx(**overrides):
    kwargs = dict(
        execution_broker="kis", broker_order_id="kis-999", is_actually_open=True,
        kis_account_no="12345678", allowed_account_no="12345678", symbol="AAPL",
        has_cancel_already_in_flight=False,
    )
    kwargs.update(overrides)
    return CancelGateContext(**kwargs)


class TestEvaluateCancelGate:
    def test_passes_valid_context(self):
        assert evaluate_cancel_gate(_cancel_ctx()) is True

    def test_non_kis_broker_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="execution broker"):
            evaluate_cancel_gate(_cancel_ctx(execution_broker="alpaca"))

    def test_missing_broker_order_id_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="no broker_order_id"):
            evaluate_cancel_gate(_cancel_ctx(broker_order_id=None))

    def test_not_actually_open_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="not an actual open"):
            evaluate_cancel_gate(_cancel_ctx(is_actually_open=False))

    def test_account_mismatch_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="allowed account"):
            evaluate_cancel_gate(_cancel_ctx(kis_account_no="99999999"))

    def test_duplicate_cancel_in_flight_blocked(self):
        with pytest.raises(OrderGateBlockedError, match="already in flight"):
            evaluate_cancel_gate(_cancel_ctx(has_cancel_already_in_flight=True))
