"""KISBroker -- the sole live-order broker adapter in this migration.
Strategy/Risk/Sizing/Execution code never sees a KIS TR_ID, exchange
code, or wire-format field name; every public method here takes/returns
`domain/` types (Instrument, OrderIntent, ExecutionRecord, Position,
AccountSnapshot) and does the TR_ID/field mapping internally, mirroring
broker/alpaca_client.py's existing shape (config + injectable session,
never a bare `requests` call scattered through calling code).

TR_ID/endpoint/field values below were verified against the OFFICIAL
KIS Open API GitHub reference repo (github.com/koreainvestment/
open-trading-api), cloned locally to ~/kis-open-api-reference and
diffed against this module directly (2026-07-31) -- not invented, and
no longer just indirectly inferred. That comparison caught and fixed
three real bugs this module previously had:

  - The general (non-daytime) cancel TR_ID pair was wrong -- this
    module previously reused the DAYTIME-specific TTTS6038U for both
    real and paper; the correct pair (examples_llm/overseas_stock/
    order_rvsecncl/order_rvsecncl.py) is TTTT1004U (real) /
    VTTT1004U (paper).
  - `OVRS_EXCG_CD` on the order/cancel/orderable-amount endpoints uses
    a DIFFERENT code space than `EXCD` on the price/quote endpoint
    ("NASD"/"NYSE"/"AMEX", 4-letter, vs "NAS"/"NYS"/"AMS", 3-letter) --
    this module was sending the quote-API 3-letter code to the
    order-API field, which the reference repo's order.py/order_
    rvsecncl.py/inquire_psamount docstrings confirm is wrong.
  - Cancel requests must send `OVRS_ORD_UNPR="0"` (per order_rvsecncl.
    py's own docstring: "취소주문 시, '0' 입력'"), not the order's
    actual limit price -- this module was sending the real price.

The response field name for last-traded price (`output.last`) WAS
independently confirmed too (examples_llm/overseas_stock/price/
chk_price.py's own field-name comment: `'last': '현재가'`).

CODEX-052 -- two INDEPENDENT axes of verification, which this module
previously conflated. An older "to be verified" comment said the cancel
TR_ID and the price field were unconfirmed, while the paragraphs above
said they had been confirmed; both described the same values. The two
statements were about different axes, and neither said which:

  REFERENCE_VERIFIED     the value was read out of KIS's own official
                         examples / reference repository
  LIVE_RESPONSE_PENDING  no real KIS response has been observed for it
                         yet -- Oracle read-only (or a 모의투자 order)
                         must confirm it before KIS_LIVE_ORDER_ENABLED

The two are orthogonal: a value can be REFERENCE_VERIFIED and still
LIVE_RESPONSE_PENDING, which is exactly the state of the cancel TR_ID
and the price field today. `VERIFICATION_MATRIX` below is the single
machine-readable statement of that status -- prose in this module must
not contradict it, and tests assert both that the matrix matches the
constants actually used and that the runbook lists every pending item.

Every state-mutating call (submit_order/cancel_order) runs
config.validate_live_order_allowed() FIRST, before any network call --
mirrors broker/alpaca_client.py's _validate_runtime_safety() pattern.
Read-only calls (get_current_price/get_account_snapshot/get_positions/
get_open_orders/get_fills) run the separate, weaker
config.validate_read_allowed() gate, so Shadow Mode (spec §26) can read
KIS state without also being able to submit an order.
"""

import math
import uuid
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional

import requests

from brokers import kis_rate_limiter, kis_token_cache
from brokers.kis_config import KISConfig, KISConfigError
from domain.account_snapshot import (
    CASH_SOURCE_BALANCE_LACKS_FIELDS,
    AccountSnapshot,
)
from domain.cash_sizing import ORDERABLE_CASH_UNAVAILABLE
from domain.execution_event import ExecutionRecord
from domain.order_intent import OrderIntent
from domain.position import Position
from domain.exchange import (
    USExchange,
    UnsupportedExchangeError,
    exchange_for_kis_order_code,
    supported_kis_order_exchange_codes,
    to_kis_exchange_code,
    to_kis_order_exchange_code,
)
from execution.secret_redaction import redact_text, safe_repr

TOKEN_PATH = "/oauth2/tokenP"
PRICE_PATH = "/uapi/overseas-price/v1/quotations/price"
BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
PRESENT_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-present-balance"
PSAMOUNT_PATH = "/uapi/overseas-stock/v1/trading/inquire-psamount"
ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
# CODEX-052: REFERENCE_VERIFIED, LIVE_RESPONSE_PENDING. The path and the
# TTTT1004U/VTTT1004U pair come from the official reference repo's
# order_rvsecncl.py (this module previously reused the daytime-specific
# TTTS6038U, which that comparison corrected). No real KIS cancel
# response has been observed yet -- see VERIFICATION_MATRIX below and
# the Oracle runbook's read-only confirmation step.
CANCEL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
NCCS_PATH = "/uapi/overseas-stock/v1/trading/inquire-nccs"
CCNL_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"

TR_ID_BALANCE = {"live": "TTTS3012R", "paper": "VTTS3012R"}
TR_ID_PRESENT_BALANCE = {"live": "CTRP6504R", "paper": "VTRP6504R"}
#: The currency-tagged USD deposit in CTRP6504R's output2. Confirmed
#: against a live account: it matched get_orderable_usd() EXACTLY, while
#: output3's totals came back ~1,415x and ~4,429x larger (KRW, and
#: mixing currencies). See ACCOUNT_CASH_FIELD below.
ACCOUNT_CASH_FIELD = "frcr_dncl_amt_2"
ACCOUNT_CASH_CURRENCY_FIELD = "crcy_cd"
TR_ID_PSAMOUNT = {"live": "TTTS3007R", "paper": "VTTS3007R"}
# The single field the orderable amount is read from, named once so the
# parser, the verification matrix and the read-only probe cannot disagree.
ORDERABLE_AMOUNT_FIELD = "ord_psbl_frcr_amt"
TR_ID_NCCS = {"live": "TTTS3018R", "paper": "TTTS3018R"}
TR_ID_CCNL = {"live": "TTTS3035R", "paper": "VTTS3035R"}
TR_ID_PRICE = "HHDFS00000300"
# --- today's own range, for execution-time sanity ------------------------
#
# The quote endpoint above returns 11 fields and none of them is a high, a
# low or a timestamp. `price-detail` returns 41, including today's open/
# high/low, the tick size and an orderable flag. Verified against a live
# response (AMBA, NASDAQ, 2026-08-18): open 76.2150 high 77.0400 low
# 74.5100 last 75.5400 base 78.5400 e_hogau 0.0100 e_ordyn "매매 가능".
#
# Used ONLY to sanity-check an execution price. It is never fed to a
# scanner: S1's signal is computed on completed daily bars, and letting a
# forming high/low into that calculation is the exact drift the same-day
# design exists to prevent.
PRICE_DETAIL_PATH = "/uapi/overseas-price/v1/quotations/price-detail"
TR_ID_PRICE_DETAIL = "HHDFS76200200"
#: Field names read from that response, named once so the parser, the
#: matrix and any probe cannot disagree.
PRICE_DETAIL_FIELDS = {
    "last": "last", "high": "high", "low": "low", "open": "open",
    "prev_close": "base", "currency": "curr", "tick_size": "e_hogau",
    "orderable": "e_ordyn", "today_volume": "tvol",
}
# US-market order TR_IDs, verified from examples_user/overseas_stock/
# overseas_stock_functions.py's order() function.
TR_ID_ORDER_US = {
    ("live", "buy"): "TTTT1002U", ("paper", "buy"): "VTTT1002U",
    ("live", "sell"): "TTTT1006U", ("paper", "sell"): "VTTT1001U",
}
# Verified against the official koreainvestment/open-trading-api
# reference repo (examples_llm/overseas_stock/order_rvsecncl/
# order_rvsecncl.py) -- the general (non-daytime) cancel TR_ID pair is
# TTTT1004U/VTTT1004U, NOT the daytime-specific TTTS6038U this module
# previously (incorrectly) reused for both.
TR_ID_CANCEL = {"live": "TTTT1004U", "paper": "VTTT1004U"}

# -- US daytime trading (미국주간거래), the OVERNIGHT_DAYTIME session ----
#
# A SEPARATE endpoint and TR family, not a variant of the regular order.
# Verified against the official reference repo:
#   examples_llm/overseas_stock/daytime_order/daytime_order.py
#     [v1_해외주식-026]  /uapi/overseas-stock/v1/trading/daytime-order
#     buy TTTS6036U, sell TTTS6037U
#   examples_llm/overseas_stock/daytime_order_rvsecncl/...py
#     [v1_해외주식-027]  /uapi/overseas-stock/v1/trading/daytime-order-rvsecncl
#     TTTS6038U
#
# OVRS_EXCG_CD is NASD/NYSE/AMEX -- the SAME code space the regular order
# uses. This module previously recorded BAQ/BAY/BAA as the daytime order
# codes and refused the session on that basis; those three are the
# real-time QUOTE stream's tr_key values (delayed_ccnl.py), a different
# API entirely. The mistake cost S6-O its order route for no reason.
#
# The payload is the regular one minus SLL_TYPE, which the daytime
# endpoint does not take.
DAYTIME_ORDER_PATH = "/uapi/overseas-stock/v1/trading/daytime-order"
DAYTIME_CANCEL_PATH = "/uapi/overseas-stock/v1/trading/daytime-order-rvsecncl"
TR_ID_DAYTIME_ORDER_US = {("live", "buy"): "TTTS6036U",
                          ("live", "sell"): "TTTS6037U"}
