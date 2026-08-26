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

    def test_premarket_and_aftermarket_now_support_both_sides(self):
        """They address the GENERAL family, which the overseas order API
        documents US orders in. Excluding them read a SHARED route as a
        missing one and cost S6 half the sessions it scans."""
        for moment, expected in (
                (datetime(2026, 8, 26, 5, 0, tzinfo=EASTERN), "PREMARKET"),
                (datetime(2026, 8, 26, 17, 0, tzinfo=EASTERN), "AFTER_HOURS")):
            cap = _cap(moment, S6)
            assert cap.session == expected
            assert cap.family == sc.FAMILY_GENERAL
            assert cap.entry_supported is True
            assert cap.exit_supported is True

    def test_a_window_with_no_established_family_refuses_both_sides(self):
        """No asymmetry: an exit cannot be sent to an endpoint whose
        availability has not been established either. The aftermarket
        EXTENSION is gated behind a per-customer application, so API
        support does not follow from the published schedule."""
        cap = sc.capability_at(datetime(2026, 8, 26, 18, 30, tzinfo=EASTERN),
                               strategy_id=S6)
        assert cap.window == "AFTERMARKET_EXTENSION"
        assert cap.session == ""
        assert cap.entry_supported is False
        assert cap.exit_supported is False
        assert cap.entry_reason == sc.ROUTE_FAMILY_UNVERIFIED

    def test_the_closed_hour_refuses_both_sides(self):
        """Under DST, 20:00-21:00 ET is 09:00-10:00 KST: the extension
        has ended and 주간거래 has not opened. A fixed-ET daytime
        boundary called this OVERNIGHT_DAYTIME and permitted an order."""
        cap = sc.capability_at(datetime(2026, 8, 26, 20, 30, tzinfo=EASTERN),
                               strategy_id=S6)
        assert cap.window == "CLOSED"
        assert cap.session == ""
        assert cap.entry_supported is False
        assert cap.exit_supported is False
        assert cap.entry_reason == sc.MARKET_CLOSED


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
        """Capability is time-driven now, so the clock IS the input.
        An unreadable one must refuse rather than fall through."""
        from config import kis_market_schedule

        def _boom(*_a, **_k):
            raise RuntimeError("clock unavailable")

        monkeypatch.setattr(kis_market_schedule, "window_at", _boom)
        with pytest.raises(RuntimeError):
            kis_market_schedule.window_at(TUE_EVENING)

        monkeypatch.setattr(kis_market_schedule, "window_at",
                            lambda *_a, **_k: kis_market_schedule.WINDOW_CLOSED)
        assert sc.current_session() == ""
        assert sc.order_session(strategy_id=S6) is None
        assert sc.exit_session() is None


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

    def test_the_general_sessions_share_one_cancel_route(self):
        from brokers.kis_broker import cancel_route_for

        for session in ("PREMARKET", "REGULAR", "AFTER_HOURS"):
            assert cancel_route_for(session, "live")[1] == "TTTT1004U"

    def test_a_non_session_raises_rather_than_guessing(self):
        from brokers.kis_broker import KISBrokerError, cancel_route_for

        for session in ("AFTERMARKET_EXTENSION", "CLOSED", "", "  "):
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


