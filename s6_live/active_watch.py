"""Persisted, bounded S6 symbols for the fast evaluation path.

One store, one shape, every S6 session.  The full scanner remains the
broad discovery producer.  This store is a small hand-off containing only
names that the existing KIS collector can actually keep current, plus
completed S6 discoveries when capacity remains.  It has no broker or
execution dependency and never decides that a symbol is buyable.

Scope
-----
A watchlist belongs to ONE (session start date, session).  The scope key
is the date the session STARTED, not the trading day: OVERNIGHT_DAYTIME
opens at 20:00 ET and runs past midnight, so `us_trading_day` changes
underneath it, and keying on that would split one session into two
watchlists at 00:00 and silently restart its admission.  The trading day
is still what the SCANNER keyed its published candidates by, so the
refresh takes it separately.
"""

import fcntl
import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from market_data import kis_hdfscnt0 as wire

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "s6_active_watch_v2"
SUBDIR = "s6_active_watch"
EASTERN = ZoneInfo("America/New_York")

#: The PHYSICAL WebSocket transport ceiling. One appkey streams at most
#: this many symbols -- measured, not chosen -- and it is untouched and
#: unreachable from this module: the actual subscription REQUEST is made
#: by market_data.bootstrap_watchlist (pre-session symbol selection,
#: capped the same way) and scripts/run_realtime_bar_collector.py (which
#: refuses subscription 42 outright). Kept here ONLY as a read-only
#: reference so this module can classify an entry as WS-backed vs
#: REST-backed; nothing in this file ever requests a subscription.
MAX_SUBSCRIPTIONS = wire.MAX_SUBSCRIPTIONS

#: The LOGICAL active-watch capacity -- how many symbols the fast-watch
#: tick may hold under evaluation at once. Deliberately NOT
#: MAX_SUBSCRIPTIONS: that conflation is the defect this constant exists
#: to remove. Only WebSocket-backed entries are limited to 41 (a
#: transport fact enforced elsewhere, see MAX_SUBSCRIPTIONS above); a
#: symbol outside that set is REST-evaluated within fast_watch's existing
#: 30-second pretrade budget (S6_PRETRADE_BUDGET_SECONDS, ~12 symbols per
#: tick at the measured 2.44s/symbol chart-read cost -- see
#: pretrade_validation and kis_minute_chart), which defers what it cannot
#: afford to the next tick rather than dropping it.
#:
#: 120 = 41 (physical WS ceiling) plus headroom sized against that same
#: REST throughput: roughly ten ticks' worth (~12/tick) of non-WS
#: backlog, comfortably longer than the few minutes it takes a fresh
#: PASS to reach the front of the queue (see the fast-watch source's own
#: symbol-ordering method, which puts S6-discovered entries ahead of bare
#: collector-membership ones), while staying a small, fixed, auditable
#: number rather than an unbounded one. Measured against real production
#: PASS rates (11 in one ~16-minute PREMARKET scan segment, 2026-09-09),
#: this is generous headroom, not a tight fit.
MAX_LOGICAL_WATCH_SYMBOLS = 120

#: How a watched symbol's market data actually arrives THIS cycle --
#: independent of WHY it is being watched. Recomputed every refresh from
#: the collector's live subscription set, so a symbol that rotates in or
#: out of the WebSocket stream is reclassified automatically.
TRANSPORT_WEBSOCKET = "KIS_WEBSOCKET"
TRANSPORT_REST = "KIS_REST"

#: Incremental (mid-scan) PASS admissions, separate from the main
#: active-watch file so a scanner still running does not contend for the
#: same lock the 1-minute refresh cycle takes on every tick, and so a
#: refresh's `replace=True` rebuild (below) never has to know this file
#: exists to avoid clobbering it -- it reads this store as one more
#: SOURCE, exactly like the collector and the completed manifest.
PROVISIONAL_SCHEMA_VERSION = "s6_active_watch_provisional_v1"
PROVISIONAL_SOURCE = "S6_PROVISIONAL_PASS"

#: The completed-manifest strategy_source, referenced by name in more
#: than one place below.
FULL_DISCOVERY_SOURCE = "S6_FULL_DISCOVERY"