TR_ID_DAYTIME_CANCEL = {"live": "TTTS6038U"}

#: Sessions whose order route the official specification defines.
#:
#: REGULAR uses the standard US order; OVERNIGHT_DAYTIME uses the
#: daytime family above. PREMARKET and AFTER_HOURS are ABSENT on
#: purpose: the overseas API exposes no US extended-hours order route.
#: The complete set of US order TRs in the reference is TTTT1002U/1006U,
#: TTTT1004U and the three daytime ones -- ORD_DVSN's LOO/LOC/MOO/MOC
#: values are at-the-open and at-the-close types WITHIN the regular
#: session, not extended-hours trading. A session with no specified
#: route stays refused rather than being served a guessed one.
#: Sessions the general order family serves. The overseas order API
#: documents US orders in the premarket and aftermarket as well as the
#: regular session -- they share one endpoint and one TR family, because
#: there is nothing session-specific about them on the wire.
#:
#: Reading "there is no premarket-specific TR" as "the API cannot order
#: in premarket" was wrong, and it cost S6 two of the four sessions it
#: scans. The absence of a distinct TR is what SHARING a route looks
#: like, not evidence that the route is unavailable.
GENERAL_SESSIONS = frozenset({"PREMARKET", "REGULAR", "AFTER_HOURS"})
DAYTIME_SESSIONS = frozenset({"OVERNIGHT_DAYTIME"})

FAMILY_GENERAL = "GENERAL"
FAMILY_DAYTIME = "DAYTIME"

#: Session -> API family. The aftermarket EXTENSION is not a session
#: here: it is gated behind a per-customer application, so it is refused
#: by `config.kis_market_schedule` before a route is ever asked for.
FAMILY_BY_SESSION = {s: FAMILY_GENERAL for s in GENERAL_SESSIONS}
FAMILY_BY_SESSION.update({s: FAMILY_DAYTIME for s in DAYTIME_SESSIONS})

ROUTED_SESSIONS = frozenset(FAMILY_BY_SESSION)


def family_for_session(session):
    """The API family a session addresses, or raise.

    `None` means "not specified" and keeps the regular route, matching
    `order_route_for`. A blank string does not: that means a caller
    COMPUTED a session and got nothing, and on a live-order path a fault
    must not resolve to a working route.
    """
    if session is None:
        return FAMILY_GENERAL
    name = str(session).strip().upper()
    family = FAMILY_BY_SESSION.get(name)
    if family is None:
        raise KISBrokerError(
            f"no KIS order route is specified for session {name!r}; the "
            "overseas API serves the premarket, regular and aftermarket "
            "sessions through the general order family and US daytime "
            "trading through its own")
    return family


def order_route_for_family(family, env_key, side):
    """`(path, tr_id)` for an API family, or raise.

    Families are never assumed to share a route. Serving daytime with the
    general TR would be a real order sent to an endpoint that does not
    run at that hour, and the failure is a rejected or mis-booked live
    order rather than an error anyone sees in testing.
    """
    name = str(family or "").strip().upper()
    if name == FAMILY_GENERAL:
        tr_id, path = TR_ID_ORDER_US.get((env_key, side)), ORDER_PATH
    elif name == FAMILY_DAYTIME:
        tr_id, path = TR_ID_DAYTIME_ORDER_US.get((env_key, side)), DAYTIME_ORDER_PATH
    else:
        raise KISBrokerError(f"no KIS order route for family {name!r}")
    if tr_id is None:
        raise KISBrokerError(
            f"no order TR_ID for family={name!r} env={env_key!r} side={side!r}")
    return path, tr_id


def cancel_route_for_family(family, env_key):
    """`(path, tr_id)` for cancelling an order in this API family."""
    name = str(family or "").strip().upper()
    if name == FAMILY_GENERAL:
        tr_id, path = TR_ID_CANCEL.get(env_key), CANCEL_PATH
    elif name == FAMILY_DAYTIME:
        tr_id, path = TR_ID_DAYTIME_CANCEL.get(env_key), DAYTIME_CANCEL_PATH
    else:
        raise KISBrokerError(f"no KIS cancel route for family {name!r}")
    if tr_id is None:
        raise KISBrokerError(
            f"no cancel TR_ID for family={name!r} env={env_key!r}")
    return path, tr_id


def order_route_for(session, env_key, side):
    """`(path, tr_id)` for this session, or raise.

    Sessions are never assumed to share a route. Serving OVERNIGHT with
    the REGULAR TR would be a real order sent to an endpoint that does
    not run at that hour, and the failure mode is a rejected or
    mis-booked live order rather than an error anyone sees in testing.
    """
    # Only None means "not specified". An empty or blank string means a
    # caller COMPUTED a session and got nothing, which is a fault -- and
    # on a live-order path a fault must not resolve to a working route.
    return order_route_for_family(family_for_session(session), env_key, side)


def cancel_route_for(session, env_key):
    """`(path, tr_id)` for cancelling an order placed INTO this session.

    The cancel side had the defect the order side was fixed for: it read
    `TR_ID_CANCEL[env]` and `CANCEL_PATH` unconditionally, so a daytime
    order -- placed through /trading/daytime-order with TTTS6036U -- was
    cancelled through /trading/order-rvsecncl with TTTT1004U. A cancel
    addressed to the wrong family is not a cancel: the resting order it
    was meant to pull stays live, which is the failure that matters when
    the reason for cancelling is that something has gone wrong.

    Mirrors `order_route_for` exactly, including its treatment of None
    ("not specified", so the regular route) versus a blank string (a
    caller COMPUTED a session and got nothing, which is a fault).
    """
    return cancel_route_for_family(family_for_session(session), env_key)

# EXCD (3-letter) is the quotations-API exchange code (price/quote
# endpoint); OVRS_EXCG_CD (4-letter, order/balance endpoints) is a
# DIFFERENT code space -- verified against the reference repo's order.py
# ("NASD"/"NYSE"/"AMEX", not "NAS"/"NYS"/"AMS"). Conflating the two was a
# real bug this comparison caught: order submission/orderable-amount
# calls were sending the wrong-format exchange code.
# HIGH-1: both tables now live in domain/exchange.py so that price,
# order, cancel and response-normalization cannot drift apart. The names
# below remain as thin views for readability; nothing writes to them.
# These stay in the wire vocabulary an operator reads in KIS's own docs
# ("AMEX", not the canonical USExchange.NYSE_AMERICAN). They are derived
# from the central tables, so a change there shows up here, but lookups
# go through normalize_exchange() below -- which accepts either spelling.
_EXCHANGE_TO_EXCD = {
    "NASDAQ": to_kis_exchange_code(USExchange.NASDAQ),
    "NYSE": to_kis_exchange_code(USExchange.NYSE),
    "AMEX": to_kis_exchange_code(USExchange.NYSE_AMERICAN),
}
_EXCHANGE_TO_ORDER_EXCG_CD = {
    "NASDAQ": to_kis_order_exchange_code(USExchange.NASDAQ),
    "NYSE": to_kis_order_exchange_code(USExchange.NYSE),
    "AMEX": to_kis_order_exchange_code(USExchange.NYSE_AMERICAN),
}

# HIGH-1: KIS answers a wrong-exchange price query with SUCCESS and an
# empty price rather than an error (verified on Oracle: BBVA/EXCD=NAS ->
# rt_cd=0, last=''). These reason codes let an operator tell that apart
# from a genuinely unavailable quote, instead of seeing one flat
# "PRICE_UNAVAILABLE" for both.
REASON_PRICE_FIELD_EMPTY = "PRICE_FIELD_EMPTY"
REASON_PRICE_EXCHANGE_MISMATCH_SUSPECTED = "PRICE_EXCHANGE_MISMATCH_SUSPECTED"
REASON_PRICE_RESPONSE_MALFORMED = "PRICE_RESPONSE_MALFORMED"
REASON_PRICE_NOT_AVAILABLE = "PRICE_NOT_AVAILABLE"

# -- CODEX-052: verification status ------------------------------------
# See the module docstring for what the two axes mean. Nothing here is a
# runtime switch; it is the authoritative record of HOW each wire-format
# value was established, so an operator can tell "confirmed against KIS's
# own examples" apart from "confirmed against a real KIS response".
REFERENCE_VERIFIED = "REFERENCE_VERIFIED"
REFERENCE_UNVERIFIED = "REFERENCE_UNVERIFIED"
LIVE_RESPONSE_CONFIRMED = "LIVE_RESPONSE_CONFIRMED"
LIVE_RESPONSE_PENDING = "LIVE_RESPONSE_PENDING"


