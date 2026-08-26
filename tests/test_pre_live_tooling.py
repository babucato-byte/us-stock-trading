"""The two scripts that stand between the current posture and a first
real order.

`final_pre_live_check.sh` answers "is this deployment ready to place a
real order right now?" and answers it by checking, never by fixing.
`limited_live_bootstrap.sh` is the one-shot path for the two live-only
wire values, and it must be impossible to run by accident.

The property both share, and the reason these tests exist: **neither may
ever place an order as a side effect of being run.** The checker is run
for real here, in the current posture, and must report PRE_LIVE_BLOCKED
-- a READY today would mean a check is missing, not that we are ready.
"""
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CHECK = REPO_ROOT / "scripts" / "final_pre_live_check.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "limited_live_bootstrap.sh"
CHECK_SOURCE = CHECK.read_text(encoding="utf-8")
BOOTSTRAP_SOURCE = BOOTSTRAP.read_text(encoding="utf-8")


def _code_only(source):
    """Comment lines out. These scripts explain in prose what they must
    never do ("never a direct broker.submit_order() call"), so a naive
    substring search over the whole file matches the explanation and
    fails on a file that is correct."""
    lines = []
    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        lines.append(raw)
    return "\n".join(lines)


CHECK_CODE = _code_only(CHECK_SOURCE)
BOOTSTRAP_CODE = _code_only(BOOTSTRAP_SOURCE)


def _run(script, env=None, timeout=180):
    import os

    merged = {**os.environ, "TRADING_PROJECT_ROOT": str(REPO_ROOT),
              "PYTHON_BIN": sys.executable}
    merged.update(env or {})
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        timeout=timeout, env=merged, cwd=str(REPO_ROOT))


