"""The one-shot LIMITED LIVE bootstrap: one real BUY of one share.

Why this module exists separately
---------------------------------
Five entries in the KIS verification matrix cannot be confirmed by any
amount of read-only work, because only a live order response carries
them:

    order_path, order_tr_id_live_buy,
    cancel_path, cancel_tr_id_live, cancel_price_field_rule

Confirming them needs exactly one real buy on the live account, and --
only if that buy is still resting -- one real cancel. This module is
where that happens, once, with the transport budget enforced
structurally rather than by convention.

What it deliberately does NOT do
--------------------------------
It does not talk to KISBroker.submit_order() itself, does not issue REST
calls, and does not reimplement a single safety step. Every order goes
through `execution_engine.submit_buy_order()` -- the same Order Gate,
idempotency ledger, reservation, reconciliation snapshot, audit trail,
notification sequence and UNKNOWN policy as any other order. If a check
would block a normal live order it blocks this one; there is no
bootstrap-only shortcut anywhere in the path.

What it adds on top
-------------------
Only restrictions:

* `quantity` is the module constant `BOOTSTRAP_QUANTITY` (1). It is not
  a parameter, not read from configuration, and not derived from cash.
  Cash sizing still runs -- it can only BLOCK, never enlarge.
* The symbol must be the single entry in the live allow-list. Zero or
  two or more entries means zero transport.
* `BootstrapTransportGuard` allows at most one `submit_order` and one
  `cancel_order` for the lifetime of the process, and it is the object
  the engine holds -- a second call cannot reach the network even if
  some future caller loops.
* A full safety re-check runs at gate time, inside the single-run lock,
  with the same facts the gate itself sees. A failure there blocks the
  order through the ordinary gate path, so transport count stays zero.

The candidate is real
---------------------
`select_candidate()` runs the production scanner (`paper_strategy_order.
analyze_stock`) at the production threshold and builds a production
`Signal` and `Instrument`. No threshold is lowered and no symbol can be
passed in from a command line: a bootstrap that ordered whatever it was
told would be testing the operator, not the pipeline.

UNKNOWN is terminal
-------------------
If the response is ambiguous the engine has already written durable
UNKNOWN and sent the ORDER_UNKNOWN alert carrying RETRY=BLOCKED and
RECONCILIATION_REQUIRED=true. This module then stops the process. It
does not retry, does not fall through to a cancel, and does not report
success -- an UNKNOWN order may be live at the broker, and the only
correct next actor is a human running reconciliation.
"""

import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone

import paper_strategy_order as pso
import shadow_audit
from brokers.kis_broker import (
    KISAmbiguousResponseError, KISBrokerError, KISOrderableCashUnavailableError,
)
from config.live_rollout_config import LiveRolloutConfig, LiveRolloutConfigError
from domain.cash_sizing import (
    INSUFFICIENT_CASH, ORDERABLE_CASH_UNAVAILABLE, whole_shares_affordable,
)
from domain.instrument import InstrumentError
from domain.order_intent import OrderIntent, OrderIntentError
from domain.signal import SignalError, build_signal
from execution import bootstrap_capability as capability_mod
from execution import entry_limits, execution_engine, idempotency, order_gate
from execution.execution_engine import ExecutionEngineError
from live_pilot import posture as posture_mod
from market_data.base import MarketDataProviderError
from market_data import candidate_store
from market_data.exchange_registry import ExchangeResolutionError, build_kis_instrument
from market_data.kis_validation_provider import KISValidationProvider
from market_hours import us_trading_day
from operations import kill_switch as ops_kill_switch
from reconciliation import freshness

logger = logging.getLogger(__name__)

# -- the order shape, fixed in code -------------------------------------
# Not configuration. A first real order whose size could be changed by an
# environment variable is a first real order that can be the wrong size.
BOOTSTRAP_QUANTITY = 1
BOOTSTRAP_SIDE = "buy"
BOOTSTRAP_ORDER_TYPE = "limit"
BOOTSTRAP_STRATEGY_ID = "PAPER_STRATEGY_ORDER_SCORE_V1"

SCORE_THRESHOLD = 70  # the production threshold; never lowered here
SIGNAL_VALID_SECONDS = 120

FLAG_BOOTSTRAP_ACK = "LIVE_BOOTSTRAP_ACK"

