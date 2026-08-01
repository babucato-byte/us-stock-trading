"""KISBrokerAdapter -- the bridge that lets `positions/lifecycle.py`'s
EXISTING, already-verified exit engine (`check_and_manage()`,
`check_invalidation()`, `record_fill()`) submit a real sell order
through the new KIS execution path, without that module (or
`paper_strategy_order.submit_order()`, which it calls internally)
having to know anything about KIS, `OrderIntent`, `order_gate`, or
`execution_engine`.

`positions/lifecycle.py::_execute_exit()` calls exactly:

    paper_strategy_order.submit_order(symbol, qty=exit_qty, broker=broker,
                                       client_order_id=client_order_id, side="sell")

which (for side="sell", confirmed by reading that function) reduces to
calling this adapter's own `submit_order` method below directly, with
the same (symbol, qty, side, client_order_id) keyword shape, and
expects back an object with `.status_code` (200/201 = accepted,
anything else = the position goes to MANUAL_REVIEW -- the EXISTING,
unmodified fail-closed behavior, not something this adapter invents)
and `.data` shaped so `positions/order_status.extract_order_info()` can
read `data["status"]`/`data["filled_qty"]`/`data["filled_avg_price"]`/
`data["id"]` using ALPACA'S OWN status vocabulary ("accepted", "filled",
"partially_filled", ...) -- `classify_broker_order_status()` only
recognizes those exact strings. This adapter translates KIS's
ExecutionRecord.status ("ACCEPTED"/"REJECTED"/...) into that vocabulary
so the existing, unmodified fill-classification logic keeps working
unchanged.

This adapter does NOT compute fill quantity/price synchronously --
KIS's order-submission response never includes fill data (a limit order
may sit unfilled for a while). `data["filled_qty"]`/`data["filled_avg_
price"]` are always None immediately after submission, matching a
freshly "accepted" order; the actual fill gets applied later via
`record_fill()`, driven by `kis_position_manager.py`'s reconciliation
sync against `KISBroker.get_fills()` -- exactly the same two-phase
(submit now, reconcile fill later) shape this codebase's `positions/
lifecycle.py` already uses for the Alpaca path (`reconcile_pending_
exit`/`recover_on_restart`).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union

import shadow_audit
from brokers.kis_broker import KISAmbiguousResponseError, KISBroker, KISBrokerError
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent, OrderIntentError
from execution import execution_engine, idempotency, order_gate
from execution.execution_engine import ExecutionEngineError
from market_data.kis_validation_provider import KISValidationProvider
from state_store import db as state_db


@dataclass
class BrokerResponse:
    """Deliberately NOT imported from broker/alpaca_client.py -- same
    field shape (status_code/text/data/dry_run), defined locally here so
    this adapter has zero import-time dependency on the Alpaca order-
    client module (tests/test_kis_negative_suite.py structurally
    verifies this). positions/order_status.extract_order_info() and
    positions/lifecycle.py only ever duck-type this object (`.status_
    code`, `.data`), never isinstance-check it against Alpaca's class."""
    status_code: int
    text: str
    data: Optional[Union[dict, list]] = None
    dry_run: bool = False


class KISBrokerAdapterError(Exception):
    """Raised for adapter-level misuse (e.g. side="buy", which this
    adapter deliberately does not support -- KIS buy entries go through
    kis_live_trading.py's own pipeline, never through this adapter, to
    avoid two independent code paths racing to create the same
    position's entry)."""


@dataclass(frozen=True)
class _FakeConfig:
    """`positions/lifecycle.py`/`paper_strategy_order.submit_order()`
    read `broker.config.is_live_mode` to decide whether to run the
    LEGACY Alpaca-KRW live-entry-context gate (CODEX-026 through
    CODEX-041). That gate is specific to the Alpaca/KRW-percent-of-
    balance pilot model and has no meaning for KIS (pure USD, no FX
    conversion, a completely different and already fully KIS-scoped
    gate -- execution/order_gate.py -- runs instead, inside this
    adapter's own submit_order()). Always reporting is_live_mode=False
    here is what correctly steers `paper_strategy_order.submit_order()`
    straight to this adapter's own `submit_order` method without
    resurrecting that unrelated legacy gate -- not a safety bypass, since order_gate.py's
    checks are still fully applied below, just under a different name."""
    is_live_mode: bool = False


