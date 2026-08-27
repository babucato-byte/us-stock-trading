"""S6's candidates, offered to the SHARED buy cycle.

The same shape S2 uses, for the same reason: only the SOURCE is
pluggable, and a second candidate source must never mean a second
pipeline. Everything below -- COMMON_STOCK, orderable cash,
reconciliation, duplicate orders, the kill switch, position limits, the
execution-price gate -- is the shared cycle's and is not touched here.

One thing S2 did not need: VARIANT
----------------------------------
S6 runs the same scanner in four sessions, and each forms its own range.
A candidate is therefore identified by (trading_day, session, variant),
not by day alone -- an S6-O row reaching an S6-R evaluation would be a
breakout of a level nobody in the regular session traded against.
The variant is re-checked here rather than trusted to the file path,
because a path is not a guarantee.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional

from config import s6_sessions, scanner_live_mode

logger = logging.getLogger(__name__)

SOURCE_S6 = "s6_orb_breakout"
STRATEGY_ID = s6_sessions.STRATEGY_ID

#: The scan ran and no symbol broke out. Wait.
NO_CANDIDATE = "scan ran; no symbol met the S6 breakout conditions"
#: No scan ran at all. Waiting will not help.
NO_PRODUCER_RUN = ("no S6 scan ran for this session -- the candidate "
                   "producer is missing, not the candidates")



def _latest_generation(rows):
    """Only the newest scan's rows. Older generations are superseded.

    The published file is append-only for the whole session: a scan
    every fifteen minutes writes its complete candidate set, so by
    mid-afternoon one REGULAR session holds seventeen generations and
    229 rows. Filtering on trading day and variant alone keeps all of
    them, and the entry cycle then sees the same symbol several times --
    STE three times, ROP three times, SM ten -- once per generation it
    ever appeared in.

    Two things go wrong with that, and the cheap one is the cost: each
    repeat spends a full KIS round trip, and a cycle that should take
    two minutes takes seven, in a window the scanner is already
    occupying most of.

    The expensive one is that the older copies are WRONG. A candidate
    row carries the ORB range, breakout price, rank and score the scan
    computed at the time. Evaluating a symbol against a two-hour-old row
    judges it against a range the market has since left -- which is the
    exact failure that let DT be re-offered every fifteen minutes on
    market data that had not changed since morning.

    A scan publishes a COMPLETE set, so "newest generation" is the right
    unit rather than "newest row per symbol": a symbol the latest scan
    did not publish is not a candidate any more, and keeping its last
    appearance would quietly resurrect it.

    Rows with no `generated_at` cannot be ordered, so they are treated
    as one generation of their own and kept only if nothing else
    qualifies -- an unstamped file still trades rather than silently
    becoming empty.
    """
    stamped = [r for r in rows if str(r.get("generated_at") or "")]
    if not stamped:
        # Nothing to order by. Deduplicate on symbol keeping the LAST
        # occurrence, which is newest in an append-only file.
        seen = {}
        for row in rows:
            seen[str(row.get("symbol") or "").upper()] = row
        return list(seen.values())

    newest = max(str(r.get("generated_at")) for r in stamped)
    latest = [r for r in stamped if str(r.get("generated_at")) == newest]
    if len(latest) < len(rows):
        logger.info("S6 candidate set: %s of %s published rows are the "
                    "newest generation (%s); the rest are superseded",
                    len(latest), len(rows), newest)
    return latest

class S6CandidateSource:
    """This session's published S6 rows, or nothing at all."""

    name = SOURCE_S6

    def __init__(self, *, trading_day, session=None, rollout=None,
                 modes=None):
        self._trading_day = str(trading_day)
        self._session = session
        self._variant = s6_sessions.variant_for(session)
        self._rollout = rollout
        self._modes = modes
        self._rows: Optional[List[dict]] = None
        self._loaded = False
        self._refusal: Optional[str] = None
        # Stamped when the rows are actually READ, not when the source is
        # constructed. The age that matters is between publication and
        # use, and a constructor that ran early would understate it.
        self._consumed_at: Optional[datetime] = None
        #: What the scan-cycle check saw, kept so the audit record can
        #: state it rather than re-deriving it a second time.
        self._cycle_state: Optional[Dict[str, Any]] = None

    def _live_mode_ok(self) -> bool:
        try:
            scanner_live_mode.require_limited_live(
                s6_sessions.SCANNER_NAME, self._modes)
            return True
        except scanner_live_mode.ScannerLiveModeError as exc:
            self._refusal = f"S6 is not LIMITED_LIVE: {exc}"
            return False

    def _session_ok(self) -> bool:
        if s6_sessions.orders_allowed(self._session):
            return True
        # Scanning happens in every session; ordering does not. A shadow
        # session offering symbols to the buy cycle would erase that
        # distinction at the one point where it matters.
        self._refusal = (
            f"session {self._session!r} is {s6_sessions.mode_for(self._session)}; "
            f"orders are enabled only in {sorted(s6_sessions.LIVE_SESSIONS)}")
        return False

    def _cycle_ok(self) -> bool:
        """Is the file on disk this cycle's answer, or the last one's?

        S6 scans every fifteen minutes and consumes five minutes later,
        so a scan that runs long leaves a file whose trading day, session
        and variant are all CORRECT and whose contents are superseded.
        Every other refusal in this class is keyed on one of those three,
        which is why none of them catches it.

        Two facts are enough, and neither is a threshold. A scan holding
        its cycle lock is producing the answer that replaces this file --
        so the file is the previous cycle's and is refused. A scan that
        ended FAILED produced no answer at all -- so whatever is on disk
        predates it and is refused too.

        Refusing is only ever about a new BUY. Nothing here is reachable
        from an exit: `s6_live.exit_runtime` reads no candidate file.
        """
        from scanners.publish import scan_cycle

        try:
            state = scan_cycle.state(self._trading_day, self._session,
                                     scanner=s6_sessions.SCANNER_NAME)
        except Exception as exc:  # noqa: BLE001 - the source must never
            # raise into the shared cycle; an unanswerable question is
            # refused, which is the same direction as every other check.
            self._refusal = f"scan state could not be established: {exc}"
            return False

        self._cycle_state = state.as_dict()
        if state.blocks_consumption:
            self._refusal = state.refusal()
            return False

        try:
            ok, detail = scan_cycle.last_run_consumable(
                self._trading_day, self._session, strategy_id=STRATEGY_ID)
        except Exception as exc:  # noqa: BLE001
            self._refusal = f"last scan status could not be read: {exc}"
            return False
        if not ok:
            self._refusal = detail
            return False
        return True

    def _load(self):
        if self._loaded:
            return self._rows
        self._loaded = True

        if not self._live_mode_ok() or not self._session_ok():
            logger.info("S6 candidate source empty: %s", self._refusal)
            return None

        if not self._cycle_ok():
            logger.warning("S6 candidate source refused: %s", self._refusal)
            return None

        try:
            from scanners.publish import candidates as publisher

            rows = [r for r in publisher.read(self._trading_day, self._session)
                    if str(r.get("strategy_id")) == STRATEGY_ID]
        except Exception:  # noqa: BLE001 - an unreadable file is empty,
            # never an exception that could abort the shared cycle and
            # take S1's entries down with it.
            logger.warning("could not read S6 candidates for %s/%s",
                           self._trading_day, self._session, exc_info=True)
            self._refusal = "candidate file could not be read"
            return None

        if not rows:
            if publisher.scan_ran(self._trading_day, self._session):
                self._refusal = NO_CANDIDATE
            else:
                self._refusal = NO_PRODUCER_RUN
                logger.error("no S6 scan ran for %s/%s", self._trading_day,
                             self._session)
            return None

        fresh = [r for r in rows
                 if str(r.get("trading_day")) == self._trading_day
                 and str(r.get("variant") or "") == self._variant]
        if not fresh:
            self._refusal = (
                f"published S6 rows are not {self._variant} for "
                f"{self._trading_day}")
            logger.warning("S6 candidate source refused: %s", self._refusal)
            return None

        fresh = _latest_generation(fresh)

        self._consumed_at = datetime.now(timezone.utc)
        self._rows = sorted(fresh, key=lambda r: (int(r.get("rank") or 10**6),
                                                  str(r.get("symbol") or "")))
        return self._rows

    def _validated_symbols(self) -> FrozenSet[str]:
        rows = self._load()
        if not rows:
            return frozenset()
        symbols = frozenset(str(r.get("symbol") or "").upper() for r in rows)
        operator = getattr(self._rollout, "allowed_symbols", None) or frozenset()
        if operator:
            tightened = symbols & frozenset(s.upper() for s in operator)
            if tightened != symbols:
                logger.info("S6 candidate set tightened by the operator "
                            "allow-list: %s of %s remain", len(tightened),
                            len(symbols))
            return tightened
        return symbols

    def symbols(self) -> List[str]:
        rows = self._load()
        if not rows:
            return []
        allowed = self._validated_symbols()
        return [str(r["symbol"]).upper() for r in rows
                if str(r.get("symbol") or "").upper() in allowed]

    def allowed_symbols(self) -> FrozenSet[str]:
        return self._validated_symbols()

    def candidate_row(self, symbol) -> Optional[dict]:
        wanted = str(symbol or "").upper()
        for row in self._load() or []:
            if str(row.get("symbol") or "").upper() == wanted:
                return row
        return None

    def qualify(self, symbol, *, analyze=None, score_threshold=None):
        """The source-specific step. `analyze`/`score_threshold` ignored
        for the reason S1 and S2 ignore them: a second, unrelated score
        would make the thing that trades "S6 AND legacy score"."""
        from s6_live import qualification

        return qualification.qualify_s6(
            symbol, candidate_row=self.candidate_row(symbol))

    def freshness(self) -> Dict[str, Any]:
        """When the rows were made and when they were used.

        Reported rather than enforced: the trading-day and session checks
        above are this stage's staleness policy, and how old a price may
        be at the moment an order is placed is the shared gate's
        question. Inventing a second age limit here would put two
        freshness policies in the codebase.
        """
        rows = self._rows or []
        generated = rows[0].get("generated_at") if rows else None
        age = None
        if generated and self._consumed_at is not None:
            try:
                made = datetime.fromisoformat(
                    str(generated).replace("Z", "+00:00"))
                if made.tzinfo is None:
                    made = made.replace(tzinfo=timezone.utc)
                age = (self._consumed_at - made).total_seconds()
            except Exception:  # noqa: BLE001 - a display value must not
                # be able to fail the source that carries it.
                age = None
        return {
            "candidate_generated_at": generated,
            "candidate_consumed_at": (self._consumed_at.isoformat()
                                      if self._consumed_at else None),
            "candidate_age_at_consume_seconds": age,
        }

    def describe(self) -> dict:
        rows = self._load()
        return {
            "source": self.name, "strategy_id": STRATEGY_ID,
            **self.freshness(),
            "variant": self._variant, "trading_day": self._trading_day,
            "session": self._session, "candidates": len(rows or []),
            "allowed": len(self._validated_symbols()),
            "mode": s6_sessions.mode_for(self._session),
            "scan_state": self._cycle_state,
            "refusal": self._refusal,
        }