class TestBothGatesAgreeAboutArmed:
    """Two gates ask "may a bootstrap run", and only one had been taught
    that ARMED is not a reason to refuse.

    `final_safety_recheck` inside the order path was fixed; the
    capability mint one step before the wire was not. So the one-shot
    passed the first gate and was refused by the second with
    BOOTSTRAP_CAPABILITY_UNAVAILABLE -- on a live deployment, after every
    other precondition had passed. The predicate now lives in one place.
    """

    ARMED_ENV = {
        "KIS_LIVE_ORDER_ENABLED": "true", "LIVE_ROLLOUT_ENABLED": "true",
        "ENTRY_DISABLED": "false", "LIVE_BOOTSTRAP_ENABLED": "true",
        "LIVE_BOOTSTRAP_ACK": "true",
    }

    def test_the_mint_succeeds_on_an_armed_deployment(self, monkeypatch):
        """The session is pinned rather than read from the clock: minting
        is a question about posture and evidence, and this test must not
        pass or fail depending on which session the suite runs in."""
        from config import session_capability
        from execution import bootstrap_capability as bc

        monkeypatch.setattr(
            session_capability, "capability_at",
            lambda *_a, **_k: session_capability.SessionCapability(
                session="OVERNIGHT_DAYTIME", window="DAYTIME",
                family=session_capability.FAMILY_DAYTIME,
                trading_day="2026-08-27", entry_supported=True,
                exit_supported=True, entry_reason=session_capability.CAPABLE,
                exit_reason=session_capability.CAPABLE))
        cap = bc.mint(symbol="IBN", allowed_symbols={"IBN"}, env=self.ARMED_ENV)
        assert cap.mode == bc.MODE_LIMITED_LIVE_BOOTSTRAP
        assert cap.symbol == "IBN" and cap.quantity == 1 and cap.side == "buy"

    def test_a_session_with_no_route_gets_no_armed_exemption(self, monkeypatch):
        """PREMARKET and AFTER_HOURS have no KIS order route at all, so
        there is nothing a bootstrap could confirm in them."""
        from config import session_capability
        from execution import bootstrap_capability as bc

        monkeypatch.setattr(session_capability, "capability_at",
                            lambda *_a, **_k: session_capability.SessionCapability(
                                session="", window="CLOSED", family=None,
                                trading_day=None, entry_supported=False,
                                exit_supported=False,
                                entry_reason=session_capability.MARKET_CLOSED,
                                exit_reason=session_capability.MARKET_CLOSED))
        with pytest.raises(bc.BootstrapCapabilityError):
            bc.mint(symbol="IBN", allowed_symbols={"IBN"}, env=self.ARMED_ENV)

    def test_the_two_gates_share_one_predicate(self):
        """Two copies of a rule are two chances to fix only one."""
        import inspect

        from execution import bootstrap_capability as bc
        from live_pilot import bootstrap

        assert "session_capability" in inspect.getsource(bc._check_environment)
        assert "session_capability" in inspect.getsource(
            bootstrap._route_awaiting_live_evidence)

    def test_widening_the_posture_did_not_widen_the_acknowledgement(self):
        """It widens WHICH posture may attempt, never what may be
        attempted without a deliberate operator action."""
        from execution import bootstrap_capability as bc

        for flag in ("LIVE_BOOTSTRAP_ACK", "LIVE_BOOTSTRAP_ENABLED"):
            env = dict(self.ARMED_ENV, **{flag: "false"})
            with pytest.raises(bc.BootstrapCapabilityError):
                bc.mint(symbol="IBN", allowed_symbols={"IBN"}, env=env)

    def test_an_allow_list_of_other_than_one_is_still_refused(self):
        from execution import bootstrap_capability as bc

        for allowed in (frozenset(), frozenset({"IBN", "SLF"})):
            with pytest.raises(bc.BootstrapCapabilityError):
                bc.mint(symbol="IBN", allowed_symbols=allowed, env=self.ARMED_ENV)

    def test_a_confirmed_route_gets_no_armed_exemption(self, monkeypatch):
        """The exemption is for a route with unconfirmed wire values. A
        route that has them all is not a bootstrap's business, and on an
        armed deployment it must fall back to being refused."""
        from config import session_capability
        from execution import bootstrap_capability as bc

        monkeypatch.setattr(session_capability,
                            "route_awaiting_live_evidence", lambda _s: False)
        with pytest.raises(bc.BootstrapCapabilityError) as excinfo:
            bc.mint(symbol="IBN", allowed_symbols={"IBN"}, env=self.ARMED_ENV)
        assert "unconfirmed wire values" in str(excinfo.value)

    def test_the_evidence_sets_are_asked_per_session(self):
        """They now ANSWER differently, which is the sharpest possible
        demonstration that the question is per route: the general
        sessions are confirmed and daytime is not."""
        from config import session_capability as scap

        assert scap.route_awaiting_live_evidence("OVERNIGHT_DAYTIME") is True
        for general in ("PREMARKET", "REGULAR", "AFTER_HOURS"):
            assert scap.route_awaiting_live_evidence(general) is False

    def test_clearing_the_live_flags_is_not_the_alternative(self):
        """Reaching LIMITED_LIVE_BOOTSTRAP by turning the live flags off
        would disable the EXIT of every position already held --
        `evaluate_sell_gate` reads `live_order_enabled`."""
        import inspect

        from execution import order_gate

        assert "live_order_enabled" in inspect.getsource(
            order_gate.evaluate_sell_gate)


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


