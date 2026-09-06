"""How long a Signal lives, and on which clock. One boundary, asked once.

The two ages that were one number
---------------------------------
A candidate has two ages and the buy cycle used to measure neither:

    strategy age   how old the market observation is -- when the ORB
                   breakout happened, when the scanner last saw the bars.
                   Owned by the scanner's freshness contract, the S6 scan
                   cycle and the precision watch. Not this module's.

    pipeline age   how long a candidate the cycle has ACCEPTED has been
                   moving through quoting, sizing, the execution lock,
                   revalidation and the gate on its way to the broker.
                   This module's.

`SIGNAL_VALID_SECONDS = 120` in `kis_live_trading` is the pipeline
budget. What it was measured against decided whether it meant anything:

  * before 904c7f5 the gate compared `expires_at` with the CYCLE clock
    -- the same `now` the Signal was created from -- so the age was
    always zero and the budget was never enforced for any source.
  * 904c7f5 measured it at submit against the wall clock, from the
    cycle's START. For S6 the start is before the precision watch, which
    is most of the cycle, so every candidate arrived at submit already
    expired: 2 of 2 on 2026-09-02, REVALIDATION_SIGNAL_EXPIRED.
  * f25a076 removed the wall-clock measurement and went back to the
    frozen clock. Nothing expired again, because nothing was measured.

Neither reading was right. The clock started in the wrong place and the
budget was one number for three strategies with different pipelines.

What this decides
-----------------
The SOURCE that offered the candidate says how long its pipeline budget
is, through one optional hook:

    source.signal_valid_seconds() -> positive finite seconds

A source that does not implement it keeps exactly the historical
behaviour: the default budget, created from the cycle clock and never
measured at submit. That is the legacy watchlist, S1 and S2, byte for
byte. A source that DOES implement it opts into the real measurement:
its Signal is created when the candidate is accepted (qualified), and
the under-lock revalidation compares `expires_at` with the wall clock
before the order is sent. Nothing rebuilds the Signal later.

Fail closed. A hook that answers with something unusable -- None, zero,
a negative number, a string, an exception -- stops the cycle before any
candidate is read, with its own reason code. Guessing a budget for a
source that meant to state one would be the historical defect again.

The default lives in `kis_live_trading`, where it always has; this
module receives it and never redefines it.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

#: The optional hook a candidate source implements.
HOOK = "signal_valid_seconds"

#: `policy_source` for the historical behaviour.
DEFAULT_POLICY = "DEFAULT"

#: Cycle-level reason code when a source's policy cannot be resolved.
REASON_UNRESOLVED = "SIGNAL_VALIDITY_UNRESOLVED"

#: Per-candidate reason code when a measured source offers a candidate
#: whose provenance carries no usable observation time.
REASON_SOURCE_TIMESTAMP_UNUSABLE = "SOURCE_SIGNAL_TIMESTAMP_UNUSABLE"


class SignalValidityError(Exception):
    """The source's policy could not be resolved. The cycle must stop."""


@dataclass(frozen=True)
class SignalValidity:
    """One source's pipeline budget and the clock it is measured on."""

    valid_for_seconds: float
    policy_source: str
    #: True when the source supplied the budget. Then the Signal is
    #: anchored at acceptance and measured at submit against the wall
    #: clock. False keeps the historical cycle-clock semantics.
    measured_at_submit: bool

    @property
    def requires_source_timestamp(self) -> bool:
        """A measured source must say WHEN it observed the market.

        The pipeline clock starts at acceptance, deliberately not at the
        source's observation time -- but a candidate whose provenance
        cannot say when it was observed has no strategy age at all, and
        a pipeline budget is not a substitute for one.
        """
        return self.measured_at_submit

    def anchor(self, cycle_now: datetime, *, clock: Optional[Callable[[], datetime]] = None
               ) -> datetime:
        """When the Signal's lifetime starts.

        The moment of acceptance for a measured source; the cycle clock
        for the default, exactly as before.
        """
        if not self.measured_at_submit:
            return cycle_now
        return (clock or _utcnow)()

    def submit_moment(self, cycle_now: datetime, *,
                      clock: Optional[Callable[[], datetime]] = None
                      ) -> Optional[datetime]:
        """The clock to measure the Signal against at submit, or None.

        None means "do not measure" -- the default policy's historical
        behaviour, in which the gate compares against the cycle clock
        and the revalidation asks nothing.
        """
        if not self.measured_at_submit:
            return None
        return (clock or _utcnow)()

    def as_dict(self) -> dict:
        return {"valid_for_seconds": self.valid_for_seconds,
                "policy_source": self.policy_source,
                "measured_at_submit": self.measured_at_submit}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _usable_seconds(value: Any) -> Optional[float]:
    """A positive finite NUMBER. Strings are refused even when they
    parse: a policy is stated in seconds, not in text that happens to
    look like some."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def resolve(source, *, default_seconds) -> SignalValidity:
    """The policy for this source. Asked once per cycle, before any read.

    Raises SignalValidityError when the source has the hook and it does
    not answer with a positive finite number of seconds. A source
    without the hook gets the default, unmeasured.
    """
    hook = getattr(source, HOOK, None)
    if hook is None:
        default = _usable_seconds(default_seconds)
        if default is None:
            raise SignalValidityError(
                f"default signal validity {default_seconds!r} is not a "
                "positive finite number of seconds")
        return SignalValidity(valid_for_seconds=default,
                              policy_source=DEFAULT_POLICY,
                              measured_at_submit=False)

    name = getattr(source, "name", None) or type(source).__name__
    if not callable(hook):
        raise SignalValidityError(
            f"source {name!r} exposes {HOOK} but it is not callable")
    try:
        answer = hook()
    except Exception as exc:  # noqa: BLE001 - the policy failed; no guess
        raise SignalValidityError(
            f"source {name!r} could not state its signal validity: "
            f"{type(exc).__name__}: {exc}") from exc
    seconds = _usable_seconds(answer)
    if seconds is None:
        raise SignalValidityError(
            f"source {name!r} answered {answer!r} for {HOOK}; a positive "
            "finite number of seconds is required")
    return SignalValidity(valid_for_seconds=seconds, policy_source=str(name),
                          measured_at_submit=True)


def parse_timestamp(stamp) -> Optional[datetime]:
    """An aware datetime from an ISO stamp, or None if it is unusable."""
    if stamp is None:
        return None
    if isinstance(stamp, datetime):
        parsed = stamp
    else:
        text = str(stamp).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed


def source_timestamp_refusal(validity: SignalValidity, stamp) -> Optional[str]:
    """Why a candidate's provenance is not good enough, or None.

    Only a measured policy asks. The default policy never did, and a
    legacy candidate without a stamp must keep trading exactly as it
    always has.
    """
    if not validity.requires_source_timestamp:
        return None
    if parse_timestamp(stamp) is None:
        return (f"{REASON_SOURCE_TIMESTAMP_UNUSABLE}: the candidate's "
                f"source signal timestamp {stamp!r} is missing or not an "
                "aware ISO-8601 instant; the pipeline clock is not a "
                "substitute for the observation time")
    return None


def strategy_age_seconds(stamp, moment: datetime) -> Optional[float]:
    """Seconds from the source's observation to `moment`; None if unknown.

    Recorded, never decided on here: strategy age belongs to the
    scanner's freshness contract and the precision watch.
    """
    observed = parse_timestamp(stamp)
    if observed is None or moment is None:
        return None
    return (moment - observed).total_seconds()
