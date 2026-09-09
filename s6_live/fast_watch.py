"""S6 active-watch source for the existing shared BUY cycle.

This is the fast second stage, and it is the SAME engine in every S6
session: it resolves the current session, that session's official origin
and its own scoped watchlist, builds the session's live range on closed
bars, and offers only READY symbols to the unchanged shared cycle.  It
does not scan a universe and it owns no order code.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, List, Optional

from config import s6_sessions, scanner_live_mode
from s6_live import active_watch, pretrade_validation, precision_watch
from s6_live import realtime_features
from s6_live.candidate_source import SIGNAL_VALID_SECONDS, SOURCE_S6

logger = logging.getLogger(__name__)

#: S6's premarket input is a one-minute grid (collector store bars and the
#: closed-bar filter in kis_bar_features both use whole minutes).
BAR_WIDTH_MINUTES = 1.0


class ActiveWatchSource:
    name = SOURCE_S6

    def __init__(self, *, trading_day, session, rollout, now=None, conn=None,
                 provider=None, budget_seconds=None, env=None):
        self._trading_day = str(trading_day)
        self._session = str(session or "").upper()
        self._rollout = rollout
        self._now = now or datetime.now(timezone.utc)
        self._conn = conn
        self._provider = provider
        self._budget_seconds = budget_seconds
        self._env = env
        # The date this session STARTED. For OVERNIGHT_DAYTIME that is not
        # the trading day once the clock passes midnight ET.
        self._scope = active_watch.session_scope(self._session, self._now)
        self._state: Optional[dict] = None
        self._rows: Dict[str, dict] = {}
        self.evaluations: Dict[str, precision_watch.WatchEvaluation] = {}
        self.waiting_for_data: List[str] = []
        self.validation_report: Dict[str, Any] = {}
        self._consumed_at = None

    def _load(self):
        if self._state is not None:
            return self._state
        if self._session not in s6_sessions.SCAN_SESSIONS:
            self._state = {"status": "WRONG_SESSION", "entries": []}
            return self._state
        if self._scope is None:
            # No canonical window for this session means no origin and no
            # lifetime; refusing is the only safe answer.
            self._state = {"status": "NO_SESSION_SCOPE", "entries": []}
            return self._state
        try:
            scanner_live_mode.require_limited_live(s6_sessions.SCANNER_NAME)
            self._state = active_watch.refresh_from_existing_sources(
                self._scope, session=self._session,
                trading_day=self._trading_day, now=self._now, env=self._env)
        except Exception as exc:
            logger.warning("S6 active-watch unavailable", exc_info=True)
            self._state = {"status": "UNAVAILABLE", "entries": [],
                           "reason": str(exc)}
        self._consumed_at = datetime.now(timezone.utc)
        return self._state

    def _active_symbols(self) -> List[str]:
        state = self._load()
        if state.get("status") != "ACTIVE":
            return []
        return [str(r.get("symbol") or "").upper()
                for r in state.get("entries") or () if r.get("symbol")]

    def _operator_allowed(self, symbols) -> FrozenSet[str]:
        available = frozenset(symbols)
        operator = getattr(self._rollout, "allowed_symbols", None) or frozenset()
        return available & frozenset(str(s).upper() for s in operator) if operator else available

    def allowed_symbols(self) -> FrozenSet[str]:
        return self._operator_allowed(self._active_symbols())

    def symbols(self) -> List[str]:
        offered = [s for s in self._active_symbols() if s in self.allowed_symbols()]
        budget = pretrade_validation.Budget(self._budget_seconds)
        ready = []
        self.waiting_for_data = []
        for symbol in offered:
            if not budget.allows():
                budget.defer(symbol)
                self.waiting_for_data.append(symbol)
                continue
            evaluated_at = datetime.now(timezone.utc)
            live_minutes = s6_sessions.orb_minutes_for(self._session)
            features = realtime_features.build(
                symbol, session=self._session, now=evaluated_at, provider=None,
                range_minutes=live_minutes, closed_bar_only=True)
            # Current discovery may admit a symbol outside the static stream,
            # and REGULAR is not a KIS-authoritative session at all. Only that
            # bounded set falls back to the measured REST path.
            if features.market_data_asof is None and self._provider is not None:
                features = realtime_features.build(
                    symbol, session=self._session, now=evaluated_at,
                    provider=self._provider, range_minutes=live_minutes,
                    closed_bar_only=True)
            evaluation = precision_watch.evaluate(
                symbol, session=self._session, now=evaluated_at, conn=self._conn,
                features=features, require_scanner_thesis=True)
            budget.spent_on(symbol)
            self.evaluations[symbol] = evaluation
            if evaluation.ready:
                self._rows[symbol] = self._row_from(evaluation)
                ready.append(symbol)
            entry = self._entry(symbol)
            active_watch.record_evaluation(self._scope, {
                "symbol": symbol, "session": self._session,
                "trading_day": self._trading_day,
                "session_date": self._scope,
                "scanner_variant": s6_sessions.scanner_variant_for(self._session),
                "orb_minutes": live_minutes,
                "provider": getattr(features, "price_source", None),
                "bar_interval_minutes": BAR_WIDTH_MINUTES,
                "watchlist_added_at": entry.get("added_at"),
                "fast_watch_evaluated_at": evaluated_at.isoformat(),
                "candidate_published_at": (evaluated_at.isoformat()
                                           if evaluation.ready else None),
                "watch_state": evaluation.state,
                "blocking": list(evaluation.blocking or ()),
                "market_data_asof": (features.market_data_asof.isoformat()
                                     if features.market_data_asof else None),
                "closed_bar_only": features.closed_bar_only,
                "range_origin_timestamp": (features.range_origin_timestamp.isoformat()
                                           if features.range_origin_timestamp else None),
            }, session=self._session, env=self._env)
        self.validation_report = budget.report()
        return ready

    def _row_from(self, evaluation) -> dict:
        feats = evaluation.features
        quality = getattr(feats, "entry_quality", None)
        breakout = getattr(quality, "first_breakout_at", None)
        evaluated = evaluation.evaluated_at or self._now
        # One breakout opportunity keeps one durable signal id across minute
        # ticks; a genuinely new breakout after re-entry receives a new id.
        identity = "%s|%s|%s|%s" % (
            self._trading_day, evaluation.symbol, self._session,
            breakout.isoformat() if breakout else "unknown")
        signal_id = "s6aw-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        source_at = getattr(feats, "market_data_asof", None)
        # A closed bar is actionable only after its interval ends, so the
        # timestamp supplied to signal-validity is the bar's CLOSE, not the
        # bar-open timestamp stored in the frame.  The width of that bar is
        # one minute by construction -- both the collector store and the
        # closed-bar filter work on a one-minute grid.  It is deliberately
        # NOT `entry_quality.bar_interval_minutes`: that measures the
        # observed cadence between traded minutes, and premarket bars are
        # sparse, so on real data its median runs to 5-60 minutes and would
        # stamp this signal minutes into the future.
        bar_open = getattr(quality, "source_timestamp", None)
        signal_at = (bar_open + timedelta(minutes=BAR_WIDTH_MINUTES)
                     if bar_open is not None else source_at or evaluated)
        entry = self._entry(evaluation.symbol)
        return {
            "strategy_id": s6_sessions.STRATEGY_ID,
            "scanner_run_id": "ACTIVE_WATCH",
            "trading_day": self._trading_day,
            "session": self._session,
            "generated_at": evaluated.isoformat(),
            "symbol": evaluation.symbol,
            "rank": None,
            "score": None,
            "price": feats.price,
            "market_data_asof": source_at.isoformat() if source_at else None,
            "variant": s6_sessions.variant_for(self._session),
            "scanner_variant": s6_sessions.scanner_variant_for(self._session),
            "range_minutes": feats.range_minutes,
            "range_high": feats.range_high,
            "range_low": feats.range_low,
            "vwap": feats.vwap,
            "ema9": feats.ema9,
            "ema21": feats.ema21,
            "volume_expansion": feats.volume_expansion,
            "extension_pct": feats.extension_pct,
            "entry_quality": quality.as_record() if quality else None,
            "provenance": {
                "signal_id": signal_id,
                "signal_timestamp": signal_at.isoformat(),
                "source": "S6_ACTIVE_WATCH",
                "full_scan_started_at": entry.get("full_scan_started_at"),
                "symbol_evaluated_at": entry.get("symbol_evaluated_at"),
                "candidate_discovered_at": entry.get("candidate_discovered_at"),
                "watchlist_added_at": entry.get("added_at"),
                "fast_watch_evaluated_at": evaluated.isoformat(),
                "candidate_published_at": evaluated.isoformat(),
                "source_consumed_at": evaluated.isoformat(),
                "precision_watch_started_at": evaluated.isoformat(),
                "discovery_generation": entry.get("discovery_generation"),
            },
        }

    def _entry(self, symbol) -> dict:
        for row in (self._state or {}).get("entries") or ():
            if str(row.get("symbol") or "").upper() == str(symbol).upper():
                return row
        return {}

    def candidate_row(self, symbol) -> Optional[dict]:
        return self._rows.get(str(symbol or "").upper())

    def qualify(self, symbol, *, analyze=None, score_threshold=None):
        from s6_live.qualification import qualify_s6
        return qualify_s6(symbol, candidate_row=self.candidate_row(symbol))

    def signal_valid_seconds(self):
        return SIGNAL_VALID_SECONDS

    def describe(self):
        state = self._load()
        return {
            "source": self.name,
            "runtime": "S6_ACTIVE_WATCH",
            "trading_day": self._trading_day,
            "session": self._session,
            "watchlist_status": state.get("status"),
            "watchlist_size": len(state.get("entries") or ()),
            "watchlist_capacity": state.get("capacity", active_watch.MAX_SYMBOLS),
            "session_date": self._scope,
            "scanner_variant": s6_sessions.scanner_variant_for(self._session),
            "shadow_variant": (s6_sessions.SHADOW_SCANNER_VARIANT
                               if s6_sessions.shadow_orb_minutes_for(self._session)
                               else None),
            "collector_state": (state.get("metadata") or {}).get("collector_state"),
            "subscription_count": (state.get("metadata") or {}).get("subscription_count"),
            "candidate_consumed_at": (self._consumed_at.isoformat()
                                      if self._consumed_at else None),
            "precision_watch": {s: {"state": e.state, "blocking": e.blocking}
                                for s, e in self.evaluations.items()},
        }
