"""S6 entry timing: when a breakout is judged, not only whether.

The audit finding
-----------------
The ORB scanner's REGULAR path slices the WHOLE regular session so far,
takes the first 15 minutes as the range, and asks four questions of the
rest: did a bar close above the range, is price still above it, is it
above VWAP with EMA9 over EMA21, and is the mean post-range volume 1.2x
the mean range volume. None of those questions mentions the clock. A
name that broke out at 10:00 and drifted sideways is, at 15:30, the
same candidate it was at 10:05 -- the whole-post volume mean still
carries the morning's expansion, and nothing asks how long ago the last
new high was.

So S6-R "fresh" candidates can be generated in the last hour of the
session from a breakout that is five hours old. The first test below
pins that as the CURRENT behaviour, so the change is visible.

What this adds, and what it deliberately does not
-------------------------------------------------
Four measurements, always recorded:

    session_elapsed_minutes        how far into the session the judgement is
    breakout_age_minutes           how old the breakout is
    minutes_since_session_high     how long since the last new high
    recent_volume_expansion_<W>m   the last W minutes' volume against the range

and four gates that read them, every one of which ships OFF (null in
config.json). The distributions have to be measured on real candidates
before 30, 45, 60 or 90 minutes can be called the right window --
choosing one now would encode a guess into the entry policy and then
collect a month of data under it. The gates exist so that turning one on
is a config change with tests behind it, not a code change.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_hours import EASTERN  # noqa: E402
from scanners.base.market_data_provider import SymbolData  # noqa: E402
from scanners.base.models import ScannerDataError  # noqa: E402
from scanners.base.scanner_base import Rejected  # noqa: E402
from scanners.registry import build_scanner  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402

DAY = date(2026, 8, 12)


def session(*, breakout_at="09:50", judged_at="15:30", range_minutes=15,
            base=100.0, range_pct=0.8, breakout_pct=1.2,
            range_volume=8_000.0, post_volume=24_000.0,
            fade_volume_after=None, fade_volume=2_000.0,
            spike_high_at=None, spike_pct=0.4):
    """A regular session judged at `judged_at`, one-minute bars.

    Price sits inside the range until `breakout_at`, then closes above
    the range high and climbs toward `breakout_pct` above it. Two knobs
    build the shapes the gates exist to distinguish:

      fade_volume_after   after this time, per-bar volume drops to
                          `fade_volume` -- the morning's expansion is in
                          the whole-post mean, but the recent bars are dead
      spike_high_at       one bar prints a wick `spike_pct` above where
                          the close path ends, and no later bar reaches
                          it -- price keeps grinding up but the session
                          HIGH was set back then
    """
    start = pd.Timestamp(datetime.combine(DAY, datetime.min.time()),
                         tz=EASTERN).replace(hour=9, minute=30)
    end = pd.Timestamp(datetime.combine(DAY, datetime.min.time()),
                       tz=EASTERN).replace(hour=int(judged_at[:2]),
                                           minute=int(judged_at[3:]))
    index = pd.date_range(start=start, end=end, freq="1min")
    count = len(index)

    high = base * (1.0 + range_pct / 100.0)
    low = base * (1.0 - range_pct / 100.0)
    target = high * (1.0 + breakout_pct / 100.0)

    def at(text):
        return start.replace(hour=int(text[:2]), minute=int(text[3:]))

    breakout = int((at(breakout_at) - start).total_seconds() // 60)
    closes = np.empty(count)
    closes[:range_minutes] = np.linspace(low * 1.001, high * 0.999, range_minutes)
    # Inside the range, drifting up but never closing above it, until the
    # breakout bar. The drift keeps price over VWAP and EMA9 over EMA21
    # once the breakout arrives, which is what makes the gates under test
    # the ONLY thing that can reject the frame.
    closes[range_minutes:breakout] = np.linspace(
        high * 0.990, high * 0.9995, max(0, breakout - range_minutes))
    remaining = count - breakout
    closes[breakout:] = np.linspace(high * 1.001, target, remaining)

    highs = closes + 0.02
    lows = closes - 0.05
    highs[:range_minutes] = high
    lows[:range_minutes] = low

    if spike_high_at is not None:
        spike = int((at(spike_high_at) - start).total_seconds() // 60)
        highs[spike] = target * (1.0 + spike_pct / 100.0)

    volumes = np.full(count, post_volume)
    volumes[:range_minutes] = range_volume
    if fade_volume_after is not None:
        fade = int((at(fade_volume_after) - start).total_seconds() // 60)
        volumes[fade:] = fade_volume

    opens = np.concatenate([[closes[0]], closes[:-1]])
    intraday = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes,
         "Volume": volumes}, index=index)
    daily = fx.daily_frame(fx.accelerating_uptrend(fx.DEFAULT_DAILY_BARS))
    return SymbolData(symbol="TEST", daily=daily, intraday=intraday)


def scanner(**params):
    built = build_scanner("orb")
    for key, value in params.items():
        assert key in built.config.params, f"{key} is not a configured parameter"
        built.config.params[key] = value
    return built


def check(built, bundle):
    features = built.build_features(bundle)
    context = {}
    reasons = built.check(features, bundle, context)
    return reasons, context


class TestTheCurrentConditionIsPinned:
    """What the audit found: nothing about the clock rejects."""

    def test_a_five_hour_old_breakout_is_still_a_fresh_candidate_by_default(self):
        reasons, context = check(scanner(), session(breakout_at="10:00",
                                                    judged_at="15:30"))
        assert reasons
        assert context["breakout_confirmed"] is True

    def test_the_whole_post_mean_hides_dead_recent_volume(self):
        """Mean post-range volume over five hours still clears 1.2x when
        the last hour traded at a quarter of the range's volume."""
        reasons, context = check(scanner(), session(
            breakout_at="10:00", judged_at="15:30",
            fade_volume_after="14:30", fade_volume=2_000.0))
        assert context["volume_expansion"] >= 1.2
        assert context["recent_volume_expansion_30m"] < 1.0

    def test_every_timing_gate_ships_off(self):
        params = json.loads(
            (REPO_ROOT / "scanners" / "orb" / "config.json").read_text())["params"]
        for key in ("entry_window_minutes", "max_breakout_age_minutes",
                    "recent_volume_window_minutes",
                    "recent_volume_expansion_min",
                    "max_minutes_since_session_high"):
            assert key in params, key
            assert params[key] is None, key
        # The observation windows, on the other hand, are set: they are
        # what the gates will be calibrated from.
        assert params["recent_volume_observation_minutes"] == [15, 30]


