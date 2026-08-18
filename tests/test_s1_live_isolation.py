"""S1 Limited Live stays off the order path until something turns it on.

Two questions this file answers, both structurally:

1. Does adding an S1 candidate source change the EXISTING KIS path?
   It must not. With the source switched off, the cycle must ask for the
   same symbols and hand the gate the same allow-list it did before.

2. Can publishing a candidate set cause an order?
   It must not. Publication writes two files; every gate that decides
   whether an order exists is downstream and unchanged, and the rollout
   flags stay exactly where they shipped.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

S1_DIR = REPO_ROOT / "s1_live"


def imported_modules(path):
    """Both `X` and `X.Y` for `from X import Y` -- see the note in
    tests/test_watchlist_isolation.py about why the bare form alone
    lets `from market_data import candidate_store` slip through."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module)
                for alias in node.names:
                    names.add(f"{node.module}.{alias.name}")
    return names


def python_files(directory):
    return sorted(directory.rglob("*.py"))


@pytest.fixture(autouse=True)
def _isolate_durable_state(tmp_path, monkeypatch):
    """Point the order-state DB and the shared stores at tmp_path.

    `run_live_buy_entry_cycle` calls `_persist_blocked_record()` BEFORE it
    raises on a structural refusal, and that writes a Shadow Mode row
    through `state_store.db.open_db()`. Without this, the refusal test
    below creates a real `TRADING_STATE.db` at the repository root -- and
    then `test_state_store.py::test_real_db_file_never_created_by_tests`
    fails, along with every audit-coverage test that expected a fresh
    database. That is exactly what happened the first time this file ran
    in the full suite: 20 failures, none of them in this file.

    The KIS test modules each set `STATE_STORE_DB_FILE` themselves; this
    is the same convention, applied to the whole module so a test added
    later cannot forget it.
    """
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))
    monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "shared_state"))


class TestPublisherNeverTouchesTheTradingCandidateStore:
    @pytest.mark.parametrize("path", python_files(S1_DIR), ids=lambda p: p.name)
    def test_no_candidate_store_or_decision_import(self, path):
        for module in imported_modules(path):
            assert "candidate_store" not in module, f"{path.name} imports {module}"
            assert "candidate_decision" not in module, f"{path.name} imports {module}"

    #: The ONE file in s1_live/ that is allowed to reach the order path,
    #: named here rather than excluded silently. `executor.py` is the
    #: orchestrator the server runs unattended: it calls
    #: `kis_live_trading.run_live_buy_entry_cycle()` for entries and hands
    #: a broker adapter to the exit runtime. Every other module in this
    #: package produces or evaluates data and must stay unable to trade,
    #: which is what the sweep below still enforces.
    MAY_REACH_THE_ORDER_PATH = {"executor.py"}

    @pytest.mark.parametrize("path", python_files(S1_DIR), ids=lambda p: p.name)
    def test_the_publisher_cannot_submit_an_order(self, path):
        """It may not import the engine, the broker or the gate. The
        publisher's job ends at two files on disk."""
        if path.name in self.MAY_REACH_THE_ORDER_PATH:
            pytest.skip(f"{path.name} is the declared order-path orchestrator")
        forbidden = {"execution", "brokers", "broker", "kis_live_trading",
                     "execution.execution_engine", "live_pilot"}
        for module in imported_modules(path):
            assert module.split(".")[0] not in forbidden, f"{path.name} imports {module}"

    def test_the_order_path_exception_list_stays_exactly_one_file(self):
        """The exemption is a door, so it is worth counting. A second name
        appearing here should have to be argued for in review rather than
        added quietly alongside the first."""
        assert self.MAY_REACH_THE_ORDER_PATH == {"executor.py"}
        present = {p.name for p in python_files(S1_DIR)}
        assert self.MAY_REACH_THE_ORDER_PATH <= present, "exemption names a missing file"

    def test_the_exempt_orchestrator_still_owns_no_policy(self):
        """It may place orders; it may not decide what to place. Entry
        gating stays in kis_live_trading, exit policy in exit_policy."""
        from s1_live import executor

        source = (S1_DIR / "executor.py").read_text()
        for owned_elsewhere in ("HARD_STOP_PCT", "SCORE_THRESHOLD", "adx_min",
                                "PROFIT_PROTECTION_STEPS"):
            assert owned_elsewhere not in source, owned_elsewhere
        assert executor.STRATEGY_ID == "hma_early_trend"

    def test_the_two_stores_use_different_filenames_and_variables(self):
        from market_data import candidate_store
        from s1_live import store

        assert store.CANDIDATE_FILE != candidate_store.CANDIDATE_FILE
        assert store.MANIFEST_FILE != candidate_store.MANIFEST_FILE
        assert store.S1_CANDIDATE_DIR_ENV != candidate_store.CANDIDATE_DIR_ENV

    def test_publishing_writes_only_its_own_two_files(self, tmp_path, monkeypatch):
        from s1_live import publisher, store
        from scanners.base import result_store, run_context
        from scanners.base.models import ScannerSignal

        shared = tmp_path / "shared_state"
        shared.mkdir()
        # Point the TRADING candidate store at the same directory on
        # purpose: if the publisher were ever going to write the wrong
        # filename, this is where it would show up.
        monkeypatch.setenv("KIS_CANDIDATE_DIR", str(shared))
        monkeypatch.setenv(store.S1_CANDIDATE_DIR_ENV, str(shared))
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))

        result_store.write_signals([ScannerSignal(
            timestamp="2026-08-17T20:10:00+00:00", trading_day="2026-08-17",
            symbol="NVDA", scanner_name="hma_early_trend",
            scanner_version="hma_early_trend_v1.0", scanner_score=80.0,
            signal_price=100.0, scanner_run_id="R1")], trading_day="2026-08-17")
        result_store.write_run_manifest({
            "run_id": "R1", "trading_day": "2026-08-17", "profile": "daily",
            "run_status": run_context.SUCCESS, "market_data_provider": "yfinance",
            "scanners": [{"scanner_name": "hma_early_trend", "status": run_context.SUCCESS,
                          "scanner_version": "hma_early_trend_v1.0",
                          "config_fingerprint": "fp", "failed": False}],
        }, trading_day="2026-08-17")

        publisher.publish("2026-08-17")

        written = sorted(p.name for p in shared.iterdir())
        assert written == ["s1_live_candidates.csv", "s1_live_candidates.manifest.json"]
        assert "order_candidates.csv" not in written
        assert "order_candidates.manifest.json" not in written

    def test_candidate_decision_is_still_disabled(self):
        from scanners import candidate_decision

        assert candidate_decision.is_enabled() is False