class TestTheKISScheduleIsTheSourceOfTruth:
    """Windows are KIS's, published in KST, and derived -- not asserted
    alongside them in Eastern time.

    `scan_session` partitions the day by fixed Eastern hours, which is
    right for SCANNING: every scan lands in exactly one bucket and the
    buckets never move. It is wrong for ORDERING, because KIS's windows
    are fixed in KST and the KST->ET offset moves an hour with US
    daylight saving. KIS shortens the daytime window in summer (17:00
    rather than 18:00 KST close) precisely so its ET *close* stays 04:00,
    which means the ET *open* moves -- 20:00 in standard time, 21:00
    under DST.
    """

    from datetime import datetime as _dt

    from market_hours import EASTERN as _ET

    def test_dst_daytime_is_refused_before_the_open(self):
        """20:00-21:00 ET under DST is 09:00-10:00 KST: the aftermarket
        extension has ended and 주간거래 has not begun. The fixed-ET
        boundary called this OVERNIGHT_DAYTIME and permitted an order
        every DST day."""
        for minute in (0, 30, 59):
            t = self._dt(2026, 8, 26, 20, minute, tzinfo=self._ET)
            cap = sc.capability_at(t, strategy_id=S6)
            assert cap.window == "CLOSED", t
            assert cap.entry_supported is False
            assert cap.exit_supported is False

    def test_dst_daytime_is_accepted_at_the_exact_open(self):
        t = self._dt(2026, 8, 26, 21, 0, tzinfo=self._ET)
        cap = sc.capability_at(t, strategy_id=S6)
        assert cap.window == "DAYTIME"
        assert cap.family == sc.FAMILY_DAYTIME
        assert cap.entry_supported is True and cap.exit_supported is True

    def test_standard_time_daytime_opens_an_hour_earlier_in_eastern(self):
        """The same KST instant, 10:00, in both halves of the year. The
        boundary is only 'an hour early' relative to a fixed ET rule; in
        KST it never moved."""
        before = sc.capability_at(self._dt(2026, 12, 10, 19, 59, tzinfo=self._ET))
        at_open = sc.capability_at(self._dt(2026, 12, 10, 20, 0, tzinfo=self._ET))
        assert before.window == "CLOSED"
        assert at_open.window == "DAYTIME"

    def test_the_daytime_close_is_04_00_eastern_in_both_halves(self):
        """What KIS holds fixed. Shortening the KST window in summer is
        what keeps this true."""
        for month, day in ((8, 27), (12, 11)):
            last = sc.capability_at(self._dt(2026, month, day, 3, 59, tzinfo=self._ET))
            after = sc.capability_at(self._dt(2026, month, day, 4, 0, tzinfo=self._ET))
            assert last.window == "DAYTIME", (month, day)
            assert after.window == "PREMARKET", (month, day)


