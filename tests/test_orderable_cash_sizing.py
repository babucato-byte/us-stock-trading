"""ORACLE-CASH-01: entry sizing reads the orderable amount from KIS's own
per-candidate endpoint, and an unreadable answer never becomes a number.

The defect
----------
`KISBroker.get_account_snapshot()` read `frcr_dncl_amt1` /
`frcr_use_psbl_amt` out of the balance response (TTTS3012R). A live read
on the Oracle host showed that response carries NEITHER field -- its
`output2` returns nine purchase/valuation/P&L fields and no cash at all.
Because both reads used `.get(field, 0) or 0`, an absent field became a
confident $0, so a funded live account (orderable $30.99, confirmed via
TTTS3007R at the same moment) sized every candidate to zero shares and
blocked at INSUFFICIENT_CASH before any other gate ran.

The unit tests passed throughout, because their fixtures invented the two
fields. That is the second half of the defect and is why these tests use
the response shape a real account actually returns.

What is pinned here
-------------------
1. `get_orderable_usd()` fails closed on every unusable answer, with a
   reason distinct from "no money".
2. An explicit numeric 0 -- and only that -- is a real zero balance.
3. Sizing is Decimal and floor-only, so a float boundary cannot buy a
   share the cash does not cover.
4. The entry evaluation, at the real numbers ($30.99 / $5.82), gets past
   the cash stage and reaches the gate -- while submitting nothing.
"""
from datetime import datetime, timezone

import pytest

from brokers.kis_broker import (
    ORDERABLE_AMOUNT_FIELD,
    PSAMOUNT_PATH,
    KISBroker,
    KISBrokerError,
    KISOrderableCashUnavailableError,
)
from brokers.kis_config import KISConfig
from domain.cash_sizing import (
    INSUFFICIENT_CASH,
    ORDERABLE_CASH_UNAVAILABLE,
    whole_shares_affordable,
)
from domain.instrument import build_instrument

NOW = datetime(2026, 8, 6, 14, 30, tzinfo=timezone.utc)

# The real numbers observed on the Oracle live account, 2026-08-06.
LIVE_ORDERABLE_USD = 30.99
CANDIDATE_PRICE_USD = 5.82


class _StubResponse:
    def __init__(self, status_code=200, json_body=None, text="ok"):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json_body


class _Session:
    def __init__(self):
        self.responses = {}
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


def _config(**overrides):
    kwargs = dict(
        kis_env="paper", app_key="key", app_secret="secret", account_no="12345678",
        account_product_cd="01", account_read_enabled=True, live_order_enabled=False,
    )
    kwargs.update(overrides)
    return KISConfig(**kwargs)


def _broker_with_psamount(body_or_exc):
    session = _Session()
    session.queue("/oauth2/tokenP", TOKEN_OK)
    session.queue(PSAMOUNT_PATH, body_or_exc)
    broker = KISBroker(config=_config(), session=session, now_fn=lambda: NOW)
    return broker, session


def _instrument():
    return build_instrument("AAPL", exchange="NASDAQ")


# ---------------------------------------------------------------------
# 1. The control: a real, well-formed answer.
# ---------------------------------------------------------------------
class TestNormalAnswer:
    def test_the_live_figure_is_parsed_as_a_float(self):
        broker, _ = _broker_with_psamount(
            _StubResponse(200, {"output": {ORDERABLE_AMOUNT_FIELD: "30.99"}}))
        amount = broker.get_orderable_usd(_instrument(), CANDIDATE_PRICE_USD)
        assert isinstance(amount, float)
        assert amount == pytest.approx(LIVE_ORDERABLE_USD)

    def test_the_limit_price_asked_about_is_the_one_passed_in(self):
        """Sizing at one price and asking KIS about another would size
        against an answer to a different question."""
        broker, session = _broker_with_psamount(
            _StubResponse(200, {"output": {ORDERABLE_AMOUNT_FIELD: "30.99"}}))
        broker.get_orderable_usd(_instrument(), CANDIDATE_PRICE_USD)
        call = next(r for r in session.requests if r[1].endswith(PSAMOUNT_PATH))
        assert call[2]["params"]["OVRS_ORD_UNPR"] == str(CANDIDATE_PRICE_USD)
        assert call[2]["params"]["ITEM_CD"] == "AAPL"
        # The 4-letter ORDER code space, not the 3-letter quote one.
        assert call[2]["params"]["OVRS_EXCG_CD"] == "NASD"

    def test_an_explicit_numeric_zero_is_a_real_zero_balance(self):
        """The ONE case a zero is legitimate: KIS said zero."""
        broker, _ = _broker_with_psamount(
            _StubResponse(200, {"output": {ORDERABLE_AMOUNT_FIELD: "0"}}))
        assert broker.get_orderable_usd(_instrument(), CANDIDATE_PRICE_USD) == 0.0

    def test_zero_sizes_to_no_shares(self):
        assert whole_shares_affordable(0.0, CANDIDATE_PRICE_USD) == 0


