"""KIS refuses more than two decimals at $1 and above.

2026-09-01 13:55:25Z, the first natural S6 order ever to reach the broker:

    KIS_ORDER_REJECTED symbol=LUMN side=buy qty=19 limit=6.405
    tr_id=TTTT1002U rt_cd=7 msg_cd=APTR0057
    "주문 가격을 확인 하시기 바랍니다. 1$이상 소수점 2자리까지만 가능 합니다."

PCVX was queued behind it at 60.855 and refused identically. JBS was
accepted in the same minute at 13.75 -- two decimals by accident of what
the last trade printed. Acceptance was luck, not construction.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers.order_price import (  # noqa: E402
    normalize_limit_price, wire_price,
)


class TestTheRejectionThatCausedThis:
    def test_the_exact_lumn_input_becomes_acceptable(self):
        assert wire_price(6.405, side="buy") == "6.40"

    def test_the_exact_pcvx_input_becomes_acceptable(self):
        assert wire_price(60.855, side="buy") == "60.85"

    @pytest.mark.parametrize("price", [6.405, 60.855, 6.404, 6.406, 1.005])
    def test_no_three_decimal_price_survives_normalization(self, price):
        for side in ("buy", "sell"):
            assert Decimal(wire_price(price, side=side)).as_tuple().exponent >= -2


class TestDirectionNeverWorksAgainstTheStrategy:
    def test_a_buy_never_pays_more_than_the_strategy_authorised(self):
        for price in (6.405, 6.406, 6.409, 1.005, 60.859):
            assert Decimal(wire_price(price, side="buy")) <= Decimal(str(price))

    def test_a_sell_never_accepts_less_than_the_strategy_authorised(self):
        for price in (6.401, 6.405, 1.001, 60.851):
            assert Decimal(wire_price(price, side="sell")) >= Decimal(str(price))

    def test_the_adjustment_is_never_more_than_one_cent(self):
        for price in (6.405, 60.855, 1.009, 999.999):
            for side in ("buy", "sell"):
                moved = abs(Decimal(wire_price(price, side=side))
                            - Decimal(str(price)))
                assert moved < Decimal("0.01")

    def test_binary_float_rounding_is_not_used(self):
        """round(1.005, 2) is 1.0 on this interpreter; a sell must not
        land below the price the strategy chose because of that."""
        assert round(1.005, 2) == 1.0
        assert Decimal(wire_price(1.005, side="sell")) >= Decimal("1.005")


class TestAlreadyValidPricesAreUntouched:
    @pytest.mark.parametrize("price,expected", [
        (13.75, "13.75"), (6.40, "6.40"), (60.85, "60.85"), (1.00, "1.00"),
    ])
    def test_two_decimal_prices_pass_through(self, price, expected):
        assert wire_price(price, side="buy") == expected

    def test_the_accepted_jbs_order_is_unchanged(self):
        """The one order that worked must keep working."""
        assert wire_price(13.75, side="buy") == "13.75"

    def test_trailing_zeros_survive_onto_the_wire(self):
        """'6.4' is a different number of decimal places than '6.40'."""
        assert wire_price(6.4, side="buy") == "6.40"


class TestTheUnprovenRuleIsNotGuessed:
    """The broker stated the rule for $1 and above and nothing else."""

    @pytest.mark.parametrize("price", [0.4567, 0.99, 0.005, 0.1])
    def test_sub_dollar_prices_are_left_exactly_as_they_are(self, price):
        assert normalize_limit_price(price, side="buy") == price

    def test_one_dollar_is_inside_the_proven_rule(self):
        assert wire_price(1.0, side="buy") == "1.00"

    def test_unparseable_input_is_left_to_the_callers_validation(self):
        for junk in (None, "", "abc", object()):
            assert normalize_limit_price(junk, side="buy") is junk


class TestOneCommonPathForEveryRealOrder:
    def test_the_broker_normalizes_at_the_single_wire_point(self):
        text = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        assert "from brokers.order_price import wire_price" in text
        assert '"OVRS_ORD_UNPR": broker_order_price' in text
        assert '"OVRS_ORD_UNPR": str(order_intent.limit_price)' not in text

    def test_there_is_no_strategy_specific_formatter(self):
        """No strategy module may BUILD an order payload of its own.

        Matching the field name alone was too blunt: `entry_timeout`
        mentions OVRS_ORD_UNPR="0" in a comment recording KIS's cancel
        rule, which is documentation, not a second formatter.
        """
        for module in ("s6_live", "s1_live", "execution"):
            for path in (REPO_ROOT / module).rglob("*.py"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert '"OVRS_ORD_UNPR":' not in text, path

    def test_both_prices_are_logged_when_they_differ(self):
        text = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        assert "ORDER_PRICE_NORMALIZED" in text
        assert "strategy_price=" in text and "broker_order_price=" in text

    def test_the_strategy_decision_price_is_not_mutated(self):
        """The intent keeps what the strategy decided; only the wire
        value is normalized."""
        text = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        assert "order_intent.limit_price =" not in text
