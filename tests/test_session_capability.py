"""One session resolver, and the four callers that used to disagree.

The defect this pins
--------------------
`get_market_state()` models the US venue's own sessions and returns
CLOSED for exactly 20:00->04:00 ET -- which is precisely the
OVERNIGHT_DAYTIME window. `run_s6_runtime` conjoined it into
`orders_allowed`, so daytime orders were not "blocked at the moment" but
blocked for the whole session, every day, by construction. The entry path
had already been fixed to ask a routing question instead, which left the
worse half in place: a session a BUY could be placed into but a SELL
could not leave.

So the tests below check two different things and both matter:

  * capability is decided by ROUTE and CALENDAR, never by the US venue's
    own open/closed state;
  * the guard that the market-state check WAS carrying -- weekends and
    holidays -- did not go missing when it was removed.
"""

from datetime import datetime

import pytest

from config import session_capability as sc
from market_hours import EASTERN

S6 = "S6_ORB_BREAKOUT_V1"
S1 = "S1_HMA_EARLY_TREND_V1"

# A Tuesday evening: inside OVERNIGHT_DAYTIME, ahead of a Wednesday session.
TUE_EVENING = datetime(2026, 8, 25, 23, 30, tzinfo=EASTERN)
WED_REGULAR = datetime(2026, 8, 26, 12, 0, tzinfo=EASTERN)


def _cap(moment, strategy_id=None):
    return sc.capability_for(sc.current_session(moment), now=moment,
                             strategy_id=strategy_id)


class TestCapabilityIsNotTheVenuesOwnState:
    def test_daytime_is_capable_while_the_us_market_is_closed(self):
        """The whole point. `get_market_state()` says CLOSED here and
        that is CORRECT -- the US primary market is shut. It is simply
        not evidence about whether KIS's 미국주간거래 can take an order."""
        from market_hours import get_market_state

        assert get_market_state(TUE_EVENING) == "CLOSED"
        cap = _cap(TUE_EVENING, S6)
        assert cap.session == "OVERNIGHT_DAYTIME"
        assert cap.entry_supported is True
        assert cap.exit_supported is True

    def test_the_daytime_window_is_never_market_open(self):
        """Not a timing coincidence -- a structural one. If any hour of
        OVERNIGHT_DAYTIME reported anything but CLOSED, the old
        conjunction would have worked sometimes, and this whole class of
        bug would have been intermittent instead of total."""
        from market_hours import get_market_state
        from scanners.base import scan_session

        for hour in (20, 21, 22, 23, 0, 1, 2, 3):
            day = 25 if hour >= 20 else 26
            t = datetime(2026, 8, day, hour, 0, tzinfo=EASTERN)
            assert scan_session.session_at(t) == "OVERNIGHT_DAYTIME"
            assert get_market_state(t) == "CLOSED"


class TestTheCalendarGuardSurvivedTheRemoval:
    """`session_at` is calendar-independent on purpose, so the
    market-state check that was wrong about the HOURS was nonetheless
    carrying the weekend/holiday guard. Removing it without replacing
    that guard would have permitted a Saturday order."""

    def test_friday_evening_belongs_to_saturday_and_is_refused(self):
        cap = _cap(datetime(2026, 8, 28, 21, 0, tzinfo=EASTERN), S6)
        assert cap.session == "OVERNIGHT_DAYTIME"
        assert cap.trading_day == "2026-08-29"
        assert cap.entry_supported is False
        assert cap.exit_supported is False
        assert cap.entry_reason == sc.NOT_A_TRADING_DAY

    def test_sunday_evening_belongs_to_monday_and_is_allowed(self):
        """The mirror image, and the reason the day cannot simply be the
        calendar date the window opens on."""
        cap = _cap(datetime(2026, 8, 30, 21, 0, tzinfo=EASTERN), S6)
        assert cap.trading_day == "2026-08-31"
        assert cap.entry_supported is True

    def test_saturday_small_hours_are_refused(self):
        cap = _cap(datetime(2026, 8, 29, 2, 0, tzinfo=EASTERN), S6)
        assert cap.trading_day == "2026-08-29"
        assert cap.entry_supported is False

    def test_a_holiday_is_refused(self):
        cap = _cap(datetime(2026, 12, 24, 21, 0, tzinfo=EASTERN), S6)
        assert cap.trading_day == "2026-12-25"
        assert cap.entry_supported is False
        assert cap.entry_reason == sc.NOT_A_TRADING_DAY