class TestBothScriptsAreSyntacticallySound:
    @pytest.mark.parametrize("script", [CHECK, BOOTSTRAP])
    def test_bash_parses_it(self, script):
        result = subprocess.run(["bash", "-n", str(script)],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("script", [CHECK, BOOTSTRAP])
    def test_it_is_executable(self, script):
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


class TestTheCheckerPlacesNoOrder:
    def test_it_never_names_a_submission_call(self):
        for forbidden in ("submit_order", "submit_buy_order", "cancel_order",
                          "submit_sell_order"):
            assert forbidden not in CHECK_CODE, forbidden

    def test_it_writes_no_state(self):
        """A readiness check that mutates state is not a check."""
        for forbidden in ("set_halt", "activate(", "record_result",
                          "os.replace", "write_text", "open("):
            assert forbidden not in CHECK_CODE, forbidden

    def test_it_redirects_to_no_file(self):
        """The only redirection permitted is discarding output. A shell
        redirect into a path would make the checker a writer."""
        import re

        # `> path` / `>> path`, but not `>/dev/null`, `2>&1`, `>=`, `->`.
        writer = re.compile(r"(?<![0-9\-])>>?\s*(?!/dev/null|&)[A-Za-z._$/\"']")
        for line in CHECK_CODE.splitlines():
            stripped = line.strip()
            if stripped.startswith(("printf", "echo")):
                continue  # message text, not a redirect
            assert not writer.search(line), f"writes to a file: {stripped}"

    def test_it_does_not_print_a_secret(self):
        for forbidden in ("APP_KEY", "APP_SECRET", "access_token",
                          "Authorization", "app_key", "app_secret"):
            assert forbidden not in CHECK_SOURCE, forbidden

    def test_the_account_is_only_ever_masked(self):
        assert "mask_account_number" in CHECK_SOURCE
        assert "account_id}" not in CHECK_SOURCE


class TestTheCheckerBlocksInTheCurrentPosture:
    """Run for real. The current posture has an empty live allow-list and
    six unconfirmed ARMED wire values, so READY would be wrong."""

    def test_it_reports_blocked(self):
        result = _run(CHECK)
        assert "RESULT: PRE_LIVE_BLOCKED" in result.stdout, result.stdout[-2000:]
        assert "PRE_LIVE_READY" not in result.stdout

    def test_the_exit_code_matches_the_verdict(self):
        assert _run(CHECK).returncode == 1

    def test_it_lists_every_blocking_reason_code(self):
        stdout = _run(CHECK).stdout
        assert "BLOCKING REASON CODES" in stdout
        # The two that must be present in this posture, whatever else is.
        assert "LIVE_ALLOWLIST_NOT_EXACTLY_ONE" in stdout
        assert "SESSION_MATRIX_PENDING" in stdout

    def test_an_empty_allowlist_is_a_block(self):
        result = _run(CHECK, env={"LIVE_ROLLOUT_ALLOWED_SYMBOLS": ""})
        assert "LIVE_ALLOWLIST_NOT_EXACTLY_ONE" in result.stdout

    def test_two_allowlisted_symbols_are_also_a_block(self):
        """Limited live means ONE symbol. Two is not 'more ready'."""
        result = _run(CHECK, env={"LIVE_ROLLOUT_ALLOWED_SYMBOLS": "AAPL,MSFT"})
        assert "LIVE_ALLOWLIST_NOT_EXACTLY_ONE" in result.stdout

    def test_a_non_live_kis_env_is_a_block(self):
        result = _run(CHECK, env={"KIS_ENV": "paper"})
        assert "KIS_ENV_NOT_LIVE" in result.stdout

    @pytest.mark.parametrize("var", [
        "LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY",
        "LIVE_ROLLOUT_MAX_DAILY_ENTRIES", "LIVE_ROLLOUT_MAX_QUANTITY"])
    def test_a_widened_limit_is_a_block(self, var):
        result = _run(CHECK, env={var: "5"})
        assert f"{var}_NOT_1" in result.stdout

    def test_a_global_cap_beyond_two_is_a_block(self):
        """One position each for S1 and S6 is the authorised ceiling.
        Two is therefore allowed and three is not -- the global cap is
        no longer pinned to 1, but it is not unbounded either."""
        result = _run(CHECK, env={"LIVE_ROLLOUT_MAX_POSITIONS": "3"})
        assert "LIVE_ROLLOUT_MAX_POSITIONS_NOT_1_OR_2" in result.stdout

    def test_a_per_strategy_cap_above_the_global_one_is_a_block(self):
        """Two numbers that are both meant to be enforced must not
        contradict each other."""
        result = _run(CHECK, env={"LIVE_ROLLOUT_MAX_POSITIONS": "1",
                                  "LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY": "2"})
        assert "PER_STRATEGY_CAP_EXCEEDS_GLOBAL" in result.stdout

class TestTheCheckerSurvivesEveryCommitPosture:
    """The checker must REPORT a mismatch, not die on the branch whose
    whole job is reporting it.

    `${DEPLOYED_COMMIT:0:8}` carries no default, and substring expansion
    of an unset variable under `set -u` is an unbound-variable error --
    in bash 4.4 and later. The dev machines run bash 3.2, where the same
    expansion quietly yields empty, so every behaviour test here passed
    while the production host (bash 5.1) killed the script at that line:
    two lines of header, no RESULT, no reason codes, on the last gate
    before a real order.

    So there are two kinds of test below. The behaviour matrix proves the
    script reports rather than aborts, and `test_every_substring_...`
    proves it statically -- because on bash 3.2 the behaviour matrix
    cannot fail, and the static one can.
    """

    #: Every commit posture the checker can be run in.
    POSTURES = {
        "mismatch": {"DEPLOYED_COMMIT": "deadbeefdeadbeef",
                     "VALIDATED_COMMIT": "deadbeefdeadbeef"},
        "deployed_unset": {"VALIDATED_COMMIT": "deadbeefdeadbeef"},
        "validated_unset": {"DEPLOYED_COMMIT": "deadbeefdeadbeef"},
        "both_unset": {},
        "deployed_empty": {"DEPLOYED_COMMIT": "", "VALIDATED_COMMIT": ""},
        "half_matching": {"DEPLOYED_COMMIT": "deadbeefdeadbeef"},
    }

    def _run_posture(self, name, tmp_path=None):
        import os

        env = {k: v for k, v in os.environ.items()
               if k not in ("DEPLOYED_COMMIT", "VALIDATED_COMMIT")}
        env.update({"TRADING_PROJECT_ROOT": str(REPO_ROOT),
                    "PYTHON_BIN": sys.executable})
        env.update(self.POSTURES[name])
        return subprocess.run(["bash", str(CHECK)], capture_output=True,
                              text=True, timeout=180, env=env,
                              cwd=str(REPO_ROOT))

    @pytest.mark.parametrize("posture", sorted(POSTURES))
    def test_it_never_aborts_on_an_unset_commit(self, posture):
        result = self._run_posture(posture)
        assert "unbound variable" not in result.stderr, result.stderr[-800:]

    @pytest.mark.parametrize("posture", sorted(POSTURES))
    def test_it_always_reaches_a_verdict(self, posture):
        result = self._run_posture(posture)
        assert "RESULT:" in result.stdout, result.stdout[-1500:]

    @pytest.mark.parametrize("posture", sorted(POSTURES))
    def test_a_mismatch_is_named_as_a_blocking_reason(self, posture):
        result = self._run_posture(posture)
        assert "BLOCKING REASON CODES" in result.stdout, result.stdout[-1500:]
        assert "COMMIT_MISMATCH" in result.stdout, result.stdout[-1500:]

    @pytest.mark.parametrize("posture", sorted(POSTURES))
    def test_a_mismatch_exits_non_zero(self, posture):
        """Non-zero is the policy. Dying at line 44 also exits non-zero,
        which is exactly why the exit code alone never caught this."""
        assert self._run_posture(posture).returncode != 0

    def test_an_unreadable_head_is_reported_not_fatal(self, tmp_path):
        import os

        env = {k: v for k, v in os.environ.items()
               if k not in ("DEPLOYED_COMMIT", "VALIDATED_COMMIT")}
        env.update({"TRADING_PROJECT_ROOT": str(tmp_path),
                    "PYTHON_BIN": sys.executable})
        result = subprocess.run(["bash", str(CHECK)], capture_output=True,
                                text=True, timeout=180, env=env,
                                cwd=str(tmp_path))
        assert "unbound variable" not in result.stderr, result.stderr[-800:]
        assert "COMMIT_UNREADABLE" in result.stdout, result.stdout[-1500:]
        assert "RESULT:" in result.stdout
        assert result.returncode != 0

    def test_every_substring_expansion_reads_a_locally_assigned_name(self):
        """The one that works on bash 3.2.

        `${NAME:offset:length}` on a name the script did not assign is an
        unbound-variable error on any bash the production host runs. The
        fix is not `set +u` -- a safety script that stops checking its
        own inputs is worse than one that aborts loudly. It is to default
        once into a local name and read only that.
        """
        import re

        # A name is "assigned" if the script binds it: plain assignment,
        # a `read` target, or a `for` loop variable. All three are set by
        # the time the body runs; an env-supplied name is not.
        assigned = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=",
                                  CHECK_CODE, re.MULTILINE))
        assigned |= set(re.findall(r"\bread\b(?:\s+-[A-Za-z]+)*\s+"
                                   r"([A-Za-z_][A-Za-z0-9_]*)", CHECK_CODE))
        assigned |= set(re.findall(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b",
                                   CHECK_CODE))
        used = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):[0-9]",
                          CHECK_CODE)
        unguarded = sorted({n for n in used if n not in assigned})
        assert not unguarded, (
            f"substring-expanded without a local default: {unguarded}. "
            "Assign NAME_SAFE=\"${NAME:-}\" first and expand that.")

    def test_set_u_is_still_on(self):
        """The fix must not be a weakening. `set -u` is what makes an
        unset variable a fault instead of an empty string."""
        assert "set -uo pipefail" in CHECK_CODE
        assert "set +u" not in CHECK_CODE


    def test_an_unparseable_check_line_is_treated_as_a_failure(self):
        """Unknown is not ready -- the parser must not skip a line it
        cannot classify."""
        assert "CHECK_OUTPUT_UNPARSEABLE" in CHECK_SOURCE


