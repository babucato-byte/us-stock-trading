"""Unit tests for domain/ -- the broker-agnostic order models introduced
for the Alpaca-data-only / KIS-live-broker migration."""
from datetime import datetime, timedelta, timezone

import pytest

from domain.account_snapshot import AccountSnapshot, AccountSnapshotError
from domain.execution_event import ExecutionRecord, ExecutionRecordError
from domain.instrument import Instrument, InstrumentError, build_instrument, normalized_symbol
from domain.order_intent import OrderIntent, OrderIntentError
from domain.position import Position, PositionError
from domain.signal import Signal, SignalError, build_signal

NOW = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)


def _instrument(**overrides):
    kwargs = dict(
        symbol="AAPL", normalized_symbol="AAPL", alpaca_symbol="AAPL", kis_symbol="AAPL",
        exchange="NASDAQ", currency="USD", asset_type="us_equity", tradable=True,
        fractionable=False, leveraged=False, inverse=False, otc=False,
    )
    kwargs.update(overrides)
    return Instrument(**kwargs)


class TestInstrument:
    def test_valid_instrument(self):
        inst = _instrument()
        assert inst.is_order_eligible

    @pytest.mark.parametrize("field,value", [
        ("symbol", ""), ("exchange", "   "), ("currency", 123),
    ])
    def test_invalid_string_fields_rejected(self, field, value):
        with pytest.raises(InstrumentError):
            _instrument(**{field: value})

    @pytest.mark.parametrize("field", ["leveraged", "inverse", "otc"])
    def test_leveraged_inverse_otc_not_order_eligible(self, field):
        inst = _instrument(**{field: True})
        assert not inst.is_order_eligible

    def test_not_tradable_not_order_eligible(self):
        assert not _instrument(tradable=False).is_order_eligible

    def test_normalized_symbol_strips_and_uppercases(self):
        assert normalized_symbol("  aapl ") == "AAPL"

    def test_normalized_symbol_rejects_empty(self):
        with pytest.raises(InstrumentError):
            normalized_symbol("   ")

    def test_build_instrument_defaults_kis_symbol_to_normalized(self):
        inst = build_instrument("aapl", exchange="NASDAQ")
        assert inst.alpaca_symbol == "AAPL"
        assert inst.kis_symbol == "AAPL"


def _signal(**overrides):
    kwargs = dict(
        signal_id="sig-1", strategy_id="strat-1", strategy_version="v1",
        config_version="cfg-1", code_commit="abc123", symbol="AAPL", exchange="NASDAQ",
        created_at=NOW, expires_at=NOW + timedelta(minutes=5), signal_price=100.0,
        score=80.0, entry_reason="breakout",
    )
    kwargs.update(overrides)
    return Signal(**kwargs)


class TestSignal:
    def test_valid_signal(self):
        assert not _signal().is_expired(now=NOW)

    def test_expiry_enforced(self):
        sig = _signal()
        assert sig.is_expired(now=NOW + timedelta(minutes=10))

    def test_expires_at_must_be_after_created_at(self):
        with pytest.raises(SignalError):
            _signal(expires_at=NOW - timedelta(seconds=1))

    def test_naive_datetime_rejected(self):
        with pytest.raises(SignalError):
            _signal(created_at=datetime(2026, 7, 29))

    @pytest.mark.parametrize("price", [0, -1.0, float("nan"), float("inf")])
    def test_invalid_signal_price_rejected(self, price):
        with pytest.raises(SignalError):
            _signal(signal_price=price)

    def test_stop_price_optional(self):
        assert _signal(stop_price=None).stop_price is None

    def test_build_signal_requires_explicit_validity(self):
        sig = build_signal(
            strategy_id="s", strategy_version="v1", config_version="c1",
            code_commit="abc", symbol="AAPL", exchange="NASDAQ", signal_price=10.0,
            score=1.0, entry_reason="x", valid_for_seconds=60, now=NOW,
        )
        assert sig.expires_at == NOW + timedelta(seconds=60)

    def test_build_signal_rejects_non_positive_validity(self):
        with pytest.raises(SignalError):
            build_signal(
                strategy_id="s", strategy_version="v1", config_version="c1",
                code_commit="abc", symbol="AAPL", exchange="NASDAQ", signal_price=10.0,
                score=1.0, entry_reason="x", valid_for_seconds=0, now=NOW,
            )