#: Set ONLY when a symbol has no S6 discovery claim at all -- neither a
#: live provisional PASS nor a completed-manifest row -- and is watched
#: purely because the collector already streams it. Distinct from
#: TRANSPORT_WEBSOCKET: this is a STRATEGY reason (why watched), that is
#: a TRANSPORT fact (how its data arrives); a symbol can be
#: WebSocket-backed for either reason.
COLLECTOR_MEMBERSHIP_SOURCE = "KIS_COLLECTOR_MEMBERSHIP"

#: Kept only as the default for callers that name no session.
SESSION = "PREMARKET"


def _utc(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _root(env=None) -> Path:
    env = env if env is not None else os.environ
    root = env.get("S6_ACTIVE_WATCH_DIR") or env.get("SCANNER_DATA_ROOT")
    if not root:
        raise RuntimeError("S6 active-watch needs SCANNER_DATA_ROOT or S6_ACTIVE_WATCH_DIR")
    return Path(root) if env.get("S6_ACTIVE_WATCH_DIR") else Path(root) / SUBDIR


def path_for(session_date, *, session=SESSION, env=None) -> Path:
    return _root(env) / f"{session_date}-{str(session).upper()}.json"


def session_scope(session, now=None) -> Optional[str]:
    """The scope key for the session containing `now`, as YYYY-MM-DD."""
    from scanners.base import session_range as srange

    moment = _utc(now or datetime.now(timezone.utc)).astimezone(EASTERN)
    started = srange.session_start_date(moment, session)
    return started.isoformat() if started else None


def _session_expiry(session_date, session=SESSION) -> datetime:
    """When this session's watchlist stops being current.

    The session's own close, from the canonical window table -- not a
    fixed 09:30.  A wrapping session closes on the following date.
    """
    from scanners.base import session_range as srange

    day = datetime.fromisoformat(str(session_date)).date()
    window = srange.window_for(session)
    if window is None:
        # An unknown session gets no lifetime at all rather than a
        # borrowed one: `read` then reports EXPIRED and offers nothing.
        return datetime.combine(day, time(0, 0), tzinfo=EASTERN).astimezone(timezone.utc)
    end = window[1]
    if srange.wraps_midnight(session):
        day = day + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=EASTERN).astimezone(timezone.utc)


def _normal_symbol(value) -> str:
    text = str(value or "").strip().upper()
    return text if text and text.replace(".", "").replace("-", "").isalnum() else ""


