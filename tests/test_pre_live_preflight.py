"""The one-command pre-live preflight: read-only, and it covers what it says.

The script runs on the production host and cannot be executed in the
test suite (it reads the host's env file, crontab and process table),
so what is pinned here is its CONTRACT: every check the operator relies
on is present by name, the script is syntactically valid bash, it never
writes state or places orders, and it ends in exactly one of the three
verdicts.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPT = REPO_ROOT / "scripts" / "pre_live_preflight.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")

#: The checks the operator asked for, by the token that names each.
REQUIRED_CHECKS = (
    "HEAD==VALIDATED==DEPLOYED",
    "production worktree clean",
    "schema version",
    "peak_price_at",
    "KIS token cache",
    "KIS_ACCOUNT_NO == KIS_ALLOWED_ACCOUNT_NO",
    "cash probe",
    "reconciliation clean",
    "unknown_count",
    "security-type cache readable",
    "universe readable",
    "daily_liquidity readable",
    "collector running",
    "collector SHA == deployed SHA",
    "collector connected",
    "collector subscriptions restored",
    "SCAN_CRON",
    "ENTRY_CRON",
    "EXIT_CRON",
    "RECONCILIATION_CRON",
    "POST_EXIT_CRON",
    "runtime lock path valid",
    "disk",
)


class TestTheContract:
    @pytest.mark.parametrize("token", REQUIRED_CHECKS)
    def test_every_required_check_is_present(self, token):
        assert token in SOURCE, token

    def test_it_is_valid_bash(self):
        bash = shutil.which("bash")
        if not bash:
            pytest.skip("bash not available")
        result = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_it_is_executable_and_has_a_shebang(self):
        assert SOURCE.startswith("#!/bin/bash")

    def test_it_returns_exactly_the_three_verdicts(self):
        verdicts = set(re.findall(r"RESULT: (PASS|WARNING|BLOCKED)", SOURCE))
        assert verdicts == {"PASS", "WARNING", "BLOCKED"}
        assert "FAILED: %s" in SOURCE, "a BLOCKED result must list the failed checks"


class TestItIsReadOnly:
    def test_the_database_is_opened_read_only(self):
        assert "mode=ro" in SOURCE
        assert "uri=True" in SOURCE

    def test_it_never_writes_state_or_moves_files(self):
        for forbidden in ("mv -f", "rm -rf", "os.replace", "write_text(",
                          "ALTER TABLE", "run_migrations", "crontab -e",
                          "kill ", "pkill", "systemctl restart"):
            assert forbidden not in SOURCE, forbidden

    def test_it_only_calls_the_documented_read_only_probe(self):
        assert "verify_kis_account_cash.py" in SOURCE
        for forbidden in ("submit_order", "run_live_buy_entry", "kis_live_trading",
                          "execution_engine", "place_order", "run_s6_runtime"):
            assert forbidden not in SOURCE, forbidden

    def test_it_never_prints_a_secret(self):
        """Only the requested key is read from the env file, and the
        secret-bearing names are never echoed."""
        assert 'grep -m1 "^$1="' in SOURCE
        for name in ("KIS_APP_SECRET", "KIS_APP_KEY", "ALPACA_SECRET_KEY",
                     "WEBHOOK_URL"):
            assert name not in SOURCE, name

    def test_it_does_not_abort_on_the_first_failure(self):
        """The 2026-09-06 switch script died on `diff | grep` under
        pipefail before its final move; a preflight must report every
        check rather than stop at the first."""
        active = "\n".join(line for line in SOURCE.splitlines()
                           if not line.lstrip().startswith("#"))
        assert "set -e" not in active
        assert "pipefail" not in active