class TestTheBootstrapCannotRunByAccident:
    def test_it_requires_an_explicit_acknowledgement(self):
        assert "LIVE_BOOTSTRAP_ACK" in BOOTSTRAP_SOURCE

    def test_without_the_ack_it_places_nothing(self):
        result = _run(BOOTSTRAP)
        assert "BOOTSTRAP_BLOCKED" in result.stdout
        assert "No order was placed" in result.stdout or "Nothing was submitted" in result.stdout

    def test_with_the_ack_but_failing_preconditions_it_still_places_nothing(self):
        """The acknowledgement is not an override. Preconditions are
        evaluated first and independently."""
        result = _run(BOOTSTRAP, env={"LIVE_BOOTSTRAP_ACK": "true"})
        assert "BOOTSTRAP_BLOCKED" in result.stdout
        assert "PRE_LIVE_READY" not in result.stdout

    def test_it_delegates_preconditions_rather_than_restating_them(self):
        """Two copies of "is this safe" drift, and the copy that drifts is
        the one that lets an order through."""
        assert "final_pre_live_check.sh" in BOOTSTRAP_SOURCE

    def test_the_submission_step_is_wired_to_the_runner_only(self):
        """The script itself must contain no order logic. It delegates to
        one Python entry point and branches on its exit code -- so every
        safety decision lives in testable Python rather than in bash."""
        assert "BOOTSTRAP_NOT_IMPLEMENTED" not in BOOTSTRAP_SOURCE
        assert "run_limited_live_bootstrap.py" in BOOTSTRAP_CODE
        executable = [l for l in BOOTSTRAP_CODE.splitlines()
                      if not l.strip().startswith(("printf", "echo"))]
        for line in executable:
            # It may INVOKE the runner; it may not name a transport verb
            # itself, which would mean order logic had leaked into bash.
            for forbidden in ("submit_order", "cancel_order", "submit_buy_order"):
                assert forbidden not in line, f"{forbidden} in: {line.strip()}"

    def test_it_invokes_the_runner_exactly_once_and_never_retries(self):
        """A second invocation after an ambiguous first is precisely the
        duplicate-order case the design exists to prevent."""
        invocations = [l for l in BOOTSTRAP_CODE.splitlines()
                       if "${PYTHON_BIN}" in l and "${RUNNER}" in l]
        assert len(invocations) == 1, invocations
        assigned = [l for l in BOOTSTRAP_CODE.splitlines()
                    if l.strip().startswith("RUNNER=")]
        assert len(assigned) == 1 and "run_limited_live_bootstrap.py" in assigned[0]
        # No loop construct anywhere: the runner cannot be re-entered by
        # the wrapper regardless of what it returned.
        for looping in ("while ", "until ", "for ((", "for reason", " do\n"):
            assert looping not in BOOTSTRAP_CODE, looping

    def test_a_nonzero_runner_exit_is_never_treated_as_success(self):
        for code, banner in (("1", "BOOTSTRAP_BLOCKED"),
                             ("3", "BOOTSTRAP_UNKNOWN")):
            assert f"{code})" in BOOTSTRAP_CODE
            assert banner in BOOTSTRAP_CODE
        assert "RETRY=BLOCKED" in BOOTSTRAP_CODE
        assert "RECONCILIATION_REQUIRED=true" in BOOTSTRAP_CODE
        assert "NEW_ENTRY_BLOCKED=true" in BOOTSTRAP_CODE

    def test_it_states_the_transport_and_retry_budget(self):
        assert "1 order, 0 retries" in BOOTSTRAP_SOURCE
        assert "no retry" in BOOTSTRAP_SOURCE.lower()

    def test_it_names_the_two_live_only_values_it_exists_for(self):
        assert "order_tr_id_live_buy" in BOOTSTRAP_SOURCE
        assert "cancel_tr_id_live" in BOOTSTRAP_SOURCE

    def test_it_enables_no_service_or_timer(self):
        for forbidden in ("systemctl", "--now"):
            assert forbidden not in BOOTSTRAP_CODE, forbidden

    def test_it_does_not_widen_any_limit(self):
        """It must not ASSIGN any safety flag or limit."""
        for forbidden in ("LIVE_ROLLOUT_MAX_POSITIONS=", "LIVE_ROLLOUT_MAX_QUANTITY=",
                          "LIVE_ROLLOUT_MAX_DAILY_ENTRIES=", "ENTRY_DISABLED=",
                          "KIS_LIVE_ORDER_ENABLED=", "LIVE_ROLLOUT_ENABLED=",
                          "LIVE_ROLLOUT_ALLOWED_SYMBOLS="):
            assert forbidden not in BOOTSTRAP_CODE, forbidden


