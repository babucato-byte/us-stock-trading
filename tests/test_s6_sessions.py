"""S6 scans everywhere; S6 orders in one place.

The property this file defends is that those two are decided
SEPARATELY. Conflating them is how a scan-only session quietly becomes a
live one -- and the tempting shortcut is exactly the wrong one, because
OVERNIGHT_DAYTIME's broker route IS verified and it still must not
trade. A verified route is a precondition for trading a session, not a
decision to.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import position_limits as pl  # noqa: E402
from config import s6_sessions as s6  # noqa: E402
from config import scanner_live_mode as slm  # noqa: E402

S1 = "S1_HMA_EARLY_TREND_V1"
S2 = "S2_VOLUME_ACCUMULATION_V1"
S6 = "S6_ORB_BREAKOUT_V1"


class TestScanningIsAllSession:
    @pytest.mark.parametrize("session,variant", [
        ("REGULAR", "S6-R"), ("OVERNIGHT_DAYTIME", "S6-O"),
        ("PREMARKET", "S6-P"), ("AFTER_HOURS", "S6-A")])
    def test_every_session_has_its_own_variant(self, session, variant):
        assert s6.variant_for(session) == variant
        assert s6.scans(session) is True

    def test_the_variants_cover_the_four_clock_sessions(self):
        from scanners.base import scan_session

        assert set(s6.SCAN_SESSIONS) == set(scan_session.SESSIONS)

    def test_every_variant_is_distinct(self):
        """A candidate must always be able to say which range produced
        it; two sessions sharing a variant would lose that."""
        assert len(set(s6.VARIANT_BY_SESSION.values())) == 4

    def test_an_unknown_session_has_no_variant(self):
        assert s6.variant_for("DAILY") == ""
        assert s6.scans(None) is False


class TestOrderingIsTheSpecifiedSessions:
    """Ordering is permitted in the sessions whose KIS route the official
    specification defines, and in no others.

    This class read "REGULAR only", then "REGULAR and daytime". It now
    reads "all four", because the overseas order API documents US orders
    in the premarket and aftermarket too -- they share the general
    endpoint and TR family with the regular session. The old reading
    inferred from "no premarket-specific TR exists" that premarket could
    not be ordered in at all, which inverted what the absence meant.

    The invariant this protects was never a NUMBER. It is that ordering
    is strictly narrower than scanning, chosen by evidence rather than
    convenience. That is still true -- but the narrowing has moved from
    the session set to the CLOCK. S6 scans in every session whatever the
    hour; it may order only inside a window KIS actually runs, which
    excludes the closed hour between the aftermarket extension and
    daytime, and excludes the extension itself.
    """

    ORDER_CAPABLE = {"PREMARKET", "REGULAR", "AFTER_HOURS", "OVERNIGHT_DAYTIME"}
    #: Never orderable at any hour: not sessions at all, or a window
    #: whose API support has not been established.
    NEVER_ORDER_CAPABLE = ("CLOSED", "AFTERMARKET_EXTENSION", "", None)

    def test_exactly_the_specified_sessions_may_order(self):
        assert s6.LIVE_SESSIONS == self.ORDER_CAPABLE
        for session in sorted(self.ORDER_CAPABLE):
            assert s6.orders_allowed(session) is True

    @pytest.mark.parametrize("session", NEVER_ORDER_CAPABLE)
    def test_a_non_session_is_shadow(self, session):
        assert s6.orders_allowed(session) is False
        assert s6.mode_for(session) == s6.MODE_REALTIME_SHADOW

    @pytest.mark.parametrize("session", ["CLOSED", "AFTERMARKET_EXTENSION", ""])
    def test_a_non_session_is_refused_by_the_broker_too(self, session):
        """Refused at the route resolver as well as by policy. Two
        independent refusals, because a policy list can be edited and the
        absence of an endpoint cannot."""
        from brokers.kis_broker import KISBrokerError, order_route_for

        with pytest.raises(KISBrokerError):
            order_route_for(session, "live", "buy")

    def test_the_clock_still_narrows_what_the_session_set_permits(self):
        """The separation that replaced the session-set one: being in
        LIVE_SESSIONS is not being inside a KIS window."""
        from datetime import datetime

        from config import session_capability as sc
        from market_hours import EASTERN

        # 20:30 ET under DST is 09:30 KST -- after the aftermarket
        # extension, before 주간거래 opens. A fixed-ET daytime boundary
        # called this OVERNIGHT_DAYTIME and permitted an order.
        closed = sc.capability_at(datetime(2026, 8, 26, 20, 30, tzinfo=EASTERN))
        assert closed.entry_supported is False
        assert closed.exit_supported is False
        assert closed.session == ""

    def test_a_verified_route_is_still_not_permission_to_trade(self):
        """The property that mattered when this said REGULAR-only, and
        still matters now that two sessions are routed.

        Both routed sessions have a verified route AND are in
        LIVE_SESSIONS, and neither may place an order today: S6 is
        DISCOVERY_ONLY, and each session's wire values are separately
        unconfirmed. Route, session permission and promotion are three
        gates, not one.
        """
        from config import scanner_live_mode as slm
        from scanners.base import scan_session

        for session in sorted(self.ORDER_CAPABLE):
            assert scan_session.order_route_verified(session) is True
            assert s6.orders_allowed(session) is True

        # ...and a verified route is STILL not permission on its own:
        # hold the strategy down and both capable sessions go quiet
        # without any session change. Route, session permission and
        # promotion remain three gates.
        stood_down = dict(slm.SCANNER_LIVE_MODE)
        stood_down["orb"] = slm.MODE_DISCOVERY_ONLY
        assert slm.is_limited_live("orb", stood_down) is False

    def test_session_permission_is_not_bootstrap_evidence(self):
        """A session may be permitted while its wire values have never
        been confirmed by a real response. Conflating the two would let
        an edit to a policy list stand in for a KIS answer."""
        from brokers.kis_broker import (
            LIVE_RESPONSE_PENDING,
            REQUIRED_FOR_ARMED,
            REQUIRED_FOR_DAYTIME,
            matrix_entries_for,
        )

        # The GENERAL route was confirmed by the 2026-08-26 premarket
        # bootstrap; the DAYTIME route still awaits a response of its
        # own. Which is the point: permission and evidence move
        # independently, per ROUTE.
        general = [e for e in matrix_entries_for(REQUIRED_FOR_ARMED)
                   if e.live_status == LIVE_RESPONSE_PENDING]
        daytime = [e for e in matrix_entries_for(REQUIRED_FOR_DAYTIME)
                   if e.live_status == LIVE_RESPONSE_PENDING]
        assert general == []
        assert daytime, "DAYTIME has no outstanding evidence"

    def test_the_two_sessions_evidence_sets_are_disjoint(self):
        """Neither session's pending evidence may block the other."""
        from brokers.kis_broker import (
            REQUIRED_FOR_ARMED,
            REQUIRED_FOR_DAYTIME,
            matrix_entries_for,
        )

        armed = {e.name for e in matrix_entries_for(REQUIRED_FOR_ARMED)}
        daytime = {e.name for e in matrix_entries_for(REQUIRED_FOR_DAYTIME)}
        assert armed and daytime
        assert armed.isdisjoint(daytime)

    def test_scanning_and_ordering_are_separated_by_the_clock(self):
        """The session SETS are now equal -- every session S6 scans has a
        route. The separation did not collapse; it moved to the clock,
        and asserting the old set inequality here would have forced the
        premarket correction to be reverted to keep a test green.

        Scanning is calendar- and clock-independent by design; ordering
        is confined to a window KIS actually runs.
        """
        from datetime import datetime

        from config import session_capability as sc
        from market_hours import EASTERN

        assert s6.SCAN_SESSIONS == s6.LIVE_SESSIONS
        # Hours inside a scanned session that are still not orderable.
        for moment in (datetime(2026, 8, 26, 20, 30, tzinfo=EASTERN),   # closed
                       datetime(2026, 8, 26, 18, 30, tzinfo=EASTERN),   # extension
                       datetime(2026, 8, 29, 22, 0, tzinfo=EASTERN)):   # Saturday
            assert sc.capability_at(moment).orders_allowed is False

    def test_the_routed_sessions_report_limited_live(self):
        assert s6.mode_for("REGULAR") == s6.MODE_LIMITED_LIVE
        assert s6.mode_for("OVERNIGHT_DAYTIME") == s6.MODE_LIMITED_LIVE


