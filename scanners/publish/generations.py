"""One scan, one generation, published all-or-nothing.

What was here before
--------------------
A scan appended its rows to `{day}-{SESSION}.jsonl` and, separately,
appended a line to `{day}-{SESSION}.jsonl.ran`. A consumer then decided
which rows were current by taking `max(generated_at)` ACROSS THE ROWS
PRESENT. Three things follow from that, and all three were observed:

  * A generation is only as atomic as an append loop. A write that dies
    halfway leaves rows that are complete-looking and incomplete, and
    nothing downstream can tell.

  * A COMPLETED scan that found NOTHING writes no rows at all --
    `publish()` returns early on an empty set -- so the newest rows on
    disk stay the PREVIOUS generation's. Fifteen candidates from
    generation 20 remain live after generation 21 has authoritatively
    answered "none". Absence of rows was doing duty for two opposite
    facts: "nothing to report" and "not reported yet".

  * "Which generation is current" was inferred from data instead of
    declared. So there was no way to say a generation exists, completed,
    and is empty.

What this is
------------
A generation record, replaced atomically, that DECLARES the answer:

    trading_day, session, variant, generation_id,
    status, candidate_count, generated_at, completed_at

`os.replace()` is atomic on POSIX, so a reader sees the old record or
the new one and never a half-written one. The record is written LAST --
after every row is on disk -- so a generation becomes visible only once
it is complete. If the rows fail to write, no record is published, and
the previous completed generation is left exactly as it was.

candidate_count is the authority
--------------------------------
Zero is a result. A COMPLETED generation with candidate_count=0 means
the scan ran and nothing broke out, and it supersedes whatever the last
generation found. That is different from a missing producer, and the two
must never collapse into each other -- one says wait, the other says the
scanner is not running.

This does NOT relax cross-variant safety
----------------------------------------
The record carries the variant that produced it, and the consumer still
checks it. A generation is scoped to (trading_day, session, variant) and
nothing here lets an S6-O generation answer an S6-R question.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GENERATION_SUFFIX = ".generation.json"

#: A scan is producing this generation now. Never consumable.
STATUS_IN_PROGRESS = "IN_PROGRESS"
#: The scan finished and its answer is complete. candidate_count may be 0.
STATUS_COMPLETED = "COMPLETED"
#: The scan did not produce an answer. Never consumable, and never read
#: as zero candidates -- it is the absence of a result, not a result.
STATUS_FAILED = "FAILED"

CONSUMABLE_STATUSES = frozenset({STATUS_COMPLETED})


def _safe(value) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_"
                   for ch in str(value)) or "unknown"


def manifest_path(trading_day: str, session: Optional[str] = None,
                  strategy_id: Optional[str] = None) -> Path:
    """One record per (day, session, STRATEGY).

    The strategy segment is not decoration. Several publishing scanners
    run in the same (day, session) -- the DAILY profile publishes
    S2_VOLUME_ACCUMULATION_V1 while S6 publishes S6_ORB_BREAKOUT_V1 --
    and keying only on (day, session) gave them ONE record to overwrite.

    Observed on 2026-09-03: S6 published its generation at 05:00:15 and
    again at 06:05:13; the daily profile finished at 06:15:38 and its S2
    record replaced both. S6's consumer then read a record whose
    generation_id no S6 row carries and offered zero candidates.
    """
    from scanners.publish.candidates import candidates_path

    base = str(candidates_path(trading_day, session))
    if strategy_id:
        return Path(f"{base}.{_safe(strategy_id)}{GENERATION_SUFFIX}")
    return Path(base + GENERATION_SUFFIX)


def publish(trading_day: str, session: Optional[str] = None, *,
            generation_id: Optional[str] = None,
            variant: Optional[str] = None,
            strategy_id: Optional[str] = None,
            status: str = STATUS_COMPLETED,
            candidate_count: int = 0,
            generated_at: Optional[str] = None,
            completed_at: Optional[str] = None) -> bool:
    """Replace the current generation record, atomically. Never raises.

    Call this AFTER the rows are written. A record published before its
    payload would advertise a generation a consumer could then read only
    half of -- which is the torn publication this file exists to make
    impossible.
    """
    payload = {
        "trading_day": trading_day,
        "session": session,
        "variant": variant,
        "strategy_id": strategy_id,
        "generation_id": generation_id,
        "status": str(status),
        "candidate_count": int(candidate_count or 0),
        "generated_at": generated_at,
        "completed_at": completed_at or datetime.now(timezone.utc).isoformat(),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    target = None
    try:
        target = manifest_path(trading_day, session, strategy_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(target.parent),
            prefix=target.name + ".", suffix=".tmp", delete=False)
        try:
            json.dump(payload, handle, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        # Atomic on POSIX: the reader sees one record or the other.
        os.replace(handle.name, target)
        logger.info("S6 generation published: %s/%s %s status=%s count=%d",
                    trading_day, session, generation_id, status,
                    payload["candidate_count"])
        return True
    except Exception:  # noqa: BLE001 - a publication failure must not
        # cost the scan its signals, and must not leave a torn record.
        logger.warning("could not publish the generation record for %s/%s",
                       trading_day, session, exc_info=True)
        try:
            if target is not None:
                for leftover in target.parent.glob(target.name + ".*.tmp"):
                    leftover.unlink()
        except Exception:  # noqa: BLE001
            pass
        return False


def current(trading_day: str, session: Optional[str] = None,
            strategy_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The current generation record, or None when there is none.

    None means "no generation has been declared" -- a producer that has
    not run, or a store written before generation records existed. It
    does NOT mean zero candidates.
    """
    try:
        path = manifest_path(trading_day, session, strategy_id)
        if not path.exists():
            # Deliberately NO fallback to an unscoped record. One written
            # before records were strategy-scoped may belong to another
            # strategy entirely, and reading it would be the very
            # cross-strategy confusion the scoping exists to stop. None
            # sends the consumer to its historical row inference, which
            # is the safe direction.
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable is not empty
        logger.warning("generation record unreadable for %s/%s",
                       trading_day, session, exc_info=True)
        return None


def age_seconds(record: Optional[Dict[str, Any]], now=None) -> Optional[float]:
    """How long since the generation completed, or None if unknowable."""
    if not record:
        return None
    stamp = record.get("completed_at") or record.get("generated_at")
    if not stamp:
        return None
    try:
        completed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    return (moment - completed).total_seconds()


def is_consumable(record: Optional[Dict[str, Any]], *, variant=None,
                  trading_day=None, session=None, strategy_id=None) -> bool:
    """Is this record a completed generation for the asked-for scope?

    Scope is checked here as well as by the caller's file path, because a
    path is not a guarantee -- the same reason the variant is re-checked
    on every row.
    """
    if not record:
        return False
    if str(record.get("status")) not in CONSUMABLE_STATUSES:
        return False
    if trading_day is not None and str(record.get("trading_day")) != str(trading_day):
        return False
    if session is not None and str(record.get("session")) != str(session):
        return False
    if strategy_id is not None and \
            str(record.get("strategy_id")) != str(strategy_id):
        return False
    if variant is not None and str(record.get("variant")) != str(variant):
        # FAIL CLOSED on a missing variant. This used to skip the check
        # when the record carried none, which let a variant-less record
        # -- the daily profile writes `variant: null` -- satisfy an
        # S6-O question it had nothing to do with.
        return False
    return True
