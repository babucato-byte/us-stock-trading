"""Unit/integration tests for brokers/kis_broker.py -- all network calls
go through a fake requests.Session double (never a real HTTP call), per
this project's established pattern (see tests/test_broker_safety.py for
the Alpaca equivalent).
"""
from datetime import datetime, timedelta, timezone

import pytest
import requests

from brokers.kis_broker import (
    KISAmbiguousResponseError,
    KISBroker,
    KISBrokerError,
    KISOrderableCashUnavailableError,
)
from brokers.kis_config import KISConfig, KISConfigError
from domain.account_snapshot import (
    CASH_SOURCE_BALANCE_LACKS_FIELDS,
    CASH_UNAVAILABLE,
    AccountCashUnavailableError,
)
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from execution import authorization as authz

NOW = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)


def _authorize(order_intent, action="order"):
    """This file tests brokers/kis_broker.py's own wire-protocol
    correctness in isolation, not execution/execution_engine.py's
    orchestration (covered separately in tests/test_execution_engine_
    kis.py) -- a trivially-passing gate is enough to mint a valid,
    real AuthorizedExecution token via the same authorize_new_order()/
    authorize_cancel() every real caller must go through."""
    fn = authz.authorize_new_order if action == "order" else authz.authorize_cancel
    return fn(order_intent, lambda: object(), lambda ctx: True, now=NOW)


class _StubResponse:
    def __init__(self, status_code=200, json_body=None, text="ok"):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json_body


class _FakeSession:
    """Routes on (method, path suffix) -> queued response(s). `requests`
    is keyed by (method, url) tuples so tests can assert call counts and
    payload contents."""

    def __init__(self):
        self.responses = {}  # path -> _StubResponse or Exception
        self.requests = []

    def queue(self, path, response):
        self.responses[path] = response

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        for path, response in self.responses.items():
            if url.endswith(path):
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"no stubbed response for {method} {url}")


TOKEN_OK = _StubResponse(200, {"access_token": "tok-1", "expires_in": 3600})


class _VenueSession(_FakeSession):
    """ORACLE-HIGH-1: account reads sweep NASD/NYSE/AMEX, and KIS filters
    by venue, so each leg gets its OWN body. Answering all three with one
    body would model an API that does not exist."""

    def __init__(self, path, per_venue):
        super().__init__()
        self.queue("/oauth2/tokenP", TOKEN_OK)
        self._path = path
        self._per_venue = per_venue

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if url.endswith("/oauth2/tokenP"):
            return TOKEN_OK
        if url.endswith(self._path):
            code = (kwargs.get("params") or {}).get("OVRS_EXCG_CD")
            return _StubResponse(200, self._per_venue.get(
                code, {"rt_cd": "0", "output": [], "output1": [], "output2": {}}))
        raise AssertionError(f"no stubbed response for {method} {url}")



def _config(**overrides):
    kwargs = dict(
        kis_env="paper", app_key="key", app_secret="secret", account_no="12345678",
        account_product_cd="01", account_read_enabled=True, live_order_enabled=False,
    )
    kwargs.update(overrides)
    return KISConfig(**kwargs)


def _broker(config=None, session=None, now=NOW):
    return KISBroker(config=config or _config(), session=session or _FakeSession(), now_fn=lambda: now)


def _instrument(**overrides):
    kwargs = dict(exchange="NASDAQ")
    kwargs.update(overrides)
    return build_instrument("AAPL", **kwargs)


def _order_intent(**overrides):
    kwargs = dict(
        internal_order_id="ord-1", signal_id="sig-1", strategy_id="strat-1",
        symbol="AAPL", exchange="NASDAQ", side="buy", quantity=1, order_type="limit",
        limit_price=100.0, stop_price=None, target_price=None, created_at=NOW,
    )
    kwargs.update(overrides)
    return OrderIntent(**kwargs)