def _read_unlocked(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unrecognised S6 active-watch payload")
    return payload


def read(session_date, *, session=SESSION, now=None, env=None) -> Dict[str, Any]:
    """Return current state; wrong-scope/session/expired state fails closed."""
    target = path_for(session_date, session=session, env=env)
    payload = _read_unlocked(target)
    if payload is None:
        return {"status": "MISSING", "entries": [], "path": str(target)}
    moment = _utc(now or datetime.now(timezone.utc))
    if str(payload.get("session_date")) != str(session_date) or \
            str(payload.get("session")).upper() != str(session).upper():
        return {"status": "STALE_SCOPE", "entries": [], "path": str(target)}
    expiry = _utc(payload.get("expires_at"))
    if moment >= expiry:
        return {**payload, "status": "EXPIRED", "entries": [], "path": str(target)}
    entries = []
    seen = set()
    for row in payload.get("entries") or ():
        symbol = _normal_symbol((row or {}).get("symbol"))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        entries.append(dict(row, symbol=symbol))
    if len(entries) > MAX_LOGICAL_WATCH_SYMBOLS:
        return {**payload, "status": "OVER_CAPACITY", "entries": [], "path": str(target)}
    return {**payload, "status": "ACTIVE", "entries": entries, "path": str(target)}


#: Fields that describe a symbol's S6 discovery GENERATION.  Only a
#: STRATEGY addition (one that names a strategy_source) may ever set
#: these; a TRANSPORT-only addition (collector membership) must never
#: touch them, so a symbol's generation identity survives untouched
#: across cycles where only transport facts are being refreshed.
_GENERATION_FIELDS = ("full_scan_started_at", "symbol_evaluated_at",
                     "candidate_discovered_at", "provisional",
                     "final_generation_published", "scan_id",
                     "scanner_variant")


def merge(session_date, additions: Iterable[Dict[str, Any]], *, session=SESSION,
          now=None, env=None, max_symbols=MAX_LOGICAL_WATCH_SYMBOLS,
          replace=False, metadata=None, trading_day=None,
          preserve_existing_order=False) -> Dict[str, Any]:
    """Atomically add/update rows, preserving first-added time and cap.

    The capacity defaults to the LOGICAL watch ceiling, independent of the
    physical WebSocket transport limit (see MAX_LOGICAL_WATCH_SYMBOLS).
    Callers may lower it in tests; it is never raised above that ceiling.

    Generation identity vs transport
    ---------------------------------
    Each addition is either a STRATEGY addition (carries a
    `strategy_source` -- an S6 discovery: a live provisional PASS or a
    completed manifest row -- or, as a legacy synonym, a bare `source`)
    or a TRANSPORT-only addition (carries none; a pure collector-
    membership fact). A strategy addition is a COMPLETE, authoritative
    statement of the symbol's current generation: every field in
    `_GENERATION_FIELDS` is set FROM it, including clearing one that is
    legitimately absent this cycle, because that addition speaks for the
    whole generation, not a patch onto whatever a differently-sourced row
    left behind. A transport-only addition never touches those fields --
    it may only ever ESTABLISH a `strategy_source` when the row has none
    at all (pure collector sweep, no S6 signal), and always refreshes
    `transport_source`, which is a live fact independent of why the
    symbol is being watched at all.

    Two same-symbol strategy additions in one call are resolved by the
    caller's ordering (this function processes `additions` in order and
    the LAST strategy addition for a symbol wins) -- see
    `refresh_from_existing_sources` for the actual generation-precedence
    rule (current provisional over old final) applied before this is
    ever called.
    """
    cap = min(int(max_symbols), MAX_LOGICAL_WATCH_SYMBOLS)
    if cap < 1:
        raise ValueError("active-watch capacity must be positive")
    moment = _utc(now or datetime.now(timezone.utc))
    target = path_for(session_date, session=session, env=env)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = _read_unlocked(target) if target.exists() else None
        prior = {}
        # Rows are carried forward only within the SAME scope. A file from
        # another session or another session-date contributes nothing, so a
        # previous session's admissions can never become this one's.
        if existing and str(existing.get("session_date")) == str(session_date) \
                and str(existing.get("session")).upper() == str(session).upper():
            prior = {_normal_symbol(r.get("symbol")): dict(r)
                     for r in existing.get("entries") or () if _normal_symbol(r.get("symbol"))}
        raw_additions = list(additions or ())
        requested = [_normal_symbol((raw or {}).get("symbol"))
                     for raw in raw_additions]
        requested = {symbol for symbol in requested if symbol}
        # A refresh rebuilds from authoritative sources, so removed sources
        # still disappear.  For symbols that remain authoritative, retain
        # their admission order: a newly-found provisional PASS may fill a
        # spare slot but cannot silently evict an already-admitted watch
        # symbol merely because the source list is rebuilt every minute.
        ordered = (list(prior) if not replace else
                   ([symbol for symbol in prior if symbol in requested]
                    if preserve_existing_order else []))
        expires_at = _session_expiry(session_date, session).isoformat()
        for raw in raw_additions:
            raw = raw or {}
            symbol = _normal_symbol(raw.get("symbol"))
            if not symbol:
                continue
            row = dict(prior.get(symbol) or {})
            if symbol not in ordered:
                ordered.append(symbol)
            row["symbol"] = symbol
            row["added_at"] = row.get("added_at") or moment.isoformat()
            row["updated_at"] = moment.isoformat()
            row["expires_at"] = expires_at
            transport_source = raw.get("transport_source")
            if transport_source is not None:
                row["transport_source"] = str(transport_source)
            # `strategy_source` is the current name; a bare `source` is a
            # legacy synonym still accepted so a caller stating one
            # explicitly is always treated as a strategy addition.
            strategy_source = raw.get("strategy_source")
            if strategy_source is None:
                strategy_source = raw.get("source")
            if strategy_source is not None:
                row["strategy_source"] = str(strategy_source)
                row["source"] = row["strategy_source"]  # legacy alias
                row["reason"] = str(raw.get("reason") or "source admission")
                row["discovery_generation"] = raw.get("discovery_generation")
                for key in _GENERATION_FIELDS:
                    row[key] = raw.get(key)
            elif not row.get("strategy_source"):
                # TRANSPORT-only addition and no S6 discovery identity
                # exists yet for this symbol: collector membership alone
                # is a legitimate, minimal reason to watch it.
                row["strategy_source"] = COLLECTOR_MEMBERSHIP_SOURCE
                row["source"] = row["strategy_source"]
                row["reason"] = str(raw.get("reason") or "current collector subscription")
                row["discovery_generation"] = raw.get("discovery_generation")
                row.setdefault("provisional", False)
                row.setdefault("final_generation_published", False)
                # A uniform row shape: every generation field exists
                # (as None where there is no S6 claim behind it) so a
                # reader can always .get()/[] any of them consistently.
                for key in _GENERATION_FIELDS:
                    row.setdefault(key, None)
            # else: transport-only addition for a symbol that already
            # carries an S6 discovery identity -- only transport_source/
            # updated_at/expires_at above are touched; the generation
            # fields survive untouched from `prior[symbol]`.
            prior[symbol] = row
        kept = ordered[:cap]
        kept_rows = [prior[s] for s in kept]
        websocket_backed = sum(1 for r in kept_rows
                               if r.get("transport_source") == TRANSPORT_WEBSOCKET)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "session_date": str(session_date),
            "trading_day": str(trading_day) if trading_day else None,
            "session": str(session).upper(),
            "capacity": cap,
            "updated_at": moment.isoformat(),
            "expires_at": expires_at,
            "entries": kept_rows,
            "dropped": max(0, len(ordered) - cap),
            "websocket_backed": websocket_backed,
            "rest_backed": len(kept_rows) - websocket_backed,
            "metadata": dict(metadata or {}),
        }
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(target)
        return {**payload, "status": "ACTIVE", "path": str(target)}


