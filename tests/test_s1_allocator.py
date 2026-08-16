"""S1 allocator, cash reservation and whole-share sizing (PHASE 4A §4-§8).

The distinction under test throughout: the account may be fully
deployed, one symbol may not. Rank weights say how the pool is split;
`MAX_SINGLE_POSITION_PCT` says how much any one name may ever be, and it
wins whenever the two disagree.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s1_allocation  # noqa: E402
from s1_live import allocator  # noqa: E402


def candidates(*symbols):
    return [{"symbol": symbol} for symbol in symbols]


def prices(mapping):
    return lambda symbol: mapping.get(symbol)


class TestPolicyConsistency:
    def test_weights_and_reserve_sum_to_one(self):
        assert sum(s1_allocation.RANK_WEIGHTS) + s1_allocation.RESERVE_WEIGHT == pytest.approx(1.0)
        assert s1_allocation.validate() is True

    def test_the_documented_shape(self):
        assert s1_allocation.RANK_WEIGHTS == (0.35, 0.30, 0.25)
        assert s1_allocation.RESERVE_WEIGHT == 0.10
        assert s1_allocation.MAX_SINGLE_POSITION_PCT == 0.35
        assert s1_allocation.LIVE_CASH_LIMIT_PERCENT == 100
        assert s1_allocation.TARGET_POSITION_COUNT == 3
        assert s1_allocation.PLANNED_MAX_POSITION_COUNT == 4

    def test_an_inconsistent_policy_is_refused(self, monkeypatch):
        monkeypatch.setattr(s1_allocation, "RANK_WEIGHTS", (0.5, 0.5))
        with pytest.raises(s1_allocation.AllocationConfigError, match="sum to 1.0"):
            s1_allocation.validate()

    def test_a_weight_above_the_single_cap_is_clamped_not_refused(self, monkeypatch):
        """§6: the cap always takes precedence over the ranking weight.

        Refusing this combination instead would make the cap unreachable
        -- if every weight must already be below it, it can never bind,
        and a guard that cannot fire reads like protection while being
        dead code.
        """
        monkeypatch.setattr(s1_allocation, "RANK_WEIGHTS", (0.60, 0.30))
        monkeypatch.setattr(s1_allocation, "RESERVE_WEIGHT", 0.10)
        monkeypatch.setattr(s1_allocation, "TARGET_POSITION_COUNT", 2)
        assert s1_allocation.validate() is True
        plan = allocator.allocate(candidates("A", "B"), cash_pool_usd=1000.0,
                                  price_lookup=prices({"A": 1.0, "B": 1.0}))
        assert plan.allocations[0].budget_usd == pytest.approx(350.0), "0.60 clamped to 0.35"
        assert plan.allocations[0].capped_by == allocator.CAP_SINGLE_POSITION
        assert plan.allocations[1].budget_usd == pytest.approx(300.0), "0.30 is under the cap"

    def test_allocate_refuses_an_inconsistent_policy(self, monkeypatch):
        monkeypatch.setattr(s1_allocation, "RESERVE_WEIGHT", 0.50)
        with pytest.raises(s1_allocation.AllocationConfigError):
            allocator.allocate(candidates("A"), cash_pool_usd=1000.0,
                               price_lookup=prices({"A": 10.0}))


class TestRankAllocation:
    def test_thirty_five_thirty_twenty_five(self):
        plan = allocator.allocate(
            candidates("A", "B", "C"), cash_pool_usd=1000.0,
            price_lookup=prices({"A": 1.0, "B": 1.0, "C": 1.0}))
        budgets = [item.budget_usd for item in plan.allocations]
        assert budgets == [350.0, 300.0, 250.0]
        assert [item.quantity for item in plan.allocations] == [350, 300, 250]

    def test_reserve_is_never_deployed(self):
        plan = allocator.allocate(
            candidates("A", "B", "C"), cash_pool_usd=1000.0,
            price_lookup=prices({"A": 1.0, "B": 1.0, "C": 1.0}))
        assert plan.reserve_usd == pytest.approx(100.0)
        assert plan.deployable_usd == pytest.approx(900.0)
        assert plan.committed_usd <= plan.deployable_usd

    def test_a_fourth_candidate_gets_nothing(self):
        plan = allocator.allocate(
            candidates("A", "B", "C", "D"), cash_pool_usd=1000.0,
            price_lookup=prices({s: 1.0 for s in "ABCD"}))
        assert plan.allocations[3].status == allocator.SKIP_BEYOND_TARGET_COUNT
        assert plan.allocations[3].quantity == 0

    def test_the_plan_records_the_allocation_version(self):
        plan = allocator.allocate(candidates("A"), cash_pool_usd=100.0,
                                  price_lookup=prices({"A": 1.0}))
        assert plan.allocation_version == s1_allocation.ALLOCATION_VERSION == "s1_alloc_v1"


class TestSinglePositionCap:
    def test_the_cap_outranks_the_rank_weight(self, monkeypatch):
        """With a 50% rank weight, the 35% cap must bind."""
        monkeypatch.setattr(s1_allocation, "RANK_WEIGHTS", (0.50, 0.25, 0.15))
        monkeypatch.setattr(s1_allocation, "RESERVE_WEIGHT", 0.10)
        plan = allocator.allocate(candidates("A"), cash_pool_usd=1000.0,
                                  price_lookup=prices({"A": 1.0}))
        item = plan.allocations[0]
        assert item.budget_usd == pytest.approx(350.0)
        assert item.capped_by == allocator.CAP_SINGLE_POSITION

    def test_the_cap_holds_even_at_a_pathological_weight(self, monkeypatch):
        """A config edited to put everything in one name still cannot."""
        monkeypatch.setattr(s1_allocation, "RANK_WEIGHTS", (0.90,))
        monkeypatch.setattr(s1_allocation, "RESERVE_WEIGHT", 0.10)
        monkeypatch.setattr(s1_allocation, "TARGET_POSITION_COUNT", 1)
        plan = allocator.allocate(candidates("A"), cash_pool_usd=1000.0,
                                  price_lookup=prices({"A": 1.0}))
        assert plan.allocations[0].cost_usd == 350.0
        assert plan.committed_usd < 1000.0 * 0.9

    def test_one_symbol_can_never_be_the_whole_account(self):
        """100% cash usage != single-symbol 100%."""
        plan = allocator.allocate(candidates("ONLY"), cash_pool_usd=1000.0,
                                  price_lookup=prices({"ONLY": 1.0}))
        item = plan.allocations[0]
        assert item.cost_usd <= 1000.0 * s1_allocation.MAX_SINGLE_POSITION_PCT
        assert item.cost_usd == 350.0
        assert plan.remaining_usd > 0, "the rest of the pool stays uncommitted"

    def test_the_cap_is_a_fraction_of_the_pool_not_of_what_is_left(self):
        """A cheap rank 1 must not widen rank 2's ceiling."""
        plan = allocator.allocate(
            candidates("CHEAP", "B"), cash_pool_usd=1000.0,
            price_lookup=prices({"CHEAP": 1.0, "B": 1.0}))
        assert plan.allocations[1].budget_usd == pytest.approx(300.0)


