"""One minute, one bar, and a record of where it came from.

Why a merge needs rules rather than a concatenation
---------------------------------------------------
A symbol joining Tier2 mid-session has a live stream from this moment
and a REST history for everything before it. Both can describe the SAME
minute: the stream saw the minute it joined in, and REST will return
that minute too once it closes.

Appending both produces two bars for one minute. Nothing raises. The
warmup bar count goes up, the duplicate check may or may not catch it
depending on ordering, and any average over the history double-counts
that minute's volume.

Which copy wins
---------------
The STREAM's, for a minute it observed completely. We watched every
print in it; REST is a summary of the same trades and can only agree or
be staler. But a minute the stream saw only PART of -- the minute it
subscribed in -- is exactly the case where REST is better, because REST
has the whole minute and the stream has the tail of it.

That distinction is the entire reason this is not a dict update.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SOURCE_STREAM = "KIS_STREAM"
SOURCE_REST = "REST_BACKFILL"
SOURCE_MERGED = "MERGED"


def _minute_key(bar):
    return getattr(bar, "minute", None)


def merge(*, stream_bars, rest_bars, stream_joined_at=None) -> List:
    """One bar per minute, ordered, each carrying its provenance.

    `stream_joined_at` is when the subscription began. The minute
    containing it was only partly observed by the stream, so REST wins
    there; every later minute the stream saw in full, so the stream
    wins.
    """
    by_minute: Dict[object, object] = {}
    provenance: Dict[object, str] = {}

    for bar in rest_bars or ():
        key = _minute_key(bar)
        if key is None:
            continue
        by_minute[key] = bar
        provenance[key] = SOURCE_REST

    partial_minute = None
    if stream_joined_at is not None:
        partial_minute = stream_joined_at.replace(second=0, microsecond=0)

    for bar in stream_bars or ():
        key = _minute_key(bar)
        if key is None:
            continue
        if key == partial_minute and key in by_minute:
            # The stream joined mid-minute and saw only the tail of it.
            # REST has the whole minute; keeping the stream's partial
            # copy would understate that minute's volume permanently.
            provenance[key] = SOURCE_REST
            continue
        if key in by_minute:
            provenance[key] = SOURCE_MERGED
        else:
            provenance[key] = SOURCE_STREAM
        by_minute[key] = bar

    ordered = [by_minute[k] for k in sorted(by_minute)]
    for bar in ordered:
        try:
            bar.source = provenance[_minute_key(bar)]
        except Exception:  # noqa: BLE001 - provenance is a label, not the bar
            pass
    return ordered


def provenance_counts(bars) -> Dict[str, int]:
    """How much of a history came from where.

    Worth reporting: a warmup that completed almost entirely on REST
    data is a different claim from one the stream filled in, and the
    two should not look identical afterwards.
    """
    counts = {SOURCE_STREAM: 0, SOURCE_REST: 0, SOURCE_MERGED: 0}
    for bar in bars or ():
        name = getattr(bar, "source", None)
        if name in counts:
            counts[name] += 1
    return counts


def duplicate_minutes(bars) -> List:
    """Minutes appearing more than once. Should always be empty after a
    merge -- asserted rather than assumed."""
    seen, repeated = set(), []
    for bar in bars or ():
        key = _minute_key(bar)
        if key in seen:
            repeated.append(key)
        seen.add(key)
    return repeated