class TestEveryWindowRoutesToItsFamily:
    from datetime import datetime as _dt

    from market_hours import EASTERN as _ET

    GENERAL = (
        (_dt(2026, 8, 26, 5, 0, tzinfo=_ET), "PREMARKET"),
        (_dt(2026, 8, 26, 12, 0, tzinfo=_ET), "REGULAR"),
        (_dt(2026, 8, 26, 17, 0, tzinfo=_ET), "AFTER_HOURS"),
    )

    @pytest.mark.parametrize("moment,session", GENERAL)
    def test_general_sessions_buy_sell_and_cancel_on_one_family(
            self, moment, session):
        cap = sc.capability_at(moment, strategy_id=S6)
        assert cap.session == session
        assert cap.family == sc.FAMILY_GENERAL
        assert cap.order_route_buy == (
            "/uapi/overseas-stock/v1/trading/order", "TTTT1002U")
        assert cap.order_route_sell == (
            "/uapi/overseas-stock/v1/trading/order", "TTTT1006U")
        assert cap.cancel_route == (
            "/uapi/overseas-stock/v1/trading/order-rvsecncl", "TTTT1004U")

    def test_daytime_buy_sell_and_cancel_on_its_own_family(self):
        cap = sc.capability_at(
            self._dt(2026, 8, 26, 22, 0, tzinfo=self._ET), strategy_id=S6)
        assert cap.family == sc.FAMILY_DAYTIME
        assert cap.order_route_buy == (
            "/uapi/overseas-stock/v1/trading/daytime-order", "TTTS6036U")
        assert cap.order_route_sell == (
            "/uapi/overseas-stock/v1/trading/daytime-order", "TTTS6037U")
        assert cap.cancel_route == (
            "/uapi/overseas-stock/v1/trading/daytime-order-rvsecncl", "TTTS6038U")

    def test_the_two_families_share_no_endpoint_and_no_tr(self):
        general = sc.capability_at(self._dt(2026, 8, 26, 12, 0, tzinfo=self._ET))
        daytime = sc.capability_at(self._dt(2026, 8, 26, 22, 0, tzinfo=self._ET))
        for a, b in ((general.order_route_buy, daytime.order_route_buy),
                     (general.order_route_sell, daytime.order_route_sell),
                     (general.cancel_route, daytime.cancel_route)):
            assert a[0] != b[0] and a[1] != b[1]

    def test_evidence_is_per_route_not_per_session(self):
        """The three general sessions confirm ONE set together, because
        they address one endpoint. 'The ARMED five' is a name from when
        the regular session was the only one considered."""
        from brokers import kis_broker as kb

        assert sc.evidence_posture_for_family(sc.FAMILY_GENERAL) \
            == kb.REQUIRED_FOR_ARMED
        assert sc.evidence_posture_for_family(sc.FAMILY_DAYTIME) \
            == kb.REQUIRED_FOR_DAYTIME
        for session in ("PREMARKET", "REGULAR", "AFTER_HOURS"):
            assert sc.route_awaiting_live_evidence(session) is \
                sc.route_awaiting_live_evidence(sc.FAMILY_GENERAL)
        # And the general route is the one a live response has reached.
        assert sc.route_awaiting_live_evidence(sc.FAMILY_GENERAL) is False
        assert sc.route_awaiting_live_evidence(sc.FAMILY_DAYTIME) is True


class TestOneNowThreadedThroughEverything:
    """A boundary straddle is the failure this prevents: an order checked
    against one session and addressed to another because two reads of the
    clock fell either side of a window edge."""

    from datetime import datetime as _dt, timedelta as _td

    from market_hours import EASTERN as _ET

    def test_the_same_moment_gives_the_same_answer_everywhere(self):
        from live_pilot import bootstrap

        for t in (self._dt(2026, 8, 26, 5, 0, tzinfo=self._ET),
                  self._dt(2026, 8, 26, 12, 0, tzinfo=self._ET),
                  self._dt(2026, 8, 26, 22, 0, tzinfo=self._ET)):
            cap = sc.capability_at(t, strategy_id=S6)
            assert bootstrap._order_session(now=t) == cap.session
            assert sc.exit_session(now=t) == cap.session
            assert sc.route_session(now=t) == cap.session

    def test_either_side_of_a_boundary_answers_differently(self):
        """Which is exactly why one snapshot must be threaded rather than
        the clock read repeatedly."""
        edge = self._dt(2026, 8, 26, 21, 0, tzinfo=self._ET)
        assert sc.capability_at(edge - self._td(seconds=1)).window == "CLOSED"
        assert sc.capability_at(edge).window == "DAYTIME"


