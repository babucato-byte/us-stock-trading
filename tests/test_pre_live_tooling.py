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
        assert "ARMED_MATRIX_PENDING" in stdout

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
        "LIVE_ROLLOUT_MAX_POSITIONS", "LIVE_ROLLOUT_MAX_DAILY_ENTRIES",
        "LIVE_ROLLOUT_MAX_QUANTITY"])
    def test_a_widened_limit_is_a_block(self, var):
        result = _run(CHECK, env={var: "5"})
        assert f"{var}_NOT_1" in result.stdout

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

    def test_the_submission_step_is_not_wired(self):
        """Until it is, the script must say so and exit non-zero rather
        than appear to have succeeded."""
        assert "BOOTSTRAP_NOT_IMPLEMENTED" in BOOTSTRAP_SOURCE
        # The decisive property: it runs no Python at all, so it cannot
        # reach a broker however its prose is worded.
        executable = [l for l in BOOTSTRAP_CODE.splitlines()
                      if not l.strip().startswith(("printf", "echo"))]
        for line in executable:
            for forbidden in ("python", "submit_order", "cancel_order"):
                assert forbidden not in line, f"{forbidden} in: {line.strip()}"

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