class TestLiveAndPaperRequirementsAreSeparate:
    """`cancel_tr_id_paper` is the paper cancel TR. `_env_key()` selects
    the LIVE one whenever KIS_ENV=live, so no live order or cancel can
    read it -- gating live eligibility on evidence about a code path a
    live order cannot take would block a real order for no safety gain.

    Its scope changed; its evidence did not. It is still
    LIVE_RESPONSE_PENDING, because no response has confirmed it, and
    that distinction is the point: moving a value out of scope is not
    the same as confirming it.
    """

    def test_the_paper_value_is_not_an_armed_requirement(self):
        from brokers import kis_broker

        armed = {e.name for e in kis_broker.matrix_entries_for(kis_broker.REQUIRED_FOR_ARMED)}
        assert "cancel_tr_id_paper" not in armed

    def test_its_evidence_was_not_fabricated(self):
        from brokers import kis_broker

        entry = next(e for e in kis_broker.VERIFICATION_MATRIX
                     if e.name == "cancel_tr_id_paper")
        assert entry.live_status == kis_broker.LIVE_RESPONSE_PENDING
        assert kis_broker.REQUIRED_FOR_PAPER in entry.required_for

    def test_it_is_still_tracked_under_its_own_scope(self):
        from brokers import kis_broker

        assert list(kis_broker.pending_items_for(kis_broker.REQUIRED_FOR_PAPER)) == [
            "cancel_tr_id_paper"]

    def test_armed_now_waits_on_exactly_the_five_live_only_values(self):
        from brokers import kis_broker

        assert set(kis_broker.pending_items_for(kis_broker.REQUIRED_FOR_ARMED)) == {
            "order_path", "order_tr_id_live_buy", "cancel_path",
            "cancel_tr_id_live", "cancel_price_field_rule"}

    def test_observe_is_unaffected(self):
        from brokers import kis_broker

        assert kis_broker.pending_items_for(kis_broker.REQUIRED_FOR_OBSERVE) == ()

    def test_a_live_env_selects_only_the_live_cancel_tr(self):
        from brokers.kis_broker import TR_ID_CANCEL

        assert TR_ID_CANCEL["live"] != TR_ID_CANCEL["paper"]
        # The selector the broker uses.
        from brokers.kis_broker import KISBroker
        from brokers.kis_config import KISConfig

        live_cfg = KISConfig(kis_env="live", app_key="k", app_secret="s",
                             account_no="12345678", account_product_cd="01",
                             account_read_enabled=True, live_order_enabled=False)
        broker = KISBroker(config=live_cfg, session=object())
        assert TR_ID_CANCEL[broker._env_key()] == TR_ID_CANCEL["live"]

    def test_no_live_cancel_path_references_the_paper_tr_id(self):
        """Static: the paper TR literal must appear in exactly one place
        in CODE -- the env-keyed table -- never hard-coded into a branch.

        Prose is excluded: the module docstring and comments discuss both
        TR ids by name, and matching those would fail a correct file.
        """
        import ast

        path = REPO_ROOT / "brokers" / "kis_broker.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef))
            and getattr(node, "body", None)
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        code_lines = {
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "VTTT1004U"
            and id(node) not in docstrings
        }
        assert len(code_lines) == 1, f"paper TR appears in code at lines {sorted(code_lines)}"
        line = source.splitlines()[min(code_lines) - 1]
        assert "TR_ID_CANCEL" in line, line


