"""S6 across four sessions: one scanner, four independent ranges.

REGULAR is not being changed. S6-R is the measured ORB v1.0 and takes
the original code path byte for byte; the test that matters most here is
the one asserting a REGULAR run still reaches `sess.slice_session`, not
the new engine.

The other three route through the session-aware engine, which is the
only thing that knows a 20:00->04:00 window wraps midnight. What must
never happen is a variant reading REGULAR's range: a breakout of 09:30's
level means nothing at 20:00.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_sessions as s6  # noqa: E402
from market_hours import EASTERN  # noqa: E402
from scanners.base.models import ScannerDataError  # noqa: E402
from scanners.registry import build_scanner  # noqa: E402

SCANNER = build_scanner("orb")


def minute_bars(*specs):
    index = [pd.Timestamp(t, tz=EASTERN) for t, _h, _l in specs]
    return pd.DataFrame(
        {"Open": [h for _t, h, _l in specs],
         "High": [h for _t, h, _l in specs],
         "Low": [l for _t, _h, l in specs],
         "Close": [h for _t, h, _l in specs],
         "Volume": [10_000] * len(specs)},
        index=index)


class Data:
    def __init__(self, intraday, symbol="ABC"):
        self.symbol, self.intraday, self.daily = symbol, intraday, None


class TestRegularKeepsItsOriginalPath:
    def test_the_regular_branch_calls_the_original_slicer(self):
        """S6-R is ORB v1.0 and is not being modified."""
        source = (REPO_ROOT / "scanners" / "orb" / "scanner.py").read_text()
        head = source[source.index('requested = str(context.get("session")'):]
        regular_block = head[:head.index("else:")]
        assert "sess.slice_session(" in regular_block
        assert "session_range" not in regular_block

    def test_an_absent_session_is_treated_as_regular(self):
        """Every existing caller passes no session and must be
        unaffected."""
        source = (REPO_ROOT / "scanners" / "orb" / "scanner.py").read_text()
        assert 'context.get("session") or "REGULAR"' in source

    def test_the_regular_config_is_untouched(self):
        import json

        params = json.loads(
            (REPO_ROOT / "scanners" / "orb" / "config.json").read_text())["params"]
        assert params["orb_minutes"] == 15
        assert params["require_close_breakout"] is True
        assert params["require_price_above_vwap"] is True
        assert params["require_ema9_above_ema21"] is True
        assert params["volume_expansion_min"] == 1.2
        assert params["max_extension_above_or_high_pct"] == 6.0


class TestEachVariantUsesItsOwnRange:
    def all_sessions(self):
        """One frame with bars in all four windows."""
        return minute_bars(
            ("2026-08-21 04:05", 101, 100), ("2026-08-21 04:20", 102, 99),
            ("2026-08-21 09:35", 111, 110), ("2026-08-21 09:50", 112, 109),
            ("2026-08-21 16:05", 121, 120), ("2026-08-21 16:20", 122, 119),
            ("2026-08-21 20:05", 131, 130), ("2026-08-22 01:00", 132, 129),
        )

    @pytest.mark.parametrize("session,high,low", [
        ("PREMARKET", 102, 99),
        ("AFTER_HOURS", 122, 119),
        ("OVERNIGHT_DAYTIME", 132, 129),
    ])
    def test_a_variant_sees_only_its_own_session(self, session, high, low):
        from scanners.base import session_range as sr

        window = sr.opening_range(self.all_sessions(), session, minutes=600)
        assert window.range_high == high
        assert window.range_low == low

    def test_no_variant_ever_sees_regulars_range(self):
        """The failure the whole family design exists to prevent."""
        from scanners.base import session_range as sr

        for session in ("PREMARKET", "AFTER_HOURS", "OVERNIGHT_DAYTIME"):
            window = sr.opening_range(self.all_sessions(), session, minutes=600)
            assert window.range_high != 112, session

    def test_every_session_maps_to_a_distinct_variant(self):
        variants = {s: s6.variant_for(s) for s in s6.SCAN_SESSIONS}
        assert len(set(variants.values())) == 4
        assert variants["REGULAR"] == "S6-R"


class TestAnUnformedRangeIsNotARejection:
    def test_a_session_with_no_bars_raises_a_data_error(self):
        """Not a market judgement: a session that has not opened has
        nothing to say, and recording it as "no setups" would make an
        early scan look like a quiet session."""
        with pytest.raises(ScannerDataError, match="PREMARKET"):
            SCANNER.check(None, Data(minute_bars(
                ("2026-08-21 09:35", 111, 110))),
                {"session": "PREMARKET"})

    def test_an_empty_frame_raises_rather_than_rejecting(self):
        with pytest.raises(ScannerDataError):
            SCANNER.check(None, Data(pd.DataFrame()),
                          {"session": "AFTER_HOURS"})


class TestTheSessionMatrixIsHonoured:
    def test_only_the_routed_sessions_may_order(self):
        """REGULAR and OVERNIGHT_DAYTIME have a specified KIS order
        route; the other two have none, so they stay shadow no matter
        what the scanner finds in them."""
        for session in ("REGULAR", "OVERNIGHT_DAYTIME"):
            assert s6.orders_allowed(session) is True
        for session in ("PREMARKET", "AFTER_HOURS"):
            assert s6.orders_allowed(session) is False
            assert s6.mode_for(session) == s6.MODE_REALTIME_SHADOW

    def test_s6_is_live_only_now_that_its_lifecycle_exists(self):
        """§11: the mode goes up only once the whole lifecycle exists.

        It now does -- qualification, position store, exit policy and
        runtime -- so `orb` is LIMITED_LIVE. Pinned rather than removed,
        so an accidental change to either strategy still trips.
        """
        from config import scanner_live_mode

        assert scanner_live_mode.SCANNER_LIVE_MODE["orb"] == "LIMITED_LIVE"