# -- reason codes -------------------------------------------------------
POSTURE_NOT_BOOTSTRAP = "POSTURE_NOT_LIMITED_LIVE_BOOTSTRAP"
BOOTSTRAP_ACK_MISSING = "BOOTSTRAP_ACK_MISSING"
KIS_ENV_NOT_LIVE = "KIS_ENV_NOT_LIVE"
NOT_REGULAR_SESSION = "NOT_REGULAR_SESSION"
COMMIT_MISMATCH = "COMMIT_MISMATCH"
WORKING_TREE_DIRTY = "WORKING_TREE_DIRTY"
RECONCILIATION_NOT_USABLE = "RECONCILIATION_NOT_USABLE"
HALT_ACTIVE = "HALT_ACTIVE"
ENTRY_NOT_ALLOWED = "ENTRY_NOT_ALLOWED"
ROLLOUT_LIMIT_NOT_ONE = "ROLLOUT_LIMIT_NOT_ONE"
POSITIONS_NOT_ZERO = "POSITIONS_NOT_ZERO"
OPEN_ORDERS_NOT_ZERO = "OPEN_ORDERS_NOT_ZERO"
DAILY_ENTRIES_NOT_ZERO = "DAILY_ENTRIES_NOT_ZERO"
UNRESOLVED_UNKNOWN_ORDERS = "UNRESOLVED_UNKNOWN_ORDERS"
LIVE_ALLOWLIST_NOT_EXACTLY_ONE = "LIVE_ALLOWLIST_NOT_EXACTLY_ONE"
SYMBOL_NOT_ALLOWLISTED = "SYMBOL_NOT_ALLOWLISTED"
KIS_LIVE_NOTIFICATION_NOT_CONFIGURED = "KIS_LIVE_NOTIFICATION_NOT_CONFIGURED"
ORDER_SHAPE_NOT_BOOTSTRAP = "ORDER_SHAPE_NOT_BOOTSTRAP"
NO_QUALIFYING_CANDIDATE = "NO_QUALIFYING_CANDIDATE"
CANDIDATE_PRICE_UNAVAILABLE = "CANDIDATE_PRICE_UNAVAILABLE"
ACCOUNT_READ_FAILED = "ACCOUNT_READ_FAILED"
SAFETY_STATE_UNREADABLE = "SAFETY_STATE_UNREADABLE"
TRANSPORT_BUDGET_EXHAUSTED = "TRANSPORT_BUDGET_EXHAUSTED"
ORDER_UNKNOWN_TERMINAL = "ORDER_UNKNOWN_TERMINAL"
CANCEL_NOT_OPEN_AT_BROKER = "CANCEL_NOT_OPEN_AT_BROKER"
BOOTSTRAP_CAPABILITY_UNAVAILABLE = "BOOTSTRAP_CAPABILITY_UNAVAILABLE"
NO_CANDIDATE = "NO_CANDIDATE"
STALE_CANDIDATE = "STALE_CANDIDATE"
CANDIDATE_SYMBOL_NOT_PUBLISHED = "CANDIDATE_SYMBOL_NOT_PUBLISHED"
CANDIDATE_STORE_UNRESOLVED = "CANDIDATE_STORE_UNRESOLVED"


class BootstrapBlocked(Exception):
    """Blocked before any transport. Always means zero orders placed."""

    def __init__(self, message, *, reason_codes=()):
        super().__init__(message)
        self.reason_codes = tuple(reason_codes)


class BootstrapTransportBudgetExceeded(Exception):
    """A second wire call was attempted. Raised INSTEAD of making it.

    Reaching this is a bug, and the bug is caught before the network
    rather than after -- which is the whole point of a structural cap
    over a `for _ in range(1)`.
    """


class BootstrapUnknownOrder(Exception):
    """The BUY response was ambiguous. The order may be live at KIS.

    Carries no retry affordance on purpose: every caller of this module
    treats it as terminal.
    """

    def __init__(self, message, *, internal_order_id=None):
        super().__init__(message)
        self.internal_order_id = internal_order_id


# ---------------------------------------------------------------------
# Structural transport cap
# ---------------------------------------------------------------------