class TestExistingKISPathUnchanged:
    """S1 mode OFF -> the cycle behaves exactly as it did before."""

    class Rollout:
        allowed_symbols = frozenset({"AAPL", "MSFT"})

    def test_the_default_source_returns_the_legacy_watchlist(self, monkeypatch):
        from s1_live import candidate_source

        calls = []

        def fake_watchlist():
            calls.append(1)
            return ["AAPL", "MSFT", "TSLA"]

        source = candidate_source.LegacyWatchlistSource(
            self.Rollout(), load_watchlist=fake_watchlist)
        assert source.symbols() == ["AAPL", "MSFT", "TSLA"]
        assert source.allowed_symbols() == {"AAPL", "MSFT"}
        assert calls == [1]

    def test_resolve_without_the_env_var_is_the_legacy_source(self):
        from s1_live import candidate_source

        source = candidate_source.resolve(self.Rollout(), trading_day="2026-08-17", env={})
        assert isinstance(source, candidate_source.LegacyWatchlistSource)
        assert source.allowed_symbols() is self.Rollout.allowed_symbols

    def test_the_legacy_source_reproduces_the_original_two_expressions(self, monkeypatch):
        """The inline code was `pso.load_watchlist()` and
        `rollout.allowed_symbols`. Assert the source is those, exactly."""
        import kis_live_trading as klt
        from s1_live import candidate_source

        sentinel = ["ZZZ", "YYY"]
        monkeypatch.setattr(klt.pso, "load_watchlist", lambda: sentinel)
        source = candidate_source.LegacyWatchlistSource(
            self.Rollout(), watchlist_module=klt.pso)
        assert source.symbols() == sentinel
        assert source.allowed_symbols() is self.Rollout.allowed_symbols

    def test_the_source_reads_the_callers_module_even_when_sys_modules_lost_it(
            self, monkeypatch):
        """Regression: eighteen existing tests failed on this.

        `tests/test_ai_analysis.py` pops "paper_strategy_order" out of
        sys.modules and leaves it popped, on purpose. If the candidate
        source performs its own `import paper_strategy_order`, it builds
        a NEW module object -- so a monkeypatch on `klt.pso.load_watchlist`
        (which every existing KIS test uses) is invisible to it, the real
        loader runs, and the cycle silently evaluates zero symbols.

        This reproduces that exact condition rather than trusting the
        alphabetical accident that surfaced it.
        """
        import sys

        import kis_live_trading as klt
        from s1_live import candidate_source

        sentinel = ["PATCHED"]
        monkeypatch.setattr(klt.pso, "load_watchlist", lambda: sentinel)
        # Exactly what test_ai_analysis leaves behind.
        monkeypatch.delitem(sys.modules, "paper_strategy_order", raising=False)
        assert "paper_strategy_order" not in sys.modules

        source = candidate_source.resolve(
            self.Rollout(), trading_day="2026-08-17", env={},
            watchlist_module=klt.pso)
        assert source.symbols() == sentinel, (
            "the source re-imported the module instead of using the caller's")

    def test_the_cycle_accepts_an_injected_source(self):
        """The parameter exists and defaults to None, so every existing
        caller keeps the resolved-from-environment behaviour."""
        import inspect

        import kis_live_trading as klt

        signature = inspect.signature(klt.run_live_buy_entry_cycle)
        assert "candidate_source" in signature.parameters
        assert signature.parameters["candidate_source"].default is None

    def test_there_is_still_exactly_one_entry_cycle(self):
        """A second candidate source must not have created a second
        pipeline: two pipelines are two ideas of what is safe."""
        text = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        cycles = [node.name for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef)
                  and "buy_entry_cycle" in node.name]
        assert cycles == ["run_live_buy_entry_cycle"], cycles

    def test_the_gates_are_still_called_exactly_once_each(self):
        """order_gate / entry_limits / execution_engine each appear in
        one pipeline, not two."""
        text = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")
        assert text.count("entry_limits.collect(") == 1
        assert text.count("order_gate.BuyGateContext(") == 1
        # == 1, not <= 1: "at most one" would also pass if the call had
        # been removed entirely, which is not the property being checked.
        assert text.count("execution_engine.submit_buy_order(") == 1


