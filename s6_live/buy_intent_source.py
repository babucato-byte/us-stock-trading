"""The execution worker's candidate source: claimed BUY_INTENT rows.

Implements the SAME source interface `kis_live_trading.run_live_buy_entry_cycle`
already accepts from the fast-watch source in `s6_live/fast_watch.py`
-- `.name`, `.symbols()`, `.allowed_symbols()`, `.candidate_row()`,
`.qualify()`, `.signal_valid_seconds()`, `.describe()` -- so the shared
cycle runs completely unchanged for these candidates. The only
difference from that fast-watch source is where `.symbols()` gets its
list: instead of running the WATCHING/READY evaluation itself, it
claims whatever fast-watch already decided was READY and wrote to the
`s6_live.buy_intent` queue.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional

from s6_live import buy_intent
from s6_live.candidate_source import SIGNAL_VALID_SECONDS, SOURCE_S6

logger = logging.getLogger(__name__)


class IntentQueueSource:
    name = SOURCE_S6

    def __init__(self, *, trading_day, session, rollout, now=None, env=None):
        self._trading_day = str(trading_day)
        self._session = str(session or "").upper()
        self._rollout = rollout
        self._now = now or datetime.now(timezone.utc)
        self._env = env
        self._claimed: Dict[str, Dict[str, Any]] = {}
        self._rows: Dict[str, dict] = {}
        self._claimed_once = False
        # No fresh WATCHING/READY evaluation happens here -- these
        # candidates were already judged READY by a fast-watch tick.
        # Kept empty (not omitted) because callers -- `_funnel`,
        # `_log_s6_transport_funnel` in scripts/run_live_buy_entry.py --
        # already handle an empty `evaluations` correctly; a worker-
        # specific funnel is used instead of forcing this shape to fit.
        self.evaluations: Dict[str, Any] = {}

    def _operator_allowed(self, symbols) -> FrozenSet[str]:
        available = frozenset(symbols)
        operator = getattr(self._rollout, "allowed_symbols", None) or frozenset()
        return available & frozenset(str(s).upper() for s in operator) if operator else available

    def _ensure_claimed(self) -> None:
        """Claim the queue exactly once per instance, whichever method
        is asked first. `run_live_buy_entry_cycle` calls
        `.allowed_symbols()` BEFORE `.symbols()` -- an earlier version
        of this class populated `self._claimed` only inside `.symbols()`,
        so `.allowed_symbols()` always saw an empty claim and every
        candidate was refused as "not in live_rollout.allowed_symbols"
        regardless of the operator's actual configuration. A production
        worker tick on 2026-09-09 reproduced exactly that: real,
        already-claimed candidates (IOT, OWL, UMC, VIST) all refused
        this way, with no operator restriction actually in effect.
        A boolean flag, not `if not self._claimed`, because an
        genuinely empty queue must not look unclaimed and trigger a
        second, redundant claim attempt.
        """
        if self._claimed_once:
            return
        self._claimed_once = True
        self._claimed = buy_intent.claim_ready(
            self._trading_day, self._session, env=self._env)
        for symbol, entry in self._claimed.items():
            candidate = dict((entry or {}).get("candidate") or {})
            candidate.setdefault("symbol", symbol)
            self._rows[symbol] = candidate

    def allowed_symbols(self) -> FrozenSet[str]:
        self._ensure_claimed()
        return self._operator_allowed(self._claimed)

    def symbols(self) -> List[str]:
        self._ensure_claimed()
        return sorted(self._rows)

    def candidate_row(self, symbol) -> Optional[dict]:
        return self._rows.get(str(symbol or "").upper())

    def claimed_symbols(self) -> List[str]:
        return sorted(self._rows)

    def qualify(self, symbol, *, analyze=None, score_threshold=None):
        from s6_live.qualification import qualify_s6
        return qualify_s6(symbol, candidate_row=self.candidate_row(symbol))

    def signal_valid_seconds(self):
        return SIGNAL_VALID_SECONDS

    def intent_metadata(self, symbol) -> Dict[str, Any]:
        """first_ready_at/last_seen_ready_at for the execution funnel's
        READY -> BUY_INTENT -> execution latency report (§18)."""
        return dict(self._claimed.get(str(symbol or "").upper()) or {})

    def describe(self):
        return {
            "source": self.name,
            "runtime": "S6_BUY_INTENT_EXECUTION",
            "trading_day": self._trading_day,
            "session": self._session,
            "claimed": len(self._rows),
        }