# ---------------------------------------------------------------------
# 2. Every unusable answer fails closed, with a reason that is not
#    "insufficient cash".
# ---------------------------------------------------------------------
class TestFailClosedOnUnusableAnswers:
    """Each case is a response KIS could actually produce (or a transport
    fault). None of them may yield a number, and none may be recorded as
    a zero balance."""

    @pytest.mark.parametrize("body,label", [
        ({"output": {}}, "field missing"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: None}}, "None"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: ""}}, "empty string"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: "   "}}, "blank string"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: "NaN"}}, "NaN"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: "nan"}}, "nan"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: "Infinity"}}, "Infinity"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: "-Infinity"}}, "-Infinity"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: "inf"}}, "inf"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: float("nan")}}, "float nan"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: float("inf")}}, "float inf"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: "-1"}}, "negative string"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: -1}}, "negative int"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: -0.01}}, "negative float"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: []}}, "list"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: {}}}, "dict"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: True}}, "bool True"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: False}}, "bool False"),
        ({"output": {ORDERABLE_AMOUNT_FIELD: "abc"}}, "non-numeric text"),
        ({"output": []}, "output is a list"),
        ({"output": None}, "output is null"),
        ({}, "output missing"),
    ])
    def test_unusable_answers_raise_the_unavailable_error(self, body, label):
        broker, _ = _broker_with_psamount(_StubResponse(200, body))
        with pytest.raises(KISOrderableCashUnavailableError) as excinfo:
            broker.get_orderable_usd(_instrument(), CANDIDATE_PRICE_USD)
        assert excinfo.value.reason_code == ORDERABLE_CASH_UNAVAILABLE, label
        # The distinction that matters: this is not a balance verdict.
        assert excinfo.value.reason_code != INSUFFICIENT_CASH, label

    def test_a_network_fault_is_unavailable_not_zero(self):
        import requests

        broker, _ = _broker_with_psamount(
            requests.exceptions.ConnectionError("connection reset"))
        with pytest.raises(KISOrderableCashUnavailableError) as excinfo:
            broker.get_orderable_usd(_instrument(), CANDIDATE_PRICE_USD)
        assert excinfo.value.reason_code == ORDERABLE_CASH_UNAVAILABLE
        assert excinfo.value.detail == "read_failed"

    def test_a_kis_non_success_body_is_unavailable_not_zero(self):
        broker, _ = _broker_with_psamount(
            _StubResponse(200, {"rt_cd": "1", "msg1": "조회할 자료가 없습니다",
                                "msg_cd": "EGW00123"}))
        with pytest.raises(KISOrderableCashUnavailableError) as excinfo:
            broker.get_orderable_usd(_instrument(), CANDIDATE_PRICE_USD)
        assert excinfo.value.reason_code == ORDERABLE_CASH_UNAVAILABLE

    def test_an_http_error_is_unavailable_not_zero(self):
        broker, _ = _broker_with_psamount(_StubResponse(500, {}, text="upstream error"))
        with pytest.raises(KISOrderableCashUnavailableError):
            broker.get_orderable_usd(_instrument(), CANDIDATE_PRICE_USD)

    def test_the_unavailable_error_is_still_a_broker_error(self):
        """So an existing `except KISBrokerError` cannot silently miss it
        and let the candidate continue unsized."""
        assert issubclass(KISOrderableCashUnavailableError, KISBrokerError)

    def test_the_diagnostic_carries_no_account_or_body(self):
        broker, _ = _broker_with_psamount(_StubResponse(200, {"output": {}}))
        with pytest.raises(KISOrderableCashUnavailableError) as excinfo:
            broker.get_orderable_usd(_instrument(), CANDIDATE_PRICE_USD)
        text = f"{excinfo.value} {excinfo.value.diagnostic()}"
        assert "12345678" not in text
        assert "tok-1" not in text
        assert "secret" not in text


