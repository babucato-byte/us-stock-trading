"""What the account may actually order with.

The runtime read `ord_psbl_frcr_amt` -- foreign CASH -- and treated it
as buying power. Measured on the live account 2026-08-29, same response,
same instant:

    ord_psbl_frcr_amt   20.96   cash only
    sll_ruse_psbl_amt   33.78   sell proceeds reusable for new orders
    ovrs_ord_psbl_amt   54.44   what may actually be ordered
    max_ord_psbl_qty        1   at a $40 limit price

54.44 is the number the KIS app shows, and `max_ord_psbl_qty` tracks
floor(54.44 / price) exactly from $5 to $60. Reading the cash field
understated buying power by the entire reusable balance: at $40 it
computed zero shares where KIS itself answered one.

I had previously concluded the opposite -- that unsettled sell proceeds
were not buying power on this account -- from reading one extracted
field instead of the whole response. `sll_ruse_psbl_amt` was sitting
beside it the entire time.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_broker as kb  # noqa: E402
from domain.cash_sizing import whole_shares_affordable  # noqa: E402

#: The live response, verbatim.
OBSERVED = {
    "tr_crcy_cd": "USD",
    "ord_psbl_frcr_amt": "20.96",
    "sll_ruse_psbl_amt": "33.78",
    "ovrs_ord_psbl_amt": "54.44",
    "max_ord_psbl_qty": "1",
    "ord_psbl_qty": "1",
    "ovrs_max_ord_psbl_qty": "0",
    "exrt": "1380.3000000000",
}


class TestTheAuthorityIsTheOrderableAmount:
    def test_the_field_read_is_the_overseas_orderable_amount(self):
        assert kb.ORDERABLE_AMOUNT_FIELD == "ovrs_ord_psbl_amt"

    def test_it_is_not_the_cash_field(self):
        """Cash alone was the defect."""
        assert kb.ORDERABLE_AMOUNT_FIELD != "ord_psbl_frcr_amt"
        assert kb.ORDERABLE_CASH_COMPONENT_FIELD == "ord_psbl_frcr_amt"

    def test_the_observed_response_parses_to_the_app_figure(self):
        parsed = kb._parse_orderable_amount({"output": OBSERVED}, symbol="F")
        assert parsed == pytest.approx(54.44)

    def test_the_cash_figure_would_have_said_zero_at_forty_dollars(self):
        """The exact defect, stated as arithmetic."""
        assert whole_shares_affordable(20.96, 40.0) == 0
        assert whole_shares_affordable(54.44, 40.0) == 1


class TestOurDivisionMatchesKIS:
    """`max_ord_psbl_qty` is KIS's own share count. If our arithmetic
    ever diverges from it, that is information rather than rounding."""

    @pytest.mark.parametrize("price,expected", [
        (5.0, 10), (10.0, 5), (20.0, 2), (30.0, 1),
        (40.0, 1), (50.0, 1), (54.0, 1), (60.0, 0),
    ])
    def test_the_sweep_reproduces_the_measured_quantities(self, price, expected):
        assert whole_shares_affordable(54.44, price) == expected

    def test_the_quantity_field_is_named_for_cross_checking(self):
        assert kb.ORDERABLE_QTY_FIELD == "max_ord_psbl_qty"

    def test_the_other_quantity_field_is_not_used(self):
        """`ovrs_max_ord_psbl_qty` read 0 at every price, including ones
        where an order was demonstrably possible."""
        assert kb.ORDERABLE_QTY_FIELD != "ovrs_max_ord_psbl_qty"


class TestTheThirtyCentDifferenceIsNotExplainedAway:
    def test_the_components_do_not_sum_to_the_total(self):
        cash = float(OBSERVED["ord_psbl_frcr_amt"])
        reuse = float(OBSERVED["sll_ruse_psbl_amt"])
        total = float(OBSERVED["ovrs_ord_psbl_amt"])
        assert cash + reuse == pytest.approx(54.74)
        assert total == pytest.approx(54.44)
        assert (cash + reuse) - total == pytest.approx(0.30, abs=0.001)

    def test_the_gap_is_named_rather_than_attributed(self):
        """No field in the response accounts for it. Calling it fees
        would be a guess with a number attached."""
        assert kb.UNKNOWN_ADJUSTMENT == "UNKNOWN_ADJUSTMENT"
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text(
            encoding="utf-8")
        assert "left alone rather than explained away as fees" in source

    def test_the_total_is_used_not_the_reconstructed_sum(self):
        """54.44 is what KIS will honour. Ordering against 54.74 would
        submit what it refuses."""
        parsed = kb._parse_orderable_amount({"output": OBSERVED}, symbol="F")
        assert parsed < 54.74


class TestTheParserStillFailsClosed:
    def test_a_missing_field_still_raises(self):
        with pytest.raises(kb.KISOrderableCashUnavailableError):
            kb._parse_orderable_amount({"output": {"ord_psbl_frcr_amt": "20.96"}},
                                       symbol="F")

    def test_a_real_zero_is_not_an_error(self):
        assert kb._parse_orderable_amount(
            {"output": {"ovrs_ord_psbl_amt": "0"}}, symbol="F") == 0.0

    def test_a_negative_amount_raises(self):
        with pytest.raises(kb.KISOrderableCashUnavailableError):
            kb._parse_orderable_amount(
                {"output": {"ovrs_ord_psbl_amt": "-1"}}, symbol="F")
