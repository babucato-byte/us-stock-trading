"""S1 exit research: excursions, simulations, maturity, look-ahead (PHASE 5A).

The property under test everywhere is that this is POST-HOC. A horizon
that has not elapsed is PENDING, never measured over a shorter window;
and nothing on the scanning, ranking or live path may import this.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.analytics import exit_research as er  # noqa: E402


def bars(*triples):
    """(high, low, close) per session, oldest first."""
    return [{"high": h, "low": l, "close": c} for h, l, c in triples]


FLAT = bars((100, 100, 100), (100, 100, 100), (100, 100, 100),
            (100, 100, 100), (100, 100, 100))


class TestExcursion:
    def test_mfe_and_mae_with_their_timing(self):
        path = bars((102, 99, 101), (110, 100, 108), (109, 95, 96))
        result = er.excursion(100.0, path, horizon_days=3)
        assert result.complete is True
        assert result.mfe_pct == pytest.approx(0.10)
        assert result.time_to_mfe_days == 2
        assert result.mae_pct == pytest.approx(-0.05)
        assert result.time_to_mae_days == 3

    def test_an_incomplete_window_is_not_measured(self):
        """The look-ahead rule: 2 sessions is not a 5-day excursion."""
        result = er.excursion(100.0, bars((110, 95, 105), (112, 98, 110)),
                              horizon_days=5)
        assert result.complete is False
        assert result.mfe_pct is None and result.mae_pct is None
        assert result.sessions_used == 2

    def test_no_favourable_move_is_zero_not_negative(self):
        result = er.excursion(100.0, bars((99, 90, 95), (98, 92, 97),
                                          (97, 91, 93)), horizon_days=3)
        assert result.mfe_pct == 0.0
        assert result.time_to_mfe_days is None
        assert result.mae_pct == pytest.approx(-0.10)

    def test_a_flat_path_has_neither_excursion(self):
        result = er.excursion(100.0, FLAT, horizon_days=5)
        assert result.mfe_pct == 0.0 and result.mae_pct == 0.0

    @pytest.mark.parametrize("price", [None, 0.0, -5.0, float("nan"), True])
    def test_an_unusable_entry_price_measures_nothing(self, price):
        assert er.excursion(price, FLAT, horizon_days=1).mfe_pct is None

    def test_missing_bar_fields_do_not_crash(self):
        path = [{"high": None, "low": None, "close": None}] * 3
        result = er.excursion(100.0, path, horizon_days=3)
        assert result.complete is True
        assert result.mfe_pct is None and result.mae_pct is None

    def test_it_is_deterministic(self):
        path = bars((105, 97, 103), (110, 99, 108), (108, 94, 95))
        first = er.excursion(100.0, path, horizon_days=3).as_dict()
        second = er.excursion(100.0, path, horizon_days=3).as_dict()
        assert first == second


class TestStopSimulation:
    def test_a_stop_that_is_hit_records_the_day(self):
        path = bars((101, 99, 100), (100, 95, 96), (99, 94, 95))
        out = er.simulate_stop(100.0, path, -0.03, horizon_days=3)
        assert out.hit is True and out.hit_day == 2
        assert out.return_at_stop == pytest.approx(-0.03)

    def test_a_stop_that_is_never_reached(self):
        out = er.simulate_stop(100.0, bars((102, 99, 101), (103, 98, 102),
                                           (104, 99, 103)), -0.05, horizon_days=3)
        assert out.hit is False and out.hit_day is None

    def test_a_premature_stop_is_flagged(self):
        """§7's case: stopped at -3%, then the name ran +12%."""
        path = bars((100, 96, 97), (105, 98, 104), (113, 103, 112))
        out = er.simulate_stop(100.0, path, -0.03, horizon_days=3)
        assert out.hit is True
        assert out.max_upside_after_stop == pytest.approx(0.13)
        assert out.premature is True

    def test_a_stop_that_avoided_a_worse_loss(self):
        path = bars((100, 96, 97), (97, 90, 91), (92, 80, 82))
        out = er.simulate_stop(100.0, path, -0.03, horizon_days=3)
        assert out.hit is True
        assert out.return_if_held == pytest.approx(-0.18)
        assert out.avoided_worse is True
        assert out.premature is False

    def test_an_incomplete_window_simulates_nothing(self):
        out = er.simulate_stop(100.0, bars((100, 90, 95)), -0.03, horizon_days=5)
        assert out.complete is False and out.hit is False

    def test_every_research_level_is_negative_and_not_a_risk_setting(self):
        assert all(level < 0 for level in er.STOP_CANDIDATES)
        assert er.STOP_CANDIDATES == (-0.02, -0.03, -0.04, -0.05, -0.06, -0.08)
        import risk_config

        # The research levels are NOT the production stop.
        assert risk_config.STOP_LOSS_RATE == -0.08


