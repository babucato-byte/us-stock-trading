"""The small symbol set a session starts on, prepared before it opens.

The problem
-----------
Every S6 scan reads the whole ranked universe -- 593 to 600 symbols -- one
KIS minute chart at a time, at SCANNER priority (P2) on the shared read
interval. Measured, repeatedly, since 2026-08-27:

    2026-08-27 12:03:25  universe=591  duration=957.2s
    2026-09-18 00:00:25  universe=593  duration=770.0s

That is not a fault; it is what the structure costs. On 2026-09-18 the
PREMARKET scan started 08:02:11 and finished 08:12:47 -- 636 seconds during
which the entry worker was healthy, `orders_allowed=True`, and had nothing
to work with because `scanned=0`. The 08:17 scan ran long too and the 08:32
invocation was SCANNER_LOCK_SKIPPED behind it.

So the heavy discovery moves BEFORE the session, and the collector starts
streaming the set that heavy discovery already chose.

The scanner does NOT start on that set. For a while it did, and the two
halves of that sentence were the same list, which made the transport
ceiling the strategy's universe ceiling: 41 symbols evaluated on a morning
when 593 were available. The transport ceiling is a property of one
appkey's websocket, not a statement about how much market is worth
looking at.

What actually costs a session its first minutes is not the universe size
but the clock. An opening range needs `orb_minutes` to form and
`min_post_range_bars` to be broken, so a scan before that cannot describe
the market -- every symbol fails for the same structural reason. Measured
live on 2026-09-19, a scan two minutes into REGULAR rejected 41 of 41
symbols with INSUFFICIENT_POST_RANGE_BARS and no acquisition failures;
twenty-four minutes in, one of 593. So this module also answers WHEN a
verdict can first mean anything, and a pre-evaluable invocation waits for
that moment instead of spending itself on a market that cannot answer yet.

What this module is, and is not
-------------------------------
It is a CONSUMER of two things that already exist and a producer of
neither.

The schedule is `scan_session.session_at()`, which asks
`config.kis_market_schedule` for 주간거래 and the Eastern clock for the
rest. The next boundary is found by asking it forward in time rather than
by restating 04:00/09:30/16:00/20:00 here: a second copy of those numbers
is a second definition of when a session starts, and the daytime window
moves with US DST against a KST venue, which is exactly the disagreement
that produced a daytime session an hour before KIS opened one.

The symbol selection is `market_data.bootstrap_watchlist.build()`, which
the collector already uses and which is already bounded by the real
subscription ceiling. This module never imports it -- `scanners/` must not
reach `state_store`, and `build()` does (tests/test_scanner_trading_
isolation.py). The preparing script imports both; the scanner reads only
the JSON this module validates.

What the artifact carries, and does not
---------------------------------------
Symbols and provenance. No price, volume, VWAP, EMA, ORB or signal: the
artifact answers WHO TO LOOK AT, and the session's own fresh market data
answers WHAT THEY LOOK LIKE NOW. Storing a value here that a scan could
reuse is how a candidate published in one session gets built from another
session's numbers.

One generation, two views. `symbols` is the collector's, bounded by the
transport ceiling. `scanner_view` is the strategy's, and holds no symbol
list at all -- it names the manifest as the universe and records the
moment that universe becomes evaluable. A second symbol list here would
be a second universe bounded by whoever wrote it.
"""
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

from scanners.base import scan_session

logger = logging.getLogger(__name__)

#: Bumped when the artifact's meaning changes. An unrecognised version is
#: refused rather than guessed at.
#:
#: v2 splits the artifact into two CONSUMER VIEWS of one generation. v1
#: carried a single `symbols` list that both the collector and the scanner
#: read, which made the transport ceiling the scanner's universe ceiling
#: too -- 41 symbols evaluated when 593 were available. v2 keeps `symbols`
#: as the collector view, byte for byte, and states the scanner's universe
#: separately.
ARTIFACT_VERSION = 2

#: v1 is still READ. A v1 artifact is a valid collector view and says
#: nothing about the scanner, which is exactly the fail-safe degradation
#: wanted: collector = its 41, scanner = the manifest.
SUPPORTED_ARTIFACT_VERSIONS = (1, 2)

#: How long before a session opens the preparation window runs. A range
#: rather than an instant because the wrapper fires on a cron tick and must
#: find itself inside it, not exactly on it.
PREP_LEAD_MAX_MINUTES = 40
PREP_LEAD_MIN_MINUTES = 30

#: An artifact older than this cannot describe the session it claims. Set
#: from the preparation lead: anything prepared before the window that
#: produced it is from an earlier cycle.
MAX_ARTIFACT_AGE_SECONDS = 3 * 60 * 60

