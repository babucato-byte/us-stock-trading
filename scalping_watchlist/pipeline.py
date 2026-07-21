"""Pipeline runner: orchestrates Stage A-E for one scan cycle and persists
the result (Phase 2 instructions, sections 5 and 11).

Deliberately not a generic framework — one function, small helpers, wired
to the concrete provider/feature/eligibility/scorer/repeat_tracker/
repository modules. Report formatting (e.g. a Slack summary reusing
slack_utils.py's structure) is left for a later, explicitly-requested
step; this module's job is only "decide today's watchlist and persist it"
per the Phase 2 charter question.
"""

from market_hours import eastern_now, get_us_market_session

from . import calendar_guard, eligibility, freshness, repository, scorer
from .features import compute_features
from .models import (
    NOT_EVALUATED,
    STATUS_ACTIVE,
    STATUS_NEW,
    STATUS_REJECTED,
    WatchlistEntry,
)
from .repeat_tracker import update_repeat_tracker


def _dedupe_symbols(symbols):
    seen = set()
    unique = []
    duplicates = []
    for symbol in symbols:
        if symbol in seen:
            duplicates.append(symbol)
            continue
        seen.add(symbol)
        unique.append(symbol)
    return unique, duplicates


def run_scan_cycle(provider, now=None, symbols=None,
                    max_watchlist_size=None, ttl_minutes=None, expire_minutes=None,
                    lock_timeout=5.0, persist=True):
    """Runs one full Stage A-E cycle. `symbols` overrides the provider's
    universe (mainly for tests); production use leaves it None to scan the
    whole universe.csv-derived list.

    Returns {"selected": [dict...], "rejected": [dict...], "trading_date": str}
    — every dict is a WatchlistEntry-shaped row (already CSV-ready).
    """
    from config import scalping_watchlist_config as cfg

    max_watchlist_size = max_watchlist_size if max_watchlist_size is not None else cfg.MAX_WATCHLIST_SIZE
    ttl_minutes = ttl_minutes if ttl_minutes is not None else cfg.WATCHLIST_TTL_MINUTES
    expire_minutes = expire_minutes if expire_minutes is not None else cfg.WATCHLIST_EXPIRE_MINUTES

    now_dt = eastern_now(now)
    trading_date = now_dt.strftime("%Y-%m-%d")
    detected_at = now_dt.isoformat()
    session = get_us_market_session(now_dt)

    # CODEX-012: holiday/session gate runs before anything else touches the
    # provider or the persisted watchlist. A skip must never look like a
    # successful empty run (a persisted empty watchlist could otherwise be
    # mistaken for "genuinely no candidates today"), so the file is not
    # written to at all in this branch.
    skip_reason = calendar_guard.check_pipeline_allowed(now_dt, session, cfg)
    if skip_reason is not None:
        return {
            "selected": [], "rejected": [], "trading_date": trading_date,
            "status": "SKIPPED", "skip_reason": skip_reason,
            "candidate_count": 0, "persisted_count": 0, "output_path": "",
            "error_code": "", "error_message": "", "evaluated_at": detected_at,
        }

    candidate_symbols = symbols if symbols is not None else provider.get_universe_symbols()
    unique_symbols, duplicate_symbols = _dedupe_symbols(candidate_symbols)

    eligible_rows = []
    rejected_rows = []
    eligible_observations = {}  # for the repeat tracker, keyed by symbol

    for symbol in duplicate_symbols:
        rejected_rows.append(_build_entry(
            symbol, session, detected_at,
            features={}, eligibility_reasons=[], rejection_reasons=["DUPLICATE_SYMBOL"],
            status=STATUS_REJECTED, repeat_info=None, smart_money_score=NOT_EVALUATED,
        ).__dict__)

    for symbol in unique_symbols:
        try:
            snapshot = provider.get_symbol_snapshot(symbol, session, now=now_dt)
            data_quality_reasons = []
        except Exception as exc:
            snapshot = None
            data_quality_reasons = [f"PROVIDER_ERROR: {exc}"]

        if snapshot is not None:
            # CODEX-011: freshness is judged against this pipeline's own
            # evaluation time (now_dt), never provider_fetched_at — a
            # provider that fetches quickly but returns an old bar must
            # still be caught.
            data_quality_reasons += freshness.check_data_freshness(
                snapshot.data_as_of, now_dt, session, cfg
            )

        features, feature_reasons = compute_features(snapshot)
        data_quality_reasons = data_quality_reasons + feature_reasons

        elig_reasons, rejection_reasons = eligibility.evaluate_eligibility(symbol, features, data_quality_reasons)

        if rejection_reasons:
            rejected_rows.append(_build_entry(
                symbol, session, detected_at, features, elig_reasons, rejection_reasons,
                status=STATUS_REJECTED, repeat_info=None, smart_money_score=NOT_EVALUATED,
            ).__dict__)
            continue

        eligible_observations[symbol] = {
            "relative_volume": features.get("relative_volume"),
            "price": features.get("latest_price"),
            "scalping_score": None,  # filled in after scoring, only used by future cycles
        }
        eligible_rows.append((symbol, features, elig_reasons))

    # Stage D: update repeat state for everything that passed A-C this cycle.
    repeat_results = update_repeat_tracker(
        {s: obs for s, obs in eligible_observations.items()},
        trading_date, detected_at, lock_timeout=lock_timeout,
    ) if eligible_observations else {}

    selected_dicts = []
    for symbol, features, elig_reasons in eligible_rows:
        repeat_info = repeat_results.get(symbol)
        # CODEX-014: NEW on the first-ever detection, ACTIVE from the
        # second consecutive/total detection onward — a symbol seen only
        # once has not yet demonstrated persistence (Phase 2 instructions
        # section 8's documented NEW -> ACTIVE transition).
        detect_count = int(repeat_info.get("detect_count", 1)) if repeat_info else 1
        entry_status = STATUS_NEW if detect_count <= 1 else STATUS_ACTIVE
        entry = _build_entry(
            symbol, session, detected_at, features, elig_reasons, [],
            status=entry_status, repeat_info=repeat_info, smart_money_score=NOT_EVALUATED,
            expire_minutes=expire_minutes,
        )
        selected_dicts.append(entry.__dict__)

    # Deterministic order: score desc, symbol asc as a stable tiebreaker.
    selected_dicts.sort(key=lambda r: (-_safe_float(r.get("scalping_score")), r["symbol"]))

    result = {
        "selected": selected_dicts,
        "rejected": rejected_rows,
        "trading_date": trading_date,
        "status": "SUCCESS",
        "candidate_count": len(selected_dicts),
        "persisted_count": 0,
        "output_path": str(repository.WATCHLIST_FILE) if persist else "",
        "error_code": "",
        "error_message": "",
        "evaluated_at": detected_at,
    }

    if persist:
        # CODEX-013: the caller must be able to tell "computed successfully
        # but failed to persist" apart from a genuine success — a silently
        # dropped return value here is exactly how a save failure used to
        # look identical to a successful run.
        persistence = repository.save_watchlist_cycle(
            selected_dicts, rejected_rows, now_dt, ttl_minutes, expire_minutes,
            max_watchlist_size, lock_timeout=lock_timeout,
        )
        result["persisted_count"] = persistence["persisted_count"]
        if not persistence["success"]:
            result["status"] = "FAILED_PERSISTENCE"
            result["error_code"] = persistence["error_code"]
            result["error_message"] = persistence["error_message"]

    return result


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def _build_entry(symbol, session, detected_at, features, eligibility_reasons, rejection_reasons,
                  status, repeat_info, smart_money_score, expire_minutes=None):
    repeat_count = int(repeat_info.get("detect_count", 1)) if repeat_info else 1
    scalping_score = NOT_EVALUATED
    expires_at = "UNKNOWN"
    if status != STATUS_REJECTED:
        scalping_score, _sub_scores = scorer.compute_scalping_score(features, repeat_info, smart_money_score)
        if expire_minutes is not None:
            expires_at = _add_minutes_iso(detected_at, expire_minutes)

    # repeat_info (from repeat_tracker.py) already carries the true
    # first_detected_at across cycles; a REJECTED row (repeat_info=None)
    # has no history, so "now" is both its first and last detection.
    first_detected_at = repeat_info.get("first_detected_at", detected_at) if repeat_info else detected_at
    last_detected_at = repeat_info.get("last_detected_at", detected_at) if repeat_info else detected_at

    return WatchlistEntry(
        symbol=symbol,
        first_detected_at=first_detected_at,
        last_detected_at=last_detected_at,
        updated_at=detected_at,
        trading_session=session,
        latest_price=features.get("latest_price", "UNKNOWN") if features else "UNKNOWN",
        previous_close=features.get("previous_close", "UNKNOWN") if features else "UNKNOWN",
        gap_percent=features.get("gap_percent", "UNKNOWN") if features else "UNKNOWN",
        premarket_volume=features.get("premarket_volume", "NOT_EVALUATED") if features else "NOT_EVALUATED",
        current_volume=features.get("current_volume", "UNKNOWN") if features else "UNKNOWN",
        average_volume=features.get("average_volume", "UNKNOWN") if features else "UNKNOWN",
        relative_volume=features.get("relative_volume", "UNKNOWN") if features else "UNKNOWN",
        average_dollar_volume=features.get("average_dollar_volume", "UNKNOWN") if features else "UNKNOWN",
        atr=features.get("atr", "UNKNOWN") if features else "UNKNOWN",
        atr_percent=features.get("atr_percent", "UNKNOWN") if features else "UNKNOWN",
        liquidity_score=features.get("liquidity_score", "UNKNOWN") if features else "UNKNOWN",
        spread_estimate="NOT_AVAILABLE",
        repeat_count=repeat_count,
        smart_money_score=smart_money_score,
        source_score=NOT_EVALUATED,
        scalping_score=scalping_score,
        eligibility_reasons=";".join(eligibility_reasons),
        rejection_reasons=";".join(rejection_reasons),
        status=status,
        expires_at=expires_at,
    )


def _add_minutes_iso(detected_at_iso, minutes):
    from datetime import datetime, timedelta

    try:
        dt = datetime.fromisoformat(detected_at_iso)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return (dt + timedelta(minutes=minutes)).isoformat()
