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
TR_ID_PSAMOUNT = {"live": "TTTS3007R", "paper": "VTTS3007R"}
# The single field the orderable amount is read from, named once so the
# parser, the verification matrix and the read-only probe cannot disagree.
ORDERABLE_AMOUNT_FIELD = "ord_psbl_frcr_amt"
TR_ID_NCCS = {"live": "TTTS3018R", "paper": "TTTS3018R"}
TR_ID_CCNL = {"live": "TTTS3035R", "paper": "VTTS3035R"}
TR_ID_PRICE = "HHDFS00000300"
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

# Everything ARMED does, it does on top of what OBSERVE does, so an
# OBSERVE requirement is automatically an ARMED requirement too.
_OBSERVE_AND_ARMED = frozenset({REQUIRED_FOR_OBSERVE, REQUIRED_FOR_ARMED})
_ARMED_ONLY = frozenset({REQUIRED_FOR_ARMED})


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


VERIFICATION_MATRIX = (
    WireValueVerification(
        "order_path", ORDER_PATH, REFERENCE_VERIFIED, LIVE_RESPONSE_PENDING,
        "examples_user/overseas_stock/overseas_stock_functions.py::order()",
        required_for=_ARMED_ONLY,
    ),
    WireValueVerification(
        "order_tr_id_live_buy", TR_ID_ORDER_US[("live", "buy")], REFERENCE_VERIFIED,
        LIVE_RESPONSE_PENDING,
        "examples_user/overseas_stock/overseas_stock_functions.py::order()",
        required_for=_ARMED_ONLY,
    ),
    WireValueVerification(
        "cancel_path", CANCEL_PATH, REFERENCE_VERIFIED, LIVE_RESPONSE_PENDING,
        "examples_llm/overseas_stock/order_rvsecncl/order_rvsecncl.py",
        required_for=_ARMED_ONLY,
    ),
    WireValueVerification(
        "cancel_tr_id_live", TR_ID_CANCEL["live"], REFERENCE_VERIFIED, LIVE_RESPONSE_PENDING,
        "examples_llm/overseas_stock/order_rvsecncl/order_rvsecncl.py",
        required_for=_ARMED_ONLY,
    ),
    WireValueVerification(
        "cancel_tr_id_paper", TR_ID_CANCEL["paper"], REFERENCE_VERIFIED, LIVE_RESPONSE_PENDING,
        "examples_llm/overseas_stock/order_rvsecncl/order_rvsecncl.py",
        required_for=_ARMED_ONLY,
    ),
    WireValueVerification(
        "cancel_price_field_rule", "OVRS_ORD_UNPR=0", REFERENCE_VERIFIED,
        LIVE_RESPONSE_PENDING,
        "order_rvsecncl.py docstring: 취소주문 시, '0' 입력",
        required_for=_ARMED_ONLY,
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
_TAG_KEYS = ("kis_exchange_code", "canonical_exchange")


def _order_identity(row, code):
    """An ORDER is identified by its number, scoped to its venue -- two
    venues could in principle issue the same odno."""
    for field in ("odno", "ODNO"):
        value = row.get(field)
        if value:
            return (code, str(value))
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
    return (code, payload)


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
                row.setdefault("kis_exchange_code", code)
                try:
                    row.setdefault(
                        "canonical_exchange", exchange_for_kis_order_code(code).value)
                except UnsupportedExchangeError:  # pragma: no cover
                    pass
            tagged.append(row)
        return tagged

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
                # A symbol lists on exactly one venue; if two legs report
                # the same one, keep the first rather than double-count.
                # Scoped by venue so an identical ticker on two venues is
                # not silently collapsed.
                key = (code, (row or {}).get("ovrs_pdno", ""))
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

    def submit_order(self, order_intent: OrderIntent, instrument, *, authorization=None) -> ExecutionRecord:
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
        self.config.validate_live_order_allowed()
        if order_intent.order_type != "limit":
            raise KISBrokerError("only limit orders are permitted in this pilot")
        tr_id = TR_ID_ORDER_US.get((self._env_key(), order_intent.side))
        if tr_id is None:
            raise KISBrokerError(f"no order TR_ID for env={self._env_key()!r} side={order_intent.side!r}")
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
                "POST", f"{self.config.base_url}{ORDER_PATH}", headers=self._auth_headers(tr_id),
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

    def cancel_order(self, order_intent: OrderIntent, instrument, broker_order_id: str, *, authorization=None) -> ExecutionRecord:
        """CODEX-043: `authorization` MUST be a currently-valid
        AuthorizedExecution minted by `execution.authorization.
        authorize_cancel()` (which does NOT check HALT -- cancels of an
        existing unfilled order remain allowed during HALT by explicit
        policy -- but does run order_gate.evaluate_cancel_gate())."""
        from execution import authorization as _authz
        _authz.consume(authorization, order_intent, expected_action="cancel")
        self.config.validate_live_order_allowed()
        tr_id = TR_ID_CANCEL[self._env_key()]
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
                "POST", f"{self.config.base_url}{CANCEL_PATH}", headers=self._auth_headers(tr_id),
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