class TestAuth:
    def test_token_issued_and_cached(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-price/v1/quotations/price",
            _StubResponse(200, {"output": {"last": "150.00"}}),
        )
        broker = _broker(session=session)
        broker.get_current_price(_instrument())
        broker.get_current_price(_instrument())
        token_calls = [r for r in session.requests if r[1].endswith("/oauth2/tokenP")]
        assert len(token_calls) == 1  # cached across two reads

    def test_token_refreshed_after_expiry(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", _StubResponse(200, {"access_token": "tok-1", "expires_in": 60}))
        session.queue(
            "/uapi/overseas-price/v1/quotations/price",
            _StubResponse(200, {"output": {"last": "150.00"}}),
        )
        clock = {"t": NOW}
        broker = KISBroker(config=_config(), session=session, now_fn=lambda: clock["t"])
        broker.get_current_price(_instrument())
        clock["t"] = NOW + timedelta(hours=2)
        broker.get_current_price(_instrument())
        token_calls = [r for r in session.requests if r[1].endswith("/oauth2/tokenP")]
        assert len(token_calls) == 2

    def test_missing_credentials_blocks_before_network(self):
        session = _FakeSession()
        broker = _broker(config=_config(app_key=None), session=session)
        with pytest.raises(KISConfigError):
            broker.get_current_price(_instrument())
        assert session.requests == []

    def test_malformed_token_response_raises(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", _StubResponse(200, {"no_token_here": True}))
        broker = _broker(session=session)
        with pytest.raises(KISBrokerError):
            broker.get_current_price(_instrument())

    def test_token_network_error_raises(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", requests.exceptions.ConnectionError("boom"))
        broker = _broker(session=session)
        with pytest.raises(KISBrokerError):
            broker.get_current_price(_instrument())

    def test_token_issuance_http_error_redacts_secrets_in_response_body(self):
        # CODEX-050: KIS's own error response body is embedded verbatim
        # into the exception message -- if it ever echoed back the
        # submitted appkey/appsecret, that must never reach a log unmasked.
        session = _FakeSession()
        session.queue("/oauth2/tokenP", _StubResponse(
            400, {"error": "invalid"},
            text='{"appkey": "ABCDEFG1234", "appsecret": "SECRETVALUE9", "msg": "invalid credentials"}',
        ))
        broker = _broker(session=session)
        with pytest.raises(KISBrokerError) as excinfo:
            broker.get_current_price(_instrument())
        assert "ABCDEFG1234" not in str(excinfo.value)
        assert "SECRETVALUE9" not in str(excinfo.value)


class TestReadGate:
    def test_read_disabled_blocks_before_network(self):
        session = _FakeSession()
        broker = _broker(config=_config(account_read_enabled=False), session=session)
        with pytest.raises(KISConfigError):
            broker.get_current_price(_instrument())
        assert session.requests == []


class TestGetCurrentPrice:
    def test_success(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-price/v1/quotations/price",
            _StubResponse(200, {"output": {"last": "212.34"}}),
        )
        assert _broker(session=session).get_current_price(_instrument()) == pytest.approx(212.34)

    def test_missing_last_field_raises(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue("/uapi/overseas-price/v1/quotations/price", _StubResponse(200, {"output": {}}))
        with pytest.raises(KISBrokerError):
            _broker(session=session).get_current_price(_instrument())

    def test_non_positive_price_raises(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-price/v1/quotations/price",
            _StubResponse(200, {"output": {"last": "0"}}),
        )
        with pytest.raises(KISBrokerError):
            _broker(session=session).get_current_price(_instrument())

    def test_unmapped_exchange_raises_before_network(self):
        session = _FakeSession()
        with pytest.raises(KISBrokerError):
            _broker(session=session).get_current_price(_instrument(exchange="LSE"))
        assert session.requests == []


# ORACLE-CASH-01: the REAL output2 of a live TTTS3012R balance read,
# observed on the Oracle host 2026-08-06 on all three venue legs. Nine
# purchase/valuation/P&L fields and no cash field whatsoever. The old
# fixture invented `frcr_dncl_amt1`/`frcr_use_psbl_amt`, which is why the
# suite passed while a funded live account reported $0.
LIVE_BALANCE_OUTPUT2 = {
    "frcr_pchs_amt1": "0.00000",
    "ovrs_rlzt_pfls_amt": "0.00000",
    "ovrs_tot_pfls": "0.00000",
    "rlzt_erng_rt": "0.00000000",
    "tot_evlu_pfls_amt": "0.00000000",
    "tot_pftrt": "0.00000000",
    "frcr_buy_amt_smtl1": "0.000000",
    "ovrs_rlzt_pfls_amt2": "0.00000",
    "frcr_buy_amt_smtl2": "0.000000",
}


class TestAccountAndPositions:
    def test_get_account_snapshot_reports_cash_unavailable(self):
        """The balance endpoint carries no cash, so the snapshot must say
        UNAVAILABLE -- not $0, which is a number a caller would size on."""
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            _StubResponse(200, {"output1": [], "output2": dict(LIVE_BALANCE_OUTPUT2)}),
        )
        snap = _broker(session=session).get_account_snapshot()
        assert snap.usd_cash is None
        assert snap.usd_orderable_cash is None
        assert snap.krw_cash is None
        assert snap.cash_status == CASH_UNAVAILABLE
        assert snap.cash_source == CASH_SOURCE_BALANCE_LACKS_FIELDS
        assert snap.usd_available_for_new_order is None
        assert snap.source == "kis_balance"

    def test_account_snapshot_refuses_to_hand_out_an_unknown_cash_figure(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            _StubResponse(200, {"output1": [], "output2": dict(LIVE_BALANCE_OUTPUT2)}),
        )
        snap = _broker(session=session).get_account_snapshot()
        with pytest.raises(AccountCashUnavailableError):
            snap.require_usd_available_for_new_order()

    def test_a_cash_field_that_IS_present_is_still_honoured(self):
        """Not a rejection of cash fields as such: if KIS ever does return
        one, an explicit number is used -- including an explicit 0."""
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        payload = dict(LIVE_BALANCE_OUTPUT2)
        payload.update({"frcr_dncl_amt1": "1000.50", "frcr_use_psbl_amt": "0"})
        session.queue(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            _StubResponse(200, {"output1": [], "output2": payload}),
        )
        snap = _broker(session=session).get_account_snapshot()
        assert snap.usd_cash == pytest.approx(1000.50)
        assert snap.usd_orderable_cash == 0.0
        assert snap.cash_status == "AVAILABLE"
        assert snap.usd_available_for_new_order == 0.0

    def test_get_positions_filters_zero_quantity(self):
        session = _VenueSession("/uapi/overseas-stock/v1/trading/inquire-balance", {
            "NASD": {"rt_cd": "0", "output1": [
                {"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "2", "pchs_avg_pric": "150.0",
                 "evlu_pfls_amt": "10.0"},
                {"ovrs_pdno": "MSFT", "ovrs_cblc_qty": "0", "pchs_avg_pric": "0",
                 "evlu_pfls_amt": "0"},
            ], "output2": {}},
        })
        positions = _broker(session=session).get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"
        assert positions[0].quantity == 2

    def test_get_orderable_usd(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            _StubResponse(200, {"output": {"ovrs_ord_psbl_amt": "543.21",
                                           "ord_psbl_frcr_amt": "20.96",
                                           "sll_ruse_psbl_amt": "522.25"}}),
        )
        amount = _broker(session=session).get_orderable_usd(_instrument(), 100.0)
        assert amount == pytest.approx(543.21)

    def test_get_orderable_usd_uses_4letter_order_exchange_code(self):
        # Regression: was previously "NASS" (a botched _excd_for()+"S"),
        # must be "NASD".
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            _StubResponse(200, {"output": {"ovrs_ord_psbl_amt": "543.21",
                                           "ord_psbl_frcr_amt": "20.96",
                                           "sll_ruse_psbl_amt": "522.25"}}),
        )
        _broker(session=session).get_orderable_usd(_instrument(), 100.0)
        call = next(r for r in session.requests if r[1].endswith("/inquire-psamount"))
        assert call[2]["params"]["OVRS_EXCG_CD"] == "NASD"

    def test_get_open_orders(self):
        """ORACLE-HIGH-01: an account read sweeps every supported venue.
        The same stub answers all three legs, so the identical order must
        be deduplicated by broker order id rather than counted three
        times."""
        session = _VenueSession("/uapi/overseas-stock/v1/trading/inquire-nccs", {
            "NASD": {"rt_cd": "0", "output": [{"odno": "1"}]},
        })
        orders = _broker(session=session).get_open_orders()
        assert len(orders) == 1
        assert orders[0]["odno"] == "1"
        assert orders[0]["kis_exchange_code"] == "NASD"
        sent = [kw["params"]["OVRS_EXCG_CD"] for _m, url, kw in session.requests
                if "inquire-nccs" in url]
        assert sent == ["NASD", "NYSE", "AMEX"], sent

    def test_get_fills(self):
        session = _VenueSession("/uapi/overseas-stock/v1/trading/inquire-ccnl", {
            "NASD": {"rt_cd": "0", "output": [{"odno": "2"}]},
        })
        fills = _broker(session=session).get_fills(
            start_date="20260701", end_date="20260729")
        assert len(fills) == 1
        assert fills[0]["odno"] == "2"
        sent = [kw["params"]["OVRS_EXCG_CD"] for _m, url, kw in session.requests
                if "inquire-ccnl" in url]
        assert sent == ["NASD", "NYSE", "AMEX"], sent


class TestSubmitOrderGate:
    def test_blocked_when_live_order_disabled(self):
        session = _FakeSession()
        broker = _broker(config=_config(live_order_enabled=False), session=session)
        with pytest.raises(KISConfigError):
            oi = _order_intent()
            broker.submit_order(oi, _instrument(), authorization=_authorize(oi))
        assert session.requests == []

    def test_missing_authorization_blocked_zero_network_calls(self):
        # CODEX-043: submit_order() without an authorization= at all
        # (e.g. a caller bypassing execution/execution_engine.py
        # entirely) must be blocked before token issuance is even
        # attempted -- session.queue() below is never even reached.
        session = _FakeSession()
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        with pytest.raises(authz.UnauthorizedExecutionError):
            broker.submit_order(_order_intent(), _instrument())
        assert session.requests == []

    def test_hand_built_authorization_blocked_zero_network_calls(self):
        # Not "protection by underscore": a hand-constructed
        # AuthorizedExecution (never minted via authorize_new_order())
        # still fails, since its token was never registered.
        session = _FakeSession()
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        oi = _order_intent()
        fake = authz.AuthorizedExecution(
            internal_order_id=oi.internal_order_id, side=oi.side, action="order",
            token="made-up", authorized_at=NOW,
        )
        with pytest.raises(authz.UnauthorizedExecutionError):
            broker.submit_order(oi, _instrument(), authorization=fake)
        assert session.requests == []

    def test_market_order_rejected_before_network(self):
        # OrderIntent itself already rejects order_type != "limit" at
        # construction -- this proves the broker layer has its own
        # independent belt-and-suspenders check by constructing an
        # OrderIntent and monkeypatching order_type after the fact is not
        # possible (frozen dataclass validated at construction), so this
        # test instead confirms construction itself fails closed.
        with pytest.raises(Exception):
            _order_intent(order_type="market")


class TestSubmitOrderSuccessAndFailure:
    def test_success_returns_accepted(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/order",
            _StubResponse(200, {"rt_cd": "0", "output": {"ODNO": "kis-999"}}),
        )
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        oi = _order_intent()
        record = broker.submit_order(oi, _instrument(), authorization=_authorize(oi))
        assert record.status == "ACCEPTED"
        assert record.broker_order_id == "kis-999"
        assert record.broker == "kis"

    def test_order_uses_4letter_order_exchange_code_not_quote_code(self):
        # Regression: OVRS_EXCG_CD on the order endpoint must be "NASD"
        # (4-letter, order-API code space), never "NAS" (3-letter,
        # quote-API EXCD code space) -- caught by comparing against the
        # official reference repo.
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/order",
            _StubResponse(200, {"rt_cd": "0", "output": {"ODNO": "kis-999"}}),
        )
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        oi = _order_intent()
        broker.submit_order(oi, _instrument(), authorization=_authorize(oi))
        order_call = next(r for r in session.requests if r[1].endswith("/trading/order"))
        assert order_call[2]["json"]["OVRS_EXCG_CD"] == "NASD"

    def test_rejected_rt_cd_returns_rejected_status(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/order",
            _StubResponse(200, {"rt_cd": "1", "msg_cd": "E001", "msg1": "insufficient funds", "output": {}}),
        )
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        oi = _order_intent()
        record = broker.submit_order(oi, _instrument(), authorization=_authorize(oi))
        assert record.status == "REJECTED"
        assert record.error_code == "E001"

    def test_network_error_raises_ambiguous_not_rejected(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue("/uapi/overseas-stock/v1/trading/order", requests.exceptions.Timeout("boom"))
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        with pytest.raises(KISAmbiguousResponseError):
            oi = _order_intent()
            broker.submit_order(oi, _instrument(), authorization=_authorize(oi))

    @pytest.mark.parametrize("status_code", [500, 502, 503, 504, 408, 429])
    def test_5xx_and_ambiguous_statuses_raise_ambiguous(self, status_code):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/order",
            _StubResponse(status_code, {}, text="server error"),
        )
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        with pytest.raises(KISAmbiguousResponseError):
            oi = _order_intent()
            broker.submit_order(oi, _instrument(), authorization=_authorize(oi))

    def test_malformed_json_raises_ambiguous(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)

        class _BadJSON(_StubResponse):
            def json(self):
                raise ValueError("not json")

        session.queue("/uapi/overseas-stock/v1/trading/order", _BadJSON(200))
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        with pytest.raises(KISAmbiguousResponseError):
            oi = _order_intent()
            broker.submit_order(oi, _instrument(), authorization=_authorize(oi))


class TestCancelOrder:
    def test_blocked_when_live_order_disabled(self):
        session = _FakeSession()
        broker = _broker(config=_config(live_order_enabled=False), session=session)
        with pytest.raises(KISConfigError):
            oi = _order_intent()
            broker.cancel_order(oi, _instrument(), "kis-999", authorization=_authorize(oi, action="cancel"))
        assert session.requests == []

    def test_success_returns_cancelled(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/order-rvsecncl",
            _StubResponse(200, {"rt_cd": "0", "output": {}}),
        )
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        oi = _order_intent()
        record = broker.cancel_order(oi, _instrument(), "kis-999", authorization=_authorize(oi, action="cancel"))
        assert record.status == "CANCELLED"

    def test_cancel_payload_sends_zero_price_and_4letter_exchange_code(self):
        # Regression: cancel must send OVRS_ORD_UNPR="0" (per the
        # reference repo's order_rvsecncl.py docstring), not the order's
        # actual limit price; and OVRS_EXCG_CD must be "NASD", not "NAS".
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue(
            "/uapi/overseas-stock/v1/trading/order-rvsecncl",
            _StubResponse(200, {"rt_cd": "0", "output": {}}),
        )
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        oi = _order_intent(limit_price=123.45)
        broker.cancel_order(oi, _instrument(), "kis-999", authorization=_authorize(oi, action="cancel"))
        call = next(r for r in session.requests if r[1].endswith("/order-rvsecncl"))
        assert call[2]["json"]["OVRS_ORD_UNPR"] == "0"
        assert call[2]["json"]["OVRS_EXCG_CD"] == "NASD"

    def test_network_error_raises_ambiguous(self):
        session = _FakeSession()
        session.queue("/oauth2/tokenP", TOKEN_OK)
        session.queue("/uapi/overseas-stock/v1/trading/order-rvsecncl", requests.exceptions.Timeout("boom"))
        broker = _broker(config=_config(live_order_enabled=True), session=session)
        with pytest.raises(KISAmbiguousResponseError):
            oi = _order_intent()
            broker.cancel_order(oi, _instrument(), "kis-999", authorization=_authorize(oi, action="cancel"))