class TestTargetSimulation:
    def test_a_target_that_is_hit_records_the_day_and_the_cost(self):
        path = bars((103, 99, 102), (109, 101, 108), (120, 107, 118))
        out = er.simulate_target(100.0, path, 0.05, horizon_days=3)
        assert out.hit is True and out.hit_day == 2
        assert out.max_upside_after_hit == pytest.approx(0.20)
        assert out.forgone_pct == pytest.approx(0.15)

    def test_a_target_never_reached(self):
        out = er.simulate_target(100.0, FLAT, 0.10, horizon_days=5)
        assert out.hit is False and out.forgone_pct is None

    def test_a_target_hit_at_the_top_costs_nothing(self):
        path = bars((105, 99, 104), (104, 100, 102), (103, 99, 101))
        out = er.simulate_target(100.0, path, 0.05, horizon_days=3)
        assert out.hit is True
        assert out.forgone_pct == pytest.approx(0.0)

    def test_an_incomplete_window_simulates_nothing(self):
        out = er.simulate_target(100.0, bars((120, 99, 118)), 0.05, horizon_days=5)
        assert out.complete is False and out.hit is False


class TestTimeExitSimulation:
    def test_it_returns_the_close_of_the_nth_session(self):
        path = bars((101, 99, 100), (105, 100, 104), (108, 102, 107))
        assert er.simulate_time_exit(100.0, path, horizon_days=3)["return_pct"] \
            == pytest.approx(0.07)

    def test_a_shorter_hold_is_a_different_number(self):
        path = bars((101, 99, 100), (105, 100, 104), (108, 102, 107))
        assert er.simulate_time_exit(100.0, path, horizon_days=1)["return_pct"] \
            == pytest.approx(0.0)

    def test_an_incomplete_window_is_not_measured(self):
        out = er.simulate_time_exit(100.0, bars((110, 99, 108)), horizon_days=5)
        assert out["complete"] is False and out["return_pct"] is None


class TestDistributions:
    def test_percentiles_and_median_are_reported_not_just_the_mean(self):
        stats = er.distribution([0.01, 0.02, 0.03, 0.04, 1.40])
        assert stats["n"] == 5
        assert stats["median"] == pytest.approx(0.03)
        assert stats["mean"] > stats["median"], "one outlier moves the mean"
        assert stats["p25"] is not None and stats["p95"] is not None

    def test_an_empty_sample_is_all_none_with_n_zero(self):
        stats = er.distribution([])
        assert stats["n"] == 0
        assert all(stats[k] is None for k in ("mean", "median", "p25", "p95"))

    def test_a_single_value_is_every_percentile(self):
        stats = er.distribution([0.05])
        assert stats["median"] == stats["p25"] == stats["p95"] == pytest.approx(0.05)

    def test_nulls_are_excluded_not_zeroed(self):
        rows = [{"return_1d": 0.10}, {"return_1d": None}, {"return_1d": 0.20}]
        values = er.numbers(rows, "return_1d")
        assert values == [0.10, 0.20]
        assert er.distribution(values)["mean"] == pytest.approx(0.15)


