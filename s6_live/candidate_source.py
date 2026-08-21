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
from typing import FrozenSet, List, Optional

from config import s6_sessions, scanner_live_mode

logger = logging.getLogger(__name__)

SOURCE_S6 = "s6_orb_breakout"
STRATEGY_ID = s6_sessions.STRATEGY_ID

#: The scan ran and no symbol broke out. Wait.
NO_CANDIDATE = "scan ran; no symbol met the S6 breakout conditions"
#: No scan ran at all. Waiting will not help.
NO_PRODUCER_RUN = ("no S6 scan ran for this session -- the candidate "
                   "producer is missing, not the candidates")


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

    def _load(self):
        if self._loaded:
            return self._rows
        self._loaded = True

        if not self._live_mode_ok() or not self._session_ok():
            logger.info("S6 candidate source empty: %s", self._refusal)
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

    def describe(self) -> dict:
        rows = self._load()
        return {
            "source": self.name, "strategy_id": STRATEGY_ID,
            "variant": self._variant, "trading_day": self._trading_day,
            "session": self._session, "candidates": len(rows or []),
            "allowed": len(self._validated_symbols()),
            "mode": s6_sessions.mode_for(self._session),
            "refusal": self._refusal,
        }
