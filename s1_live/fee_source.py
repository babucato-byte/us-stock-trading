"""Where broker-reported fees will come from, once there are any.

What the PHASE 4E probe established
-----------------------------------
Two endpoints carry account-level fee totals, and their FIELD NAMES are
now confirmed against the live account:

    CTOS4001R  inquire-period-trans   output2
        dmst_fee_smtl      domestic fee subtotal
        ovrs_fee_smtl      overseas fee subtotal
        frcr_buy_amt_smtl  foreign-currency buy amount subtotal
        frcr_sll_amt_smtl  foreign-currency sell amount subtotal

    TTTS3039R  inquire-period-profit  output2
        smtl_fee1                fee subtotal
        excc_dfrm_amt            settlement/deferred amount
        ovrs_rlzt_pfls_tot_amt   overseas realised P&L total
        exrt                     exchange rate
        stck_buy_amt_smtl        stock buy amount subtotal
        stck_sll_amt_smtl        stock sell amount subtotal

What it did NOT establish, and why
----------------------------------
Every one of those fields came back ZERO, and both `output1` blocks --
the per-transaction detail, which is where a single trade's own fee would
live -- came back EMPTY. That is not a defect: the account has no trading
history at all (`inquire-ccnl` returned zero fills across 365 days, and
there are no positions).

So three things remain unknown and are recorded as such:

    * the CURRENCY of each field. `exrt` sitting alongside them suggests
      at least one is won-denominated, and PHASE 4C already caught
      `output3.tot_asst_amt` being a KRW aggregate that looked like USD.
      Guessing here would repeat exactly that mistake.
    * the SEMANTICS. Which of `dmst_fee_smtl` and `ovrs_fee_smtl` carries
      the US commission, and whether either includes the SEC/TAF
      regulatory fees, cannot be told from a field that reads 0.
    * whether a PER-TRADE fee is available at all, or only these
      subtotals. `output1` being empty leaves that open.

Consequently this module reads and records; it does not compute. Nothing
here produces a fee number for `s1_live/trade_store.py`, so `fees_status`
stays UNKNOWN and `net_pnl` stays NULL -- which is the correct state
until a real trade settles and these fields carry something to interpret.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

UNKNOWN = "UNKNOWN"
FIELDS_CONFIRMED = "FIELD_NAMES_CONFIRMED"
SEMANTICS_UNVERIFIED = "SEMANTICS_UNVERIFIED"

PERIOD_TRANS = {
    "tr_id": "CTOS4001R",
    "path": "/uapi/overseas-stock/v1/trading/inquire-period-trans",
    "summary_block": "output2",
    "detail_block": "output1",
    "fee_fields": ("dmst_fee_smtl", "ovrs_fee_smtl"),
    "amount_fields": ("frcr_buy_amt_smtl", "frcr_sll_amt_smtl"),
}

PERIOD_PROFIT = {
    "tr_id": "TTTS3039R",
    "path": "/uapi/overseas-stock/v1/trading/inquire-period-profit",
    "summary_block": "output2",
    "detail_block": "output1",
    "fee_fields": ("smtl_fee1",),
    "amount_fields": ("stck_buy_amt_smtl", "stck_sll_amt_smtl",
                      "ovrs_rlzt_pfls_tot_amt", "excc_dfrm_amt"),
    "fx_field": "exrt",
}

SOURCES = (PERIOD_TRANS, PERIOD_PROFIT)

#: Every fee field name confirmed to EXIST on the live account.
CONFIRMED_FEE_FIELDS = tuple(
    name for source in SOURCES for name in source["fee_fields"])


@dataclass
class FeeObservation:
    """One reading of a fee block. Deliberately not a fee AMOUNT."""

    tr_id: str
    block: str
    present_fields: Dict[str, Any] = field(default_factory=dict)
    missing_fields: List[str] = field(default_factory=list)
    detail_rows: int = 0
    status: str = UNKNOWN
    detail: str = ""

    @property
    def usable_for_accounting(self) -> bool:
        """Always False for now, and the reason is recorded.

        A True here would mean this module had established which field is
        the US commission and in which currency. It has not, and a fee
        figure whose currency is unknown is worse than no figure: it would
        be subtracted from a USD gross P&L to produce something labelled
        net.
        """
        return False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tr_id": self.tr_id, "block": self.block,
            "present_fields": sorted(self.present_fields),
            "missing_fields": self.missing_fields,
            "detail_rows": self.detail_rows,
            "status": self.status, "detail": self.detail,
            "usable_for_accounting": self.usable_for_accounting,
        }


def _number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if result == result and abs(result) != float("inf") else None


def observe(body, source) -> FeeObservation:
    """Record which fee fields a response actually carried.

    Never raises and never returns a fee: a malformed or empty response
    yields an observation whose status says so.
    """
    tr_id, block = source["tr_id"], source["summary_block"]
    if not isinstance(body, dict):
        return FeeObservation(tr_id, block, status=UNKNOWN,
                              detail="response body is not an object")
    if str(body.get("rt_cd")) != "0":
        return FeeObservation(tr_id, block, status=UNKNOWN,
                              detail=f"endpoint refused (rt_cd={body.get('rt_cd')!r})")

    rows = body.get(block)
    if isinstance(rows, list):
        rows = rows[0] if rows else None
    if not isinstance(rows, dict):
        return FeeObservation(tr_id, block, status=UNKNOWN,
                              detail=f"{block} carried no usable row")

    present, missing = {}, []
    for name in source["fee_fields"]:
        if name in rows:
            present[name] = _number(rows.get(name))
        else:
            missing.append(name)

    detail_rows = len(body.get(source["detail_block"]) or [])
    if missing:
        status, detail = UNKNOWN, f"expected fee fields absent: {missing}"
    elif detail_rows == 0:
        status = FIELDS_CONFIRMED
        detail = ("fee fields present but the per-transaction block is empty; "
                  "no trade has settled, so their currency and meaning stay "
                  "unverified")
    else:
        status = SEMANTICS_UNVERIFIED
        detail = ("per-transaction rows are present; their currency and "
                  "meaning still need one settled trade to interpret")
    return FeeObservation(tr_id, block, present_fields=present,
                          missing_fields=missing, detail_rows=detail_rows,
                          status=status, detail=detail)


def accounting_status() -> Dict[str, Any]:
    """What `s1_live/trade_store.py` should record today: UNKNOWN.

    Kept as a function rather than a constant so the answer comes from
    one place when a source is eventually established.
    """
    return {
        "fees_status": "UNKNOWN",
        "net_pnl": None,
        "reason": ("broker fee fields are confirmed to exist "
                   f"({', '.join(CONFIRMED_FEE_FIELDS)}) but every one read "
                   "zero against an account with no trading history, so their "
                   "currency and semantics are unverified"),
        "confirmed_fields": list(CONFIRMED_FEE_FIELDS),
        "usable": False,
    }