class TestTheCheckerDistinguishesCashOutcomes:
    def test_an_unpriced_candidate_is_not_reported_as_a_shortfall(self):
        """"Nothing was priced" and "the account is short" are different
        findings; reporting the first as the second is an invented one."""
        assert "ORDERABLE_CASH_NOT_EVALUATED" in CHECK_SOURCE
        assert "no candidate to price" in CHECK_SOURCE

    def test_a_real_shortfall_keeps_its_own_code(self):
        assert "INSUFFICIENT_ORDERABLE_CASH" in CHECK_SOURCE


class TestTheCheckerRefreshesAStaleSnapshot:
    def test_only_staleness_is_retried(self):
        """A dirty / unknown / halted snapshot must stay a hard failure;
        only an aged-out one is refreshed, and only once."""
        assert 'stale = "STALE" in reason.upper() or "MISSING" in reason.upper()' in CHECK_SOURCE
        assert "run_reconciliation.py" in CHECK_SOURCE
        assert "after refresh" in CHECK_SOURCE
        # Dirty/unknown/halted take the else branch and are never retried.
        assert "never retried, never hidden" in CHECK_SOURCE

    def test_the_refresh_is_opt_in_so_a_plain_run_writes_nothing(self):
        """The checker's contract is that it checks and never fixes.
        Running reconciliation writes a snapshot, so it happens only when
        the operator asks -- which is also why invoking this script from
        a test suite leaves no state behind."""
        assert "PRE_LIVE_ALLOW_RECONCILE_REFRESH" in CHECK_SOURCE
        assert "allow_refresh" in CHECK_SOURCE

    def test_the_refreshed_snapshot_is_the_one_judged(self):
        assert CHECK_SOURCE.count("snapshot = freshness.evaluate()") == 2

    def test_success_is_not_read_from_a_nonexistent_flag(self):
        """freshness.evaluate() signals success by returning and failure
        by raising; it has no `.usable`. Reading one reported a healthy
        snapshot as a blocker."""
        assert "snapshot.usable" not in CHECK_SOURCE