#: The longest a pre-evaluable invocation may wait for the market to
#: become evaluable. One scanner cron interval: a wait longer than the
#: cadence would still be waiting when the next tick arrived, and that
#: tick is already closer to the answer than this one can get.
MAX_DEFER_SECONDS = 15 * 60

#: What the scanner's universe is. Named so the artifact records the
#: decision rather than implying it by omission, and deliberately NOT a
#: symbol list: copying one here would give the scanner a second universe
#: bounded by whatever wrote it.
SCANNER_VIEW_SOURCE_MANIFEST = "MANIFEST"

SOURCE_PREPARED = "SESSION_STARTUP_PREP"
SOURCE_COLLECTOR_FALLBACK = "COLLECTOR_FALLBACK"

STATUS_VALID = "VALID"
STATUS_MISSING = "MISSING"
STATUS_UNREADABLE = "UNREADABLE"
STATUS_VERSION = "UNSUPPORTED_VERSION"
STATUS_WRONG_DAY = "WRONG_TRADING_DAY"
STATUS_WRONG_SESSION = "WRONG_TARGET_SESSION"
STATUS_STALE = "STALE"
STATUS_EMPTY = "NO_SYMBOLS"
STATUS_TOO_MANY = "EXCEEDS_CAP"
STATUS_DUPLICATE = "DUPLICATE_SYMBOLS"
STATUS_NO_PROVENANCE = "NO_SOURCE_GENERATION"

_SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,10}$")


def _minutes(delta) -> float:
    return delta.total_seconds() / 60.0


def next_boundary(now=None, *, horizon_minutes=26 * 60, step_minutes=1):
    """(next_session, boundary_moment), or (None, None) within the horizon.

    Found by asking `scan_session.session_at()` forward rather than by
    holding a table of session start times. That keeps one definition of
    when a session begins -- including 주간거래, whose window is published
    in KST and moves against Eastern with US DST.
    """
    current = now or datetime.now(timezone.utc)
    here = scan_session.session_at(current)
    step = timedelta(minutes=step_minutes)
    moment = current
    for _ in range(int(horizon_minutes / step_minutes)):
        moment = moment + step
        if scan_session.session_at(moment) != here:
            return scan_session.session_at(moment), moment
    return None, None


def prep_due(now=None):
    """(target_session, boundary) when a preparation should run now.

    (None, None) otherwise, which is the ordinary answer: the wrapper runs
    on a plain cron tick and is a NO-OP for all but a few minutes before
    each session.
    """
    current = now or datetime.now(timezone.utc)
    session, boundary = next_boundary(current)
    if session is None:
        return None, None
    if session == scan_session.CLOSED:
        return None, None
    lead = _minutes(boundary - current)
    if PREP_LEAD_MIN_MINUTES <= lead <= PREP_LEAD_MAX_MINUTES:
        return session, boundary
    return None, None


def artifact_dir(discovery_root):
    return os.path.join(str(discovery_root), "startup")


def artifact_path(discovery_root, *, trading_day, target_session):
    return os.path.join(artifact_dir(discovery_root),
                        f"{trading_day}-{str(target_session).upper()}.json")


