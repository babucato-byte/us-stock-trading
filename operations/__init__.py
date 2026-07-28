"""Operational controls for the KIS live-order path (spec §20/§26/§27).
These modules deliberately WRAP the existing, already-tested
kill_switch_state.py/notification_health.py/slack_utils.py rather than
reimplementing them (spec §5: 기존 기능 재사용) -- the one genuinely new
concept this package adds is the three-way ENTRY_OFF/HALT/
EMERGENCY_LIQUIDATE distinction spec §20 requires, which the existing
2-state (ACTIVE/ENTRY_DISABLED) kill_switch_state.py does not itself
model (it has no "stop everything including exits" state, and no
separate-approval full-liquidation concept).
"""
