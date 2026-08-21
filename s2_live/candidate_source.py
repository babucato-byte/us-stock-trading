"""S2's candidates, offered to the SHARED buy cycle.

Why this is a source and not a pipeline
---------------------------------------
`kis_live_trading.run_live_buy_entry_cycle` states the rule:

    "Only the SOURCE is pluggable. Every gate below -- allow-list check,
     price re-validation, orderable cash, duplicate order, entry limits,
     kill switch, reconciliation, the Execution Engine -- is shared by
     every source and exists exactly once. A second candidate source
     must never mean a second pipeline: two pipelines are two ideas of
     what is safe, and they diverge silently."

So S2 does not get a submit path. It answers the two questions the cycle
asks a source -- which symbols to look at, and which the Order Gate is
told are permitted -- and every safety check downstream is the same code
that runs for S1.

What this refuses, and why each refusal is separate
---------------------------------------------------
An empty set is returned, never an exception, for each of:

    S2 is not LIMITED_LIVE            the strategy's status
    the session is not REGULAR        the rollout's reach
    the published set is stale        the day it was written for
    the operator list excludes it     LIVE_ROLLOUT_ALLOWED_SYMBOLS

They are checked separately and logged separately because an operator
reading "no S2 candidates" needs to know WHICH of those it was. A single
combined refusal would make a stood-down strategy look like a quiet
market.

Staleness is the trading day, at this stage
-------------------------------------------
The published file must be for today, compared the same way S1's is. How
fresh a price must be at the moment an order is placed is a different
question, asked later by the shared gate -- guessing at it here would put
a second freshness policy in the codebase.
"""

import logging
from typing import FrozenSet, List, Optional

from config import scanner_live_mode

logger = logging.getLogger(__name__)

SOURCE_S2 = "s2_accumulation"

#: The scan ran and the market was quiet. Wait.
NO_CANDIDATE = "REGULAR scan ran; no symbol met the S2 conditions"
#: No scan ran at all. Waiting will not help.
NO_PRODUCER_RUN = ("no S2 scan ran for this session -- the candidate "
                   "producer is missing, not the candidates")

#: The strategy id the publisher writes and the risk matrix knows.
STRATEGY_ID = "S2_VOLUME_ACCUMULATION_V1"


class S2CandidateSource:
    """Today's published S2 rows, or nothing at all.

    Shares the `CandidateSource` shape used by S1 -- `symbols()`,
    `allowed_symbols()`, `describe()` -- so the buy cycle needs no branch
    for which strategy it is serving. A branch there would be the second
    pipeline the cycle's docstring forbids.
    """

    name = SOURCE_S2

    def __init__(self, *, trading_day, session=None, rollout=None,
                 modes=None):
        self._trading_day = str(trading_day)
        self._session = session
        self._rollout = rollout
        self._modes = modes
        self._rows: Optional[List[dict]] = None
        self._loaded = False
        self._refusal: Optional[str] = None

    # -- refusals, each answered on its own ------------------------------

    def _live_mode_ok(self) -> bool:
        try:
            scanner_live_mode.require_limited_live(
                scanner_live_mode.S2_SCANNER_NAME, self._modes)
            return True
        except scanner_live_mode.ScannerLiveModeError as exc:
            self._refusal = f"S2 is not LIMITED_LIVE: {exc}"
            return False

    def _session_ok(self) -> bool:
        from s2_live import entry_policy
        from scanners.base import scan_session

        normalised = scan_session.normalize(self._session)
        if normalised in entry_policy.S2_LIVE_SESSIONS:
            return True
        self._refusal = (
            f"session {self._session!r} is not enabled for S2 live orders; "
            f"enabled: {sorted(entry_policy.S2_LIVE_SESSIONS)}")
        return False

    def _load(self):
        if self._loaded:
            return self._rows
        self._loaded = True

        if not self._live_mode_ok() or not self._session_ok():
            logger.info("S2 candidate source empty: %s", self._refusal)
            return None

        try:
            from scanners.publish import candidates as publisher

            rows = [row for row in publisher.read(self._trading_day,
                                                  self._session)
                    if str(row.get("strategy_id")) == STRATEGY_ID]
        except Exception:  # noqa: BLE001 - an unreadable file is empty,
            # never an exception that could abort the shared cycle and
            # take S1's entries down with it.
            logger.warning("could not read S2 candidates for %s/%s",
                           self._trading_day, self._session, exc_info=True)
            self._refusal = "candidate file could not be read"
            return None

        if not rows:
            # Two different situations, and they demand opposite
            # responses: a quiet market is waited out, a missing producer
            # is fixed. Without the run marker they read identically --
            # which is how S2 sat at NO_CANDIDATE for a whole session
            # while no REGULAR scan existed to produce anything.
            if publisher.scan_ran(self._trading_day, self._session):
                self._refusal = NO_CANDIDATE
            else:
                self._refusal = NO_PRODUCER_RUN
                logger.error(
                    "no S2 scan ran for %s/%s -- the candidate producer is "
                    "missing, not the candidates", self._trading_day,
                    self._session)
            return None

        # The published file is per (day, session) by construction, but
        # the day is re-checked rather than trusted: a path is not a
        # guarantee, and reusing yesterday's rows is exactly the failure
        # this comparison exists to prevent.
        fresh = [row for row in rows
                 if str(row.get("trading_day")) == self._trading_day]
        if not fresh:
            self._refusal = (
                f"published S2 rows are not for {self._trading_day}")
            logger.warning("S2 candidate source refused: %s", self._refusal)
            return None

        self._rows = sorted(fresh, key=lambda r: (int(r.get("rank") or 10**6),
                                                  str(r.get("symbol") or "")))
        return self._rows

    # -- the CandidateSource interface -----------------------------------

    def _validated_symbols(self) -> FrozenSet[str]:
        rows = self._load()
        if not rows:
            return frozenset()
        symbols = frozenset(str(r.get("symbol") or "").upper() for r in rows)
        operator = getattr(self._rollout, "allowed_symbols", None) or frozenset()
        if operator:
            tightened = symbols & frozenset(s.upper() for s in operator)
            if tightened != symbols:
                logger.info(
                    "S2 candidate set tightened by the operator allow-list: "
                    "%s of %s symbols remain", len(tightened), len(symbols))
            return tightened
        return symbols

    def symbols(self) -> List[str]:
        """Ranked order, not set order -- rank 1 is examined first."""
        rows = self._load()
        if not rows:
            return []
        allowed = self._validated_symbols()
        return [str(r["symbol"]).upper() for r in rows
                if str(r.get("symbol") or "").upper() in allowed]

    def allowed_symbols(self) -> FrozenSet[str]:
        return self._validated_symbols()

    def candidate_row(self, symbol) -> Optional[dict]:
        rows = self._load() or []
        wanted = str(symbol or "").upper()
        for row in rows:
            if str(row.get("symbol") or "").upper() == wanted:
                return row
        return None

    def qualify(self, symbol, *, analyze=None, score_threshold=None):
        """The source-specific step of the shared cycle.

        `analyze`/`score_threshold` are accepted so the cycle can call
        every source identically, and are ignored for the reason S1
        ignores them: applying the legacy score to an S2 candidate would
        make the thing that trades "S2 AND legacy score".

        Everything after this -- instrument validation, the live price
        re-check, the Order Gate, entry limits, the kill switch,
        reconciliation, the Execution Engine -- is shared and is not
        touched here.
        """
        from s2_live import qualification

        return qualification.qualify_s2(
            symbol, candidate_row=self.candidate_row(symbol))

    def describe(self) -> dict:
        """What this source did, for the cycle's audit record."""
        rows = self._load()
        return {
            "source": self.name,
            "strategy_id": STRATEGY_ID,
            "trading_day": self._trading_day,
            "session": self._session,
            "candidates": len(rows or []),
            "allowed": len(self._validated_symbols()),
            "refusal": self._refusal,
        }