class TestMaturity:
    def test_no_signals_is_insufficient(self):
        block = er.maturity([])
        assert block["signals"] == 0
        assert block["status"] == er.INSUFFICIENT

    def test_status_is_driven_by_the_matured_five_day_count(self):
        rows = [{"return_1d": 0.01, "return_3d": 0.01, "return_5d": 0.01}] * 25
        block = er.maturity(rows)
        assert block["matured_return_5d"] == 25
        assert block["status"] == er.EARLY
        assert block["status_basis"] == "matured_return_5d"

    def test_pending_horizons_do_not_count_as_matured(self):
        """A signal from yesterday has no 5-day return."""
        rows = [{"return_1d": 0.01, "return_3d": None, "return_5d": None}] * 10
        block = er.maturity(rows)
        assert block["matured_return_1d"] == 10
        assert block["matured_return_5d"] == 0
        assert block["status"] == er.INSUFFICIENT

    @pytest.mark.parametrize("count,expected", [
        (0, er.INSUFFICIENT), (19, er.INSUFFICIENT), (20, er.EARLY),
        (59, er.EARLY), (60, er.PROVISIONAL), (150, er.MATURE)])
    def test_the_bands(self, count, expected):
        assert er.classify_maturity(count) == expected


class TestReport:
    def test_an_empty_window_reports_zero_and_withholds_simulations(self):
        report = er.build_report("2026-08-17", "2026-08-21", rows=[])
        assert report["maturity"]["signals"] == 0
        assert report["policy_status"] == "BLOCKED_BY_SAMPLE_MATURITY"
        assert "simulations_withheld" in report
        assert er.format_report(report)

    def test_a_thin_sample_still_withholds_simulations(self):
        """A stop chosen from four signals is a stop chosen from noise."""
        rows = [{"return_1d": 0.01, "return_5d": 0.02, "mfe_5d": 0.05,
                 "mae_5d": -0.02}] * 4
        report = er.build_report("2026-08-17", "2026-08-21", rows=rows)
        assert report["maturity"]["status"] == er.INSUFFICIENT
        assert "simulations_withheld" in report

    def test_the_report_never_recommends_a_level(self):
        """Tests the property, not the vocabulary.

        The notice legitimately contains the word "recommended" -- it is
        the sentence saying nothing IS recommended. What must be absent
        is a chosen VALUE: no recommendation field, and no key that
        names a single stop or target as the answer.
        """
        report = er.build_report("2026-08-17", "2026-08-21", rows=[])
        for forbidden in ("recommended_stop", "recommended_target",
                          "chosen_stop", "suggested_stop", "stop_loss_rate",
                          "take_profit_rate", "recommendation"):
            assert forbidden not in report, forbidden
        assert report["policy_status"] == "BLOCKED_BY_SAMPLE_MATURITY"
        # The candidate LISTS are present; a single chosen value is not.
        assert len(report["stop_candidates"]) == 6
        assert "research only" in er.format_report(report).lower()

    def test_the_candidate_levels_are_never_narrowed_to_one(self):
        rows = [{"return_1d": 0.03, "return_5d": 0.05, "mfe_5d": 0.09,
                 "mae_5d": -0.04}] * 200
        report = er.build_report("2026-08-17", "2026-08-21", rows=rows)
        assert report["maturity"]["status"] == er.MATURE
        # Even at MATURE the report offers candidates, never a decision.
        assert len(report["stop_candidates"]) == 6
        assert report["policy_status"] == "BLOCKED_BY_SAMPLE_MATURITY"

    def test_it_is_deterministic(self):
        rows = [{"return_1d": 0.03, "return_5d": 0.05, "mfe_5d": 0.09,
                 "mae_5d": -0.04}] * 30
        first = er.build_report("2026-08-17", "2026-08-21", rows=rows)
        second = er.build_report("2026-08-17", "2026-08-21", rows=rows)
        assert first == second

    def test_win_rate_is_reported_next_to_the_returns(self):
        rows = [{"return_1d": 0.05}, {"return_1d": -0.02}, {"return_1d": 0.01}]
        report = er.build_report("2026-08-17", "2026-08-21", rows=rows)
        assert report["returns"]["return_1d"]["positive_rate"] == pytest.approx(2 / 3)