class TestCashReservation:
    def test_the_same_cash_is_not_promised_twice(self):
        """The bug: ask 'how much is available?' three times, get the same
        answer, and commit 35+30+25% of a pool you only have one of."""
        plan = allocator.allocate(
            candidates("A", "B", "C"), cash_pool_usd=1000.0,
            price_lookup=prices({s: 1.0 for s in "ABC"}))
        assert plan.committed_usd == 900.0
        assert plan.committed_usd <= plan.deployable_usd
        assert plan.remaining_usd == pytest.approx(0.0)

    def test_a_later_rank_sees_what_earlier_ranks_took(self):
        """Pool 100, reserve 10 -> deployable 90. Rank 1 takes 35, rank 2
        30; rank 3's 25% budget is 25 but only 25 is left, so it is
        capped by remaining cash, not by its weight."""
        plan = allocator.allocate(
            candidates("A", "B", "C"), cash_pool_usd=100.0,
            price_lookup=prices({s: 1.0 for s in "ABC"}))
        assert [item.quantity for item in plan.allocations] == [35, 30, 25]
        assert plan.allocations[2].capped_by in (
            allocator.CAP_RANK_WEIGHT, allocator.CAP_REMAINING_CASH)
        assert plan.committed_usd == 90.0

    def test_open_order_cash_comes_off_the_top(self):
        plan = allocator.allocate(
            candidates("A"), cash_pool_usd=1000.0, reserved_usd=500.0,
            price_lookup=prices({"A": 1.0}))
        # 500 available, 90% deployable = 450; rank weight on the POOL is
        # 350, and the single cap is 350 -- the smaller binds.
        assert plan.allocations[0].budget_usd == pytest.approx(350.0)
        assert plan.deployable_usd == pytest.approx(450.0)

    def test_reservation_larger_than_the_pool_funds_nothing(self):
        plan = allocator.allocate(
            candidates("A"), cash_pool_usd=100.0, reserved_usd=500.0,
            price_lookup=prices({"A": 1.0}))
        assert plan.funded == []
        assert plan.deployable_usd == 0.0


