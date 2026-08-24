"""The contract between the scanner node and the trading node.

Why a file and a schema rather than a call
------------------------------------------
The two nodes fail independently. A trading node that blocks on an HTTP
call to a laptop has taken the laptop's uptime as a dependency of its
order path, which is exactly backwards: the laptop is the optional half.
A file has the property that matters here -- its absence is
unambiguous, and the trading node's response to "no manifest" is
identical to its response to "laptop switched off", which is to fall
back to its own discovery and, failing that, to trade nothing.

What the trading node must NOT assume
-------------------------------------
That any of this is true. Every field is checked on read: the schema
version it was written with, the trading day it claims, when it was
generated, whether the symbols are unique, whether the file is whole. A
manifest is a list of symbols worth LOOKING at, produced by a machine
with no order permission -- it is never a buy signal, and the precision
scan on the trading node re-derives every strategy condition from the
trading node's own market data.

Atomicity
---------
Written to a temporary file in the same directory and renamed. `rename`
within a filesystem is atomic on POSIX, so a reader either sees the
previous manifest or the new one, never half of one. Writing in place
would let the trading node read a truncated JSON document during the six
minutes the scanner takes to produce the next one -- and a truncated
document that happens to parse is worse than one that does not.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "s6_discovery_manifest_v1"

#: The source label. Recorded so a manifest can never be mistaken for
#: server-side discovery output after the fact.
SOURCE_LAPTOP_MARKET_SCAN = "LAPTOP_MARKET_WIDE_SCAN"

#: Verdicts from `validate`.
VALID = "VALID"
MISSING = "MISSING"
UNREADABLE = "UNREADABLE"
SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
WRONG_TRADING_DAY = "WRONG_TRADING_DAY"
STALE = "STALE"
DUPLICATE_SYMBOLS = "DUPLICATE_SYMBOLS"
#: Usable, but drawn from a sample of the market rather than all
#: of it -- the provider throttled part of the pass.
PARTIAL = "PARTIAL"
EMPTY = "EMPTY"

#: How old a manifest may be and still inform a NEW entry.
#:
#: The scanner refreshes hourly, so a manifest older than two refresh
#: intervals means the scanner node has missed one and nobody noticed.
#: Ninety minutes accepts a normal hourly cadence plus a late run,
#: and refuses a laptop that went to sleep at lunchtime.
DEFAULT_MAX_AGE_SECONDS = 90 * 60


def _as_utc(value) -> Optional[datetime]:
    """Parse an ISO-8601 stamp to aware UTC, or None.

    Written here rather than borrowed from `s1_live.freshness` so this
    module -- the one BOTH nodes import -- depends on nothing that can
    place an order. The scanner node holds no broker credentials, and
    the cheapest way to keep it that way is to give it nothing to
    import that has any.
    """
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build(*, trading_day, session, symbols, scanner_commit=None,
          scan_id=None, universe_size=None, evaluated=None,
          duration_seconds=None, generated_at=None, coverage=None,
          complete=None) -> Dict[str, Any]:
    """The canonical document. Pure -- writes nothing."""
    return {
        "schema_version": SCHEMA_VERSION,
        "trading_day": str(trading_day),
        "session": session,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "source": SOURCE_LAPTOP_MARKET_SCAN,
        "scanner_commit": scanner_commit,
        "scan_id": scan_id,
        "universe_size": universe_size,
        "first_stage_evaluated": evaluated,
        "first_stage_passed": len(symbols),
        "scan_duration_seconds": duration_seconds,
        # What fraction of the universe was actually priced. A provider
        # that throttles halfway leaves a ranking drawn from a sample,
        # and the reader must be able to see that rather than infer it.
        "coverage": coverage,
        "complete": complete,
        "symbols": list(symbols),
    }


def write(document: Dict[str, Any], path) -> Path:
    """Atomically. A reader sees the old file or the new one."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent),
                                         prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def read(path) -> Optional[Dict[str, Any]]:
    """The document, or None. Never raises."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def validate(document, *, trading_day, now=None,
             max_age_seconds=DEFAULT_MAX_AGE_SECONDS) -> Dict[str, Any]:
    """`{"status": ..., "detail": ..., "symbols": [...], "age_seconds": ...}`.

    Every check is a separate verdict rather than a boolean, because the
    operator's response differs: a wrong trading day means the scanner
    node's clock or calendar is wrong, a stale manifest means it stopped
    running, and duplicates mean it has a bug. Collapsing them to
    "invalid" would make all three look like the same outage.
    """
    if document is None:
        return {"status": MISSING, "detail": "no manifest", "symbols": [],
                "age_seconds": None}
    if not isinstance(document, dict):
        return {"status": UNREADABLE, "detail": "not a JSON object",
                "symbols": [], "age_seconds": None}

    if document.get("schema_version") != SCHEMA_VERSION:
        return {"status": SCHEMA_MISMATCH,
                "detail": f"schema {document.get('schema_version')!r} != "
                          f"{SCHEMA_VERSION!r}", "symbols": [],
                "age_seconds": None}

    if str(document.get("trading_day")) != str(trading_day):
        return {"status": WRONG_TRADING_DAY,
                "detail": f"manifest says {document.get('trading_day')!r}, "
                          f"today is {trading_day!r}", "symbols": [],
                "age_seconds": None}

    moment = now or datetime.now(timezone.utc)
    made = _as_utc(document.get("generated_at"))
    age = (round((moment - made).total_seconds(), 3)
           if made is not None else None)
    if age is None:
        return {"status": UNREADABLE, "detail": "generated_at unreadable",
                "symbols": [], "age_seconds": None}
    if age < 0:
        # A manifest from the future is a clock fault on one of the two
        # nodes, and acting on it would mean trusting the one that is
        # wrong.
        return {"status": UNREADABLE,
                "detail": f"generated_at is {abs(age):.0f}s in the future",
                "symbols": [], "age_seconds": age}
    if age > float(max_age_seconds):
        return {"status": STALE,
                "detail": f"{age:.0f}s old, limit {max_age_seconds}s",
                "symbols": [], "age_seconds": age}

    rows = document.get("symbols") or []
    if not rows:
        # A real answer, not a fault: the market can genuinely offer
        # nothing worth a precision scan. Distinguished from MISSING so
        # "the scanner ran and found nothing" is not filed as "the
        # scanner did not run".
        return {"status": EMPTY, "detail": "scanner returned no symbols",
                "symbols": [], "age_seconds": age}

    names = [str((r or {}).get("symbol") or "").upper() for r in rows]
    if any(not n for n in names):
        return {"status": UNREADABLE, "detail": "a row has no symbol",
                "symbols": [], "age_seconds": age}
    if len(set(names)) != len(names):
        return {"status": DUPLICATE_SYMBOLS,
                "detail": f"{len(names) - len(set(names))} duplicate symbol(s)",
                "symbols": [], "age_seconds": age}

    coverage = document.get("coverage")
    detail = f"{len(rows)} symbols, {age:.0f}s old"
    if coverage is not None:
        detail += f", coverage {float(coverage) * 100:.0f}%"
    if document.get("complete") is False:
        # VALID, not refused: a partial ranking still beats yesterday's,
        # and refusing it would send the trading node back to the very
        # staleness this replaces. Labelled so the manifest cannot be
        # read as a complete market sweep.
        return {"status": PARTIAL, "detail": detail, "symbols": rows,
                "age_seconds": age, "coverage": coverage}
    return {"status": VALID, "detail": detail, "symbols": rows,
            "age_seconds": age, "coverage": coverage}
