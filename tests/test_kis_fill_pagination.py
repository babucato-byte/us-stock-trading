"""The fill history is every page, not the first one.

The defect this closes
----------------------
`get_fills` sent `CTX_AREA_NK200=""` and kept whatever came back, which
is one capped page per exchange leg. KIS pages that endpoint, so a WIDER
window returned FEWER of the rows that mattered -- and reconciliation
widens its window exactly when an order is stuck, which made the
blindness self-reinforcing.

Measured in production on 2026-09-03: SLGN order 0030974162 filled 3 @
41.61 and the account held the shares. Reconciliation's own derived
window (20260902-20260904) returned 30 rows WITHOUT it; a narrower read
(20260903-20260904) returned 23 rows WITH it. The ledger therefore stayed
ACCEPTED, `fill_adoption` refused for want of a FILLED order, and three
real shares sat with no position row and no exit rule attached.

The tests below are the shape of that failure, not a generic paging
exercise: the row that matters is absent from page 1 and present on a
later page.

Scope
-----
`get_fills` ONLY. `get_open_orders`, `get_positions` and
`get_account_snapshot` have the identical single-page defect and are
deliberately left alone in this urgent patch -- one endpoint changed
under an unmanaged live position, with the rest tracked as follow-up.
"""

from datetime import datetime, timezone

import pytest

from brokers.kis_broker import KISBroker, KISBrokerError
from brokers.kis_config import KISConfig

NOW = datetime(2026, 9, 4, 2, 30, tzinfo=timezone.utc)
CCNL = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
TOKEN_BODY = {"access_token": "t", "token_type": "Bearer", "expires_in": 86400}


class _Resp:
    def __init__(self, body, tr_cont="", status_code=200):
        self.status_code = status_code
        self._body = body
        self.headers = {"tr_cont": tr_cont}
        self.text = "ok"

    def json(self):
        return self._body


def _fill(odno, symbol="AAPL", qty="1", price="10.00"):
    return {"odno": odno, "pdno": symbol, "ord_dt": "20260903",
            "sll_buy_dvsn_cd": "02", "ft_ord_qty": qty, "ft_ccld_qty": qty,
            "ft_ccld_unpr3": price, "nccs_qty": "0", "prcs_stat_name": "완료",
            "ovrs_excg_cd": "NYSE"}


def _page(rows, *, fk="", nk=""):
    return {"rt_cd": "0", "output": rows, "ctx_area_fk200": fk, "ctx_area_nk200": nk}


class _PagingSession:
    """Serves a per-venue list of pages and records every request."""

    def __init__(self, pages_by_venue):
        self._pages = pages_by_venue
        self._served = {}
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if url.endswith("/oauth2/tokenP"):
            return _Resp(TOKEN_BODY)
        if url.endswith(CCNL):
            venue = (kwargs.get("params") or {}).get("OVRS_EXCG_CD")
            index = self._served.get(venue, 0)
            self._served[venue] = index + 1
            pages = self._pages.get(venue) or [(_page([]), "")]
            if index >= len(pages):
                raise AssertionError(
                    f"{venue}: asked for page {index + 1}, only {len(pages)} stubbed")
            body, tr_cont = pages[index]
            return _Resp(body, tr_cont=tr_cont)
        raise AssertionError(f"unstubbed {method} {url}")

    def ccnl_requests(self):
        return [kw for _m, url, kw in self.requests if url.endswith(CCNL)]


def _broker(session):
    return KISBroker(
        config=KISConfig(kis_env="paper", app_key="k", app_secret="s",
                         account_no="12345678", account_product_cd="01",
                         account_read_enabled=True, live_order_enabled=False),
        session=session, now_fn=lambda: NOW)


def _quiet(*venues):
    return {v: [(_page([]), "")] for v in venues}


# --- the production failure, reproduced --------------------------------

