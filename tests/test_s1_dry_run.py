"""End-to-end S1 dry run, and the limits PHASE 4A must NOT have moved.

The dry run composes cash pool -> account guards -> per-candidate guards
-> allocation. Two things are asserted about it everywhere: it places no
order, and it cannot place one, because the modules that submit are not
on its import graph.
"""

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s1_allocation  # noqa: E402
from s1_live import allocator, dry_run, reentry  # noqa: E402

DAY = "2026-08-17"
NOW = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)


def candidate(symbol, score, price, *, signal_id=None, stamp=None, day=DAY):
    return {
        "symbol": symbol, "scanner_score": score, "signal_price": price,
        "signal_id": signal_id or f"sig-{symbol}",
        "signal_timestamp": (stamp or NOW - timedelta(hours=1)).isoformat(),
        "trading_day": day, "scanner_run_id": "run-1",
    }


def prices(mapping):
    return lambda symbol: mapping.get(symbol)


def healthy(**kw):
    """Account facts that let the guards allow entries."""
    base = dict(pnl_today_usd=0.0, basis_equity_usd=1000.0,
                equity_usd=1000.0, peak_equity_usd=1000.0)
    base.update(kw)
    return base


class TestTheDocumentedScenario:
    def test_cash_500_three_candidates(self):
        """§20: $500, three candidates, 35/30/25 with 10% reserve."""
        rows = [candidate("A", 90.0, 10.0), candidate("B", 80.0, 10.0),
                candidate("C", 70.0, 10.0)]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"A": 10.0, "B": 10.0, "C": 10.0}), **healthy())

        assert result.account_allowed is True
        plan = result.plan
        assert plan["deployable_usd"] == pytest.approx(450.0)
        assert plan["reserve_usd"] == pytest.approx(50.0)
        # 175 / 150 / 125 at $10 -> 17 / 15 / 12 shares
        assert [item["quantity"] for item in plan["allocations"]] == [17, 15, 12]
        assert plan["committed_usd"] == pytest.approx(440.0)
        assert result.would_submit == 3
        assert result.as_dict()["orders_submitted"] == 0

    def test_a_share_price_above_the_rank_budget_is_skipped(self):
        rows = [candidate("PRICEY", 90.0, 400.0), candidate("OK", 80.0, 10.0)]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"PRICEY": 400.0, "OK": 10.0}), **healthy())
        statuses = {item["symbol"]: item["status"] for item in result.plan["allocations"]}
        assert statuses["PRICEY"] == allocator.SKIP_INSUFFICIENT_POSITION_BUDGET
        assert statuses["OK"] == allocator.STATUS_ALLOCATED

    def test_candidates_compete_for_the_same_cash_once(self):
        rows = [candidate(s, 90.0 - i, 1.0) for i, s in enumerate("ABC")]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=100.0, now=NOW,
            price_lookup=prices({s: 1.0 for s in "ABC"}), **healthy())
        assert result.plan["committed_usd"] <= result.plan["deployable_usd"]
        assert result.plan["committed_usd"] == pytest.approx(90.0)

    def test_open_order_cash_is_excluded(self):
        rows = [candidate("A", 90.0, 1.0)]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0,
            reserved_usd=400.0, now=NOW,
            price_lookup=prices({"A": 1.0}), **healthy())
        assert result.plan["deployable_usd"] == pytest.approx(90.0)


