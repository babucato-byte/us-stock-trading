"""One-minute bars from KIS, including the hours yfinance cannot see.

S6's premarket scan on 2026-08-31: universe 83, DATA_ERROR 77, evaluated
6, signals 0. Ninety-three percent of the candidates died before any
strategy rule was applied, because the provider has no usable premarket
intraday data. KIS does -- measured against the live account at 05:02
ET, 120 bars with real premarket volume.

These tests are mostly about the wire format, because each of its three
surprises fails silently: newest-first ordering that an EMA will happily
compute backwards, gaps that are the market rather than missing data,
and a response that reaches back across midnight into another session.
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import kis_minute_chart as chart  # noqa: E402

ET = ZoneInfo("America/New_York")


def _row(ymd, hms, *, close="10.00", volume="100", high=None, low=None,
         open_=None):
    return {"xymd": ymd, "xhms": hms, "kymd": ymd, "khms": hms,
            "open": open_ or close, "high": high or close,
            "low": low or close, "last": close, "evol": volume,
            "eamt": "1000"}


class TestTheWireIsNewestFirst:
    def test_bars_come_back_oldest_first(self):
        """`output2[0]` is the most recent minute. Reversing a series is
        the kind of mistake an EMA computes without complaint."""
        rows = [_row("20260831", "050200", close="12.00"),
                _row("20260831", "050100", close="11.00"),
                _row("20260831", "050000", close="10.00")]
        parsed = chart.parse_rows(rows)
        assert [b["close"] for b in parsed] == [10.0, 11.0, 12.0]

    def test_timestamps_are_monotonic_afterwards(self):
        rows = [_row("20260831", "050200"), _row("20260831", "045900"),
                _row("20260831", "050100")]
        parsed = chart.parse_rows(rows)
        assert [b["at"] for b in parsed] == sorted(b["at"] for b in parsed)


class TestItDoesNotCrossTheDayBoundary:
    """120 bars from 05:02 ET reach back into the previous evening. A
    session VWAP computed over two days is wrong in a way that looks
    entirely reasonable."""

    def test_only_the_requested_day_is_kept(self):
        rows = [_row("20260831", "050200", close="12.00"),
                _row("20260830", "190200", close="99.00")]
        parsed = chart.parse_rows(rows, trading_day="2026-08-31")
        assert [b["close"] for b in parsed] == [12.0]

    def test_without_a_day_everything_is_kept(self):
        rows = [_row("20260831", "050200"), _row("20260830", "190200")]
        assert len(chart.parse_rows(rows)) == 2

    def test_the_eastern_date_is_what_filters(self):
        """The KST pair is ignored on purpose -- the session boundaries
        this feeds are Eastern."""
        row = _row("20260831", "050200")
        row["kymd"] = "20260901"  # KST is already tomorrow
        assert len(chart.parse_rows([row], trading_day="2026-08-31")) == 1


class TestVolumeIsPerBarNotCumulative:
    def test_each_bar_keeps_its_own_volume(self):
        rows = [_row("20260831", "050200", volume="79"),
                _row("20260831", "050100", volume="120")]
        parsed = chart.parse_rows(rows)
        assert [b["volume"] for b in parsed] == [120.0, 79.0]

    def test_premarket_volume_is_not_zero(self):
        """The whole point: extended-hours bars carry real volume."""
        parsed = chart.parse_rows([_row("20260831", "050200", volume="79")])
        assert parsed[0]["volume"] == pytest.approx(79.0)

    def test_a_missing_volume_is_zero_not_none(self):
        row = _row("20260831", "050200")
        row["evol"] = ""
        assert chart.parse_rows([row])[0]["volume"] == 0.0


class TestMalformedRowsAreSkippedNotFatal:
    def test_a_row_with_no_price_is_dropped(self):
        row = _row("20260831", "050200")
        row["last"] = ""
        assert chart.parse_rows([row]) == []

    def test_a_row_with_a_bad_timestamp_is_dropped(self):
        assert chart.parse_rows([_row("2026083", "0502")]) == []

    def test_a_non_dict_row_is_dropped(self):
        assert chart.parse_rows(["nonsense", None]) == []

    def test_no_rows_is_empty_not_an_error(self):
        assert chart.parse_rows(None) == []

    def test_ohlc_falls_back_to_close_when_absent(self):
        row = _row("20260831", "050200", close="10.00")
        row["high"] = row["low"] = row["open"] = ""
        parsed = chart.parse_rows([row])[0]
        assert parsed["high"] == parsed["low"] == parsed["open"] == 10.0


class TestFetchNeverTakesTheScanDown:
    """A warmup that cannot be filled leaves a candidate WARMING_UP. It
    does not kill the scan, which is what DATA_ERROR did."""

    def test_a_broker_error_returns_no_bars(self):
        class _Broker:
            class config:
                @staticmethod
                def validate_read_allowed():
                    return True

            def _get(self, *a, **k):
                raise RuntimeError("KIS unreachable")

        assert chart.fetch(_Broker(), symbol="AAPL", exchange="NASDAQ") == []

    def test_a_refusal_returns_no_bars(self):
        class _Broker:
            class config:
                @staticmethod
                def validate_read_allowed():
                    return True

            def _get(self, *a, **k):
                return {"rt_cd": "1", "msg1": "not permitted"}

        assert chart.fetch(_Broker(), symbol="AAPL", exchange="NASDAQ") == []

    def test_a_good_response_is_parsed(self):
        class _Broker:
            class config:
                @staticmethod
                def validate_read_allowed():
                    return True

            def _get(self, *a, **k):
                return {"rt_cd": "0", "output2": [
                    _row("20260831", "050200", close="12.00"),
                    _row("20260831", "050100", close="11.00")]}

        bars = chart.fetch(_Broker(), symbol="AAPL", exchange="NASDAQ",
                           trading_day="2026-08-31")
        assert [b["close"] for b in bars] == [11.0, 12.0]


class TestItProducesTheSharedBarShape:
    def test_records_convert_to_realtime_bars(self):
        """So the merge and the warmup see one shape whatever produced
        it."""
        from market_data import bar_merge

        records = chart.parse_rows([_row("20260831", "050200", close="12.0"),
                                    _row("20260831", "050100", close="11.0")])
        bars = chart.to_bars(records, symbol="AAPL", session="PREMARKET")
        assert [b.close for b in bars] == [11.0, 12.0]
        assert all(b.source == chart.SOURCE for b in bars)
        assert bar_merge.duplicate_minutes(bars) == []

    def test_the_merge_prefers_the_stream_over_backfill(self):
        """REST fills history; the stream owns minutes it watched."""
        from market_data import bar_merge, realtime_bars as rb

        records = chart.parse_rows([_row("20260831", "050100", close="11.0")])
        rest = chart.to_bars(records, symbol="AAPL", session="PREMARKET")
        live = rb.Bar(symbol="AAPL", session="PREMARKET",
                      minute=rest[0].minute, open=11.0, high=11.5, low=10.9,
                      close=11.4, volume=500.0, trade_count=9,
                      first_trade_at=rest[0].minute,
                      last_trade_at=rest[0].minute)
        merged = bar_merge.merge(stream_bars=[live], rest_bars=rest)
        assert len(merged) == 1
        assert merged[0].close == pytest.approx(11.4)

    def test_the_measured_cost_is_written_down(self):
        """Cadence decisions have to cite a number somebody measured."""
        assert chart.MEASURED_SECONDS_PER_SYMBOL > 0
        assert chart.BARS_PER_CALL == 120