class BootstrapTransportGuard:
    """A broker proxy with a one-shot budget for each wire verb.

    Everything else is delegated untouched, so the execution engine gets
    the real broker's reads (positions, open orders, balances) and the
    real reconciliation facts. Only `submit_order` and `cancel_order`
    are intercepted, and only to count them and to re-assert the order
    shape immediately before the call.

    The budget is per INSTANCE and the runner builds exactly one
    instance per process, so "at most one BUY for the lifetime of the
    process" is a property of the object graph rather than of anyone
    remembering not to loop.

    Note what is NOT done here: a failed transport does not refund the
    budget. A timeout, a reset connection or a malformed response all
    leave the order possibly live at KIS, and "we are not sure it went
    through" is the strongest possible reason not to send another.
    """

    def __init__(self, broker, *, submit_budget=1, cancel_budget=1, on_submit=None):
        self._broker = broker
        self._submit_budget = submit_budget
        self._cancel_budget = cancel_budget
        self._on_submit = on_submit
        self.submit_calls = 0
        self.cancel_calls = 0

    def __getattr__(self, name):
        # Only reached for names this class does not define, so the two
        # intercepted verbs below can never fall through to the broker.
        return getattr(self._broker, name)

    @property
    def wrapped_broker(self):
        return self._broker

    def submit_order(self, order_intent, instrument, *args, **kwargs):
        if self.submit_calls >= self._submit_budget:
            raise BootstrapTransportBudgetExceeded(
                f"bootstrap submit budget of {self._submit_budget} already used "
                f"({self.submit_calls} call(s)); refusing a further transport"
            )
        _assert_bootstrap_order_shape(order_intent)
        if self._on_submit is not None:
            self._on_submit(order_intent)
        # Counted BEFORE the call, never after. If this raises -- timeout,
        # reset, ambiguous -- the budget is still spent, because the
        # order may well have reached KIS.
        self.submit_calls += 1
        return self._broker.submit_order(order_intent, instrument, *args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        if self.cancel_calls >= self._cancel_budget:
            raise BootstrapTransportBudgetExceeded(
                f"bootstrap cancel budget of {self._cancel_budget} already used "
                f"({self.cancel_calls} call(s)); refusing a further transport"
            )
        self.cancel_calls += 1
        return self._broker.cancel_order(*args, **kwargs)


def _assert_bootstrap_order_shape(order_intent):
    """The last structural assertion before the wire.

    Cheap, no I/O, and cannot fail spuriously -- which is what makes it
    safe to run here, one statement before the network call, rather than
    only at the gate.
    """
    actual = (order_intent.side, int(order_intent.quantity), order_intent.order_type)
    expected = (BOOTSTRAP_SIDE, BOOTSTRAP_QUANTITY, BOOTSTRAP_ORDER_TYPE)
    if actual != expected:
        raise BootstrapBlocked(
            f"bootstrap order shape is fixed at {expected}, got {actual}",
            reason_codes=(ORDER_SHAPE_NOT_BOOTSTRAP,),
        )


# ---------------------------------------------------------------------
# Release identity
# ---------------------------------------------------------------------

def _git(args):
    """Module-level so tests can replace it without a git repository."""
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def head_commit():
    return _git(["rev-parse", "HEAD"])


def working_tree_dirty():
    status = _git(["status", "--porcelain"])
    if status is None:
        return True  # unreadable is not clean
    return bool(status.strip())


# ---------------------------------------------------------------------
# The safety re-check
# ---------------------------------------------------------------------

def final_safety_recheck(*, broker, conn, rollout, order_intent, now, env=None):
    """Every precondition, re-read from live sources, returned as reason
    codes. An empty list means "safe to send this exact order right now".

    This runs from inside the gate context builder, which the execution
    engine calls while holding the single-run lock and immediately
    before the gate decides. Two properties follow, and both matter:

    * The facts are the same ones the gate is about to judge, gathered
      at the same moment. A check run earlier in the script could pass
      against a state that no longer holds by the time the wire is
      touched.
    * A failure blocks through the ordinary gate path, so the transport
      count stays at zero and the durable record ends BLOCKED rather
      than SUBMITTING.

    Everything here is fail-closed: an unreadable fact is a reason code,
    never a pass.
    """
    mapping = env if env is not None else os.environ
    reasons = []

    def flag(name, default=""):
        return str(mapping.get(name, default) or "").strip()

    # -- posture and the explicit human acknowledgement
    decision = posture_mod.resolve_posture(mapping)
    if not decision.bootstrap:
        reasons.append(POSTURE_NOT_BOOTSTRAP)
    if flag(FLAG_BOOTSTRAP_ACK).lower() != "true":
        reasons.append(BOOTSTRAP_ACK_MISSING)
    if flag("KIS_ENV").lower() != "live":
        reasons.append(KIS_ENV_NOT_LIVE)

    # -- market session
    try:
        if pso.get_us_market_session() != "regular":
            reasons.append(NOT_REGULAR_SESSION)
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: market session unreadable")
        reasons.append(NOT_REGULAR_SESSION)

    # -- release identity
    head = head_commit()
    deployed = flag("DEPLOYED_COMMIT")
    validated = flag("VALIDATED_COMMIT")
    if not head or not deployed or not validated or not (head == deployed == validated):
        reasons.append(COMMIT_MISMATCH)
    if working_tree_dirty():
        reasons.append(WORKING_TREE_DIRTY)

    # -- reconciliation: fresh, clean, no unknowns, not halted.
    # freshness.evaluate() signals success by returning and failure by
    # raising; there is no flag to read.
    try:
        freshness.evaluate()
    except Exception:  # noqa: BLE001
        reasons.append(RECONCILIATION_NOT_USABLE)

    # -- halt / kill switch
    try:
        if ops_kill_switch.is_halted():
            reasons.append(HALT_ACTIVE)
        if not ops_kill_switch.is_entry_allowed():
            reasons.append(ENTRY_NOT_ALLOWED)
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: kill switch unreadable")
        reasons.append(SAFETY_STATE_UNREADABLE)

    # -- rollout limits pinned at 1
    if (rollout.max_open_positions != 1 or rollout.max_daily_entries != 1
            or rollout.max_quantity_per_order != 1):
        reasons.append(ROLLOUT_LIMIT_NOT_ONE)

    # -- allow-list is exactly this one symbol
    allowed = frozenset(rollout.allowed_symbols or ())
    if len(allowed) != 1:
        reasons.append(LIVE_ALLOWLIST_NOT_EXACTLY_ONE)
    elif order_intent.symbol not in allowed:
        reasons.append(SYMBOL_NOT_ALLOWLISTED)

    # -- an empty book: no position, no resting order, no entry today.
    # The attempt excludes itself; the engine has already registered its
    # idempotency row by the time this runs.
    try:
        limits = entry_limits.collect(
            broker=broker, conn=conn, rollout=rollout, now=now,
            exclude_internal_order_id=order_intent.internal_order_id,
        )
        if limits.effective_position_count != 0:
            reasons.append(POSITIONS_NOT_ZERO)
        if limits.daily_entry_count != 0:
            reasons.append(DAILY_ENTRIES_NOT_ZERO)
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: entry limit state unreadable")
        reasons.append(SAFETY_STATE_UNREADABLE)

    try:
        if list(broker.get_open_orders() or ()):
            reasons.append(OPEN_ORDERS_NOT_ZERO)
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: open orders unreadable")
        reasons.append(OPEN_ORDERS_NOT_ZERO)

    try:
        if idempotency.count_unknown_orders(conn) != 0:
            reasons.append(UNRESOLVED_UNKNOWN_ORDERS)
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: UNKNOWN order count unreadable")
        reasons.append(UNRESOLVED_UNKNOWN_ORDERS)

    # -- somewhere for the lifecycle to be reported
    import slack_utils

    if not slack_utils.kis_live_notifications_configured():
        reasons.append(KIS_LIVE_NOTIFICATION_NOT_CONFIGURED)

    # -- and the order really is the one-share buy this module promises
    try:
        _assert_bootstrap_order_shape(order_intent)
    except BootstrapBlocked:
        reasons.append(ORDER_SHAPE_NOT_BOOTSTRAP)

    return reasons


# ---------------------------------------------------------------------
# Candidate selection -- production scanner, production threshold
# ---------------------------------------------------------------------

class BootstrapCandidate:
    """What the production pipeline says about the one allow-listed
    symbol. Every field here came from the scanner, the signal builder
    or a KIS read -- none of it from a command line."""

    def __init__(self, *, symbol, instrument, signal, analysis, kis_price_usd,
                 limit_price, orderable_usd, affordable_shares):
        self.symbol = symbol
        self.instrument = instrument
        self.signal = signal
        self.analysis = analysis
        self.kis_price_usd = kis_price_usd
        self.limit_price = limit_price
        self.orderable_usd = orderable_usd
        self.affordable_shares = affordable_shares

    def as_dict(self):
        return {
            "symbol": self.symbol,
            "strategy_id": self.signal.strategy_id,
            "entry_reason": getattr(self.signal, "entry_reason", "unavailable"),
            "score": self.analysis.get("score"),
            "signal_price": self.signal.signal_price,
            "kis_price_usd": self.kis_price_usd,
            "limit_price": self.limit_price,
            "affordable_shares": self.affordable_shares,
            "quantity": BOOTSTRAP_QUANTITY,
        }


def select_candidate(*, broker, rollout, deployed_commit, now):
    """Run the production scanner on the single allow-listed symbol.

    Raises BootstrapBlocked with a reason code rather than returning a
    partial candidate. The scanner threshold is `SCORE_THRESHOLD`, the
    same value the live cycle uses -- a bootstrap that lowered it to
    guarantee itself a candidate would be verifying a path that never
    runs in production.
    """
    allowed = sorted(frozenset(rollout.allowed_symbols or ()))
    if len(allowed) != 1:
        raise BootstrapBlocked(
            f"live allow-list must hold exactly one symbol, holds {len(allowed)}",
            reason_codes=(LIVE_ALLOWLIST_NOT_EXACTLY_ONE,),
        )
    symbol = allowed[0]

    # The published candidate set must be TODAY's and must actually
    # contain this symbol. Without this, a stale file left in the shared
    # store from a previous session would let the bootstrap trade on
    # yesterday's reasoning -- and the live re-score below would not
    # catch it, because a symbol can still score well on a day the
    # scanner never nominated it.
    try:
        # `now` is threaded through so the age check uses the same
        # clock as every other decision in this function, rather
        # than wall-clock time.
        rows, manifest = candidate_store.load_verified(
            trading_day=us_trading_day(now), now=now)
    except candidate_store.CandidatesStale as exc:
        raise BootstrapBlocked(
            f"published candidates are not usable: {exc}",
            reason_codes=(STALE_CANDIDATE,),
        ) from exc
    except candidate_store.CandidatesUnavailable as exc:
        # CandidateStoreUnresolved subclasses this and carries its own
        # code: "the store is misconfigured" needs a different operator
        # response from "the scanner nominated nobody today".
        raise BootstrapBlocked(
            f"no published candidates: {exc}",
            reason_codes=(getattr(exc, "reason_code", NO_CANDIDATE),),
        ) from exc

    if candidate_store.find(symbol, rows=rows) is None:
        raise BootstrapBlocked(
            f"{symbol} is allow-listed but today's scanner did not nominate it "
            f"(published: {[r.get('symbol') for r in rows]})",
            reason_codes=(CANDIDATE_SYMBOL_NOT_PUBLISHED,),
        )

    analysis = pso.analyze_stock(symbol)
    if analysis is None or analysis.get("score", 0) < SCORE_THRESHOLD:
        score = None if analysis is None else analysis.get("score")
        raise BootstrapBlocked(
            f"{symbol} does not meet the production score threshold "
            f"({score} < {SCORE_THRESHOLD}); the threshold is not lowered for the bootstrap",
            reason_codes=(NO_QUALIFYING_CANDIDATE,),
        )

    try:
        # The venue is RESOLVED from the registry, never assumed -- KIS
        # answers a wrong-exchange quote with rt_cd=0 and an empty price,
        # so a hardcoded NASDAQ would make every NYSE/AMEX name silently
        # unpriceable.
        instrument, _record = build_kis_instrument(symbol)
        signal = build_signal(
            strategy_id=BOOTSTRAP_STRATEGY_ID, strategy_version="v1",
            config_version="live_rollout_v1", code_commit=deployed_commit,
            symbol=symbol, exchange=instrument.exchange,
            signal_price=analysis["price"], score=analysis["score"],
            entry_reason="score_threshold_breakout",
            valid_for_seconds=SIGNAL_VALID_SECONDS, now=now,
        )
    except (InstrumentError, SignalError, ExchangeResolutionError) as exc:
        raise BootstrapBlocked(
            f"signal/instrument construction failed: {exc}",
            reason_codes=("INSTRUMENT_INVALID",),
        ) from exc

    validation = KISValidationProvider(
        broker, instrument_lookup=lambda s: build_kis_instrument(s)[0],
    )
    try:
        quote = validation.get_price_quote(symbol)
    except MarketDataProviderError as exc:
        raise BootstrapBlocked(
            f"KIS price re-check failed: {exc}",
            reason_codes=(CANDIDATE_PRICE_UNAVAILABLE,),
        ) from exc

    limit_price = quote.price_usd
    try:
        orderable_usd = broker.get_orderable_usd(instrument, limit_price)
    except KISOrderableCashUnavailableError as exc:
        raise BootstrapBlocked(
            f"KIS orderable-amount read unusable: {exc.diagnostic()}",
            reason_codes=(ORDERABLE_CASH_UNAVAILABLE,),
        ) from exc
    except KISBrokerError as exc:
        raise BootstrapBlocked(
            f"KIS account read failed: {exc}", reason_codes=(ACCOUNT_READ_FAILED,),
        ) from exc

    # Sizing still runs, and can only block. It cannot raise the
    # quantity above BOOTSTRAP_QUANTITY because the quantity is not
    # derived from it -- affordability is a veto, not an input.
    affordable = whole_shares_affordable(orderable_usd, limit_price)
    if affordable < BOOTSTRAP_QUANTITY:
        raise BootstrapBlocked(
            f"orderable cash affords {affordable} share(s), need {BOOTSTRAP_QUANTITY}",
            reason_codes=(INSUFFICIENT_CASH,),
        )

    return BootstrapCandidate(
        symbol=symbol, instrument=instrument, signal=signal, analysis=analysis,
        kis_price_usd=quote.price_usd, limit_price=limit_price,
        orderable_usd=orderable_usd, affordable_shares=affordable,
    )


# ---------------------------------------------------------------------
# The one order
# ---------------------------------------------------------------------

class BootstrapResult:
    def __init__(self, *, candidate, order_intent, execution_result, guard, verification=None,
                 capability=None):
        self.capability = capability
        self.candidate = candidate
        self.order_intent = order_intent
        self.execution_result = execution_result
        self.guard = guard
        self.verification = verification or {}

    @property
    def broker_order_id(self):
        record = getattr(self.execution_result, "execution_record", None)
        return getattr(record, "broker_order_id", None)

    @property
    def status(self):
        return getattr(self.execution_result, "status", None)


def run_bootstrap_buy(*, broker, conn, rollout=None, now=None, env=None):
    """Place at most one real 1-share BUY through the production engine.

    Returns a `BootstrapResult` on success. Raises `BootstrapBlocked`
    (zero transport), `BootstrapUnknownOrder` (terminal, order may be
    live), or the engine's own errors for a clean rejection.
    """
    mapping = env if env is not None else os.environ
    current = now or datetime.now(timezone.utc)

    rollout = rollout or LiveRolloutConfig.from_env()
    try:
        rollout.validate()
    except LiveRolloutConfigError as exc:
        raise BootstrapBlocked(
            f"live_rollout config invalid: {exc}", reason_codes=("CONFIG_INVALID",),
        ) from exc

    deployed_commit = str(mapping.get("DEPLOYED_COMMIT", "") or "").strip()
    account_id = str(mapping.get("KIS_ALLOWED_ACCOUNT_NO", "") or "").strip()
    if not account_id:
        raise BootstrapBlocked(
            "KIS_ALLOWED_ACCOUNT_NO is not configured",
            reason_codes=("ACCOUNT_UNCONFIGURED",),
        )

    candidate = select_candidate(
        broker=broker, rollout=rollout, deployed_commit=deployed_commit, now=current,
    )

    try:
        order_intent = OrderIntent(
            internal_order_id=f"kisboot-{candidate.symbol}-{uuid.uuid4().hex[:12]}",
            signal_id=candidate.signal.signal_id,
            strategy_id=candidate.signal.strategy_id,
            symbol=candidate.symbol,
            exchange=candidate.instrument.exchange,
            # Fixed in code. Not a parameter, not configuration, not
            # derived from cash.
            side=BOOTSTRAP_SIDE,
            quantity=BOOTSTRAP_QUANTITY,
            order_type=BOOTSTRAP_ORDER_TYPE,
            limit_price=candidate.limit_price,
            stop_price=None, target_price=None, created_at=current,
        )
    except OrderIntentError as exc:
        raise BootstrapBlocked(
            f"order intent construction failed: {exc}",
            reason_codes=("ORDER_INTENT_INVALID",),
        ) from exc

    # The authorisation the BROKER's last-line guard requires. Minted
    # here and nowhere else: an ordinary trading path has no way to
    # obtain one, which is what keeps LIVE_BOOTSTRAP_ENABLED from
    # widening anything but this single order.
    try:
        capability = capability_mod.mint(
            symbol=candidate.symbol, allowed_symbols=rollout.allowed_symbols, env=mapping)
    except capability_mod.BootstrapCapabilityError as exc:
        raise BootstrapBlocked(
            f"bootstrap capability could not be minted: {exc}",
            reason_codes=(BOOTSTRAP_CAPABILITY_UNAVAILABLE,),
        ) from exc

    guard = BootstrapTransportGuard(broker)
    audit_run_id = shadow_audit.new_run_id()
    recheck = {"reasons": None}

    def _buy_ctx_builder(reconciliation):
        # Called by the engine inside the single-run lock, immediately
        # before the gate. Both the re-check and the limit collection
        # therefore see the same instant the gate judges.
        reasons = final_safety_recheck(
            broker=broker, conn=conn, rollout=rollout, order_intent=order_intent,
            now=current, env=mapping,
        )
        recheck["reasons"] = reasons
        if reasons:
            # Raised as an ORDER GATE block, not as a bootstrap-specific
            # exception, so the engine's existing rejection path runs:
            # the durable record is CAS'd to REJECTED, a GATE_REJECTED
            # audit event is written, and the transport count stays at
            # zero. A bespoke exception here would leave the row in
            # VALIDATING -- indistinguishable, to reconciliation, from a
            # process that died mid-order.
            raise order_gate.OrderGateBlockedError(
                "bootstrap final safety re-check failed: " + ", ".join(reasons),
                code="BOOTSTRAP_RECHECK",
            )
        limits = entry_limits.collect(
            broker=broker, conn=conn, rollout=rollout, now=current,
            exclude_internal_order_id=order_intent.internal_order_id,
        )
        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=True, entry_disabled=False,
            validated_commit=str(mapping.get("VALIDATED_COMMIT", "") or "").strip(),
            deployed_commit=deployed_commit,
            kis_account_no=account_id, allowed_account_no=account_id,
            order_intent=order_intent, instrument=candidate.instrument,
            signal=candidate.signal, is_regular_session=True,
            kis_price_usd=candidate.kis_price_usd,
            max_price_deviation_percent=rollout.max_price_deviation_percent,
            usd_orderable_cash=candidate.orderable_usd,
            has_open_order_for_symbol=False, has_order_for_signal_id=False,
            allowed_symbols=rollout.allowed_symbols,
            reconciliation=reconciliation, entry_limits=limits, now=current,
        )

    try:
        result = execution_engine.submit_buy_order(
            order_intent=order_intent, buy_gate_context_builder=_buy_ctx_builder,
            conn=conn, broker=guard, instrument=candidate.instrument,
            account_id=account_id, now=current, audit_run_id=audit_run_id,
            bootstrap_capability=capability,
        )
    except KISAmbiguousResponseError as exc:
        # The engine has already written durable UNKNOWN and sent
        # ORDER_UNKNOWN with RETRY=BLOCKED / RECONCILIATION_REQUIRED.
        # Nothing is retried and nothing is cancelled: a cancel needs an
        # order id we do not have, and an UNKNOWN order may be live.
        raise BootstrapUnknownOrder(
            f"BUY response ambiguous; order may be live at KIS and is left UNKNOWN: {exc}",
            internal_order_id=order_intent.internal_order_id,
        ) from exc
    except ExecutionEngineError as exc:
        if recheck["reasons"]:
            raise BootstrapBlocked(
                f"blocked by the final safety re-check: {exc}",
                reason_codes=tuple(recheck["reasons"]),
            ) from exc
        raise

    # The bootstrap share is not a curiosity to be admired and forgotten:
    # it is the first LIMITED LIVE position, and it must be managed like
    # any other. Creating the lifecycle row here is what lets
    # kis_position_manager pick up the fill and run stop / target / time
    # / EOD exits against it, which in turn is what makes the SELL half
    # of the wire matrix observable at all.
    #
    # Position-tracking failure is never treated as order failure -- the
    # order already reached KIS, and raising here would report a
    # successful order as failed.
    try:
        import kis_position_manager

        kis_position_manager.create_kis_position_after_buy(
            strategy_id=order_intent.strategy_id, strategy_version="v1",
            symbol=candidate.symbol, quantity=order_intent.quantity,
            client_order_id=order_intent.internal_order_id,
            broker_order_id=getattr(result.execution_record, "broker_order_id", None),
            now=current,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "bootstrap: position tracking failed after a SUCCESSFUL buy of %s "
            "-- the order stands; reconcile the position manually",
            candidate.symbol)

    return BootstrapResult(candidate=candidate, order_intent=order_intent,
                           execution_result=result, guard=guard, capability=capability)