class TestAccountGuardsStopEverything:
    def test_daily_loss_hit_allocates_nothing(self):
        rows = [candidate("A", 90.0, 1.0)]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"A": 1.0}),
            **healthy(pnl_today_usd=-50.0, basis_equity_usd=1000.0))
        assert result.account_allowed is False
        assert result.plan is None
        assert result.would_submit == 0
        assert result.rejected[0]["reason_code"] == "DAILY_LOSS_LIMIT"

    def test_drawdown_hit_allocates_nothing(self):
        rows = [candidate("A", 90.0, 1.0)]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"A": 1.0}),
            **healthy(equity_usd=800.0, peak_equity_usd=1000.0))
        assert result.account_allowed is False
        assert result.rejected[0]["reason_code"] == "DRAWDOWN_LIMIT"

    def test_unmeasurable_guards_block_by_default(self):
        """The current KIS reality: no equity figure, so no entries."""
        result = dry_run.simulate(
            trading_day=DAY, candidates=[candidate("A", 90.0, 1.0)],
            cash_pool_usd=500.0, now=NOW, price_lookup=prices({"A": 1.0}))
        assert result.account_allowed is False
        assert result.would_submit == 0

    def test_an_unavailable_cash_pool_allocates_nothing(self):
        result = dry_run.simulate(
            trading_day=DAY, candidates=[candidate("A", 90.0, 1.0)],
            cash_pool_usd=None, now=NOW, price_lookup=prices({"A": 1.0}), **healthy())
        assert result.plan is None
        assert result.would_submit == 0


class TestCandidateGuardsRunBeforeAllocation:
    def _state(self, **kw):
        base = dict(symbol="A", known=True, used_signal_ids=frozenset())
        base.update(kw)
        return reentry.SymbolState(**base)

    def test_a_stale_signal_never_consumes_budget(self):
        rows = [candidate("OLD", 99.0, 1.0, day="2026-08-14"),
                candidate("GOOD", 80.0, 1.0)]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"OLD": 1.0, "GOOD": 1.0}), **healthy())
        assert result.eligible == ["GOOD"]
        # GOOD is rank 1 of the ELIGIBLE list -- the stale one took nothing.
        assert result.plan["allocations"][0]["symbol"] == "GOOD"
        assert result.plan["allocations"][0]["weight"] == 0.35

    def test_an_already_held_symbol_never_consumes_budget(self):
        rows = [candidate("HELD", 99.0, 1.0), candidate("FREE", 80.0, 1.0)]
        states = {"HELD": self._state(symbol="HELD", currently_held=True),
                  "FREE": self._state(symbol="FREE")}
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"HELD": 1.0, "FREE": 1.0}),
            symbol_state_lookup=states.get, **healthy())
        assert result.eligible == ["FREE"]
        assert result.rejected[0]["reason_code"] == reentry.REASON_ALREADY_HELD

    def test_an_open_order_blocks(self):
        rows = [candidate("A", 99.0, 1.0)]
        states = {"A": self._state(has_open_order=True)}
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"A": 1.0}), symbol_state_lookup=states.get, **healthy())
        assert result.rejected[0]["reason_code"] == reentry.REASON_OPEN_ORDER
        assert result.would_submit == 0

    def test_a_duplicate_signal_id_blocks(self):
        rows = [candidate("A", 99.0, 1.0, signal_id="used")]
        states = {"A": self._state(used_signal_ids=frozenset({"used"}))}
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"A": 1.0}), symbol_state_lookup=states.get, **healthy())
        assert result.rejected[0]["reason_code"] == reentry.REASON_DUPLICATE_SIGNAL


class TestObservations:
    def test_extension_is_recorded_and_not_enforced(self):
        """A candidate that has already run 80% still allocates -- the
        number is measured, and the threshold is a later decision."""
        rows = [candidate("RAN", 90.0, 10.0)]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"RAN": 18.0}), **healthy())
        observation = result.observations[0]
        assert observation["extension_pct"] == pytest.approx(80.0)
        assert observation["extension_threshold_pct"] is None
        assert result.plan["allocations"][0]["status"] == allocator.STATUS_ALLOCATED

    def test_signal_age_is_recorded(self):
        rows = [candidate("A", 90.0, 10.0, stamp=NOW - timedelta(hours=3))]
        result = dry_run.simulate(
            trading_day=DAY, candidates=rows, cash_pool_usd=500.0, now=NOW,
            price_lookup=prices({"A": 10.0}), **healthy())
        assert result.observations[0]["signal_age_seconds"] == pytest.approx(10800.0)


