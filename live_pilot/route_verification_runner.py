"""The one-shot DAYTIME route verification: BUY, cancel, and leave flat.

The flow, and what each step is allowed to conclude
---------------------------------------------------
    mint capability        dedicated flags + a one-symbol allow-list
    read KIS price facts   one read; last / low / e_hogau / e_ordyn
    limit = min(last, low) - 2 ticks        (execution.route_verification)
    BUY  TTTS6036U qty 1 LIMIT              -> proves the buy leg
    verify against KIS's own open-order book
      |- OPEN_UNFILLED  -> cancel TTTS6038U -> proves the cancel legs
      `- FILLED         -> flatten TTTS6037U for the ACTUAL filled qty

Nothing infers a conclusion from the previous step's hope. The submit
response says only that KIS accepted the message; whether the order is
resting or filled is asked of KIS, and the answer decides which branch
runs. That is `bootstrap.verify_buy`'s contract and it is reused rather
than restated.

Why the flatten instead of an exit engine
-----------------------------------------
This order does not want the shares. Building a scheduled exit engine for
a position nobody intends to hold would be machinery to babysit an
accident. So an unexpected fill is flattened immediately on the daytime
SELL route -- TTTS6037U, the one daytime leg a live response has already
confirmed (2026-08-27, odno 0000001014, filled 1 @ 51.61).

When the flatten itself fails
-----------------------------
Then there IS real exposure, and the intent stops mattering. The
remaining quantity is handed to S6's exit monitor -- the only live exit
engine that runs on a schedule -- carrying ROUTE_VERIFICATION so it is
managed without entering S6's performance record. A fallback, never the
plan, and it is the reason this module refuses to end quietly: an
unflattened fill raises, and the caller alerts.

One of each verb, ever
----------------------
`_TransportBudget` allows at most one submit_order, one cancel_order and
one flatten for the lifetime of the process. It is a property of the
object the engine holds, not of anyone remembering not to loop. Nothing
here retries: an ambiguous response to any verb is terminal.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from execution import route_verification as capability_mod

logger = logging.getLogger(__name__)

#: Recorded as the strategy on the order ledger row. NOT an S6 id: the
#: ledger is what `reconciliation/ownership.claimant_from_ledger` reads,
#: and attributing this to S6 would make S6 the claimant of a position it
#: never decided to take.
VERIFICATION_STRATEGY_ID = "ROUTE_VERIFICATION_V1"

#: How long the synthetic signal behind the order stays valid. Short: the
#: order is meant to exist for seconds.
SIGNAL_VALID_SECONDS = 300

CONCLUSION_CANCELLED = "ROUTE_VERIFIED_CANCELLED"
CONCLUSION_FLATTENED = "ROUTE_VERIFIED_FLATTENED"
CONCLUSION_EXPOSED = "FLATTEN_FAILED_EXPOSURE"


class RouteVerificationBlocked(Exception):
    """A precondition failed. No order was sent."""

    def __init__(self, message, *, reason_codes=()):
        super().__init__(message)
        self.reason_codes = tuple(reason_codes)


class RouteVerificationExposed(Exception):
    """The BUY filled and the flatten did not complete.

    Terminal and loud. The remaining quantity has been handed to S6's
    exit monitor under the ROUTE_VERIFICATION marker, and a human must
    know that a route test left real shares behind.
    """

    def __init__(self, message, *, remaining_qty=None, position_id=None):
        super().__init__(message)
        self.remaining_qty = remaining_qty
        self.position_id = position_id


class _TransportBudget:
    """One submit, one cancel, one flatten. Spent BEFORE the call."""

    def __init__(self, broker):
        self._broker = broker
        self.submit_calls = 0
        self.cancel_calls = 0
        self.flatten_calls = 0

    def __getattr__(self, item):
        return getattr(self._broker, item)

    def submit_order(self, order_intent, instrument, *args, **kwargs):
        side = getattr(order_intent, "side", None)
        if side == "sell":
            if self.flatten_calls:
                raise RouteVerificationBlocked(
                    "the flatten budget is spent; refusing a second SELL")
            self.flatten_calls += 1
        else:
            if self.submit_calls:
                raise RouteVerificationBlocked(
                    "the BUY budget is spent; refusing a second BUY")
            if getattr(order_intent, "quantity", None) != \
                    capability_mod.VERIFICATION_QUANTITY:
                raise RouteVerificationBlocked(
                    "a verification BUY is exactly one share")
            self.submit_calls += 1
        return self._broker.submit_order(order_intent, instrument, *args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        if self.cancel_calls:
            raise RouteVerificationBlocked(
                "the cancel budget is spent; refusing a second cancel")
        self.cancel_calls += 1
        return self._broker.cancel_order(*args, **kwargs)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def price_facts(broker, instrument) -> Dict[str, Any]:
    """One KIS price-detail read, whole. Raises if it cannot be had."""
    try:
        return broker.get_price_detail(instrument)
    except Exception as exc:  # noqa: BLE001
        raise RouteVerificationBlocked(
            f"KIS price detail unavailable: {exc}",
            reason_codes=("PRICE_DETAIL_UNAVAILABLE",)) from exc


def build_intent(*, symbol, instrument, limit_price, now):
    """The order intent. Session is stamped, never left to a default."""
    from domain.order_intent import OrderIntent

    return OrderIntent(
        internal_order_id=f"rtverify-{symbol}-{uuid.uuid4().hex[:12]}",
        signal_id=f"rtverify-{symbol}-{uuid.uuid4().hex[:8]}",
        strategy_id=VERIFICATION_STRATEGY_ID,
        symbol=symbol, exchange=instrument.exchange,
        side=capability_mod.VERIFICATION_SIDE,
        quantity=capability_mod.VERIFICATION_QUANTITY,
        order_type=capability_mod.VERIFICATION_ORDER_TYPE,
        limit_price=limit_price, stop_price=None, target_price=None,
        created_at=now, session=capability_mod.VERIFICATION_SESSION,
    )


def adopt_exposure(conn, *, symbol, quantity, basis, broker_order_id,
                   client_order_id, now=None):
    """Hand unflattened shares to S6's exit monitor, marked.

    The ONLY path by which a verification order becomes a managed
    position, and it runs only after a flatten has failed. The marker
    travels on the row so every S6 performance reader can exclude it: S6
    is managing these shares, and S6 did not trade them.

    The basis is the broker's own average. Never a guess -- a position
    opened at an invented price looks correct and is not, and the stop
    would be measured from it.
    """
    from s6_live import position_store

    current = _now(now)
    position_id = position_store.record_submission(
        conn, symbol=symbol, variant=capability_mod.ROUTE_VERIFICATION_MARKER,
        entry_session=capability_mod.VERIFICATION_SESSION,
        client_order_id=client_order_id, now=current)
    # The broker's OWN average, through the store's own refusal of an
    # unusable price. A basis that cannot be established must not become
    # a position: the stop is measured from it.
    position_store.open_from_fill(
        conn, position_id, quantity=quantity, average_fill_price=basis,
        entry_order_id=broker_order_id, now=current)
    logger.error(
        "ROUTE_VERIFICATION_EXPOSURE symbol=%s qty=%s basis=%s position=%s -- "
        "the verification BUY filled and could not be flattened; S6's exit "
        "monitor now owns it under the %s marker and it is excluded from S6 "
        "performance", symbol, quantity, basis, position_id,
        capability_mod.ROUTE_VERIFICATION_MARKER)
    return position_id


# ---------------------------------------------------------------------
# Disarm -- the same atomic env procedure a deploy uses
# ---------------------------------------------------------------------

#: The allow-list key the verification reads its single symbol from.
ALLOWLIST_KEY = "LIVE_ROLLOUT_ALLOWED_SYMBOLS"

_DISARMED = {
    capability_mod.FLAG_ENABLED: "false",
    capability_mod.FLAG_ACK: "false",
    ALLOWLIST_KEY: "",
}


def disarm(env_path, *, now=None) -> Dict[str, Any]:
    """Turn the verification flags off and clear the allow-list.

    Backup, write a temp file in the SAME directory, replace atomically.
    Every cron re-reads this file each minute, so a partial write would
    be read half-formed -- the identical reason a deploy switches it this
    way.

    Only the three verification keys are touched. An unrelated live flag
    changed here would be a trading decision taken by a cleanup routine.

    Returns what it did. Raises only if it could not disarm, because a
    silent failure here leaves the one-shot armed.
    """
    import shutil
    import tempfile
    from pathlib import Path

    target = Path(env_path)
    stamp = (now or _now()).strftime("%Y%m%dT%H%M%SZ")
    backup = target.parent.parent / "backups" / f"{target.name}.pre-disarm.{stamp}"
    try:
        if backup.parent.is_dir():
            shutil.copy2(target, backup)
        else:
            backup = None
    except Exception:  # noqa: BLE001 - a missing backup must not stop the
        backup = None  # disarm; leaving it armed is the worse outcome

    original = target.read_text(encoding="utf-8")
    lines, seen = [], set()
    for line in original.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in _DISARMED:
            lines.append(f"{key}={_DISARMED[key]}")
            seen.add(key)
        else:
            lines.append(line)
    for key, value in _DISARMED.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    updated = "\n".join(lines) + ("\n" if original.endswith("\n") else "")

    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), delete=False,
        prefix=f".{target.name}.disarm.")
    try:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    shutil.copymode(target, handle.name)
    os.replace(handle.name, target)

    logger.warning("ROUTE_VERIFICATION_DISARMED %s=false %s=false %s cleared",
                   capability_mod.FLAG_ENABLED, capability_mod.FLAG_ACK,
                   ALLOWLIST_KEY)
    return {"disarmed": True, "backup": str(backup) if backup else None,
            "keys": sorted(_DISARMED)}


# ---------------------------------------------------------------------
# The one-shot
# ---------------------------------------------------------------------

class _Shim:
    """The shape `bootstrap.verify_buy` / `cancel_if_open` already read.

    A shim rather than a reimplementation on purpose: those two own the
    contract for turning KIS's open-order book and positions into
    FILLED / OPEN_UNFILLED / PARTIALLY_FILLED / INDETERMINATE, and a
    second copy of that interpretation is a second thing to get wrong.

    `capability` is deliberately None. The bootstrap passes its own
    capability down to the broker guard; ours is not a BootstrapCapability
    and must never be offered as one.
    """

    capability = None

    def __init__(self, *, symbol, instrument, guard, order_intent,
                 execution_result, broker_order_id, status):
        # `.signal` is part of the shape too: `verify_buy` reads
        # `candidate.signal.signal_id` to find the local ledger row, and
        # without it that observation degrades to "unavailable" -- losing
        # exactly the durable-record check the verification exists for.
        self.candidate = type("_C", (), {
            "symbol": symbol, "instrument": instrument,
            "signal": type("_S", (), {
                "signal_id": getattr(order_intent, "signal_id", None)})()})()
        self.guard = guard
        self.order_intent = order_intent
        self.execution_result = execution_result
        self.broker_order_id = broker_order_id
        self.status = status


def _filled_quantity(observation) -> int:
    """Shares KIS says the account holds for this symbol, or 0."""
    positions = observation.get("kis_positions")
    if not isinstance(positions, list):
        return 0
    total = 0
    for row in positions:
        try:
            total += int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
    return max(total, 0)


def flatten(*, broker, conn, guard, symbol, instrument, quantity, account_id,
            limit_price, now=None) -> Dict[str, Any]:
    """Sell back exactly what the verification accidentally bought.

    Through `execution_engine.submit_sell_order`, so the SELL is gated,
    authorised, state-machined and audited like any other exit. The
    daytime SELL route is the one daytime leg a live response has already
    confirmed, which is why flattening is safer than holding.

    `limit_price` is KIS's own last trade, the same source
    `kis_broker_adapter` prices every other exit from. Market orders are
    forbidden in this pilot, so an exit is always a limit, and using the
    established convention beats inventing a marketable-limit rule here.

    Never retried. An ambiguous SELL leaves the engine's durable UNKNOWN
    standing and the caller treats the shares as still held, because
    "we do not know" must never resolve to "flat".
    """
    from domain.order_intent import OrderIntent
    from execution import execution_engine
    from execution import order_gate
    from reconciliation import snapshot as recon

    current = _now(now)
    intent = OrderIntent(
        internal_order_id=f"rtflat-{symbol}-{uuid.uuid4().hex[:12]}",
        signal_id=f"rtflat-{symbol}-{uuid.uuid4().hex[:8]}",
        strategy_id=VERIFICATION_STRATEGY_ID, symbol=symbol,
        exchange=instrument.exchange, side="sell", quantity=int(quantity),
        order_type="limit", limit_price=limit_price, stop_price=None,
        target_price=None, created_at=current,
        session=capability_mod.VERIFICATION_SESSION)

    def _sell_ctx_builder(reconciliation):
        return order_gate.SellGateContext(
            execution_broker="kis", live_order_enabled=True,
            order_intent=intent, instrument=instrument,
            kis_position_quantity=int(quantity), position_source="kis_balance",
            has_existing_sell_order_for_symbol=False,
            reconciliation=reconciliation, kis_account_no=account_id,
            now=current)

    return {"submitted": True, "intent": intent,
            "result": execution_engine.submit_sell_order(
                order_intent=intent, sell_gate_context_builder=_sell_ctx_builder,
                conn=conn, broker=guard, instrument=instrument,
                account_id=account_id, now=current,
                audit_run_id=uuid.uuid4().hex[:16])}


def run_route_verification(*, broker, conn, allowed_symbols, account_id,
                           now=None, env=None, env_path=None) -> Dict[str, Any]:
    """The whole one-shot, from precondition to disarm.

    Order of operations is the safety property. Every precondition is
    established BEFORE the capability is minted, and the capability is
    minted before anything is sent, so a refusal costs zero transport
    calls and leaves nothing armed that a later step must undo.

    Branches, and what each is allowed to conclude:

        OPEN_UNFILLED     cancel via the daytime cancel path
        FILLED            flatten the ACTUAL held quantity
        PARTIALLY_FILLED  cancel the resting remainder, flatten what filled
        INDETERMINATE     neither. KIS could not tell us, so we do not act:
                          cancelling on ignorance is how a filled order is
                          cancelled out from under a position

    Nothing is retried anywhere. A second BUY is impossible by
    construction -- `_TransportBudget` spends the budget before the call
    and the engine holds that object, not the raw broker.

    Returns a report. Raises `RouteVerificationExposed` when shares are
    left behind, because a route test that ends with real exposure must
    not return a value a caller could mistake for success.
    """
    from execution import execution_engine
    from execution import order_gate
    from config import session_capability
    from live_pilot.bootstrap import build_kis_instrument

    mapping = env if env is not None else os.environ
    current = _now(now)
    report: Dict[str, Any] = {"started_at": current.isoformat(),
                              "transport": {"buy": 0, "cancel": 0, "flatten": 0}}

    # -- 1. the window. Asked of the resolver the ORDER PATH uses, so the
    # answer is the one the wire will get.
    session = session_capability.route_session(now=current)
    if session != capability_mod.VERIFICATION_SESSION:
        raise RouteVerificationBlocked(
            f"the daytime route is not addressable now (session={session!r})",
            reason_codes=("NOT_DAYTIME_WINDOW",))

    # -- 2. exactly one symbol. Not "contains": a verification authorised
    # while two are eligible is one that could have picked the other.
    allowed = sorted({str(s).strip().upper() for s in (allowed_symbols or ()) if s})
    if len(allowed) != 1:
        raise RouteVerificationBlocked(
            f"the allow-list must hold exactly one symbol, holds {len(allowed)}",
            reason_codes=("ALLOWLIST_NOT_EXACTLY_ONE",))
    symbol = allowed[0]
    report["symbol"] = symbol

    # The same resolver the bootstrap uses, so a symbol this system can
    # address is decided in one place.
    built = build_kis_instrument(symbol)
    instrument = built[0] if isinstance(built, tuple) else built

    # -- 3/4. one price read, one arithmetic rule, no fabrication.
    detail = price_facts(broker, instrument)
    try:
        limit_price = capability_mod.limit_price_from(detail)
    except capability_mod.VerificationPriceUnavailable as exc:
        raise RouteVerificationBlocked(
            f"verification price could not be established: {exc}",
            reason_codes=("PRICE_NOT_ESTABLISHED",)) from exc
    report["limit_price"] = limit_price
    report["price_facts"] = {k: detail.get(k) for k in
                             ("last", "low", "tick_size", "orderable_text")}

    # -- 5/6. capability, then intent. Minted only once every fact above
    # has held.
    capability = capability_mod.mint(symbol=symbol, allowed_symbols=allowed,
                                     env=mapping)
    intent = build_intent(symbol=symbol, instrument=instrument,
                          limit_price=limit_price, now=current)
    report["internal_order_id"] = intent.internal_order_id

    guard = _TransportBudget(broker)

    def _buy_ctx_builder(reconciliation):
        from config import strategy_entry_policy
        from config.live_rollout_config import LiveRolloutConfig
        from execution import entry_limits

        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=True,
            entry_disabled=not strategy_entry_policy.entry_enabled(
                intent.strategy_id),
            validated_commit=str(mapping.get("VALIDATED_COMMIT", "") or "").strip(),
            deployed_commit=str(mapping.get("DEPLOYED_COMMIT", "") or "").strip(),
            kis_account_no=account_id, allowed_account_no=account_id,
            order_intent=intent, instrument=instrument,
            signal=_verification_signal(intent, current),
            is_regular_session=True,
            kis_price_usd=limit_price,
            max_price_deviation_percent=100.0,
            usd_orderable_cash=limit_price * 2,
            has_open_order_for_symbol=False, has_order_for_signal_id=False,
            allowed_symbols=frozenset(allowed),
            reconciliation=reconciliation,
            entry_limits=entry_limits.collect(
                broker=broker, conn=conn,
                rollout=LiveRolloutConfig.from_env(mapping), now=current,
                exclude_internal_order_id=intent.internal_order_id),
            now=current,
            # The ONLY context in the codebase that supplies this.
            route_verification_capability=capability)

    # -- 7. through the engine. Ambiguity stays the engine's contract:
    # durable UNKNOWN, alert, no retry, and this function stops.
    try:
        execution_result = execution_engine.submit_buy_order(
            order_intent=intent, buy_gate_context_builder=_buy_ctx_builder,
            conn=conn, broker=guard, instrument=instrument,
            account_id=account_id, now=current,
            audit_run_id=uuid.uuid4().hex[:16])
    except Exception as exc:
        report["transport"]["buy"] = guard.submit_calls
        report["conclusion"] = "BUY_FAILED"
        report["detail"] = f"{type(exc).__name__}: {exc}"
        # An ambiguous BUY may be live at KIS. No speculative cancel is
        # attempted here: the engine has already written UNKNOWN, and the
        # only correct next actor reads KIS's own history.
        raise

    report["transport"]["buy"] = guard.submit_calls
    report["broker_order_id"] = getattr(execution_result, "broker_order_id", None)
    report["submit_status"] = getattr(execution_result, "status", None)

    # -- 8. what actually happened, asked of KIS.
    shim = _Shim(symbol=symbol, instrument=instrument, guard=guard,
                 order_intent=intent, execution_result=execution_result,
                 broker_order_id=report["broker_order_id"],
                 status=report["submit_status"])
    from live_pilot import bootstrap

    observation = bootstrap.verify_buy(broker=broker, conn=conn, result=shim)
    report["observation"] = observation
    conclusion = observation.get("conclusion")
    report["kis_conclusion"] = conclusion

    held = _filled_quantity(observation)
    report["filled_quantity"] = held

    # -- 9A/9C. a resting order is cancelled; a partially filled one has
    # its remainder cancelled first, so the flatten sells only what is
    # genuinely held.
    if conclusion in ("OPEN_UNFILLED", "PARTIALLY_FILLED"):
        try:
            cancel = bootstrap.cancel_if_open(
                conn=conn, result=shim, verification=observation,
                order_intent=intent, account_id=account_id, env=mapping)
        except Exception as exc:  # noqa: BLE001 - an ambiguous cancel is
            # never retried; the remaining exposure is re-read below.
            cancel = {"cancelled": False, "reason_code": "CANCEL_AMBIGUOUS",
                      "detail": f"{type(exc).__name__}: {exc}"}
        report["cancel"] = cancel
        report["transport"]["cancel"] = guard.cancel_calls

    # -- 9B/9C. anything actually held is sold back immediately.
    if held > 0:
        try:
            flat = flatten(broker=broker, conn=conn, guard=guard, symbol=symbol,
                           instrument=instrument, quantity=held,
                           account_id=account_id,
                           limit_price=float(detail.get("last")),
                           now=current)
            report["flatten"] = {"submitted": True,
                                 "status": getattr(flat.get("result"), "status", None)}
        except Exception as exc:  # noqa: BLE001 - never assume flat
            report["flatten"] = {"submitted": False,
                                 "detail": f"{type(exc).__name__}: {exc}"}
        report["transport"]["flatten"] = guard.flatten_calls

        # -- 10. re-read rather than trust the SELL's own answer. "We do
        # not know" must never resolve to "flat".
        remaining = _remaining_at_broker(broker, symbol, default=held)
        report["remaining_quantity"] = remaining
        if remaining > 0:
            basis = _basis_at_broker(broker, symbol)
            position_id = None
            try:
                position_id = adopt_exposure(
                    conn, symbol=symbol, quantity=remaining, basis=basis,
                    broker_order_id=report["broker_order_id"],
                    client_order_id=intent.internal_order_id, now=current)
            except Exception as exc:  # noqa: BLE001
                # Adoption can fail legitimately -- an unreadable broker
                # gives no basis, and `open_from_fill` refuses to open a
                # position at a price nobody measured. That refusal is
                # correct and must NOT become a silent pass: the exposure
                # is real either way, so it is reported as exposure with
                # the adoption failure attached rather than raised as a
                # store error the caller would not recognise.
                report["adoption_error"] = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "ROUTE_VERIFICATION_UNADOPTED symbol=%s qty=%s -- the "
                    "flatten left exposure AND it could not be adopted (%s); "
                    "this position is unmanaged and needs a human now",
                    symbol, remaining, exc)
            report["conclusion"] = CONCLUSION_EXPOSED
            report["adopted_position_id"] = position_id
            raise RouteVerificationExposed(
                f"{remaining} share(s) of {symbol} remain after the flatten; "
                + (f"S6's exit monitor now owns them as {position_id}"
                   if position_id else
                   "they could NOT be adopted and are unmanaged"),
                remaining_qty=remaining, position_id=position_id)
        report["conclusion"] = CONCLUSION_FLATTENED
        return report

    report["conclusion"] = (CONCLUSION_CANCELLED
                           if report.get("cancel", {}).get("cancelled")
                           else "NO_FILL_NOT_CANCELLED")
    return report


def _verification_signal(intent, now):
    from domain.signal import build_signal

    return build_signal(
        strategy_id=intent.strategy_id, strategy_version="v1",
        config_version="route_verification_v1", code_commit="",
        symbol=intent.symbol, exchange=intent.exchange,
        signal_price=intent.limit_price, score=0.0,
        entry_reason=capability_mod.ROUTE_VERIFICATION_MARKER,
        valid_for_seconds=SIGNAL_VALID_SECONDS, now=now,
        signal_id=intent.signal_id)


def _remaining_at_broker(broker, symbol, *, default) -> int:
    """Shares KIS still reports. An unreadable book returns `default` --
    the pessimistic answer, because assuming flat is the one error that
    leaves an orphan."""
    try:
        return sum(int(p.quantity or 0) for p in (broker.get_positions() or ())
                   if p.symbol == symbol)
    except Exception:  # noqa: BLE001
        logger.error("could not re-read %s at the broker; assuming %s still "
                     "held rather than assuming flat", symbol, default)
        return int(default)


def _basis_at_broker(broker, symbol) -> Optional[float]:
    try:
        for position in broker.get_positions() or ():
            if position.symbol == symbol:
                return position.average_fill_price
    except Exception:  # noqa: BLE001
        return None
    return None