class TestLookAheadIsolation:
    """§15: research must never feed back into what gets scanned or traded."""

    def _imports(self, path):
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
                names.update(f"{node.module}.{a.name}" for a in node.names)
        return names

    def test_no_decision_path_imports_the_research(self):
        """Walks the decision packages that EXIST rather than a fixed list.

        A hardcoded list would have to differ between the scanner-only
        branch and the KIS-integrated one, and a list that differs by
        branch is a list that stops covering a file the day it moves.
        Walking whichever of these directories is present covers both,
        and a package added later is covered the moment it appears.
        """
        packages = [name for name in ("scanners", "watchlist", "s1_live")
                    if (REPO_ROOT / name).is_dir()]
        assert "scanners" in packages, "the scanner package must exist"

        offenders = []
        for package in packages:
            for path in sorted((REPO_ROOT / package).rglob("*.py")):
                if path.name == "exit_research.py":
                    continue
                for module in self._imports(path):
                    if "exit_research" in module:
                        offenders.append(f"{path.relative_to(REPO_ROOT)} -> {module}")
        assert offenders == [], (
            "research fed back into a decision path: " + str(offenders))

    def test_the_research_never_imports_an_order_or_scanner_decision_path(self):
        forbidden = {"execution", "brokers", "broker", "kis_live_trading",
                     "s1_live", "watchlist", "live_pilot"}
        for module in self._imports(REPO_ROOT / "scanners/analytics/exit_research.py"):
            assert module.split(".")[0] not in forbidden, module

    def test_the_research_writes_nothing_to_the_signal_store(self):
        """The source data is READ ONLY."""
        source = (REPO_ROOT / "scanners" / "analytics" / "exit_research.py").read_text()
        tree = ast.parse(source)
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        for writer in ("write_signals", "write_performance", "write_run_manifest",
                       "purge_day", "to_csv", "write_text", "unlink"):
            assert writer not in called, f"exit_research calls {writer}"

    def test_only_s1_rows_are_analysed(self):
        assert er.S1_SCANNER == "hma_early_trend"
        source = (REPO_ROOT / "scanners" / "analytics" / "exit_research.py").read_text()
        assert 'scanner_name")) == S1_SCANNER' in source


class TestScannerConfigUntouched:
    """§12: the research must not have moved a scanner parameter."""

    def test_the_s1_scanner_config_is_unchanged(self):
        from scanners.registry import build_scanner

        scanner = build_scanner("hma_early_trend")
        assert scanner.version == "hma_early_trend_v1.0"

    def test_candidate_decision_is_still_disabled(self):
        from scanners import candidate_decision

        assert candidate_decision.is_enabled() is False

    def test_the_live_rollout_is_untouched(self):
        """Stronger check wherever the live-rollout module is available.

        Same pattern, and the same reason, as
        `test_scanner_trading_isolation.py`'s candidate-store check: the
        module is not present on every branch this analytics package runs
        on, but the requirement that research changes nothing about live
        trading is. Binding the test to the module's importability would
        mean the invariant is only verified on some branches.
        """
        try:
            from config.live_rollout_config import LiveRolloutConfig
        except ImportError:
            return

        config = LiveRolloutConfig.from_env({})
        assert config.enabled is False
        assert (config.max_quantity_per_order, config.max_open_positions,
                config.max_daily_entries) == (1, None, None)