def _order_intent(**overrides):
    kwargs = dict(
        internal_order_id="ord-1", signal_id="sig-1", strategy_id="strat-1",
        symbol="AAPL", exchange="NASDAQ", side="buy", quantity=1,
        order_type="limit", limit_price=100.0, stop_price=95.0, target_price=110.0,
        created_at=NOW,
    )
    kwargs.update(overrides)
    return OrderIntent(**kwargs)


class TestOrderIntent:
    def test_valid_order_intent(self):
        assert _order_intent().quantity == 1

    def test_market_order_type_rejected(self):
        with pytest.raises(OrderIntentError):
            _order_intent(order_type="market")

    @pytest.mark.parametrize("qty", [0, -1, 1.5, True])
    def test_non_positive_or_non_int_quantity_rejected(self, qty):
        with pytest.raises(OrderIntentError):
            _order_intent(quantity=qty)

    def test_invalid_side_rejected(self):
        with pytest.raises(OrderIntentError):
            _order_intent(side="short")

    def test_naive_created_at_rejected(self):
        with pytest.raises(OrderIntentError):
            _order_intent(created_at=datetime(2026, 7, 29))


def _execution_record(**overrides):
    kwargs = dict(
        internal_order_id="ord-1", broker="kis", broker_order_id="kis-123",
        requested_quantity=1, requested_price=100.0, filled_quantity=0.0,
        average_fill_price=None, status="ACCEPTED", submitted_at=NOW, updated_at=NOW,
    )
    kwargs.update(overrides)
    return ExecutionRecord(**kwargs)


class TestExecutionRecord:
    def test_valid_record(self):
        rec = _execution_record()
        assert not rec.is_terminal

    def test_filled_is_terminal(self):
        rec = _execution_record(status="FILLED", filled_quantity=1.0, average_fill_price=100.0)
        assert rec.is_terminal

    def test_unknown_is_not_terminal(self):
        assert not _execution_record(status="UNKNOWN").is_terminal

    def test_invalid_status_rejected(self):
        with pytest.raises(ExecutionRecordError):
            _execution_record(status="DONE")

    def test_overfill_rejected(self):
        with pytest.raises(ExecutionRecordError):
            _execution_record(filled_quantity=2.0, requested_quantity=1)


class TestPosition:
    def test_valid_position(self):
        pos = Position(symbol="AAPL", quantity=0, average_fill_price=0.0,
                        unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis")
        assert pos.is_flat

    def test_negative_quantity_rejected(self):
        with pytest.raises(PositionError):
            Position(symbol="AAPL", quantity=-1, average_fill_price=100.0,
                      unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis")


class TestAccountSnapshot:
    def test_valid_snapshot(self):
        snap = AccountSnapshot(
            krw_cash=0.0, usd_cash=1000.0, usd_orderable_cash=1000.0,
            usd_reserved_in_open_orders=200.0, as_of=NOW, source="kis", account_id="acct-1",
        )
        assert snap.usd_available_for_new_order == 800.0

    def test_reserved_never_pushes_available_below_zero(self):
        snap = AccountSnapshot(
            krw_cash=0.0, usd_cash=100.0, usd_orderable_cash=100.0,
            usd_reserved_in_open_orders=500.0, as_of=NOW, source="kis", account_id="acct-1",
        )
        assert snap.usd_available_for_new_order == 0.0

    def test_negative_cash_rejected(self):
        with pytest.raises(AccountSnapshotError):
            AccountSnapshot(
                krw_cash=-1.0, usd_cash=100.0, usd_orderable_cash=100.0,
                usd_reserved_in_open_orders=0.0, as_of=NOW, source="kis", account_id="acct-1",
            )

    def test_staleness(self):
        snap = AccountSnapshot(
            krw_cash=0.0, usd_cash=100.0, usd_orderable_cash=100.0,
            usd_reserved_in_open_orders=0.0, as_of=NOW, source="kis", account_id="acct-1",
        )
        assert not snap.is_stale(max_age_seconds=60, now=NOW + timedelta(seconds=30))
        assert snap.is_stale(max_age_seconds=60, now=NOW + timedelta(seconds=90))
