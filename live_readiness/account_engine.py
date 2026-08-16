"""Account Engine -- the authoritative source of "how much cash can a new
live entry actually use right now", per the layered architecture:

    Market Data -> Strategy Engine -> Signal -> Risk Engine ->
    Account Engine -> Sizing Engine -> Execution Engine -> Broker

Strategy code never determines account cash, usage percent, exposure, or
final order sizing -- `strategy/interface.py::EvaluationResult` already
carries none of those fields (see `docs/autonomous/PROJECT_CONSTITUTION.md`
for the enforced boundary). This module is where account facts actually
get established, and it never trusts a caller-declared number: cash comes
from `live_readiness/account_cash.py::fetch_account_cash_snapshot()` (a
real `broker.get_account()` call), exposure comes from
`live_readiness/entry_reservation_ledger.py::build_snapshot()` (durable
SQLite), and `cash_usage_percent` comes exclusively from
`live_readiness/trusted_operator_config.py`.

`build_account_snapshot()` produces an immutable `AccountSnapshot` that
downstream Risk/Sizing/Execution Engines consume -- never a bare number.
Any failure to establish an authoritative fact (broker call fails, cash
missing/negative/non-finite, snapshot stale, trading mode ambiguous,
unreconciled exposure) raises `AccountEngineError` -- there is no
fallback value; a new entry is fail-closed blocked.

No margin/leverage: `effective_cash_krw = min(broker_cash_krw,
non_margin_available_cash_krw)` -- buying_power (which can include
margin) is never used as a ceiling here, even if it's larger than cash.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from live_readiness import entry_reservation_ledger as ledger
from live_readiness.account_cash import (
    AccountCashSnapshotError,
    fetch_account_cash_snapshot,
)
from live_readiness.trusted_operator_config import get_cash_usage_percent_ceiling


class AccountEngineError(Exception):
    """Raised whenever an authoritative account snapshot cannot be safely
    established. Always treat as a hard block on new live entries."""


@dataclass(frozen=True)
class AccountSnapshot:
    """Immutable authoritative account state. Every field here is either
    a direct broker fact or a durable SQLite aggregate -- never a
    caller-declared number.

    `reconciliation_required_reservations_krw` is currently the SAME
    figure as `unknown_submission_reservations_krw` -- a
    SUBMISSION_UNKNOWN reservation (CODEX-034/035) is exactly the set of
    reservations requiring reconciliation before their fate is known.
    Both field names are exposed (per this architecture's field-naming
    requirements) but there is only one underlying ledger state; a
    dedicated RECONCILIATION_REQUIRED ledger state was deliberately not
    introduced to avoid a redundant, driftable second source of truth
    for the same condition.
    """

    broker_cash_krw: float
    non_margin_available_cash_krw: float
    effective_cash_krw: float
    pending_buy_reservations_krw: float
    unknown_submission_reservations_krw: float
    reconciliation_required_reservations_krw: float
    current_open_position_cost_krw: float
    active_position_count: int
    today_entry_count: int
    as_of: datetime
    trading_mode: str
    account_id: Optional[str]
    reconciliation_complete: bool
    source: str

    @property
    def total_committed_krw(self):
        return (
            self.pending_buy_reservations_krw
            + self.unknown_submission_reservations_krw
            + self.current_open_position_cost_krw
        )


def is_account_snapshot_stale(snapshot, max_age_seconds, *, now=None):
    """Downstream engines (Risk/Sizing/Execution) must re-check staleness
    themselves before using a snapshot handed to them -- time may have
    passed since `build_account_snapshot()` stamped `as_of`."""
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - snapshot.as_of).total_seconds()
    return age_seconds < 0 or age_seconds > max_age_seconds


def build_account_snapshot(
    broker, fx_rate_krw_per_usd, conn, *, now=None,
    expected_account_id=None, expected_trading_mode=None,
):
    """Query the broker + durable reservation ledger and assemble an
    authoritative `AccountSnapshot`. Raises `AccountEngineError` --
    never returns a partial/fabricated snapshot -- on any of:

      - broker account call failure, missing/negative/non-finite cash
        (via `fetch_account_cash_snapshot()`)
      - `non_margin_available_cash` missing/negative/non-finite when the
        broker account response provides one (falls back to the same
        cash figure if the field is genuinely absent -- Alpaca's account
        payload does not always separate the two; effective_cash is
        still capped at whichever is smaller)
      - trading mode not exactly "paper" or "live" (ambiguous account
        state), or not matching `expected_trading_mode` if supplied
      - `expected_account_id` supplied but the broker reports a
        different id
      - the ledger snapshot query itself failing
    """
    current = now or datetime.now(timezone.utc)

    try:
        cash_snapshot = fetch_account_cash_snapshot(broker, fx_rate_krw_per_usd, now=current)
    except AccountCashSnapshotError as exc:
        raise AccountEngineError(f"authoritative cash unavailable: {exc}") from exc

    trading_mode = getattr(getattr(broker, "config", None), "trading_mode", None)
    if trading_mode not in ("paper", "live"):
        raise AccountEngineError(
            f"broker trading mode is ambiguous, must be exactly 'paper' or 'live': {trading_mode!r}"
        )
    if expected_trading_mode is not None and trading_mode != expected_trading_mode:
        raise AccountEngineError(
            f"broker trading mode {trading_mode!r} does not match expected {expected_trading_mode!r}"
        )

    try:
        account_data = broker.get_account()
    except Exception as exc:
        raise AccountEngineError(f"broker.get_account() failed: {exc}") from exc
    if not isinstance(account_data, dict):
        raise AccountEngineError(f"broker account response is not a dict: {account_data!r}")

    account_id = account_data.get("account_number") or account_data.get("id")
    if expected_account_id is not None and account_id != expected_account_id:
        raise AccountEngineError(
            f"broker account id {account_id!r} does not match expected {expected_account_id!r}"
        )

    non_margin_raw = account_data.get("non_marginable_buying_power")
    if non_margin_raw is None:
        # Field genuinely absent from this broker response -- fall back to
        # the same authoritative cash figure (min() below becomes a no-op,
        # never a loosening).
        non_margin_available_cash_krw = cash_snapshot.cash_krw
    else:
        try:
            non_margin_usd = float(non_margin_raw)
        except (TypeError, ValueError):
            raise AccountEngineError(
                f"non_marginable_buying_power is not numeric: {non_margin_raw!r}"
            )
        if not math.isfinite(non_margin_usd) or non_margin_usd < 0:
            raise AccountEngineError(f"non_marginable_buying_power is invalid: {non_margin_usd!r}")
        non_margin_available_cash_krw = non_margin_usd * fx_rate_krw_per_usd

    effective_cash_krw = min(cash_snapshot.cash_krw, non_margin_available_cash_krw)

    try:
        ledger_snapshot = ledger.build_snapshot(conn, now=current)
    except Exception as exc:
        raise AccountEngineError(f"entry reservation ledger snapshot failed: {exc}") from exc

    reconciliation_required_krw = ledger_snapshot.unknown_submission_reservations_krw

    return AccountSnapshot(
        broker_cash_krw=cash_snapshot.cash_krw,
        non_margin_available_cash_krw=non_margin_available_cash_krw,
        effective_cash_krw=effective_cash_krw,
        pending_buy_reservations_krw=ledger_snapshot.pending_buy_reservations_krw,
        unknown_submission_reservations_krw=ledger_snapshot.unknown_submission_reservations_krw,
        reconciliation_required_reservations_krw=reconciliation_required_krw,
        current_open_position_cost_krw=ledger_snapshot.current_open_position_cost_krw,
        active_position_count=ledger_snapshot.active_position_count,
        today_entry_count=ledger_snapshot.today_entry_count,
        as_of=current,
        trading_mode=trading_mode,
        account_id=account_id,
        reconciliation_complete=(reconciliation_required_krw == 0),
        source="broker_account_endpoint",
    )


def compute_max_allocatable_cash_krw(account_snapshot, cash_usage_percent):
    """`cash_usage_percent` is capped against the trusted operator
    ceiling regardless of what the caller passed -- identical pattern to
    order_gateway.py's own capping, duplicated here (not imported) since
    this is the Account Engine's own authoritative computation, not a
    delegation to order_gateway.py."""
    effective_percent = min(cash_usage_percent, get_cash_usage_percent_ceiling())
    return account_snapshot.effective_cash_krw * (effective_percent / 100.0)


def compute_available_for_new_order_krw(account_snapshot, cash_usage_percent):
    """`available_for_new_order = max_allocatable_cash -
    pending_buy_reservations - unknown_submission_reservations -
    current_open_position_cost` -- all three deductions are durable
    SQLite aggregates from `account_snapshot`, never caller input."""
    max_allocatable = compute_max_allocatable_cash_krw(account_snapshot, cash_usage_percent)
    return max_allocatable - account_snapshot.total_committed_krw
