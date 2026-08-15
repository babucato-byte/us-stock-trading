"""The Manual Watchlist cannot reach the order path -- in either direction.

Track C-6. Structural tests against the source tree rather than
behavioural ones, for the same reason `test_scanner_trading_isolation.py`
gives: a behaviour test proves the paths someone thought to exercise are
safe, while an import that does not exist cannot be reached by a path
nobody thought of.

Two directions matter, and only one of them is obvious:

    watchlist -> order    the watchlist must not be able to ACT
    order -> watchlist    the order path must not be able to READ it

The second is the one that actually enforces MANUAL_ONLY. A watchlist
with no outbound imports is still an order input the moment something on
the order side imports it, and at that point the "manual" in the name is
the only thing standing between a reading list and a trade.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WATCHLIST_DIR = REPO_ROOT / "watchlist"

#: Every module that can place, size, authorise or publish for an order.
#: Same list as the scanner isolation test, plus the decision layer and
#: the watchlist-specific ones.
FORBIDDEN_ROOTS = frozenset({
    "broker", "brokers", "execution", "live_pilot", "live_readiness",
    "kis_live_trading", "kis_position_manager", "paper_strategy_order",
    "order_intent_ledger", "order_safety", "order_monitor",
    "kill_switch", "kill_switch_state", "risk_config", "account_risk",
    "positions", "reconciliation", "state_store", "domain",
    "daily_candidate_scanner", "premarket_scan_runner", "shadow_mode",
    "operations", "market_data",
})

#: Modules the watchlist package may import. Anything else is a new
#: dependency that has to be justified by editing this list, which is
#: the point: the review happens here rather than in a diff nobody
#: connected to the isolation promise.
ALLOWED_NON_STDLIB = frozenset({
    "watchlist", "scanners", "config", "market_hours",
})

STDLIB_HINTS = frozenset({
    "json", "os", "sys", "math", "logging", "argparse", "datetime",
    "pathlib", "typing", "tempfile", "collections", "statistics",
    "dataclasses", "itertools", "functools", "re", "decimal",
})


def python_files(directory):
    return sorted(path for path in directory.rglob("*.py"))


def imported_modules(path):
    """Every module name this file imports, INCLUDING the dotted form of
    `from X import Y`.

    Recording only `node.module` for an `ImportFrom` loses the name that
    was actually imported, and that gap is not theoretical: a mutation
    test of this file showed `from market_data import candidate_store`
    being recorded as plain `market_data`, so a check looking for the
    substring "candidate_store" never saw it. Both `market_data` and
    `market_data.candidate_store` are emitted here, so a root-prefix
    check and a substring check both work.
    """
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


def called_attributes(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


class TestWatchlistCannotReachTheOrderPath:
    @pytest.mark.parametrize("path", python_files(WATCHLIST_DIR), ids=lambda p: p.name)
    def test_no_forbidden_import(self, path):
        for module in imported_modules(path):
            root = module.split(".")[0]
            assert root not in FORBIDDEN_ROOTS, (
                f"{path.relative_to(REPO_ROOT)} imports {module!r}. The Manual "
                f"Watchlist is MANUAL_ONLY and must not reach the order system.")

    @pytest.mark.parametrize("path", python_files(WATCHLIST_DIR), ids=lambda p: p.name)
    def test_only_allowlisted_dependencies(self, path):
        for module in imported_modules(path):
            root = module.split(".")[0]
            assert root in ALLOWED_NON_STDLIB or root in STDLIB_HINTS, (
                f"{path.relative_to(REPO_ROOT)} imports {module!r}, which is not on "
                f"the watchlist dependency allow-list. Add it deliberately or "
                f"remove the import.")

    def test_never_touches_the_candidate_store(self):
        """Neither the trading candidate store nor the decision layer.

        `candidate_store.publish()` overwrites the file the limited-live
        bootstrap reads. A watchlist able to call it would be an order
        input wearing a reading list's name.
        """
        offenders = []
        for path in python_files(WATCHLIST_DIR):
            for module in imported_modules(path):
                if "candidate_store" in module or "candidate_decision" in module:
                    offenders.append(f"{path.name} -> {module}")
        assert offenders == [], offenders

    def test_the_cli_is_bound_by_the_same_rules(self):
        """The entry point is where an import would actually be added."""
        path = REPO_ROOT / "scripts" / "run_manual_watchlist.py"
        for module in imported_modules(path):
            root = module.split(".")[0]
            assert root not in FORBIDDEN_ROOTS, f"{path.name} imports {module!r}"
            assert "candidate_store" not in module
            assert "candidate_decision" not in module


class TestNothingOnTheOrderSideReadsTheWatchlist:
    """The direction that actually enforces MANUAL_ONLY."""

    def test_no_repository_module_imports_watchlist(self):
        offenders = []
        skip_dirs = {".git", "venv", ".venv", "node_modules", "__pycache__",
                     "watchlist", "tests"}
        for path in REPO_ROOT.rglob("*.py"):
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.name == "run_manual_watchlist.py":
                continue  # the watchlist's own entry point
            try:
                modules = imported_modules(path)
            except (SyntaxError, UnicodeDecodeError):
                continue
            for module in modules:
                if module.split(".")[0] == "watchlist":
                    offenders.append(f"{path.relative_to(REPO_ROOT)} -> {module}")
        assert offenders == [], (
            "the Manual Watchlist became an input to something else: " + str(offenders))

    def test_the_scanner_package_does_not_import_the_watchlist(self):
        """The dependency runs one way. Scanners produce signals; the
        watchlist consumes them. A scanner reading the watchlist would
        make a run's output depend on a previous run's reading list."""
        for path in python_files(REPO_ROOT / "scanners"):
            assert not any(module.split(".")[0] == "watchlist"
                           for module in imported_modules(path)), path


class TestWatchlistWritesOnlyItsOwnFiles:
    def test_only_the_store_module_writes(self):
        """Every write goes through `watchlist/store.py`, so there is one
        place to check that the destination is the watchlist directory."""
        writers = {"write_text", "write_bytes", "to_csv", "to_json", "unlink",
                   "replace", "mkdir", "fsync"}
        for path in python_files(WATCHLIST_DIR):
            if path.name == "store.py":
                continue
            offending = called_attributes(path) & writers
            assert offending == set(), f"{path.name} performs I/O: {offending}"

    def test_the_store_writes_under_the_watchlist_directory(self, tmp_path, monkeypatch):
        from watchlist import config, store

        monkeypatch.setenv(config.WATCHLIST_DIR_ENV, str(tmp_path / "wl"))
        written = store.write_json({"entries": []}, trading_day="2026-08-17",
                                   stage=config.STAGE_TODAY)
        assert Path(written).parent == tmp_path / "wl"

    def test_the_store_env_var_is_not_the_candidate_store_one(self):
        from watchlist import config

        assert config.WATCHLIST_DIR_ENV == "MANUAL_WATCHLIST_DIR"
        assert config.WATCHLIST_DIR_ENV not in {"KIS_CANDIDATE_DIR",
                                                "SCANNER_ANALYTICS_DIR"}

    def test_a_full_build_writes_nothing_into_the_candidate_store(
            self, tmp_path, monkeypatch):
        """The end-to-end version of the structural tests above."""
        from scanners.base import result_store
        from watchlist import builder, config, store

        orders = tmp_path / "orders"
        orders.mkdir()
        monkeypatch.setenv("KIS_CANDIDATE_DIR", str(orders))
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))
        monkeypatch.setenv(config.WATCHLIST_DIR_ENV, str(tmp_path / "wl"))

        result_store.write_signals([], trading_day="2026-08-14")
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        store.write_json(payload, trading_day="2026-08-17",
                         stage=config.STAGE_TOMORROW)

        assert list(orders.iterdir()) == []
