"""Manual Watchlist -- a reading list for a human, and nothing else.

MANUAL_ONLY
-----------
This package produces a file and a Slack message. It has no other
output. It does not publish candidates, does not size, does not decide,
and does not touch the order path -- `tests/test_watchlist_isolation.py`
asserts that structurally, in both directions:

    watchlist/ -> order modules      : zero imports
    order modules -> watchlist/      : zero imports
    candidate store                  : zero reads, zero writes
    filesystem                       : writes only under its own directory

Why a separate package rather than another analytics report
-----------------------------------------------------------
`scanners/analytics/` answers "which scanner was right", over a month,
after the fact. This answers "what should I look at tomorrow morning",
today, before the fact. The two want different shapes: the analytics
reports are grouped by experiment and are deliberately blind to which
symbols they name, while a watchlist is nothing BUT the symbols.

Keeping them apart also keeps the promise simple. The analytics store is
append-only research data; a bug here can produce a bad reading list,
which a person then ignores. A bug in the analytics store corrupts the
month-1 dataset the whole exercise exists to produce.

Two stages
----------
    D   after the daily scanners close  -> Tomorrow Watchlist  (file only)
    D+1 after the premarket scanner run -> Today Watchlist     (file + Slack)

The first stage is deliberately silent. An 18:45 ET message is a message
nobody acts on before sleeping, and by the morning the premarket scan
has already changed the picture -- so the evening pass writes its
reasoning down and the morning pass is the one that speaks.
"""

from watchlist.config import (  # noqa: F401
    MANUAL_WATCH_VERSION,
    SLACK_TOP_N,
    SLACK_TOP_N_MAX,
    FILE_TOP_N,
)
