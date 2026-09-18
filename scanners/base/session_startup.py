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

So the heavy discovery moves BEFORE the session, and the session starts on
the small set that heavy discovery already chose.

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
ARTIFACT_VERSION = 1

#: How long before a session opens the preparation window runs. A range
#: rather than an instant because the wrapper fires on a cron tick and must
#: find itself inside it, not exactly on it.
PREP_LEAD_MAX_MINUTES = 40
PREP_LEAD_MIN_MINUTES = 30

#: An artifact older than this cannot describe the session it claims. Set
#: from the preparation lead: anything prepared before the window that
#: produced it is from an earlier cycle.
MAX_ARTIFACT_AGE_SECONDS = 3 * 60 * 60

#: How long after a session opens the startup mode applies. The existing
#: scanner cron fires at :02/:17/:32/:47, so this covers the first
#: invocation after a boundary and no more -- past it the ordinary cadence
#: resumes with the ordinary universe.
STARTUP_WINDOW_MINUTES = 5

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
                   selection_source=None, fallback_reason=None, now=None):
    """The artifact's content. Symbols only -- never a measurement."""
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
        "collector_cap": int(cap),
        "symbols": ordered,
        "symbol_count": len(ordered),
    }


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

    if artifact.get("version") != ARTIFACT_VERSION:
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


def in_startup_window(now=None, *, window_minutes=STARTUP_WINDOW_MINUTES):
    """Is this the first few minutes of a session?

    Decides whether a scan should start on the prepared universe or on the
    ordinary one. False for everything else, including a session whose
    start cannot be located -- an unanswerable clock must not silently
    shrink the universe a scan looks at.
    """
    current = now or datetime.now(timezone.utc)
    if scan_session.session_at(current) == scan_session.CLOSED:
        return False
    started = session_started_at(current)
    if started is None:
        return False
    return 0 <= _minutes(current - started) <= window_minutes


def universe_mode(now=None) -> str:
    """`prepared` inside the startup window, empty otherwise.

    Printed for a shell caller, which passes it straight to
    `--universe`. Empty means "leave the profile's own choice alone".
    """
    return "prepared" if in_startup_window(now) else ""
