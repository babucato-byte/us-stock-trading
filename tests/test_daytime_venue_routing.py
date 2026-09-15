"""KIS market-data routing is per-SESSION, because R/D is a VENUE split.

미국주간거래 (OVERNIGHT_DAYTIME) is a different book from the US
regular/extended one. Addressing it with the regular venue's codes
returns a subscription that acks and never sends, and a chart that
answers with the PREVIOUS regular session's bars -- both of which read
like "this account has no daytime data" and neither of which is.
"""

import pytest

from market_data import kis_hdfscnt0 as wire
from market_data import kis_minute_chart as mc


DAYTIME = "OVERNIGHT_DAYTIME"
OTHER_SESSIONS = ("PREMARKET", "REGULAR", "AFTER_HOURS")


class TestWebSocketDaytimeKeys:
    """1-3. The daytime venue's tr_key prefixes."""

    @pytest.mark.parametrize("exchange,expected", [
        ("NASDAQ", "RBAQAAPL"),
        ("NYSE", "RBAYAAPL"),
        ("AMEX", "RBAAAAPL"),
    ])
    def test_daytime_uses_the_r_prefix_venue(self, exchange, expected):
        assert wire.tr_key_for_session("AAPL", exchange, DAYTIME) == expected


class TestWebSocketStandardKeys:
    """4-6. Every other session keeps the regular/extended venue."""

    @pytest.mark.parametrize("session", OTHER_SESSIONS)
    @pytest.mark.parametrize("exchange,expected", [
        ("NASDAQ", "DNASAAPL"),
        ("NYSE", "DNYSAAPL"),
        ("AMEX", "DAMSAAPL"),
    ])
    def test_other_sessions_use_the_d_prefix_venue(self, session, exchange,
                                                   expected):
        assert wire.tr_key_for_session("AAPL", exchange, session) == expected

    def test_an_unknown_session_is_not_treated_as_daytime(self):
        """Only the daytime session gets the daytime venue; anything
        unrecognised keeps the book that has always worked."""
        assert wire.tr_key_for_session("AAPL", "NASDAQ", "") == "DNASAAPL"
        assert wire.tr_key_for_session("AAPL", "NASDAQ", None) == "DNASAAPL"
        assert wire.tr_key_for_session("AAPL", "NASDAQ", "NONSENSE") == "DNASAAPL"

    def test_exchange_spellings_are_all_accepted(self):
        """The first live bootstrap died on one 'NASDAQ' where the wire
        wanted 'NAS'; both spellings must route in both venues."""
        for spelling in ("NASDAQ", "NAS", "NASD"):
            assert wire.tr_key_for_session("AAPL", spelling, DAYTIME) == "RBAQAAPL"
            assert wire.tr_key_for_session("AAPL", spelling, "REGULAR") == "DNASAAPL"

    def test_an_unmappable_exchange_still_raises_per_symbol(self):
        with pytest.raises(ValueError):
            wire.tr_key_for_session("AAPL", "LSE", DAYTIME)


class TestMinuteChartDaytimeCodes:
    """7-9. The daytime venue's EXCD codes."""

    @pytest.mark.parametrize("exchange,expected", [
        ("NASDAQ", "BAQ"), ("NYSE", "BAY"), ("AMEX", "BAA"),
    ])
    def test_daytime_excd(self, exchange, expected):
        assert mc.excd_for_session(exchange, DAYTIME) == expected


class TestMinuteChartStandardCodes:
    """10. Non-daytime routing is untouched."""

    @pytest.mark.parametrize("session", OTHER_SESSIONS + (None, "", "NONSENSE"))
    @pytest.mark.parametrize("exchange,expected", [
        ("NASDAQ", "NAS"), ("NYSE", "NYS"), ("AMEX", "AMS"),
    ])
    def test_other_sessions_keep_the_generic_code(self, session, exchange,
                                                  expected):
        assert mc.excd_for_session(exchange, session) == expected

    def test_daytime_mapping_reuses_the_canonical_normalisation(self):
        """Keyed off the standard code, so every alias the generic table
        understands routes in the daytime venue too -- for free."""
        for spelling in ("NASDAQ", "NAS", "NASD"):
            assert mc.excd_for_session(spelling, DAYTIME) == "BAQ"


