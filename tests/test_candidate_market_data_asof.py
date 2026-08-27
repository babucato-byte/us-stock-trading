"""A published candidate must say when its numbers were last true.

DT, 2026-08-26. The candidate file carried a new `generated_at` every
fifteen minutes for three hours while the price, volume, VWAP and EMAs
inside it were bit-identical -- 51.640, 7,932,617, 51.140, 51.602,
51.551 -- because the scanner was re-serving regular-session data after
the close. A consumer had no way to tell a fresh row from a fresh
timestamp, and one of them bought at 52.75 on a thesis computed at
51.64.

`market_data_asof` is that missing fact. It is never defaulted to
`generated_at`: doing so would restate the exact error it exists to
expose.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base.scanner_base import bar_timestamp as _bar_timestamp  # noqa: E402
from scanners.publish import candidates  # noqa: E402

ET = "America/New_York"


class _Signal:
    def __init__(self, symbol="DT", score=72.82, metrics=None, **kw):
        self.symbol = symbol
        self.scanner_score = score
        self.signal_price = 51.64
        self.metrics = metrics or {}
        for key, value in kw.items():
            setattr(self, key, value)


class TestTheRowCarriesBothTimestamps:
    def test_market_data_asof_comes_from_the_producer(self):
        rows = candidates.build_rows(
            [_Signal(metrics={"market_data_asof": "2026-08-26T19:55:00+00:00"})],
            strategy_id="S6_ORB_BREAKOUT_V1", trading_day="2026-08-26",
            session="AFTER_HOURS", generated_at="2026-08-26T20:38:49+00:00")
        assert rows[0].generated_at == "2026-08-26T20:38:49+00:00"
        assert rows[0].market_data_asof == "2026-08-26T19:55:00+00:00"

    def test_the_DT_gap_is_visible_on_the_row(self):
        """43 minutes between publication and observation, on one row."""
        from datetime import datetime

        rows = candidates.build_rows(
            [_Signal(metrics={"market_data_asof": "2026-08-26T19:55:00+00:00"})],
            strategy_id="S6_ORB_BREAKOUT_V1", trading_day="2026-08-26",
            session="AFTER_HOURS", generated_at="2026-08-26T20:38:49+00:00")
        published = datetime.fromisoformat(rows[0].generated_at)
        observed = datetime.fromisoformat(rows[0].market_data_asof)
        assert (published - observed).total_seconds() > 2400

    def test_it_is_never_defaulted_to_generated_at(self):
        """A row whose market age is unknown must say so rather than
        claim to be as fresh as its own timestamp."""
        rows = candidates.build_rows(
            [_Signal()], strategy_id="S6_ORB_BREAKOUT_V1",
            trading_day="2026-08-26", session="REGULAR",
            generated_at="2026-08-26T20:38:49+00:00")
        assert rows[0].market_data_asof is None
        assert rows[0].generated_at == "2026-08-26T20:38:49+00:00"

    def test_a_signal_attribute_is_also_accepted(self):
        rows = candidates.build_rows(
            [_Signal(market_data_asof="2026-08-26T19:55:00+00:00")],
            strategy_id="S6_ORB_BREAKOUT_V1", trading_day="2026-08-26",
            session="REGULAR")
        assert rows[0].market_data_asof == "2026-08-26T19:55:00+00:00"

    def test_the_field_survives_a_round_trip(self):
        rows = candidates.build_rows(
            [_Signal(metrics={"market_data_asof": "2026-08-26T19:55:00+00:00"})],
            strategy_id="S6_ORB_BREAKOUT_V1", trading_day="2026-08-26",
            session="REGULAR")
        payload = rows[0].to_dict() if hasattr(rows[0], "to_dict") else None
        if payload is not None:
            assert payload["market_data_asof"] == "2026-08-26T19:55:00+00:00"


class TestTheBarTimestamp:
    def _frame(self, start="2026-08-26 15:55", periods=2):
        index = pd.date_range(start=start, periods=periods, freq="5min", tz=ET)
        return pd.DataFrame({"Close": [1.0] * periods}, index=index)

    def test_it_is_the_newest_bar_in_utc(self):
        assert _bar_timestamp(self._frame()) == "2026-08-26T20:00:00+00:00"

    def test_an_empty_frame_is_None_not_a_guess(self):
        assert _bar_timestamp(None) is None
        assert _bar_timestamp(self._frame(periods=0)) is None

    def test_a_broken_frame_is_None_rather_than_raising(self):
        class _Bad:
            def __len__(self):
                return 1

            @property
            def index(self):
                raise RuntimeError("boom")

        assert _bar_timestamp(_Bad()) is None

    def test_the_orb_scanner_publishes_it(self):
        import inspect

        from scanners.orb import scanner

        source = inspect.getsource(scanner)
        assert '"market_data_asof": bar_timestamp(session)' in source
