"""Every session boundary, and what is true on each side of it.

The whole point of the expansion is that four sessions run the SAME
strategy continuously, so the thing worth pinning is that each boundary
moves the session and the operational trading day correctly, and that
dating a session never by itself grants permission to trade in it.
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import operational_calendar as cal  # noqa: E402
from config import session_capability  # noqa: E402
from scanners.base import scan_session  # noqa: E402

ET = ZoneInfo("America/New_York")


def _at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


#: (label, moment, expected clock session, expected operational day)
MATRIX = [
    ("Sun 20:00 -> Monday daytime", _at(2026, 8, 30, 20, 0),
     "OVERNIGHT_DAYTIME", None),          # KIS daytime opens 21:00 ET
    ("Sun 21:30 daytime open",      _at(2026, 8, 30, 21, 30),
     "OVERNIGHT_DAYTIME", "2026-08-31"),
    ("Mon 03:59 daytime",           _at(2026, 8, 31, 3, 59),
     "OVERNIGHT_DAYTIME", "2026-08-31"),
    ("Mon 04:00 premarket",         _at(2026, 8, 31, 4, 0),
     "PREMARKET", "2026-08-31"),
    ("Mon 09:29 premarket",         _at(2026, 8, 31, 9, 29),
     "PREMARKET", "2026-08-31"),
    ("Mon 09:30 regular",           _at(2026, 8, 31, 9, 30),
     "REGULAR", "2026-08-31"),
    ("Mon 15:59 regular",           _at(2026, 8, 31, 15, 59),
     "REGULAR", "2026-08-31"),
    ("Mon 16:00 after hours",       _at(2026, 8, 31, 16, 0),
     "AFTER_HOURS", "2026-08-31"),
    ("Mon 19:59 after hours",       _at(2026, 8, 31, 19, 59),
     "AFTER_HOURS", "2026-08-31"),
]


class TestTheClockSessionAtEveryBoundary:
    @pytest.mark.parametrize("label,moment,session,_day", MATRIX,
                             ids=[m[0] for m in MATRIX])
    def test_the_session_is_what_the_schedule_says(self, label, moment,
                                                   session, _day):
        assert scan_session.session_at(moment) == session

    def test_twenty_hundred_starts_the_next_overnight_context(self):
        """The clock rolls into OVERNIGHT_DAYTIME at 20:00 ET even though
        KIS's daytime window does not open until 21:00 under DST -- which
        is the disagreement that produced a Sunday trading day."""
        assert scan_session.session_at(_at(2026, 8, 31, 20, 0)) \
            == "OVERNIGHT_DAYTIME"


class TestTheOperationalTradingDayAtEveryBoundary:
    @pytest.mark.parametrize("label,moment,_session,day", MATRIX,
                             ids=[m[0] for m in MATRIX])
    def test_the_day_is_resolved_or_honestly_absent(self, label, moment,
                                                    _session, day):
        assert cal.resolve_operational_trading_day(moment)[
            "operational_trading_day"] == day

    def test_every_resolved_day_is_a_real_trading_day(self):
        from config import kis_market_schedule as ks

        for _label, moment, _session, day in MATRIX:
            if day is not None:
                assert ks.is_trading_day(day)

    def test_all_four_sessions_share_one_trading_day(self):
        """One strategy across four sessions: work done in any of them on
        Monday belongs to Monday."""
        days = {cal.resolve_operational_trading_day(m)[
            "operational_trading_day"]
            for _l, m, _s, d in MATRIX if d is not None}
        assert days == {"2026-08-31"}


class TestDatingNeverGrantsPermission:
    """A calendar mapping must not create an order route."""

    def test_a_resolved_day_before_the_window_opens_permits_nothing(self):
        out = cal.resolve_operational_trading_day(_at(2026, 8, 30, 20, 30))
        assert out["entry_supported"] is False
        assert out["exit_supported"] is False

    def test_friday_evening_dates_to_monday_and_still_refuses(self):
        out = cal.resolve_operational_trading_day(_at(2026, 8, 28, 21, 30))
        assert out["operational_trading_day"] == "2026-08-31"
        assert out["orders_allowed"] is False

    def test_a_holiday_eve_dates_forward_and_still_refuses(self):
        out = cal.resolve_operational_trading_day(_at(2026, 7, 2, 21, 30))
        assert out["operational_trading_day"] == "2026-07-06"
        assert out["orders_allowed"] is False

    @pytest.mark.parametrize("label,moment,_session,day", MATRIX,
                             ids=[m[0] for m in MATRIX])
    def test_capability_is_the_only_authority_on_orders(self, label, moment,
                                                        _session, day):
        """Whatever the calendar says, entry/exit permission comes from
        `session_capability` and nowhere else."""
        resolved = cal.resolve_operational_trading_day(moment)
        capability = session_capability.capability_at(moment)
        if resolved["operational_trading_day"] is not None:
            assert resolved["entry_supported"] == bool(
                capability.entry_supported)
            assert resolved["exit_supported"] == bool(capability.exit_supported)


class TestDiscoveryIsPossibleInEverySession:
    """§4: no session may depend on another having produced anything."""

    @pytest.mark.parametrize("session", ["OVERNIGHT_DAYTIME", "PREMARKET",
                                         "REGULAR", "AFTER_HOURS"])
    def test_a_pool_is_built_with_no_manifest_and_no_prior(self, session,
                                                           tmp_path):
        import json

        from s6_live import session_discovery as sd

        directory = tmp_path / "activity"
        directory.mkdir(parents=True)
        (directory / "yfinance.json").write_text(json.dumps({"symbols": {
            f"S{i}": {"dollar_volume": 1e9 - i, "price": 5.0}
            for i in range(60)}}))
        pool = sd.build_pool(
            session=session, operational_trading_day="2026-08-31", limit=41,
            held_symbols=(), prior_symbols=(),
            env={"SCANNER_ACTIVITY_DIR": str(directory)})
        assert len(pool["symbols"]) == 41
        assert pool["from_coarse"] == 41
        assert pool["reason"] == sd.OK
