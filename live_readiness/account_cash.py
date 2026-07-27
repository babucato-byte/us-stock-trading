"""CODEX-036: authoritative account cash snapshot.

`live_readiness/order_gateway.py`'s `LiveEntryContext.available_cash_krw`/
`cash_usage_percent` were, before this module existed, whatever a caller
happened to pass in -- nothing at the final order boundary ever compared
that figure against the broker's own account balance, or against a
trusted, caller-untightenable operator setting. A caller (buggy, stale, or
malicious) could declare `available_cash_krw=3_000_000` against a real
30,000 KRW account and have a ~2,997,000 KRW order approved with zero
broker account queries -- Codex's CODEX-036 direct reproduction.

`fetch_account_cash_snapshot()` is the ONLY way to produce an
`AccountCashSnapshot` -- it always goes through `broker.get_account()`,
the same real Alpaca account-balance endpoint used everywhere else in
this codebase (`performance_analytics.py`'s `fetch_alpaca_snapshot()`,
`account_risk.py`). `live_readiness/order_gateway.py::
validate_and_size_live_entry()` accepts an optional `account_cash_snapshot`
and, if supplied, uses its `cash_krw` as a ceiling on (never a
replacement floor for) whatever the caller's `LiveEntryContext.
available_cash_krw` was -- `min(caller value, authoritative snapshot)` --
so a caller can only ever ask for LESS than the real account actually
has, never more.

Deliberately NOT wired to fetch automatically inside
`AlpacaBroker.submit_order()`: this codebase's pre-live safety gate
(`broker/broker_config.py::BrokerConfig.validate_order_allowed()`)
currently blocks ALL live-mode broker calls, regardless of dry-run
status -- if `submit_order()` tried to fetch a snapshot on every call,
even sizing-only/dry-run validation would start failing with "Real live
trading is disabled" instead of running its intended checks. Fetching a
snapshot is therefore the responsibility of whatever future production
caller constructs the `LiveEntryContext` in the first place, at a point
in its own flow where live network calls are actually permitted (a
separate, explicit wiring decision that hasn't been made yet -- see
DECISION_LOG.md's CODEX-036 section). Passing a snapshot through today
already fully closes the caller-assertion gap for any caller that does
supply one.

`TRUSTED_CASH_USAGE_PERCENT_CEILING` is the equivalent fix for
`cash_usage_percent`: unlike `available_cash_krw` (a market/account fact
this codebase cannot independently know without asking the broker),
`cash_usage_percent` is a pure OPERATOR policy setting -- there is no
external system to query for it.

**Account/Risk/Sizing/Execution Engine layering (this codebase's
architecture doc terminology)**: the actual value now lives in
`live_readiness/trusted_operator_config.py` -- the SOLE source for every
operator policy constant (`cash_usage_percent` ceiling,
`MAX_CONCURRENT_LIVE_POSITIONS`, `MAX_DAILY_LIVE_ENTRIES`), read only via
its validated `get_*()` functions. `TRUSTED_CASH_USAGE_PERCENT_CEILING`
is re-exported here for backward compatibility with existing imports
(`live_readiness/order_gateway.py`, its test suite) -- new code should
prefer `trusted_operator_config.get_cash_usage_percent_ceiling()`
directly.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from live_readiness.trusted_operator_config import get_cash_usage_percent_ceiling

TRUSTED_CASH_USAGE_PERCENT_CEILING = get_cash_usage_percent_ceiling()


class AccountCashSnapshotError(Exception):
    """Raised when an authoritative account cash snapshot cannot be
    safely established. Callers must treat this as a hard block on live
    entries -- there is no meaningful fallback value."""


@dataclass(frozen=True)
class AccountCashSnapshot:
    """The ONLY legitimate representation of "how much cash the live
    account actually has right now" -- always constructed by
    fetch_account_cash_snapshot() from a real broker.get_account() call,
    never hand-built from an arbitrary number. `source` is recorded for
    audit purposes and is always "broker_account_endpoint" for a
    snapshot this module produced."""

    cash_krw: float
    as_of: datetime
    source: str


def fetch_account_cash_snapshot(broker, fx_rate_krw_per_usd, *, now=None) -> AccountCashSnapshot:
    """Query the broker's real account endpoint and convert its USD cash
    figure to KRW using the caller-supplied, already-validated FX rate
    (order_gateway.py validates FX rate freshness/finiteness separately --
    this function only requires it be usable for the conversion).

    Raises AccountCashSnapshotError -- never returns a fabricated or
    default value -- if the broker call fails, the response is missing
    or has a malformed `cash` field, or the resulting figure is
    non-finite or negative. Fail-closed: an entry that cannot establish a
    real cash figure must never proceed as if it had one.
    """
    if fx_rate_krw_per_usd is None or isinstance(fx_rate_krw_per_usd, bool) \
            or not isinstance(fx_rate_krw_per_usd, (int, float)) \
            or not math.isfinite(fx_rate_krw_per_usd) or fx_rate_krw_per_usd <= 0:
        raise AccountCashSnapshotError(f"invalid fx_rate_krw_per_usd {fx_rate_krw_per_usd!r}")

    try:
        account = broker.get_account()
    except AccountCashSnapshotError:
        raise
    except Exception as exc:
        # Only wrap failures that are actually about "couldn't obtain a
        # cash figure" (network/transport errors) into
        # AccountCashSnapshotError. A RuntimeError from the broker's own
        # pre-network safety gates (kill switch, credential mismatch, the
        # pre-live "real trading disabled" hard block, etc.) is a
        # deliberate, differently-typed hard stop elsewhere in this
        # codebase -- swallowing it here and re-raising as a generic
        # AccountCashSnapshotError would mask that distinction from
        # callers (e.g. AlpacaBroker.submit_order()'s own exception
        # handling) that rely on it, so it is left to propagate as-is.
        if isinstance(exc, RuntimeError):
            raise
        raise AccountCashSnapshotError(f"broker.get_account() failed: {exc}") from exc

    if not isinstance(account, dict) or "cash" not in account:
        raise AccountCashSnapshotError(
            f"broker account response missing a usable 'cash' field: {account!r}"
        )

    try:
        cash_usd = float(account["cash"])
    except (TypeError, ValueError):
        raise AccountCashSnapshotError(f"broker account 'cash' field is not numeric: {account['cash']!r}")

    if not math.isfinite(cash_usd) or cash_usd < 0:
        raise AccountCashSnapshotError(f"broker account cash is invalid: {cash_usd!r}")

    return AccountCashSnapshot(
        cash_krw=cash_usd * fx_rate_krw_per_usd,
        as_of=now or datetime.now(timezone.utc),
        source="broker_account_endpoint",
    )
