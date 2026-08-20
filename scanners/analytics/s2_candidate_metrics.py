"""What happened to an S2 candidate nobody bought.

The four measures section 10 asks for are all answers to "was the setup
right, independently of whether we acted on it":

    time_to_peak            how long the favourable move took to top out
    time_to_vwap_failure    when it first lost VWAP after the signal
    volume_decay            how fast the volume that triggered it drained
    holding_duration        how long the candidate was still valid

Why these, for S2 specifically
------------------------------
S2's thesis is that volume arrives before the price move, and its score
rewards a QUIET price (30 of 100 points). So the ordinary forward-return
horizons answer a different question than the one S2 makes a claim about.
A candidate whose 5-day return is +3% tells you little; a candidate that
peaked 40 minutes in, lost VWAP at minute 55 and whose volume was back to
normal by lunch tells you the thesis did not hold that day, regardless of
where the close landed.

Absent is reported as absent
----------------------------
Every function here returns None when the data cannot answer, and None
means "not measured" -- never 0, never a default. With four trading days
behind this dataset the temptation to fill a gap with a plausible number
is exactly the thing that would make the first month's conclusions
unfalsifiable. A zero `time_to_vwap_failure` reads as "failed instantly",
which is a finding; the truth is usually "there were no minute bars".

Nothing here is a threshold. These are measurements, and no value
computed in this module gates an order or feeds back into a scanner
condition.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Reported instead of a number when the input could not answer.
NOT_MEASURED = None

#: What `volume_decay` compares against: the candidate's own signal-time
#: volume multiple. Not a fixed multiple, because "decayed" means
#: "relative to what triggered this", and a shared constant would make a
#: 6x candidate and a 1.6x candidate decay at the same absolute level.
DECAY_REFERENCE = "signal_volume_multiple"

#: The fraction of the signal's volume multiple below which the volume
#: that justified the candidate is considered drained. Half is a
#: description, not a tuned parameter: it is the point at which the
#: excess volume over baseline has halved, and it is recorded on every
#: row so a later study can recompute with a different definition rather
#: than inherit this one.
DECAY_FRACTION = 0.5


def _finite(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def _minutes_between(start, end) -> Optional[float]:
    try:
        delta = (end - start).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None
    return delta if delta >= 0 else None


def time_to_peak(bars: Sequence[Dict[str, Any]], *, signal_time=None
                 ) -> Optional[float]:
    """Minutes from the signal to the highest HIGH that followed it.

    Measured on the high, not the close: the question is how long the
    favourable excursion took to top out, and a candidate that spiked at
    minute 20 and closed flat did peak at minute 20.

    Ties go to the EARLIEST bar. A flat top that stays at its high for an
    hour peaked when it first got there; reporting the last such bar
    would make a stalled move look like a slow, continuing one.
    """
    usable = [b for b in (bars or []) if _finite(b.get("high")) is not None
              and b.get("timestamp") is not None]
    if not usable:
        return NOT_MEASURED
    start = signal_time or usable[0]["timestamp"]
    best = None
    for bar in usable:
        high = _finite(bar["high"])
        if best is None or high > best[0]:
            best = (high, bar["timestamp"])
    return _minutes_between(start, best[1])


def time_to_vwap_failure(bars: Sequence[Dict[str, Any]], *, signal_time=None
                         ) -> Optional[float]:
    """Minutes until the close first printed below VWAP.

    None means two different things that must not be conflated with each
    other or with zero: either there were no usable bars, or the
    candidate never lost VWAP in the window. The second is returned as
    `float('inf')`-free None too, so callers distinguish it via
    `vwap_held`, below -- a numeric sentinel would end up in an average.
    """
    usable = [b for b in (bars or [])
              if _finite(b.get("close")) is not None
              and _finite(b.get("vwap")) is not None
              and b.get("timestamp") is not None]
    if not usable:
        return NOT_MEASURED
    start = signal_time or usable[0]["timestamp"]
    for bar in usable:
        if _finite(bar["close"]) < _finite(bar["vwap"]):
            return _minutes_between(start, bar["timestamp"])
    return NOT_MEASURED


def vwap_held(bars: Sequence[Dict[str, Any]]) -> Optional[bool]:
    """True if VWAP was never lost, False if it was, None if unmeasurable.

    The companion to `time_to_vwap_failure`: without it, "no failure
    time" is ambiguous between "held all session" and "no data", and
    those two support opposite conclusions.
    """
    usable = [b for b in (bars or [])
              if _finite(b.get("close")) is not None
              and _finite(b.get("vwap")) is not None]
    if not usable:
        return None
    return not any(_finite(b["close"]) < _finite(b["vwap"]) for b in usable)


def volume_decay_minutes(bars: Sequence[Dict[str, Any]], *,
                         signal_volume_multiple, baseline_volume,
                         signal_time=None, fraction: float = DECAY_FRACTION
                         ) -> Optional[float]:
    """Minutes until the excess volume over baseline had fallen by `fraction`.

    Relative to the candidate's OWN trigger, not to a fixed multiple: a
    6x candidate dropping to 3x has lost as much of its excess as a 1.6x
    candidate dropping to 1.3x, and a shared absolute threshold would
    call only the first one decayed.

    Requires a real baseline. Without `baseline_volume` there is nothing
    to be a multiple OF, and inferring one from the window would measure
    the candidate against itself.
    """
    multiple = _finite(signal_volume_multiple)
    baseline = _finite(baseline_volume)
    if multiple is None or baseline is None or baseline <= 0:
        return NOT_MEASURED
    excess = multiple - 1.0
    if excess <= 0:
        # Nothing was elevated, so nothing can decay. Not a zero-minute
        # decay -- that would report the quietest candidates as the
        # fastest to fade.
        return NOT_MEASURED
    target_multiple = 1.0 + excess * (1.0 - float(fraction))

    usable = [b for b in (bars or []) if _finite(b.get("volume")) is not None
              and b.get("timestamp") is not None]
    if not usable:
        return NOT_MEASURED
    start = signal_time or usable[0]["timestamp"]
    for bar in usable:
        if (_finite(bar["volume"]) / baseline) <= target_multiple:
            return _minutes_between(start, bar["timestamp"])
    return NOT_MEASURED


def holding_duration_minutes(bars: Sequence[Dict[str, Any]], *,
                             signal_time=None) -> Optional[float]:
    """How long the observation window actually covered.

    Reported alongside every other measure because they are all bounded
    by it: "never lost VWAP" over eleven minutes of bars is not the same
    finding as "never lost VWAP" over a full session, and without this
    field the two are written down identically.
    """
    usable = [b for b in (bars or []) if b.get("timestamp") is not None]
    if not usable:
        return NOT_MEASURED
    start = signal_time or usable[0]["timestamp"]
    return _minutes_between(start, usable[-1]["timestamp"])


def measure(bars: Sequence[Dict[str, Any]], *, signal_volume_multiple=None,
            baseline_volume=None, signal_time=None) -> Dict[str, Any]:
    """All four measures for one candidate, plus what they were computed from.

    The provenance fields are not decoration: `bars_seen` and
    `holding_duration_minutes` are what make a None interpretable. A row
    with `time_to_vwap_failure: None` and `bars_seen: 0` says the data
    was missing; the same None with `bars_seen: 390` and
    `vwap_held: true` says the candidate held VWAP all day. Those are
    opposite readings of the same field.
    """
    bars = list(bars or [])
    return {
        "time_to_peak_minutes": time_to_peak(bars, signal_time=signal_time),
        "time_to_vwap_failure_minutes": time_to_vwap_failure(
            bars, signal_time=signal_time),
        "vwap_held": vwap_held(bars),
        "volume_decay_minutes": volume_decay_minutes(
            bars, signal_volume_multiple=signal_volume_multiple,
            baseline_volume=baseline_volume, signal_time=signal_time),
        "holding_duration_minutes": holding_duration_minutes(
            bars, signal_time=signal_time),
        "bars_seen": len(bars),
        "decay_reference": DECAY_REFERENCE,
        "decay_fraction": DECAY_FRACTION,
    }


def summarise(rows: List[Dict[str, Any]], field: str,
              *, minimum: int = 1) -> Dict[str, Any]:
    """Mean of `field` across rows, with the count it came from.

    Nulls are excluded rather than read as zero, matching
    `scanners.analytics.common`. The count travels with the mean because
    an average of two and an average of two hundred are not comparable,
    and a summary that hides which one it is invites the reader to treat
    them the same.
    """
    values = [_finite(row.get(field)) for row in rows or []]
    values = [v for v in values if v is not None]
    if len(values) < max(1, minimum):
        return {"mean": None, "n": len(values), "sufficient": False}
    return {"mean": sum(values) / len(values), "n": len(values),
            "sufficient": True}
