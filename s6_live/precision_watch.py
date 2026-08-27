"""Between the hourly scan and the order: is this candidate ready NOW?

The trade this exists for
-------------------------
DT, 2026-08-26. The scanner published a candidate every fifteen minutes
whose price, volume, VWAP and EMAs were bit-identical for three hours --
51.640, 7,932,617, 51.140, 51.602, 51.551 -- because it was
re-publishing regular-session data with a new `generated_at`. The entry
path saw a fresh timestamp, bought at 52.75 in a zero-volume after-hours
book, and landed 4.01% above the breakout range it was supposedly
trading.

Nothing was broken. Every gate passed. The candidate was simply a
description of a market that had stopped existing hours earlier, and no
step between "the scanner liked this" and "send the order" ever asked
what the market was doing right now.

What this does
--------------
Takes an hourly candidate and re-asks S6's own entry conditions against
the current intraday view, every minute. A candidate is only READY_TO_BUY
while those conditions actually hold; the moment one breaks it stops
being READY. Being on the hourly list is no longer a reason to buy.

Thresholds are READ, never invented
-----------------------------------
Every number comes from `scanners/orb/config.json` -- the same file the
scanner scores against. This module introduces no threshold of its own,
so it cannot drift away from the strategy it is supposed to be watching.

Availability is not falsity
---------------------------
A condition whose input is missing is UNAVAILABLE and blocks READY, the
same as a condition that is false. That is deliberate and is the whole
lesson of the exit-rule defect: a rule that cannot run must never be
counted as a rule that passed.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from s6_live import realtime_features as rf

logger = logging.getLogger(__name__)

STRATEGY_ID = "S6_ORB_BREAKOUT_V1"

# --- candidate states -----------------------------------------------------
DISCOVERED = "DISCOVERED"
WATCHING = "WATCHING"
READY_TO_BUY = "READY_TO_BUY"
INVALIDATED = "INVALIDATED"
DROPPED = "DROPPED"

#: A condition's answer. UNAVAILABLE blocks READY exactly as FAIL does.
PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"

# --- the conditions, in the order they are reported ------------------------
#: The candidate never said when its data was observed. Distinct from
#: "the data is old": one is a stale row, the other is a row that cannot
#: be judged at all, and only the second means the producer is not
#: supplying the contract.
MARKET_DATA_ASOF_UNKNOWN = "MARKET_DATA_ASOF_UNKNOWN"

C_MARKET_DATA_ASOF = "MARKET_DATA_ASOF_KNOWN"
C_MARKET_DATA_FRESH = "MARKET_DATA_FRESH"
C_PRICE = "EXECUTABLE_PRICE"
C_VWAP_AVAILABLE = "VWAP_AVAILABLE"
C_EMA_AVAILABLE = "EMA_AVAILABLE"
C_PRICE_ABOVE_VWAP = "PRICE_ABOVE_VWAP"
C_EMA_STRUCTURE = "EMA9_ABOVE_EMA21"
C_BREAKOUT = "ORB_BREAKOUT_HOLDS"
C_VOLUME_VALID = "VOLUME_DATA_VALID"
C_VOLUME_EXPANSION = "VOLUME_EXPANSION"
C_EXTENSION = "EXTENSION_WITHIN_LIMIT"
C_REENTRY = "SAME_DAY_REENTRY"

CONDITION_ORDER = (
    C_MARKET_DATA_ASOF, C_MARKET_DATA_FRESH, C_PRICE, C_VWAP_AVAILABLE, C_EMA_AVAILABLE,
    C_PRICE_ABOVE_VWAP, C_EMA_STRUCTURE, C_BREAKOUT, C_VOLUME_VALID,
    C_VOLUME_EXPANSION, C_EXTENSION, C_REENTRY,
)


@dataclass(frozen=True)
class WatchEvaluation:
    """One candidate, one minute."""

    symbol: str
    session: Optional[str]
    state: str
    conditions: Dict[str, str] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    features: Optional[rf.SessionFeatures] = None
    evaluated_at: Optional[datetime] = None
    reason: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.state == READY_TO_BUY

    @property
    def blocking(self):
        """Every condition that is not PASS, in report order."""
        return [name for name in CONDITION_ORDER
                if self.conditions.get(name, UNAVAILABLE) != PASS]

    def as_record(self, now=None):
        record = {
            "symbol": self.symbol,
            "session": self.session,
            "state": self.state,
            "reason": self.reason,
            "conditions": dict(self.conditions),
            "detail": dict(self.detail),
            "blocking": self.blocking,
            "evaluated_at": (self.evaluated_at.isoformat()
                             if self.evaluated_at else None),
        }
        if self.features is not None:
            record["features"] = self.features.as_record(now)
        return record


def _orb_config():
    from scanners.base import config as scanner_config

    return scanner_config.load_config("orb", scanner_name="orb")


def evaluate(symbol, *, session=None, now=None, features=None, conn=None,
             config=None, max_age_seconds=None, provider=None,
             candidate=None) -> WatchEvaluation:
    """Re-ask S6's entry conditions against the market as it is now.

    `features` may be supplied by a caller that already built the view
    for this tick (the runtime builds one per symbol and shares it);
    otherwise it is built here.

    Never raises: a candidate that cannot be evaluated is INVALIDATED
    with the reason, which is the safe direction -- it stops being a
    buy, and says why.
    """
    moment = now or datetime.now(timezone.utc)
    try:
        return _evaluate(symbol, session=session, now=moment,
                         features=features, conn=conn, config=config,
                         max_age_seconds=max_age_seconds, provider=provider,
                         candidate=candidate)
    except Exception as exc:  # noqa: BLE001
        logger.warning("S6 precision watch failed for %s", symbol,
                       exc_info=True)
        return WatchEvaluation(
            symbol=symbol, session=session, state=INVALIDATED,
            evaluated_at=moment,
            reason=f"evaluation failed: {type(exc).__name__}: {exc}")


def _evaluate(symbol, *, session, now, features, conn, config,
              max_age_seconds, provider, candidate):
    cfg = config or _orb_config()
    feats = features if features is not None else rf.build(
        symbol, session=session, now=now, provider=provider,
        range_minutes=cfg.require_int("orb_minutes"))

    max_age = (max_age_seconds if max_age_seconds is not None
               else rf.DEFAULT_MAX_BAR_AGE_SECONDS)
    conditions: Dict[str, str] = {}
    detail: Dict[str, Any] = {}

    # -- 1. is the view describing the market NOW?
    #
    # The DT candidate's `generated_at` was minutes old while its market
    # data was hours old. This asks the second question.
    # Research may record a candidate whose data age is unknown. A LIVE
    # BUY may not: "we do not know how old this is" is not a weaker form
    # of "it is recent", and the only safe reading of it is refusal.
    if feats.market_data_asof is None:
        conditions[C_MARKET_DATA_ASOF] = UNAVAILABLE
        detail["market_data_asof_unknown"] = MARKET_DATA_ASOF_UNKNOWN
    else:
        conditions[C_MARKET_DATA_ASOF] = PASS
    stale = feats.is_stale(now, max_age=max_age)
    conditions[C_MARKET_DATA_FRESH] = FAIL if stale else PASS
    detail["market_data_asof"] = (feats.market_data_asof.isoformat()
                                  if feats.market_data_asof else None)
    detail["age_seconds"] = feats.age_seconds(now)

    # -- 2. inputs
    price = feats.price
    conditions[C_PRICE] = PASS if price is not None else UNAVAILABLE
    conditions[C_VWAP_AVAILABLE] = (
        PASS if feats.vwap is not None else UNAVAILABLE)
    conditions[C_EMA_AVAILABLE] = (
        PASS if (feats.ema9 is not None and feats.ema21 is not None)
        else UNAVAILABLE)

    # -- 3. the strategy's own entry conditions, read from its config
    if cfg.require_bool("require_price_above_vwap"):
        if price is None or feats.vwap is None:
            conditions[C_PRICE_ABOVE_VWAP] = UNAVAILABLE
        else:
            conditions[C_PRICE_ABOVE_VWAP] = (
                PASS if price > feats.vwap else FAIL)
            detail["price_vs_vwap_pct"] = (price / feats.vwap - 1.0) * 100.0
    else:
        conditions[C_PRICE_ABOVE_VWAP] = PASS

    if cfg.require_bool("require_ema9_above_ema21"):
        if feats.ema9 is None or feats.ema21 is None:
            conditions[C_EMA_STRUCTURE] = UNAVAILABLE
        else:
            conditions[C_EMA_STRUCTURE] = (
                PASS if feats.ema9 > feats.ema21 else FAIL)
    else:
        conditions[C_EMA_STRUCTURE] = PASS

    # -- 4. is the breakout thesis still true?
    if price is None or feats.range_high is None:
        conditions[C_BREAKOUT] = UNAVAILABLE
    else:
        conditions[C_BREAKOUT] = PASS if price > feats.range_high else FAIL
        detail["range_high"] = feats.range_high

    # -- 5. volume. Unavailable and zero are both "cannot judge", which
    # is what the after-hours DT book was.
    if feats.volume_status == rf.VOLUME_OK:
        conditions[C_VOLUME_VALID] = PASS
    else:
        conditions[C_VOLUME_VALID] = UNAVAILABLE
    detail["volume_status"] = feats.volume_status

    minimum = cfg.require_float("volume_expansion_min")
    detail["volume_expansion_min"] = minimum
    if feats.volume_expansion is None:
        conditions[C_VOLUME_EXPANSION] = UNAVAILABLE
    else:
        conditions[C_VOLUME_EXPANSION] = (
            PASS if feats.volume_expansion >= minimum else FAIL)
        detail["volume_expansion"] = feats.volume_expansion

    # -- 6. extension, measured from the price we would actually pay.
    #
    # The scanner checks this at scan time. DT was 1.76% extended when
    # scanned and 4.01% when bought, and nothing re-asked in between.
    ceiling = cfg.require_float("max_extension_above_or_high_pct")
    detail["max_extension_pct"] = ceiling
    if feats.extension_pct is None:
        conditions[C_EXTENSION] = UNAVAILABLE
    else:
        conditions[C_EXTENSION] = (
            PASS if feats.extension_pct <= ceiling else FAIL)
        detail["extension_pct"] = feats.extension_pct

    # -- 7. not something this strategy already sold today.
    conditions[C_REENTRY] = PASS
    if conn is not None:
        from execution import reentry_policy

        try:
            blocked = reentry_policy.blocked_symbols(
                conn, strategy_id=STRATEGY_ID, now=now)
            if str(symbol).upper() in blocked:
                conditions[C_REENTRY] = FAIL
                detail["same_day_reentry"] = True
        except reentry_policy.ReentryStateUnavailable as exc:
            # Unreadable history must not read as "not blocked".
            conditions[C_REENTRY] = UNAVAILABLE
            detail["same_day_reentry_error"] = str(exc)

    if candidate:
        detail["candidate"] = {
            k: candidate.get(k) for k in
            ("rank", "score", "generated_at", "generation_id", "candidate_id")
            if k in candidate}

    failing = [name for name in CONDITION_ORDER
               if conditions.get(name, UNAVAILABLE) != PASS]
    if not failing:
        state, reason = READY_TO_BUY, None
    else:
        # A candidate that merely is not ready yet is still WATCHING; one
        # whose strategy thesis has actually broken is INVALIDATED. The
        # difference decides whether it can come back this generation.
        broken = {C_PRICE_ABOVE_VWAP, C_EMA_STRUCTURE, C_BREAKOUT,
                  C_EXTENSION, C_REENTRY}
        hard = [name for name in failing
                if name in broken and conditions[name] == FAIL]
        state = INVALIDATED if hard else WATCHING
        reason = ",".join(hard or failing)

    return WatchEvaluation(
        symbol=symbol, session=feats.session or session, state=state,
        conditions=conditions, detail=detail, features=feats,
        evaluated_at=now, reason=reason)


def rank_ready(evaluations, candidates=None):
    """READY candidates, best first.

    Ranking is the scanner's, not this module's: a watch that re-scored
    candidates would be a second strategy wearing the first one's name.
    """
    ranks = {}
    for row in candidates or ():
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            ranks[symbol] = (row.get("rank") if row.get("rank") is not None
                             else 10**6, -(row.get("score") or 0.0))
    ready = [e for e in evaluations if e.ready]
    return sorted(ready, key=lambda e: ranks.get(
        str(e.symbol).upper(), (10**6, 0.0)))


class WatchedCandidateSource:
    """An S6 candidate source that only offers what is READY right now.

    Wrapping rather than replacing: the underlying source still decides
    which rows exist for this session, applies the allow-list and
    reports its own freshness. This adds one question on top -- do this
    candidate's conditions hold against the CURRENT intraday view -- and
    removes from `symbols()` everything that answers no.

    A pure restriction. It can only ever offer fewer candidates than the
    source it wraps, never a different one and never more, which is why
    it is safe to put in front of the live entry path: the failure mode
    of a broken watch is that nothing trades.

    Each evaluation is kept in `evaluations` so the entry cycle's audit
    can say which candidates were considered and precisely which
    condition stopped each one.
    """

    def __init__(self, inner, *, conn=None, session=None, now=None,
                 provider=None, max_age_seconds=None):
        self._inner = inner
        self._conn = conn
        self._session = session
        self._now = now
        self._provider = provider
        self._max_age = max_age_seconds
        self.evaluations: Dict[str, WatchEvaluation] = {}

    # -- everything the entry cycle asks that this does not change.
    #
    # `name` is delegated rather than shadowed: kis_live_trading routes
    # S6 through the capability resolver by matching on it, and a
    # wrapper that answered None would quietly change the route.
    @property
    def name(self):
        return getattr(self._inner, "name", None)

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def symbols(self):
        offered = self._inner.symbols() or []
        ready = []
        for symbol in offered:
            evaluation = evaluate(
                symbol, session=self._session or getattr(
                    self._inner, "_session", None),
                now=self._now, conn=self._conn, provider=self._provider,
                max_age_seconds=self._max_age,
                candidate=self._inner.candidate_row(symbol))
            self.evaluations[symbol] = evaluation
            if evaluation.ready:
                ready.append(symbol)
            else:
                logger.info(
                    "S6 precision watch: %s is %s, not offered for entry "
                    "(blocking: %s)", symbol, evaluation.state,
                    ", ".join(evaluation.blocking))
        return ready

    def allowed_symbols(self):
        # The allow-list is the operator's; readiness is the market's.
        # Both must hold, and this keeps them separate rather than
        # folding one into the other.
        return self._inner.allowed_symbols()

    def describe(self):
        described = dict(self._inner.describe() or {})
        described["precision_watch"] = {
            symbol: {"state": e.state, "blocking": e.blocking}
            for symbol, e in self.evaluations.items()}
        return described
