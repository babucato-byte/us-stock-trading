"""Which of the two execution postures a pilot tick runs in.

Separated into its own module with no heavy imports so the decision can
be read, tested and asserted about without pulling in a broker, a
scanner or a database -- and so a static check can prove that reading
the posture cannot itself reach an order path.

    OBSERVE  the default. Everything is evaluated against live data;
             nothing can be submitted, because the modules that submit
             are never imported on this path.
    LIMITED_LIVE_BOOTSTRAP
             a SEPARATE capability, not a weaker ARMED. It exists for one
             reason: five wire values can only be established by a real
             live response, and confirming them by switching the whole
             system to ARMED would mean the first real order is placed by
             the general trading path rather than by a one-shot with
             every precondition checked.

             It authorises the bootstrap script and nothing else. The
             general scanner -> live order path stays blocked, because
             that path reads the three flags below and this posture does
             not set them.
    ARMED    the operator turned the existing live-order flags on. The
             pilot then drives the ALREADY-EXISTING live entry and exit
             cycles. This module does not turn anything on; it only
             reports what the environment says.

There is deliberately no "pilot arms itself" switch. The flags below are
the same three the Order Gate, the live service unit and the preflight
already use, so a pilot run can never be armed by a control that the
rest of the system does not also see.
"""

import os
from dataclasses import dataclass

from broker.broker_config import env_bool

POSTURE_OBSERVE = "OBSERVE"
POSTURE_LIMITED_LIVE_BOOTSTRAP = "LIMITED_LIVE_BOOTSTRAP"
POSTURE_ARMED = "ARMED"

# The three flags that, together, mean "live entries are on". Named here
# once so the pilot cannot drift from execution/order_gate.py's view.
FLAG_LIVE_ORDER = "KIS_LIVE_ORDER_ENABLED"
FLAG_ROLLOUT = "LIVE_ROLLOUT_ENABLED"
FLAG_ENTRY_DISABLED = "ENTRY_DISABLED"

# A separate capability, deliberately NOT one of the three above. Setting
# it does not enable live entries: the order gate, the live service unit
# and the preflight all read the three flags, none of which this touches.
# What it does is make the bootstrap posture reachable at all.
FLAG_BOOTSTRAP_ENABLED = "LIVE_BOOTSTRAP_ENABLED"


@dataclass(frozen=True)
class PostureDecision:
    """`posture` is what the tick will do. `reason` says why, in the
    operator's terms. `flags` is the raw evidence, so a recorded tick
    can be re-derived from the log alone."""

    posture: str
    reason: str
    live_order_enabled: bool
    rollout_enabled: bool
    entry_disabled: bool

    bootstrap_enabled: bool = False

    @property
    def armed(self):
        return self.posture == POSTURE_ARMED

    @property
    def bootstrap(self):
        return self.posture == POSTURE_LIMITED_LIVE_BOOTSTRAP

    def as_dict(self):
        return {
            "posture": self.posture,
            "posture_reason": self.reason,
            "live_order_enabled": self.live_order_enabled,
            "rollout_enabled": self.rollout_enabled,
            "entry_disabled": self.entry_disabled,
            "bootstrap_enabled": self.bootstrap_enabled,
        }


def resolve_posture(env=None):
    """Reads the environment EVERY time it is called.

    Deliberately not cached and not resolved once at start-up: an
    operator who clears ENTRY_DISABLED mid-session, or who sets it to
    stop entries, should be obeyed on the next tick rather than at the
    next restart. The expensive direction (OBSERVE -> ARMED) requires
    all three flags, so a partial edit degrades to OBSERVE, never the
    other way round.
    """
    mapping = os.environ if env is None else env
    live_order = env_bool(mapping, FLAG_LIVE_ORDER, False)
    rollout = env_bool(mapping, FLAG_ROLLOUT, False)
    entry_disabled = env_bool(mapping, FLAG_ENTRY_DISABLED, False)
    bootstrap_enabled = env_bool(mapping, FLAG_BOOTSTRAP_ENABLED, False)

    # ARMED is evaluated FIRST. The bootstrap capability is for reaching
    # a live response while ARMED is still blocked; once the three flags
    # really are on, ARMED is the honest description and the bootstrap
    # capability must not shadow it.
    if live_order and rollout and not entry_disabled:
        return PostureDecision(
            posture=POSTURE_ARMED,
            reason="all three live-entry flags are set by the operator",
            live_order_enabled=live_order, rollout_enabled=rollout,
            entry_disabled=entry_disabled, bootstrap_enabled=bootstrap_enabled,
        )

    if bootstrap_enabled:
        # Reachable with the three live flags in their SAFE state, which
        # is the point: the general path stays blocked and only the
        # one-shot script may act. The script itself still verifies every
        # precondition and still needs LIVE_BOOTSTRAP_ACK at run time --
        # this posture is permission to attempt, never permission to act.
        return PostureDecision(
            posture=POSTURE_LIMITED_LIVE_BOOTSTRAP,
            reason=f"{FLAG_BOOTSTRAP_ENABLED} is set; one-shot bootstrap only, "
                   "the general live path stays blocked",
            live_order_enabled=live_order, rollout_enabled=rollout,
            entry_disabled=entry_disabled, bootstrap_enabled=bootstrap_enabled,
        )

    if not live_order:
        reason = f"{FLAG_LIVE_ORDER} is not enabled"
    elif not rollout:
        reason = f"{FLAG_ROLLOUT} is not enabled"
    else:
        reason = f"{FLAG_ENTRY_DISABLED} is set"
    return PostureDecision(
        posture=POSTURE_OBSERVE, reason=reason, live_order_enabled=live_order,
        rollout_enabled=rollout, entry_disabled=entry_disabled,
        bootstrap_enabled=bootstrap_enabled,
    )


def contradictory_posture(env=None):
    """Returns a description of a half-enabled configuration, or None.

    Same rule `scripts/preflight_kis_live.py::check_flag_consistency()`
    enforces: an order flag on while entries are disabled, or on without
    the rollout, means somebody enabled live trading part-way. The pilot
    refuses to start rather than quietly running in OBSERVE and letting
    the operator believe it was armed.
    """
    mapping = os.environ if env is None else env
    live_order = env_bool(mapping, FLAG_LIVE_ORDER, False)
    rollout = env_bool(mapping, FLAG_ROLLOUT, False)
    entry_disabled = env_bool(mapping, FLAG_ENTRY_DISABLED, False)
    if live_order and entry_disabled:
        return f"{FLAG_LIVE_ORDER}=true while {FLAG_ENTRY_DISABLED}=true"
    if live_order and not rollout:
        return f"{FLAG_LIVE_ORDER}=true while {FLAG_ROLLOUT} is not true"
    if rollout and not live_order:
        return f"{FLAG_ROLLOUT}=true while {FLAG_LIVE_ORDER} is not true"
    return None
