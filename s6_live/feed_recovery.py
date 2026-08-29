"""What is safe to do while the feed is broken, and how to come back.

Two different questions
-----------------------
A disconnect makes new entries unsafe immediately: a breakout decided on
a frozen view of a moving market is the failure the whole realtime layer
exists to prevent. So DATA_STALE blocks new BUYs, and that part is easy.

The part that is not easy is that the same disconnect does NOT make it
safe to stop watching a position we already hold. An OPEN position has a
stop; an EXIT_PENDING one has a sell that must go out. Blocking
everything equally would leave a real holding unmanaged for the length
of the outage -- trading the risk of a bad entry for the risk of an
unmanaged exit, which is the worse of the two.

So entries stop and exits keep running on whatever data can be had,
labelled DATA_DEGRADED so nothing downstream mistakes a REST fallback
price for a streaming one.

Coming back is ordered
----------------------
Reconnecting is not "the socket is up". Subscriptions have to be
rebuilt, the missing interval backfilled, features recomputed on the
merged history, and that history checked -- and only then is the symbol
watchable again. Skipping to the end gives a symbol whose feed status
reads LIVE and whose indicators were computed across the hole.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: The feed is not delivering. New entries are refused.
DATA_STALE = "DATA_STALE"
#: Held positions are being managed on fallback data. Not an error --
#: a statement about what the numbers are worth.
DATA_DEGRADED = "DATA_DEGRADED"
DATA_LIVE = "DATA_LIVE"

#: The recovery sequence, in order. A symbol is watchable only after the
#: last one; each earlier step names where it stopped.
STEP_RECONNECT = "RECONNECTED"
STEP_RESUBSCRIBE = "SUBSCRIPTIONS_REBUILT"
STEP_BACKFILL = "GAP_BACKFILLED"
STEP_RECOMPUTE = "FEATURES_RECOMPUTED"
STEP_VALIDATE = "INTEGRITY_VALIDATED"
RECOVERY_ORDER = (STEP_RECONNECT, STEP_RESUBSCRIBE, STEP_BACKFILL,
                  STEP_RECOMPUTE, STEP_VALIDATE)


def entries_permitted(feed_status) -> bool:
    """New BUYs need a live feed. Nothing else will do."""
    return feed_status == DATA_LIVE


def exit_management_permitted(feed_status) -> bool:
    """Always. A held position is not abandoned because the feed broke.

    This returns True even when entries are refused -- that asymmetry is
    the point of the module.
    """
    return True


def data_quality(*, feed_live, fallback_available) -> str:
    if feed_live:
        return DATA_LIVE
    return DATA_DEGRADED if fallback_available else DATA_STALE


def recovery_complete(steps_done) -> bool:
    """Every step, in order, with none skipped."""
    done = list(steps_done or ())
    return done == list(RECOVERY_ORDER)


def next_step(steps_done):
    """What recovery must do next, or None when it is finished."""
    done = list(steps_done or ())
    for index, step in enumerate(RECOVERY_ORDER):
        if index >= len(done) or done[index] != step:
            return step
    return None


def watchable_after_recovery(steps_done, *, integrity_sound) -> bool:
    """A symbol is watchable only after the full sequence AND a sound
    history. Either alone is the failure this guards: a rebuilt
    subscription with indicators computed across the hole reads LIVE and
    is wrong."""
    return recovery_complete(steps_done) and bool(integrity_sound)


def describe(*, feed_live, fallback_available, steps_done=None,
             now=None) -> dict:
    quality = data_quality(feed_live=feed_live,
                           fallback_available=fallback_available)
    return {
        "observed_at": (now or datetime.now(timezone.utc)).isoformat(),
        "data_quality": quality,
        "entries_permitted": entries_permitted(quality),
        "exit_management_permitted": exit_management_permitted(quality),
        "recovery_next_step": next_step(steps_done),
        "recovery_complete": recovery_complete(steps_done),
    }