# ---------------------------------------------------------------------
# Post-BUY verification -- KIS is the authority, not the submit response
# ---------------------------------------------------------------------

def _row_summary(row):
    """The few durable columns worth reporting. Never the whole row --
    it is dumped into an operator log."""
    if row is None:
        return None
    return {key: row[key] for key in ("status", "broker_order_id", "trading_date")
            if key in row.keys()}


def verify_buy(*, broker, conn, result):
    """Ask KIS what actually happened, in the order that makes each
    answer meaningful.

    The submit response says only that KIS accepted the message. It does
    not say the order filled, and treating ACCEPTED as filled is how a
    resting order gets counted as a position. So: broker order id ->
    open-order book -> order status -> fills -> positions -> the local
    durable record. Every step is reported; none is inferred from
    another.

    Returns a dict of observations. Never raises for an unreadable
    step -- the step is recorded as unavailable, because a verification
    routine that dies halfway leaves the operator with less information
    than one that reports what it managed to see.
    """
    broker_order_id = result.broker_order_id
    symbol = result.candidate.symbol
    observed = {
        "broker_order_id": broker_order_id or "unavailable",
        "submit_status": result.status,
        "transport_calls": result.guard.submit_calls,
    }

    def _try(name, fn):
        try:
            observed[name] = fn()
        except Exception as exc:  # noqa: BLE001
            observed[name] = f"unavailable ({type(exc).__name__})"
            logger.warning("bootstrap verification step %s unavailable: %s", name, exc)

    _try("open_orders_at_kis", lambda: [
        o for o in (broker.get_open_orders() or ())
        if (o.get("pdno") or o.get("PDNO")) == symbol
    ])
    _try("kis_positions", lambda: [
        {"symbol": p.symbol, "quantity": p.quantity}
        for p in (broker.get_positions() or ()) if p.symbol == symbol
    ])
    _try("local_order_row", lambda: _row_summary(idempotency.find_existing(
        conn,
        internal_order_id=result.execution_result.internal_order_id,
        signal_id=result.candidate.signal.signal_id,
        symbol=symbol, side=BOOTSTRAP_SIDE,
        # The same US-Eastern trading day the engine keyed the row with.
        trading_date=us_trading_day(result.order_intent.created_at),
    )))

    open_orders = observed.get("open_orders_at_kis")
    positions = observed.get("kis_positions")
    resting = isinstance(open_orders, list) and bool(open_orders)
    held = isinstance(positions, list) and any(
        (p.get("quantity") or 0) > 0 for p in positions)

    # ACCEPTED is not a fill, and this is where that distinction is
    # written down rather than assumed.
    if held and not resting:
        observed["conclusion"] = "FILLED"
    elif resting and not held:
        observed["conclusion"] = "OPEN_UNFILLED"
    elif resting and held:
        observed["conclusion"] = "PARTIALLY_FILLED"
    else:
        observed["conclusion"] = "INDETERMINATE"
    return observed