class TestWholeShares:
    def test_shares_are_floored_never_rounded(self):
        plan = allocator.allocate(candidates("A"), cash_pool_usd=1000.0,
                                  price_lookup=prices({"A": 99.0}))
        # budget 350 / 99 = 3.53 -> 3
        assert plan.allocations[0].quantity == 3
        assert plan.allocations[0].cost_usd == pytest.approx(297.0)

    def test_a_budget_below_one_share_is_a_named_skip(self):
        plan = allocator.allocate(candidates("PRICEY"), cash_pool_usd=1000.0,
                                  price_lookup=prices({"PRICEY": 400.0}))
        item = plan.allocations[0]
        assert item.status == allocator.SKIP_INSUFFICIENT_POSITION_BUDGET
        assert item.quantity == 0
        assert "cannot afford one share" in item.reason

    def test_no_fractional_quantity_is_ever_produced(self):
        plan = allocator.allocate(
            candidates("A", "B", "C"), cash_pool_usd=987.65,
            price_lookup=prices({"A": 33.33, "B": 7.77, "C": 101.01}))
        for item in plan.allocations:
            assert isinstance(item.quantity, int)
            assert item.quantity == int(item.quantity)

    def test_committed_cash_never_exceeds_the_budget(self):
        plan = allocator.allocate(
            candidates("A", "B", "C"), cash_pool_usd=513.0,
            price_lookup=prices({"A": 49.9, "B": 12.3, "C": 7.1}))
        for item in plan.funded:
            assert item.cost_usd <= item.budget_usd + 1e-9


class TestOrderableCeiling:
    def test_the_broker_figure_caps_the_budget(self):
        plan = allocator.allocate(
            candidates("A"), cash_pool_usd=1000.0,
            price_lookup=prices({"A": 1.0}),
            orderable_lookup=lambda symbol, price: 100.0)
        item = plan.allocations[0]
        assert item.budget_usd == 100.0
        assert item.capped_by == allocator.CAP_ORDERABLE
        assert item.quantity == 100

    def test_the_broker_figure_never_raises_the_budget(self):
        plan = allocator.allocate(
            candidates("A"), cash_pool_usd=1000.0,
            price_lookup=prices({"A": 1.0}),
            orderable_lookup=lambda symbol, price: 999_999.0)
        assert plan.allocations[0].budget_usd == 350.0

    @pytest.mark.parametrize("value", [None, float("nan"), -5.0, "abc", True])
    def test_an_unusable_orderable_figure_skips_rather_than_zeroes(self, value):
        """An outage must not read as 'the account has no money'."""
        plan = allocator.allocate(
            candidates("A"), cash_pool_usd=1000.0,
            price_lookup=prices({"A": 1.0}),
            orderable_lookup=lambda symbol, price: value)
        assert plan.allocations[0].status == allocator.SKIP_ORDERABLE_UNKNOWN
        assert plan.allocations[0].quantity == 0

    def test_orderable_is_asked_at_the_price_the_order_would_use(self):
        seen = []
        allocator.allocate(
            candidates("A"), cash_pool_usd=1000.0,
            price_lookup=prices({"A": 42.5}),
            orderable_lookup=lambda symbol, price: seen.append((symbol, price)) or 1000.0)
        assert seen == [("A", 42.5)]


class TestUnusableInputs:
    @pytest.mark.parametrize("pool", [None, -1.0, float("nan"), float("inf"), True, "500"])
    def test_an_unusable_pool_is_a_hard_error(self, pool):
        with pytest.raises(allocator.AllocatorError):
            allocator.allocate(candidates("A"), cash_pool_usd=pool,
                               price_lookup=prices({"A": 1.0}))

    def test_an_unusable_price_skips_that_candidate_only(self):
        plan = allocator.allocate(
            candidates("BAD", "GOOD"), cash_pool_usd=1000.0,
            price_lookup=prices({"BAD": None, "GOOD": 1.0}))
        assert plan.allocations[0].status == allocator.SKIP_PRICE_UNKNOWN
        assert plan.allocations[1].funded is True

    def test_no_candidates_is_an_empty_plan_not_an_error(self):
        plan = allocator.allocate([], cash_pool_usd=1000.0, price_lookup=prices({}))
        assert plan.allocations == []
        assert plan.committed_usd == 0.0


class TestDeterminism:
    def test_the_same_inputs_give_the_same_plan(self):
        args = dict(cash_pool_usd=777.0,
                    price_lookup=prices({"A": 13.0, "B": 29.0, "C": 3.0}))
        first = allocator.allocate(candidates("A", "B", "C"), **args)
        second = allocator.allocate(candidates("A", "B", "C"), **args)
        assert first.as_dict() == second.as_dict()
