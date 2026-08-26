"""The four sessions: a label on a run, never a condition on a scanner.

Two properties carry the weight here.

The four buckets must PARTITION the clock -- no gap, no overlap -- because
a per-session comparison is only meaningful if every scan lands in
exactly one of them. A gap would silently drop runs; an overlap would
double-count them.

And scanning must never imply permission to trade. PREMARKET and
AFTER_HOURS are scannable and have no verified order route, and the code
has to say so rather than leave a reader to infer it from the fact that a
scan happened at all.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_hours import EASTERN  # noqa: E402
from scanners.base import scan_session as ss  # noqa: E402


def et(hour, minute=0, day=19):
    return datetime(2026, 8, day, hour, minute, tzinfo=EASTERN)


class TestTheBucketsPartitionTheClock:
    @pytest.mark.parametrize("hour,minute,expected", [
        (4, 0, ss.PREMARKET),        # the boundary belongs to the later bucket
        (7, 30, ss.PREMARKET),
        (9, 29, ss.PREMARKET),
        (9, 30, ss.REGULAR),
        (12, 0, ss.REGULAR),
        (15, 59, ss.REGULAR),
        (16, 0, ss.AFTER_HOURS),
        (18, 0, ss.AFTER_HOURS),
        (19, 59, ss.AFTER_HOURS),
        (20, 0, ss.OVERNIGHT_DAYTIME),
        (23, 30, ss.OVERNIGHT_DAYTIME),
        (0, 30, ss.OVERNIGHT_DAYTIME),
        (3, 59, ss.OVERNIGHT_DAYTIME),
    ])
    def test_each_moment_lands_in_one_bucket(self, hour, minute, expected):
        assert ss.session_at(et(hour, minute)) == expected

    def test_every_minute_of_the_day_is_covered_exactly_once(self):
        """The property, not a sample of it."""
        seen = set()
        for hour in range(24):
            for minute in range(60):
                bucket = ss.session_at(et(hour, minute))
                assert bucket in ss.SESSIONS, (hour, minute, bucket)
                seen.add(bucket)
        assert seen == set(ss.SESSIONS), "a bucket no minute reaches is dead"

    def test_a_naive_datetime_is_read_as_eastern(self):
        """Not as UTC. A naive 10:00 treated as UTC is 06:00 ET, which
        files a regular-hours scan under PREMARKET."""
        assert ss.session_at(datetime(2026, 8, 19, 10, 0)) == ss.REGULAR

    def test_a_utc_datetime_is_converted_not_truncated(self):
        from datetime import timezone

        # 20:00 UTC == 16:00 ET == the first minute of after-hours.
        assert ss.session_at(
            datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)) == ss.AFTER_HOURS

    def test_a_holiday_still_has_sessions(self):
        """The clock does not stop on a holiday, and a scan that ran at
        07:00 ran in the premarket window. Whether the market was OPEN is
        a different question, asked of market_hours -- folding it in here
        would relabel every holiday scan as OVERNIGHT_DAYTIME and move it
        into the one off-hours bucket that is order-verified."""
        july4 = datetime(2026, 7, 3, 7, 0, tzinfo=EASTERN)  # observed holiday
        assert ss.session_at(july4) == ss.PREMARKET


class TestNormalizeRefusesToGuess:
    @pytest.mark.parametrize("given,expected", [
        ("REGULAR", ss.REGULAR),
        ("regular", ss.REGULAR),
        ("  Regular  ", ss.REGULAR),
        ("after-hours", ss.AFTER_HOURS),
        ("after hours", ss.AFTER_HOURS),
        ("OVERNIGHT_DAYTIME", ss.OVERNIGHT_DAYTIME),
    ])
    def test_case_and_separators_are_forgiven(self, given, expected):
        assert ss.normalize(given) == expected

    @pytest.mark.parametrize("given", [
        "REGULARR", "REG", "DAYTIME", "OVERNIGHT", "AFTERMARKET", "", None, 7])
    def test_anything_else_is_none_not_a_near_miss(self, given):
        """A typo that became REGULAR would file an off-hours scan under
        the one session allowed to trade."""
        assert ss.normalize(given) is None

    def test_the_two_halves_of_the_combined_bucket_are_not_sessions(self):
        """OVERNIGHT_DAYTIME is one bucket because the venue treats it as
        one. Accepting either half alone would invent a distinction the
        order path does not make."""
        assert ss.normalize("OVERNIGHT") is None
        assert ss.normalize("DAYTIME") is None


class TestScanningIsNotPermissionToTrade:
    """All four sessions now have a specified order route -- premarket
    and aftermarket share the general endpoint and TR family with the
    regular session, which the overseas order API documents.

    Excluding them rested on "no premarket-specific TR exists", which
    read a SHARED route as a missing one. So this class no longer
    separates scanning from ordering by SESSION. The separation it
    protects is real and still here; it moved to the clock and to the
    layers above, which the last three tests pin.
    """

    def test_every_scanned_session_has_a_specified_route(self):
        assert ss.ORDER_VERIFIED_SESSIONS == {
            ss.PREMARKET, ss.REGULAR, ss.AFTER_HOURS, ss.OVERNIGHT_DAYTIME}

    @pytest.mark.parametrize(
        "session", [ss.PREMARKET, ss.REGULAR, ss.AFTER_HOURS, ss.OVERNIGHT_DAYTIME])
    def test_routed_sessions_say_so(self, session):
        assert ss.order_route_verified(session) is True
        assert ss.execution_status(session) == ss.STATUS_ORDER_VERIFIED

    def test_an_unknown_session_fails_closed(self):
        """Not verified is the safe answer to a question about a session
        nobody defined."""
        for bogus in ("REGULARR", "", None, "DAYTIME"):
            assert ss.order_route_verified(bogus) is False

    def test_a_specified_route_is_not_an_open_market(self):
        """The separation that replaced the session-set one. Having a
        route says nothing about the hour: under DST, 20:00-21:00 ET is
        09:00-10:00 KST, after the aftermarket extension and before
        주간거래 opens, and KIS runs no window there at all."""
        from datetime import datetime

        from config import session_capability as sc
        from market_hours import EASTERN

        assert ss.order_route_verified(ss.OVERNIGHT_DAYTIME) is True
        closed = sc.capability_at(datetime(2026, 8, 26, 20, 30, tzinfo=EASTERN))
        assert closed.orders_allowed is False

    def test_a_specified_route_is_not_a_confirmed_one(self):
        """Route SPECIFIED and wire values CONFIRMED BY A LIVE RESPONSE
        are different facts, and the two families now DEMONSTRATE it by
        differing: both have a specified route, only one has been
        confirmed by a real KIS answer."""
        from config import session_capability as sc

        assert ss.order_route_verified(ss.OVERNIGHT_DAYTIME) is True
        assert sc.route_awaiting_live_evidence(sc.FAMILY_DAYTIME) is True
        assert sc.route_awaiting_live_evidence(sc.FAMILY_GENERAL) is False

    def test_a_specified_route_is_not_permission(self):
        """`s6_sessions.LIVE_SESSIONS` is the rollout, and
        `strategy_entry_policy` can stand a strategy down on top of it.
        Route, rollout and permission remain three gates."""
        from config import strategy_entry_policy as sep

        assert sep.entry_enabled("S1_HMA_EARLY_TREND_V1") is False
        assert sep.exit_enabled("S1_HMA_EARLY_TREND_V1") is True


class TestItChangesNoScannerCondition:
    def test_the_session_module_imports_no_scanner(self):
        """A session is a label. If this module could reach a scanner it
        could change what one looks for, which is the thing it must not
        do."""
        import ast

        source = (REPO_ROOT / "scanners" / "base" / "scan_session.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    assert "accumulation" not in name
                    assert "scanner" not in name.split(".")[-1].lower() \
                        or name.startswith("scanners.base")

    def test_s2_thresholds_are_untouched_by_session_work(self):
        """The conditions live in config.json and stay there, at the
        values that were measured."""
        import json

        config = json.loads(
            (REPO_ROOT / "scanners" / "accumulation" / "config.json").read_text())["params"]
        assert config["volume_multiple_min"] == 1.5
        assert config["price_change_max_pct"] == 8.0
        assert config["require_price_above_hma200"] is True
        assert config["require_hma200_rising"] is True
        assert config["score_weight_volume"] == 40
        assert config["score_weight_quietness"] == 30
        assert config["score_weight_efficiency"] == 20
        assert config["score_weight_trend"] == 10

    def test_price_change_max_is_a_ceiling_with_no_floor(self):
        """§5: positive price confirmation is an execution-time question.
        A floor here would change what S2 measures -- the score rewards
        quietness, and a scanner that required a rise would be a
        different, unmeasured strategy wearing the same name."""
        import json

        raw = json.loads(
            (REPO_ROOT / "scanners" / "accumulation" / "config.json").read_text())
        config = raw["params"]
        assert "price_change_min_pct" not in config
        assert "require_positive_price_change" not in config
        # The scanner's own config says why, and that sentence is the
        # record of a deliberate choice rather than an oversight.
        assert "no LOWER bound on price_change_pct" in raw["description"]