class KISBrokerAdapter:
    def __init__(self, kis_broker: Optional[KISBroker] = None, *, allowed_symbols=None,
                 max_price_deviation_percent=0.30, is_regular_session_fn=None, now_fn=None):
        self.kis_broker = kis_broker or KISBroker()
        self.config = _FakeConfig()
        self.allowed_symbols = allowed_symbols or frozenset()
        self.max_price_deviation_percent = max_price_deviation_percent
        self._is_regular_session_fn = is_regular_session_fn or (lambda: True)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._kis_validation = KISValidationProvider(
            self.kis_broker, instrument_lookup=lambda s: build_instrument(s, exchange="NASDAQ"),
        )

    def _instrument(self, symbol):
        return build_instrument(symbol, exchange="NASDAQ")

    def _audit(self, run_id, event_type, result, *, symbol, internal_order_id=None,
                reason_code=None, detail=None, now):
        """CODEX-048: the SELL path records the same durable audit events
        the buy path does. Before this, `shadow_mode.persist()` was called
        only from kis_live_trading.py's buy cycle, so an exit that was
        gate-rejected, reconciliation-blocked or UNKNOWN-blocked left no
        Shadow record at all.

        Fails CLOSED, exactly like the buy path: an unpersistable audit
        event abandons the evaluation instead of continuing."""
        try:
            shadow_audit.record_event(
                shadow_run_id=run_id, event_type=event_type, result=result, symbol=symbol,
                side="sell", internal_order_id=internal_order_id, reason_code=reason_code,
                payload={"detail": detail} if detail else None, now=now,
            )
        except shadow_audit.ShadowAuditError as exc:
            shadow_audit.handle_audit_failure(
                exc, shadow_run_id=run_id, symbol=symbol, side="sell", stage=event_type,
            )

    def _blocked(self, run_id, event_type, *, symbol, internal_order_id, reason_code, detail,
                  status_code, text, now):
        """Records the specific block event AND exactly one terminal
        SHADOW_BLOCKED before returning the caller-facing response, so no
        sell evaluation can end without a final outcome event."""
        self._audit(run_id, event_type, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                    internal_order_id=internal_order_id, reason_code=reason_code,
                    detail=detail, now=now)
        self._audit(run_id, shadow_audit.SHADOW_BLOCKED, shadow_audit.RESULT_BLOCKED,
                    symbol=symbol, internal_order_id=internal_order_id,
                    reason_code=reason_code, now=now)
        return BrokerResponse(
            status_code=status_code, text=text, data={"blocked_reason": detail}, dry_run=False,
        )

    def submit_order(self, symbol, qty=1, *, side, order_type="market", time_in_force="day",
                      client_order_id=None, live_entry_context=None, account_cash_snapshot=None):
        if side != "sell":
            raise KISBrokerAdapterError(
                f"KISBrokerAdapter.submit_order() only supports side='sell' "
                f"(got {side!r}) -- KIS buy entries go through kis_live_trading.py's own pipeline"
            )
        current = self._now_fn()
        run_id = shadow_audit.new_run_id()
        instrument = self._instrument(symbol)
        internal_order_id = client_order_id or f"kissell-{symbol}-{uuid.uuid4().hex[:12]}"

        self._audit(run_id, shadow_audit.SIGNAL_RECEIVED, shadow_audit.RESULT_INFO, symbol=symbol,
                    internal_order_id=internal_order_id, reason_code="EXIT_REQUESTED", now=current)

        try:
            kis_price = self._kis_validation.get_price_quote(symbol).price_usd
        except Exception as exc:
            return self._blocked(
                run_id, shadow_audit.PRICE_DEVIATION_BLOCKED, symbol=symbol,
                internal_order_id=internal_order_id, reason_code="PRICE_UNAVAILABLE",
                detail=str(exc), status_code=422,
                text=f"KIS price re-check failed: {exc}", now=current,
            )

        try:
            order_intent = OrderIntent(
                internal_order_id=internal_order_id, signal_id=internal_order_id,
                strategy_id="POSITIONS_LIFECYCLE_EXIT", symbol=symbol, exchange=instrument.exchange,
                side="sell", quantity=int(qty), order_type="limit", limit_price=kis_price,
                stop_price=None, target_price=None, created_at=current,
            )
        except OrderIntentError as exc:
            return self._blocked(
                run_id, shadow_audit.INSTRUMENT_BLOCKED, symbol=symbol,
                internal_order_id=internal_order_id, reason_code="ORDER_INTENT_INVALID",
                detail=str(exc), status_code=422,
                text=f"order intent construction failed: {exc}", now=current,
            )

        try:
            kis_positions = self.kis_broker.get_positions()
        except KISBrokerError as exc:
            return self._blocked(
                run_id, shadow_audit.RECONCILIATION_BLOCKED, symbol=symbol,
                internal_order_id=internal_order_id, reason_code="POSITION_READ_FAILED",
                detail=str(exc), status_code=422,
                text=f"KIS position read failed: {exc}", now=current,
            )
        position_qty = next((p.quantity for p in kis_positions if p.symbol == symbol), 0)

        try:
            open_orders = self.kis_broker.get_open_orders()
        except KISBrokerError as exc:
            return self._blocked(
                run_id, shadow_audit.RECONCILIATION_BLOCKED, symbol=symbol,
                internal_order_id=internal_order_id, reason_code="OPEN_ORDER_READ_FAILED",
                detail=str(exc), status_code=422,
                text=f"KIS open-orders read failed: {exc}", now=current,
            )
        has_existing_sell_order = any(
            (o.get("pdno") or o.get("PDNO")) == symbol for o in open_orders
        )

        try:
            account_id = self.kis_broker.get_account_snapshot().account_id
        except KISBrokerError as exc:
            return self._blocked(
                run_id, shadow_audit.RECONCILIATION_BLOCKED, symbol=symbol,
                internal_order_id=internal_order_id, reason_code="ACCOUNT_READ_FAILED",
                detail=str(exc), status_code=422,
                text=f"KIS account read failed: {exc}", now=current,
            )

        conn = state_db.open_db()

        def _sell_ctx_builder(reconciliation):
            # CODEX-044: `reconciliation` is the snapshot the Execution
            # Engine itself built from live KIS reads immediately before
            # the gate -- this adapter never asserts reconciliation
            # status, and the sell path is held to exactly the same
            # policy as the buy path.
            return order_gate.SellGateContext(
                execution_broker="kis", live_order_enabled=True, order_intent=order_intent,
                instrument=instrument, kis_position_quantity=position_qty, position_source="kis",
                has_existing_sell_order_for_symbol=has_existing_sell_order,
                reconciliation=reconciliation, kis_account_no=account_id, now=current,
            )

        try:
            try:
                # CODEX-048: the engine records GATE_APPROVED and
                # EXECUTION_PLANNED for this run BEFORE it calls the
                # broker, so a crash during the transport call still
                # leaves the approval audited.
                result = execution_engine.submit_sell_order(
                    order_intent=order_intent, sell_gate_context_builder=_sell_ctx_builder,
                    conn=conn, broker=self.kis_broker, instrument=instrument,
                    account_id=account_id, now=current, audit_run_id=run_id,
                )
            except ExecutionEngineError as exc:
                return self._blocked(
                    run_id, shadow_audit.event_type_for_reason_code(exc.reason_code), symbol=symbol,
                    internal_order_id=internal_order_id, reason_code=exc.reason_code or "GATE",
                    detail=str(exc), status_code=423,
                    text=f"order gate blocked: {exc}", now=current,
                )
            except KISAmbiguousResponseError as exc:
                # Exactly one terminal event: SHADOW_ERROR.
                self._audit(run_id, shadow_audit.SHADOW_ERROR, shadow_audit.RESULT_ERROR,
                            symbol=symbol, internal_order_id=internal_order_id,
                            reason_code="AMBIGUOUS_RESPONSE", detail=str(exc), now=current)
                # Propagate -- positions/lifecycle.py's _execute_exit()
                # catches any Exception here and marks the exit intent
                # SUBMISSION_UNKNOWN, exactly the UNKNOWN-never-auto-
                # retried behavior spec §9 requires. Do not swallow this
                # into a BrokerResponse.
                raise
            except KISBrokerError as exc:
                return self._blocked(
                    run_id, shadow_audit.GATE_REJECTED, symbol=symbol,
                    internal_order_id=internal_order_id, reason_code="BROKER_REJECTED",
                    detail=str(exc), status_code=400,
                    text=f"KIS rejected the order: {exc}", now=current,
                )
        finally:
            conn.close()

        # Translate KIS's ExecutionRecord.status into Alpaca's own
        # status vocabulary so positions/order_status.py's EXISTING,
        # unmodified classify_broker_order_status() keeps working.
        alpaca_status = "accepted" if result.status == "ACCEPTED" else "rejected"
        approved = result.status == "ACCEPTED"
        audit_result = shadow_audit.RESULT_APPROVED if approved else shadow_audit.RESULT_BLOCKED
        # GATE_APPROVED/EXECUTION_PLANNED were recorded by the engine
        # before the transport call; only the outcome remains, and only
        # a rejection needs its own non-terminal event.
        if not approved:
            self._audit(run_id, shadow_audit.GATE_REJECTED, audit_result, symbol=symbol,
                        internal_order_id=internal_order_id, reason_code=result.status, now=current)
        self._audit(run_id, shadow_audit.terminal_event_for(audit_result), audit_result,
                    symbol=symbol, internal_order_id=internal_order_id,
                    reason_code=result.status, now=current)
        return BrokerResponse(
            status_code=200 if result.status == "ACCEPTED" else 400,
            text=f"KIS order {result.execution_record.broker_order_id} status={result.status}",
            data={
                "id": result.execution_record.broker_order_id,
                "status": alpaca_status,
                "filled_qty": None,
                "filled_avg_price": None,
            },
            dry_run=False,
        )

    def get_order_by_client_order_id(self, client_order_id):
        """Used by positions/lifecycle.py's reconcile_pending_exit()/
        recover_on_restart(). KIS has no native client-order-id lookup
        (unlike Alpaca), so this looks up our OWN durable idempotency
        table (internal_order_id == client_order_id, exactly what this
        adapter passed as internal_order_id above) to get the KIS-side
        broker_order_id and the ORIGINALLY requested quantity, then
        checks KIS's own fill/open-order history for it. Returns None if
        nothing is found (matching the existing "may return None"
        contract).

        CODEX-045: `ft_ccld_qty` fill rows are per-execution-event, not
        cumulative -- a 2-share sell that fills 1-then-1 across two
        separate KIS fill rows must sum to 2, not report "filled" the
        instant the first 1-share row is seen. Status is derived from
        cumulative_filled_qty vs requested_quantity, never from
        "any fill row exists"."""
        conn = state_db.open_db()
        try:
            row = idempotency.find_existing(
                conn, internal_order_id=client_order_id, signal_id=client_order_id,
                symbol="", side="sell", trading_date="",
            )
        finally:
            conn.close()
        if row is None or not row["broker_order_id"]:
            return None
        broker_order_id = row["broker_order_id"]
        requested_quantity = row["requested_quantity"]
        try:
            fills = self.kis_broker.get_fills(
                start_date=self._now_fn().strftime("%Y%m%d"), end_date=self._now_fn().strftime("%Y%m%d"),
            )
        except KISBrokerError:
            fills = []

        cumulative_filled_qty = 0.0
        weighted_price_sum = 0.0
        matched_any_fill = False
        for fill in fills:
            if fill.get("ODNO") != broker_order_id and fill.get("odno") != broker_order_id:
                continue
            try:
                event_qty = float(fill.get("ft_ccld_qty") or fill.get("FT_CCLD_QTY") or 0)
                event_price = float(fill.get("ft_ccld_unpr3") or fill.get("FT_CCLD_UNPR3") or 0)
            except (TypeError, ValueError):
                continue
            if event_qty <= 0:
                continue
            matched_any_fill = True
            cumulative_filled_qty += event_qty
            weighted_price_sum += event_qty * event_price

        if matched_any_fill:
            filled_avg_price = (weighted_price_sum / cumulative_filled_qty) if cumulative_filled_qty else None
            if requested_quantity is not None and cumulative_filled_qty > requested_quantity:
                from operations import kill_switch as ops_kill_switch
                reason = (
                    f"KIS fill data integrity error: order {broker_order_id!r} "
                    f"(internal_order_id={client_order_id!r}) shows cumulative_filled_qty="
                    f"{cumulative_filled_qty!r} exceeding requested_quantity={requested_quantity!r}"
                )
                ops_kill_switch.set_halt(True, reason=reason, actor="kis_broker_adapter")
                return {
                    "status": "data_integrity_error", "filled_qty": cumulative_filled_qty,
                    "filled_avg_price": filled_avg_price, "id": broker_order_id,
                }
            if requested_quantity is not None and cumulative_filled_qty >= requested_quantity:
                status = "filled"
            else:
                status = "partially_filled"
            return {
                "status": status, "filled_qty": cumulative_filled_qty,
                "filled_avg_price": filled_avg_price, "id": broker_order_id,
            }
        try:
            open_orders = self.kis_broker.get_open_orders()
        except KISBrokerError:
            open_orders = []
        for order in open_orders:
            if order.get("ODNO") == broker_order_id or order.get("odno") == broker_order_id:
                return {"status": "accepted", "filled_qty": None, "filled_avg_price": None, "id": broker_order_id}
        return None

    def get_positions(self):
        """Used only by positions/lifecycle.py's store-corruption
        recovery path. Returns KIS's own domain.Position list, already
        the authoritative source -- no translation needed there since
        that path only checks presence/quantity, not Alpaca-shaped
        fields."""
        return self.kis_broker.get_positions()