class TestTheMeasurementsAreAlwaysRecorded:
    def test_elapsed_and_breakout_age_come_from_the_bars(self):
        _, context = check(scanner(), session(breakout_at="10:00",
                                              judged_at="15:30"))
        assert context["session_elapsed_minutes"] == 360
        assert context["breakout_age_minutes"] == 330
        assert context["breakout_at"] is not None

    def test_the_last_new_high_is_dated(self):
        _, context = check(scanner(), session(
            breakout_at="10:00", judged_at="12:00",
            spike_high_at="10:30", spike_pct=0.4))
        assert context["minutes_since_session_high"] == 90
        assert context["drawdown_from_session_high_pct"] == pytest.approx(
            0.4, abs=0.05)

    def test_a_name_still_making_highs_reports_zero(self):
        _, context = check(scanner(), session(breakout_at="10:00",
                                              judged_at="12:00"))
        assert context["minutes_since_session_high"] == 0
        assert context["drawdown_from_session_high_pct"] == pytest.approx(
            0.0, abs=0.05)

    def test_recent_volume_is_measured_at_each_observation_window(self):
        _, context = check(scanner(), session(
            breakout_at="10:00", judged_at="15:30",
            fade_volume_after="15:16", fade_volume=2_000.0))
        # The last 15 minutes are all faded; the last 30 are half faded.
        assert context["recent_volume_expansion_15m"] == pytest.approx(0.25)
        assert context["recent_volume_expansion_30m"] == pytest.approx(
            (15 * 24_000 + 15 * 2_000) / 30 / 8_000)

    def test_the_measurements_reach_the_signal_metrics(self):
        signal = scanner().evaluate(session(breakout_at="10:00",
                                            judged_at="11:00"),
                                    trading_day=str(DAY))
        for key in ("session_elapsed_minutes", "breakout_age_minutes",
                    "minutes_since_session_high",
                    "drawdown_from_session_high_pct",
                    "recent_volume_expansion_15m",
                    "recent_volume_expansion_30m"):
            assert key in signal.metrics, key
            assert signal.metrics[key] is not None, key

    def test_the_measurements_reach_the_published_row_and_the_snapshot(self):
        from scanners.publish import candidates, s6_snapshot

        signal = scanner().evaluate(session(breakout_at="10:00",
                                            judged_at="11:00"),
                                    trading_day=str(DAY))
        row = candidates.build_rows([signal], strategy_id="S6_ORB_BREAKOUT_V1",
                                    trading_day=str(DAY), session="REGULAR",
                                    variant="S6-R")[0].as_dict()
        timing = row["entry_timing"]
        assert timing["session_elapsed_minutes"] == 90
        assert timing["breakout_age_minutes"] == 60
        record = s6_snapshot.build(row, trading_day=str(DAY), session="REGULAR")
        assert record["entry_timing"]["breakout_age_minutes"] == 60

    def test_the_analytics_tracker_carries_the_timing_through(self):
        from scanners.analytics import s6_candidate_tracker as tracker

        row = tracker.follow(
            {"symbol": "TEST", "price": 100.0, "range_high": 99.0,
             "entry_timing": {"breakout_age_minutes": 12}},
            [], candidate_time=datetime(2026, 8, 12, 10, 0, tzinfo=EASTERN))
        assert row["entry_timing"] == {"breakout_age_minutes": 12}


