"""S1 recomputed on demand, from completed bars only.

The property that matters is not "a candidate was produced" -- it is that
the answer cannot be moved by the bar that is still forming. A signal
that drifts during the session is a different strategy from the one whose
behaviour was measured, and nothing downstream could tell.
"""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s1_live import same_day_scan as sds  # noqa: E402
from scanners.base.market_data_provider import SymbolData  # noqa: E402

MONDAY = "2026-08-17"
FRIDAY = "2026-08-14"
SESSIONS = ("OVERNIGHT", "PREMARKET", "REGULAR", "AFTER_HOURS")

#: Anything that actually BUILDS FEATURES has to use bars that are current
#: when the test runs. `build_features` refuses a frame older than
#: `max_daily_bar_age_days` -- a production guard, and the right one -- so a
#: fixture pinned to a fixed date passes until it ages past that limit and
#: then fails on a calendar rather than on a change.
#:
#: The fixed MONDAY/FRIDAY pair stays for the pure calendar assertions
#: (`signal_day_for` is a date function with no staleness rule), which is
#: where a hardcoded date is the point rather than a liability.
SCAN_DAY = pd.Timestamp.today().normalize()
SCAN_DAY_ISO = SCAN_DAY.date().isoformat()


def turning_up(end=None, seed=3, bars=400):
    """A long base then an early turn up -- passes all five S1 conditions."""
    index = pd.bdate_range(end=end or SCAN_DAY, periods=bars)
    base = np.full(bars, 100.0)
    turn = min(80, bars)
    base[-turn:] = 100.0 + np.arange(turn) ** 1.5 * 0.02
    close = base * (1 + np.random.default_rng(seed).normal(0, 0.004, bars))
    return pd.DataFrame({"Open": close, "High": close * 1.006, "Low": close * 0.994,
                         "Close": close, "Volume": [3_000_000] * bars}, index=index)


def falling(end=None, bars=400):
    close = np.linspace(150, 50, bars)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": [2_000_000] * bars},
                        index=pd.bdate_range(end=end or SCAN_DAY, periods=bars))


def bundle(symbol, frame):
    return SymbolData(symbol=symbol, daily=frame)


def scan_one(frame, symbol="TURN", **kw):
    kw.setdefault("trading_day", SCAN_DAY_ISO)
    return sds.scan([symbol], bundles={symbol: bundle(symbol, frame)}, **kw)


class TestSignalDayIsTheLastCompletedSession:
    def test_monday_computes_on_friday(self):
        assert sds.signal_day_for(MONDAY) == FRIDAY

    def test_the_scan_records_which_day_it_used(self):
        assert scan_one(turning_up()).signal_day == sds.signal_day_for(SCAN_DAY_ISO)

    def test_a_date_object_is_accepted(self):
        assert sds.signal_day_for(date(2026, 8, 17)) == FRIDAY

    def test_the_current_day_is_never_the_signal_day(self):
        for day in (MONDAY, "2026-08-18", "2026-08-19", "2026-08-20"):
            assert sds.signal_day_for(day) < day


class TestTheIncompleteBarCannotMoveTheSignal:
    def test_todays_bar_is_dropped_from_the_window(self):
        frame = turning_up()
        signal_day = sds.signal_day_for(SCAN_DAY_ISO)
        kept = sds.daily_through(frame, signal_day)
        assert len(kept) < len(frame), "the forming bar must be dropped"
        assert kept.index[-1].date().isoformat() <= signal_day

    def test_tripling_todays_bar_does_not_change_the_score(self):
        """The whole point. If this fails, S1 drifts during the session."""
        frame = turning_up()
        clean = scan_one(frame).candidates[0]

        spiked = frame.copy()
        spiked.iloc[-1] = spiked.iloc[-1] * 3.0
        moved = scan_one(spiked).candidates[0]

        assert moved.score == pytest.approx(clean.score, abs=1e-12)
        assert moved.signal_price == pytest.approx(clean.signal_price, abs=1e-12)

    def test_collapsing_todays_bar_does_not_trigger_a_rejection(self):
        frame = turning_up()
        crashed = frame.copy()
        crashed.iloc[-1] = crashed.iloc[-1] * 0.2
        assert scan_one(crashed).status == sds.STATUS_OK

    def test_the_intraday_and_premarket_frames_are_dropped_entirely(self):
        """No in-session bar may reach the daily calculation by any route."""
        frame = turning_up()
        data = SymbolData(symbol="T", daily=frame, intraday=frame, premarket=object())
        truncated = sds._truncated_bundle(data, sds.signal_day_for(SCAN_DAY_ISO))
        assert truncated.intraday is None
        assert truncated.premarket is None

    def test_truncation_does_not_mutate_the_shared_bundle(self):
        """S2..S6 judge the same bundle -- it must be left alone."""
        frame = turning_up()
        data = bundle("T", frame)
        sds._truncated_bundle(data, sds.signal_day_for(SCAN_DAY_ISO))
        assert len(data.daily) == len(frame)
        assert data.daily.index[-1] == frame.index[-1]