class TestTheRowOnALaterPageIsReturned:
    def test_a_fill_absent_from_page_one_is_still_found(self):
        """The SLGN shape: the order that matters is not on page 1."""
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [
            (_page([_fill("0030000001"), _fill("0030000002")],
                   fk="FK1", nk="NK1"), "F"),
            (_page([_fill("0030974162", symbol="SLGN", qty="3", price="41.61")]),
             "D"),
        ]
        session = _PagingSession(pages)
        rows = _broker(session).get_fills(start_date="20260902", end_date="20260904")

        found = [r for r in rows if r.get("odno") == "0030974162"]
        assert len(found) == 1
        assert found[0]["pdno"] == "SLGN"
        assert found[0]["ft_ccld_qty"] == "3"
        assert found[0]["ft_ccld_unpr3"] == "41.61"

    def test_every_page_of_every_venue_is_read(self):
        pages = {v: [(_page([_fill(f"{v}-1")], fk="F", nk="N"), "F"),
                     (_page([_fill(f"{v}-2")]), "D")]
                 for v in ("NASD", "NYSE", "AMEX")}
        session = _PagingSession(pages)
        rows = _broker(session).get_fills(start_date="20260902", end_date="20260904")
        assert len(session.ccnl_requests()) == 6
        assert {r["odno"] for r in rows} == {
            f"{v}-{n}" for v in ("NASD", "NYSE", "AMEX") for n in (1, 2)}


# --- the continuation contract -----------------------------------------

class TestTheContinuationContract:
    def _two_pages(self):
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [
            (_page([_fill("A")], fk="FK-ONE", nk="NK-ONE"), "F"),
            (_page([_fill("B")]), "D"),
        ]
        return _PagingSession(pages)

    def test_ctx_area_nk200_is_carried_into_the_next_request(self):
        session = self._two_pages()
        _broker(session).get_fills(start_date="20260902", end_date="20260904")
        nyse = [kw for kw in session.ccnl_requests()
                if kw["params"]["OVRS_EXCG_CD"] == "NYSE"]
        assert nyse[0]["params"]["CTX_AREA_NK200"] == ""
        assert nyse[1]["params"]["CTX_AREA_NK200"] == "NK-ONE"
        assert nyse[1]["params"]["CTX_AREA_FK200"] == "FK-ONE"

    def test_tr_cont_header_is_carried_into_the_next_request(self):
        session = self._two_pages()
        _broker(session).get_fills(start_date="20260902", end_date="20260904")
        nyse = [kw for kw in session.ccnl_requests()
                if kw["params"]["OVRS_EXCG_CD"] == "NYSE"]
        assert nyse[0]["headers"]["tr_cont"] == ""
        assert nyse[1]["headers"]["tr_cont"] == "N"

    @pytest.mark.parametrize("last", ["D", "E", "", "   "])
    def test_a_terminal_continuation_stops_the_read(self, last):
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [(_page([_fill("A")], fk="F", nk="N"), last)]
        session = _PagingSession(pages)
        rows = _broker(session).get_fills(start_date="20260902", end_date="20260904")
        assert [r["odno"] for r in rows] == ["A"]

    def test_more_pages_without_a_cursor_stops_rather_than_guessing(self):
        """"F" with no NK is a page we cannot request. Stopping beats
        inventing a cursor."""
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [(_page([_fill("A")], fk="", nk=""), "F")]
        session = _PagingSession(pages)
        rows = _broker(session).get_fills(start_date="20260902", end_date="20260904")
        assert [r["odno"] for r in rows] == ["A"]

    @pytest.mark.parametrize("more", ["F", "M"])
    def test_both_continuation_markers_are_followed(self, more):
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [(_page([_fill("A")], fk="F", nk="N"), more),
                         (_page([_fill("B")]), "D")]
        rows = _broker(_PagingSession(pages)).get_fills(
            start_date="20260902", end_date="20260904")
        assert {r["odno"] for r in rows} == {"A", "B"}


# --- no duplicates, no silent truncation --------------------------------