class TestTheRangeIsNotSilentlyCopied:
    def test_regular_keeps_the_measured_orb(self):
        import json

        config = json.loads(
            (REPO_ROOT / "scanners" / "orb" / "config.json").read_text())
        assert config["params"]["orb_minutes"] == s6.REGULAR_ORB_MINUTES == 15

    def test_the_other_sessions_carry_a_comparison_not_a_decision(self):
        """15 leads because it is the measured REGULAR value. Inheriting
        it silently would make every session use a number chosen for a
        different one."""
        assert s6.SHADOW_RANGE_MINUTES == (5, 15, 30)
        assert s6.REGULAR_ORB_MINUTES in s6.SHADOW_RANGE_MINUTES

    def test_the_scanner_already_supports_those_windows(self):
        import json

        config = json.loads(
            (REPO_ROOT / "scanners" / "orb" / "config.json").read_text())
        assert config["params"]["supported_orb_minutes"] == [5, 15, 30]


class TestTheStrategyTransition:
    def test_s1_is_untouched(self):
        assert slm.SCANNER_LIVE_MODE["hma_early_trend"] == "DISCOVERY_ONLY"
        assert pl.check_entry(S1, {}).allowed is True
        assert pl.check_entry(S1, {S1: 1}).allowed is False

    def test_s2_is_back_to_discovery_only(self):
        assert slm.SCANNER_LIVE_MODE["accumulation"] == "DISCOVERY_ONLY"
        assert slm.is_limited_live("accumulation") is False

    def test_s2_can_no_longer_take_a_position(self):
        decision = pl.check_entry(S2, {})
        assert decision.allowed is False
        assert decision.reason == pl.BLOCK_UNKNOWN_STRATEGY

    def test_s2_is_absent_from_the_matrix_rather_than_zero(self):
        """Absent means "no agreed limit" and refuses; a 0 would read as
        a limit that was decided and happens to be zero. Those need
        different operator responses."""
        assert S2 not in pl.PROPOSED_STRATEGY_MAX

    def test_s2s_infrastructure_is_kept(self):
        """§21: the code is not deleted -- S6 reuses all of it."""
        for module in ("s2_live.candidate_source", "s2_live.exit_policy",
                       "s2_live.position_store", "s2_live.trade_record",
                       "scanners.publish.candidates",
                       "reconciliation.internal_holdings"):
            __import__(module)

    def test_s6_has_a_place_in_the_matrix(self):
        assert pl.PROPOSED_STRATEGY_MAX[S6] == 1
        assert pl.effective_limits() == (2, {S1: 1, S6: 1})

    def test_s6_went_live_only_after_its_machinery_existed(self):
        """§25 Phase 1 puts LIMITED_LIVE after the lifecycle is built.
        Flipping the mode first would mark a strategy tradeable while
        nothing could manage a position it opened -- so the promotion is
        asserted TOGETHER with the machinery that justifies it."""
        import importlib

        for module in ("s6_live.qualification", "s6_live.position_store",
                       "s6_live.exit_policy", "s6_live.exit_runtime"):
            assert importlib.import_module(module) is not None
        assert slm.SCANNER_LIVE_MODE["orb"] == "LIMITED_LIVE"


class TestTheApprovedRiskCases:
    """§12, every case."""

    @pytest.mark.parametrize("strategy,book,allowed", [
        (S6, {S1: 1}, True),
        (S1, {S6: 1}, True),
        (S1, {S1: 1}, False),
        (S6, {S6: 1}, False),
        (S6, {S1: 1, S6: 1}, False),
        (S2, {}, False),
    ])
    def test_case(self, strategy, book, allowed):
        assert pl.check_entry(strategy, book).allowed is allowed

    def test_one_of_each_is_the_full_book(self):
        assert pl.check_entry(S6, {S1: 1}).allowed is True
        assert pl.check_entry(S1, {S6: 1}).allowed is True
        assert pl.PROPOSED_GLOBAL_MAX == 2

    def test_a_third_strategy_is_blocked_globally(self):
        decision = pl.check_entry(S6, {S1: 1, "S9_OTHER": 1})
        assert decision.allowed is False
        assert decision.reason == pl.BLOCK_GLOBAL
