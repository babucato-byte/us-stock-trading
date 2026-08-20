"""S2 candidate measurement: absent stays absent.

The single property these tests exist to defend is that a gap in the data
never becomes a number. With four trading days behind the dataset, one
fabricated zero is enough to make a month's conclusion unfalsifiable --
and the fabrications that matter are the plausible ones. A
`time_to_vwap_failure` of 0 reads as "failed immediately", which is a
finding; the truth is almost always "there were no minute bars".

So every measure returns None when it cannot answer, and the ambiguous
Nones are disambiguated by a companion field rather than by a sentinel
number that would end up inside an average.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.analytics import s2_candidate_metrics as m  # noqa: E402

START = datetime(2026, 8, 19, 9, 30)


def bar(minute, close=100.0, high=None, low=None, vwap=100.0, volume=1_000_000):
    return {"timestamp": START + timedelta(minutes=minute),
            "close": close, "high": high if high is not None else close,
            "low": low if low is not None else close,
            "vwap": vwap, "volume": volume}


class TestNoDataIsNotZero:
    @pytest.mark.parametrize("fn,kw", [
        (m.time_to_peak, {}),
        (m.time_to_vwap_failure, {}),
        (m.holding_duration_minutes, {}),
        (m.volume_decay_minutes,
         {"signal_volume_multiple": 3.0, "baseline_volume": 1_000_000}),
    ])
    def test_an_empty_window_measures_nothing(self, fn, kw):
        assert fn([], **kw) is None
        assert fn(None, **kw) is None

    def test_bars_without_the_needed_field_measure_nothing(self):
        blind = [{"timestamp": START, "close": 100.0}]  # no vwap
        assert m.time_to_vwap_failure(blind) is None
        assert m.vwap_held(blind) is None

    def test_nan_is_not_a_measurement(self):
        nan = float("nan")
        assert m.time_to_peak([{"timestamp": START, "high": nan}]) is None

    def test_the_summary_excludes_nulls_rather_than_zeroing_them(self):
        rows = [{"x": 10.0}, {"x": None}, {"x": 20.0}]
        assert m.summarise(rows, "x") == {"mean": 15.0, "n": 2, "sufficient": True}

    def test_the_summary_reports_how_little_it_had(self):
        assert m.summarise([], "x")["sufficient"] is False
        assert m.summarise([{"x": 1.0}], "x", minimum=5)["sufficient"] is False


class TestTimeToPeak:
    def test_it_measures_to_the_highest_high(self):
        bars = [bar(0, high=100), bar(10, high=105), bar(20, high=102)]
        assert m.time_to_peak(bars) == 10

    def test_a_flat_top_peaks_when_it_first_got_there(self):
        """Reporting the last equal bar would make a stalled move look
        like a slow, continuing one."""
        bars = [bar(0, high=100), bar(10, high=105), bar(60, high=105)]
        assert m.time_to_peak(bars) == 10

    def test_it_uses_the_high_not_the_close(self):
        """A candidate that spiked at minute 20 and closed flat did peak
        at minute 20."""
        bars = [bar(0, close=100, high=100), bar(20, close=100, high=110)]
        assert m.time_to_peak(bars) == 20

    def test_the_signal_time_anchors_the_measurement(self):
        bars = [bar(10, high=105)]
        assert m.time_to_peak(bars, signal_time=START) == 10


class TestVwapFailure:
    def test_it_reports_the_first_close_below_vwap(self):
        bars = [bar(0, close=101, vwap=100), bar(30, close=99, vwap=100),
                bar(40, close=98, vwap=100)]
        assert m.time_to_vwap_failure(bars) == 30

    def test_holding_vwap_is_not_a_failure_at_minute_zero(self):
        bars = [bar(0, close=101, vwap=100), bar(60, close=102, vwap=100)]
        assert m.time_to_vwap_failure(bars) is None
        assert m.vwap_held(bars) is True

    def test_held_and_unmeasurable_are_told_apart(self):
        """Both give `time_to_vwap_failure is None`, and they support
        opposite conclusions -- so the companion field must separate
        them."""
        held = [bar(0, close=101, vwap=100)]
        blind = [{"timestamp": START, "close": 101.0}]
        assert m.time_to_vwap_failure(held) is m.time_to_vwap_failure(blind)
        assert m.vwap_held(held) is True
        assert m.vwap_held(blind) is None

    def test_touching_vwap_exactly_is_not_a_failure(self):
        assert m.vwap_held([bar(0, close=100.0, vwap=100.0)]) is True


class TestVolumeDecayIsRelativeToTheTrigger:
    def test_decay_is_measured_against_the_candidates_own_multiple(self):
        """A 6x dropping to 3.5x has lost half its excess; the default
        fraction is half the EXCESS over baseline, not half the multiple."""
        base = 1_000_000
        bars = [bar(0, volume=6 * base), bar(30, volume=int(3.5 * base))]
        assert m.volume_decay_minutes(
            bars, signal_volume_multiple=6.0, baseline_volume=base) == 30

    def test_a_quiet_candidate_and_a_loud_one_decay_at_different_levels(self):
        """A shared absolute threshold would call only the loud one
        decayed."""
        base = 1_000_000
        quiet = [bar(0, volume=int(1.6 * base)), bar(20, volume=int(1.3 * base))]
        assert m.volume_decay_minutes(
            quiet, signal_volume_multiple=1.6, baseline_volume=base) == 20

    def test_nothing_elevated_cannot_decay(self):
        """Otherwise the quietest candidates report as the fastest to
        fade, which inverts the finding."""
        assert m.volume_decay_minutes(
            [bar(0, volume=900_000)], signal_volume_multiple=1.0,
            baseline_volume=1_000_000) is None

    def test_a_missing_baseline_measures_nothing(self):
        """Inferring one from the window would measure the candidate
        against itself."""
        assert m.volume_decay_minutes(
            [bar(0, volume=5_000_000)], signal_volume_multiple=5.0,
            baseline_volume=None) is None
        assert m.volume_decay_minutes(
            [bar(0, volume=5_000_000)], signal_volume_multiple=5.0,
            baseline_volume=0) is None

    def test_volume_that_never_drains_reports_no_decay_time(self):
        base = 1_000_000
        loud = [bar(0, volume=6 * base), bar(60, volume=6 * base)]
        assert m.volume_decay_minutes(
            loud, signal_volume_multiple=6.0, baseline_volume=base) is None


class TestTheRowIsInterpretable:
    def test_measure_reports_what_it_had_to_work_with(self):
        """`bars_seen` is what makes a None readable: the same None means
        "no data" at 0 bars and "held all day" at 390."""
        row = m.measure([], signal_volume_multiple=3.0, baseline_volume=1_000_000)
        assert row["bars_seen"] == 0
        assert row["time_to_vwap_failure_minutes"] is None
        assert row["vwap_held"] is None
        assert row["holding_duration_minutes"] is None

    def test_a_full_session_row_is_distinguishable_from_an_empty_one(self):
        base = 1_000_000
        bars = [bar(i, close=101, vwap=100, high=101 + i * 0.01, volume=base)
                for i in range(0, 390, 30)]
        row = m.measure(bars, signal_volume_multiple=2.0, baseline_volume=base)
        assert row["bars_seen"] == 13
        assert row["vwap_held"] is True
        assert row["holding_duration_minutes"] == 360
        assert row["time_to_vwap_failure_minutes"] is None

    def test_the_decay_definition_travels_with_the_row(self):
        """So a later study can recompute with a different definition
        instead of inheriting this one silently."""
        row = m.measure([bar(0)], signal_volume_multiple=2.0,
                        baseline_volume=1_000_000)
        assert row["decay_fraction"] == m.DECAY_FRACTION
        assert row["decay_reference"] == "signal_volume_multiple"

    def test_every_measure_is_bounded_by_the_window_it_reports(self):
        bars = [bar(0, high=100), bar(45, high=110)]
        row = m.measure(bars, signal_volume_multiple=2.0, baseline_volume=1)
        assert row["time_to_peak_minutes"] <= row["holding_duration_minutes"]


class TestItIsMeasurementNotPolicy:
    def test_no_measure_gates_an_order(self):
        import ast

        source = (REPO_ROOT / "scanners" / "analytics"
                  / "s2_candidate_metrics.py").read_text()
        banned = {"brokers", "execution", "order_gate", "kis_broker",
                  "kis_live_trading", "s1_live", "risk"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, f"imports {name}"

    def test_it_does_not_feed_back_into_a_scanner_condition(self):
        """These describe what happened to a candidate. A scanner reading
        them would be tuning itself on its own past output."""
        import ast

        source = (REPO_ROOT / "scanners" / "analytics"
                  / "s2_candidate_metrics.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = str(getattr(node, "module", "") or "")
                assert "accumulation" not in module
