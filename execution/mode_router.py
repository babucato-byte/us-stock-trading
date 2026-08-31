"""One door. Which side of it an intent goes through is a config value.

    Strategy / Scanner
            |
            v
      BUY / SELL intent
            |
            v
      mode_router.route()
        /              \\
     LIVE              PAPER
       |                  |
       v                  v
  Common Execution   Virtual Execution
  Engine                  |
       |                  v
       v            (never a broker)
      KIS

Why a router rather than a check inside each strategy
-----------------------------------------------------
"Is this strategy live?" answered in six places is six chances to answer
it differently, and the dangerous answer is the one that says yes by
accident. It is asked here, once, against
`config/scanner_live_mode.py` -- the same table that already refuses
unless exactly one scanner is live.

The property that matters
-------------------------
Promotion is a mode change. A strategy that runs in PAPER emits exactly
the same intents it would emit LIVE; flipping its row from
DISCOVERY_ONLY to LIMITED_LIVE changes which engine receives them and
nothing else. If promotion required a strategy rewrite, everything
measured in PAPER would describe code that no longer exists.

Fails closed
------------
An unreadable mode table routes to PAPER. The failure mode of guessing
LIVE is a real order from a strategy nobody promoted; the failure mode
of guessing PAPER is a missed opportunity that shows up in the funnel.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MODE_LIVE = "LIVE"
MODE_PAPER = "PAPER"

#: Why an intent was routed to PAPER when the caller may have expected
#: otherwise. Recorded so a strategy that quietly stopped trading is
#: visible.
REASON_NOT_LIVE = "STRATEGY_NOT_LIVE"
REASON_MODE_UNREADABLE = "LIVE_MODE_TABLE_UNREADABLE"
REASON_LIVE = "STRATEGY_IS_LIVE"


def mode_for(scanner_name, *, modes=None) -> Dict[str, Any]:
    """LIVE or PAPER for `scanner_name`, and why.

    `modes` is injectable so a test can exercise a scenario without
    depending on which strategy the deployment happens to have promoted.
    """
    try:
        from config import scanner_live_mode

        if scanner_live_mode.is_limited_live(scanner_name, modes) \
                if _accepts_modes(scanner_live_mode.is_limited_live) \
                else scanner_live_mode.is_limited_live(scanner_name):
            return {"mode": MODE_LIVE, "reason": REASON_LIVE,
                    "scanner": scanner_name}
        return {"mode": MODE_PAPER, "reason": REASON_NOT_LIVE,
                "scanner": scanner_name}
    except Exception:  # noqa: BLE001 - guessing LIVE would place a real
        # order for a strategy nobody promoted.
        logger.warning("live-mode table unreadable for %s; routing to PAPER",
                       scanner_name, exc_info=True)
        return {"mode": MODE_PAPER, "reason": REASON_MODE_UNREADABLE,
                "scanner": scanner_name}


def _accepts_modes(function) -> bool:
    import inspect

    try:
        return "modes" in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def is_live(scanner_name, *, modes=None) -> bool:
    return mode_for(scanner_name, modes=modes)["mode"] == MODE_LIVE


def route(intent, *, scanner_name, live_execute, paper_execute,
          modes=None) -> Dict[str, Any]:
    """Hand `intent` to whichever engine this scanner is entitled to.

    `live_execute` and `paper_execute` are callables taking the intent.
    They are injected rather than imported so this module depends on
    neither engine -- it decides WHICH, never HOW, and cannot itself
    reach a broker.
    """
    decision = mode_for(scanner_name, modes=modes)
    if decision["mode"] == MODE_LIVE:
        decision["result"] = live_execute(intent)
    else:
        decision["result"] = paper_execute(intent)
    return decision