#: Strategy ids this module can resolve a source for. Unknown strategies
#: get nothing -- see `resolve_for_strategy`.
KNOWN_STRATEGIES = frozenset({STRATEGY_ID, "S1_HMA_EARLY_TREND_V1"})


class RefusedSource:
    """A source that offers nothing, and says why.

    Returned instead of raising when a strategy cannot be served. The
    shared buy cycle must not learn to catch exceptions from source
    resolution: S1's entries run through that same cycle, and an
    exception raised on S2's behalf would stop them.
    """

    name = "refused"

    def __init__(self, reason, strategy_id=None):
        self._reason = str(reason)
        self._strategy_id = strategy_id

    def symbols(self):
        return []

    def allowed_symbols(self):
        return frozenset()

    def candidate_row(self, symbol):
        return None

    def qualify(self, symbol, *, analyze=None, score_threshold=None):
        """Nothing qualifies from a source that offers nothing.

        Present because the shared cycle calls it on whatever source it
        is given, and a refusal is still a source. Omitting it would turn
        a clean "no candidates" into an AttributeError inside the cycle
        -- which is exactly the failure this class exists to avoid.
        """
        from s1_live.qualification import Qualification

        return Qualification(False, symbol, reason_code="SOURCE_REFUSED",
                             detail=self._reason)

    def describe(self):
        return {"source": self.name, "strategy_id": self._strategy_id,
                "candidates": 0, "allowed": 0, "refusal": self._reason}


def resolve_for_strategy(strategy_id, *, rollout=None, trading_day=None,
                         session=None, env=None, modes=None,
                         watchlist_module=None):
    """The candidate source for one strategy. Never raises.

    Strategy-aware rather than env-aware. The env switch stays exactly
    where it was for S1 -- this delegates to `s1_live.candidate_source.
    resolve()` unchanged, so S1's behaviour is the same code it has been
    running, not a reimplementation that happens to agree today.

    An unknown strategy gets a RefusedSource rather than a default. A
    default here would mean a typo in a caller silently trading somebody
    else's candidates.
    """
    wanted = str(strategy_id or "").strip()

    if wanted == "S1_HMA_EARLY_TREND_V1":
        from s1_live import candidate_source as s1_source

        return s1_source.resolve(rollout, trading_day=trading_day, env=env,
                                 modes=modes,
                                 watchlist_module=watchlist_module)

    if wanted == STRATEGY_ID:
        if not trading_day:
            # The trading day is what makes a stale candidate set
            # detectable; guessing one would defeat the check it exists
            # for. Same rule S1's resolver applies.
            return RefusedSource(
                "no trading day supplied; refusing to guess one",
                strategy_id=wanted)
        return S2CandidateSource(trading_day=trading_day, session=session,
                                 rollout=rollout, modes=modes)

    return RefusedSource(f"unknown strategy {wanted!r}", strategy_id=wanted)