class TestEntryAndExitAreSeparateQuestions:
    def test_a_stood_down_strategy_keeps_its_exit(self):
        """The distinction the whole entry-policy split exists for. A
        stand-down that also removed the exit would not be a stand-down;
        it would strand the position it was meant to stop adding to."""
        cap = _cap(TUE_EVENING, S1)
        assert cap.entry_supported is False
        assert cap.entry_reason == sc.ENTRY_DISABLED_FOR_STRATEGY
        assert cap.exit_supported is True
        assert cap.exit_reason == sc.CAPABLE

    def test_exit_session_ignores_entry_permission(self):
        assert sc.exit_session(now=TUE_EVENING) == "OVERNIGHT_DAYTIME"
        assert sc.order_session(now=TUE_EVENING, strategy_id=S1) is None
        assert sc.order_session(now=TUE_EVENING, strategy_id=S6) == "OVERNIGHT_DAYTIME"

    def test_a_session_with_no_route_refuses_both_sides(self):
        """No asymmetry here: an exit cannot be sent to a nonexistent
        endpoint either."""
        for moment in (datetime(2026, 8, 26, 5, 0, tzinfo=EASTERN),
                       datetime(2026, 8, 26, 17, 0, tzinfo=EASTERN)):
            cap = _cap(moment, S6)
            assert cap.entry_supported is False
            assert cap.exit_supported is False


class TestItFailsClosed:
    @pytest.mark.parametrize("value", [None, "", "   ", "NOT_A_SESSION", "regular "])
    def test_an_unusable_session_name_yields_no_capability(self, value):
        if str(value or "").strip().upper() == "REGULAR":
            pytest.skip("'regular ' normalises to a real session")
        cap = sc.capability_for(value, now=TUE_EVENING)
        assert cap.entry_supported is False
        assert cap.exit_supported is False

    def test_a_whitespaced_name_still_normalises(self):
        assert sc.capability_for("  overnight_daytime  ",
                                 now=TUE_EVENING).entry_supported is True

    def test_an_unreadable_clock_is_a_refusal_not_a_pass(self, monkeypatch):
        from scanners.base import scan_session

        def _boom(*_a, **_k):
            raise RuntimeError("clock unavailable")

        monkeypatch.setattr(scan_session, "session_at", _boom)
        assert sc.current_session() == ""
        assert sc.order_session(strategy_id=S6) is None


class TestTheRoutesComeFromTheBroker:
    def test_daytime_carries_the_daytime_family_on_all_three(self):
        cap = _cap(TUE_EVENING, S6)
        assert cap.order_route_buy == (
            "/uapi/overseas-stock/v1/trading/daytime-order", "TTTS6036U")
        assert cap.order_route_sell == (
            "/uapi/overseas-stock/v1/trading/daytime-order", "TTTS6037U")
        assert cap.cancel_route == (
            "/uapi/overseas-stock/v1/trading/daytime-order-rvsecncl", "TTTS6038U")

    def test_regular_carries_the_regular_family(self):
        cap = _cap(WED_REGULAR, S6)
        assert cap.order_route_buy[1] == "TTTT1002U"
        assert cap.order_route_sell[1] == "TTTT1006U"
        assert cap.cancel_route[1] == "TTTT1004U"

    def test_no_route_is_shared_between_the_two_sessions(self):
        """Serving one session with the other's route is a real
        mis-routed order, not a cosmetic slip."""
        day, reg = _cap(TUE_EVENING, S6), _cap(WED_REGULAR, S6)
        for a, b in ((day.order_route_buy, reg.order_route_buy),
                     (day.order_route_sell, reg.order_route_sell),
                     (day.cancel_route, reg.cancel_route)):
            assert a[0] != b[0] and a[1] != b[1]


