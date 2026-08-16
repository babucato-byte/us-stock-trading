"""ENTRY_OFF / HALT / EMERGENCY_LIQUIDATE (spec §20):

    ENTRY_OFF          = 신규 매수 차단 (maps directly onto the existing
                          kill_switch_state.ENTRY_DISABLED state -- exits
                          stay allowed, exactly as that module already
                          enforces via is_liquidation_allowed())
    HALT               = 자동 주문 실행 전체 중지, 포함 매도 (a NEW
                          concept -- the existing 2-state kill switch has
                          no "stop everything, including exits" state; a
                          separate halt flag file is used so existing
                          kill_switch_state.py itself is never modified)
    EMERGENCY_LIQUIDATE = 별도 명시적 승인 (NOT auto-triggered by
                          anything in this codebase -- this module only
                          gates/records the decision; it never itself
                          places a liquidation order. Actually executing
                          one remains a human-initiated action per
                          spec §29's "긴급 전량 청산" approval requirement)

This module never calls the KIS broker or any strategy code -- it is
purely a decision/state facade that execution_engine.py and the future
strategy-wiring layer both consult before attempting any order.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import kill_switch_state

BASE_DIR = Path(__file__).resolve().parent.parent
_HALT_FILE = BASE_DIR / "OPERATIONS_HALT_STATE.json"


class OperationsError(Exception):
    """Raised on any operations-control failure. Callers must treat this
    as a hard block (fail-closed -- an error reading halt state means
    treat the system as halted, never as clear)."""


def _resolve_halt_path():
    override = os.environ.get("OPERATIONS_HALT_STATE_FILE")
    return Path(override) if override else _HALT_FILE


def is_entry_allowed() -> bool:
    """ENTRY_OFF check -- delegates entirely to the existing, already-
    tested kill_switch_state.is_entry_allowed()."""
    return kill_switch_state.is_entry_allowed()


def is_halted() -> bool:
    """HALT check -- True means no automatic order (buy OR sell) may be
    submitted. Fail-closed: a missing/corrupted halt-state file is
    treated as NOT halted only when explicitly absent (matches this
    codebase's file-based state conventions elsewhere -- absence is the
    documented default, corruption is fail-closed to halted)."""
    path = _resolve_halt_path()
    if not path.exists():
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise OperationsError(f"OPERATIONS_HALT_STATE file is corrupted, failing closed to halted: {exc}") from exc
    return bool(data.get("halted", False))


def set_halt(halted: bool, *, reason: str, actor: str) -> None:
    # Read the PREVIOUS state first so the notification below fires on
    # the false -> true transition only. Repeated set_halt(True) calls --
    # which a polling caller or a retry can easily produce -- must not
    # re-alert. A read failure here is not allowed to stop the write:
    # setting HALT is the safety action and it happens regardless.
    try:
        was_halted = is_halted()
    except Exception:  # noqa: BLE001 -- a corrupt file must not block halting
        was_halted = None

    path = _resolve_halt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "halted": halted, "reason": reason, "actor": actor,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    # After the write: the durable HALT state is what protects trading,
    # and it now stands whether or not this message is delivered.
    if halted and was_halted is not True:
        from operations import live_notifications

        live_notifications.notify(
            live_notifications.HALT_ACTIVATED,
            {"reason": reason, "source": actor, "new_entries_blocked": True,
             "note": "all automatic orders blocked until HALT is cleared"},
        )


def is_automatic_order_allowed() -> bool:
    """The single check the execution/order path should use for "is any
    automatic order attempt permitted at all right now" -- True only if
    NOT halted (ENTRY_OFF alone does not block exits, matching
    kill_switch_state's existing is_liquidation_allowed() semantics, but
    HALT blocks everything)."""
    return not is_halted()


@dataclass(frozen=True)
class EmergencyLiquidationApproval:
    approved: bool
    approved_by: str
    reason: str
    approved_at: datetime


def request_emergency_liquidation_approval(*, approved_by: str, reason: str, confirmation_token: str, expected_token: str) -> EmergencyLiquidationApproval:
    """EMERGENCY_LIQUIDATE requires an explicit, separately-supplied
    confirmation token that must exactly match `expected_token` (an
    operator-configured secret/passphrase, never a code constant) --
    this is deliberately NOT a plain boolean flag, so a single
    misconfigured `True` default can never silently authorize a full
    liquidation. This function only returns a decision; it never
    executes anything."""
    if not confirmation_token or confirmation_token != expected_token:
        raise OperationsError("emergency liquidation confirmation token does not match -- not approved")
    if not approved_by or not approved_by.strip():
        raise OperationsError("approved_by is required for an emergency liquidation approval")
    return EmergencyLiquidationApproval(
        approved=True, approved_by=approved_by, reason=reason, approved_at=datetime.now(timezone.utc),
    )
