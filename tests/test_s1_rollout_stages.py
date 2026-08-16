"""Rollout stages, readiness and the aggressive-profile simulation (PHASE 4D).

Nothing here enables anything. The two properties under test are that
the PLANNED shape behaves as intended when simulated, and that the
ACTUAL configuration is still 1/1/1 with the rollout off.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s1_allocation, s1_rollout_stages as stages  # noqa: E402
from s1_live import allocator, dry_run, readiness, risk_state  # noqa: E402

NOW = datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc)


def prices(mapping):
    return lambda symbol: mapping.get(symbol)


def rows(*symbols):
    return [{"symbol": s} for s in symbols]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    from state_store import db

    connection = db.open_db()
    yield connection
    connection.close()


# ---------------------------------------------------------------- profiles

class TestProfiles:
    def test_the_aggressive_profile_is_the_documented_shape(self):
        profile = stages.profile_for(stages.STAGE_AGGRESSIVE)
        assert profile["profile"] == "S1_AGGRESSIVE_V1"
        assert profile["live_cash_limit_percent"] == 100
        assert profile["target_positions"] == 3
        assert profile["hard_max_positions"] == 4
        assert profile["max_new_entries_per_day"] == 5
        assert profile["rank_weights"] == (0.35, 0.30, 0.25)
        assert profile["reserve_weight"] == 0.10
        assert profile["max_single_position_pct"] == 0.35

    def test_first_live_validation_is_the_narrowest_shape(self):
        profile = stages.profile_for(stages.STAGE_FIRST_LIVE)
        assert profile["profile"] == "S1_FIRST_LIVE_VALIDATION"
        assert profile["target_positions"] == 1
        assert profile["hard_max_positions"] == 1
        assert profile["max_new_entries_per_day"] == 1
        assert profile["max_quantity_per_order"] == 1

    def test_first_live_does_not_deploy_what_it_does_not_rank(self):
        """A one-position stage that quietly deployed the rest would be a
        two-position stage wearing the wrong name."""
        profile = stages.profile_for(stages.STAGE_FIRST_LIVE)
        assert sum(profile["rank_weights"]) + profile["reserve_weight"] == pytest.approx(1.0)
        assert profile["reserve_weight"] == 0.65

    @pytest.mark.parametrize("stage", stages.STAGE_ORDER)
    def test_every_profile_is_internally_consistent(self, stage):
        assert stages.validate_profile(stages.profile_for(stage)) is True

    @pytest.mark.parametrize("stage", stages.STAGE_ORDER)
    def test_no_profile_exceeds_the_single_position_cap(self, stage):
        profile = stages.profile_for(stage)
        for weight in profile["rank_weights"]:
            assert weight <= profile["max_single_position_pct"] + 1e-9

    def test_an_unknown_stage_raises(self):
        for call in (stages.profile_for, stages.requirements_for, stages.stage_index):
            with pytest.raises(stages.RolloutStageError):
                call("STAGE_99_MOON")


class TestStageProgression:
    def test_the_order_is_observe_first_aggressive_last(self):
        assert stages.STAGE_ORDER[0] == stages.STAGE_OBSERVE
        assert stages.STAGE_ORDER[-1] == stages.STAGE_AGGRESSIVE

    def test_promotion_moves_one_step_and_never_skips(self):
        assert stages.next_stage(stages.STAGE_OBSERVE) == stages.STAGE_FIRST_LIVE
        assert stages.next_stage(stages.STAGE_FIRST_LIVE) == stages.STAGE_LIMITED_ROTATION
        assert stages.next_stage(stages.STAGE_AGGRESSIVE) is None

    def test_each_stage_requires_everything_the_previous_one_did(self):
        for lower, upper in zip(stages.STAGE_ORDER, stages.STAGE_ORDER[1:]):
            assert set(stages.requirements_for(lower)) <= set(stages.requirements_for(upper))

    def test_stage_1_requires_an_exit_policy(self):
        """§13: no exit policy means the rollout cannot be switched on."""
        assert stages.REQ_EXIT_POLICY in stages.requirements_for(stages.STAGE_FIRST_LIVE)

    def test_stage_1_requires_a_verified_minimum_order(self):
        assert stages.REQ_MINIMUM_ORDER in stages.requirements_for(stages.STAGE_FIRST_LIVE)

    def test_position_valuation_is_a_stage_2_requirement(self):
        assert stages.REQ_POSITION_VALUATION not in stages.requirements_for(
            stages.STAGE_FIRST_LIVE)
        assert stages.REQ_POSITION_VALUATION in stages.requirements_for(
            stages.STAGE_LIMITED_ROTATION)

    def test_reserved_order_cash_is_a_stage_3_requirement(self):
        assert stages.REQ_RESERVED_ORDER_CASH in stages.requirements_for(
            stages.STAGE_AGGRESSIVE)


# --------------------------------------------------------------- readiness

def full_ready(conn, **overrides):
    kwargs = dict(
        conn=conn, risk_state=_state(), equity_snapshot=_equity(),
        candidate_source_ok=True, candidate_decision_enabled=False,
        kill_switch_healthy=True, reconciliation_healthy=True,
        minimum_order_verified=True, exit_policy_defined=True,
        fees_reported=True, now=NOW)
    kwargs.update(overrides)
    return readiness.build_matrix(**kwargs)


def _state():
    return risk_state.RiskState(
        trading_day="2026-08-17", start_equity=1000.0, current_equity=1000.0,
        peak_equity=1000.0, daily_loss_status="ALLOW", drawdown_status="ALLOW")


class _Equity:
    available = True
    cash_usd = 500.0
    position_value_usd = 500.0
    detail = ""


def _equity():
    return _Equity()


class TestReadinessMatrix:
    def test_every_declared_requirement_has_an_evaluator(self, conn):
        """A requirement listed but never checked is a gate that reads
        like protection and enforces nothing."""
        matrix = full_ready(conn)
        evaluated = set(matrix.by_key())
        declared = set()
        for stage in stages.STAGE_ORDER:
            declared |= set(stages.requirements_for(stage))
        assert declared <= evaluated, declared - evaluated

    def test_nothing_is_ready_by_default(self, conn):
        matrix = readiness.build_matrix(conn=conn, now=NOW)
        assert matrix.highest_stage() == stages.STAGE_OBSERVE
        assert matrix.unmet_for(stages.STAGE_FIRST_LIVE)

    def test_a_missing_exit_policy_blocks_stage_1(self, conn):
        matrix = full_ready(conn, exit_policy_defined=False)
        unmet = {check.key for check in matrix.unmet_for(stages.STAGE_FIRST_LIVE)}
        assert stages.REQ_EXIT_POLICY in unmet
        assert matrix.highest_stage() == stages.STAGE_OBSERVE

    def test_a_missing_minimum_order_blocks_stage_1(self, conn):
        matrix = full_ready(conn, minimum_order_verified=False)
        assert matrix.highest_stage() == stages.STAGE_OBSERVE

    def test_an_enabled_candidate_decision_blocks(self, conn):
        matrix = full_ready(conn, candidate_decision_enabled=True)
        assert matrix.highest_stage() == stages.STAGE_OBSERVE

    def test_a_blocked_daily_loss_blocks(self, conn):
        state = _state()
        state.daily_loss_status = "BLOCK"
        assert full_ready(conn, risk_state=state).highest_stage() == stages.STAGE_OBSERVE

    def test_everything_ready_reaches_stage_1_but_not_stage_2(self, conn):
        """position_valuation is UNVERIFIED until a real position exists."""
        matrix = full_ready(conn)
        assert matrix.highest_stage() == stages.STAGE_FIRST_LIVE
        unmet = {check.key for check in matrix.unmet_for(stages.STAGE_LIMITED_ROTATION)}
        assert unmet == {stages.REQ_POSITION_VALUATION}

    def test_verified_valuation_reaches_stage_2_but_not_stage_3(self, conn):
        readiness.record_verification(conn, stages.REQ_POSITION_VALUATION,
                                      readiness.READY, now=NOW)
        matrix = full_ready(conn)
        assert matrix.highest_stage() == stages.STAGE_LIMITED_ROTATION
        unmet = {check.key for check in matrix.unmet_for(stages.STAGE_AGGRESSIVE)}
        assert unmet == {stages.REQ_RESERVED_ORDER_CASH}

    def test_both_verified_reaches_stage_3(self, conn):
        for key in readiness.VERIFICATION_KEYS:
            readiness.record_verification(conn, key, readiness.READY, now=NOW)
        assert full_ready(conn).highest_stage() == stages.STAGE_AGGRESSIVE

    def test_a_later_stage_cannot_be_reached_past_a_blocked_earlier_one(self, conn):
        for key in readiness.VERIFICATION_KEYS:
            readiness.record_verification(conn, key, readiness.READY, now=NOW)
        matrix = full_ready(conn, exit_policy_defined=False)
        assert matrix.highest_stage() == stages.STAGE_OBSERVE

    def test_the_matrix_always_reports_the_rollout_as_disabled(self, conn):
        assert full_ready(conn).as_dict()["live_rollout"] == readiness.DISABLED

    def test_it_renders(self, conn):
        text = readiness.format_matrix(full_ready(conn))
        assert "S1 LIVE READINESS" in text
        assert "highest stage permitted" in text


class TestVerificationState:
    def test_both_default_to_unverified(self, conn):
        for key in readiness.VERIFICATION_KEYS:
            assert readiness.verification_status(conn, key) == readiness.UNVERIFIED

    def test_a_mismatch_latches(self, conn):
        """One clean read does not disprove a valuation disagreement."""
        key = stages.REQ_POSITION_VALUATION
        readiness.record_verification(conn, key, readiness.MISMATCH, now=NOW)
        readiness.record_verification(conn, key, readiness.READY, now=NOW)
        assert readiness.verification_status(conn, key) == readiness.MISMATCH

    def test_a_mismatch_blocks_promotion(self, conn):
        readiness.record_verification(conn, stages.REQ_POSITION_VALUATION,
                                      readiness.MISMATCH, now=NOW)
        matrix = full_ready(conn)
        assert matrix.highest_stage() == stages.STAGE_FIRST_LIVE


class TestPositionValuationComparison:
    def test_an_exact_match_is_ready(self):
        assert readiness.compare_position_valuation(1000.0, 1000.0)["status"] == readiness.READY

    def test_a_rounding_difference_is_tolerated(self):
        assert readiness.compare_position_valuation(
            1000.004, 1000.0)["status"] == readiness.READY

    def test_a_meaningful_difference_is_a_mismatch(self):
        result = readiness.compare_position_valuation(1000.0, 990.0)
        assert result["status"] == readiness.MISMATCH
        assert result["difference_usd"] == pytest.approx(10.0)

    def test_the_tolerance_is_not_generous(self):
        """A cent, or a part per million -- not a percent."""
        assert readiness.VALUATION_ABS_TOLERANCE_USD == 0.01
        assert readiness.compare_position_valuation(
            1000.0, 1000.5)["status"] == readiness.MISMATCH

    @pytest.mark.parametrize("internal,broker", [
        (None, 100.0), (100.0, None), (True, 100.0), ("100", 100.0)])
    def test_an_unusable_input_stays_unverified(self, internal, broker):
        assert readiness.compare_position_valuation(
            internal, broker)["status"] == readiness.UNVERIFIED


# ------------------------------------------------------------ simulation

def simulate_profile(profile, *, cash, price, candidates, monkeypatch):
    """Apply a planned profile to the live allocator, for simulation only."""
    monkeypatch.setattr(s1_allocation, "RANK_WEIGHTS", tuple(profile["rank_weights"]))
    monkeypatch.setattr(s1_allocation, "RESERVE_WEIGHT", profile["reserve_weight"])
    monkeypatch.setattr(s1_allocation, "MAX_SINGLE_POSITION_PCT",
                        profile["max_single_position_pct"])
    monkeypatch.setattr(s1_allocation, "TARGET_POSITION_COUNT",
                        max(1, profile["target_positions"]))
    symbols = [f"S{i}" for i in range(candidates)]
    return allocator.allocate(rows(*symbols), cash_pool_usd=cash,
                              price_lookup=prices({s: price for s in symbols}))


class TestAggressiveSimulationSweep:
    """§8: several cash sizes against several candidate prices."""

    CASH = (100.0, 250.0, 500.0, 1000.0, 2000.0)
    PRICE = (5.0, 20.0, 50.0, 100.0, 300.0)

    @pytest.mark.parametrize("cash", CASH)
    @pytest.mark.parametrize("price", PRICE)
    def test_no_single_position_ever_exceeds_thirty_five_percent(self, cash, price, monkeypatch):
        plan = simulate_profile(stages.profile_for(stages.STAGE_AGGRESSIVE),
                                cash=cash, price=price, candidates=3,
                                monkeypatch=monkeypatch)
        for item in plan.funded:
            assert item.cost_usd <= cash * 0.35 + 1e-9, (cash, price, item)

    @pytest.mark.parametrize("cash", CASH)
    @pytest.mark.parametrize("price", PRICE)
    def test_committed_never_exceeds_the_deployable_pool(self, cash, price, monkeypatch):
        plan = simulate_profile(stages.profile_for(stages.STAGE_AGGRESSIVE),
                                cash=cash, price=price, candidates=3,
                                monkeypatch=monkeypatch)
        assert plan.committed_usd <= plan.deployable_usd + 1e-9
        assert plan.committed_usd <= cash * 0.90 + 1e-9

    @pytest.mark.parametrize("cash", CASH)
    @pytest.mark.parametrize("price", PRICE)
    def test_every_quantity_is_a_whole_number(self, cash, price, monkeypatch):
        plan = simulate_profile(stages.profile_for(stages.STAGE_AGGRESSIVE),
                                cash=cash, price=price, candidates=3,
                                monkeypatch=monkeypatch)
        for item in plan.allocations:
            assert isinstance(item.quantity, int)

    @pytest.mark.parametrize("cash", CASH)
    def test_at_least_ten_percent_is_always_left(self, cash, monkeypatch):
        plan = simulate_profile(stages.profile_for(stages.STAGE_AGGRESSIVE),
                                cash=cash, price=1.0, candidates=3,
                                monkeypatch=monkeypatch)
        assert plan.reserve_usd == pytest.approx(cash * 0.10)


class TestCandidateShortage:
    """§10: fewer candidates must not widen the ones that exist."""

    @pytest.mark.parametrize("count", [1, 2, 3])
    def test_the_cap_holds_however_few_candidates_there_are(self, count, monkeypatch):
        plan = simulate_profile(stages.profile_for(stages.STAGE_AGGRESSIVE),
                                cash=1000.0, price=1.0, candidates=count,
                                monkeypatch=monkeypatch)
        for item in plan.funded:
            assert item.cost_usd <= 350.0 + 1e-9

    def test_one_candidate_gets_thirty_five_percent_not_a_hundred(self, monkeypatch):
        plan = simulate_profile(stages.profile_for(stages.STAGE_AGGRESSIVE),
                                cash=1000.0, price=1.0, candidates=1,
                                monkeypatch=monkeypatch)
        assert plan.committed_usd == 350.0
        assert plan.remaining_usd == pytest.approx(550.0)

    def test_two_candidates_keep_their_own_weights(self, monkeypatch):
        plan = simulate_profile(stages.profile_for(stages.STAGE_AGGRESSIVE),
                                cash=1000.0, price=1.0, candidates=2,
                                monkeypatch=monkeypatch)
        assert [item.cost_usd for item in plan.funded] == [350.0, 300.0]


class TestUnusedAllocation:
    """§9: an unfunded rank returns its budget to reserve, not to a peer."""

    def test_an_expensive_rank_one_does_not_enlarge_rank_two(self):
        plan = allocator.allocate(
            rows("PRICEY", "B", "C"), cash_pool_usd=100.0,
            price_lookup=prices({"PRICEY": 400.0, "B": 1.0, "C": 1.0}))
        assert plan.allocations[0].status == allocator.SKIP_INSUFFICIENT_POSITION_BUDGET
        assert plan.allocations[1].budget_usd == pytest.approx(30.0), "still pool*0.30"
        assert plan.allocations[2].budget_usd == pytest.approx(25.0)

    def test_the_unused_budget_stays_uncommitted(self):
        plan = allocator.allocate(
            rows("PRICEY", "B", "C"), cash_pool_usd=100.0,
            price_lookup=prices({"PRICEY": 400.0, "B": 1.0, "C": 1.0}))
        assert plan.committed_usd == 55.0
        assert plan.remaining_usd == pytest.approx(35.0)

    def test_every_candidate_too_expensive_commits_nothing(self):
        plan = allocator.allocate(
            rows("A", "B", "C"), cash_pool_usd=100.0,
            price_lookup=prices({s: 400.0 for s in "ABC"}))
        assert plan.funded == []
        assert plan.committed_usd == 0.0


class TestRiskStateGatesAllocationEntirely:
    """§11: guards run BEFORE allocation, never part-way through."""

    def _candidates(self):
        return [{"symbol": "A", "signal_price": 1.0, "signal_id": "s1",
                 "signal_timestamp": (NOW).isoformat(), "trading_day": "2026-08-17"}]

    def test_a_blocked_daily_loss_allocates_nothing(self):
        state = _state()
        state.daily_loss_status = "BLOCK"
        result = dry_run.simulate(
            trading_day="2026-08-17", candidates=self._candidates(),
            cash_pool_usd=1000.0, price_lookup=prices({"A": 1.0}),
            now=NOW, risk_state=state)
        assert result.plan is None
        assert result.would_submit == 0

    def test_a_blocked_drawdown_allocates_nothing(self):
        state = _state()
        state.drawdown_status = "BLOCK"
        result = dry_run.simulate(
            trading_day="2026-08-17", candidates=self._candidates(),
            cash_pool_usd=1000.0, price_lookup=prices({"A": 1.0}),
            now=NOW, risk_state=state)
        assert result.plan is None and result.would_submit == 0

    def test_unknown_risk_state_allocates_nothing(self):
        result = dry_run.simulate(
            trading_day="2026-08-17", candidates=self._candidates(),
            cash_pool_usd=1000.0, price_lookup=prices({"A": 1.0}),
            now=NOW, risk_state=risk_state.RiskState(trading_day="2026-08-17"))
        assert result.plan is None and result.would_submit == 0


class TestDisprovedFieldsCannotReturn:
    """§12: the KRW aggregates must not become a USD equity source."""

    def test_equity_never_reads_the_krw_totals(self):
        source = (REPO_ROOT / "s1_live" / "equity.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        # Mentioned in prose (the docstring explains WHY they are wrong);
        # never read as a value.
        for field in ("tot_asst_amt", "tot_dncl_amt", "frcr_use_psbl_amt"):
            assert f'get("{field}")' not in code
            assert f'["{field}"]' not in code

    def test_the_broker_reads_only_the_verified_cash_field(self):
        """Checked on the syntax tree, not on the text.

        The method's docstring NAMES the disproved fields, because
        explaining why they are wrong is the point of writing it down --
        a substring search would fail on the explanation instead of on an
        actual read. So the docstring is stripped and only the remaining
        string literals are examined.
        """
        import ast

        from brokers import kis_broker

        assert kis_broker.ACCOUNT_CASH_FIELD == "frcr_dncl_amt_2"
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text(encoding="utf-8")
        function = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "get_account_cash_usd")
        body = function.body[1:] if ast.get_docstring(function) else function.body
        literals = {node.value for statement in body for node in ast.walk(statement)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for disproved in ("tot_asst_amt", "tot_dncl_amt", "frcr_evlu_amt2",
                          "frcr_use_psbl_amt", "output3"):
            assert disproved not in literals, f"{disproved} is read, not merely described"
        assert "output2" in literals, "the verified block IS read"

    def test_the_disproof_is_recorded_in_the_matrix(self):
        from brokers.kis_broker import VERIFICATION_MATRIX

        names = {entry.name for entry in VERIFICATION_MATRIX}
        assert "account_equity_not_in_tot_asst_amt" in names


class TestActualRolloutUnmoved:
    def test_the_live_config_is_still_one_one_one_and_off(self):
        from config.live_rollout_config import LiveRolloutConfig

        config = LiveRolloutConfig.from_env({})
        assert config.enabled is False
        assert (config.max_quantity_per_order, config.max_open_positions,
                config.max_daily_entries) == (1, 1, 1)

    def test_the_s1_source_is_still_off_by_default(self):
        from s1_live import candidate_source

        assert candidate_source.is_s1_source_enabled({}) is False

    def test_no_stage_module_writes_a_rollout_flag(self):
        for name in ("config/s1_rollout_stages.py", "s1_live/readiness.py"):
            source = (REPO_ROOT / name).read_text(encoding="utf-8")
            for forbidden in ("os.environ[", "setenv", "LIVE_ROLLOUT_ENABLED ="):
                assert forbidden not in source, name

    def test_the_planned_profile_is_not_the_actual_config(self):
        from config.live_rollout_config import LiveRolloutConfig

        planned = stages.profile_for(stages.STAGE_AGGRESSIVE)
        actual = LiveRolloutConfig.from_env({})
        assert planned["hard_max_positions"] == 4
        assert actual.max_open_positions == 1, "planned must not have leaked into actual"


class TestOrderStateDatabaseIsolation:
    """Regression: the autouse fixture must protect a file that forgets.

    PHASE 4A added a test that reached `state_store.db.open_db()` without
    setting `STATE_STORE_DB_FILE`, wrote to the real `TRADING_STATE.db`
    at the repository root, and broke twenty unrelated tests -- none of
    them in the new file, all of them passing in isolation.
    """

    def test_the_autouse_fixture_redirects_the_order_state_db(self):
        """This test deliberately does NOT set the variable itself."""
        import os

        from state_store import db

        value = os.environ.get("STATE_STORE_DB_FILE")
        assert value, "the autouse fixture must set STATE_STORE_DB_FILE"
        assert db.DEFAULT_DB_FILE != db.resolve_db_path() if hasattr(db, "resolve_db_path") \
            else str(db.DEFAULT_DB_FILE) != value

    def test_opening_the_db_without_setting_the_variable_stays_off_the_repo_root(self):
        from state_store import db

        conn = db.open_db()
        try:
            assert not db.DEFAULT_DB_FILE.exists(), (
                "a test that forgot to redirect the state DB just wrote to the "
                "repository root")
        finally:
            conn.close()

    def test_a_test_can_still_override_the_default(self, tmp_path, monkeypatch):
        """Per-file isolation must keep working -- the fixture is a floor,
        not a ceiling."""
        import os

        target = tmp_path / "MY_OWN.db"
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(target))
        from state_store import db

        conn = db.open_db()
        try:
            assert os.environ["STATE_STORE_DB_FILE"] == str(target)
            assert target.exists()
        finally:
            conn.close()


class TestOrderRulesAndFeeSource:
    """PHASE 4E: what the official sources did and did not establish."""

    def test_the_minimum_order_rule_is_unknown_not_assumed(self):
        from config import s1_order_rules

        assert s1_order_rules.MINIMUM_ORDER_RULE == s1_order_rules.UNKNOWN
        assert s1_order_rules.minimum_order_verified() is False

    def test_the_whole_share_rule_exists_but_is_not_in_force(self):
        """'No minimum is documented' and 'no minimum exists' are
        different statements; only the first is established."""
        from config import s1_order_rules

        assert s1_order_rules.RULE_WHOLE_SHARE_ONLY == "WHOLE_SHARE_ONLY"
        assert s1_order_rules.MINIMUM_ORDER_RULE != s1_order_rules.RULE_WHOLE_SHARE_ONLY

    def test_the_minimum_order_gate_stays_unmet_by_default(self, conn):
        matrix = full_ready(conn, minimum_order_verified=None)
        unmet = {check.key for check in matrix.unmet_for(stages.STAGE_FIRST_LIVE)}
        assert stages.REQ_MINIMUM_ORDER in unmet

    def test_the_placeholder_is_never_treated_as_verified(self):
        from config import s1_order_rules
        from live_readiness.sizing import DEFAULT_MIN_ORDER_AMOUNT_USD

        assert DEFAULT_MIN_ORDER_AMOUNT_USD == 1.0
        assert s1_order_rules.minimum_order_verified() is False

    def test_no_tick_size_constant_is_invented(self):
        """§4: no rounding rule is created. The policy is named, not
        implemented."""
        from config import s1_order_rules

        assert s1_order_rules.TICK_SIZE_POLICY == s1_order_rules.TICK_POLICY_BROKER_ENFORCED
        assert s1_order_rules.TICK_POLICY_VERIFIED == s1_order_rules.UNKNOWN
        # Checked on the syntax tree. The module docstring NAMES the
        # common US increments precisely to explain why they are not
        # constants here -- a text search would fail on the explanation
        # instead of on an actual rule.
        import ast

        source = (REPO_ROOT / "config" / "s1_order_rules.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        numeric_assignments = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, (int, float)) \
                    and not isinstance(node.value.value, bool):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        numeric_assignments[target.id] = node.value.value
        assert numeric_assignments == {}, (
            f"a numeric tick/price rule was bound: {numeric_assignments}")
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "round" not in called, "a rounding rule was implemented"

    def test_fee_fields_are_confirmed_but_not_usable(self):
        from s1_live import fee_source

        assert "ovrs_fee_smtl" in fee_source.CONFIRMED_FEE_FIELDS
        assert "smtl_fee1" in fee_source.CONFIRMED_FEE_FIELDS
        status = fee_source.accounting_status()
        assert status["fees_status"] == "UNKNOWN"
        assert status["net_pnl"] is None
        assert status["usable"] is False

    def test_an_empty_detail_block_is_reported_as_unverified(self):
        from s1_live import fee_source

        body = {"rt_cd": "0", "output1": [],
                "output2": [{"dmst_fee_smtl": "0", "ovrs_fee_smtl": "0"}]}
        observation = fee_source.observe(body, fee_source.PERIOD_TRANS)
        assert observation.status == fee_source.FIELDS_CONFIRMED
        assert observation.usable_for_accounting is False
        assert "no trade has settled" in observation.detail

    @pytest.mark.parametrize("body", [
        None, "nope", {}, {"rt_cd": "7"}, {"rt_cd": "0"},
        {"rt_cd": "0", "output2": []},
        {"rt_cd": "0", "output2": [{"dmst_fee_smtl": "0"}]}])
    def test_a_malformed_official_response_never_yields_a_fee(self, body):
        from s1_live import fee_source

        observation = fee_source.observe(body, fee_source.PERIOD_TRANS)
        assert observation.usable_for_accounting is False

    def test_a_populated_detail_block_is_still_not_usable_yet(self):
        """Rows existing does not tell us the currency."""
        from s1_live import fee_source

        body = {"rt_cd": "0", "output1": [{"x": 1}],
                "output2": [{"dmst_fee_smtl": "1.5", "ovrs_fee_smtl": "2.5"}]}
        observation = fee_source.observe(body, fee_source.PERIOD_TRANS)
        assert observation.status == fee_source.SEMANTICS_UNVERIFIED
        assert observation.usable_for_accounting is False

    def test_the_module_computes_no_fee_amount(self):
        """It reads and records; it must not produce a number to subtract."""
        import ast

        source = (REPO_ROOT / "s1_live" / "fee_source.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name in ("observe",):
                dumped = ast.dump(node)
                assert "net_pnl" not in dumped
        for rate in ("0.0025", "0.25", "0.007", "COMMISSION_RATE"):
            assert rate not in source