class TestNoDependenceOnAPriorCandidateFile:
    def test_a_candidate_is_produced_from_bars_alone(self, tmp_path, monkeypatch):
        """No store, no manifest, no yesterday's file."""
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "empty"))
        monkeypatch.setenv("KIS_CANDIDATE_DIR", str(tmp_path / "empty"))
        result = scan_one(turning_up())
        assert result.status == sds.STATUS_OK
        assert result.candidates[0].symbol == "TURN"

    def test_the_scan_carries_the_existing_s1_reasons(self):
        reasons = " ".join(scan_one(turning_up()).candidates[0].reasons)
        assert "HMA200" in reasons and "ADX" in reasons


class TestEverySessionCanScanAgain:
    def test_all_four_sessions_reach_the_same_conclusion(self):
        frame = turning_up()
        scores = {s: scan_one(frame, session=s) for s in SESSIONS}
        for session, result in scores.items():
            assert result.status == sds.STATUS_OK, session
            assert result.signal_day == sds.signal_day_for(SCAN_DAY_ISO)
            assert result.candidates[0].session == session

    def test_a_session_with_no_candidate_does_not_block_the_next(self):
        """§2: premarket 0 must not fix the day at 0."""
        empty = sds.scan([], bundles={}, trading_day=SCAN_DAY_ISO, session="PREMARKET")
        assert empty.status == sds.STATUS_DATA_UNAVAILABLE

        later = scan_one(turning_up(), session="REGULAR")
        assert later.status == sds.STATUS_OK

    def test_the_scan_holds_no_state_between_calls(self):
        frame = turning_up()
        first, second = scan_one(frame), scan_one(frame)
        assert first.as_dict() == second.as_dict()


class TestS1LogicIsUnchanged:
    def test_no_score_threshold_is_invented(self):
        """S1 filters with check(); score only ranks. A floor added here
        would make S1 stricter than the measured version."""
        assert sds.s1_score_threshold() is None

    def test_the_scanner_is_the_existing_one(self):
        scanner = sds.build_s1_scanner()
        assert scanner.scanner_name == "hma_early_trend"
        assert scanner.requires_intraday is False

    def test_a_downtrend_is_rejected_by_s1s_own_conditions(self):
        result = scan_one(falling(), symbol="DOWN")
        assert result.status == sds.STATUS_NO_CANDIDATE
        assert result.rejected == 1

    def test_only_s1_can_run_through_this_module(self):
        """S2..S6 are DISCOVERY_ONLY and must not reach the order path.

        Checked against the import graph and string LITERALS, not the
        prose: a substring sweep matches "orb" inside "forbids" and
        reports the docstring that explains the rule as a violation of it.
        """
        import ast

        others = {"accumulation", "breakout_ready", "gap_pullback", "orb",
                  "premarket_momentum"}
        assert sds.S1_SCANNER_NAME == "hma_early_trend"
        assert others.isdisjoint({sds.S1_SCANNER_NAME})

        tree = ast.parse((REPO_ROOT / "s1_live" / "same_day_scan.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                target = getattr(node, "module", "") or ""
                names = [a.name for a in node.names]
                for part in [target, *names]:
                    for segment in str(part).split("."):
                        assert segment not in others, f"imports {part}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in others, f"names {node.value!r}"


class TestOrderingAndFailure:
    def test_candidates_rank_by_score_then_symbol(self):
        strong, weak = turning_up(seed=3), turning_up(seed=11)
        result = sds.scan(["BBB", "AAA"], bundles={
            "BBB": bundle("BBB", strong), "AAA": bundle("AAA", weak)},
            trading_day=SCAN_DAY_ISO)
        scores = [c.score for c in result.candidates]
        assert scores == sorted(scores, reverse=True)

    def test_a_symbol_with_no_bars_is_counted_unavailable_not_rejected(self):
        result = sds.scan(["X"], bundles={"X": SymbolData(symbol="X", daily=None)},
                          trading_day=SCAN_DAY_ISO)
        assert result.unavailable == 1 and result.rejected == 0
        assert result.status == sds.STATUS_DATA_UNAVAILABLE

    def test_too_little_history_is_a_rejection_not_a_crash(self):
        short = turning_up(bars=30)
        assert scan_one(short).status == sds.STATUS_NO_CANDIDATE

    def test_an_empty_scan_is_not_reported_as_no_candidate(self):
        """"Nothing could be evaluated" and "nothing qualified" differ."""
        assert sds.scan([], bundles={}, trading_day=SCAN_DAY_ISO).status \
            == sds.STATUS_DATA_UNAVAILABLE

    def test_limit_caps_the_result(self):
        frame = turning_up()
        result = sds.scan(["A", "B", "C"], bundles={
            s: bundle(s, turning_up(seed=i + 3)) for i, s in enumerate("ABC")},
            trading_day=SCAN_DAY_ISO, limit=2)
        assert len(result.candidates) <= 2
