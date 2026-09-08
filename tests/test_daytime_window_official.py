"""The daytime (미국주간거래) window, pinned to KIS's published hours.

Source: KIS 해외주식 거래시간 안내 (truefriend.com, TF03ca050001), read
2026-09-08:

    표준시   주간거래 10:00-18:00 KST
    서머타임 주간거래 10:00-17:00 KST

The summer close moves so the Eastern close stays 04:00, which is when
the overnight venue actually stops. A request to widen the summer window
to 18:00 KST was declined on that evidence: 17:00-18:00 KST in summer is
04:00-05:00 ET, after the venue has closed and inside KIS's own premarket
window. If KIS changes the published table, change `_DST` in
config/kis_market_schedule.py and these expectations together.
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import kis_market_schedule as sched  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def kst(month, day, hour, minute):
    return datetime(2026, month, day, hour, minute, tzinfo=KST)


class TestSummerTime:
    """2026-09-09 (Wednesday) is under US daylight saving."""

    @pytest.mark.parametrize("hour,minute,expected", [
        (9, 59, sched.WINDOW_CLOSED),
        (10, 0, sched.WINDOW_DAYTIME),
        (16, 59, sched.WINDOW_DAYTIME),
        (17, 0, sched.WINDOW_PREMARKET),
        (17, 59, sched.WINDOW_PREMARKET),
        (18, 0, sched.WINDOW_PREMARKET),
    ])
    def test_the_boundaries(self, hour, minute, expected):
        assert sched.window_at(kst(9, 9, hour, minute)) == expected

    def test_the_summer_close_is_04_00_eastern(self):
        moment = kst(9, 9, 16, 59)
        assert moment.astimezone(sched.EASTERN).strftime("%H:%M") == "03:59"


class TestStandardTime:
    """2026-12-10 (Thursday) is on Eastern standard time."""

    @pytest.mark.parametrize("hour,minute,expected", [
        (9, 59, sched.WINDOW_CLOSED),
        (10, 0, sched.WINDOW_DAYTIME),
        (16, 59, sched.WINDOW_DAYTIME),
        (17, 0, sched.WINDOW_DAYTIME),
        (17, 59, sched.WINDOW_DAYTIME),
        (18, 0, sched.WINDOW_PREMARKET),
    ])
    def test_the_boundaries(self, hour, minute, expected):
        assert sched.window_at(kst(12, 10, hour, minute)) == expected

    def test_the_standard_close_is_04_00_eastern(self):
        moment = kst(12, 10, 17, 59)
        assert moment.astimezone(sched.EASTERN).strftime("%H:%M") == "03:59"


class TestOneSourceOfTruth:
    def test_every_consumer_derives_from_the_schedule(self):
        """No module keeps its own daytime close."""
        for path in ("config/session_capability.py", "execution/order_gate.py",
                     "live_pilot/route_verification_runner.py",
                     "scanners/base/scan_session.py", "trading_health_check.py"):
            text = (REPO_ROOT / path).read_text()
            assert "time(17" not in text and "time(18" not in text, path

    def test_the_tables_hold_the_published_values(self):
        from datetime import time

        assert sched._DST[sched.WINDOW_DAYTIME] == (time(10, 0), time(17, 0))
        assert sched._STANDARD[sched.WINDOW_DAYTIME] == (time(10, 0), time(18, 0))