class TestOrdersRemainBlocked:
    def test_rollout_flags_are_unchanged(self):
        from config.live_rollout_config import LiveRolloutConfig

        config = LiveRolloutConfig.from_env({})
        assert config.enabled is False
        assert config.max_quantity_per_order == 1
        assert config.max_open_positions == 1
        assert config.max_daily_entries == 1
        assert config.allow_fractional is False
        assert config.allow_leverage is False
        assert config.allow_margin is False
        assert config.allow_extended_hours is False
        assert config.allow_short is False
        assert config.allow_inverse is False
        assert config.allow_market_order is False

    def test_an_empty_allowlist_still_rejects_everything(self):
        """Untouched behaviour -- the fail-closed path needed no new gate."""
        from live_readiness.allowlist import is_symbol_allowed

        assert is_symbol_allowed("AAPL", frozenset()) is False
        assert is_symbol_allowed("AAPL", None) is False

    def test_publishing_does_not_flip_any_live_flag(self, tmp_path, monkeypatch):
        from config.live_rollout_config import LiveRolloutConfig
        from live_pilot import posture

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "a"))
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("S1_LIVE_SOURCE_ENABLED", "true")

        assert LiveRolloutConfig.from_env().enabled is False
        assert posture.resolve_posture().posture == posture.POSTURE_OBSERVE

    def test_the_cycle_refuses_before_any_symbol_when_rollout_is_off(self, monkeypatch):
        """LIVE_ROLLOUT_ENABLED=false -> zero broker submissions, and the
        refusal happens before the candidate source is even consulted."""
        import kis_live_trading as klt
        from config.live_rollout_config import LiveRolloutConfig
        from s1_live import candidate_source

        class ExplodingSource(candidate_source.CandidateSource):
            name = "should_never_be_asked"

            def symbols(self):
                raise AssertionError("the cycle consulted the source despite rollout=off")

            def allowed_symbols(self):
                raise AssertionError("the cycle consulted the source despite rollout=off")

        class ExplodingBroker:
            def submit_order(self, *a, **kw):
                raise AssertionError("an order was submitted")

            def get_positions(self):
                raise AssertionError("the broker was contacted")

        with pytest.raises(klt.KISLiveTradingError, match="live_rollout.enabled is False"):
            klt.run_live_buy_entry_cycle(
                broker=ExplodingBroker(),
                live_rollout=LiveRolloutConfig.from_env({}),
                candidate_source=ExplodingSource())