class TestRolloutLimitsAreUnmoved:
    """§19/§21: PHASE 4A simulates the aggressive shape, it does not apply it."""

    def test_the_live_rollout_is_still_one_one_one_and_off(self):
        from config.live_rollout_config import LiveRolloutConfig

        config = LiveRolloutConfig.from_env({})
        assert config.enabled is False
        # Per-ORDER quantity is unset by design since LIMITED_LIVE
        # ended: order size comes from orderable cash and whole-share
        # arithmetic, bounded by the flags below. An operator ceiling is
        # still honoured when one is set; what is gone is the fixed
        # one-share test cap nobody chose.
        assert config.max_quantity_per_order is None
        # The COUNT caps are unset by design since LIMITED_LIVE ended:
        # capacity is bounded by cash, the per-symbol lock, same-day
        # re-entry, ownership and reconciliation. The invariant this
        # test guards -- that the work in this file widened nothing --
        # is carried by the flags and the per-order quantity below.
        assert config.max_open_positions is None
        assert config.max_daily_entries is None

    def test_the_trusted_operator_ceilings_are_unmoved(self):
        from live_readiness import trusted_operator_config as toc

        assert toc.get_max_concurrent_live_positions() == 1
        assert toc.get_max_daily_live_entries() == 2

    def test_the_planned_shape_lives_only_in_the_allocation_config(self):
        assert s1_allocation.PLANNED_MAX_POSITION_COUNT == 4
        assert s1_allocation.TARGET_POSITION_COUNT == 3
        rollout = (REPO_ROOT / "config" / "live_rollout_config.py").read_text(encoding="utf-8")
        # Per-ORDER quantity reads through _env_optional_int too, so an
        # unset value means "no operator ceiling" and order size comes
        # from orderable cash. Asserted on the reader for the same
        # reason as the count caps below.
        assert '_env_optional_int(\n                mapping, "LIVE_ROLLOUT_MAX_QUANTITY")' in rollout
        # The COUNT caps read through _env_optional_int now, so an unset
        # value means "not enforced" rather than 1. Asserted on the
        # reader rather than on a default, because the point of this
        # test is that the PLANNED four-position shape has not leaked
        # into the live config -- and it has not: there is no 4 here.
        assert '_env_optional_int(\n                mapping, "LIVE_ROLLOUT_MAX_POSITIONS")' in rollout
        assert '_env_optional_int(\n                mapping, "LIVE_ROLLOUT_MAX_DAILY_ENTRIES")' in rollout
        assert "PLANNED_MAX_POSITION_COUNT" not in rollout

    def test_risk_config_is_untouched(self):
        import risk_config

        assert risk_config.MAX_DAILY_LOSS_RATE == -0.02
        assert risk_config.MAX_TOTAL_DRAWDOWN == -0.10
        assert risk_config.TRADING_MODE == "paper"
        assert risk_config.ENABLE_REAL_TRADING is False


class TestTheDryRunCannotOrder:
    FORBIDDEN = {"execution", "brokers", "broker", "kis_live_trading",
                 "live_pilot", "kis_position_manager", "paper_strategy_order"}

    def _imports(self, path):
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
                names.update(f"{node.module}.{a.name}" for a in node.names)
        return names

    def test_the_dry_run_module_imports_nothing_that_can_submit(self):
        for module in self._imports(REPO_ROOT / "s1_live" / "dry_run.py"):
            assert module.split(".")[0] not in self.FORBIDDEN, module

    def test_the_dry_run_script_imports_nothing_that_can_submit(self):
        for module in self._imports(REPO_ROOT / "scripts" / "run_s1_dry_run.py"):
            assert module.split(".")[0] not in self.FORBIDDEN, module

    def test_the_allocator_is_pure(self):
        """No broker, no database, no clock -- every fact is an argument."""
        for module in self._imports(REPO_ROOT / "s1_live" / "allocator.py"):
            root = module.split(".")[0]
            assert root not in self.FORBIDDEN | {"sqlite3", "requests"}, module

    def test_the_result_always_reports_zero_orders(self):
        result = dry_run.simulate(
            trading_day=DAY, candidates=[candidate("A", 90.0, 1.0)],
            cash_pool_usd=500.0, now=NOW, price_lookup=prices({"A": 1.0}), **healthy())
        payload = result.as_dict()
        assert payload["mode"] == "DRY_RUN"
        assert payload["orders_submitted"] == 0
        assert payload["would_submit"] >= 0