# Which pilot posture actually depends on a value. Named here, beside
# the matrix, so preflight and runtime cannot each keep their own list.
REQUIRED_FOR_OBSERVE = "OBSERVE"
REQUIRED_FOR_ARMED = "ARMED"
# A value the PAPER environment uses and the live environment never
# reads. Its evidence still matters -- it is tracked, and it is not
# marked confirmed without a real response -- but it cannot gate live
# eligibility, because no live order path can reach it.
REQUIRED_FOR_PAPER = "PAPER"

# Everything ARMED does, it does on top of what OBSERVE does, so an
# OBSERVE requirement is automatically an ARMED requirement too.
_OBSERVE_AND_ARMED = frozenset({REQUIRED_FOR_OBSERVE, REQUIRED_FOR_ARMED})
_ARMED_ONLY = frozenset({REQUIRED_FOR_ARMED})
_PAPER_ONLY = frozenset({REQUIRED_FOR_PAPER})

# A value only the US DAYTIME session's order path reads.
#
# Deliberately NOT an ARMED requirement. ARMED's outstanding set is the
# five values one authorised REGULAR order confirms, and the bootstrap
# is built to clear exactly those. The daytime values cannot be
# confirmed by a regular order at all -- different endpoint, different
# TR family, and a session that is closed while REGULAR is open -- so
# folding them into ARMED would leave the gate waiting on ten values
# that no single bootstrap could ever satisfy, and would block the
# REGULAR session on evidence about a route it never takes.
#
# They gate the OVERNIGHT_DAYTIME session and nothing else. That session
# gets its own bootstrap, in its own hours, against its own endpoint.
REQUIRED_FOR_DAYTIME = "DAYTIME"
_DAYTIME_ONLY = frozenset({REQUIRED_FOR_DAYTIME})


class WireValueVerification(NamedTuple):
    """One wire-format value, how far its verification got, and which
    posture actually depends on it.

    `required_for` is derived from where the value is REFERENCED, not
    from what its name suggests. `order_exchange_code_space` is the
    example that matters: it reads like an order-only concern, but
    OVRS_EXCG_CD is what `_sweep_exchanges()` puts on the balance,
    open-order and fill reads, so OBSERVE depends on it completely.
    Classifying by name would have un-gated it.
    """

    name: str
    value: str
    reference_status: str
    live_status: str
    source: str
    required_for: frozenset = _OBSERVE_AND_ARMED


#: The GENERAL route's live evidence, observed rather than documented.
#:
#: LIMITED LIVE bootstrap, 2026-08-26, PREMARKET: a real qty-1 BUY of TPG
#: at 52.88 was ACCEPTED as KIS order 0030661086, echoed back by the
#: open-order read as NASD / 매수 / ft_ord_qty=1 / ft_ord_unpr3=52.88000000,
#: and then CANCELLED with the cancel confirmed. Both transports were
#: seen on the wire.
#:
#: Premarket, the regular session and the aftermarket all address this
#: route, so one live response confirms these five for all three. It says
#: nothing about the DAYTIME family: different endpoint, different TR
#: family, and its five stay LIVE_RESPONSE_PENDING until a real daytime
#: response of its own.
#: The one live response the DAYTIME family has produced.
#:
#: S6 exited DT in OVERNIGHT_DAYTIME on 2026-08-27: the order was built
#: for the DAYTIME family, addressed to /trading/daytime-order with
#: TTTS6037U, accepted as 0000001014 and filled 1 @ 51.61.
#:
#: It confirms the PATH and the SELL TR, and those only. TTTS6036U has
#: never carried a buy and TTTS6038U has never carried a cancel; marking
#: either from this would be asserting a wire value that nothing has
#: exercised, which is precisely what this matrix exists to prevent.
DAYTIME_SELL_EVIDENCE = (
    "LIVE 2026-08-27 OVERNIGHT_DAYTIME: S6 exit of DT -> daytime-order "
    "TTTS6037U accepted odno=0000001014, filled 1 @ 51.61"
)

BOOTSTRAP_GENERAL_EVIDENCE = (
    "LIMITED LIVE bootstrap 2026-08-26 PREMARKET: BUY TPG qty=1 @52.88 "
    "-> ACCEPTED odno=0030661086 (NASD, 매수, ft_ord_qty=1, "
    "ft_ord_unpr3=52.88000000); CANCEL -> CONFIRMED"
)

