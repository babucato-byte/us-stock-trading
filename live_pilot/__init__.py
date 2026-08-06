"""T9: the real-time pilot harness -- scanner, entry conditions and exit
conditions driven against LIVE market data on a repeating intraday tick.

The point of this package is to answer a question no unit test can:
"given today's actual prices, actual account and actual candidate list,
what does this system decide, tick after tick, for a whole session?"

It adds NO trading rule of its own. Every verdict it records comes from
code that already exists and is already tested:

    entry  OBSERVE -> scripts/run_shadow_mode.run_once()
           ARMED   -> kis_live_trading.run_live_buy_entry_cycle()
    exit   OBSERVE -> scripts/run_shadow_exit_evaluation.run_once()
           ARMED   -> kis_position_manager.sync_kis_fills_and_manage_exits()

Which of the two postures a tick runs in is decided by the operator's
environment, never by this package: see live_pilot.runner.resolve_posture().
The default -- every flag unset -- is OBSERVE, and in OBSERVE this
package never imports a module that can submit an order.
"""

from live_pilot.posture import (
    POSTURE_ARMED,
    POSTURE_OBSERVE,
    PostureDecision,
    resolve_posture,
)

__all__ = [
    "POSTURE_ARMED",
    "POSTURE_OBSERVE",
    "PostureDecision",
    "resolve_posture",
]