def _provisional_path(session_date, session=SESSION, env=None) -> Path:
    return _root(env) / f"{session_date}-{str(session).upper()}-provisional.json"


def record_provisional_pass(session_date, session, row: Dict[str, Any], *,
                             now=None, env=None) -> bool:
    """A scanner PASS, recorded the instant it happens -- mid-scan, before
    the run's own completion/publication.

    This is the ONLY thing that removes the full-scan publication barrier:
    without it, a symbol that passes at minute 40 of a 50-minute scan is
    invisible to `refresh_from_existing_sources()` (and therefore to
    active-watch, and therefore to the 1-minute fast-watch tick) until the
    run finishes. This function's write, and `refresh_from_existing_
    sources()` reading it back in as one more source below, is the entire
    fix -- everything else (WATCHING vs READY, precision/risk/sizing/
    account/kill-switch/session-capability/closed-bar gates, the broker
    call) is completely untouched and still runs exactly as it always has,
    driven by the SAME 1-minute BUY tick.

    Deliberately a SEPARATE small file from the main active-watch state
    (not a `merge()` call): the main file is rewritten wholesale
    (`replace=True`) by every 1-minute refresh, so a `merge(replace=False)`
    call from mid-scan would only survive until the very next tick's
    rebuild. This store is instead READ BY that rebuild (see
    `refresh_from_existing_sources()` below), so a provisional admission
    is durable across ticks for as long as the provisional file still
    carries it -- exactly as durable as, and no more privileged than, the
    collector-subscription and completed-manifest sources it sits beside.

    Never raises -- mirrors `record_evaluation()`'s contract: an
    observability/hand-off write must never turn into a reason the
    scanner itself fails or slows down."""
    try:
        symbol = _normal_symbol((row or {}).get("symbol"))
        if not symbol:
            return False
        moment = _utc(now or datetime.now(timezone.utc))
        target = _provisional_path(session_date, session, env)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_suffix(".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            payload = None
            if target.exists():
                try:
                    payload = json.loads(target.read_text(encoding="utf-8"))
                except Exception:
                    payload = None
            if not isinstance(payload, dict) \
                    or payload.get("schema_version") != PROVISIONAL_SCHEMA_VERSION \
                    or str(payload.get("session_date")) != str(session_date) \
                    or str(payload.get("session")).upper() != str(session).upper():
                # Wrong/missing/stale scope -- start a fresh scope-correct
                # file rather than carrying anything forward. This is the
                # cross-session-leakage guard: a provisional file can only
                # ever describe the ONE (session_date, session) it is
                # named for.
                payload = {
                    "schema_version": PROVISIONAL_SCHEMA_VERSION,
                    "session_date": str(session_date),
                    "session": str(session).upper(),
                    "entries": {},
                }
            entries = payload.get("entries") or {}
            existing = entries.get(symbol) or {}
            # Idempotency is scoped to the SAME scan: a later PASS for
            # this symbol under a DIFFERENT (newer) scan_id is a new
            # generation, not a repeat of the old one, and must not
            # silently inherit its predecessor's discovery instant.
            same_scan = str(existing.get("scan_id") or "") == \
                str((row or {}).get("scan_id") or "")
            entries[symbol] = {
                "symbol": symbol,
                "source": PROVISIONAL_SOURCE,
                "trading_day": (row or {}).get("trading_day"),
                "scan_id": (row or {}).get("scan_id"),
                "scanner_variant": (row or {}).get("scanner_variant"),
                "full_scan_started_at": (row or {}).get("full_scan_started_at"),
                "symbol_evaluated_at": (row or {}).get("symbol_evaluated_at"),
                # A repeated PASS for the same symbol within one scan
                # (should not happen -- one evaluation per symbol per run --
                # but idempotent either way) keeps the FIRST discovery time,
                # so latency lineage always reflects the earliest signal.
                "candidate_discovered_at":
                    (existing.get("candidate_discovered_at") if same_scan else None)
                    or (row or {}).get("candidate_discovered_at") or moment.isoformat(),
                "updated_at": moment.isoformat(),
                "provisional": True,
                "final_generation_published": False,
                "reason": "provisional S6 discovery (full scan still in progress)",
            }
            payload["entries"] = entries
            payload["updated_at"] = moment.isoformat()
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temp.replace(target)
            return True
    except Exception:
        # Admission is non-fatal to the broad scanner, but it is not silent:
        # this is precisely the hand-off whose absence reintroduces the long
        # full-scan publication delay.  The runner's outer guard logs too
        # when an injected writer raises; this covers direct writer failures.
        logger.exception("S6 provisional PASS admission failed")
        return False


def _read_provisional(session_date, session=SESSION, *, env=None) -> List[Dict[str, Any]]:
    """Rows admitted mid-scan, still scoped to (session_date, session), in
    deterministic discovery order (earliest first -- the same FIFO
    tiebreak `merge()`'s own cap already applies to its `additions`
    ordering, reused here rather than inventing a second policy)."""
    try:
        target = _provisional_path(session_date, session, env)
        if not target.exists():
            return []
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != PROVISIONAL_SCHEMA_VERSION:
        return []
    if str(payload.get("session_date")) != str(session_date) or \
            str(payload.get("session")).upper() != str(session).upper():
        return []
    entries = payload.get("entries") or {}
    if not isinstance(entries, dict):
        return []
    rows = [dict(r) for r in entries.values() if _normal_symbol((r or {}).get("symbol"))]
    rows.sort(key=lambda r: (str(r.get("candidate_discovered_at") or ""), str(r.get("symbol") or "")))
    return rows


def _live_provisional(session_date, session, trading_day, *, env=None) -> List[Dict[str, Any]]:
    """Only PASS rows owned by the scan that still holds the cycle lock.

    A provisional row is a bridge *during* its producer's scan, never a
    second candidate generation.  If a scan aborts, crashes, or is replaced,
    the kernel releases the lock and this returns no provisional rows even if
    the audit file remains on disk until session expiry.
    """
    rows = _read_provisional(session_date, session, env=env)
    if not rows:
        return []
    try:
        from config import s6_sessions
        from scanners.publish import scan_cycle

        state = scan_cycle.state(trading_day or session_date, session,
                                 scanner=s6_sessions.SCANNER_NAME)
    except Exception:
        # Cannot establish producer liveness: fail closed for provisional
        # rows. Completed manifests and collector entries remain independent.
        return []
    if not state.running or not state.run_id:
        return []
    return [row for row in rows
            if str(row.get("scan_id") or "") == str(state.run_id)]


def refresh_from_existing_sources(session_date, *, session=SESSION,
                                  trading_day=None, now=None,
                                  env=None) -> Dict[str, Any]:
    """Merge current collector membership and completed discovery output.

    `session_date` scopes the watchlist; `trading_day` is the key the
    SCANNER published its candidates under, which for a session that
    crosses midnight is not the same string.

    Generation precedence: OLD FINAL < CURRENT RUNNING PROVISIONAL <
    CURRENT FINAL. `_live_provisional` returns a row for a symbol ONLY
    while the scan that produced it still holds the cycle lock (see its
    own docstring) -- and a scan cannot have published its OWN completed
    manifest while it still holds that lock. So whenever both a live
    provisional row and a completed-manifest row exist for the same
    symbol in one refresh, the manifest row is NECESSARILY from an
    older, already-finished generation: building the final rows first
    and then letting provisional OVERWRITE them for any symbol both name
    encodes exactly that precedence. The moment the producing scan
    completes, `_live_provisional` stops returning it (lock released),
    and the (now current) final row is used with nothing left to
    outrank it -- CURRENT FINAL supersedes CURRENT PROVISIONAL without
    any extra rule, purely because the lock-liveness gate already
    retired the provisional side.
    """
    from market_data import collector_status

    status = collector_status.describe(env=env, now=now)
    subscribed = [_normal_symbol(s) for s in status.get("subscribed_symbols") or ()]
    subscribed = [s for s in subscribed if s]
    subscribed_set = set(subscribed)

    def _transport_for(symbol: str) -> str:
        return TRANSPORT_WEBSOCKET if symbol in subscribed_set else TRANSPORT_REST

    final_by_symbol: Dict[str, Dict[str, Any]] = {}
    try:
        from config import s6_sessions
        from scanners.publish import candidates

        rows = [r for r in candidates.read(trading_day or session_date, session)
                if str(r.get("strategy_id")) == s6_sessions.STRATEGY_ID]
        rows.sort(key=lambda r: (str(r.get("generated_at") or ""),
                                 -(int(r.get("rank") or 10**6))), reverse=True)
        seen = set()
        for row in rows:
            symbol = _normal_symbol(row.get("symbol"))
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            provenance = row.get("provenance") or {}
            final_by_symbol[symbol] = {
                "symbol": symbol, "strategy_source": FULL_DISCOVERY_SOURCE,
                "transport_source": _transport_for(symbol),
                "discovery_generation": row.get("scanner_run_id"),
                "reason": "completed S6 discovery candidate",
                "full_scan_started_at": provenance.get(
                    "full_scan_started_at") or row.get("generated_at"),
                "symbol_evaluated_at": provenance.get(
                    "symbol_evaluated_at") or provenance.get("signal_timestamp"),
                "candidate_discovered_at": provenance.get(
                    "candidate_discovered_at") or provenance.get(
                        "signal_timestamp") or row.get("generated_at"),
                "provisional": False,
                "final_generation_published": True,
                "scan_id": row.get("scanner_run_id"),
            }
    except Exception:
        # The collector seed is independently useful.  A discovery read
        # failure is surfaced by the full scanner's own health path.
        pass

    provisional_by_symbol: Dict[str, Dict[str, Any]] = {}
    for row in _live_provisional(session_date, session, trading_day, env=env):
        symbol = _normal_symbol(row.get("symbol"))
        if not symbol:
            continue
        provisional_by_symbol[symbol] = {
            "symbol": symbol, "strategy_source": PROVISIONAL_SOURCE,
            "transport_source": _transport_for(symbol),
            "discovery_generation": row.get("scan_id"),
            "reason": "provisional S6 discovery (full scan still in progress)",
            "full_scan_started_at": row.get("full_scan_started_at"),
            "symbol_evaluated_at": row.get("symbol_evaluated_at"),
            "candidate_discovered_at": row.get("candidate_discovered_at"),
            "provisional": True,
            "final_generation_published": False,
            "scan_id": row.get("scan_id"),
            "scanner_variant": row.get("scanner_variant"),
        }

    # Final first, provisional overwrites -- see the precedence rule in
    # this function's docstring. A symbol found in both this cycle costs
    # its evaluation budget exactly ONCE either way: this collapses to
    # one row per symbol before anything downstream sees it.
    discovery_by_symbol: Dict[str, Dict[str, Any]] = dict(final_by_symbol)
    discovery_by_symbol.update(provisional_by_symbol)
    discovery_additions = list(discovery_by_symbol.values())

    # Kept for observability/reporting only (§14/§17 funnel): the REST
    # throughput fast_watch's own per-tick Budget actually enforces at
    # EVALUATION time (evaluate what fits, defer the rest, retry next
    # tick -- s6_live/fast_watch.py). Admission itself is no longer
    # gated by it: an admission-time REST cap on TOP of that budget used
    # to silently drop an outsider discovery symbol from `additions`
    # some cycles and not others (ordering-dependent), which could evict
    # an already-admitted, still-authoritative watch symbol the very
    # next cycle purely because it fell outside that cycle's outsider
    # slice -- exactly the "current same-symbol refresh must never be
    # dropped" failure this task rules out. The LOGICAL cap
    # (MAX_LOGICAL_WATCH_SYMBOLS) is now the only thing bounding
    # admission.
    from market_data import kis_minute_chart
    from s6_live import pretrade_validation
    rest_capacity = max(0, int(pretrade_validation.budget_seconds(env) /
                               kis_minute_chart.MEASURED_SECONDS_PER_SYMBOL))

    additions: List[Dict[str, Any]] = list(discovery_additions)
    if str(status.get("market_session") or "").upper() == str(session).upper():
        for symbol in subscribed:
            additions.append({
                "symbol": symbol, "transport_source": TRANSPORT_WEBSOCKET,
                "discovery_generation": status.get("collector_started_at"),
                "reason": "current collector subscription",
                "candidate_discovered_at": status.get("collector_started_at"),
            })
    return merge(session_date, additions, session=session, now=now, env=env,
                 replace=True, preserve_existing_order=True,
                 trading_day=trading_day, metadata={
                     "collector_state": status.get("state"),
                     "collector_heartbeat_at": status.get("last_heartbeat_at"),
                     "subscription_count": status.get("subscription_count"),
                     "subscription_requested": status.get("subscription_requested"),
                     "rest_fallback_capacity": rest_capacity,
                 })


def record_evaluation(session_date, record, *, session=SESSION, env=None) -> None:
    """Append mandatory per-symbol fast-path timing; never read by trading."""
    try:
        target = _root(env) / f"{session_date}-{str(session).upper()}-evaluations.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except Exception:
        # This is observability after the decision. It must not manufacture
        # a different decision because its own disk is unavailable.
        return


def record_evaluations_batch(session_date, records, *, session=SESSION, env=None) -> None:
    """The same durable per-symbol record `record_evaluation` writes, for
    every symbol a fast-watch tick evaluated, in ONE flock acquisition and
    ONE write instead of one of each per symbol.

    A fast-watch tick with N evaluated symbols used to pay N separate
    open/flock/write round trips for this audit trail alone -- pure
    observability, never read by trading, but still real per-symbol wall
    time inside the same tick a live py-spy trace on 2026-09-09 found
    already running close to its own budget. Batching removes that cost
    without changing what is recorded or its shape: each row is still
    exactly what `record_evaluation` would have written for that symbol.
    """
    if not records:
        return
    try:
        target = _root(env) / f"{session_date}-{str(session).upper()}-evaluations.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            for record in records:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except Exception:
        # Same contract as record_evaluation: an observability write must
        # never manufacture a different trading decision.
        return
