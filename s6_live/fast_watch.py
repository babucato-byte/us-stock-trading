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
from s6_live import active_watch, cash_precheck, execution_liquidity
from s6_live import pretrade_validation, precision_watch
from s6_live import realtime_features, watch_priority_state
from s6_live.candidate_source import SIGNAL_VALID_SECONDS, SOURCE_S6

logger = logging.getLogger(__name__)

#: S6's premarket input is a one-minute grid (collector store bars and the
#: closed-bar filter in kis_bar_features both use whole minutes).
BAR_WIDTH_MINUTES = 1.0

#: How much of the TICK's own wall clock (measured from `self._now`, the
#: tick's true start -- see __init__) this whole symbols() call may
#: spend: loading/prioritising the watchlist AND the per-symbol
#: evaluation loop, combined. Production evidence 2026-09-09/10: ticks
#: ranging 37-69s with a 41% OVERLAP_SKIPPED rate, traced to the
#: per-symbol Budget being a fresh, independent clock started only
#: AFTER `_load()` already ran -- so a slow load left the eval loop
#: just as much time as a fast one, and the tick's TOTAL cost was
#: whatever the two happened to add up to, unbounded from the tick's
#: own perspective. Constructing the Budget from the time actually
#: LEFT in this deadline (not a fresh window) makes total wall time
#: independent of how large the logical watchlist is: a slow load or
#: a big backlog can only ever shrink how much evaluation happens this
#: tick, never how long the tick itself runs. Leaves ~5-10s headroom
#: under run_live_buy_entry.py's own 50s hard tick budget for
#: _s6_write_intents' write and _funnel's report.
FAST_WATCH_TICK_DEADLINE_SECONDS = 45.0


