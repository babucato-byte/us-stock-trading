"""What the account is worth, in one currency, or an explicit refusal.

Equity is not cash
------------------
    equity = cash + market value of open positions

The daily-loss and drawdown limits are both measured on EQUITY. Using
cash alone would show a "loss" every time money is deployed into a
position, and using position value alone would ignore the cash the
strategy is sitting on. Neither is the quantity `risk_config`'s -2% and
-10% describe.

Currency, decided rather than assumed
-------------------------------------
USD, and only USD. `brokers/kis_broker.py` queries the balance with
`TR_CRCY_CD="USD"` and reads `ovrs_ord_psbl_amt` (the overseas orderable
amount, which includes reusable sell proceeds) from the per-symbol
endpoint for SIZING. Risk equity here deliberately stays on the cash
deposit instead: what may be ordered and what the account actually holds
are different questions, and a risk denominator inflated by unsettled
proceeds would understate every risk figure; `AccountSnapshot.krw_cash`
is never populated by that path at all. So nothing here converts, and
`assert_single_currency()` refuses a mixed input rather than applying a
rate. No FX rate is read, stored or assumed, because none is needed --
and a fixed one would be a fabricated number in the denominator of every
risk figure.

Where the cash half comes from (PHASE 4C)
-----------------------------------------
`inquire-balance` (TTTS3012R) does not carry cash -- that is recorded as
`balance_cash_fields_absent` in the broker's verification matrix, from a
real probe. `inquire-present-balance` (CTRP6504R) does, in an `output2`
row tagged with its own `crcy_cd`, and `KISBroker.get_account_cash_usd()`
reads the USD row's `frcr_dncl_amt_2`. A live probe cross-validated it
against `get_orderable_usd()` -- already independently confirmed -- and
the two matched EXACTLY.

Equity is CALCULATED, not reported
-----------------------------------
KIS publishes no USD account equity. Its only "total assets" field,
`output3.tot_asst_amt`, came back at 5,844x the USD orderable amount and
IDENTICAL whether the request asked for the KRW or the foreign-currency
division: a won-denominated total spanning every currency the account
holds. It is the one field whose name suggests exactly what this module
needs, and it is the wrong number -- using it would put KRW cash and an
implicit FX rate into the denominator of every risk figure.

So the sum is done here, from a verified USD cash figure and USD
position value, and `source` records which half came from where.

The position half is exposed separately as `position_value_usd` and is
never returned AS equity: equity minus its cash term is a different
number with the same name, and substituting one for the other is exactly
how ORACLE-CASH-01 turned "this endpoint reports nothing" into a
confident zero.

Still unverified: the position-value formula has not been checked against
a broker-reported valuation, because the account held no positions when
the probe ran.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"

USD = "USD"

#: Why equity could not be established. Each is a distinct operator
#: action, which is why they are not one generic code.
REASON_NO_ACCOUNT_CASH = "ACCOUNT_CASH_UNAVAILABLE"
REASON_NO_POSITION_VALUE = "POSITION_VALUE_UNAVAILABLE"
REASON_CURRENCY_MIXED = "EQUITY_CURRENCY_MIXED"
REASON_READ_FAILED = "EQUITY_READ_FAILED"
REASON_MALFORMED = "EQUITY_RESPONSE_MALFORMED"

#: The live-probe record that explains the current UNAVAILABLE state.
CASH_EVIDENCE = ("brokers/kis_broker.py VERIFICATION_MATRIX: "
                 "balance_cash_fields_absent (LIVE_RESPONSE_CONFIRMED, "
                 "Oracle 2026-08-06)")


class EquityUnavailable(Exception):
    """Equity could not be established. A hard block on new entries."""

    def __init__(self, message, *, reason_code=REASON_NO_ACCOUNT_CASH):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class EquitySnapshot:
    status: str
    equity_usd: Optional[float]
    currency: str
    as_of: datetime
    source: str
    cash_usd: Optional[float] = None
    position_value_usd: Optional[float] = None
    position_count: int = 0
    reason_code: Optional[str] = None
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.status == AVAILABLE and self.equity_usd is not None

    def require(self) -> float:
        if not self.available:
            raise EquityUnavailable(
                f"account equity could not be established: {self.detail}",
                reason_code=self.reason_code or REASON_NO_ACCOUNT_CASH)
        return float(self.equity_usd)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "equity_usd": self.equity_usd,
            "currency": self.currency, "as_of": self.as_of.isoformat(),
            "source": self.source, "cash_usd": self.cash_usd,
            "position_value_usd": self.position_value_usd,
            "position_count": self.position_count,
            "reason_code": self.reason_code, "detail": self.detail,
        }


def _finite(value) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _usable_cash(value) -> Optional[float]:
    """A cash figure must also be NON-NEGATIVE.

    `KISBroker.get_account_cash_usd()` already refuses a negative, but
    this is the layer that turns cash into equity and it does not get to
    assume its caller checked. A negative balance is not a small balance:
    this codebase forbids negative cash outright, so the honest response
    is "unknown", not an equity figure below zero that every downstream
    percentage would then divide by.
    """
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def assert_single_currency(*currencies) -> str:
    """Every supplied currency must be USD. Raises otherwise.

    Deliberately strict rather than converting: a risk figure computed
    across two currencies with an assumed rate is a fabricated number,
    and this account's overseas path is USD-denominated throughout.
    """
    seen = {str(item).upper() for item in currencies if item}
    if not seen:
        return USD
    if seen != {USD}:
        raise EquityUnavailable(
            f"equity would mix currencies {sorted(seen)}; this pilot accounts in "
            f"{USD} only and applies no FX rate",
            reason_code=REASON_CURRENCY_MIXED)
    return USD


def position_value(positions) -> Optional[float]:
    """Market value of open positions, in USD.

    `qty * average_fill_price` is the cost basis and `unrealized_pnl` is
    what the market has done to it since, so their sum is the current
    value. Both come from the same balance row (`pchs_avg_pric`,
    `evlu_pfls_amt`), so they are consistent with each other by
    construction.

    Returns None -- never 0.0 -- if any row is unusable. A position whose
    value cannot be read makes the TOTAL unknown; treating it as zero
    would understate equity and therefore overstate the drawdown.
    """
    if positions is None:
        return None
    total = 0.0
    for position in positions:
        quantity = _finite(getattr(position, "quantity", None))
        price = _finite(getattr(position, "average_fill_price", None))
        unrealized = _finite(getattr(position, "unrealized_pnl", None))
        if quantity is None or price is None or unrealized is None:
            logger.warning("position row unusable for equity: %r", position)
            return None
        total += quantity * price + unrealized
    return round(total, 6)


def from_account(account_snapshot, positions, *, now=None,
                 source="kis_balance", cash_override=None) -> EquitySnapshot:
    """Build the snapshot, or refuse with the reason. Never raises.

    `cash_override` is the account-level USD cash from
    `get_account_cash_usd()`. It is preferred over the balance
    snapshot's own `usd_cash` because that field is absent on this
    broker -- see `brokers/kis_broker.py`'s matrix. It is an OVERRIDE and
    not a fallback: when it is present it is the figure used, because it
    is the one that was verified against a second endpoint.
    """
    stamp = now or datetime.now(timezone.utc)
    count = len(list(positions)) if positions is not None else 0

    try:
        assert_single_currency(
            USD if getattr(account_snapshot, "usd_cash", None) is not None else None,
            "KRW" if getattr(account_snapshot, "krw_cash", None) is not None else None)
    except EquityUnavailable as exc:
        return EquitySnapshot(UNAVAILABLE, None, USD, stamp, source,
                              position_count=count, reason_code=exc.reason_code,
                              detail=str(exc))

    value = position_value(positions)
    cash = _usable_cash(cash_override)
    if cash is None:
        cash = _usable_cash(getattr(account_snapshot, "usd_cash", None))

    if value is None:
        return EquitySnapshot(
            UNAVAILABLE, None, USD, stamp, source, cash_usd=cash, position_count=count,
            reason_code=REASON_NO_POSITION_VALUE,
            detail="at least one position row carried no usable quantity, price or "
                   "unrealized P&L, so the position total is unknown")

    if cash is None:
        # The half that this broker does not report. Position value is
        # still surfaced so the gap is visible, but it is NOT equity.
        return EquitySnapshot(
            UNAVAILABLE, None, USD, stamp, source, cash_usd=None,
            position_value_usd=value, position_count=count,
            reason_code=REASON_NO_ACCOUNT_CASH,
            detail=f"the account response carried no USD cash figure "
                   f"({getattr(account_snapshot, 'cash_source', '') or 'unreported'}); "
                   f"position value is ${value:,.2f} but equity also needs cash. "
                   f"Evidence: {CASH_EVIDENCE}")

    return EquitySnapshot(
        AVAILABLE, round(cash + value, 6), USD, stamp, source,
        cash_usd=cash, position_value_usd=value, position_count=count)


def from_amount(equity_usd, *, source="caller", now=None,
                currency=USD) -> EquitySnapshot:
    """Wrap a figure the caller already holds -- tests and the dry run."""
    stamp = now or datetime.now(timezone.utc)
    try:
        assert_single_currency(currency)
    except EquityUnavailable as exc:
        return EquitySnapshot(UNAVAILABLE, None, str(currency).upper(), stamp, source,
                              reason_code=exc.reason_code, detail=str(exc))
    value = _finite(equity_usd)
    if value is None or value < 0:
        return EquitySnapshot(UNAVAILABLE, None, USD, stamp, source,
                              reason_code=REASON_MALFORMED,
                              detail=f"{equity_usd!r} is not a usable equity amount")
    return EquitySnapshot(AVAILABLE, value, USD, stamp, source)


def read(broker, *, now=None) -> EquitySnapshot:
    """One equity read through the broker. Any failure is UNAVAILABLE.

    PHASE 4C: cash comes from `get_account_cash_usd()` --
    `inquire-present-balance`'s currency-tagged USD row -- because
    `inquire-balance` genuinely does not carry it. When the broker
    predates that method the read still works and still refuses, which is
    what keeps this function honest on an older wrapper rather than
    silently substituting a zero.

    KIS reports no USD-denominated account equity. Its only "total
    assets" figure (`output3.tot_asst_amt`) is won-denominated and spans
    every currency the account holds -- a live probe measured it at
    5,844x the USD orderable amount and identical under both the KRW and
    foreign-currency request divisions. So equity is CALCULATED here,
    from a verified USD cash figure plus USD position value, and the
    provenance of both halves is recorded on the snapshot.
    """
    stamp = now or datetime.now(timezone.utc)
    try:
        snapshot = broker.get_account_snapshot()
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001 - a failed read is unknown, not zero
        logger.warning("equity read failed: %s", type(exc).__name__)
        return EquitySnapshot(UNAVAILABLE, None, USD, stamp, "kis_balance",
                              reason_code=REASON_READ_FAILED,
                              detail=f"the account read failed: {type(exc).__name__}")

    cash = None
    cash_source = "kis_balance"
    reader = getattr(broker, "get_account_cash_usd", None)
    if callable(reader):
        try:
            cash = _usable_cash(reader())
            cash_source = "kis_present_balance:output2[USD].frcr_dncl_amt_2"
        except Exception as exc:  # noqa: BLE001
            logger.warning("account cash read failed: %s", type(exc).__name__)
            return EquitySnapshot(
                UNAVAILABLE, None, USD, stamp, "kis_present_balance",
                position_value_usd=position_value(positions),
                position_count=len(list(positions)) if positions is not None else 0,
                reason_code=REASON_READ_FAILED,
                detail=f"the account cash read failed: {type(exc).__name__}")

    return from_account(snapshot, positions, now=stamp, source=cash_source,
                        cash_override=cash)