class TestNoSessionLeak:
    """13. A daytime mapping must not survive into another session."""

    def test_ws_routing_is_recomputed_per_session(self):
        seq = [(DAYTIME, "RBAQAAPL"), ("PREMARKET", "DNASAAPL"),
               (DAYTIME, "RBAQAAPL"), ("REGULAR", "DNASAAPL")]
        for session, expected in seq:
            assert wire.tr_key_for_session("AAPL", "NASDAQ", session) == expected

    def test_minute_routing_is_recomputed_per_session(self):
        seq = [(DAYTIME, "BAQ"), ("PREMARKET", "NAS"),
               (DAYTIME, "BAQ"), ("AFTER_HOURS", "NAS")]
        for session, expected in seq:
            assert mc.excd_for_session("NASDAQ", session) == expected

    def test_the_tables_are_distinct_objects(self):
        """A shared/mutated table is how a leak would actually happen."""
        assert wire.DAYTIME_PREFIX is not wire.STANDARD_PREFIX
        assert set(wire.DAYTIME_PREFIX.values()) == {"RBAQ", "RBAY", "RBAA"}
        assert set(wire.STANDARD_PREFIX.values()) == {"DNAS", "DNYS", "DAMS"}


class TestUnchangedContracts:
    """11, 12, 15. What this fix must NOT have moved."""

    def test_hdfscnt0_tr_id_is_unchanged(self):
        assert wire.TR_TRADE == "HDFSCNT0"

    def test_chart_tr_id_and_path_are_unchanged(self):
        assert mc.TR_ID_CHART == "HHDFS76950200"
        assert mc.CHART_PATH == (
            "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice")

    def test_subscription_cap_is_unchanged(self):
        """12. Routing changes which venue is addressed, never how many."""
        assert wire.MAX_SUBSCRIPTIONS == 41

    def test_subscribe_frame_shape_is_unchanged(self):
        import json

        frame = json.loads(wire.subscribe_frame("KEY", "RBAQAAPL"))
        assert frame["body"]["input"]["tr_id"] == "HDFSCNT0"
        assert frame["body"]["input"]["tr_key"] == "RBAQAAPL"
        assert frame["header"]["tr_type"] == "1"

    def test_parser_is_untouched_by_routing(self):
        """15. `parse_trades` never saw the venue and still does not --
        the same frame parses identically whichever key requested it."""
        import inspect

        body = inspect.getsource(wire.parse_trades)
        for token in ("RBAQ", "DNAS", "OVERNIGHT_DAYTIME", "session"):
            assert token not in body

    def test_deprecated_aliases_still_resolve(self):
        """Existing callers and tests must not break on the rename."""
        assert wire.DELAYED_PREFIX is wire.STANDARD_PREFIX
        assert wire.REALTIME_PREFIX is wire.DAYTIME_PREFIX
        assert wire.tr_key("AAPL", "NASDAQ", wire.FEED_DELAYED) == "DNASAAPL"


class TestCallersThreadTheSession:
    """The routing is only correct if the callers actually pass it."""

    def test_collector_uses_the_session_aware_key(self):
        import inspect

        from scripts import run_realtime_bar_collector as runner

        body = inspect.getsource(runner.collect)
        assert "tr_key_for_session" in body
        assert "DEFAULT_FEED" not in body, (
            "a global default is what mis-routed the daytime venue")

    def test_chart_fetch_accepts_and_uses_a_session(self):
        import inspect

        assert "session" in inspect.signature(mc.fetch).parameters
        assert "excd_for_session" in inspect.getsource(mc.fetch)

    def test_provider_passes_its_session_to_the_chart(self):
        import inspect

        from market_data.kis_bar_provider import KISBarMarketDataProvider

        body = inspect.getsource(KISBarMarketDataProvider.get_intraday_bars)
        assert "session=self._session" in body