def build_artifact(*, trading_day, target_session, symbols, cap,
                   source=SOURCE_PREPARED, source_session=None,
                   source_manifest_generation=None,
                   source_manifest_created_at=None,
                   selection_source=None, fallback_reason=None,
                   scanner_view=None, now=None):
    """One generation, two consumer views. Symbols only -- never a
    measurement.

    `symbols` and `collector_cap` stay at the top level in the v1 spelling
    and mean what they always meant: the symbols the COLLECTOR will
    subscribe to, bounded by the transport ceiling. The collector reads
    them unchanged.

    `scanner_view` says what the STRATEGY universe is, and says it without
    a symbol list. That is the point of the split: the scanner's universe
    is the manifest, and the manifest is already a single source of truth
    for it. Copying 593 symbols in here would create a second one whose
    bound is whatever this writer happened to have.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ordered = [str(s).strip().upper() for s in symbols if str(s).strip()]
    return {
        "version": ARTIFACT_VERSION,
        "trading_day": str(trading_day),
        "target_session": str(target_session).upper(),
        "generated_at": stamp.isoformat(),
        "source": source,
        "prepared_normally": source == SOURCE_PREPARED,
        "fallback_reason": fallback_reason,
        "source_session": source_session,
        "source_manifest_generation": source_manifest_generation,
        "source_manifest_created_at": source_manifest_created_at,
        "selection_source": selection_source,
        # -- collector view (v1 spelling, unchanged meaning) --
        "collector_cap": int(cap),
        "symbols": ordered,
        "symbol_count": len(ordered),
        # -- scanner view --
        "scanner_view": dict(scanner_view) if scanner_view else None,
    }


def build_scanner_view(target_session, *, timing=None):
    """The scanner's half of the artifact: where its universe comes from,
    and the earliest moment a verdict about that session can mean
    anything.

    `cap` is present and null ON PURPOSE. An absent key reads as "nobody
    considered it"; a null one records that the strategy universe is
    deliberately unbounded by the transport ceiling.
    """
    view = {
        "source": SCANNER_VIEW_SOURCE_MANIFEST,
        "cap": None,
    }
    resolved = timing if timing is not None else strategy_timing(
        session=target_session)
    for key in ("earliest_evaluable_at", "orb_minutes", "min_post_range_bars",
                "bar_interval_minutes", "session_started_at"):
        value = resolved.get(key)
        view[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return view


def collector_symbols(artifact):
    """The symbols the COLLECTOR subscribes to, for v1 and v2 alike.

    One accessor so no reader has to know which version it was handed --
    a reader that checked the version itself would be the place a v1
    artifact silently became a scanner universe again.
    """
    return list((artifact or {}).get("symbols") or [])


def scanner_view_of(artifact):
    """The scanner's view, or None for a v1 artifact.

    None is not a failure and must not be treated as one: it means this
    artifact has nothing to say about the strategy universe, so the
    scanner uses the manifest -- which is what it uses anyway.
    """
    return (artifact or {}).get("scanner_view") or None


def write_atomic(discovery_root, artifact) -> str:
    """Write the artifact where both the collector and the scanner read it.

    Atomic because two readers share it: a reader that opened a
    half-written file would disagree with the other about which symbols
    this session starts on, which is the disagreement the shared artifact
    exists to remove.
    """
    path = artifact_path(discovery_root,
                         trading_day=artifact["trading_day"],
                         target_session=artifact["target_session"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(path),
                                    prefix=".startup-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(artifact, out, sort_keys=True, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return path


def load_valid(discovery_root, *, trading_day, target_session, cap,
               now=None, max_age_seconds=MAX_ARTIFACT_AGE_SECONDS):
    """(artifact, STATUS_VALID) or (None, why).

    Every refusal is named. "No prepared universe" and "a prepared universe
    for the wrong session" are different operational facts and the second
    one must never be usable.
    """
    path = artifact_path(discovery_root, trading_day=trading_day,
                         target_session=target_session)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            artifact = json.load(handle)
    except FileNotFoundError:
        return None, STATUS_MISSING
    except (OSError, ValueError):
        return None, STATUS_UNREADABLE

    if artifact.get("version") not in SUPPORTED_ARTIFACT_VERSIONS:
        return None, STATUS_VERSION
    if str(artifact.get("trading_day")) != str(trading_day):
        return None, STATUS_WRONG_DAY
    if str(artifact.get("target_session", "")).upper() != str(target_session).upper():
        return None, STATUS_WRONG_SESSION

    stamp = artifact.get("generated_at")
    try:
        generated = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None, STATUS_STALE
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = (current - generated).total_seconds()
    if age < 0 or age > max_age_seconds:
        return None, STATUS_STALE

    symbols = artifact.get("symbols") or []
    if not symbols:
        return None, STATUS_EMPTY
    if len(symbols) > int(cap):
        return None, STATUS_TOO_MANY
    upper = [str(s).strip().upper() for s in symbols]
    if len(set(upper)) != len(upper):
        return None, STATUS_DUPLICATE
    if not all(_SYMBOL.match(s) for s in upper):
        return None, STATUS_DUPLICATE

    # A normally-prepared artifact must say what it was derived from. A
    # fallback one is allowed to have no manifest generation -- it was
    # built because the prepared one was not there -- and carries its
    # reason instead.
    if artifact.get("prepared_normally") and not (
            artifact.get("source_manifest_generation")
            or artifact.get("selection_source")):
        return None, STATUS_NO_PROVENANCE

    artifact["symbols"] = upper
    return artifact, STATUS_VALID


def artifact_age_seconds(artifact, now=None) -> Optional[float]:
    try:
        generated = datetime.fromisoformat(str(artifact.get("generated_at")))
    except (TypeError, ValueError):
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - generated).total_seconds()


def session_started_at(now=None, *, horizon_minutes=26 * 60, step_minutes=1):
    """When the CURRENT session began, or None beyond the horizon.

    The mirror of `next_boundary`, and for the same reason: the answer
    comes from asking the production session truth, not from a table of
    start times kept here.
    """
    current = now or datetime.now(timezone.utc)
    here = scan_session.session_at(current)
    step = timedelta(minutes=step_minutes)
    moment = current
    for _ in range(int(horizon_minutes / step_minutes)):
        earlier = moment - step
        if scan_session.session_at(earlier) != here:
            return moment
        moment = earlier
    return None


def _bar_interval_minutes(timeframe) -> int:
    """Minutes per bar, from the scanner's own `source_timeframe`.

    Read from the scanner rather than written here. The ORB verdict is
    built from whatever that string names, so a second constant would be
    a second opinion about how long the post-range bars take to form.
    """
    text = str(timeframe or "").strip().lower()
    for suffix in ("min", "m"):
        if text.endswith(suffix):
            head = text[: -len(suffix)].strip()
            if head.isdigit() and int(head) > 0:
                return int(head)
            break
    raise ValueError(f"unsupported source_timeframe {timeframe!r}")


def strategy_inputs(session):
    """(orb_minutes, min_post_range_bars, bar_interval_minutes).

    All three come from the ORB scanner instance the runner itself builds,
    including the `orb_minutes_by_session` override. Asking the scanner is
    what keeps this from becoming a copy of its configuration that drifts
    the first time a session's window changes.
    """
    from scanners.registry import build_scanner

    scanner = build_scanner("orb")
    return (int(scanner.orb_minutes(session)),
            int(scanner.config.require_int("min_post_range_bars")),
            _bar_interval_minutes(scanner.source_timeframe))


def earliest_evaluable_at(session_start, *, orb_minutes,
                          min_post_range_bars, bar_interval_minutes):
    """The first moment an ORB verdict about this session can mean
    anything.

    Before it, every symbol fails for the same structural reason and the
    scan is not describing the market:

        opening range          orb_minutes
        + enough bars to break min_post_range_bars * bar_interval

    Measured live on 2026-09-19: a REGULAR scan at T+2 rejected 41 of 41
    symbols with INSUFFICIENT_POST_RANGE_BARS and zero acquisition
    failures, while T+24 rejected 1 of 593 for that reason. The universe
    size was never the problem; the clock was.
    """
    return session_start + timedelta(
        minutes=int(orb_minutes) + int(min_post_range_bars) * int(bar_interval_minutes))


def strategy_timing(now=None, session=None):
    """When the current session's strategy scan may first say something.

    Returns a dict rather than a moment because every field is telemetry
    the deferral has to report: an operator reading a six-minute wait must
    be able to see which three numbers produced it.

    `earliest_evaluable_at` is None when it cannot be derived -- a session
    whose start is not locatable, a closed market, an unreadable scanner
    config. None means DO NOT DEFER: waiting on a moment nobody can
    compute would turn an unanswerable clock into a skipped scan.
    """
    current = now or datetime.now(timezone.utc)
    here = scan_session.session_at(current)
    timing = {
        "session": here,
        "now": current,
        "session_started_at": None,
        "earliest_evaluable_at": None,
        "orb_minutes": None,
        "min_post_range_bars": None,
        "bar_interval_minutes": None,
        "unavailable_reason": None,
    }
    if here == scan_session.CLOSED:
        timing["unavailable_reason"] = "SESSION_CLOSED"
        return timing
    started = session_started_at(current)
    if started is None:
        timing["unavailable_reason"] = "SESSION_START_NOT_LOCATABLE"
        return timing
    timing["session_started_at"] = started
    try:
        orb, post, interval = strategy_inputs(session or here)
    except Exception as exc:  # noqa: BLE001 - see the docstring: never defer blind
        timing["unavailable_reason"] = "STRATEGY_INPUTS_UNAVAILABLE:%r" % (exc,)
        return timing
    timing["orb_minutes"] = orb
    timing["min_post_range_bars"] = post
    timing["bar_interval_minutes"] = interval
    timing["earliest_evaluable_at"] = earliest_evaluable_at(
        started, orb_minutes=orb, min_post_range_bars=post,
        bar_interval_minutes=interval)
    return timing


def defer_seconds(timing, *, max_defer_seconds=MAX_DEFER_SECONDS):
    """How long this invocation should wait, and 0.0 when it should not.

    0.0 for everything that is not a plainly-bounded wait toward a
    computable moment: an unavailable derivation, a market already
    evaluable, or a wait longer than the scanner's own cadence. In that
    last case the NEXT tick is closer to the answer than this one can
    get by holding a process open across it.
    """
    earliest = timing.get("earliest_evaluable_at")
    if earliest is None:
        return 0.0
    wait = (earliest - timing["now"]).total_seconds()
    if wait <= 0:
        return 0.0
    if wait > max_defer_seconds:
        return 0.0
    return wait