class TestTheEntryWindowGate:
    def test_a_late_session_fresh_candidate_is_blocked(self):
        with pytest.raises(Rejected, match="entry window"):
            check(scanner(entry_window_minutes=60),
                  session(breakout_at="10:00", judged_at="15:30"))

    def test_the_same_setup_inside_the_window_passes(self):
        reasons, _ = check(scanner(entry_window_minutes=60),
                           session(breakout_at="10:00", judged_at="10:20"))
        assert reasons

    @pytest.mark.parametrize("window,judged_at,passes", [
        (30, "09:59", True), (30, "10:01", False),
        (45, "10:14", True), (45, "10:16", False),
        (60, "10:29", True), (60, "10:31", False),
        (90, "10:59", True), (90, "11:01", False),
    ])
    def test_the_candidate_windows_cut_where_they_say(self, window, judged_at,
                                                      passes):
        built = scanner(entry_window_minutes=window)
        bundle = session(breakout_at="09:47", judged_at=judged_at)
        if passes:
            assert check(built, bundle)[0]
        else:
            with pytest.raises(Rejected, match="entry window"):
                check(built, bundle)


class TestTheBreakoutRecencyGate:
    def test_an_old_breakout_is_blocked_even_early_in_the_session(self):
        with pytest.raises(Rejected, match="breakout is"):
            check(scanner(max_breakout_age_minutes=30),
                  session(breakout_at="09:50", judged_at="10:45"))

    def test_a_recent_breakout_passes(self):
        reasons, context = check(scanner(max_breakout_age_minutes=30),
                                 session(breakout_at="10:30", judged_at="10:45"))
        assert reasons
        assert context["breakout_age_minutes"] == 15


class TestTheRecentVolumeGate:
    def test_recent_volume_that_has_died_is_blocked(self):
        """The whole-post mean passes; the last thirty minutes do not."""
        built = scanner(recent_volume_window_minutes=30,
                        recent_volume_expansion_min=1.2)
        with pytest.raises(Rejected, match="recent volume"):
            check(built, session(breakout_at="10:00", judged_at="15:30",
                                 fade_volume_after="14:30",
                                 fade_volume=2_000.0))

    def test_recent_volume_that_persists_passes(self):
        built = scanner(recent_volume_window_minutes=30,
                        recent_volume_expansion_min=1.2)
        assert check(built, session(breakout_at="10:00",
                                    judged_at="15:30"))[0]

    def test_a_threshold_without_a_window_is_a_config_error(self):
        from scanners.base.config import ScannerConfigError

        built = scanner(recent_volume_expansion_min=1.2)
        with pytest.raises(ScannerConfigError):
            check(built, session())


class TestTheNewHighRecencyGate:
    def test_a_stalled_breakout_is_blocked(self):
        with pytest.raises(Rejected, match="new session high"):
            check(scanner(max_minutes_since_session_high=30),
                  session(breakout_at="10:00", judged_at="12:00",
                          spike_high_at="10:30"))

    def test_a_breakout_still_making_highs_passes(self):
        assert check(scanner(max_minutes_since_session_high=30),
                     session(breakout_at="10:00", judged_at="12:00"))[0]


