"""The scanners cannot reach the order path. Sections 1, 10, 23 and 30.

Everything else in this package is about finding good symbols. This file
is about the promise that finding them changes nothing else: no order,
no candidate publication, no risk decision, no kill-switch interaction,
and no behavioural change to the scanners that were already running.

These are structural tests, checked against the source tree rather than
against behaviour, because behaviour tests only prove that the paths
someone thought to exercise are safe. An import that does not exist
cannot be reached by a path nobody thought of.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCANNERS_DIR = REPO_ROOT / "scanners"

#: Modules the scanner package must never import. Each is a capability
#: that would let a scanner do something other than observe.
FORBIDDEN_PREFIXES = (
    "broker",            # Alpaca order submission
    "brokers",           # KIS order submission
    "execution",         # order gate, authorization, execution engine
    "live_pilot",        # the limited-live bootstrap
    "kis_live_trading",
    "kis_position_manager",
    "paper_strategy_order",
    "order_intent_ledger",
    "order_safety",
    "order_monitor",
    "kill_switch",
    "kill_switch_state",
    "risk_config",
    "account_risk",
    "positions",
    "reconciliation",
    "state_store",
)

#: Allowed, with reasons:
#:   indicators, score_scanner  -- reuse, per section 24
#:   market_hours, market_guard -- the trading-day calendar
#:   config.paths               -- project root resolution
#:   universe (read only)       -- section 3's data flow


def python_files():
    return sorted(path for path in SCANNERS_DIR.rglob("*.py"))


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module)
    return names


class TestNoOrderPath:
    @pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
    def test_no_scanner_module_imports_the_order_system(self, path):
        for module in imported_modules(path):
            root = module.split(".")[0]
            assert root not in FORBIDDEN_PREFIXES, (
                f"{path.relative_to(REPO_ROOT)} imports {module!r}. Section 30: adding "
                f"these scanners must not create a path to the order system.")

    def test_no_scanner_module_imports_the_trading_candidate_store(self):
        """Section 10 keeps the analytics store and the trading candidate
        store apart. `candidate_store.publish()` overwrites the shared
        `order_candidates.csv` the limited-live bootstrap reads -- a
        scanner able to call it could replace the validated scanner's
        candidate set with symbols that have no measured track record."""
        offenders = []
        for path in python_files():
            for module in imported_modules(path):
                if "candidate_store" in module:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == [], offenders

    def test_the_scanner_package_does_not_import_yfinance_outside_the_provider(self):
        """Only the provider talks to the data vendor. A scanner that
        imported it directly could bypass the seam section 3 exists to
        create, and would stop being unit-testable from fixtures."""
        allowed = {SCANNERS_DIR / "base" / "market_data_provider.py"}
        for path in python_files():
            if path in allowed:
                continue
            assert "yfinance" not in imported_modules(path), path


class TestCandidateDecisionLayerIsInert:
    def test_ships_disabled(self):
        """Section 30: installing the scanners must not, by itself,
        change what the live system can be handed."""
        from scanners import candidate_decision

        assert candidate_decision.is_enabled() is False

    def test_disabled_means_empty_not_pass_everything(self):
        """A disabled decision layer that quietly passed everything
        through would be worse than none at all."""
        from scanners import candidate_decision
        from scanners.base.models import ScannerSignal

        signals = [ScannerSignal(
            timestamp="2026-08-12T14:00:00+00:00", trading_day="2026-08-12",
            symbol="TEST", scanner_name="hma_early_trend",
            scanner_version="hma_early_trend_v1.0", scanner_score=99.0,
            signal_price=100.0)]
        assert candidate_decision.select_candidates(signals) == []

    def test_publishing_is_refused_with_a_stated_reason(self):
        from scanners import candidate_decision

        with pytest.raises(candidate_decision.CandidateDecisionDisabled,
                           match="not published"):
            candidate_decision.publish([{"symbol": "TEST"}])

    def test_the_policy_applies_when_deliberately_enabled(self):
        """The layer has to WORK, so that turning it on later is a
        config change and a wiring step rather than a rewrite."""
        from scanners import candidate_decision
        from scanners.base.config import ScannerConfig
        from scanners.base.models import ScannerSignal

        policy = ScannerConfig(
            scanner_name="candidate_decision", version="test_v1",
            params={"enabled": True, "eligible_scanners": ["hma_early_trend"],
                    "min_scanner_score": 70, "min_confirmation_count": 1,
                    "max_candidates": 2, "max_extension_hma200_pct": 25.0,
                    "rank_by": "scanner_score"})

        def make(symbol, score, scanner="hma_early_trend", extension=5.0):
            return ScannerSignal(
                timestamp="2026-08-12T14:00:00+00:00", trading_day="2026-08-12",
                symbol=symbol, scanner_name=scanner,
                scanner_version="v1", scanner_score=score, signal_price=100.0,
                extension_hma200_pct=extension)

        selected = candidate_decision.select_candidates(
            [make("HIGH", 95.0), make("LOW", 40.0), make("MID", 80.0),
             make("BEST", 99.0), make("OTHER", 99.0, scanner="orb"),
             make("STRETCHED", 98.0, extension=60.0)],
            policy=policy)

        symbols = [row["symbol"] for row in selected]
        assert symbols == ["BEST", "HIGH"], "ranked by score, capped at max_candidates"
        assert "LOW" not in symbols, "below the score floor"
        assert "OTHER" not in symbols, "scanner not eligible"
        assert "STRETCHED" not in symbols, "above the extension ceiling"

    def test_no_scheduled_entry_point_calls_the_decision_layer(self):
        """The runner and the report scripts must not reference it. It
        is reachable only by someone who wrote a script to call it."""
        for name in ("scanners/runner.py", "scripts/run_scanners.py",
                     "scripts/run_scanner_performance.py",
                     "scripts/run_scanner_report.py"):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            assert "candidate_decision" not in text, name


class TestExistingSystemUnchanged:
    def test_the_existing_score_scanner_is_not_modified(self):
        """Section S4 and section 24: the premarket adapter WRAPS this
        module. Its thresholds, its checks and its scoring must still be
        the ones it shipped with, or the momentum arm of the experiment
        stops being the system's real momentum logic."""
        from score_scanner.premarket_momentum_score import ScoreScannerConfig

        defaults = ScoreScannerConfig()
        assert defaults.min_score == 60
        assert defaults.min_premarket_gain_pct == 7.0
        assert defaults.min_volume_multiple == 2.0
        assert defaults.adx_threshold == 25.0
        assert defaults.near_52w_ratio == 0.98
        assert defaults.avg_volume_window == 20

    def test_the_adapter_does_not_mutate_the_wrapped_modules_defaults(self):
        """Constructing the adapter must not reach into the existing
        module's globals."""
        from score_scanner.premarket_momentum_score import ScoreScannerConfig
        from scanners.registry import build_scanner

        before = ScoreScannerConfig()
        build_scanner("premarket_momentum").score_scanner_config()
        assert ScoreScannerConfig() == before

    def test_the_shared_indicators_module_is_untouched(self):
        """`indicators.technical_entry_filter` gates real orders."""
        import indicators

        assert indicators.TECHNICAL_CHECK_COLUMNS == [
            "price_above_hma200", "hma200_rising", "hma_macd_bullish",
            "macd_histogram_rising", "sqzmom_green"]

    def test_scanners_only_read_the_universe_file(self):
        """Sections 1 and 3: the universe build is not touched.

        Checked on the parsed syntax tree rather than by searching the
        text, because the module's own docstring names
        `universe_builder.py` when explaining that it does not use it --
        a substring search would fail on the explanation instead of on
        an actual call.
        """
        import scanners.universe as universe_module

        path = Path(universe_module.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for module in imported_modules(path):
            root = module.split(".")[0]
            assert root not in {"universe_builder", "universe_daily_runner",
                                "universe_filter", "universe_metrics",
                                "universe_budget"}, module

        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        for writer in ("to_csv", "write_text", "write_bytes", "mkdir", "unlink",
                       "replace", "to_json"):
            assert writer not in called, f"scanners/universe.py calls {writer}"

    def test_the_analytics_store_is_a_different_directory(self, monkeypatch, tmp_path):
        """Section 10, checked without depending on the order-path module.

        The trading candidate store is not present on every branch this
        framework runs on; the requirement that the two stores stay
        apart is. Binding the test to that module's importability would
        mean the invariant is only verified on some branches -- and it
        matters most on the branch where the order path exists, which is
        precisely the one where a regression would be dangerous.

        So the separation is asserted from the scanner side, which is
        the side under test: different environment variable, different
        resolved directory, and no overlap between them.
        """
        from scanners.base import result_store

        orders = tmp_path / "orders"
        research = tmp_path / "research"
        monkeypatch.setenv("KIS_CANDIDATE_DIR", str(orders))
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(research))

        assert result_store.ANALYTICS_DIR_ENV == "SCANNER_ANALYTICS_DIR"
        assert result_store.ANALYTICS_DIR_ENV != "KIS_CANDIDATE_DIR"
        resolved = result_store.analytics_dir()
        assert resolved == research
        assert resolved != orders
        assert orders not in resolved.parents

        # Stronger check wherever the order-path module is available.
        try:
            from market_data import candidate_store
        except ImportError:
            return
        assert resolved != candidate_store.candidate_dir()
        assert result_store.ANALYTICS_DIR_ENV != candidate_store.CANDIDATE_DIR_ENV

    def test_a_scanner_run_writes_nothing_into_the_candidate_store(self, tmp_path,
                                                                   monkeypatch):
        """The end-to-end version of the structural test above."""
        from scanners import runner
        from scanners.base.market_data_provider import StaticMarketDataProvider
        from tests import scanner_fixtures as fx

        orders = tmp_path / "orders"
        orders.mkdir()
        monkeypatch.setenv("KIS_CANDIDATE_DIR", str(orders))

        bundle = fx.uptrend_bundle("TEST", volumes=fx.volume_surge())
        runner.run_scanners(
            symbols=["TEST"],
            provider=StaticMarketDataProvider(daily={"TEST": bundle.daily}),
            trading_day="2026-08-12")
        assert list(orders.iterdir()) == []
