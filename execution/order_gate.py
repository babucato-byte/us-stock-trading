"""Order Gate -- the single central safety gate every live order must
pass (spec §12). Pure decision logic: every fact it checks (KIS price,
KIS balance, KIS position, reconciliation status, kill switch state,
allow-list, deployed/validated commit) is supplied already-fetched by
the caller (execution/execution_engine.py) via the two context
dataclasses below -- this module makes zero network/DB calls itself, so
its checks are trivially unit-testable and their ORDER is exactly the
order spec §12 lists (fail on the first violated check, never continue
past it).

Buy and sell are two entirely separate gates (`evaluate_buy_gate()` /
`evaluate_sell_gate()`) because their failure conditions don't overlap
(spec §12's two separate check lists) -- there is no shared "generic
order gate" that would blur the two.
"""

import dataclasses
import math
from dataclasses import dataclass
from datetime import datetime
from typing import FrozenSet, Optional

from domain.instrument import Instrument
from domain.order_intent import OrderIntent
from domain.signal import Signal
from config import strategy_registry
from execution import entry_limits, reentry_policy
from execution.entry_limits import EntryLimitState
from execution.secret_redaction import mask_account_number
from reconciliation.snapshot import (
    ReconciliationBlockedError,
    ReconciliationSnapshot,
    verify_snapshot,
)