class ActiveWatchSource:
    name = SOURCE_S6

    def __init__(self, *, trading_day, session, rollout, now=None, conn=None,
                 provider=None, budget_seconds=None, env=None, broker=None):
        self._trading_day = str(trading_day)
        self._session = str(session or "").upper()
        self._rollout = rollout
        self._now = now or datetime.now(timezone.utc)
        self._conn = conn
        self._provider = provider
        self._budget_seconds = budget_seconds
        self._env = env
        # Used only by the cash precheck (§7-9) -- optional, since not
        # every caller (tests, other strategies' factories) has a live
        # broker to hand it. A missing broker makes the precheck
        # UNAVAILABLE, which fails open (see s6_live.cash_precheck).
        self._broker = broker
        # The REAL wall clock at construction, deliberately separate from
        # `self._now` (which callers -- tests especially -- may set to an
        # artificial, fixed timestamp for deterministic evaluation
        # timestamps). The tick's own deadline must be measured against
        # actual elapsed wall time regardless of what `now` means for
        # signal-validity/evaluation purposes; conflating the two made an
        # early version of this budget see a "tick start" days or months
        # in the past whenever a fixed `now` was supplied, and every
        # symbol appeared to already be over budget before the first one
        # was even evaluated.
        self._constructed_at = datetime.now(timezone.utc)
        # The date this session STARTED. For OVERNIGHT_DAYTIME that is not
        # the trading day once the clock passes midnight ET.
        self._scope = active_watch.session_scope(self._session, self._now)
        self._state: Optional[dict] = None
        self._rows: Dict[str, dict] = {}
        self.evaluations: Dict[str, precision_watch.WatchEvaluation] = {}
        self.waiting_for_data: List[str] = []
        self.validation_report: Dict[str, Any] = {}
        self.transport_counts: Dict[str, int] = {}
        # Strategy PASS, execution FAIL (§4-5/§7): symbols the precision
        # watch judged READY but that did not clear the execution
        # liquidity gate or the cash precheck, keyed by symbol ->
        # (reason_code, detail). Never overlaps `_rows`/`ready`.
        self.liquidity_blocked: Dict[str, tuple] = {}
        self.cash_precheck_blocked: Dict[str, tuple] = {}
        self._consumed_at = None
        # Profiling (§8): populated once symbols() actually runs. Zeroed
        # here so a caller reading these before symbols() (or a session
        # this source never scans) gets real numbers, not AttributeError.
        self.load_ms: float = 0.0
        self.eval_loop_ms: float = 0.0
        self.eval_symbol_timings_ms: Dict[str, float] = {}

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

    #: Evaluation is wall-clock budgeted (pretrade_validation.Budget,
    #: shared across every symbol in the tick regardless of transport --
    #: see symbols() below), so with a logical watchlist that can hold
    #: far more than one tick's budget actually reaches, list POSITION
    #: decides who gets judged this minute -- not admission time, not
    #: alphabetical order, and not the raw watchlist-file order (which
    #: exists to protect CAPACITY fairness under the cap, a different
    #: question from EVALUATION priority; active_watch.merge() owns
    #: that ordering and this never touches it, only re-sorts the
    #: symbols this ONE tick iterates for evaluation).
    #:
    #: Priority, highest first (HOT = P0-P1, WARM = P2-P3, COLD = P4):
    #:   P0  HOT   a live PASS from the scan CURRENTLY holding the cycle
    #:             lock (active_watch._live_provisional's own liveness
    #:             gate is what makes strategy_source ==
    #:             PROVISIONAL_SOURCE synonymous with "current run": a
    #:             dead scan's rows are excluded before they ever reach
    #:             the watchlist, and a completed scan's rows have
    #:             already become FULL_DISCOVERY_SOURCE by the time its
    #:             lock releases). This is the whole point of
    #:             incremental admission: a fresh PASS must not queue
    #:             behind an established backlog.
    #:   P1  HOT   READY-near: the LAST tick that actually evaluated this
    #:             symbol (watch_priority_state, not this cycle's own
    #:             strategy verdict) left it READY or one condition away.
    #:             A symbol S6 already almost qualified is worth judging
    #:             again before a symbol S6 has no opinion on at all.
    #:   P2  WARM  S6 already discovered this symbol this session (a
    #:             published, completed-manifest row) -- a real signal,
    #:             just not the one that just happened and not close.
    #:   P3  WARM  no S6 signal at all, but WebSocket-backed: a local
    #:             snapshot read costs no REST budget, so judging it is
    #:             nearly free.
    #:   P4  COLD  no S6 signal, REST-backed: costs the scarce per-tick
    #:             budget for a symbol S6 never actually flagged.
    #: What the tick's Budget cannot reach this minute is deferred, not
    #: invalidated, and is retried from a (possibly changed) priority
    #: position on the next tick -- see symbols() below. Within a tier,
    #: a symbol deferred more consecutive times sorts first (aging/
    #: starvation prevention, watch_priority_state.consecutive_defers):
    #: list position alone would otherwise let a fixed set of
    #: higher-tier symbols starve the same low-tier symbol forever.
    _PROVISIONAL_STRATEGY_SOURCE = "S6_PROVISIONAL_PASS"
    _FULL_DISCOVERY_STRATEGY_SOURCE = "S6_FULL_DISCOVERY"

    #: HOT/WARM/COLD grouping of the numeric tiers above, for reporting
    #: only (§4/§21) -- the numeric tier is what scheduling actually
    #: uses; this never feeds back into it.
    _TIER_GROUP = {0: "HOT", 1: "HOT", 2: "WARM", 3: "WARM", 4: "COLD"}

    @classmethod
    def _priority_tier(cls, entry, *, ready_near: bool = False) -> int:
        strategy = entry.get("strategy_source")
        if strategy == cls._PROVISIONAL_STRATEGY_SOURCE:
            return 0
        if ready_near:
            return 1
        if strategy == cls._FULL_DISCOVERY_STRATEGY_SOURCE:
            return 2
        return 3 if entry.get("transport_source") == active_watch.TRANSPORT_WEBSOCKET else 4

    @classmethod
    def tier_group(cls, tier: int) -> str:
        return cls._TIER_GROUP.get(tier, "COLD")

    def _active_symbols(self) -> List[str]:
        state = self._load()
        if state.get("status") != "ACTIVE":
            return []
        entries = [r for r in state.get("entries") or () if r.get("symbol")]
        scheduling = watch_priority_state.read(self._scope, self._session, env=self._env)

        def _key(pair):
            idx, entry = pair
            symbol = str(entry.get("symbol") or "").upper()
            sched = scheduling.get(symbol) or {}
            tier = self._priority_tier(
                entry, ready_near=watch_priority_state.is_ready_near(sched))
            aging = -watch_priority_state.consecutive_defers(sched)
            return (tier, aging, idx)

        ranked = sorted(enumerate(entries), key=_key)
        return [str(r.get("symbol") or "").upper() for _, r in ranked]

    def _operator_allowed(self, symbols) -> FrozenSet[str]:
        available = frozenset(symbols)
        operator = getattr(self._rollout, "allowed_symbols", None) or frozenset()
        return available & frozenset(str(s).upper() for s in operator) if operator else available

    def allowed_symbols(self) -> FrozenSet[str]:
        return self._operator_allowed(self._active_symbols())

    def symbols(self) -> List[str]:
        load_started_at = datetime.now(timezone.utc)
        offered = [s for s in self._active_symbols() if s in self.allowed_symbols()]
        load_finished_at = datetime.now(timezone.utc)
        self.load_ms = (load_finished_at - load_started_at).total_seconds() * 1000

        # The per-symbol Budget sees what is actually LEFT of the tick's
        # own deadline (measured from self._constructed_at, the REAL
        # wall clock when this source was built -- not self._now, which
        # may be an artificial fixed timestamp), not a fresh window
        # starting whenever this line happens to run. A slow _load() --
        # or a large logical watchlist making the priority sort itself
        # take longer -- can only ever shrink this tick's evaluation
        # allowance, never let the tick's own total wall time grow past
        # FAST_WATCH_TICK_DEADLINE_SECONDS. See the constant's own
        # docstring for the production evidence.
        elapsed_before_eval = (load_finished_at - self._constructed_at).total_seconds()
        remaining = max(0.0, FAST_WATCH_TICK_DEADLINE_SECONDS - elapsed_before_eval)
        configured = (self._budget_seconds if self._budget_seconds is not None
                     else pretrade_validation.budget_seconds())
        budget = pretrade_validation.Budget(min(configured, remaining))

        ready = []
        self.waiting_for_data = []
        self.eval_symbol_timings_ms: Dict[str, float] = {}
        evaluated_state: Dict[str, Dict[str, Any]] = {}
        batch_records = []
        # Transport tally for the funnel report (§14/§17): counted from
        # the watchlist entry each symbol actually carries, not
        # re-derived, so it can never disagree with what active_watch
        # itself classified this cycle.
        self.transport_counts = {active_watch.TRANSPORT_WEBSOCKET: 0,
                                 active_watch.TRANSPORT_REST: 0, "UNKNOWN": 0}
        self.liquidity_blocked = {}
        self.cash_precheck_blocked = {}
        # Loaded once per tick, not per symbol: the same config object
        # `precision_watch.evaluate` already reads for entry_quality's
        # own thresholds.
        from scanners.base import config as scanner_config

        orb_cfg = scanner_config.load_config("orb", scanner_name="orb")
        liquidity_thresholds = execution_liquidity.thresholds_for(orb_cfg, self._session)
        for symbol in offered:
            entry = self._entry(symbol)
            transport = entry.get("transport_source") or "UNKNOWN"
            if not budget.allows():
                budget.defer(symbol)
                self.waiting_for_data.append(symbol)
                continue
            symbol_started_at = datetime.now(timezone.utc)
            self.transport_counts[transport] = self.transport_counts.get(transport, 0) + 1
            evaluated_at = symbol_started_at
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
            self.eval_symbol_timings_ms[symbol] = (
                datetime.now(timezone.utc) - symbol_started_at).total_seconds() * 1000
            evaluated_state[symbol] = {
                "state": evaluation.state,
                "blocking_count": len(evaluation.blocking or ()),
            }
            if evaluation.ready:
                # Read from evaluation.features, not the outer `features`
                # local: `precision_watch.evaluate` is what actually
                # decided READY, and it (not this loop) owns whether the
                # view it judged is the same object passed in.
                ready_feats = evaluation.features
                quality = getattr(ready_feats, "entry_quality", None)
                liq_verdict, liq_code, liq_detail = execution_liquidity.assess(
                    quality, liquidity_thresholds)
                if liq_verdict != execution_liquidity.PASS:
                    self.liquidity_blocked[symbol] = (liq_code, liq_detail)
                    logger.info(
                        "S6 execution liquidity: %s strategy PASS, execution "
                        "%s (%s)", symbol, liq_verdict, liq_code)
                else:
                    cash_status, cash_detail = cash_precheck.check(
                        symbol, getattr(ready_feats, "price", None),
                        broker=self._broker, now=evaluated_at, env=self._env)
                    if cash_status == cash_precheck.BLOCKED:
                        self.cash_precheck_blocked[symbol] = (
                            cash_precheck.INSUFFICIENT_CASH_PRECHECK, cash_detail)
                        logger.info(
                            "S6 cash precheck: %s blocked (available=%s "
                            "required=%s)", symbol,
                            cash_detail.get("available_cash"),
                            cash_detail.get("required_for_1_share"))
                    else:
                        self._rows[symbol] = self._row_from(evaluation)
                        ready.append(symbol)
            batch_records.append({
                "symbol": symbol, "session": self._session,
                "trading_day": self._trading_day,
                "session_date": self._scope,
                "scanner_variant": s6_sessions.scanner_variant_for(self._session),
                "strategy_source": entry.get("strategy_source"),
                "transport_source": transport,
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
            })
        # One flock/write for every symbol this tick evaluated, not one
        # per symbol -- see active_watch.record_evaluations_batch.
        active_watch.record_evaluations_batch(
            self._scope, batch_records, session=self._session, env=self._env)
        # Aging state for next tick's priority sort: resets for what was
        # actually reached, increments for what the budget deferred.
        watch_priority_state.update(
            self._scope, self._session, evaluated=evaluated_state,
            deferred=self.waiting_for_data, now=load_finished_at, env=self._env)
        self.validation_report = budget.report()
        self.eval_loop_ms = (datetime.now(timezone.utc) - load_finished_at).total_seconds() * 1000
        logger.info(
            "S6_FAST_WATCH_PROFILE load_ms=%.0f eval_loop_ms=%.0f "
            "evaluated=%d deferred=%d budget_seconds=%.1f",
            self.load_ms, self.eval_loop_ms, len(self.evaluations),
            len(self.waiting_for_data), self.validation_report.get("budget_seconds") or 0.0)
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

    def _tier_group_counts(self, state) -> Dict[str, int]:
        """HOT/WARM/COLD counts across the WHOLE logical watch (§4/§21),
        not just what this tick reached -- so a caller can see the
        backlog shape even on a tick that evaluated almost none of it."""
        entries = [r for r in state.get("entries") or () if r.get("symbol")]
        scheduling = watch_priority_state.read(self._scope, self._session, env=self._env)
        counts = {"HOT": 0, "WARM": 0, "COLD": 0}
        for entry in entries:
            symbol = str(entry.get("symbol") or "").upper()
            sched = scheduling.get(symbol) or {}
            tier = self._priority_tier(
                entry, ready_near=watch_priority_state.is_ready_near(sched))
            counts[self.tier_group(tier)] += 1
        return counts

    def describe(self):
        state = self._load()
        return {
            "source": self.name,
            "runtime": "S6_ACTIVE_WATCH",
            "trading_day": self._trading_day,
            "session": self._session,
            "watchlist_status": state.get("status"),
            "watchlist_size": len(state.get("entries") or ()),
            "watchlist_capacity": state.get("capacity", active_watch.MAX_LOGICAL_WATCH_SYMBOLS),
            "websocket_subscription_cap": active_watch.MAX_SUBSCRIPTIONS,
            "websocket_backed": state.get("websocket_backed"),
            "rest_backed": state.get("rest_backed"),
            "deferred": len(self.waiting_for_data),
            "fast_evaluated": len(self.evaluations),
            "transport_counts": dict(self.transport_counts),
            "tier_counts": self._tier_group_counts(state),
            "load_ms": round(self.load_ms, 1),
            "eval_loop_ms": round(self.eval_loop_ms, 1),
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
            "liquidity_blocked": {s: {"reason_code": code, "detail": detail}
                                  for s, (code, detail) in self.liquidity_blocked.items()},
            "cash_precheck_blocked": {s: {"reason_code": code, "detail": detail}
                                      for s, (code, detail) in self.cash_precheck_blocked.items()},
        }
