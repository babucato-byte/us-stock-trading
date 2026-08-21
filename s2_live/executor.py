"""One S2 tick: exits first, then entry. Records either way.

Exits before entries, always
----------------------------
The same ordering S1 uses, for the same reason: a tick that opened a
position before closing one could breach the position limit against a
book it had already made stale. Closing first means the limit is checked
against what is actually held.

Shadow mode is the default
--------------------------
`live=False` runs the entire cycle -- decisions, records, everything --
and submits nothing. That is not a test harness; it is how S2 earns the
right to trade. Every candidate produces the same record whether or not
it was bought, so when the review asks "did the ones we took do better
than the ones we skipped", both sides were measured by identical code.

Live mode is gated three ways and each is independent:

    scanner_live_mode        S2 must be LIMITED_LIVE, not DISCOVERY_ONLY
    position_limits          the book must have room under the matrix
    entry_policy.confirm     the price must confirm, in a verified session

None of them is a formality and none subsumes another. The live-mode
table is the strategy's status, the limit is the account's capacity, and
the confirmation is about this moment.

Submitting is somebody else's job
---------------------------------
This module decides and records. It calls an injected `submit_fn` and has
no import path to a broker, so a bug here cannot place an order and a
test cannot accidentally reach the account. `submit_fn=None` -- the
default -- means nothing can be sent at all.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from config import position_limits
from s2_live import entry_policy, exit_policy, trade_record

logger = logging.getLogger(__name__)

STRATEGY_ID = "S2_VOLUME_ACCUMULATION_V1"
SCANNER_NAME = "accumulation"

#: Whole shares only, one at a time, during validation. Not a sizing
#: model -- a sizing model is something you build once there is
#: something to size against.
VALIDATION_QUANTITY = 1

SKIP_NOT_LIVE = "S2_NOT_LIMITED_LIVE"
SKIP_LIMIT = "S2_POSITION_LIMIT"
SKIP_NOT_CONFIRMED = "S2_ENTRY_NOT_CONFIRMED"
SKIP_NO_SUBMITTER = "S2_NO_SUBMIT_FUNCTION"
SKIP_UNGATED_SUBMITTER = "S2_SUBMIT_FUNCTION_NOT_GATED"

#: A live submit function must carry this attribute, set True.
#:
#: `submit_fn` is injected so this module needs no broker import, which
#: keeps a bug here from placing an order. But injection cuts both ways:
#: it would also let someone bind a raw broker call and skip the shared
#: BUY gate -- the twenty-step sequence in `execution/order_gate` that
#: checks COMMON_STOCK, orderable cash, reconciliation, duplicate
#: signals and the kill switch. S2's own three gates do not replace any
#: of those; they sit in front of them.
#:
#: So a function that has not declared itself gated is refused in live
#: mode. The marker is deliberately something a caller must set on
#: purpose: forgetting it fails closed, which is the direction that
#: costs nothing, while forgetting the gate itself would cost real
#: money on an unchecked order.
GATED_SUBMIT_MARKER = "applies_buy_gate"


@dataclass
class CycleResult:
    trading_day: str
    session: Optional[str]
    live: bool
    started_at: str
    exits: List[Dict[str, Any]] = field(default_factory=list)
    entries: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    submitted: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"trading_day": self.trading_day, "session": self.session,
                "live": self.live, "started_at": self.started_at,
                "exits": self.exits, "entries": self.entries,
                "skipped": self.skipped, "submitted": self.submitted,
                "errors": self.errors}


def _may_submit(submit_fn) -> bool:
    """True only for a submitter that declared itself gated."""
    if submit_fn is None:
        return False
    if not getattr(submit_fn, GATED_SUBMIT_MARKER, False):
        logger.error("refusing to submit: the submit function has not "
                     "declared that it applies the shared order gates")
        return False
    return True


def s2_is_limited_live() -> bool:
    """Whether S2 is cleared to trade, per the live-mode table.

    Fails closed: any problem reading the table means not live. The
    table is the strategy's status and this module does not get to
    decide it.
    """
    try:
        from config import scanner_live_mode

        return scanner_live_mode.SCANNER_LIVE_MODE.get(
            SCANNER_NAME) == "LIMITED_LIVE"
    except Exception:  # noqa: BLE001
        logger.warning("could not read the S2 live mode; treating as not live",
                       exc_info=True)
        return False


def run_cycle(*, positions, candidates, features_fn, price_fn,
              trading_day: str, session: Optional[str] = None,
              now=None, live: bool = False,
              submit_fn: Optional[Callable] = None,
              open_book: Optional[Dict[str, int]] = None,
              emergency: bool = False) -> CycleResult:
    """Evaluate every open S2 position, then consider one entry.

    `positions` are S2PositionState objects; `candidates` are published
    rows. `features_fn(symbol)` and `price_fn(symbol)` are injected so
    this module needs no provider of its own -- and so a test can drive
    the whole cycle without a network.
    """
    moment = now or datetime.now(timezone.utc)
    result = CycleResult(trading_day=trading_day, session=session, live=live,
                         started_at=moment.isoformat())

    # --- exits first, so the book is current when entry is considered ---
    still_held: Dict[str, int] = dict(open_book or {})
    for position in positions or []:
        try:
            features = features_fn(position.symbol)
            observed = exit_policy.observe(position, features, now=moment)
            decision = exit_policy.decide(
                observed, current_price=price_fn(position.symbol),
                features=features, session=session, now=moment,
                emergency=emergency)
            record = trade_record.from_decision(
                observed, decision, trading_day=trading_day, session=session,
                live=live, now=moment)
            trade_record.append(record)
            entry = {"symbol": position.symbol, **decision.as_dict()}

            if decision.sells:
                # The same marker is required for a SELL. Not because a
                # sell needs the BUY gate -- exits must never be gated by
                # entry risk -- but because the injection risk is
                # identical: an undeclared callable here is just as
                # likely to be a raw broker call.
                if live and _may_submit(submit_fn):
                    submit_fn(symbol=position.symbol, side="sell",
                              quantity=VALIDATION_QUANTITY,
                              reason=decision.reason)
                    result.submitted += 1
                    entry["submitted"] = True
                else:
                    # Shadow: the decision is recorded, nothing is sent.
                    entry["submitted"] = False
                still_held[STRATEGY_ID] = max(
                    0, still_held.get(STRATEGY_ID, 1) - 1)
            result.exits.append(entry)
        except Exception as exc:  # noqa: BLE001 - one position must not
            # cost the others their evaluation, and an exit that failed
            # to evaluate must be visible rather than silently absent.
            logger.error("S2 exit evaluation failed for %s",
                         getattr(position, "symbol", "?"), exc_info=True)
            result.errors.append(f"exit:{getattr(position, 'symbol', '?')}:{exc}")

    # --- then at most one entry ---
    for candidate in list(candidates or [])[:1]:
        symbol = candidate.get("symbol") if isinstance(candidate, dict) \
            else getattr(candidate, "symbol", None)
        try:
            outcome = _consider_entry(
                candidate, symbol=symbol, features_fn=features_fn,
                price_fn=price_fn, session=session, live=live,
                submit_fn=submit_fn, open_book=still_held,
                trading_day=trading_day, now=moment)
            if outcome.get("entered"):
                result.entries.append(outcome)
                result.submitted += 1 if outcome.get("submitted") else 0
            else:
                result.skipped.append(outcome)
        except Exception as exc:  # noqa: BLE001
            logger.error("S2 entry evaluation failed for %s", symbol,
                         exc_info=True)
            result.errors.append(f"entry:{symbol}:{exc}")

    return result


def _consider_entry(candidate, *, symbol, features_fn, price_fn, session,
                    live, submit_fn, open_book, trading_day, now):
    """The three independent gates, then -- maybe -- a submission.

    A shadow record is written for a REFUSED entry too. The refusals are
    the more interesting half of the dataset: they say what S2 would have
    bought, which is the only way to find out whether the gates were
    right.
    """
    signal_price = (candidate.get("price") if isinstance(candidate, dict)
                    else getattr(candidate, "price", None))
    features = features_fn(symbol)
    current = price_fn(symbol)

    verdict = entry_policy.confirm(
        current_price=current, signal_price=signal_price, features=features,
        session=session)

    shadow = trade_record.S2TradeRecord(
        symbol=symbol, trading_day=trading_day, session=session, live=False,
        entry_price=None, signal_price=_number(signal_price),
        current_price=_number(current),
        entry_volume_multiple=_number(
            candidate.get("volume_multiple") if isinstance(candidate, dict)
            else getattr(candidate, "volume_multiple", None)),
        vwap=_number(getattr(features, "vwap", None)),
        provenance={"schema": trade_record.SCHEMA_VERSION,
                    "entry_verdict": verdict.as_dict(),
                    "recorded_at": now.isoformat()})

    if not verdict.allowed:
        trade_record.append(shadow)
        return {"symbol": symbol, "entered": False, "reason": SKIP_NOT_CONFIRMED,
                "verdict": verdict.as_dict()}

    if not live or not s2_is_limited_live():
        # The candidate confirmed and would have been bought. Recorded as
        # a shadow entry so the validation phase produces the comparison
        # it exists to produce.
        shadow.provenance["would_have_entered"] = True
        trade_record.append(shadow)
        return {"symbol": symbol, "entered": False, "reason": SKIP_NOT_LIVE,
                "would_have_entered": True, "verdict": verdict.as_dict()}

    allowance = position_limits.check_entry(STRATEGY_ID, open_book)
    if not allowance.allowed:
        shadow.provenance["would_have_entered"] = True
        shadow.provenance["limit"] = allowance.as_dict()
        trade_record.append(shadow)
        return {"symbol": symbol, "entered": False, "reason": SKIP_LIMIT,
                "limit": allowance.as_dict()}

    if submit_fn is None:
        return {"symbol": symbol, "entered": False, "reason": SKIP_NO_SUBMITTER}

    if not _may_submit(submit_fn):
        # Refused rather than sent. S2's three gates run BEFORE the
        # shared BUY gate, not instead of it, and an ungated submitter
        # would place a real order without the COMMON_STOCK, cash,
        # reconciliation and kill-switch checks.
        return {"symbol": symbol, "entered": False,
                "reason": SKIP_UNGATED_SUBMITTER}

    submit_fn(symbol=symbol, side="buy", quantity=VALIDATION_QUANTITY,
              limit_price=_number(current))
    return {"symbol": symbol, "entered": True, "submitted": True,
            "quantity": VALIDATION_QUANTITY, "limit_price": _number(current),
            "verdict": verdict.as_dict()}


def _number(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number
