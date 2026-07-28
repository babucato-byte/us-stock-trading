"""KISBroker -- the sole live-order broker adapter in this migration.
Strategy/Risk/Sizing/Execution code never sees a KIS TR_ID, exchange
code, or wire-format field name; every public method here takes/returns
`domain/` types (Instrument, OrderIntent, ExecutionRecord, Position,
AccountSnapshot) and does the TR_ID/field mapping internally, mirroring
broker/alpaca_client.py's existing shape (config + injectable session,
never a bare `requests` call scattered through calling code).

TR_ID/endpoint/field values below were verified against the OFFICIAL
KIS Open API GitHub examples (github.com/koreainvestment/open-trading-
api, examples_user/kis_auth.py + examples_user/overseas_stock/
overseas_stock_functions.py + examples_llm/overseas_stock/price/
price.py), not invented. Two items were NOT independently confirmed
from that source and are flagged inline as TBD_VERIFY_LIVE_DOCS:
general (non-daytime) order cancel TR_ID, and the exact response field
name for last-traded price. Confirm both against the live KIS Open API
portal (apiportal.koreainvestment.com) before KIS_LIVE_ORDER_ENABLED is
ever set true.

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
from typing import List, Optional

import requests

from brokers.kis_config import KISConfig, KISConfigError
from domain.account_snapshot import AccountSnapshot
from domain.execution_event import ExecutionRecord
from domain.order_intent import OrderIntent
from domain.position import Position

TOKEN_PATH = "/oauth2/tokenP"
PRICE_PATH = "/uapi/overseas-price/v1/quotations/price"
BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
PSAMOUNT_PATH = "/uapi/overseas-stock/v1/trading/inquire-psamount"
ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
# TBD_VERIFY_LIVE_DOCS: general cancel path/TR_ID -- only the *daytime*
# variant (daytime-order-rvsecncl / TTTS6038U) was directly confirmed
# from the official examples during this implementation. This path
# follows the same "-rvsecncl" naming convention KIS uses elsewhere in
# that repo, but confirm the exact TR_ID pair against the live docs
# before enabling KIS_LIVE_ORDER_ENABLED.
CANCEL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
NCCS_PATH = "/uapi/overseas-stock/v1/trading/inquire-nccs"
CCNL_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"

TR_ID_BALANCE = {"live": "TTTS3012R", "paper": "VTTS3012R"}
TR_ID_PSAMOUNT = {"live": "TTTS3007R", "paper": "VTTS3007R"}
TR_ID_NCCS = {"live": "TTTS3018R", "paper": "TTTS3018R"}
TR_ID_CCNL = {"live": "TTTS3035R", "paper": "VTTS3035R"}
TR_ID_PRICE = "HHDFS00000300"
# US-market order TR_IDs, verified from examples_user/overseas_stock/
# overseas_stock_functions.py's order() function.
TR_ID_ORDER_US = {
    ("live", "buy"): "TTTT1002U", ("paper", "buy"): "VTTT1002U",
    ("live", "sell"): "TTTT1006U", ("paper", "sell"): "VTTT1001U",
}
# TBD_VERIFY_LIVE_DOCS: reusing the daytime cancel TR_ID pair as the
# general-order cancel TR_ID -- confirm against live docs.
TR_ID_CANCEL = {"live": "TTTS6038U", "paper": "TTTS6038U"}

_EXCHANGE_TO_EXCD = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


class KISBrokerError(Exception):
    """Raised on any KIS adapter failure -- auth, network, or a
    non-success KIS response body. Callers must treat this as a hard
    block; there is no fallback broker (spec §2: "장애 시 Alpaca나 다른
    증권사로 자동 우회 주문하지 않는다")."""


class KISAmbiguousResponseError(KISBrokerError):
    """Raised specifically for timeouts/connection errors/non-parseable
    responses to a state-mutating call (order/cancel) -- the caller
    (execution/order_state_machine.py) must treat this as UNKNOWN, never
    as a definite failure eligible for retry (spec §9)."""


def _excd_for(exchange: str) -> str:
    excd = _EXCHANGE_TO_EXCD.get(exchange)
    if excd is None:
        raise KISBrokerError(f"no KIS exchange code mapping for exchange={exchange!r}")
    return excd


class KISBroker:
    def __init__(self, config: Optional[KISConfig] = None, session=None, now_fn=None):
        self.config = config or KISConfig()
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
            raise KISBrokerError(f"KIS token issuance failed (network): {exc}") from exc
        if response.status_code != 200:
            raise KISBrokerError(f"KIS token issuance failed: HTTP {response.status_code} {response.text}")
        try:
            body = response.json()
            token = body["access_token"]
            expires_in = int(body.get("expires_in", 0))
        except (ValueError, KeyError, TypeError) as exc:
            raise KISBrokerError(f"KIS token response malformed: {exc}") from exc
        self._access_token = token
        # Refresh 60s early so a call started right at expiry never races
        # a mid-flight 401.
        self._token_expires_at = self._now().timestamp() + max(expires_in - 60, 0)
        self._token_expires_at = datetime.fromtimestamp(self._token_expires_at, tz=timezone.utc)
        return self._access_token

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
        try:
            response = self.session.request(
                "GET", f"{self.config.base_url}{path}", headers=self._auth_headers(tr_id),
                params=params, timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            raise KISBrokerError(f"KIS GET {path} failed (network): {exc}") from exc
        if response.status_code != 200:
            raise KISBrokerError(f"KIS GET {path} failed: HTTP {response.status_code} {response.text}")
        try:
            return response.json()
        except ValueError as exc:
            raise KISBrokerError(f"KIS GET {path} response not JSON: {exc}") from exc

    # -- read-only --------------------------------------------------------

    def get_current_price(self, instrument) -> float:
        """Returns the last-traded USD price for `instrument.kis_symbol`.
        TBD_VERIFY_LIVE_DOCS: response field name -- `last` is used here
        per common KIS documentation convention, but was not directly
        confirmed from the fetched source excerpt during implementation."""
        self.config.validate_read_allowed()
        body = self._get(PRICE_PATH, TR_ID_PRICE, {
            "AUTH": "", "EXCD": _excd_for(instrument.exchange), "SYMB": instrument.kis_symbol,
        })
        output = body.get("output") or {}
        raw_price = output.get("last")
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            raise KISBrokerError(f"KIS price response missing/invalid 'last' field: {output!r}")
        if not math.isfinite(price) or price <= 0:
            raise KISBrokerError(f"KIS price response has a non-positive/non-finite price: {price!r}")
        return price

    def get_account_snapshot(self, *, source_label="kis_balance") -> AccountSnapshot:
        self.config.validate_read_allowed()
        tr_id = TR_ID_BALANCE[self._env_key()]
        body = self._get(BALANCE_PATH, tr_id, {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": "NASD", "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        })
        summary_rows = body.get("output2") or {}
        if isinstance(summary_rows, list):
            summary_rows = summary_rows[0] if summary_rows else {}
        try:
            usd_cash = float(summary_rows.get("frcr_dncl_amt1", 0) or 0)
            usd_orderable_cash = float(summary_rows.get("frcr_use_psbl_amt", usd_cash) or usd_cash)
        except (TypeError, ValueError) as exc:
            raise KISBrokerError(f"KIS balance response has non-numeric cash fields: {exc}") from exc
        return AccountSnapshot(
            krw_cash=0.0, usd_cash=usd_cash, usd_orderable_cash=usd_orderable_cash,
            usd_reserved_in_open_orders=0.0, as_of=self._now(), source=source_label,
            account_id=self.config.account_no or "",
        )

    def get_orderable_usd(self, instrument, limit_price_usd: float) -> float:
        self.config.validate_read_allowed()
        tr_id = TR_ID_PSAMOUNT[self._env_key()]
        body = self._get(PSAMOUNT_PATH, tr_id, {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": _excd_for(instrument.exchange) + "S",
            "OVRS_ORD_UNPR": str(limit_price_usd), "ITEM_CD": instrument.kis_symbol,
        })
        output = body.get("output") or {}
        try:
            return float(output.get("ord_psbl_frcr_amt", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise KISBrokerError(f"KIS orderable-amount response malformed: {exc}") from exc

    def get_positions(self) -> List[Position]:
        self.config.validate_read_allowed()
        tr_id = TR_ID_BALANCE[self._env_key()]
        body = self._get(BALANCE_PATH, tr_id, {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": "NASD", "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        })
        rows = body.get("output1") or []
        current = self._now()
        positions = []
        for row in rows:
            try:
                qty = int(float(row.get("ovrs_cblc_qty", 0) or 0))
                avg_price = float(row.get("pchs_avg_pric", 0) or 0)
                unrealized = float(row.get("evlu_pfls_amt", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise KISBrokerError(f"KIS position row malformed: {row!r}: {exc}") from exc
            if qty <= 0:
                continue
            positions.append(Position(
                symbol=row.get("ovrs_pdno", ""), quantity=qty, average_fill_price=avg_price,
                unrealized_pnl=unrealized, realized_pnl=0.0, as_of=current, source="kis_balance",
            ))
        return positions

    def get_open_orders(self) -> list:
        self.config.validate_read_allowed()
        body = self._get(NCCS_PATH, TR_ID_NCCS[self._env_key()], {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": "NASD", "SORT_SQN": "DS", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        })
        return body.get("output") or []

    def get_fills(self, *, start_date: str, end_date: str) -> list:
        """`start_date`/`end_date` are KIS's own YYYYMMDD format."""
        self.config.validate_read_allowed()
        tr_id = TR_ID_CCNL[self._env_key()]
        body = self._get(CCNL_PATH, tr_id, {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "PDNO": "%", "ORD_STRT_DT": start_date, "ORD_END_DT": end_date,
            "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00", "OVRS_EXCG_CD": "NASD",
            "SORT_SQN": "DS", "CTX_AREA_NK200": "", "CTX_AREA_FK200": "",
        })
        return body.get("output") or []

    # -- order submission ---------------------------------------------

    def submit_order(self, order_intent: OrderIntent, instrument) -> ExecutionRecord:
        """The ONLY method in this codebase that places a real KIS order.
        Runs the fail-closed live-order gate (config.validate_live_order_
        allowed()) before any network call -- a caller cannot bypass it by
        holding a KISBroker instance built before the flag was disabled,
        since this re-reads self.config fresh (frozen dataclass, so a
        caller must have built a *new* KISBroker/KISConfig to flip it,
        matching AlpacaBroker's own credential-rotation-requires-new-
        instance pattern)."""
        self.config.validate_live_order_allowed()
        if order_intent.order_type != "limit":
            raise KISBrokerError("only limit orders are permitted in this pilot")
        tr_id = TR_ID_ORDER_US.get((self._env_key(), order_intent.side))
        if tr_id is None:
            raise KISBrokerError(f"no order TR_ID for env={self._env_key()!r} side={order_intent.side!r}")
        excd = _excd_for(order_intent.exchange)
        payload = {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": excd, "PDNO": instrument.kis_symbol,
            "ORD_QTY": str(order_intent.quantity), "OVRS_ORD_UNPR": str(order_intent.limit_price),
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
        }
        current = self._now()
        try:
            response = self.session.request(
                "POST", f"{self.config.base_url}{ORDER_PATH}", headers=self._auth_headers(tr_id),
                json=payload, timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            raise KISAmbiguousResponseError(
                f"KIS order submission ambiguous (network error, order may or may not have "
                f"reached KIS): {exc}"
            ) from exc
        if response.status_code >= 500 or response.status_code in (408, 425, 429):
            raise KISAmbiguousResponseError(
                f"KIS order submission ambiguous: HTTP {response.status_code} {response.text}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise KISAmbiguousResponseError(f"KIS order response not JSON (ambiguous): {exc}") from exc
        rt_cd = body.get("rt_cd")
        output = body.get("output") or {}
        broker_order_id = output.get("ODNO")
        if rt_cd != "0" or not broker_order_id:
            return ExecutionRecord(
                internal_order_id=order_intent.internal_order_id, broker="kis",
                broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
                requested_price=order_intent.limit_price, filled_quantity=0.0,
                average_fill_price=None, status="REJECTED", submitted_at=current, updated_at=current,
                error_code=body.get("msg_cd"), error_message=body.get("msg1"),
            )
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=current, updated_at=current,
        )

    def cancel_order(self, order_intent: OrderIntent, instrument, broker_order_id: str) -> ExecutionRecord:
        self.config.validate_live_order_allowed()
        tr_id = TR_ID_CANCEL[self._env_key()]
        excd = _excd_for(order_intent.exchange)
        payload = {
            "CANO": self.config.account_no, "ACNT_PRDT_CD": self.config.account_product_cd,
            "OVRS_EXCG_CD": excd, "PDNO": instrument.kis_symbol, "ORGN_ODNO": broker_order_id,
            "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(order_intent.quantity),
            "OVRS_ORD_UNPR": str(order_intent.limit_price), "ORD_SVR_DVSN_CD": "0",
        }
        current = self._now()
        try:
            response = self.session.request(
                "POST", f"{self.config.base_url}{CANCEL_PATH}", headers=self._auth_headers(tr_id),
                json=payload, timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            raise KISAmbiguousResponseError(f"KIS cancel ambiguous (network error): {exc}") from exc
        if response.status_code >= 500 or response.status_code in (408, 425, 429):
            raise KISAmbiguousResponseError(f"KIS cancel ambiguous: HTTP {response.status_code} {response.text}")
        try:
            body = response.json()
        except ValueError as exc:
            raise KISAmbiguousResponseError(f"KIS cancel response not JSON (ambiguous): {exc}") from exc
        rt_cd = body.get("rt_cd")
        status = "CANCELLED" if rt_cd == "0" else "REJECTED"
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0, average_fill_price=None,
            status=status, submitted_at=current, updated_at=current,
            error_code=None if rt_cd == "0" else body.get("msg_cd"),
            error_message=None if rt_cd == "0" else body.get("msg1"),
        )
