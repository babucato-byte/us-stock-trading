"""Named operator commands -- the vocabulary a runbook/CLI/dashboard
button should call, rather than reaching into kill_switch_state.py or
operations/kill_switch.py's lower-level functions directly. Every
command here is explicit about which of ENTRY_OFF/HALT/EMERGENCY_
LIQUIDATE it affects (spec §20) -- there is no single "stop everything"
command that conflates the three.
"""

from operations import kill_switch


def entry_off(*, reason: str, activated_by: str):
    """Blocks new buy entries only -- existing positions remain
    monitorable/closeable. Delegates to the existing kill_switch_state
    ENTRY_DISABLED state."""
    import kill_switch_state
    return kill_switch_state.activate(
        kill_switch_state.ENTRY_DISABLED, reason, activated_by,
    )


def entry_on(*, released_by: str, reason: str = None):
    """Releases ENTRY_OFF back to ACTIVE (new entries permitted again,
    subject to every other order_gate check)."""
    import kill_switch_state
    return kill_switch_state.release(released_by, reason=reason)


def halt(*, reason: str, actor: str):
    """Stops ALL automatic order submission, including exits. Does NOT
    liquidate anything -- existing positions simply stop being actively
    managed until halt() is lifted."""
    kill_switch.set_halt(True, reason=reason, actor=actor)


def unhalt(*, reason: str, actor: str):
    kill_switch.set_halt(False, reason=reason, actor=actor)


def request_emergency_liquidation(*, approved_by: str, reason: str, confirmation_token: str, expected_token: str):
    """Returns an approval decision only -- does not itself place any
    order. Actually liquidating positions after approval remains a
    separate, human-executed action outside this codebase's automatic
    order path (spec §29)."""
    return kill_switch.request_emergency_liquidation_approval(
        approved_by=approved_by, reason=reason,
        confirmation_token=confirmation_token, expected_token=expected_token,
    )
