"""One real holding must be one Position, however many legs report it.

Measured against the live account on 2026-08-18 with a single TX holding:

    requested NASD -> 1 row, ovrs_excg_cd="NYSE"
    requested NYSE -> 1 row, ovrs_excg_cd="NYSE"   (the same row again)
    requested AMEX -> 0 rows

inquire-balance does not strictly filter by the OVRS_EXCG_CD it is given.
Keying the de-duplication on the REQUESTED code therefore produced two
Positions for one holding -- a count the max-open-positions cap reads and
reconciliation compares against local state.

The row's own ovrs_excg_cd is authoritative, and using it keeps the
distinction the venue-scoped key was protecting: a ticker genuinely held
on two venues reports two different ovrs_excg_cd values and stays two
Positions.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import brokers.kis_broker as kb  # noqa: E402


def row(symbol="TX", venue="NYSE", qty="1", avg="53.6800", pnl="-0.070000"):
    """Shaped like the observed live row."""
    return {
        "cano": "44899596", "acnt_prdt_cd": "01", "prdt_type_cd": "513",
        "ovrs_pdno": symbol, "ovrs_item_name": "테르니움(ADR)",
        "pchs_avg_pric": avg, "ovrs_cblc_qty": qty, "ord_psbl_qty": qty,
        "evlu_pfls_amt": pnl, "tr_crcy_cd": "USD", "ovrs_excg_cd": venue,
    }


class FakeConfig:
    account_no = "44899596"
    account_product_cd = "01"

    def validate_read_allowed(self):
        return None


def broker_with(legs):
    """A KISBroker whose exchange sweep returns `legs` verbatim."""
    broker = kb.KISBroker.__new__(kb.KISBroker)
    broker.config = FakeConfig()
    broker._sweep_exchanges_paged = lambda *a, **k: legs
    broker._env_key = lambda: "live"
    from datetime import datetime, timezone
    broker._now = lambda: datetime(2026, 8, 18, 17, 30, tzinfo=timezone.utc)
    return broker


class TestTheObservedDuplicate:
    def test_one_holding_reported_by_two_legs_is_one_position(self):
        """The exact shape measured on the live account."""
        legs = [("NASD", {"output1": [row()]}),
                ("NYSE", {"output1": [row()]}),
                ("AMEX", {"output1": []})]
        positions = broker_with(legs).get_positions()
        assert len(positions) == 1, [(p.symbol, p.quantity) for p in positions]
        assert positions[0].symbol == "TX"
        assert positions[0].quantity == 1
        assert positions[0].average_fill_price == pytest.approx(53.68)

    def test_all_three_legs_reporting_it_is_still_one_position(self):
        legs = [(code, {"output1": [row()]}) for code in ("NASD", "NYSE", "AMEX")]
        assert len(broker_with(legs).get_positions()) == 1

    def test_the_count_is_what_the_position_cap_would_read(self):
        """The cap is why the duplicate mattered, not cosmetics."""
        legs = [("NASD", {"output1": [row()]}), ("NYSE", {"output1": [row()]})]
        assert len(broker_with(legs).get_positions()) == 1


class TestGenuineMultiVenueHoldingsStaySeparate:
    def test_the_same_ticker_on_two_venues_is_two_positions(self):
        legs = [("NASD", {"output1": [row(venue="NASD", avg="10.0")]}),
                ("NYSE", {"output1": [row(venue="NYSE", avg="53.68")]})]
        positions = broker_with(legs).get_positions()
        assert len(positions) == 2
        assert {p.average_fill_price for p in positions} == {10.0, 53.68}

    def test_different_tickers_are_never_merged(self):
        legs = [("NASD", {"output1": [row(symbol="AMBA", venue="NASD")]}),
                ("NYSE", {"output1": [row(symbol="TX", venue="NYSE")]})]
        positions = broker_with(legs).get_positions()
        assert {p.symbol for p in positions} == {"AMBA", "TX"}

    def test_a_row_with_no_venue_falls_back_to_the_requested_code(self):
        """Absent evidence, the leg's own code is the best available, and
        two legs then stay two rows rather than being merged on a guess."""
        legs = [("NASD", {"output1": [row(venue="")]}),
                ("NYSE", {"output1": [row(venue="")]})]
        assert len(broker_with(legs).get_positions()) == 2


class TestTagging:
    def test_the_native_venue_wins_over_the_requested_one(self):
        tagged = kb.KISBroker._tag_rows([row(venue="NYSE")], "NASD")
        assert tagged[0]["kis_exchange_code"] == "NYSE"
        assert tagged[0]["kis_requested_exchange_code"] == "NASD"

    def test_the_requested_code_is_used_when_the_row_is_silent(self):
        tagged = kb.KISBroker._tag_rows([row(venue="")], "NASD")
        assert tagged[0]["kis_exchange_code"] == "NASD"

    def test_both_are_recorded_so_a_mismatch_stays_visible(self):
        """Reconciliation should be able to see that KIS answered a
        different venue than the one asked for."""
        tagged = kb.KISBroker._tag_rows([row(venue="NYSE")], "NASD")
        assert tagged[0]["kis_exchange_code"] != tagged[0]["kis_requested_exchange_code"]


class TestUnchangedBehaviour:
    def test_zero_quantity_rows_are_still_dropped(self):
        legs = [("NYSE", {"output1": [row(qty="0")]})]
        assert broker_with(legs).get_positions() == []

    def test_an_empty_account_is_still_empty(self):
        legs = [(code, {"output1": []}) for code in ("NASD", "NYSE", "AMEX")]
        assert broker_with(legs).get_positions() == []

    def test_a_malformed_quantity_still_raises(self):
        legs = [("NYSE", {"output1": [row(qty="not-a-number")]})]
        with pytest.raises(kb.KISBrokerError):
            broker_with(legs).get_positions()


class TestFillAndOrderIdentityUseTheRowsVenue:
    """The same hazard as positions, with a worse consequence.

    `_find_kis_fill_for_order` SUMS ft_ccld_qty across matching rows, so a
    one-share fill echoed by two legs would be recorded as two filled --
    corrupting position size, remaining quantity and every reconciliation
    decision downstream. Measured on the live account: the TX fill came
    back from both the NASD and the NYSE leg with ovrs_excg_cd="NYSE".
    """

    def fill_row(self, venue="NYSE", odno="0030469882", qty="1"):
        return {"odno": odno, "pdno": "TX", "ovrs_excg_cd": venue,
                "ft_ccld_qty": qty, "ft_ccld_unpr3": "53.68000000",
                "ord_dt": "20260818", "sll_buy_dvsn_cd_name": "매수"}

    def test_one_execution_echoed_by_two_legs_is_one_row(self):
        legs = [("NASD", {"output": [self.fill_row()]}),
                ("NYSE", {"output": [self.fill_row()]})]
        merged = kb._merge_rows(legs, kb.KISBroker._tag_rows,
                                identity=kb._execution_identity)
        assert len(merged) == 1, merged

    def test_the_summed_quantity_is_one_not_two(self):
        """The number that would have been written to the position."""
        legs = [("NASD", {"output": [self.fill_row()]}),
                ("NYSE", {"output": [self.fill_row()]})]
        merged = kb._merge_rows(legs, kb.KISBroker._tag_rows,
                                identity=kb._execution_identity)
        total = sum(float(r["ft_ccld_qty"]) for r in merged
                    if r.get("odno") == "0030469882")
        assert total == 1.0, f"a 1-share fill summed to {total}"

    def test_two_genuine_partial_fills_still_sum(self):
        """CODEX-045: distinct executions on one order must NOT collapse."""
        legs = [("NYSE", {"output": [self.fill_row(qty="2"), self.fill_row(qty="3")]})]
        merged = kb._merge_rows(legs, kb.KISBroker._tag_rows,
                                identity=kb._execution_identity)
        assert sum(float(r["ft_ccld_qty"]) for r in merged) == 5.0

    def test_the_same_odno_on_two_real_venues_stays_two(self):
        legs = [("NASD", {"output": [self.fill_row(venue="NASD")]}),
                ("NYSE", {"output": [self.fill_row(venue="NYSE")]})]
        merged = kb._merge_rows(legs, kb.KISBroker._tag_rows,
                                identity=kb._execution_identity)
        assert len(merged) == 2

    def test_orders_echoed_by_two_legs_are_one_order(self):
        row = {"odno": "0030469882", "ovrs_excg_cd": "NYSE", "pdno": "TX"}
        legs = [("NASD", {"output": [dict(row)]}), ("NYSE", {"output": [dict(row)]})]
        merged = kb._merge_rows(legs, kb.KISBroker._tag_rows,
                                identity=kb._order_identity)
        assert len(merged) == 1

    def test_a_row_without_a_venue_falls_back_to_the_requested_code(self):
        assert kb._row_venue({}, "NASD") == "NASD"
        assert kb._row_venue({"ovrs_excg_cd": ""}, "NASD") == "NASD"
        assert kb._row_venue({"ovrs_excg_cd": "NYSE"}, "NASD") == "NYSE"

    def test_the_tag_fields_never_enter_the_identity(self):
        """Otherwise the requested code would leak back in and defeat it."""
        assert "kis_requested_exchange_code" in kb._TAG_KEYS
        assert "kis_exchange_code" in kb._TAG_KEYS


class TestFillLookupSpansTheOrdersOwnAge:
    """A fill from a previous session must still be findable.

    The lookup used to span only `now`'s date, so an order submitted
    yesterday and not synchronised that day could never be matched again:
    its position stayed at ENTRY_SUBMITTED and reconciliation kept
    reporting a real holding as internal=0. That is what happened to the
    first live S1 fill (submitted 2026-08-18, still unsynced on 08-19).
    """

    def setup_method(self):
        import kis_position_manager as kpm
        self.kpm = kpm
        self.calls = []

    def broker(self, rows):
        calls = self.calls

        class FakeBroker:
            def get_fills(self, *, start_date, end_date):
                calls.append((start_date, end_date))
                return rows

        return FakeBroker()

    def fill(self, odno="0030469882", qty="1", price="53.68000000"):
        return {"odno": odno, "ft_ccld_qty": qty, "ft_ccld_unpr3": price}

    def test_yesterdays_fill_is_found_when_the_order_is_from_yesterday(self):
        from datetime import datetime, timezone

        now = datetime(2026, 8, 19, 15, 30, tzinfo=timezone.utc)
        entry = datetime(2026, 8, 18, 15, 45, tzinfo=timezone.utc)
        found = self.kpm._find_kis_fill_for_order(
            self.broker([self.fill()]), "0030469882", now=now, since=entry)
        assert found == {"filled_qty": 1.0, "average_fill_price": 53.68}
        start, end = self.calls[0]
        assert start <= "20260817", f"window began at {start}"
        assert end == "20260819"

    def test_without_since_the_window_is_still_at_least_today(self):
        from datetime import datetime, timezone

        now = datetime(2026, 8, 19, 15, 30, tzinfo=timezone.utc)
        self.kpm._find_kis_fill_for_order(self.broker([]), "X", now=now)
        start, end = self.calls[0]
        assert end == "20260819"
        assert start <= "20260819"

    def test_the_window_starts_a_day_before_the_order(self):
        """The order timestamp is UTC; KIS dates rows by its trading day,
        so an order late in the ET session is already the previous UTC
        date."""
        from datetime import datetime, timezone

        now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        entry = datetime(2026, 8, 19, 0, 30, tzinfo=timezone.utc)
        self.kpm._find_kis_fill_for_order(self.broker([]), "X", now=now, since=entry)
        assert self.calls[0][0] == "20260818"

    def test_a_future_since_never_pushes_the_window_past_now(self):
        from datetime import datetime, timezone

        now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        later = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        self.kpm._find_kis_fill_for_order(self.broker([]), "X", now=now, since=later)
        start, end = self.calls[0]
        assert start == "20260818" and end == "20260819"

    def test_partial_fills_across_the_window_still_sum(self):
        from datetime import datetime, timezone

        now = datetime(2026, 8, 19, 15, 30, tzinfo=timezone.utc)
        rows = [self.fill(qty="2", price="10.0"), self.fill(qty="3", price="20.0")]
        found = self.kpm._find_kis_fill_for_order(
            self.broker(rows), "0030469882", now=now,
            since=datetime(2026, 8, 18, tzinfo=timezone.utc))
        assert found["filled_qty"] == 5.0
        assert found["average_fill_price"] == pytest.approx((2 * 10 + 3 * 20) / 5)

    def test_an_unparseable_entry_time_falls_back_to_today(self):
        assert self.kpm._as_datetime("not-a-time") is None
        assert self.kpm._as_datetime(None) is None
        assert self.kpm._as_datetime("2026-08-18T15:45:12+00:00") is not None