class TestTheCancelRouteFollowsTheOrder:
    """The cancel side had the defect the order side was fixed for."""

    def test_cancel_route_is_session_specific(self):
        from brokers import kis_broker as kb

        assert kb.cancel_route_for("OVERNIGHT_DAYTIME", "live")[1] == "TTTS6038U"
        assert kb.cancel_route_for("REGULAR", "live")[1] == "TTTT1004U"
        assert kb.cancel_route_for("REGULAR", "paper")[1] == "VTTT1004U"

    def test_none_means_unspecified_and_keeps_the_regular_route(self):
        from brokers import kis_broker as kb

        assert kb.cancel_route_for(None, "live")[1] == "TTTT1004U"

    def test_an_unrouted_session_raises_rather_than_guessing(self):
        from brokers.kis_broker import KISBrokerError, cancel_route_for

        for session in ("PREMARKET", "AFTER_HOURS", "", "  "):
            with pytest.raises(KISBrokerError):
                cancel_route_for(session, "live")

    def test_cancel_order_resolves_the_route_from_the_intent(self):
        """It read `TR_ID_CANCEL[env]` and `CANCEL_PATH` unconditionally,
        so a daytime order was cancelled through the regular endpoint --
        and a cancel addressed to the wrong family leaves the resting
        order live."""
        import inspect

        from brokers.kis_broker import KISBroker

        source = inspect.getsource(KISBroker.cancel_order)
        assert "cancel_route_for(" in source
        assert "TR_ID_CANCEL[self._env_key()]" not in source
        assert "{CANCEL_PATH}" not in source


class TestTheFourCallersAgree:
    def test_the_bootstrap_delegates_rather_than_deciding(self):
        import inspect

        from live_pilot import bootstrap

        source = inspect.getsource(bootstrap._order_session)
        assert "session_capability" in source
        # It must not re-derive the answer from the parts.
        assert "ROUTED_SESSIONS" not in source

    def test_the_runtime_does_not_gate_on_market_state(self):
        import inspect

        from scripts import run_s6_runtime

        source = inspect.getsource(run_s6_runtime.run_once)
        assert "session_capability" in source
        assert 'market != "CLOSED"' not in source

    def test_the_exit_adapter_sets_the_session_on_its_intent(self):
        """Without this the SELL fell back to the broker's REGULAR hint
        and a daytime exit went to TTTT1006U."""
        import inspect

        from brokers import kis_broker_adapter

        source = inspect.getsource(kis_broker_adapter)
        assert "exit_session(" in source
        assert "session=exit_route_session" in source

    def test_the_readiness_checker_asks_the_same_resolver(self):
        import pathlib

        source = pathlib.Path("scripts/final_pre_live_check.sh").read_text()
        assert "session_capability" in source
        # The retired stand-in must be gone, not merely bypassed.
        assert "NOT_REGULAR_SESSION" not in source
        assert "NO_ENTRY_ROUTE_FOR_SESSION" in source
        assert "NO_EXIT_ROUTE_FOR_SESSION" in source


class TestStrategyEntryPolicy:
    def test_s1_entry_is_disabled_and_s6_is_enabled(self):
        from config import strategy_entry_policy as sep

        assert sep.entry_enabled(S1) is False
        assert sep.entry_enabled(S6) is True

    def test_every_spelling_of_a_strategy_resolves_to_one_decision(self):
        """The registry knows several names for each strategy and they
        are all load-bearing. Three names must not become three
        permissions that can disagree."""
        from config import strategy_entry_policy as sep

        for alias in ("hma_early_trend", "S1_HMA_EARLY_TREND_V1",
                      "PAPER_STRATEGY_ORDER_SCORE_V1", "S1"):
            assert sep.entry_enabled(alias) is False
        for alias in ("orb", "S6_ORB_BREAKOUT_V1", "S6"):
            assert sep.entry_enabled(alias) is True

    def test_exit_is_enabled_for_everything_including_unknowns(self):
        """Fails OPEN, deliberately and in the opposite direction to
        entry: a position whose owner cannot be identified still has to
        be closable."""
        from config import strategy_entry_policy as sep

        for name in (S1, S6, "who_is_this", "", None):
            assert sep.exit_enabled(name) is True

    def test_an_unknown_strategy_gets_no_entry_permission(self):
        from config import strategy_entry_policy as sep

        for name in ("who_is_this", "", None):
            assert sep.entry_enabled(name) is False

    def test_exit_permission_is_not_configurable(self):
        """There is no table to flip. Anything that wants to stop an exit
        has to delete the function and face every caller."""
        import inspect

        from config import strategy_entry_policy as sep

        assert "return True" in inspect.getsource(sep.exit_enabled)

    def test_the_stand_down_did_not_touch_the_live_mode_table(self):
        """S1 stays LIMITED_LIVE. Turning it down took the scanner's
        publisher with it, which is not what "stop opening new
        positions" means."""
        from config import scanner_live_mode as slm

        assert slm.SCANNER_LIVE_MODE["hma_early_trend"] == slm.MODE_LIMITED_LIVE
        assert slm.SCANNER_LIVE_MODE["orb"] == slm.MODE_LIMITED_LIVE