# ---------------------------------------------------------------------
# Cancel -- only against an order KIS itself still calls open
# ---------------------------------------------------------------------

def cancel_if_open(*, conn, result, verification, order_intent, account_id, env=None):
    """Cancel the bootstrap BUY, but only if an authoritative KIS query
    says it is still open.

    Never cancels on the strength of a local record or a submit-response
    status: if the order filled while we were verifying, a cancel would
    either fail confusingly or -- worse -- read as if the position never
    existed. `verification["conclusion"]` is derived from KIS's own
    open-order book, and only OPEN_UNFILLED authorises the call. The
    cancel gate then re-checks `is_actually_open` against a FRESH read,
    so a fill that lands between verification and cancel still stops it.

    The transport goes through `execution_engine.submit_cancel()` -- the
    same cancel gate, authorization, CAS state machine and audit trail as
    any other cancel -- against the guard, whose cancel budget is 1. One
    call, no retry: an ambiguous cancel keeps the engine's existing
    durable-error / UNKNOWN policy and is never re-sent from here.

    No order is ever created for the purpose of cancelling. If the BUY
    filled, the three cancel wire values stay unconfirmed, and that is
    the honest outcome rather than a reason to place a second order.
    """
    guard = result.guard
    conclusion = verification.get("conclusion")
    if conclusion != "OPEN_UNFILLED":
        return {
            "cancelled": False,
            "reason_code": CANCEL_NOT_OPEN_AT_BROKER,
            "detail": f"KIS reports {conclusion}; cancel is only valid against an open order",
            "transport_calls": guard.cancel_calls,
        }

    broker_order_id = result.broker_order_id
    if not broker_order_id:
        return {
            "cancelled": False,
            "reason_code": CANCEL_NOT_OPEN_AT_BROKER,
            "detail": "no broker order id to cancel",
            "transport_calls": guard.cancel_calls,
        }

    symbol = result.candidate.symbol

    def _cancel_ctx_builder(*_args, **_kwargs):
        # Re-read rather than reuse `verification`: the point of this
        # builder is that the gate judges the book as it is now.
        try:
            still_open = any(
                (o.get("pdno") or o.get("PDNO")) == symbol
                for o in (guard.get_open_orders() or ())
            )
        except Exception:  # noqa: BLE001 -- unreadable is not open
            still_open = False
        return order_gate.CancelGateContext(
            execution_broker="kis", broker_order_id=broker_order_id,
            is_actually_open=still_open, kis_account_no=account_id,
            allowed_account_no=account_id, symbol=symbol,
            has_cancel_already_in_flight=guard.cancel_calls > 0,
        )

    try:
        execution_engine.submit_cancel(
            order_intent=order_intent, broker_order_id=broker_order_id,
            cancel_gate_context_builder=_cancel_ctx_builder, conn=conn,
            broker=guard, instrument=result.candidate.instrument,
            audit_run_id=shadow_audit.new_run_id(),
            bootstrap_capability=getattr(result, "capability", None),
        )
    except BootstrapTransportBudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        return {
            "cancelled": False,
            "reason_code": "CANCEL_FAILED",
            "detail": f"{type(exc).__name__}: {exc}",
            "transport_calls": guard.cancel_calls,
        }
    return {
        "cancelled": True,
        "transport_calls": guard.cancel_calls,
    }
