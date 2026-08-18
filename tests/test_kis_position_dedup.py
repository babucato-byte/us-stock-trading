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
    broker._sweep_exchanges = lambda *a, **k: legs
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
