"""Every session builds its own candidate pool, from nothing if it must.

On 2026-08-31 the Monday DAYTIME session opened with candidates=0 and
the reason was not a quiet market:

    manifest unusable (WRONG_TRADING_DAY: manifest says '2026-08-28')
    no active universe available; run the daily profile first
    universe fill: 0 of 300 eligible after 0 considered

Three producers had to have run first and none had. Worse, the ranking
DID exist -- 2MB of it -- in the tree the daily profile writes to, while
the release scanner read an empty directory two paths away. "No active
universe" was true of the directory and false of the system.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import session_discovery as sd  # noqa: E402


def _store(directory, symbols, provider="yfinance", updated_at="2026-08-28T21:53:17Z"):
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"provider": provider, "updated_at": updated_at,
               "symbols": {s: {"dollar_volume": v, "price": 10.0}
                           for s, v in symbols.items()}}
    (directory / f"{provider}.json").write_text(json.dumps(payload))
    return directory


class TestASessionNeedsNoOtherSessionToHaveRun:
    """The core property. With no manifest and no prior candidates, the
    pool still has to come out non-empty."""

    def test_coarse_discovery_alone_produces_a_pool(self, tmp_path):
        _store(tmp_path / "activity", {"AAA": 9e8, "BBB": 5e8, "CCC": 1e8})
        pool = sd.build_pool(session="OVERNIGHT_DAYTIME",
                             operational_trading_day="2026-08-31", limit=10,
                             held_symbols=(), prior_symbols=(),
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert pool["symbols"] == ["AAA", "BBB", "CCC"]
        assert pool["from_coarse"] == 3
        assert pool["reason"] == sd.OK

    def test_it_ranks_by_dollar_volume_not_share_count(self, tmp_path):
        """A penny stock trading millions of shares is not more worth a
        realtime slot than a liquid name."""
        _store(tmp_path / "activity", {"SMALL": 1e6, "BIG": 9e9, "MID": 5e8})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=3,
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert pool["symbols"] == ["BIG", "MID", "SMALL"]

    def test_no_ranking_at_all_reports_why(self, tmp_path):
        """Zero is allowed. Zero without a reason is what sent people
        looking for a market explanation of a config problem."""
        pool = sd.build_pool(session="PREMARKET",
                             operational_trading_day="2026-08-31", limit=10,
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "nope")})
        assert pool["symbols"] == []
        assert pool["reason"] == sd.NO_ACTIVITY_RANKING

    def test_the_limit_is_honoured(self, tmp_path):
        _store(tmp_path / "activity", {f"S{i}": 1e9 - i for i in range(50)})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=12,
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert len(pool["symbols"]) == 12


class TestHeldPositionsComeFirstAndUnconditionally:
    def test_a_held_symbol_leads_the_pool(self, tmp_path):
        _store(tmp_path / "activity", {"AAA": 9e9})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             held_symbols=["RIG"],
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert pool["symbols"][0] == "RIG"
        assert pool["provenance"]["RIG"] == sd.SOURCE_HELD

    def test_a_held_symbol_survives_a_full_pool(self, tmp_path):
        """Obligations are not evicted by better-ranked opportunities."""
        _store(tmp_path / "activity", {f"S{i}": 1e9 - i for i in range(50)})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=5,
                             held_symbols=["RIG"],
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert "RIG" in pool["symbols"]
        assert len(pool["symbols"]) == 5

    def test_held_beats_prior_and_prior_beats_coarse(self, tmp_path):
        _store(tmp_path / "activity", {"COARSE": 9e9})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             held_symbols=["HELD"], prior_symbols=["PRIOR"],
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert pool["symbols"][:3] == ["HELD", "PRIOR", "COARSE"]

    def test_a_symbol_is_not_listed_twice(self, tmp_path):
        _store(tmp_path / "activity", {"RIG": 9e9})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             held_symbols=["RIG"], prior_symbols=["RIG"],
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert pool["symbols"].count("RIG") == 1
        assert pool["provenance"]["RIG"] == sd.SOURCE_HELD


class TestTheStoreIsFoundWhereverItActuallyIs:
    """Two directories claiming to be the activity store is how a 2MB
    populated ranking went unread while the session reported having no
    universe."""

    def test_the_configured_location_wins(self, tmp_path):
        _store(tmp_path / "explicit", {"WANTED": 9e9})
        _store(tmp_path / "legacy", {"STALE": 9e9})
        path, payload = sd.locate_activity_store(
            env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "explicit"),
                 "SCANNER_LEGACY_ANALYTICS_DIR": str(tmp_path / "legacy")})
        assert "WANTED" in payload["symbols"]
        assert "explicit" in str(path)

    def test_the_legacy_tree_is_read_when_shared_state_is_empty(self, tmp_path):
        """A bridge, not a design: a session that refuses to read the
        ranking that exists trades nothing."""
        (tmp_path / "shared" / "logs" / "scanners" / "activity").mkdir(parents=True)
        _store(tmp_path / "legacy" / "activity", {"FOUND": 9e9})
        path, payload = sd.locate_activity_store(
            env={"SCANNER_DATA_ROOT": str(tmp_path / "shared"),
                 "SCANNER_LEGACY_ANALYTICS_DIR": str(tmp_path / "legacy")})
        assert "FOUND" in payload["symbols"]
        assert "legacy" in str(path)

    def test_which_store_answered_is_reported(self, tmp_path):
        """So a pool built on last week's ranking is not mistaken for one
        built on this morning's."""
        _store(tmp_path / "activity", {"AAA": 9e9},
               updated_at="2026-08-28T21:53:17Z")
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=5,
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert "activity" in pool["activity_store"]
        assert pool["activity_updated_at"] == "2026-08-28T21:53:17Z"

    def test_an_unreadable_store_does_not_stop_the_search(self, tmp_path):
        bad = tmp_path / "bad" 
        bad.mkdir(parents=True)
        (bad / "yfinance.json").write_text("{not json")
        _store(tmp_path / "good", {"FOUND": 9e9})
        path, payload = sd.locate_activity_store(
            env={"SCANNER_ACTIVITY_DIR": str(bad),
                 "SCANNER_LEGACY_ANALYTICS_DIR": str(tmp_path)})
        assert payload is None or "FOUND" in payload.get("symbols", {})

    def test_an_empty_store_is_skipped_for_a_populated_one(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir(parents=True)
        (empty / "yfinance.json").write_text(json.dumps({"symbols": {}}))
        _store(tmp_path / "legacy" / "activity", {"FOUND": 9e9})
        _path, payload = sd.locate_activity_store(
            env={"SCANNER_ACTIVITY_DIR": str(empty),
                 "SCANNER_LEGACY_ANALYTICS_DIR": str(tmp_path / "legacy")})
        assert "FOUND" in payload["symbols"]


class TestEligibilityFiltersWithoutEmptyingThePool:
    def test_ineligible_symbols_are_dropped(self, tmp_path):
        _store(tmp_path / "activity", {"GOOD": 9e9, "BAD": 8e9})

        class _Elig:
            def should_skip(self, symbol, **kw):
                return symbol == "BAD"

        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             eligibility=_Elig(),
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert pool["symbols"] == ["GOOD"]

    def test_a_broken_eligibility_view_does_not_empty_the_pool(self, tmp_path):
        """Every symbol still faces the strategy gates; losing the
        eligibility filter must not lose the session."""
        _store(tmp_path / "activity", {"AAA": 9e9})

        class _Broken:
            def should_skip(self, symbol, **kw):
                raise RuntimeError("store gone")

        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             eligibility=_Broken(),
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert pool["symbols"] == ["AAA"]

    def test_everything_ineligible_reports_that_reason(self, tmp_path):
        _store(tmp_path / "activity", {"AAA": 9e9})

        class _All:
            def should_skip(self, symbol, **kw):
                return True

        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             eligibility=_All(),
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "activity")})
        assert pool["symbols"] == []
        assert pool["reason"] == sd.NO_ELIGIBLE_SYMBOLS