VERIFICATION_MATRIX = (
    WireValueVerification(
        "order_path", ORDER_PATH, REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        BOOTSTRAP_GENERAL_EVIDENCE,
        required_for=_ARMED_ONLY,
    ),
    WireValueVerification(
        "order_tr_id_live_buy", TR_ID_ORDER_US[("live", "buy")], REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        BOOTSTRAP_GENERAL_EVIDENCE,
        required_for=_ARMED_ONLY,
    ),
    WireValueVerification(
        "cancel_path", CANCEL_PATH, REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        BOOTSTRAP_GENERAL_EVIDENCE,
        required_for=_ARMED_ONLY,
    ),
    WireValueVerification(
        "cancel_tr_id_live", TR_ID_CANCEL["live"], REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        BOOTSTRAP_GENERAL_EVIDENCE,
        required_for=_ARMED_ONLY,
    ),
    # PAPER-only, and therefore not an ARMED requirement. `_env_key()`
    # selects TR_ID_CANCEL["live"] whenever KIS_ENV=live, so no live
    # order or cancel can read this value -- gating live eligibility on
    # it would block a real order on evidence about a code path that
    # real order can never take.
    #
    # Its status is NOT upgraded. It stays LIVE_RESPONSE_PENDING because
    # no response has confirmed it; only its SCOPE changed. Fabricating
    # a confirmation from documentation is the thing this matrix exists
    # to prevent, and moving a value out of scope is not the same as
    # confirming it.
    WireValueVerification(
        "cancel_tr_id_paper", TR_ID_CANCEL["paper"], REFERENCE_VERIFIED, LIVE_RESPONSE_PENDING,
        "examples_llm/overseas_stock/order_rvsecncl/order_rvsecncl.py",
        required_for=_PAPER_ONLY,
    ),
    WireValueVerification(
        "cancel_price_field_rule", "OVRS_ORD_UNPR=0", REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        BOOTSTRAP_GENERAL_EVIDENCE + " -- the cancel that KIS confirmed "
        "carried OVRS_ORD_UNPR=0 and RVSE_CNCL_DVSN_CD=02, so the rule is "
        "observed rather than read from the reference docstring",
        required_for=_ARMED_ONLY,
    ),
    # US daytime trading (OVERNIGHT_DAYTIME). REFERENCE_VERIFIED against
    # [v1_해외주식-026] and [v1_해외주식-027] in the official repo; no live
    # response has confirmed them, exactly as for the regular route.
    # Listing them here is what keeps "the constant exists" from being
    # mistaken for "the wire has been exercised".
    WireValueVerification(
        "daytime_order_path", DAYTIME_ORDER_PATH, REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        DAYTIME_SELL_EVIDENCE,
        required_for=_DAYTIME_ONLY,
    ),
    WireValueVerification(
        "daytime_order_tr_id_live_buy", TR_ID_DAYTIME_ORDER_US[("live", "buy")],
        REFERENCE_VERIFIED, LIVE_RESPONSE_PENDING,
        "examples_llm/overseas_stock/daytime_order/daytime_order.py",
        required_for=_DAYTIME_ONLY,
    ),
    WireValueVerification(
        "daytime_order_tr_id_live_sell", TR_ID_DAYTIME_ORDER_US[("live", "sell")],
        REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        DAYTIME_SELL_EVIDENCE,
        required_for=_DAYTIME_ONLY,
    ),
    WireValueVerification(
        "daytime_cancel_path", DAYTIME_CANCEL_PATH, REFERENCE_VERIFIED,
        LIVE_RESPONSE_PENDING,
        "examples_llm/overseas_stock/daytime_order_rvsecncl/daytime_order_rvsecncl.py",
        required_for=_DAYTIME_ONLY,
    ),
    WireValueVerification(
        "daytime_cancel_tr_id_live", TR_ID_DAYTIME_CANCEL["live"],
        REFERENCE_VERIFIED, LIVE_RESPONSE_PENDING,
        "examples_llm/overseas_stock/daytime_order_rvsecncl/daytime_order_rvsecncl.py",
        required_for=_DAYTIME_ONLY,
    ),
    # Confirmed by a REAL live-account read, not by documentation:
    # scripts/verify_kis_observe_responses.py on the Oracle host,
    # 2026-08-06, 12/12 checks -- AAPL and MSFT both answered on
    # PRICE_PATH with output.last carrying a positive float.
    WireValueVerification(
        "price_path", PRICE_PATH, REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: verify_kis_observe_responses.py (Oracle, 2026-08-06)",
    ),
    WireValueVerification(
        "price_field_last", "output.last", REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: output.last -> positive float for AAPL/MSFT via EXCD=NAS",
    ),
    # Confirmed by the account READS, which is where this code space is
    # actually used: _sweep_exchanges() puts OVRS_EXCG_CD on balance,
    # open-order and fill queries, and all three venues answered.
    WireValueVerification(
        "order_exchange_code_space", "NASD/NYSE/AMEX", REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: NASD/NYSE/AMEX accepted on balance, positions, "
        "open-orders and fills (Oracle, 2026-08-06)",
    ),
    # ORACLE-CASH-01. Orderable cash is an OBSERVE requirement because
    # OBSERVE sizes every candidate: without this endpoint there is no
    # cash figure at all, and the entry evaluation stops before any other
    # gate runs. The three entries were confirmed together by one live
    # read on the Oracle host (2026-08-06): TTTS3007R on PSAMOUNT_PATH
    # answered for AAPL/NASD at a real limit price with
    # output.ord_psbl_frcr_amt carrying a positive float.
    WireValueVerification(
        "orderable_amount_path", PSAMOUNT_PATH, REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: inquire-psamount answered on the live account "
        "(Oracle, 2026-08-06)",
    ),
    WireValueVerification(
        "orderable_amount_tr_id_live", TR_ID_PSAMOUNT["live"], REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: TTTS3007R accepted on the LIVE account -- the paper "
        "TR_ID (VTTS3007R) is a separate value and is not confirmed by this "
        "(Oracle, 2026-08-06)",
    ),
    WireValueVerification(
        "orderable_amount_field", f"output.{ORDERABLE_AMOUNT_FIELD}", REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: output.ord_psbl_frcr_amt carried a positive float "
        "(Oracle, 2026-08-06)",
    ),
    # The DISPROVED value, recorded so it cannot quietly come back. This
    # one was never in the matrix, which is exactly why a wrong field name
    # survived: the matrix only covered price and order values, so nothing
    # ever compared the balance response's cash fields against a real one.
    # PHASE 4C. The answer to balance_cash_fields_absent: the cash IS
    # reported, by a DIFFERENT endpoint the wrapper did not implement.
    # Confirmed by a live read-only probe on the Oracle host (2026-08-16),
    # cross-validated against get_orderable_usd() -- which is itself
    # already LIVE_RESPONSE_CONFIRMED, so the two agree on a value neither
    # was told by the other.
    WireValueVerification(
        "account_cash_path", PRESENT_BALANCE_PATH, REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: inquire-present-balance answered rt_cd=0 on the "
        "live account (Oracle, 2026-08-16); endpoint and TR id are from the "
        "official koreainvestment/open-trading-api repository",
    ),
    WireValueVerification(
        "account_cash_tr_id_live", TR_ID_PRESENT_BALANCE["live"], REFERENCE_VERIFIED,
        LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: CTRP6504R accepted on the LIVE account -- the "
        "paper TR_ID (VTRP6504R) is a separate value and is NOT confirmed by "
        "this (Oracle, 2026-08-16)",
    ),
    WireValueVerification(
        "account_cash_field", f"output2[crcy_cd=USD].{ACCOUNT_CASH_FIELD}",
        REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: matched get_orderable_usd() EXACTLY, while "
        "frcr_evlu_amt2 and output3.frcr_use_psbl_amt came back x1,414.88 "
        "(FX scale) and output3.tot_asst_amt x5,844.18 (Oracle, 2026-08-16)",
    ),
    # The DISPROVED equity candidate, recorded so it cannot quietly come
    # back. It is the only field whose name suggests "total assets", which
    # is exactly why it needed disproving rather than ignoring.
    WireValueVerification(
        "account_equity_not_in_tot_asst_amt", f"{TR_ID_PRESENT_BALANCE['live']}:output3-is-KRW",
        REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: output3 returned IDENTICAL values for "
        "WCRC_FRCR_DVSN_CD=01 and =02, and tot_asst_amt was x5,844.18 the USD "
        "orderable amount -- a won-denominated total across every currency the "
        "account holds, not USD equity (Oracle, 2026-08-16)",
    ),
    WireValueVerification(
        "orderable_amount_is_account_level", f"{TR_ID_PSAMOUNT['live']}:symbol-and-price-invariant",
        REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: AAPL and MSFT at current, +1% and half price gave "
        "a 0.0000% spread across all six reads, and the value equalled the "
        "account USD deposit exactly (Oracle, 2026-08-16)",
    ),
    WireValueVerification(
        "balance_cash_fields_absent", f"{TR_ID_BALANCE['live']}:no-cash-fields",
        REFERENCE_VERIFIED, LIVE_RESPONSE_CONFIRMED,
        "live read-only probe: TTTS3012R output2 returned nine purchase/valuation/"
        "P&L fields on every venue leg and NO frcr_dncl_amt1 / frcr_use_psbl_amt "
        "(Oracle, 2026-08-06)",
    ),
)

# The items an operator must still confirm against a REAL KIS response
# during the Oracle read-only stage. Kept in sync with the runbook by
# tests/test_kis_verification_matrix.py.
LIVE_RESPONSE_PENDING_ITEMS = tuple(
    entry.name for entry in VERIFICATION_MATRIX
    if entry.live_status == LIVE_RESPONSE_PENDING
)


def matrix_entries_for(posture):
    """Every wire value the given posture actually depends on."""
    return tuple(entry for entry in VERIFICATION_MATRIX
                 if posture in entry.required_for)


def pending_items_for(posture):
    """The values that posture needs and a real response has not yet
    confirmed. Empty means that posture's wire format is established.

    The one place either preflight or runtime should ask. A second,
    hand-written relevance list somewhere else is exactly how a value
    that does matter gets silently un-gated.
    """
    return tuple(entry.name for entry in matrix_entries_for(posture)
                 if entry.live_status == LIVE_RESPONSE_PENDING)


class KISBrokerError(Exception):
    """Raised on any KIS adapter failure -- auth, network, or a
    non-success KIS response body. Callers must treat this as a hard
    block; there is no fallback broker (spec §2: "장애 시 Alpaca나 다른
    증권사로 자동 우회 주문하지 않는다")."""


class KISAccountSweepError(KISBrokerError):
    """ORACLE-HIGH-01: one venue leg of an account-wide read failed, so
    the result would be a PARTIAL account. Callers must treat it as
    unavailable -- never as "these are all the positions/orders/fills"."""

    def __init__(self, message, *, exchange_code=None):
        super().__init__(message)
        self.exchange_code = exchange_code
        self.reason_code = "KIS_EXCHANGE_LEG_FAILED"


# Keys this module adds to a row; excluded from identity so tagging can
# never change what counts as the same record.
_TAG_KEYS = ("kis_exchange_code", "canonical_exchange",
             "kis_requested_exchange_code")


def _row_venue(row, code):
    """The venue a row belongs to, per the row itself.

    KIS account reads do not strictly filter by the OVRS_EXCG_CD they are
    given -- measured on the live account, a balance request for NASD
    returned a row whose own ovrs_excg_cd was NYSE, and a fill request did
    the same. Keying identity on the REQUESTED code therefore makes one
    record look like several, once per leg that echoed it.

    The row's own field is authoritative; the requested code is only the
    fallback when the row does not say.
    """
    native = str((row or {}).get("ovrs_excg_cd") or "").strip()
    return native or code


def _order_identity(row, code):
    """An ORDER is identified by its number, scoped to its venue -- two
    venues could in principle issue the same odno.

    The venue comes from the row (see `_row_venue`): a leg that echoes
    another venue's order must not make it look like a second order.
    """
    for field in ("odno", "ODNO"):
        value = row.get(field)
        if value:
            return (_row_venue(row, code), str(value))
    return None


def _execution_identity(row, code):
    """An EXECUTION is NOT identified by its order number.

    A partially-filled order produces several fill rows sharing one odno:

        odno=1001 qty=2
        odno=1001 qty=3      -> 5 filled, not 2

    Deduplicating those by odno silently discards fills, which corrupts
    filled quantity, remaining quantity, position size, sellable size and
    every reconciliation decision downstream. This module previously did
    exactly that, undoing CODEX-045.

    KIS gives no documented per-execution id here, so identity is the
    whole row (plus its venue). Any field that differs between two
    executions -- sequence, time, quantity, price -- keeps them apart,
    while a row repeated verbatim by pagination collapses to one.

    RESIDUAL RISK: two genuinely distinct executions with identical venue,
    order number, timestamp, quantity and price and no sequence field
    would merge. Nothing in the response distinguishes them, so this is
    recorded rather than guessed at.
    """
    payload = tuple(sorted(
        (key, str(value)) for key, value in row.items() if key not in _TAG_KEYS
    ))
    # Venue from the ROW, not from the leg that asked. Otherwise the same
    # execution echoed by two legs yields two identities, and
    # _find_kis_fill_for_order SUMS per-execution quantities -- a 1-share
    # fill returned twice would be recorded as 2 filled.
    return (_row_venue(row, code), payload)