class TestTheThreeStateVerdict:
    def test_all_three_states_exist(self):
        for state in ("PRE_LIVE_READY", "PRE_LIVE_BLOCKED", "READY_FOR_LIVE_BOOTSTRAP"):
            assert state in CHECK_SOURCE

    def test_bootstrap_state_requires_everything_else_to_pass(self):
        """It is not a weaker READY. Exactly two reason codes are
        tolerated -- the ARMED matrix items the bootstrap exists to
        confirm, and "not ARMED", which is the state it runs in. Every
        other reason still blocks, which the count comparison enforces:
        the tolerated ones must account for the WHOLE list."""
        assert "SESSION_MATRIX_PENDING|POSTURE_NOT_ARMED" in CHECK_SOURCE
        assert '"${BOOTSTRAP_TOLERATED}" -eq "${#REASONS[@]}"' in CHECK_SOURCE
        # INVALID_BOOTSTRAP_POSTURE is not tolerated, so a posture that is
        # neither ARMED nor LIMITED_LIVE_BOOTSTRAP still blocks.
        assert "INVALID_BOOTSTRAP_POSTURE" in CHECK_SOURCE

    def test_bootstrap_state_states_its_narrow_scope(self):
        assert "1 symbol, 1 share, 1 BUY, at most 1 CANCEL" in CHECK_SOURCE
        assert "NOT approval for ARMED or AUTO LIVE" in CHECK_SOURCE

    def test_a_pending_item_beyond_the_five_blocks_outright(self):
        assert "SESSION_PENDING_BEYOND_BOOTSTRAP" in CHECK_SOURCE