class TestItNeverTakesTheSessionDown:
    def test_a_failure_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(
            sd, "coarse_pool",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=5)
        assert pool["reason"] == sd.DISCOVERY_FAILED
        assert pool["symbols"] == []

    def test_the_funnel_line_names_the_reason(self, tmp_path):
        pool = sd.build_pool(session="PREMARKET",
                             operational_trading_day="2026-08-31", limit=5,
                             env={"SCANNER_ACTIVITY_DIR": str(tmp_path / "nope")})
        line = sd.describe(pool)
        assert "reason=NO_ACTIVITY_RANKING" in line
        assert "pool=0" in line

    def test_it_grants_no_symbol_an_easier_path(self):
        """Discovery decides what to LOOK at. It must not touch a gate."""
        source = (REPO_ROOT / "s6_live" / "session_discovery.py").read_text()
        for forbidden in ("volume_expansion", "threshold", "orb_minutes",
                          "submit_buy", "order_gate", "execution_engine"):
            assert forbidden not in source, forbidden


class TestAffordabilityReordersAndNeverRemoves:
    """A pool of mega-caps is a pool of INSUFFICIENT_CASH. Each of those
    names would occupy one of 41 realtime slots to prove the account
    cannot buy a share of it.

    But price is not eligibility: prices move, the orderable amount
    changes the moment a position closes, and a symbol dropped outright
    could never come back."""

    def _store(self, tmp_path, rows):
        import json

        d = tmp_path / "activity"
        d.mkdir(parents=True, exist_ok=True)
        (d / "yfinance.json").write_text(json.dumps({
            "provider": "yfinance", "updated_at": "2026-08-28T21:53:17Z",
            "symbols": {s: {"dollar_volume": dv, "price": p}
                        for s, (dv, p) in rows.items()}}))
        return {"SCANNER_ACTIVITY_DIR": str(d)}

    def test_affordable_names_come_first(self, tmp_path):
        env = self._store(tmp_path, {"SPY": (9e9, 600.0),
                                     "CHEAP": (1e9, 5.0)})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             env=env, orderable_usd=54.44)
        assert pool["symbols"] == ["CHEAP", "SPY"]

    def test_the_unaffordable_ones_are_kept(self, tmp_path):
        """Not removed. The orderable amount changes when a position
        closes, and a dropped symbol could not come back."""
        env = self._store(tmp_path, {"SPY": (9e9, 600.0),
                                     "CHEAP": (1e9, 5.0)})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             env=env, orderable_usd=54.44)
        assert "SPY" in pool["symbols"]

    def test_activity_order_survives_inside_each_partition(self, tmp_path):
        env = self._store(tmp_path, {"BIG1": (9e9, 600.0), "BIG2": (8e9, 700.0),
                                     "SMALL1": (3e9, 5.0), "SMALL2": (2e9, 6.0)})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             env=env, orderable_usd=50.0)
        assert pool["symbols"] == ["SMALL1", "SMALL2", "BIG1", "BIG2"]

    def test_no_orderable_figure_leaves_the_order_alone(self, tmp_path):
        """Guessing affordability from a stale number would reorder the
        pool on something less reliable than the ranking."""
        env = self._store(tmp_path, {"SPY": (9e9, 600.0), "CHEAP": (1e9, 5.0)})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             env=env, orderable_usd=None)
        assert pool["symbols"] == ["SPY", "CHEAP"]

    def test_a_zero_budget_leaves_the_order_alone(self, tmp_path):
        env = self._store(tmp_path, {"SPY": (9e9, 600.0), "CHEAP": (1e9, 5.0)})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             env=env, orderable_usd=0.0)
        assert pool["symbols"] == ["SPY", "CHEAP"]

    def test_a_symbol_with_no_price_is_not_called_affordable(self, tmp_path):
        import json

        d = tmp_path / "activity"
        d.mkdir(parents=True)
        (d / "yfinance.json").write_text(json.dumps({"symbols": {
            "NOPRICE": {"dollar_volume": 9e9},
            "CHEAP": {"dollar_volume": 1e9, "price": 5.0}}}))
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             env={"SCANNER_ACTIVITY_DIR": str(d)},
                             orderable_usd=50.0)
        assert pool["symbols"] == ["CHEAP", "NOPRICE"]

    def test_the_affordable_count_is_reported(self, tmp_path):
        env = self._store(tmp_path, {"SPY": (9e9, 600.0), "CHEAP": (1e9, 5.0)})
        pool = sd.build_pool(session="REGULAR",
                             operational_trading_day="2026-08-31", limit=10,
                             env=env, orderable_usd=54.44)
        assert pool["affordable"] == 1
        assert pool["orderable_usd"] == pytest.approx(54.44)

    def test_it_changes_no_strategy_score(self):
        """Ordering a watch pool is not scoring a signal."""
        source = (REPO_ROOT / "s6_live" / "session_discovery.py").read_text()
        for forbidden in ("score", "signal_score", "rank_score"):
            assert f"def {forbidden}" not in source
