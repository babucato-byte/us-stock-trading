"""Run identity and run status (spec sections 5, 13 and 14).

scanner_run_id
--------------
One id per runner invocation, stamped onto every signal that invocation
produced. Format:

    20260812_DAILY_a81c7f
    <trading day>_<PROFILE>_<random suffix>

The suffix is random, not derived. A content hash would collide across
two runs of the same profile on the same day, which is precisely the
case the id has to keep APART: section 5 says a re-run must not reuse
the previous id, and section 14 needs a failed run and its retry to be
distinguishable in the stored data. Two runs are two observations, taken
from two data snapshots, and merging them would hide a double-run from
every count in the month-end report.

What it buys, concretely: section 22 wants same-run intersections
distinguishable from same-day ones. "The ORB scanner and the gap
scanner both flagged NVDA at 09:50 from the same snapshot" is a much
stronger statement than "both flagged it at some point today" -- the
first is genuine agreement, the second can be two different hours of the
session. Without a run id only the weaker one is computable.

Run status
----------
Section 14's requirement, stated as a small vocabulary. The
distinction that matters is the first one:

    SUCCESS  + candidate_count 0     the scan ran, found nothing
    FAILED   + candidate_count None  the scan could not run

Both would be "0 signals today" in a naive schema, and a month of the
second silently masquerading as the first would look like a market with
no setups rather than a broken pipeline. `candidate_count` is therefore
None -- not 0 -- whenever the count is not a measurement.
"""

import secrets
from typing import Optional

#: The scan ran to completion. `candidate_count` is a real measurement,
#: and 0 means the market offered nothing.
SUCCESS = "SUCCESS"

#: Some scanners completed and at least one did not. Per-scanner status
#: says which; the run's own counts cover only the ones that finished.
PARTIAL = "PARTIAL"

#: Nothing usable was produced. `candidate_count` is None.
FAILED = "FAILED"

#: The market data provider failed for effectively every symbol -- a
#: distinct diagnosis from "the scanners are broken", and the one that
#: usually means "wait and retry" rather than "read the traceback".
FAILED_PROVIDER = "FAILED_PROVIDER"

#: No universe file, or it was unreadable.
FAILED_NO_UNIVERSE = "FAILED_NO_UNIVERSE"

#: No scanner could even be constructed (a bad config, a broken import).
FAILED_NO_SCANNER = "FAILED_NO_SCANNER"

#: A deliberate no-op: the US market was closed. NOT a failure, and not
#: a success either -- there was nothing to scan, so recording it as
#: SUCCESS with 0 candidates would put a phantom zero-signal day into
#: the month-1 dataset for a session that never happened.
SKIPPED_MARKET_CLOSED = "SKIPPED_MARKET_CLOSED"

FAILURE_STATUSES = frozenset(
    {FAILED, FAILED_PROVIDER, FAILED_NO_UNIVERSE, FAILED_NO_SCANNER})

ALL_STATUSES = frozenset(
    {SUCCESS, PARTIAL, SKIPPED_MARKET_CLOSED} | set(FAILURE_STATUSES))


#: How the candidate hand-off ended. Separate from the run status above,
#: because the two answer different questions and a run can succeed at
#: one while failing the other.
PUBLICATION_OK = "OK"

#: Nothing to hand off -- this run contained no publishing scanner.
PUBLICATION_NOT_APPLICABLE = "NOT_APPLICABLE"

#: The shared candidate store could not be located, so a publishing scan
#: published NOTHING.
#:
#: Its own status rather than a general FAILED, because the operator
#: action is specific and immediate: the producer's environment is wrong.
#: Before this existed, the publisher fell back to a runtime-local
#: directory and reported success -- so a broken hand-off and a quiet
#: market produced the same record, and the consumer's "0 candidates"
#: was the first and only symptom.
PUBLICATION_CONFIG_ERROR = "PRODUCER_CONFIG_ERROR"

#: The store was found and the write itself failed.
PUBLICATION_WRITE_FAILED = "PUBLICATION_WRITE_FAILED"

PUBLICATION_FAILURES = frozenset(
    {PUBLICATION_CONFIG_ERROR, PUBLICATION_WRITE_FAILED})


def new_run_id(trading_day: str, profile: Optional[str] = None) -> str:
    """A fresh, unique id for one runner invocation.

    `secrets.token_hex` rather than a counter or a timestamp hash: a
    counter needs shared state between processes, and a
    seconds-resolution timestamp collides when a retry follows fast
    enough -- which is exactly when two runs most need telling apart.
    """
    label = (profile or "adhoc").upper().replace(" ", "_")
    return f"{str(trading_day).replace('-', '')}_{label}_{secrets.token_hex(3)}"


def is_failure(status: Optional[str]) -> bool:
    return status in FAILURE_STATUSES


def candidate_count_for(status: Optional[str], signal_count: int) -> Optional[int]:
    """The count to record, or None when it is not a measurement.

    Section 14: a failed run has no candidate count. Reporting 0 would
    assert "the scanners looked and found nothing", which is a claim
    only a completed scan may make.
    """
    if is_failure(status) or status == SKIPPED_MARKET_CLOSED:
        return None
    return int(signal_count)