class TestTheGatesDoNotReorderTheExistingOnes:
    def test_the_existing_rejections_still_speak_first(self):
        """A frame that fails volume expansion says so, even with every
        timing gate on and failing too: the existing reasons are what
        month one's rejection statistics are keyed on."""
        built = scanner(entry_window_minutes=30, max_breakout_age_minutes=5)
        with pytest.raises(Rejected, match="volume expansion"):
            check(built, session(breakout_at="10:00", judged_at="15:30",
                                 range_volume=30_000.0, post_volume=8_000.0))

    def test_the_regular_path_is_textually_unchanged(self):
        """The variant-scanner test's own guard, restated: the REGULAR
        slice and range come from the original helpers."""
        source = (REPO_ROOT / "scanners" / "orb" / "scanner.py").read_text()
        head = source[source.index('requested = str(context.get("session")'):]
        regular_block = head[:head.index("else:")]
        assert "sess.slice_session(" in regular_block
        assert "session_range" not in regular_block

    def test_the_scan_is_unchanged_for_the_existing_fixture(self):
        """The fixture every other ORB test uses still passes, with the
        same reasons in the same order."""
        built = build_scanner("orb")
        reasons, _ = check(built, fx.orb_bundle())
        assert reasons[0].startswith("breakout confirmed")
        assert any(r.startswith("volume expansion") for r in reasons)


class TestAGateWithoutItsMeasurementFailsClosed:
    def test_a_frame_that_cannot_be_timed_is_a_data_error_not_a_pass(self):
        """An active gate that cannot read its input must not wave the
        symbol through. Bars without timestamps are counted as minutes
        (the fixture convention `opening_range` already uses); a frame
        whose timing is genuinely unknowable is refused as data."""
        built = scanner(entry_window_minutes=60)
        bundle = session(breakout_at="10:00", judged_at="15:30")
        # Strip the index: rows are then minutes, and 360 rows is still
        # past a 60-minute window.
        bundle.intraday.index = pd.RangeIndex(len(bundle.intraday))
        with pytest.raises((Rejected, ScannerDataError)):
            check(built, bundle)


class TestNullGatesPreserveLegacyBehaviour:
    """G: with every timing parameter null, the scan is the old scan."""

    def _stripped(self):
        """The scanner as if the timing parameters had never existed."""
        built = build_scanner("orb")
        for key in ("entry_window_minutes", "max_breakout_age_minutes",
                    "recent_volume_window_minutes",
                    "recent_volume_expansion_min",
                    "max_minutes_since_session_high",
                    "recent_volume_observation_minutes"):
            built.config.params.pop(key, None)
        return built

    @pytest.mark.parametrize("bundle_factory", [
        lambda: fx.orb_bundle(),
        lambda: fx.orb_bundle(retest=True),
        lambda: session(breakout_at="10:00", judged_at="15:30"),
        lambda: session(breakout_at="10:00", judged_at="15:30",
                        fade_volume_after="14:30"),
        lambda: session(breakout_at="10:00", judged_at="12:00",
                        spike_high_at="10:30"),
    ])
    def test_the_verdict_reasons_score_and_legacy_metrics_are_identical(
            self, bundle_factory):
        shipped, stripped = build_scanner("orb"), self._stripped()
        bundle = bundle_factory()
        a = shipped.evaluate(bundle, trading_day=str(DAY), timestamp="t")
        b = stripped.evaluate(bundle, trading_day=str(DAY), timestamp="t")
        assert a is not None and b is not None
        assert a.reasons == b.reasons
        assert a.scanner_score == b.scanner_score
        assert a.signal_price == b.signal_price
        legacy = {k: v for k, v in b.metrics.items()
                  if k != "config_fingerprint"}
        for key, value in legacy.items():
            assert a.metrics[key] == value, key
        # The only additions are the timing keys, all of them present
        # either way except the per-window ratios the stripped config
        # no longer asks for.
        extra = set(a.metrics) - set(b.metrics)
        assert extra == {"recent_volume_expansion_15m",
                         "recent_volume_expansion_30m"}

    @pytest.mark.parametrize("bundle_factory,message", [
        (lambda: fx.orb_bundle(confirm_close=False), "wick only|CLOSED"),
        (lambda: fx.orb_bundle(range_volume=30_000.0, post_volume=8_000.0),
         "volume expansion"),
        (lambda: fx.orb_bundle(breakout_pct=12.0), "past the"),
    ])
    def test_the_legacy_rejections_are_unchanged(self, bundle_factory, message):
        for built in (build_scanner("orb"), self._stripped()):
            with pytest.raises(Rejected, match=message):
                check(built, bundle_factory())

    def test_null_gates_never_consult_their_inputs(self):
        """Every input None and no gate raises: a gate that was on would
        have refused the frame as untimeable."""
        from scanners.orb import scanner as orb

        built = build_scanner("orb")
        timing = {key: None for key in orb.ENTRY_TIMING_KEYS}
        orb._apply_timing_gates(built.config, timing, session=None,
                                range_volume=None, symbol="X")
        assert all(value is None for value in timing.values())

    def test_the_ship_config_has_no_active_gate(self):
        params = json.loads(
            (REPO_ROOT / "scanners" / "orb" / "config.json").read_text())["params"]
        assert {k: params[k] for k in (
            "entry_window_minutes", "max_breakout_age_minutes",
            "recent_volume_window_minutes", "recent_volume_expansion_min",
            "max_minutes_since_session_high")} == {
            "entry_window_minutes": None, "max_breakout_age_minutes": None,
            "recent_volume_window_minutes": None,
            "recent_volume_expansion_min": None,
            "max_minutes_since_session_high": None}


