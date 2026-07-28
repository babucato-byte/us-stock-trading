"""Thin facade over the existing notification_health.py (spec §5: reuse
existing status-check machinery) plus a KIS-specific data-freshness
check the Alpaca-only original never needed: whether Alpaca market data
is currently stale enough that new-symbol discovery must stop while
existing-position monitoring continues on KIS's own data (spec §15).
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import notification_health


def get_notification_health_status():
    """Delegates entirely to the existing, already-tested
    notification_health.get_status()."""
    return notification_health.get_status()


@dataclass(frozen=True)
class DataHealthDecision:
    new_entry_discovery_allowed: bool
    existing_position_monitoring_allowed: bool
    reason: str


def evaluate_data_health(
    *, alpaca_data_as_of: Optional[datetime], alpaca_max_staleness_seconds: float,
    kis_read_ok: bool, now: Optional[datetime] = None,
) -> DataHealthDecision:
    """Spec §15's failure-isolation policy, made explicit and testable:
    Alpaca staleness/outage stops NEW entry discovery but never stops
    monitoring already-open positions (those use KIS's own price/balance
    reads, tracked separately via `kis_read_ok`). If KIS itself is also
    unreadable, everything stops -- there is nothing left to safely act
    on."""
    current = now or datetime.now(timezone.utc)
    if not kis_read_ok:
        return DataHealthDecision(
            new_entry_discovery_allowed=False, existing_position_monitoring_allowed=False,
            reason="KIS quote/account read is unavailable -- full stop, nothing is safely actionable",
        )
    alpaca_fresh = (
        alpaca_data_as_of is not None
        and 0 <= (current - alpaca_data_as_of).total_seconds() <= alpaca_max_staleness_seconds
    )
    if not alpaca_fresh:
        return DataHealthDecision(
            new_entry_discovery_allowed=False, existing_position_monitoring_allowed=True,
            reason="Alpaca market data is stale or unavailable -- new entry discovery stopped, "
                   "existing positions still monitored via KIS",
        )
    return DataHealthDecision(
        new_entry_discovery_allowed=True, existing_position_monitoring_allowed=True,
        reason="Alpaca data fresh, KIS reads OK",
    )