class TestTheBootstrapPostureIsASeparateCapability:
    """LIMITED_LIVE_BOOTSTRAP is not a weaker ARMED.

    Five wire values can only be established by a real live response.
    Confirming them by switching the whole system to ARMED would mean the
    first real order is placed by the general trading path rather than by
    a one-shot with every precondition checked. So the capability is its
    own flag and its own posture, and the three live-entry flags keep
    their existing meaning untouched.
    """

    def _posture(self, **env):
        from live_pilot.posture import resolve_posture

        return resolve_posture(env)

    def test_the_default_is_still_observe(self):
        assert self._posture().posture == "OBSERVE"

    def test_the_bootstrap_flag_alone_reaches_the_bootstrap_posture(self):
        decision = self._posture(LIVE_BOOTSTRAP_ENABLED="true")
        assert decision.posture == "LIMITED_LIVE_BOOTSTRAP"
        assert decision.bootstrap is True
        assert decision.armed is False

    def test_it_does_not_turn_on_any_live_entry_flag(self):
        """The general scanner -> live order path reads these three. The
        bootstrap capability must leave all of them alone."""
        decision = self._posture(LIVE_BOOTSTRAP_ENABLED="true")
        assert decision.live_order_enabled is False
        assert decision.rollout_enabled is False

    def test_armed_still_requires_all_three_flags(self):
        assert self._posture(KIS_LIVE_ORDER_ENABLED="true").posture == "OBSERVE"
        assert self._posture(LIVE_ROLLOUT_ENABLED="true").posture == "OBSERVE"
        assert self._posture(KIS_LIVE_ORDER_ENABLED="true",
                             LIVE_ROLLOUT_ENABLED="true").posture == "ARMED"

    def test_armed_is_not_shadowed_by_the_bootstrap_flag(self):
        """Once the three flags really are on, ARMED is the honest
        description; the bootstrap capability must not mask it."""
        decision = self._posture(KIS_LIVE_ORDER_ENABLED="true",
                                 LIVE_ROLLOUT_ENABLED="true",
                                 LIVE_BOOTSTRAP_ENABLED="true")
        assert decision.posture == "ARMED"

    def test_entry_disabled_does_not_block_the_bootstrap_posture(self):
        """ENTRY_DISABLED blocks the general entry path, which is exactly
        the state the bootstrap runs in."""
        assert self._posture(ENTRY_DISABLED="true",
                             LIVE_BOOTSTRAP_ENABLED="true"
                             ).posture == "LIMITED_LIVE_BOOTSTRAP"

    def test_the_general_order_gate_ignores_the_bootstrap_flag(self):
        """Capability isolation, statically: no gate, engine or entry
        pipeline may read it."""
        for rel in ("execution/order_gate.py", "execution/execution_engine.py",
                    "kis_live_trading.py", "scripts/run_shadow_mode.py",
                    "live_pilot/armed.py", "live_pilot/runner.py"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "LIVE_BOOTSTRAP_ENABLED" not in source, rel

    def test_the_checker_uses_a_bootstrap_specific_reason(self):
        assert "INVALID_BOOTSTRAP_POSTURE" in CHECK_SOURCE

    def test_posture_not_armed_no_longer_blocks_the_bootstrap_verdict(self):
        """The contradiction this fixes: requiring ARMED made
        READY_FOR_LIVE_BOOTSTRAP unreachable, since reaching ARMED is
        what the bootstrap exists to make possible."""
        assert "SESSION_MATRIX_PENDING|POSTURE_NOT_ARMED" in CHECK_SOURCE

    def test_pre_live_ready_still_requires_armed(self):
        assert 'check("POSTURE_NOT_ARMED", decision.posture == "ARMED"' in CHECK_SOURCE


class TestReadyForLiveBootstrapIsActuallyReachable:
    """A verdict that can never be produced is not a verdict.

    Everything in this class runs the REAL checker script against a
    throwaway git repository outside the project, with the Python half
    replaced by a stub that emits a controlled result set. That isolates
    what is being tested -- the shell-level checks and the verdict
    arithmetic -- from a live KIS account and from whatever state this
    working tree happens to be in.

    The tolerated-reason list is deliberately tiny: SESSION_MATRIX_PENDING
    and POSTURE_NOT_ARMED, the two things a bootstrap exists to resolve.
    Anything else outstanding must still produce PRE_LIVE_BLOCKED, which
    is what test_any_other_reason_blocks_it pins.
    """

    PY_LINES = [
        "OK::OBSERVE_MATRIX_PENDING::7/7 confirmed",
        "BAD::SESSION_MATRIX_PENDING::[REGULAR] pending: order_path, order_tr_id_live_buy",
        "OK::SESSION_PENDING_BEYOND_BOOTSTRAP::none",
        "BOOTSTRAPABLE::yes",
        "OK::RECONCILIATION_NOT_USABLE::fresh and clean (age 1.0s)",
        "OK::HALT_ACTIVE::halted=False",
        "BAD::POSTURE_NOT_ARMED::posture=LIMITED_LIVE_BOOTSTRAP",
    ]

    def _fixture(self, tmp_path, py_lines=None, env=None):
        import os
        import shutil

        release = tmp_path / "release"
        (release / "scripts").mkdir(parents=True)
        shutil.copy(CHECK, release / "scripts" / CHECK.name)
        (release / "README").write_text("fixture\n")
        run = lambda *a: subprocess.run(  # noqa: E731
            a, cwd=str(release), capture_output=True, text=True, timeout=60)
        run("git", "init", "-q")
        run("git", "config", "user.email", "fixture@example.invalid")
        run("git", "config", "user.name", "fixture")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "fixture")
        head = run("git", "rev-parse", "HEAD").stdout.strip()

        stub = tmp_path / "python-stub"
        body = "\n".join(py_lines if py_lines is not None else self.PY_LINES)
        stub.write_text("#!/usr/bin/env bash\ncat >/dev/null\n"
                        f"cat <<'OUT'\n{body}\nOUT\n")
        stub.chmod(0o755)

        merged = {
            **os.environ,
            "TRADING_PROJECT_ROOT": str(release),
            "PYTHON_BIN": str(stub),
            "DEPLOYED_COMMIT": head,
            "VALIDATED_COMMIT": head,
            "KIS_ENV": "live",
            "LIVE_ROLLOUT_MAX_POSITIONS": "1",
            "LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY": "1",
            "LIVE_ROLLOUT_MAX_DAILY_ENTRIES": "1",
            "LIVE_ROLLOUT_MAX_QUANTITY": "1",
            "LIVE_ROLLOUT_ALLOWED_SYMBOLS": "AAPL",
            "SLACK_WEBHOOK_URL": "https://hooks.slack.test/general",
            "SLACK_ALERT_WEBHOOK_URL": "https://hooks.slack.test/alert",
            "KIS_LIVE_SLACK_WEBHOOK_URL": "https://hooks.slack.test/kis-general",
            "KIS_LIVE_SLACK_ALERT_WEBHOOK_URL": "https://hooks.slack.test/kis-alert",
        }
        merged.update(env or {})
        return subprocess.run(
            ["bash", str(release / "scripts" / CHECK.name)],
            capture_output=True, text=True, timeout=180, env=merged, cwd=str(release))

    def test_a_clean_fixture_reaches_ready_for_live_bootstrap(self, tmp_path):
        result = self._fixture(tmp_path)
        assert "READY_FOR_LIVE_BOOTSTRAP" in result.stdout, result.stdout
        assert result.returncode == 0

    def test_readiness_does_not_require_the_acknowledgement(self, tmp_path):
        """READY -> operator ack -> BUY. Requiring the ack to reach READY
        would invert that order and make the ack meaningless."""
        for ack in ("false", ""):
            result = self._fixture(tmp_path / f"ack-{ack or 'unset'}",
                                   env={"LIVE_BOOTSTRAP_ACK": ack})
            assert "READY_FOR_LIVE_BOOTSTRAP" in result.stdout, result.stdout

    def test_a_missing_kis_live_webhook_blocks_readiness(self, tmp_path):
        for missing in ("KIS_LIVE_SLACK_WEBHOOK_URL", "KIS_LIVE_SLACK_ALERT_WEBHOOK_URL"):
            result = self._fixture(tmp_path / f"no-{missing}", env={missing: ""})
            assert "KIS_LIVE_NOTIFICATION_NOT_CONFIGURED" in result.stdout
            assert "READY_FOR_LIVE_BOOTSTRAP" not in result.stdout
            assert result.returncode == 1

    def test_the_alpaca_webhooks_alone_are_not_enough(self, tmp_path):
        """The failure mode this closes: a deployment that looks notified
        because the paper channels are configured."""
        result = self._fixture(tmp_path, env={
            "KIS_LIVE_SLACK_WEBHOOK_URL": "", "KIS_LIVE_SLACK_ALERT_WEBHOOK_URL": ""})
        assert "SLACK_WEBHOOK_UNCONFIGURED" not in result.stdout
        assert "KIS_LIVE_NOTIFICATION_NOT_CONFIGURED" in result.stdout

    def test_the_kis_live_webhook_value_is_never_printed(self, tmp_path):
        secret = "https://hooks.slack.test/kis-general-SECRETTOKEN"
        result = self._fixture(tmp_path, env={"KIS_LIVE_SLACK_WEBHOOK_URL": secret})
        assert "SECRETTOKEN" not in result.stdout
        assert "SECRETTOKEN" not in result.stderr

    def test_any_other_reason_blocks_it(self, tmp_path):
        """The bootstrap verdict tolerates exactly two reason codes. A
        third -- here, an unconfirmed OBSERVE value -- must still block."""
        lines = list(self.PY_LINES)
        lines[0] = "BAD::OBSERVE_MATRIX_PENDING::6/7 confirmed"
        result = self._fixture(tmp_path, py_lines=lines)
        assert "READY_FOR_LIVE_BOOTSTRAP" not in result.stdout
        assert "PRE_LIVE_BLOCKED" in result.stdout

    def test_an_info_line_is_reported_without_blocking(self, tmp_path):
        """Informational lines carry facts an operator's decision turns
        on -- the other route's evidence, the occupancy of slots that are
        not the one about to trade -- and are deliberately not verdicts.

        Emitting them as bare text made the catch-all treat each one as
        CHECK_OUTPUT_UNPARSEABLE, so three purely informational lines
        became three blocking reason codes and the bootstrap verdict
        became unreachable."""
        lines = list(self.PY_LINES)
        lines.insert(1, "INFO::OTHER_ROUTE_PENDING::ARMED: 5 pending")
        lines.insert(2, "INFO::STRATEGY_SLOT_S1::S1=1/1 ['TX']")
        result = self._fixture(tmp_path, py_lines=lines)
        assert "CHECK_OUTPUT_UNPARSEABLE" not in result.stdout
        assert "OTHER_ROUTE_PENDING" in result.stdout
        assert "READY_FOR_LIVE_BOOTSTRAP" in result.stdout

    def test_a_genuinely_unparseable_line_still_blocks(self, tmp_path):
        """The catch-all must keep meaning "a check whose verdict is
        unknown". Adding INFO:: as a known prefix must not have widened
        it into "anything the parser does not recognise is fine"."""
        lines = list(self.PY_LINES)
        lines.insert(1, "this line has no recognised prefix")
        result = self._fixture(tmp_path, py_lines=lines)
        assert "CHECK_OUTPUT_UNPARSEABLE" in result.stdout
        assert "PRE_LIVE_BLOCKED" in result.stdout

    def test_an_allowlist_that_is_not_exactly_one_blocks_it(self, tmp_path):
        for value in ("", "AAPL,MSFT"):
            result = self._fixture(tmp_path / f"al-{len(value)}",
                                   env={"LIVE_ROLLOUT_ALLOWED_SYMBOLS": value})
            assert "LIVE_ALLOWLIST_NOT_EXACTLY_ONE" in result.stdout
            assert "READY_FOR_LIVE_BOOTSTRAP" not in result.stdout

    def test_a_non_bootstrapable_matrix_blocks_it(self, tmp_path):
        """BOOTSTRAPABLE::no means something beyond the five live-only
        values is unconfirmed, so the bootstrap would be premature."""
        lines = [l.replace("BOOTSTRAPABLE::yes", "BOOTSTRAPABLE::no")
                 for l in self.PY_LINES]
        result = self._fixture(tmp_path, py_lines=lines)
        assert "READY_FOR_LIVE_BOOTSTRAP" not in result.stdout

    def test_the_checker_still_writes_nothing(self, tmp_path):
        import hashlib

        release = None
        result = self._fixture(tmp_path)
        assert result.returncode == 0
        release = tmp_path / "release"
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(release),
                                capture_output=True, text=True, timeout=60)
        assert status.stdout.strip() == "", status.stdout
        assert hashlib.sha256  # the import is the point of the assertion above
