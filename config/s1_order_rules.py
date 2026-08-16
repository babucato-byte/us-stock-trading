"""US order conditions: what is verified, and what is still unknown.

Every value here is either backed by an official source or explicitly
UNKNOWN. Nothing is inferred from a plausible-sounding default, because
the two things this file governs -- whether an order is large enough to
be accepted, and whether its price is on a legal tick -- both fail as
broker rejections that this codebase would otherwise record as strategy
outcomes.

Sources consulted (PHASE 4E)
----------------------------
1. KIS Developers API portal. Its per-endpoint pages are a JavaScript
   shell; no field or constraint table could be retrieved.
2. koreainvestment/open-trading-api (official). The overseas order
   example documents the full parameter set -- ORD_QTY, OVRS_ORD_UNPR,
   ORD_DVSN and the per-exchange TR ids -- and states NO minimum order
   quantity, NO minimum order amount and NO price tick rule. ORD_QTY is
   annotated only as "must meet exchange-specific minimum and unit
   requirements", which defers to rules that source does not carry.
3. A read-only probe of the live account.

Minimum order amount: UNKNOWN, deliberately
-------------------------------------------
The official order API documents no minimum ORDER AMOUNT, and ORD_QTY is
an integer share count. That is suggestive of "one whole share is the
minimum" -- and it is not verification. The same source explicitly defers
to exchange-specific minimums it does not publish, so "no minimum is
documented" and "no minimum exists" are different statements and only the
first is established.

`live_readiness/sizing.py`'s `DEFAULT_MIN_ORDER_AMOUNT_USD = 1.0`
therefore remains what its own comment says it is: "a conservative
placeholder floor, not a verified broker limit". It must not be used as
grounds for enabling live trading, and `s1_live/readiness.py` keeps
`minimum_order_amount` out of READY until a real source supplies one.

Price tick size: NOT ENFORCED anywhere, and that is a finding
-------------------------------------------------------------
`domain/order_intent.py` validates only that `limit_price` is a positive
finite number. No module under `brokers/`, `domain/`, `execution/` or
`market_data/` normalises a price to a tick. So a price on an illegal
increment is caught only by KIS rejecting the order.

That may well be acceptable -- the broker is the authority on its own
increments, and a local table would be a second opinion that can drift
from it. What is NOT acceptable is not knowing which it is, so the
posture is named here rather than left implicit, and no rounding rule is
invented. US equities are commonly $0.01 above $1.00 and $0.0001 below,
but that is general market knowledge and not an official KIS statement,
so it is written here as a comment and NOT as a constant anything reads.
"""

UNKNOWN = "UNKNOWN"
VERIFIED = "VERIFIED"

# --- minimum order -------------------------------------------------------

#: Candidate rule, NOT in force. Recorded so that if a source later
#: verifies it, the change is a status flip rather than a redesign.
RULE_WHOLE_SHARE_ONLY = "WHOLE_SHARE_ONLY"

#: The operative state. UNKNOWN until an official source supplies a
#: minimum order amount, or explicitly states there is none.
MINIMUM_ORDER_RULE = UNKNOWN

MINIMUM_ORDER_EVIDENCE = (
    "koreainvestment/open-trading-api overseas order example documents no "
    "minimum order quantity or amount for US stocks; ORD_QTY defers to "
    "unpublished exchange-specific minimums (checked 2026-08-17)"
)


def minimum_order_verified() -> bool:
    """False until a real source establishes the rule.

    `s1_live/readiness.py` calls this, so the STAGE 1 gate cannot be
    satisfied by the placeholder.
    """
    return MINIMUM_ORDER_RULE != UNKNOWN


# --- price tick ----------------------------------------------------------

#: Who enforces the price increment. Named so the reliance is explicit.
TICK_POLICY_BROKER_ENFORCED = "BROKER_ENFORCED"
TICK_POLICY_LOCAL_NORMALISED = "LOCAL_NORMALISED"

TICK_SIZE_POLICY = TICK_POLICY_BROKER_ENFORCED

#: Whether that reliance has been exercised against a real rejection.
#: It has not: no order has ever been placed.
TICK_POLICY_VERIFIED = UNKNOWN

TICK_EVIDENCE = (
    "no tick/price-unit normalisation exists in brokers/, domain/, execution/ "
    "or market_data/; domain/order_intent.py validates only that limit_price "
    "is a positive finite number. The official order API documents no tick "
    "rule. Reliance on broker rejection is therefore UNVERIFIED (2026-08-17)"
)