class OrderGateBlockedError(Exception):
    """Raised with the specific reason the FIRST failing check produced.
    Callers must treat this as a hard block -- zero broker calls happen
    after this is raised (execution_engine.py never calls
    KISBroker.submit_order() unless both gates return cleanly).

    `code` (CODEX-048) is the stable, machine-readable category of the
    failing check, so the Shadow audit trail can record WHICH gate
    rejected an order without pattern-matching English message text."""

    def __init__(self, message, *, code="GATE"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BuyGateContext:
    execution_broker: str
    live_order_enabled: bool
    entry_disabled: bool
    validated_commit: str
    deployed_commit: str
    kis_account_no: str
    allowed_account_no: str
    order_intent: OrderIntent
    instrument: Instrument
    signal: Signal
    is_regular_session: bool
    kis_price_usd: float
    max_price_deviation_percent: float
    usd_orderable_cash: float
    has_open_order_for_symbol: bool
    has_order_for_signal_id: bool
    #: None = no operator symbol restriction. An EMPTY frozenset is a
    #: different instruction and denies every symbol -- see
    #: config.live_rollout_config._env_symbol_set.
    allowed_symbols: Optional[FrozenSet[str]]
    # CODEX-044: the gate takes a VERIFIED SNAPSHOT, never a raw
    # `reconciliation_ok=True`/`has_unknown_order=False` boolean a caller
    # could simply assert. See reconciliation/snapshot.py.
    reconciliation: ReconciliationSnapshot
    # The two rollout caps' authoritative state, collected by the context
    # builder (execution/entry_limits.py) exactly like `reconciliation`
    # is -- the gate performs no I/O. Required, with no default: a caller
    # that forgets it must fail to construct a context, not silently skip
    # a safety limit, which is the defect this field exists to close.
    entry_limits: EntryLimitState
    now: datetime
    #: S1's execution-price verdict, or None for the legacy path.
    #:
    #: The previous-close deviation check below is right for a signal that
    #: is seconds old and wrong for one that is a day old: S1's signal
    #: price IS yesterday's close, so an overnight gap reads as an anomaly
    #: when it is the strategy. When a verdict is supplied the gate
    #: requires it to have PASSED and uses it INSTEAD of the deviation
    #: check; when it is absent the deviation check applies unchanged.
    #:
    #: Fail-closed by construction: an S1 order that forgot to compute a
    #: verdict falls through to the 0.30% check, which blocks it. A wiring
    #: mistake cannot open the gate, only shut it.
    execution_price_check: Optional[object] = None


@dataclass(frozen=True)
class SellGateContext:
    execution_broker: str
    live_order_enabled: bool
    order_intent: OrderIntent
    instrument: Instrument
    kis_position_quantity: int
    position_source: str
    has_existing_sell_order_for_symbol: bool
    # CODEX-044: sells run the IDENTICAL snapshot policy buys do --
    # account/symbol match, TTL, positions/open-orders/fills agreement,
    # zero UNKNOWN orders. `kis_account_no`/`now` exist here purely so
    # that verification is symmetric with the buy gate's.
    reconciliation: ReconciliationSnapshot
    kis_account_no: str
    now: datetime


def _is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_reconciliation(snapshot, *, account_id, symbol, now):
    """CODEX-044: identical policy for buy and sell. Any failure --
    missing snapshot, wrong account/symbol, stale, dirty, or an UNKNOWN
    order anywhere on the account -- is an OrderGateBlockedError, which
    execution_engine.py turns into zero transport calls."""
    try:
        verify_snapshot(snapshot, account_id=account_id, symbol=symbol, now=now)
    except ReconciliationBlockedError as exc:
        raise OrderGateBlockedError(
            f"reconciliation is not OK -- order blocked: {exc}", code="RECONCILIATION",
        ) from exc


def evaluate_buy_gate(ctx: BuyGateContext) -> bool:
    """Returns True if the buy order may proceed; raises
    OrderGateBlockedError with the specific reason otherwise. Checks run
    in the exact order spec §12 lists."""
    if ctx.execution_broker != "kis":
        raise OrderGateBlockedError(
            f"execution broker must be 'kis', got {ctx.execution_broker!r}", code="BROKER")
    if not ctx.live_order_enabled:
        raise OrderGateBlockedError("live order flag is not enabled", code="LIVE_FLAG")
    if ctx.entry_disabled:
        raise OrderGateBlockedError("ENTRY_DISABLED is set -- new entries are blocked", code="ENTRY_DISABLED")
    if ctx.validated_commit != ctx.deployed_commit:
        raise OrderGateBlockedError(
            f"validated commit {ctx.validated_commit!r} does not match deployed commit "
            f"{ctx.deployed_commit!r}", code="COMMIT",
        )
    if ctx.kis_account_no != ctx.allowed_account_no:
        raise OrderGateBlockedError(
            f"KIS account {mask_account_number(ctx.kis_account_no)!r} is not the allowed account "
            f"{mask_account_number(ctx.allowed_account_no)!r}", code="ACCOUNT",
        )
    quantity = ctx.order_intent.quantity
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise OrderGateBlockedError(f"quantity must be an integer, got {quantity!r}", code="QUANTITY")
    if quantity < 1:
        raise OrderGateBlockedError(f"quantity must be >= 1, got {quantity!r}", code="QUANTITY")
    if ctx.order_intent.order_type != "limit":
        raise OrderGateBlockedError(
            f"only limit orders are permitted, got {ctx.order_intent.order_type!r}", code="ORDER_TYPE")
    if not ctx.is_regular_session:
        raise OrderGateBlockedError("not currently in the US regular trading session", code="SESSION")
    if ctx.signal.is_expired(now=ctx.now):
        raise OrderGateBlockedError(f"signal {ctx.signal.signal_id!r} has expired", code="SIGNAL_EXPIRED")
    if not _is_finite_number(ctx.kis_price_usd) or ctx.kis_price_usd <= 0:
        raise OrderGateBlockedError(f"KIS price is invalid: {ctx.kis_price_usd!r}", code="PRICE_INVALID")
    verdict = getattr(ctx, "execution_price_check", None)
    if verdict is None:
        # Legacy/scalping: unchanged, still 0.30% against the signal price.
        deviation_percent = abs(ctx.kis_price_usd - ctx.signal.signal_price) / ctx.signal.signal_price * 100.0
        if deviation_percent > ctx.max_price_deviation_percent:
            raise OrderGateBlockedError(
                f"KIS price {ctx.kis_price_usd!r} deviates {deviation_percent:.4f}% from signal price "
                f"{ctx.signal.signal_price!r}, exceeding the {ctx.max_price_deviation_percent!r}% limit "
                "-- order cancelled, no chase-buy", code="PRICE_DEVIATION",
            )
    else:
        # S1: the price must sit inside the instrument's OWN trading-day
        # range. Not a looser tolerance -- a different question, and one a
        # stale or wrong-exchange quote still fails.
        if not getattr(verdict, "passed", False):
            raise OrderGateBlockedError(
                f"execution-price check failed for {ctx.order_intent.symbol}: "
                f"{getattr(verdict, 'reason_code', 'UNKNOWN')} "
                f"({getattr(verdict, 'detail', '')})", code="EXECUTION_PRICE",
            )
        if getattr(verdict, "symbol", None) != ctx.order_intent.symbol:
            # A verdict for another symbol is not evidence about this one.
            raise OrderGateBlockedError(
                f"execution-price check is for {getattr(verdict, 'symbol', None)!r}, "
                f"not {ctx.order_intent.symbol!r}", code="EXECUTION_PRICE",
            )
    order_notional_usd = quantity * ctx.order_intent.limit_price
    if not _is_finite_number(ctx.usd_orderable_cash) or ctx.usd_orderable_cash < order_notional_usd:
        raise OrderGateBlockedError(
            f"insufficient KIS orderable cash: need ${order_notional_usd:.2f}, "
            f"have ${ctx.usd_orderable_cash!r}", code="CASH",
        )
    if ctx.has_open_order_for_symbol:
        raise OrderGateBlockedError(
            f"an open (unfilled) order already exists for {ctx.order_intent.symbol!r}", code="OPEN_ORDER")
    if ctx.has_order_for_signal_id:
        raise OrderGateBlockedError(
            f"an order already exists for signal_id {ctx.signal.signal_id!r}", code="DUPLICATE_SIGNAL")
    # An allow-list that is PRESENT still restricts; an ABSENT one does
    # not. Absent and empty are different instructions.
    #
    # The list was how LIMITED_LIVE pinned trading to a single
    # hand-picked symbol, and it stayed pinned to "DT" long after that
    # test ended -- so every READY candidate in a normal session was
    # filtered out before any gate ran, and the system looked like it had
    # nothing to trade when it had twelve.
    #
    # `None` -- the variable is not set -- now means "no operator
    # restriction". What a candidate must still pass is unchanged and
    # considerable: strategy conditions, freshness, cash, ownership,
    # reconciliation, duplicate protection, same-day re-entry, instrument
    # eligibility and a verified route. Requiring a human to pre-approve
    # each ticker on top of those is a test posture, not a safety one.
    #
    # An EMPTY list still denies everything, and deliberately so: that is
    # what a blanked value, a truncated env file or a failed load looks
    # like, and none of those may read as "all symbols permitted".
    if ctx.allowed_symbols is not None \
            and ctx.order_intent.symbol not in ctx.allowed_symbols:
        raise OrderGateBlockedError(
            f"{ctx.order_intent.symbol!r} is not in the allowed-symbols list", code="SYMBOL")
    if not ctx.instrument.is_order_eligible:
        raise OrderGateBlockedError(
            f"{ctx.instrument.symbol!r} is not order-eligible (leveraged/inverse/OTC/not tradable)",
            code="INSTRUMENT",
        )
    _check_reconciliation(
        ctx.reconciliation, account_id=ctx.kis_account_no,
        symbol=ctx.order_intent.symbol, now=ctx.now,
    )
    _check_route_evidence(ctx)
    _check_entry_limits(ctx)
    return True


#: What KISBroker.submit_order uses when an intent names no session.
#: Kept in step with `KISBroker._session_hint` -- if that changes, the
#: gate would otherwise judge a route the order does not take.
_BROKER_SESSION_FALLBACK = "REGULAR"

#: The candidate's session addresses a KIS route family that has never
#: produced a live response.
ROUTE_UNVERIFIED = "ROUTE_UNVERIFIED"


def _check_route_evidence(ctx):
    """Refuse a BUY on a route family no live order has ever exercised.

    The gap this closes
    -------------------
    `route_awaiting_live_evidence` existed and was consulted by preflight
    and by the one-shot bootstrap -- but not by the ordinary live order
    path. So the DAYTIME family, whose wire values were all
    LIVE_RESPONSE_PENDING, would have accepted a real BUY on TTTS6036U:
    a first order sent to prove a route works, placed by a strategy that
    did not know it was proving anything.

    BUYS ONLY. A sell is deliberately not gated here. Refusing to exit on
    an unproven route traps a position that the account is already
    holding, which is a larger risk than the route being unproven -- and
    §J's rule is that an exit which cannot be routed LATCHES and retries,
    never that it is abandoned. Reducing exposure on an imperfectly
    evidenced route beats being unable to reduce it at all.

    Unknown fails closed: a family that cannot be resolved is refused
    rather than assumed verified.
    """
    intent = getattr(ctx, "order_intent", None)
    if getattr(intent, "side", None) != "buy":
        return
    # The route this order will ACTUALLY take. KISBroker.submit_order
    # falls back to its `_session_hint` (REGULAR) when the intent names
    # no session, so checking a different session than the one the wire
    # will use would be checking nothing. Mirrored rather than
    # re-decided: the gate and the transport must agree about which
    # route is being judged.
    session = getattr(intent, "session", None) or _BROKER_SESSION_FALLBACK
    try:
        from config import session_capability

        awaiting = session_capability.route_awaiting_live_evidence(session)
    except Exception as exc:  # noqa: BLE001 - unresolvable is unverified
        raise OrderGateBlockedError(
            f"the route family for {session} could not be resolved: {exc}",
            code=ROUTE_UNVERIFIED) from exc
    if awaiting:
        from brokers import kis_broker as kb

        # Named individually so the refusal says WHICH wire value is
        # unproven -- "the route is unverified" is not actionable, and
        # the answer differs per leg: on 2026-08-27 the daytime SELL had
        # live evidence while the BUY TR had none.
        family = kb.family_for_session(session)
        pending = sorted(kb.pending_items_for(
            session_capability.evidence_posture_for_family(family)) or ())
        raise OrderGateBlockedError(
            f"the KIS {family} route for {session} has not produced a live "
            f"response for: {', '.join(pending) or 'one or more wire values'}",
            code=ROUTE_UNVERIFIED)


def _check_entry_limits(ctx):
    """Capacity, and the two locks that replaced it.

    Last, deliberately. Every candidate-specific reason an order would
    be refused anyway -- wrong symbol, ineligible instrument, stale
    reconciliation -- is reported first, so a limit code here means the
    candidate was otherwise fit.

    Two kinds of check live here and they are not equivalent:

      ALWAYS ON   the per-symbol position lock and the same-day
                  re-entry block. Facts about THIS candidate, enforced
                  unconditionally, and the operating risk model now
                  rests on them.

      OPTIONAL    LIVE_ROLLOUT_MAX_POSITIONS,
                  LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY and
                  LIVE_ROLLOUT_MAX_DAILY_ENTRIES. Account-scoped counts
                  that LIMITED_LIVE pinned to 1/1/2 so the first real
                  orders could be counted by hand. Unset they do not
                  run; set they are enforced exactly as before.

    The always-on checks run FIRST so that when a cap is also set, the
    specific reason -- "we already hold this" -- is the one reported
    rather than "the account is full".

    SELLS ARE SUBJECT TO NONE OF IT. An account at its cap must always
    be able to close what it holds -- see evaluate_sell_gate, which does
    not call this.
    """
    limits = ctx.entry_limits
    if limits is None:
        raise OrderGateBlockedError(
            "entry-limit state was not supplied; the position locks and "
            "capacity caps cannot be enforced",
            code=entry_limits.POSITION_LIMIT_STATE_UNKNOWN,
        )

    # -- whose slot is this? Needed by every check below.
    #
    # The strategy comes from the ORDER INTENT, which is what gets
    # written to the idempotency ledger and therefore what every future
    # count will attribute this order to. The signal carries it too, and
    # the two must agree: checking one while recording the other would
    # let them drift apart silently.
    intent_strategy = getattr(ctx.order_intent, "strategy_id", None)
    signal_strategy = getattr(ctx.signal, "strategy_id", None)
    slot = strategy_registry.slot_for(intent_strategy)
    if slot != strategy_registry.slot_for(signal_strategy):
        raise OrderGateBlockedError(
            f"order intent strategy {intent_strategy!r} and signal strategy "
            f"{signal_strategy!r} resolve to different strategies -- this order's "
            "slot cannot be attributed",
            code=entry_limits.STRATEGY_ATTRIBUTION_UNKNOWN,
        )
    # No name, no slot, no lock. Blocking is the only honest answer:
    # "unknown strategy" must not be the one input that skips a limit.
    if slot is None:
        raise OrderGateBlockedError(
            f"the candidate's strategy {intent_strategy!r} is not a known live "
            "strategy, so its position locks cannot be enforced",
            code=entry_limits.STRATEGY_ATTRIBUTION_UNKNOWN,
        )

    # -- ALWAYS ON: one position per symbol.
    candidate_symbol = str(
        getattr(ctx.order_intent, "symbol", None) or "").upper()
    if not candidate_symbol:
        raise OrderGateBlockedError(
            "the order intent carries no symbol, so the per-symbol position "
            "lock cannot be enforced",
            code=entry_limits.STRATEGY_ATTRIBUTION_UNKNOWN,
        )
    if candidate_symbol in limits.strategy_symbols_for(slot):
        raise OrderGateBlockedError(
            f"{slot} already holds or has in flight {candidate_symbol}; "
            "one position per symbol",
            code=entry_limits.SYMBOL_ALREADY_HELD,
        )

    # -- ALWAYS ON: not something this strategy sold today.
    previous_exit = limits.same_day_exit_for(slot, candidate_symbol)
    if previous_exit:
        raise OrderGateBlockedError(
            f"{slot} already exited {candidate_symbol} today "
            f"({previous_exit.get('exit_reason')} at "
            f"{previous_exit.get('exit_price')}); it may be bought again on "
            "the next trading day",
            code=reentry_policy.SAME_DAY_REENTRY_BLOCK,
        )

    # -- OPTIONAL: account-scoped counts, only if an operator set them.
    if (limits.max_open_positions is not None
            and limits.effective_position_count >= limits.max_open_positions):
        raise OrderGateBlockedError(
            f"open-position cap reached: {limits.effective_position_count} of "
            f"{limits.max_open_positions} slot(s) in use "
            f"({limits.open_position_count} held, {limits.pending_entry_count} in flight)",
            code=entry_limits.MAX_OPEN_POSITIONS,
        )
    in_use = limits.strategy_effective_count(slot)
    if (limits.max_positions_per_strategy is not None
            and in_use >= limits.max_positions_per_strategy):
        detail = f"{slot} holds/has in flight {sorted(limits.strategy_symbols_for(slot))}"
        if limits.unattributed_symbols:
            # Said explicitly: a block caused by a row nobody could
            # attribute looks, in a log, exactly like one caused by the
            # strategy really being full -- and the fix for the first is
            # reconciliation, not waiting.
            detail += (f"; {sorted(limits.unattributed_symbols)} could not be "
                       "attributed to a strategy and counts against every one")
        raise OrderGateBlockedError(
            f"per-strategy position cap reached for {slot}: {in_use} of "
            f"{limits.max_positions_per_strategy} slot(s) in use ({detail})",
            code=entry_limits.MAX_STRATEGY_POSITIONS,
        )
    if (limits.max_daily_entries is not None
            and limits.daily_entry_count >= limits.max_daily_entries):
        raise OrderGateBlockedError(
            f"daily-entry cap reached for {limits.trading_day}: "
            f"{limits.daily_entry_count} of {limits.max_daily_entries} entr(y/ies) used",
            code=entry_limits.MAX_DAILY_ENTRIES,
        )


# The buy gate's checks, in the order evaluate_buy_gate() runs them.
# Stated once, here, so the diagnostic evaluator and the read-only probe
# report "how far did this get" against the gate itself rather than
# against a hand-maintained copy. tests/test_observe_cash_path_probe.py
# parses evaluate_buy_gate's source and fails if this drifts.
BUY_GATE_SEQUENCE = (
    "BROKER", "LIVE_FLAG", "ENTRY_DISABLED", "COMMIT", "ACCOUNT", "QUANTITY",
    "ORDER_TYPE", "SESSION", "SIGNAL_EXPIRED", "PRICE_INVALID", "PRICE_DEVIATION",
    # Same position in the sequence as PRICE_DEVIATION, because exactly one
    # of the two runs: the legacy path takes the deviation branch, S1 takes
    # this one. Both are listed so a report can name whichever refused.
    "EXECUTION_PRICE",
    "CASH", "OPEN_ORDER", "DUPLICATE_SIGNAL", "SYMBOL", "INSTRUMENT", "RECONCILIATION",
    # Route evidence runs before the position locks and the capacity
    # caps: a buy addressed to a route no live order has exercised is
    # refused before anything counts slots for it.
    ROUTE_UNVERIFIED,
    entry_limits.SYMBOL_ALREADY_HELD, reentry_policy.SAME_DAY_REENTRY_BLOCK,
    entry_limits.MAX_OPEN_POSITIONS, entry_limits.MAX_STRATEGY_POSITIONS,
    entry_limits.MAX_DAILY_ENTRIES,
)

# The one gate that expresses LIVE ORDER AUTHORIZATION rather than a fact
# about the candidate or the account's safety state.
LIVE_ALLOWLIST_GATE = "SYMBOL"

DIAGNOSTIC_PASS = "DIAGNOSTIC_PASS"


@dataclass(frozen=True)
class DiagnosticGateResult:
    """Two answers to two different questions, deliberately not merged.

    `live_authorization_result` answers "may this order actually be
    placed?" -- the allow-list is part of that answer and blocks it.
    `diagnostic_result` answers "are the remaining safety gates healthy
    for this candidate?" -- a question an operator needs while the
    allow-list is still empty, and one the live answer cannot express
    because it stops at the first violation.

    Neither field ever says APPROVED for a symbol the live allow-list
    does not hold. A diagnostic that passed everything downstream reports
    DIAGNOSTIC_PASS, which is not an authorization and does not read like
    one.
    """

    live_allowlist_allowed: bool
    # The raised errors themselves, so a caller reporting one does not
    # have to re-run the gate to recover its message.
    live_blocked: Optional[OrderGateBlockedError]
    diagnostic_blocked: Optional[OrderGateBlockedError]

    @property
    def live_blocked_code(self):
        return self.live_blocked.code if self.live_blocked is not None else None

    @property
    def diagnostic_blocked_code(self):
        return self.diagnostic_blocked.code if self.diagnostic_blocked is not None else None

    @property
    def live_authorization_result(self):
        if self.live_blocked_code is None:
            return "WOULD_APPROVE"
        return f"LIVE_BLOCKED:{self.live_blocked_code}"

    @property
    def diagnostic_result(self):
        if self.diagnostic_blocked_code is None:
            return DIAGNOSTIC_PASS
        return f"DIAGNOSTIC_BLOCKED:{self.diagnostic_blocked_code}"

    @property
    def diagnostic_furthest_gate(self):
        """The last check the diagnostic evaluation passed."""
        code = self.diagnostic_blocked_code
        if code is None:
            return BUY_GATE_SEQUENCE[-1]
        if code not in BUY_GATE_SEQUENCE:
            return code
        index = BUY_GATE_SEQUENCE.index(code)
        return BUY_GATE_SEQUENCE[index - 1] if index else "NONE"

    def as_audit_payload(self):
        return {
            "live_allowlist_allowed": self.live_allowlist_allowed,
            "live_authorization_result": self.live_authorization_result,
            "diagnostic_result": self.diagnostic_result,
            "diagnostic_furthest_gate": self.diagnostic_furthest_gate,
        }


def _blocked(ctx):
    try:
        evaluate_buy_gate(ctx)
        return None
    except OrderGateBlockedError as exc:
        return exc


def evaluate_buy_gate_diagnostic(ctx: BuyGateContext) -> DiagnosticGateResult:
    """OBSERVE only. Runs the real gate, then -- if and only if the real
    gate stopped at the live allow-list -- runs it again past that one
    check so the downstream gates can be observed.

    This authorizes nothing. It returns a report; it mints no
    authorization token, and execution/authorization.py cannot reach it.
    The second evaluation substitutes the allow-list in a COPY of the
    context, exactly as the hypothetical flag flip already does, so no
    configuration is touched and `evaluate_buy_gate` keeps its
    stop-at-the-first-violation semantics for every real caller.

    Why the re-run rather than a "keep going" mode inside the gate: a
    gate that can be asked to continue past a violation is a gate with a
    bypass in it. This one cannot be told to skip anything -- the caller
    hands it a different allow-list and gets a different answer, which is
    a report about a hypothetical, not a relaxation of the real check.
    """
    live = _blocked(ctx)
    allowed = (ctx.allowed_symbols is None
               or ctx.order_intent.symbol in ctx.allowed_symbols)

    if live is not None and live.code == LIVE_ALLOWLIST_GATE:
        # Look past the allow-list, and only the allow-list.
        beyond = dataclasses.replace(
            ctx, allowed_symbols=frozenset({ctx.order_intent.symbol}))
        diagnostic = _blocked(beyond)
    else:
        # The real verdict already reflects everything the diagnostic
        # would see; re-running would only risk the two disagreeing.
        diagnostic = live

    return DiagnosticGateResult(
        live_allowlist_allowed=allowed, live_blocked=live, diagnostic_blocked=diagnostic,
    )


def evaluate_sell_gate(ctx: SellGateContext) -> bool:
    """Returns True if the sell order may proceed; raises
    OrderGateBlockedError otherwise. Unlike the buy gate, sells are
    NEVER blocked by kill-switch-style entry gates (an existing position
    must always be closeable) -- see live_readiness/order_gateway.py's
    module docstring for the same asymmetry already established for the
    Alpaca/Paper path."""
    if ctx.execution_broker != "kis":
        raise OrderGateBlockedError(
            f"execution broker must be 'kis', got {ctx.execution_broker!r}", code="BROKER")
    if not ctx.live_order_enabled:
        raise OrderGateBlockedError("live order flag is not enabled", code="LIVE_FLAG")
    if ctx.position_source != "kis":
        raise OrderGateBlockedError(
            f"position source must be 'kis', got {ctx.position_source!r}", code="POSITION_SOURCE")
    if ctx.kis_position_quantity <= 0:
        raise OrderGateBlockedError(
            f"no KIS position exists for {ctx.order_intent.symbol!r}", code="NO_POSITION")
    quantity = ctx.order_intent.quantity
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise OrderGateBlockedError(
            f"sell quantity must be a positive integer, got {quantity!r}", code="QUANTITY")
    if quantity > ctx.kis_position_quantity:
        raise OrderGateBlockedError(
            f"sell quantity {quantity!r} exceeds actual KIS position quantity "
            f"{ctx.kis_position_quantity!r}", code="SELL_QTY",
        )
    if ctx.has_existing_sell_order_for_symbol:
        raise OrderGateBlockedError(
            f"a sell order already exists for {ctx.order_intent.symbol!r} -- duplicate liquidation blocked",
            code="DUPLICATE_SELL",
        )
    _check_reconciliation(
        ctx.reconciliation, account_id=ctx.kis_account_no,
        symbol=ctx.order_intent.symbol, now=ctx.now,
    )
    return True


@dataclass(frozen=True)
class CancelGateContext:
    execution_broker: str
    broker_order_id: Optional[str]
    is_actually_open: bool
    kis_account_no: str
    allowed_account_no: str
    symbol: str
    has_cancel_already_in_flight: bool


def evaluate_cancel_gate(ctx: CancelGateContext) -> bool:
    """CODEX-043: cancels are NEVER blocked by HALT (spec §3/§20 -- an
    existing unfilled order may always be cancelled to reduce risk), but
    they are not unconditionally authorized either -- these checks still
    apply. `execution/authorization.py::authorize_cancel()` is the only
    caller; HALT itself is checked there (by deliberately NOT checking
    it), not here."""
    if ctx.execution_broker != "kis":
        raise OrderGateBlockedError(
            f"execution broker must be 'kis', got {ctx.execution_broker!r}", code="BROKER")
    if not ctx.broker_order_id:
        raise OrderGateBlockedError(
            "no broker_order_id supplied -- cannot cancel an unknown order", code="CANCEL_TARGET")
    if not ctx.is_actually_open:
        raise OrderGateBlockedError(
            f"broker_order_id {ctx.broker_order_id!r} is not an actual open KIS order -- refusing to cancel",
            code="CANCEL_TARGET",
        )
    if ctx.kis_account_no != ctx.allowed_account_no:
        raise OrderGateBlockedError(
            f"KIS account {mask_account_number(ctx.kis_account_no)!r} is not the allowed account "
            f"{mask_account_number(ctx.allowed_account_no)!r}", code="ACCOUNT",
        )
    if ctx.has_cancel_already_in_flight:
        raise OrderGateBlockedError(
            f"a cancel is already in flight for {ctx.broker_order_id!r} -- duplicate cancel blocked",
            code="DUPLICATE_CANCEL",
        )
    return True
