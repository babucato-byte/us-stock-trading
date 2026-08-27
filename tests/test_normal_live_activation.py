"""The last LIMITED_LIVE limits, and what replaces them.

Why a system with twelve READY candidates traded nothing
--------------------------------------------------------
On 2026-08-27 the REGULAR funnel read SCANNED 18, READY_TO_BUY 12,
EXECUTABLE 0, BUY_SUBMITTED 0. PSKY was READY at 10.905, the broker's own
orderable amount was 20.96, one whole share was affordable, and no order
existed. Three test-era limits were still in force:

    LIVE_ROLLOUT_ALLOWED_SYMBOLS=DT   every other candidate filtered out
                                      before any gate ran
    LIVE_ROLLOUT_MAX_QUANTITY=1       min(affordable, 1) made every order
                                      one share whatever the cash allowed
    exactly-one allow-list check      the readiness checker REQUIRED the
                                      pinned single symbol

Set means set, unset means unset
--------------------------------
None of these is deleted. An operator who sets an allow-list still gets
one; a cap that is set is still honoured. What changed is the default: an
absent restriction now means "no operator restriction", not "nothing is
permitted", which is what an empty allow-list used to mean.

What still decides a BUY is unchanged and considerable: strategy
conditions, freshness, cash, ownership, reconciliation, duplicate
protection, same-day re-entry, instrument eligibility, a verified route.

Nothing here places an order.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.live_rollout_config import LiveRolloutConfig  # noqa: E402
from domain.cash_sizing import whole_shares_affordable  # noqa: E402
from execution import order_gate  # noqa: E402
from execution.order_gate import OrderGateBlockedError, evaluate_buy_gate  # noqa: E402

from tests import test_order_gate as fixtures  # noqa: E402


def _ctx(**overrides):
    return fixtures._buy_ctx(**overrides)


def _blocked(ctx):
    with pytest.raises(OrderGateBlockedError) as excinfo:
        evaluate_buy_gate(ctx)
    return excinfo.value


class TestTheAllowListNoLongerPreApproves:
    def test_item5_an_unset_list_accepts_a_normal_ready_symbol(self):
        """The exact failure: PSKY was READY and filtered out because the
        list still said DT."""
        ctx = _ctx(order_intent=fixtures._order_intent(symbol="PSKY"),
                   signal=fixtures._signal(symbol="PSKY"),
                   reconciliation=fixtures._snapshot(symbol="PSKY"),
                   allowed_symbols=None)
        assert evaluate_buy_gate(ctx) is True

    def test_a_list_an_operator_sets_still_restricts(self):
        ctx = _ctx(order_intent=fixtures._order_intent(symbol="PSKY"),
                   signal=fixtures._signal(symbol="PSKY"),
                   reconciliation=fixtures._snapshot(symbol="PSKY"),
                   allowed_symbols=frozenset({"DT"}))
        assert _blocked(ctx).code == "SYMBOL"

    def test_a_listed_symbol_passes(self):
        ctx = _ctx(allowed_symbols=frozenset({"AAPL"}))
        assert evaluate_buy_gate(ctx) is True

    def test_the_default_config_sets_no_restriction(self):
        assert LiveRolloutConfig.from_env({}).allowed_symbols is None

    def test_an_empty_list_is_not_the_same_as_an_absent_one(self):
        """The distinction the whole change rests on. Absent means "no
        operator restriction"; empty means "deny everything", and a
        truncated env file, a blanked value and a failed load all look
        like empty. If those two collapsed, a missing env file would read
        as "every symbol is permitted"."""
        assert LiveRolloutConfig.from_env(
            {"LIVE_ROLLOUT_ALLOWED_SYMBOLS": ""}).allowed_symbols == frozenset()

        ctx = _ctx(allowed_symbols=frozenset())
        assert _blocked(ctx).code == "SYMBOL"

    def test_a_gate_context_that_forgot_the_field_cannot_be_built(self):
        """`allowed_symbols` has no default, so the unrestricted posture
        has to be stated rather than arrived at by omission."""
        import dataclasses

        field = {f.name: f for f in dataclasses.fields(
            order_gate.BuyGateContext)}["allowed_symbols"]
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


class TestVariableSizing:
    """§6 -- the quantity comes from the broker's own orderable amount."""

    def test_item2_one_affordable_share_is_executable(self):
        """PSKY: orderable 20.96, buffered 11.014 -> 1 share."""
        assert whole_shares_affordable(20.96, 11.014) == 1

    def test_item3_an_unaffordable_candidate_yields_zero(self):
        """GIS: orderable 20.96, buffered 40.587 -> 0. That candidate is
        skipped; it is not a reason to stop trading."""
        assert whole_shares_affordable(20.96, 40.5869) == 0

    def test_item6_a_larger_balance_sizes_up(self):
        """The cap used to flatten this to 1 regardless."""
        assert whole_shares_affordable(1000.0, 100.0) == 10

    def test_the_cap_is_only_applied_when_set(self):
        uncapped = LiveRolloutConfig.from_env({})
        capped = LiveRolloutConfig.from_env({"LIVE_ROLLOUT_MAX_QUANTITY": "2"})
        assert uncapped.max_quantity_per_order is None
        assert capped.max_quantity_per_order == 2

    def test_the_entry_path_applies_the_cap_conditionally(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")
        assert "if rollout.max_quantity_per_order is not None" in source
        assert "else balance_qty" in source

    def test_whole_shares_only(self):
        """§7 -- the fixed 1 goes, fractional stays off."""
        assert LiveRolloutConfig.from_env({}).allow_fractional is False
        for cash, price in ((20.96, 11.014), (1000.0, 100.0), (55.0, 10.0)):
            qty = whole_shares_affordable(cash, price)
            assert isinstance(qty, int) and qty >= 0


class TestTheSafetyGatesAreUntouched:
    """§10 and §14-15 -- removing a test cap removed no protection."""

    def test_a_held_symbol_still_blocks_a_second_buy(self):
        from execution import entry_limits

        from tests.test_multi_position_and_reentry import _limits

        ctx = _ctx(allowed_symbols=None,
                   entry_limits=_limits(strategy_symbols={"S1": frozenset({"AAPL"})}))
        assert _blocked(ctx).code == entry_limits.SYMBOL_ALREADY_HELD

    def test_same_day_reentry_still_blocks(self):
        from execution import reentry_policy

        from tests.test_multi_position_and_reentry import _limits

        ctx = _ctx(allowed_symbols=None, entry_limits=_limits(
            same_day_exits={"S1": {"AAPL": {"exit_reason": "RANGE_REENTRY"}}}))
        assert _blocked(ctx).code == reentry_policy.SAME_DAY_REENTRY_BLOCK

    def test_an_open_order_still_blocks(self):
        ctx = _ctx(allowed_symbols=None, has_open_order_for_symbol=True)
        assert _blocked(ctx).code == "OPEN_ORDER"

    def test_a_dirty_reconciliation_still_blocks(self):
        ctx = _ctx(allowed_symbols=None,
                   reconciliation=fixtures._snapshot(positions_match=False))
        assert _blocked(ctx).code == "RECONCILIATION"

    def test_entry_disabled_still_blocks(self):
        ctx = _ctx(allowed_symbols=None, entry_disabled=True)
        assert _blocked(ctx).code == "ENTRY_DISABLED"


class TestTheReadinessCheckerFollowed:
    def test_it_no_longer_requires_exactly_one_symbol(self):
        text = (REPO_ROOT / "scripts" / "final_pre_live_check.sh").read_text(
            encoding="utf-8")
        assert "LIVE_ALLOWLIST_NOT_EXACTLY_ONE" not in text

    def test_it_no_longer_pins_quantity_to_one(self):
        text = (REPO_ROOT / "scripts" / "final_pre_live_check.sh").read_text(
            encoding="utf-8")
        assert "check_limit LIVE_ROLLOUT_MAX_QUANTITY 1" not in text

    def test_a_malformed_quantity_cap_is_still_refused(self):
        """Retired as a requirement, not as a sanity check."""
        text = (REPO_ROOT / "scripts" / "final_pre_live_check.sh").read_text(
            encoding="utf-8")
        assert "LIVE_ROLLOUT_MAX_QUANTITY" in text
        assert "_INVALID" in text


class TestTheEntryTickIsScheduledAndSafe:
    """§1-2 -- the missing scheduler, and not sending twice."""

    WRAPPER = REPO_ROOT / "deploy" / "cron" / "s6_buy_entry.sh"

    def test_the_wrapper_exists(self):
        assert self.WRAPPER.exists()

    def test_it_runs_the_live_entry_for_s6(self):
        text = self.WRAPPER.read_text(encoding="utf-8")
        assert "run_live_buy_entry.py" in text
        assert "--strategy s6" in text

    def test_it_uses_the_verified_release(self):
        """An entry cycle is the one place where running unverified code
        spends real money."""
        text = self.WRAPPER.read_text(encoding="utf-8")
        assert "resolve_release_root" in text
        code = "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith("#"))
        assert "/home/ubuntu/trading" not in code

    def test_overlap_is_skipped_not_queued(self):
        """A queued second cycle would evaluate the same candidate
        against the same READY snapshot while the first was submitting."""
        text = self.WRAPPER.read_text(encoding="utf-8")
        assert "flock -n" in text

    def test_it_shares_the_runtime_lock(self):
        """The runtime tick can open positions from fills; an entry
        deciding 'this symbol is flat' while that lands is the race."""
        text = self.WRAPPER.read_text(encoding="utf-8")
        assert "s6_exec.lock" in text

    def test_it_reads_the_shared_candidate_store(self):
        text = self.WRAPPER.read_text(encoding="utf-8")
        assert "resolve_shared_candidate_dir" in text

    def test_the_two_lock_outcomes_are_distinguishable_in_the_log(self):
        """§7. Without `-E`, contention and a crashed runner both arrive
        as exit 1, so a minute where the entry never ran reads exactly
        like a minute where it ran and failed -- and "no BUY today" then
        has two very different explanations and no way to tell them
        apart."""
        text = self.WRAPPER.read_text(encoding="utf-8")
        assert "flock -n -E 99" in text
        assert "OVERLAP_SKIPPED" in text
        assert "LOCK_ACQUIRED" in text

    def test_a_skipped_overlap_is_not_reported_as_a_failure(self):
        """cron mails on non-zero. An overlap is the design working, so
        it exits 0 -- while a genuine runner failure keeps its own
        status rather than being flattened into success."""
        text = self.WRAPPER.read_text(encoding="utf-8")
        assert 'if [ "$STATUS" -eq 99 ]' in text
        assert 'exit "$STATUS"' in text