def _merge_rows(legs, tag, *, identity):
    """Concatenates every venue leg's rows, dropping only rows the given
    identity function says are the SAME record."""
    merged = []
    seen = set()
    for code, body in legs:
        for row in tag(body.get("output") or [], code):
            if isinstance(row, dict):
                key = identity(row, code)
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
            merged.append(row)
    return merged


class KISPriceUnavailableError(KISBrokerError):
    """A price could not be established. `reason_code` distinguishes an
    empty field on an otherwise successful response (the exchange-code
    signature) from a malformed or absent one."""

    def __init__(self, message, *, reason_code, symbol=None, exchange=None,
                 kis_exchange_code=None, success_code=None, price_field="last"):
        super().__init__(message)
        self.reason_code = reason_code
        self.symbol = symbol
        self.exchange = exchange
        self.kis_exchange_code = kis_exchange_code
        self.success_code = success_code
        self.price_field = price_field

    def diagnostic(self):
        """Operator-facing detail: symbol and venue only. Never the raw
        response, the account number or a token."""
        return {
            "symbol": self.symbol,
            "canonical_exchange": self.exchange,
            "requested_kis_exchange_code": self.kis_exchange_code,
            "price_field": self.price_field,
            "response_success_code": self.success_code,
            "reason_code": self.reason_code,
        }

# -- CODEX-052: verification status ------------------------------------
# See the module docstring for what the two axes mean. Nothing here is a
# runtime switch; it is the authoritative record of HOW each wire-format
# value was established, so an operator can tell "confirmed against KIS's
# own examples" apart from "confirmed against a real KIS response".
REFERENCE_VERIFIED = "REFERENCE_VERIFIED"
REFERENCE_UNVERIFIED = "REFERENCE_UNVERIFIED"
LIVE_RESPONSE_CONFIRMED = "LIVE_RESPONSE_CONFIRMED"
LIVE_RESPONSE_PENDING = "LIVE_RESPONSE_PENDING"


class KISAmbiguousResponseError(KISBrokerError):
    """Raised specifically for timeouts/connection errors/non-parseable
    responses to a state-mutating call (order/cancel) -- the caller
    (execution/order_state_machine.py) must treat this as UNKNOWN, never
    as a definite failure eligible for retry (spec §9)."""


class KISAccountCashUnavailableError(KISBrokerError):
    """The account-level USD cash figure could not be established.
    Deliberately NOT a zero-cash outcome -- see get_account_cash_usd."""

    def __init__(self, message, *, detail=None):
        super().__init__(message)
        self.reason_code = "ACCOUNT_CASH_UNAVAILABLE"
        self.detail = detail

    def diagnostic(self):
        return {"reason_code": self.reason_code, "detail": self.detail,
                "field": ACCOUNT_CASH_FIELD}


class KISOrderableCashUnavailableError(KISBrokerError):
    """The orderable-amount read did not yield a usable number.

    A distinct type because the caller's correct response is distinct: an
    unusable read must be recorded as ORDERABLE_CASH_UNAVAILABLE and stop
    the candidate, NOT recorded as INSUFFICIENT_CASH. Collapsing the two
    is how an API outage reads, in the audit trail, exactly like an
    ordinary underfunded day.
    """

    def __init__(self, message, *, symbol=None, detail=None):
        super().__init__(message)
        self.reason_code = ORDERABLE_CASH_UNAVAILABLE
        self.symbol = symbol
        self.detail = detail

    def diagnostic(self):
        """Operator-facing detail only -- never a raw body or an account."""
        return {"symbol": self.symbol, "reason_code": self.reason_code,
                "detail": self.detail, "field": ORDERABLE_AMOUNT_FIELD}


def _parse_orderable_amount(body, *, symbol=None):
    """Strict: `output.ord_psbl_frcr_amt` as a finite, non-negative float.

    Everything else raises -- missing `output`, wrong `output` type,
    missing field, None, "", "NaN", "Infinity", a negative amount, a bool,
    a list, a dict. There is deliberately no default and no coercion of a
    doubtful value, because the only safe interpretation of "I could not
    read the balance" is "do not size an order".

    An explicit numeric 0 is NOT an error: it is a real, successfully-read
    zero balance, and the caller reports it as INSUFFICIENT_CASH.
    """
    def _fail(detail):
        return KISOrderableCashUnavailableError(
            f"KIS orderable-amount response unusable ({detail})",
            symbol=symbol, detail=detail,
        )

    if not isinstance(body, dict):
        raise _fail("body_not_an_object")
    output = body.get("output")
    if not isinstance(output, dict):
        raise _fail("output_missing" if output is None else "output_not_an_object")
    if ORDERABLE_AMOUNT_FIELD not in output:
        raise _fail("field_missing")
    raw = output.get(ORDERABLE_AMOUNT_FIELD)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise _fail(f"field_type_{type(raw).__name__}")
    if isinstance(raw, str) and not raw.strip():
        raise _fail("field_empty")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise _fail("field_not_numeric") from None
    if not math.isfinite(value):
        raise _fail("field_not_finite")
    if value < 0:
        raise _fail("field_negative")
    return value


def _excd_for(exchange: str) -> str:
    try:
        return to_kis_exchange_code(exchange)
    except UnsupportedExchangeError as exc:
        raise KISBrokerError(f"no KIS exchange code mapping for exchange={exchange!r}") from exc


def _order_excg_for(exchange: str) -> str:
    try:
        return to_kis_order_exchange_code(exchange)
    except UnsupportedExchangeError as exc:
        raise KISBrokerError(
            f"no KIS order-exchange code mapping for exchange={exchange!r}"
        ) from exc