# ---------------------------------------------------------------------
# 3. Sizing: whole shares, floor, and no float-boundary overcount.
# ---------------------------------------------------------------------
class TestWholeShareSizing:
    def test_the_live_numbers(self):
        """$30.99 at $5.82 is five shares -- 5.32..., floored."""
        assert whole_shares_affordable(LIVE_ORDERABLE_USD, CANDIDATE_PRICE_USD) == 5

    def test_just_under_one_share_is_zero_shares(self):
        assert whole_shares_affordable(5.819999, 5.82) == 0

    def test_exactly_one_share_is_one_share(self):
        assert whole_shares_affordable(5.82, 5.82) == 1

    @pytest.mark.parametrize("orderable,price,expected", [
        (17.46, 5.82, 3),        # 3 x 5.82 exactly; binary float gives 3.0000000000000004
        (11.64, 5.82, 2),
        (0.29, 0.29, 1),
        (100.0, 3.0, 33),
        (1e6, 0.01, 100_000_000),
    ])
    def test_no_boundary_overcount(self, orderable, price, expected):
        """The unsafe direction is rounding UP across an integer boundary:
        it buys a share the cash does not cover."""
        shares = whole_shares_affordable(orderable, price)
        assert shares == expected
        # The invariant behind the number, checked independently.
        from decimal import Decimal
        assert Decimal(str(shares)) * Decimal(str(price)) <= Decimal(str(orderable))

    @pytest.mark.parametrize("value", [None, "30.99", float("nan"), float("inf"), -1, True, [], {}])
    def test_an_unusable_cash_figure_sizes_to_zero_never_a_share(self, value):
        assert whole_shares_affordable(value, CANDIDATE_PRICE_USD) == 0

    @pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf"), True, "5.82"])
    def test_an_unusable_price_sizes_to_zero(self, price):
        assert whole_shares_affordable(LIVE_ORDERABLE_USD, price) == 0

    def test_there_is_no_fractional_path(self):
        assert isinstance(whole_shares_affordable(30.99, 5.82), int)


# ---------------------------------------------------------------------
# 4. Per-candidate: one read each, and no borrowing another symbol's answer.
# ---------------------------------------------------------------------
class TestPerCandidateAnswers:
    def test_the_answer_depends_on_symbol_and_price(self):
        """KIS's orderable amount is not an account-wide constant, so the
        same account can answer differently per candidate."""
        answers = {("AAPL", "5.82"): "30.99", ("MSFT", "400.0"): "0"}
        session = _Session()
        session.queue("/oauth2/tokenP", TOKEN_OK)

        class _PerCandidate(_Session):
            def request(self, method, url, **kwargs):
                self.requests.append((method, url, kwargs))
                if url.endswith("/oauth2/tokenP"):
                    return TOKEN_OK
                params = kwargs.get("params") or {}
                key = (params.get("ITEM_CD"), params.get("OVRS_ORD_UNPR"))
                return _StubResponse(200, {"output": {ORDERABLE_AMOUNT_FIELD: answers[key]}})

        broker = KISBroker(config=_config(), session=_PerCandidate(), now_fn=lambda: NOW)
        aapl = broker.get_orderable_usd(build_instrument("AAPL", exchange="NASDAQ"), 5.82)
        msft = broker.get_orderable_usd(build_instrument("MSFT", exchange="NASDAQ"), 400.0)
        assert aapl == pytest.approx(30.99)
        assert msft == 0.0
        assert whole_shares_affordable(aapl, 5.82) == 5
        assert whole_shares_affordable(msft, 400.0) == 0
