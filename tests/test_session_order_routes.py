"""Each session's order route, verified against the official spec.

Sessions do not share a route, and assuming they do is the failure this
file exists to prevent. Serving OVERNIGHT_DAYTIME with the REGULAR TR
would send a real order to an endpoint that does not run at that hour --
a rejected or mis-booked live order, not an error anyone catches in
testing.

What the official reference actually specifies
----------------------------------------------
From the koreainvestment/open-trading-api repo:

  REGULAR             /uapi/overseas-stock/v1/trading/order
                      TTTT1002U buy, TTTT1006U sell, TTTT1004U cancel
  OVERNIGHT_DAYTIME   /uapi/overseas-stock/v1/trading/daytime-order
                      TTTS6036U buy, TTTS6037U sell
                      /uapi/overseas-stock/v1/trading/daytime-order-rvsecncl
                      TTTS6038U cancel                [v1_해외주식-026/027]

  PREMARKET           no US extended-hours order endpoint exists
  AFTER_HOURS         no US extended-hours order endpoint exists

The complete set of US order TRs in the reference is those six. ORD_DVSN
carries LOO/LOC/MOO/MOC, but those are at-the-open and at-the-close
order types WITHIN the regular session -- they are not a premarket or
after-hours route, and reading them as one would be exactly the guess
that must not happen.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers.kis_broker import (  # noqa: E402
    DAYTIME_CANCEL_PATH,
    DAYTIME_ORDER_PATH,
    ORDER_PATH,
    ROUTED_SESSIONS,
    TR_ID_CANCEL,
    TR_ID_DAYTIME_CANCEL,
    TR_ID_DAYTIME_ORDER_US,
    TR_ID_ORDER_US,
    KISBrokerError,
    order_route_for,
)


class TestTheRegularSessionRoute:
    def test_buy_and_sell(self):
        assert order_route_for("REGULAR", "live", "buy") == \
            (ORDER_PATH, "TTTT1002U")
        assert order_route_for("REGULAR", "live", "sell") == \
            (ORDER_PATH, "TTTT1006U")

    def test_the_cancel_tr_is_the_general_one(self):
        """TTTS6038U is daytime-specific and was once reused here."""
        assert TR_ID_CANCEL["live"] == "TTTT1004U"
        assert TR_ID_CANCEL["live"] != TR_ID_DAYTIME_CANCEL["live"]

    def test_an_absent_session_defaults_to_regular(self):
        """An existing caller that knows nothing about sessions must keep
        its exact previous behaviour, not silently acquire a different
        endpoint."""
        assert order_route_for(None, "live", "buy") == (ORDER_PATH, "TTTT1002U")


class TestTheDaytimeSessionRoute:
    """[v1_해외주식-026] and [v1_해외주식-027]."""

    def test_it_is_a_different_endpoint_from_regular(self):
        path, _tr = order_route_for("OVERNIGHT_DAYTIME", "live", "buy")
        assert path == DAYTIME_ORDER_PATH
        assert path != ORDER_PATH

    def test_buy_and_sell_tr_ids(self):
        assert order_route_for("OVERNIGHT_DAYTIME", "live", "buy") == \
            (DAYTIME_ORDER_PATH, "TTTS6036U")
        assert order_route_for("OVERNIGHT_DAYTIME", "live", "sell") == \
            (DAYTIME_ORDER_PATH, "TTTS6037U")

    def test_the_cancel_route(self):
        assert DAYTIME_CANCEL_PATH == \
            "/uapi/overseas-stock/v1/trading/daytime-order-rvsecncl"
        assert TR_ID_DAYTIME_CANCEL["live"] == "TTTS6038U"

    def test_it_shares_no_tr_id_with_regular(self):
        assert set(TR_ID_DAYTIME_ORDER_US.values()).isdisjoint(
            set(TR_ID_ORDER_US.values()))

    def test_the_exchange_codes_are_the_ORDER_code_space(self):
        """The daytime endpoint takes NASD/NYSE/AMEX, the same codes the
        regular order uses. BAQ/BAY/BAA are the real-time QUOTE stream's
        tr_key values from a different API -- this module once recorded
        them as the daytime ORDER codes and refused the session on that
        basis, which cost S6-O its route for no reason."""
        from domain.exchange import to_kis_order_exchange_code

        for venue, expected in (("NASDAQ", "NASD"), ("NYSE", "NYSE"),
                                ("AMEX", "AMEX")):
            assert to_kis_order_exchange_code(venue) == expected
            assert to_kis_order_exchange_code(venue) not in ("BAQ", "BAY", "BAA")


class TestTheGeneralFamilyServesThreeSessions:
    """Premarket, regular and aftermarket SHARE one endpoint and one TR
    family -- the overseas order API documents US orders in all three.

    These were previously asserted to be unroutable, on the reasoning
    that no premarket-specific TR exists. That inverted what the absence
    meant: having no session-specific TR is what sharing a route looks
    like, and the mistake cost S6 half the sessions it scans.
    """

    @pytest.mark.parametrize("session", ["PREMARKET", "REGULAR", "AFTER_HOURS"])
    def test_they_all_resolve_to_the_general_route(self, session):
        assert order_route_for(session, "live", "buy") == (
            "/uapi/overseas-stock/v1/trading/order", "TTTT1002U")
        assert order_route_for(session, "live", "sell") == (
            "/uapi/overseas-stock/v1/trading/order", "TTTT1006U")

    @pytest.mark.parametrize("session", ["PREMARKET", "REGULAR", "AFTER_HOURS"])
    def test_their_cancel_is_the_general_cancel(self, session):
        from brokers.kis_broker import cancel_route_for

        assert cancel_route_for(session, "live") == (
            "/uapi/overseas-stock/v1/trading/order-rvsecncl", "TTTT1004U")

    def test_daytime_keeps_its_own_family(self):
        """Sharing a route between the general sessions is not licence to
        share one with daytime: different endpoint, different TRs, and an
        hour at which the general endpoint does not run."""
        assert order_route_for("OVERNIGHT_DAYTIME", "live", "buy") == (
            "/uapi/overseas-stock/v1/trading/daytime-order", "TTTS6036U")
        assert order_route_for("OVERNIGHT_DAYTIME", "live", "sell") == (
            "/uapi/overseas-stock/v1/trading/daytime-order", "TTTS6037U")

    def test_the_four_sessions_are_routed_and_nothing_else(self):
        assert ROUTED_SESSIONS == {
            "PREMARKET", "REGULAR", "AFTER_HOURS", "OVERNIGHT_DAYTIME"}

    def test_the_aftermarket_extension_is_not_a_routed_session(self):
        """It is gated behind a per-customer application, so API support
        does not follow from the published schedule. Refused rather than
        assumed either way."""
        with pytest.raises(KISBrokerError, match="no KIS order route"):
            order_route_for("AFTERMARKET_EXTENSION", "live", "buy")

    def test_an_unknown_session_name_is_refused(self):
        for name in ("LUNCH", "", "regular_session", "S6-P"):
            with pytest.raises(KISBrokerError):
                order_route_for(name, "live", "buy")

    def test_a_missing_tr_for_a_routed_session_still_raises(self):
        """paper has no daytime TR in the reference."""
        with pytest.raises(KISBrokerError, match="no order TR_ID"):
            order_route_for("OVERNIGHT_DAYTIME", "paper", "buy")


class TestTheWireValuesAreNotSelfCertified:
    """A constant existing in this file is not evidence that KIS ever
    answered it. The daytime values are REFERENCE_VERIFIED and
    LIVE_RESPONSE_PENDING, exactly like the regular ones."""

    def test_the_daytime_values_are_in_the_matrix(self):
        from brokers.kis_broker import VERIFICATION_MATRIX

        names = {w.name for w in VERIFICATION_MATRIX}
        assert {"daytime_order_path", "daytime_order_tr_id_live_buy",
                "daytime_order_tr_id_live_sell", "daytime_cancel_path",
                "daytime_cancel_tr_id_live"} <= names

    def test_none_of_them_claims_a_live_response(self):
        from brokers.kis_broker import LIVE_RESPONSE_PENDING, VERIFICATION_MATRIX

        for wire in VERIFICATION_MATRIX:
            if wire.name.startswith("daytime_"):
                assert wire.live_status == LIVE_RESPONSE_PENDING, wire.name
