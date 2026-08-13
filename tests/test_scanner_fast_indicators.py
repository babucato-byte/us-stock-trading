"""`fast_hma` must be the same indicator as `indicators.hma` (spec 16-20).

The optimisation is only worth having if it cannot move a verdict. That
claim is checked at two levels here, and the second matters more:

    numeric   the series agree, including WHERE the NaNs are
    verdict   every scanner reaches the same PASS/REJECT, score and
              reason through either implementation

A numeric test alone would miss the failure that actually matters. Being
off by one bar in the warm-up shifts every subsequent value by a bar and
still produces a beautifully correlated series -- one that would quietly
change which symbols pass for a month.

Tolerance is asserted, not assumed
----------------------------------
The two implementations sum the same products in a different order, so
they differ in the last floating-point bits and cannot be compared with
`==`. Measured agreement is ~1e-15 relative; the tests pin 1e-12, which
is loose enough not to be flaky and roughly a thousand times tighter
than anything that could move a threshold comparison.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import indicators as reference  # noqa: E402
from scanners.base import indicators as sind  # noqa: E402
from scanners.base.fast_indicators import fast_hma, fast_wma, min_bars_for_hma  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402

#: Relative agreement required between the two implementations.
TOLERANCE = 1e-12

PERIODS = (20, 50, 89, 200)


def series_cases():
    rng = np.random.default_rng(20260814)
    cases = {
        "random_walk": np.cumsum(rng.normal(0, 1, 600)) + 200.0,
        "uptrend": np.linspace(10.0, 90.0, 600),
        "downtrend": np.linspace(90.0, 10.0, 600),
        "flat": np.full(600, 42.0),
        "volatile": rng.normal(100.0, 25.0, 600),
        "tiny_values": rng.normal(0.01, 0.002, 600),
        "large_values": rng.normal(50_000.0, 500.0, 600),
    }
    with_nan = rng.normal(50.0, 3.0, 600)
    with_nan[[0, 5, 199, 400, 599]] = np.nan
    cases["with_nan"] = with_nan
    leading_nan = rng.normal(50.0, 3.0, 600)
    leading_nan[:250] = np.nan
    cases["leading_nan_block"] = leading_nan
    return {name: pd.Series(values) for name, values in cases.items()}


def relative_error(left: pd.Series, right: pd.Series) -> float:
    both = left.notna() & right.notna()
    if not both.any():
        return 0.0
    a = left[both].to_numpy()
    b = right[both].to_numpy()
    scale = np.maximum(np.abs(a), 1e-12)
    return float(np.max(np.abs(a - b) / scale))


class TestNumericEquivalence:
    @pytest.mark.parametrize("name,series", sorted(series_cases().items()))
    @pytest.mark.parametrize("period", PERIODS)
    def test_hma_matches_the_reference(self, name, series, period):
        expected = reference.hma(series, period)
        actual = fast_hma(series, period)
        assert relative_error(expected, actual) < TOLERANCE, name

    @pytest.mark.parametrize("name,series", sorted(series_cases().items()))
    @pytest.mark.parametrize("period", PERIODS)
    def test_nan_positions_match_exactly(self, name, series, period):
        """The warm-up length is the part that would silently shift every
        value by a bar while still looking correlated."""
        expected = reference.hma(series, period)
        actual = fast_hma(series, period)
        assert list(expected.isna()) == list(actual.isna()), name

    @pytest.mark.parametrize("period", PERIODS)
    def test_the_whole_series_is_compared_not_just_the_last_value(self, period):
        """Section 18. A last-value-only check passes for an
        implementation that is wrong everywhere else."""
        series = series_cases()["random_walk"]
        expected = reference.hma(series, period)
        actual = fast_hma(series, period)
        assert expected.notna().sum() > 100
        assert relative_error(expected, actual) < TOLERANCE
        # and the final value specifically
        assert expected.dropna().iloc[-1] == pytest.approx(
            actual.dropna().iloc[-1], rel=TOLERANCE)

    @pytest.mark.parametrize("period", PERIODS)
    def test_wma_matches_the_reference(self, period):
        series = series_cases()["random_walk"]
        expected = reference.weighted_moving_average(series, period)
        actual = fast_wma(series, period)
        assert list(expected.isna()) == list(actual.isna())
        assert relative_error(expected, actual) < TOLERANCE

    def test_the_index_is_preserved(self):
        series = pd.Series(np.linspace(1, 100, 400),
                           index=pd.date_range("2026-01-01", periods=400, freq="D"))
        assert fast_hma(series, 50).index.equals(series.index)


class TestEdgeCases:
    @pytest.mark.parametrize("period", PERIODS)
    def test_history_shorter_than_the_period_is_all_nan_in_both(self, period):
        series = pd.Series(np.linspace(1, 10, period - 5))
        assert reference.hma(series, period).isna().all()
        assert fast_hma(series, period).isna().all()

    def test_the_exact_warmup_boundary_agrees(self):
        """One bar short is all-NaN; exactly enough produces exactly one
        value. Both implementations must agree on which."""
        for period in PERIODS:
            needed = min_bars_for_hma(period)
            short = pd.Series(np.linspace(1, 50, needed - 1))
            exact = pd.Series(np.linspace(1, 50, needed))
            assert reference.hma(short, period).isna().all()
            assert fast_hma(short, period).isna().all()
            assert reference.hma(exact, period).notna().sum() == 1
            assert fast_hma(exact, period).notna().sum() == 1

    def test_min_bars_agrees_with_the_scanner_helper(self):
        for period in PERIODS:
            assert min_bars_for_hma(period) == sind.min_bars_for_hma(period)

    def test_an_empty_series_does_not_raise(self):
        empty = pd.Series(dtype=float)
        assert len(fast_hma(empty, 50)) == 0
        assert len(fast_wma(empty, 50)) == 0

    def test_all_nan_input_gives_all_nan_output(self):
        series = pd.Series(np.full(400, np.nan))
        assert fast_hma(series, 200).isna().all()

    def test_period_one_returns_the_series_like_the_reference(self):
        series = pd.Series(np.linspace(1, 50, 100))
        pd.testing.assert_series_equal(reference.hma(series, 1), fast_hma(series, 1))


class TestRealSymbolFixtures:
    """Section 17 asks for real symbols. The offline fixtures stand in
    for them: same shapes, no network in the test suite."""

    @pytest.mark.parametrize("period", PERIODS)
    def test_matches_on_the_scanner_fixtures(self, period):
        for builder in (fx.accelerating_uptrend, fx.coiled_under_high):
            closes = pd.Series(builder())
            expected = reference.hma(closes, period)
            actual = fast_hma(closes, period)
            assert list(expected.isna()) == list(actual.isna())
            assert relative_error(expected, actual) < TOLERANCE

    def test_the_scanner_feature_path_uses_the_fast_implementation(self):
        """Wiring check: the speedup is worthless if `hma_series` still
        routes to the reference."""
        frame = fx.daily_frame(fx.accelerating_uptrend())
        expected = reference.hma(fx.daily_frame(fx.accelerating_uptrend())["Close"], 200)
        actual = sind.hma_series(frame, 200)
        assert relative_error(expected, actual) < TOLERANCE

    def test_the_reference_is_still_exported_unchanged(self):
        """`indicators.hma` remains the live technical filter's, and is
        still reachable as the comparison baseline."""
        assert sind.hma is reference.hma


class TestOrderPathIsolation:
    """Section 20: only the scanners may use the fast implementation."""

    def test_no_order_path_module_imports_fast_indicators(self):
        import ast

        forbidden_roots = {
            "broker", "brokers", "execution", "live_pilot", "risk_config",
            "account_risk", "paper_strategy_order", "daily_candidate_scanner",
            "order_monitor", "order_safety", "indicators", "market_data",
        }
        offenders = []
        for path in sorted(REPO_ROOT.glob("*.py")):
            if path.stem not in forbidden_roots:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if "fast_indicators" in node.module:
                        offenders.append(str(path.name))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if "fast_indicators" in alias.name:
                            offenders.append(str(path.name))
        assert offenders == [], offenders

    def test_the_reference_module_does_not_import_the_fast_one(self):
        """`indicators.py` gates real orders. It must not gain a
        dependency on scanner code."""
        source = (REPO_ROOT / "indicators.py").read_text(encoding="utf-8")
        assert "fast_indicators" not in source
        assert "scanners" not in source

    def test_fast_indicators_imports_nothing_from_the_trading_system(self):
        import ast

        path = REPO_ROOT / "scanners" / "base" / "fast_indicators.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        assert modules <= {"math", "numpy", "pandas"}, modules