class KISBroker:
    def __init__(self, config: Optional[KISConfig] = None, session=None, now_fn=None,
                 limiter=None, token_cache=None):
        self.config = config or KISConfig()
        self._limiter = limiter
        self._token_cache = token_cache
        self.session = session or requests.Session()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._access_token = None
        self._token_expires_at = None

    def _now(self):
        return self._now_fn()

    # -- auth -----------------------------------------------------------

    def _env_key(self):
        return "live" if self.config.is_live else "paper"

    def _ensure_token(self):
        """Issues a token on first use and refreshes it once expired.
        Never called by a read/order method directly -- always via
        _auth_headers() -- so every call site gets the same refresh
        behavior automatically."""
        if self._access_token is not None and self._token_expires_at is not None \
                and self._now() < self._token_expires_at:
            return self._access_token
        self.config.validate_credentials()
        # MEDIUM: the in-memory cache above only helps WITHIN one process.
        # Each systemd unit is its own process, and KIS issues at most one
        # token a minute (EGW00133, hit for real on Oracle), so the token
        # is shared through a locked file. issue_fn below runs at most
        # once, inside that lock, after a second cache check.
        cache = self._token_cache or kis_token_cache.get_cache()
        token = cache.get_or_issue(self.config, self._issue_token)
        self._access_token = token
        return token

    def _issue_token(self):
        """Performs the actual token request. Returns
        (token, token_type, expires_in) for the cache to persist."""
        # HIGH-2/MEDIUM: KIS issues at most one token a minute; pace it in
        # its own category so a read burst cannot consume that budget.
        (self._limiter or kis_rate_limiter.get_limiter()).wait(
            category=kis_rate_limiter.CATEGORY_TOKEN)
        try:
            response = self.session.request(
                "POST", f"{self.config.base_url}{TOKEN_PATH}",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.config.app_key,
                    "appsecret": self.config.app_secret,
                },
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            raise KISBrokerError(
                f"KIS token issuance failed (network): {redact_text(str(exc))}"
            ) from exc
        if response.status_code != 200:
            raise KISBrokerError(f"KIS token issuance failed: HTTP {response.status_code} {redact_text(response.text)}")
        try:
            body = response.json()
            token = body["access_token"]
            expires_in = int(body.get("expires_in", 0))
        except (ValueError, KeyError, TypeError) as exc:
            raise KISBrokerError(f"KIS token response malformed: {redact_text(str(exc))}") from exc
        # Refresh 60s early so a call started right at expiry never races
        # a mid-flight 401.
        self._token_expires_at = datetime.fromtimestamp(
            self._now().timestamp() + max(expires_in - 60, 0), tz=timezone.utc,
        )
        return token, body.get("token_type") or "Bearer", expires_in

    def _auth_headers(self, tr_id, *, tr_cont=""):
        token = self._ensure_token()
        return {
            "authorization": f"Bearer {token}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "tr_cont": tr_cont,
            "Content-Type": "application/json",
        }

    def _get(self, path, tr_id, params):
        """HIGH-2: every read is paced by the shared limiter, and a KIS
        per-second rate limit (EGW00201) is retried with backoff rather
        than surfacing as a hard failure. Reads are safe to repeat --
        orders and cancels deliberately are not, and are never retried."""
        limiter = self._limiter or kis_rate_limiter.get_limiter()

        def _attempt():
            try:
                response = self.session.request(
                    "GET", f"{self.config.base_url}{path}",
                    headers=self._auth_headers(tr_id), params=params, timeout=10,
                )
            except requests.exceptions.RequestException as exc:
                raise KISBrokerError(
                    f"KIS GET {path} failed (network): {redact_text(str(exc))}"
                ) from exc
            body = None
            try:
                body = response.json()
            except ValueError:
                body = None
            if kis_rate_limiter.is_rate_limited(body):
                raise kis_rate_limiter.KISRateLimitSignal()
            if response.status_code != 200:
                raise KISBrokerError(
                    f"KIS GET {path} failed: HTTP {response.status_code} "
                    f"{redact_text(response.text)}"
                )
            if body is None:
                raise KISBrokerError(f"KIS GET {path} response not JSON")
            return body

        return limiter.call_with_retry(
            _attempt, category=kis_rate_limiter.CATEGORY_READ, describe=path,
        )

    # -- read-only --------------------------------------------------------

    def get_current_price(self, instrument) -> float:
        """Returns the last-traded USD price for `instrument.kis_symbol`.

        CODEX-052: the `output.last` field name is REFERENCE_VERIFIED
        (chk_price.py's own field comment `'last': '현재가'`) but still
        LIVE_RESPONSE_PENDING -- no real KIS quote response has been read
        yet. See VERIFICATION_MATRIX at the top of this module.

        HIGH-1: a wrong-exchange query does NOT fail here -- KIS answers
        rt_cd=0 with `last=''`. That case is reported as
        PRICE_EXCHANGE_MISMATCH_SUSPECTED rather than a flat "price
        unavailable", so the log points at the exchange code."""
        self.config.validate_read_allowed()
        excd = _excd_for(instrument.exchange)
        body = self._get(PRICE_PATH, TR_ID_PRICE, {
            "AUTH": "", "EXCD": excd, "SYMB": instrument.kis_symbol,
        })
        success_code = body.get("rt_cd")
        output = body.get("output")

        def _fail(reason, message):
            raise KISPriceUnavailableError(
                message, reason_code=reason, symbol=instrument.kis_symbol,
                exchange=instrument.exchange, kis_exchange_code=excd,
                success_code=success_code, price_field="last",
            )

        if not isinstance(output, dict):
            _fail(REASON_PRICE_RESPONSE_MALFORMED,
                  f"KIS price response has no usable 'output' object: {safe_repr(output)}")

        if "last" not in output:
            _fail(REASON_PRICE_RESPONSE_MALFORMED,
                  "KIS price response is missing the 'last' field entirely")

        raw_price = output.get("last")
        if isinstance(raw_price, str) and not raw_price.strip():
            # The signature of an exchange-code mismatch: the call
            # SUCCEEDED and the field exists, it is simply empty. Only
            # claim a mismatch when the venue is one we resolved.
            reason = (REASON_PRICE_EXCHANGE_MISMATCH_SUSPECTED
                      if str(success_code) == "0" else REASON_PRICE_FIELD_EMPTY)
            _fail(reason,
                  f"KIS returned an empty price for {instrument.kis_symbol} on "
                  f"EXCD={excd} (rt_cd={success_code}) -- the symbol is most "
                  f"likely not listed on {instrument.exchange}")

        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            # CODEX-050: never interpolate a RAW KIS response into an
            # error -- safe_repr() redacts the structure first.
            _fail(REASON_PRICE_RESPONSE_MALFORMED,
                  f"KIS price response has an unparseable 'last' field: {safe_repr(output)}")
        if not math.isfinite(price) or price <= 0:
            _fail(REASON_PRICE_NOT_AVAILABLE,
                  f"KIS price response has a non-positive/non-finite price: {price!r}")
        return price


    # -- account-wide reads span every supported venue ------------------

    def _sweep_exchanges(self, path, tr_id, base_params, *, describe):
        """ORACLE-HIGH-01: KIS account reads filter by ONE OVRS_EXCG_CD, so
        a single call sees a single venue. Every account read here used to
        pass "NASD", which hid NYSE and AMEX rows -- reconciliation then
        compared a partial account against full internal state.

        Sweeps every supported venue and yields (code, body) pairs. A leg
        that fails aborts the whole sweep: a partial account must never be
        mistaken for a complete one, so the caller gets an exception, not
        a short list.
        """
        results = []
        for code in supported_kis_order_exchange_codes():
            params = dict(base_params)
            params["OVRS_EXCG_CD"] = code
            try:
                # Each leg goes through the shared READ limiter, so the
                # sweep is paced like any other consecutive reads.
                body = self._get(path, tr_id, params)
            except (kis_rate_limiter.KISRateLimitStateInvalid,
                    kis_rate_limiter.KISRateLimitStateUnavailable):
                # A local pacing-state fault is not a venue failure; it
                # must reach the caller with its own reason code, and no
                # further leg may be attempted.
                raise
            except Exception as exc:
                error = KISAccountSweepError(
                    f"{describe} failed for exchange {code}: {exc}",
                    exchange_code=code,
                )
                error.reason_code = getattr(exc, "reason_code", None) or "KIS_EXCHANGE_LEG_FAILED"
                raise error from exc
            results.append((code, body))
        return results

    @staticmethod
    def _tag_rows(rows, code):
        """Preserves which venue each row came from, so a merged result
        never loses the distinction the sweep just established."""
        tagged = []
        for row in rows or []:
            if isinstance(row, dict):
                row = dict(row)
                # KIS's own venue field wins when it is present: the
                # requested code is what we ASKED for, and balance reads
                # have been observed answering with a different venue.
                native = str(row.get("ovrs_excg_cd") or "").strip()
                row.setdefault("kis_exchange_code", native or code)
                row.setdefault("kis_requested_exchange_code", code)
                try:
                    row.setdefault(
                        "canonical_exchange", exchange_for_kis_order_code(code).value)
                except UnsupportedExchangeError:  # pragma: no cover
                    pass
            tagged.append(row)
        return tagged

    def get_price_detail(self, instrument) -> dict:
        """Today's own range for `instrument`, plus tick size and
        tradability. Read-only.

        Returns raw-but-parsed values; interpreting them is the caller's
        job (see s1_live/execution_price.py). Every numeric field is
        returned as a float or None -- never as the empty string KIS sends
        for a wrong-exchange query, because "" compares as less than any
        number and would silently pass a range check.
        """
        self.config.validate_read_allowed()
        excd = _excd_for(instrument.exchange)
        body = self._get(PRICE_DETAIL_PATH, TR_ID_PRICE_DETAIL, {
            "AUTH": "", "EXCD": excd, "SYMB": instrument.kis_symbol,
        })
        output = body.get("output")
        if not isinstance(output, dict):
            raise KISPriceUnavailableError(
                f"KIS price-detail response has no usable 'output': {safe_repr(output)}",
                reason_code=REASON_PRICE_RESPONSE_MALFORMED,
                symbol=instrument.kis_symbol, exchange=instrument.exchange,
                kis_exchange_code=excd, success_code=body.get("rt_cd"),
                price_field="output",
            )

        def number(field):
            raw = output.get(field)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        parsed = {name: number(field) for name, field in PRICE_DETAIL_FIELDS.items()
                  if name not in ("currency", "orderable")}
        parsed["currency"] = (output.get(PRICE_DETAIL_FIELDS["currency"]) or "").strip()
        parsed["orderable_text"] = (output.get(PRICE_DETAIL_FIELDS["orderable"]) or "").strip()
        parsed["symbol"] = instrument.kis_symbol
        parsed["exchange"] = instrument.exchange
        parsed["kis_exchange_code"] = excd
        parsed["success_code"] = body.get("rt_cd")
        parsed["fetched_at"] = self._now().isoformat()
        return parsed

    def get_account_snapshot(self, *, source_label="kis_balance") -> AccountSnapshot:
        self.config.validate_read_allowed()
        tr_id = TR_ID_BALANCE[self._env_key()]
        legs = self._sweep_exchanges(BALANCE_PATH, tr_id, {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        }, describe="KIS balance read")
        # Cash is an ACCOUNT-level figure: every venue leg reports the same
        # USD deposit, so summing would triple it. Take the first leg's
        # summary -- but only after every leg succeeded, so a hidden venue
        # cannot make a partial read look complete.
        summary_rows = legs[0][1].get("output2") or {} if legs else {}
        if isinstance(summary_rows, list):
            summary_rows = summary_rows[0] if summary_rows else {}
        # ORACLE-CASH-01: this response does NOT carry cash. A live read
        # showed TTTS3012R's output2 returning exactly nine
        # purchase/valuation/P&L fields -- frcr_pchs_amt1,
        # ovrs_rlzt_pfls_amt, ovrs_tot_pfls, rlzt_erng_rt,
        # tot_evlu_pfls_amt, tot_pftrt, frcr_buy_amt_smtl1,
        # ovrs_rlzt_pfls_amt2, frcr_buy_amt_smtl2 -- and no deposit or
        # orderable-amount field at all. The previous code read
        # `frcr_dncl_amt1`/`frcr_use_psbl_amt` with a `.get(field, 0) or 0`
        # fallback, so a funded account reported $0 and every candidate
        # sized to zero shares. Orderable cash comes from
        # `get_orderable_usd()` (TTTS3007R), which is per symbol and price
        # and therefore cannot be answered by an account-level read.
        usd_cash, usd_orderable_cash = self._read_balance_cash(summary_rows)
        return AccountSnapshot(
            # KRW is not queried by this path at all, and was previously
            # hardcoded to 0.0 -- indistinguishable from "the account holds
            # no KRW", which this read cannot establish either way.
            krw_cash=None, usd_cash=usd_cash, usd_orderable_cash=usd_orderable_cash,
            usd_reserved_in_open_orders=0.0, as_of=self._now(), source=source_label,
            account_id=self.config.account_no or "",
            cash_source=(
                CASH_SOURCE_BALANCE_LACKS_FIELDS if usd_orderable_cash is None
                else f"{tr_id}:output2"
            ),
        )

    @staticmethod
    def _read_balance_cash(summary_rows):
        """(usd_cash, usd_orderable_cash), each None when the response did
        not carry the field.

        A field that IS present is parsed strictly: an explicit numeric 0
        from KIS is a real zero balance and stays 0.0, which is the one
        case a zero may be reported. Absent, null, blank or non-numeric is
        None -- unknown -- never 0.
        """
        def _parse(field):
            if field not in summary_rows:
                return None
            raw = summary_rows.get(field)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return None
            if isinstance(raw, bool):
                return None
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value) or value < 0:
                return None
            return value

        return _parse("frcr_dncl_amt1"), _parse("frcr_use_psbl_amt")

    def get_account_cash_usd(self) -> float:
        """The account's USD cash, account-level. PHASE 4C.

        `inquire-balance` (TTTS3012R) does not carry cash -- that is
        recorded as `balance_cash_fields_absent` in the matrix above, from
        a live probe. `inquire-present-balance` (CTRP6504R) does, in an
        `output2` row TAGGED with its own currency code, and this reads
        the row whose `crcy_cd` is USD.

        Why this field and not the bigger-looking ones. A live probe
        compared every candidate against `get_orderable_usd()`, which is
        already known to answer in USD:

            output2[USD].frcr_dncl_amt_2      EXACT MATCH
            output2[USD].frcr_drwg_psbl_amt_1 EXACT MATCH
            output2[USD].frcr_evlu_amt2       x1,414.88  (FX scale -> KRW)
            output3.frcr_use_psbl_amt         x1,414.88  (FX scale -> KRW)
            output3.tot_dncl_amt              x4,429.30  (KRW, all currencies)
            output3.tot_asst_amt              x5,844.18  (KRW, all currencies)

        `output3`'s totals came back IDENTICAL whether the request asked
        for the KRW or the foreign-currency division, which is the other
        half of the same finding: they are a won-denominated view across
        every currency the account holds. `tot_asst_amt` is therefore
        emphatically NOT a USD equity figure, despite being the only field
        whose name suggests "total assets" -- using it would put KRW cash
        and an implicit FX rate into the denominator of every risk figure.

        Fail-closed, on the same principle as `get_orderable_usd`: a read
        that cannot produce a well-formed, finite, non-negative number
        raises. It never degrades to 0.0, because "the read failed" and
        "the account has no money" must not produce the same record --
        which is exactly what ORACLE-CASH-01 was.
        """
        self.config.validate_read_allowed()
        tr_id = TR_ID_PRESENT_BALANCE[self._env_key()]
        body = self._get(PRESENT_BALANCE_PATH, tr_id, {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.account_product_cd,
            # 02 = foreign currency. output2 is currency-tagged either
            # way; asking for the foreign-currency division keeps the
            # request honest about what is wanted.
            "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840",
            "TR_MKET_CD": "00", "INQR_DVSN_CD": "00",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        })
        if not isinstance(body, dict):
            raise KISAccountCashUnavailableError(
                f"{tr_id} returned a non-object body")
        if str(body.get("rt_cd")) != "0":
            raise KISAccountCashUnavailableError(
                f"{tr_id} refused the request (rt_cd={body.get('rt_cd')!r}, "
                f"msg_cd={body.get('msg_cd')!r})")

        rows = body.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]
        usd_rows = [row for row in rows
                    if isinstance(row, dict)
                    and str(row.get(ACCOUNT_CASH_CURRENCY_FIELD, "")).strip().upper() == "USD"]
        if not usd_rows:
            raise KISAccountCashUnavailableError(
                f"{tr_id} output2 carried no row tagged {ACCOUNT_CASH_CURRENCY_FIELD}=USD; "
                f"refusing to read a cash figure whose currency is not stated")
        if len(usd_rows) > 1:
            raise KISAccountCashUnavailableError(
                f"{tr_id} output2 carried {len(usd_rows)} USD rows; ambiguous")

        raw = usd_rows[0].get(ACCOUNT_CASH_FIELD)
        if ACCOUNT_CASH_FIELD not in usd_rows[0]:
            raise KISAccountCashUnavailableError(
                f"{tr_id} USD row has no {ACCOUNT_CASH_FIELD} field")
        if raw is None or (isinstance(raw, str) and not raw.strip()) or isinstance(raw, bool):
            raise KISAccountCashUnavailableError(
                f"{tr_id} {ACCOUNT_CASH_FIELD} is empty")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise KISAccountCashUnavailableError(
                f"{tr_id} {ACCOUNT_CASH_FIELD} is not numeric") from None
        if not math.isfinite(value) or value < 0:
            raise KISAccountCashUnavailableError(
                f"{tr_id} {ACCOUNT_CASH_FIELD} is not a usable amount")
        return value

    def get_orderable_usd(self, instrument, limit_price_usd: float) -> float:
        """The account's orderable USD FOR THIS SYMBOL AT THIS PRICE.

        KIS answers orderable cash per (symbol, exchange, limit price) --
        it is not an account-wide constant -- so the caller must pass the
        same limit price it intends to order at. Sizing against a
        different price would size against a different answer.

        Fail-closed: anything short of a well-formed, finite, non-negative
        number raises KISOrderableCashUnavailableError. It never degrades
        to 0.0, because "the read failed" and "the account has no money"
        must not produce the same record.
        """
        self.config.validate_read_allowed()
        tr_id = TR_ID_PSAMOUNT[self._env_key()]
        try:
            body = self._get(PSAMOUNT_PATH, tr_id, {
                "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
                "OVRS_EXCG_CD": _order_excg_for(instrument.exchange),
                "OVRS_ORD_UNPR": str(limit_price_usd), "ITEM_CD": instrument.kis_symbol,
            })
        except KISBrokerError as exc:
            # Network fault, auth failure or a non-success KIS body. All
            # of them mean "unknown", none of them mean "zero".
            raise KISOrderableCashUnavailableError(
                f"KIS orderable-amount read failed: {redact_text(str(exc))}",
                symbol=getattr(instrument, "kis_symbol", None), detail="read_failed",
            ) from exc
        return _parse_orderable_amount(
            body, symbol=getattr(instrument, "kis_symbol", None))

    def get_positions(self) -> List[Position]:
        self.config.validate_read_allowed()
        tr_id = TR_ID_BALANCE[self._env_key()]
        legs = self._sweep_exchanges(BALANCE_PATH, tr_id, {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        }, describe="KIS position read")
        rows = []
        seen_symbols = set()
        for code, body in legs:
            for row in self._tag_rows(body.get("output1") or [], code):
                # Keyed on the venue the ROW reports, not the one the leg
                # asked for.
                #
                # inquire-balance does not strictly filter by
                # OVRS_EXCG_CD. Measured against the live account
                # (2026-08-18, one TX holding): requesting NASD returned
                # the row with ovrs_excg_cd="NYSE", requesting NYSE
                # returned the same row again, and AMEX returned none. A
                # (requested_code, symbol) key therefore produced TWO
                # entries for ONE position -- which the max-open-positions
                # cap counts and reconciliation compares against local
                # state.
                #
                # The row's own ovrs_excg_cd is authoritative, so this
                # still keeps the distinction the old comment wanted: the
                # same ticker genuinely held on two venues carries two
                # different ovrs_excg_cd values and stays two rows.
                venue = str((row or {}).get("ovrs_excg_cd") or "").strip() or code
                key = (venue, (row or {}).get("ovrs_pdno", ""))
                if key[1] and key in seen_symbols:
                    continue
                if key[1]:
                    seen_symbols.add(key)
                rows.append(row)
        current = self._now()
        positions = []
        for row in rows:
            try:
                qty = int(float(row.get("ovrs_cblc_qty", 0) or 0))
                avg_price = float(row.get("pchs_avg_pric", 0) or 0)
                unrealized = float(row.get("evlu_pfls_amt", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise KISBrokerError(
                    f"KIS position row malformed: {safe_repr(row)}: {redact_text(str(exc))}"
                ) from exc
            if qty <= 0:
                continue
            positions.append(Position(
                symbol=row.get("ovrs_pdno", ""), quantity=qty, average_fill_price=avg_price,
                unrealized_pnl=unrealized, realized_pnl=0.0, as_of=current, source="kis_balance",
            ))
        return positions

    def get_open_orders(self) -> list:
        self.config.validate_read_allowed()
        legs = self._sweep_exchanges(NCCS_PATH, TR_ID_NCCS[self._env_key()], {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "SORT_SQN": "DS", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        }, describe="KIS open-order read")
        return _merge_rows(legs, self._tag_rows, identity=_order_identity)

    def get_fills(self, *, start_date: str, end_date: str) -> list:
        """`start_date`/`end_date` are KIS's own YYYYMMDD format."""
        self.config.validate_read_allowed()
        tr_id = TR_ID_CCNL[self._env_key()]
        legs = self._sweep_exchanges(CCNL_PATH, tr_id, {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "PDNO": "%", "ORD_STRT_DT": start_date, "ORD_END_DT": end_date,
            "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
            "SORT_SQN": "DS", "CTX_AREA_NK200": "", "CTX_AREA_FK200": "",
        }, describe="KIS fill-history read")
        # Per-EXECUTION identity: several fills share one odno.
        return _merge_rows(legs, self._tag_rows, identity=_execution_identity)

    # -- order submission ---------------------------------------------

    #: Session used when an order_intent does not carry one. REGULAR is
    #: the only safe default: it is the route this system has always
    #: used, so an existing caller that knows nothing about sessions
    #: keeps its exact previous behaviour rather than silently acquiring
    #: a different endpoint.
    _session_hint = "REGULAR"

    def submit_order(self, order_intent: OrderIntent, instrument, *, authorization=None,
                     bootstrap_capability=None) -> ExecutionRecord:
        """The ONLY method in this codebase that places a real KIS order.
        CODEX-043: `authorization` MUST be a currently-valid
        `execution.authorization.AuthorizedExecution` for this exact
        order_intent, minted by `execution.authorization.
        authorize_new_order()` (which itself checks HALT and runs the
        central Order Gate) -- consumed (single-use) here before
        anything else runs. A direct call bypassing execution/
        execution_engine.py (no `authorization`, or a hand-built one)
        raises UnauthorizedExecutionError before the fail-closed
        live-order gate below is even reached, let alone the network.
        Also still runs the fail-closed live-order gate (config.
        validate_live_order_allowed()) before any network call -- a
        caller cannot bypass it by holding a KISBroker instance built
        before the flag was disabled, since this re-reads self.config
        fresh (frozen dataclass, so a caller must have built a *new*
        KISBroker/KISConfig to flip it, matching AlpacaBroker's own
        credential-rotation-requires-new-instance pattern)."""
        from execution import authorization as _authz
        _authz.consume(authorization, order_intent, expected_action="order")
        # Both checks are PRE-TRANSPORT by construction: neither touches
        # the network, so a failure here means the order definitively did
        # not reach KIS. execution_engine relies on that to release the
        # durable row instead of leaving it possibly-in-flight.
        self.config.validate_live_order_allowed(
            bootstrap_capability=bootstrap_capability, order_intent=order_intent)
        if order_intent.order_type != "limit":
            raise KISBrokerError("only limit orders are permitted in this pilot")
        # The route is chosen from the SESSION, not assumed. REGULAR and
        # daytime trading are different endpoints with different TR
        # families, and a session the specification does not cover is
        # refused here rather than served the regular route.
        path, tr_id = order_route_for(
            getattr(order_intent, "session", None) or self._session_hint,
            self._env_key(), order_intent.side)
        excg = _order_excg_for(order_intent.exchange)
        payload = {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": excg, "PDNO": instrument.kis_symbol,
            "ORD_QTY": str(order_intent.quantity), "OVRS_ORD_UNPR": str(order_intent.limit_price),
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
        }
        current = self._now()
        # HIGH-2: paced, but NEVER retried -- a rate-limited order may or
        # may not have reached KIS, and re-sending could double it.
        (self._limiter or kis_rate_limiter.get_limiter()).wait(
            category=kis_rate_limiter.CATEGORY_ORDER)
        try:
            response = self.session.request(
                "POST", f"{self.config.base_url}{path}", headers=self._auth_headers(tr_id),
                json=payload, timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            raise KISAmbiguousResponseError(
                f"KIS order submission ambiguous (network error, order may or may not have "
                f"reached KIS): {redact_text(str(exc))}"
            ) from exc
        if response.status_code >= 500 or response.status_code in (408, 425, 429):
            raise KISAmbiguousResponseError(
                f"KIS order submission ambiguous: HTTP {response.status_code} {redact_text(response.text)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise KISAmbiguousResponseError(
                f"KIS order response not JSON (ambiguous): {redact_text(str(exc))}"
            ) from exc
        rt_cd = body.get("rt_cd")
        output = body.get("output") or {}
        broker_order_id = output.get("ODNO")
        if kis_rate_limiter.is_rate_limited(body):
            # ORACLE-HIGH-03: a rate-limited ORDER is AMBIGUOUS, not a
            # confirmed rejection. EGW00201 says the gateway shed load; it
            # does not say the order never reached the matching engine.
            # Recording REJECTED would durably assert something KIS never
            # confirmed, and the order would then be invisible to
            # reconciliation. It is never auto-retried either -- the
            # caller must reconcile against KIS's own order history.
            raise KISAmbiguousResponseError(
                "KIS order submission ambiguous: rate limited "
                f"({kis_rate_limiter.RATE_LIMIT_MSG_CD}); the order may or may not have "
                "been accepted -- manual reconciliation required"
            )
        if rt_cd != "0" or not broker_order_id:
            return ExecutionRecord(
                internal_order_id=order_intent.internal_order_id, broker="kis",
                broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
                requested_price=order_intent.limit_price, filled_quantity=0.0,
                average_fill_price=None, status="REJECTED", submitted_at=current, updated_at=current,
                error_code=body.get("msg_cd"), error_message=redact_text(body.get("msg1")),
            )
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=current, updated_at=current,
        )

    def cancel_order(self, order_intent: OrderIntent, instrument, broker_order_id: str, *,
                     authorization=None, bootstrap_capability=None) -> ExecutionRecord:
        """CODEX-043: `authorization` MUST be a currently-valid
        AuthorizedExecution minted by `execution.authorization.
        authorize_cancel()` (which does NOT check HALT -- cancels of an
        existing unfilled order remain allowed during HALT by explicit
        policy -- but does run order_gate.evaluate_cancel_gate())."""
        from execution import authorization as _authz
        _authz.consume(authorization, order_intent, expected_action="cancel")
        self.config.validate_live_order_allowed(
            bootstrap_capability=bootstrap_capability, order_intent=order_intent)
        # The cancel follows the ORDER's session, exactly as the submit
        # follows it -- see `cancel_route_for`. Falling back to the
        # broker's hint here would send a daytime cancel to the regular
        # endpoint and leave the resting order live.
        cancel_path, tr_id = cancel_route_for(
            getattr(order_intent, "session", None) or self._session_hint,
            self._env_key())
        excg = _order_excg_for(order_intent.exchange)
        payload = {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": excg, "PDNO": instrument.kis_symbol, "ORGN_ODNO": broker_order_id,
            # RVSE_CNCL_DVSN_CD=02 (취소): OVRS_ORD_UNPR must be "0" per the
            # reference repo's order_rvsecncl.py docstring ("취소주문 시,
            # '0' 입력") -- passing the actual limit price here was a bug.
            "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(order_intent.quantity),
            "OVRS_ORD_UNPR": "0", "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
        }
        current = self._now()
        # HIGH-2: paced, never retried -- same ambiguity as an order.
        (self._limiter or kis_rate_limiter.get_limiter()).wait(
            category=kis_rate_limiter.CATEGORY_CANCEL)
        try:
            response = self.session.request(
                "POST", f"{self.config.base_url}{cancel_path}", headers=self._auth_headers(tr_id),
                json=payload, timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            raise KISAmbiguousResponseError(
                f"KIS cancel ambiguous (network error): {redact_text(str(exc))}"
            ) from exc
        if response.status_code >= 500 or response.status_code in (408, 425, 429):
            raise KISAmbiguousResponseError(f"KIS cancel ambiguous: HTTP {response.status_code} {redact_text(response.text)}")
        try:
            body = response.json()
        except ValueError as exc:
            raise KISAmbiguousResponseError(
                f"KIS cancel response not JSON (ambiguous): {redact_text(str(exc))}"
            ) from exc
        if kis_rate_limiter.is_rate_limited(body):
            # ORACLE-HIGH-03: same ambiguity on the cancel side. A cancel
            # KIS may have accepted must not be recorded as refused.
            raise KISAmbiguousResponseError(
                "KIS cancel ambiguous: rate limited "
                f"({kis_rate_limiter.RATE_LIMIT_MSG_CD}); the cancel may or may not have "
                "been accepted -- manual reconciliation required"
            )
        rt_cd = body.get("rt_cd")
        status = "CANCELLED" if rt_cd == "0" else "REJECTED"
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0, average_fill_price=None,
            status=status, submitted_at=current, updated_at=current,
            error_code=None if rt_cd == "0" else body.get("msg_cd"),
            error_message=None if rt_cd == "0" else redact_text(body.get("msg1")),
        )