class TestTheTimingJoinsTheForwardRecord:
    """Forward-outcome readiness: the existing analytics store carries
    both halves, joined by signal id."""

    def test_signal_timing_and_forward_returns_meet_in_joined_rows(
            self, tmp_path, monkeypatch):
        from scanners.analytics import performance_tracker
        from scanners.base import result_store

        monkeypatch.setenv(result_store.ANALYTICS_DIR_ENV, str(tmp_path))
        # Judged at 11:00; the fixture's bars run on to 15:30 so every
        # intraday horizon can be measured from the same frame.
        judged = session(breakout_at="10:00", judged_at="11:00")
        whole = session(breakout_at="10:00", judged_at="15:30")
        stamp = datetime(2026, 8, 12, 11, 0, tzinfo=EASTERN).isoformat()
        signal = scanner().evaluate(judged, trading_day=str(DAY),
                                    timestamp=stamp)
        result_store.write_signals([signal], trading_day=str(DAY))

        record = performance_tracker.compute_performance(
            signal, intraday=whole.intraday,
            now=datetime(2026, 8, 12, 16, 0, tzinfo=EASTERN))
        result_store.write_performance([record], trading_day=str(DAY))

        joined = result_store.joined_rows(str(DAY), str(DAY))
        assert len(joined) == 1
        row = joined[0]
        # The entry-timing axis...
        assert row["metric_session_elapsed_minutes"] == 90
        assert row["metric_breakout_age_minutes"] == 60
        assert row["metric_minutes_since_session_high"] == 0
        assert row["metric_recent_volume_expansion_30m"] == pytest.approx(3.0)
        # ...against the forward outcome, at the horizons the study needs.
        for field in ("return_15m", "return_30m", "return_1h",
                      "mfe_15m", "mae_15m", "mfe_30m", "mae_30m",
                      "mfe_1h", "mae_1h"):
            assert row[field] is not None, field
        assert row["return_15m"] < row["return_30m"] < row["return_1h"]
        assert row["mfe_15m"] <= row["mfe_30m"] <= row["mfe_1h"]
        assert row["mae_15m"] <= 0.0

    def test_intraday_excursions_are_null_until_the_window_elapses(self):
        from scanners.analytics import performance_tracker

        judged = session(breakout_at="10:00", judged_at="11:00")
        # Bars stop at 11:20: the 15m window has elapsed, the others not.
        short = session(breakout_at="10:00", judged_at="11:20")
        stamp = datetime(2026, 8, 12, 11, 0, tzinfo=EASTERN).isoformat()
        signal = scanner().evaluate(judged, trading_day=str(DAY),
                                    timestamp=stamp)
        record = performance_tracker.compute_performance(
            signal, intraday=short.intraday,
            now=datetime(2026, 8, 12, 11, 21, tzinfo=EASTERN))
        assert record["mfe_15m"] is not None and record["mae_15m"] is not None
        assert record["mfe_30m"] is None and record["mae_30m"] is None
        assert record["mfe_1h"] is None