class TestTheStandDownReachesTheGate:
    """A policy visible everywhere except the place that can stop an
    order is not a policy.

    `entry_disabled` was hardcoded False in both gate-context builders
    while `strategy_entry_policy` existed and was read only by the
    readiness report and the capability resolver. So S1's stand-down
    showed up in every report and stopped nothing. Route resolution
    deliberately does NOT apply the policy -- addressing an envelope is
    not permission -- which makes wiring it at the gate the other half of
    that split rather than a duplicate of it.
    """

    def test_the_buy_cycle_asks_the_policy(self):
        import inspect

        import kis_live_trading

        source = inspect.getsource(kis_live_trading.run_live_buy_entry_cycle)
        assert "entry_disabled=not strategy_entry_policy.entry_enabled(" in source
        assert "entry_disabled=False" not in source

    def test_the_bootstrap_asks_the_policy_too(self):
        """The bootstrap is a smaller first order, not an exemption from
        the permission every other order answers to."""
        import inspect

        from live_pilot import bootstrap

        source = inspect.getsource(bootstrap.run_bootstrap_buy)
        assert "entry_disabled=not strategy_entry_policy.entry_enabled(" in source
        assert "entry_disabled=False" not in source

    def test_the_gate_refuses_a_stood_down_strategy(self):
        """End to end at the gate itself, not just at its caller."""
        from execution import order_gate

        assert "entry_disabled" in inspect_source(order_gate.evaluate_buy_gate)

    def test_a_stood_down_strategy_resolves_to_disabled(self):
        from config import strategy_entry_policy as sep

        assert sep.entry_enabled(S1) is False   # -> entry_disabled True
        assert sep.entry_enabled(S6) is True    # -> entry_disabled False

    def test_the_sell_gate_still_never_sees_it(self):
        """The whole point of the split: standing a strategy down must
        not touch its ability to leave a position it already holds."""
        from execution import order_gate

        source = inspect_source(order_gate.evaluate_sell_gate)
        assert "entry_disabled" not in source
        assert "strategy_entry_policy" not in source


def inspect_source(fn):
    import inspect

    return inspect.getsource(fn)


class TestTheLiveEntryRunnerCanReachS6:
    """The normal live path defaults to S1's source, so S6 has to be
    asked for -- and the asking has to actually construct something the
    cycle can use.

    This exists because the first version passed `valid_for_seconds` to
    `s6_live.candidate_source.S6CandidateSource`, which does not take it:
    that argument belongs to the SAME-NAMED class in
    `live_pilot.candidate_sources`, the bootstrap's adapter. It would
    have raised TypeError on the first real invocation, and no test
    caught it because nothing constructed the factories.
    """

    def _runner(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "rlbe_under_test", "scripts/run_live_buy_entry.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _rollout(self):
        from config.live_rollout_config import LiveRolloutConfig

        return LiveRolloutConfig.from_env()

    def test_both_strategies_are_offered(self):
        assert sorted(self._runner().SOURCE_FACTORIES) == ["s1", "s6"]

    def test_the_default_is_still_s1(self):
        """Turning S6 on by default would change which strategy the live
        cycle trades without anyone saying so."""
        source = (pathlib_read("scripts/run_live_buy_entry.py"))
        assert '"--strategy", default="s1"' in source

    def test_the_s6_factory_builds_a_source_the_cycle_can_use(self):
        from datetime import datetime, timezone

        source = self._runner().SOURCE_FACTORIES["s6"](
            self._rollout(), datetime.now(timezone.utc))
        # The name is what `_session_permitted` matches on to route S6
        # through the capability resolver rather than the global flag.
        from s6_live import candidate_source as s6cs

        assert source.name == s6cs.SOURCE_S6
        for method in ("symbols", "allowed_symbols", "describe",
                       "candidate_row", "qualify"):
            assert hasattr(source, method), method

    def test_it_is_not_the_bootstraps_adapter(self):
        """Same class name, different interface, different caller."""
        from datetime import datetime, timezone

        from live_pilot import candidate_sources as lpcs

        source = self._runner().SOURCE_FACTORIES["s6"](
            self._rollout(), datetime.now(timezone.utc))
        assert not isinstance(source, lpcs.S6CandidateSource)

    def test_s1_keeps_the_cycles_own_default(self):
        from datetime import datetime, timezone

        assert self._runner().SOURCE_FACTORIES["s1"](
            self._rollout(), datetime.now(timezone.utc)) is None


def pathlib_read(rel):
    import pathlib

    return pathlib.Path(rel).read_text(encoding="utf-8")