class TestNoDuplicatesAndNoSilentTruncation:
    def test_a_row_repeated_across_pages_appears_once(self):
        pages = _quiet("NASD", "AMEX")
        repeated = _fill("0030974162", symbol="SLGN", qty="3", price="41.61")
        pages["NYSE"] = [(_page([repeated], fk="F", nk="N1"), "F"),
                         (_page([dict(repeated)]), "D")]
        rows = _broker(_PagingSession(pages)).get_fills(
            start_date="20260902", end_date="20260904")
        assert len([r for r in rows if r.get("odno") == "0030974162"]) == 1

    def test_partial_fills_sharing_one_odno_are_all_kept(self):
        """Identity is the EXECUTION, not the order number -- a partial
        fill series must survive paging."""
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [
            (_page([_fill("1001", qty="2", price="10.00")], fk="F", nk="N1"), "F"),
            (_page([_fill("1001", qty="3", price="10.50")]), "D"),
        ]
        rows = _broker(_PagingSession(pages)).get_fills(
            start_date="20260902", end_date="20260904")
        assert len([r for r in rows if r.get("odno") == "1001"]) == 2

    def test_a_repeated_cursor_fails_closed(self):
        """A loop must raise, never return the rows gathered so far: a
        truncated history that looks complete is the defect itself."""
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [(_page([_fill("A")], fk="F", nk="SAME"), "F")] * 4
        with pytest.raises(KISBrokerError, match="continuation"):
            _broker(_PagingSession(pages)).get_fills(
                start_date="20260902", end_date="20260904")

    def test_an_endless_continuation_raises_at_the_page_cap(self):
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [(_page([_fill(f"A{i}")], fk="F", nk=f"N{i}"), "F")
                         for i in range(KISBroker._MAX_FILL_PAGES + 2)]
        with pytest.raises(KISBrokerError, match="did not terminate"):
            _broker(_PagingSession(pages)).get_fills(
                start_date="20260902", end_date="20260904")

    def test_a_failing_page_still_aborts_the_whole_sweep(self):
        """A partial account must never look like a complete one -- the
        existing sweep contract, preserved across paging."""
        from brokers.kis_broker import KISAccountSweepError

        class _Failing(_PagingSession):
            def request(self, method, url, **kwargs):
                if url.endswith(CCNL) and (
                        kwargs.get("params") or {}).get("OVRS_EXCG_CD") == "NYSE":
                    return _Resp({"rt_cd": "1", "msg1": "boom"}, status_code=500)
                return super().request(method, url, **kwargs)

        with pytest.raises(KISAccountSweepError):
            _broker(_Failing(_quiet("NASD", "NYSE", "AMEX"))).get_fills(
                start_date="20260902", end_date="20260904")


# --- everything else is unchanged ---------------------------------------

class TestExistingBehaviourIsUnchanged:
    def test_a_single_page_response_behaves_exactly_as_before(self):
        pages = {v: [(_page([_fill(f"{v}-only")]), "D")]
                 for v in ("NASD", "NYSE", "AMEX")}
        session = _PagingSession(pages)
        rows = _broker(session).get_fills(start_date="20260903", end_date="20260903")
        assert len(session.ccnl_requests()) == 3
        assert {r["odno"] for r in rows} == {
            "NASD-only", "NYSE-only", "AMEX-only"}

    def test_the_request_parameters_are_unchanged(self):
        session = _PagingSession(_quiet("NASD", "NYSE", "AMEX"))
        _broker(session).get_fills(start_date="20260902", end_date="20260904")
        params = session.ccnl_requests()[0]["params"]
        assert params["ORD_STRT_DT"] == "20260902"
        assert params["ORD_END_DT"] == "20260904"
        assert params["PDNO"] == "%"
        assert params["SLL_BUY_DVSN"] == "00"
        assert params["CCLD_NCCS_DVSN"] == "00"
        assert params["SORT_SQN"] == "DS"

    def test_venue_tagging_survives_paging(self):
        pages = _quiet("NASD", "AMEX")
        pages["NYSE"] = [(_page([_fill("A")], fk="F", nk="N"), "F"),
                         (_page([_fill("B")]), "D")]
        rows = _broker(_PagingSession(pages)).get_fills(
            start_date="20260902", end_date="20260904")
        assert all("kis_exchange_code" in r for r in rows)

    def test_a_response_without_headers_is_treated_as_a_last_page(self):
        """Existing test doubles supply no headers. They must keep
        working, and must not loop."""
        class _NoHeaders(_PagingSession):
            def request(self, method, url, **kwargs):
                response = super().request(method, url, **kwargs)
                if hasattr(response, "headers"):
                    del response.headers
                return response

        rows = _broker(_NoHeaders(
            {v: [(_page([_fill(f"{v}-1")]), "")] for v in ("NASD", "NYSE", "AMEX")}
        )).get_fills(start_date="20260903", end_date="20260903")
        assert len(rows) == 3


class TestScopeIsOnlyGetFills:
    def test_the_other_account_reads_still_use_the_unpaged_sweep(self):
        """Deliberate: one endpoint changed under an unmanaged position."""
        import inspect

        source = inspect.getsource(KISBroker)
        for method in ("get_open_orders", "get_positions", "get_account_snapshot"):
            body = source.split(f"def {method}")[1].split("\n    def ")[0]
            assert "_sweep_exchanges_paged" not in body, method
        fills = source.split("def get_fills")[1].split("\n    def ")[0]
        assert "_sweep_exchanges_paged" in fills
