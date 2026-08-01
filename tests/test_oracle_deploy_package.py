"""CODEX-049: the Oracle deployment package must be executable FROM THE
REPOSITORY, not described in prose.

These tests verify, without touching a real Oracle host:

  - every systemd unit file exists and parses as an INI file with the
    required sections and hardening directives;
  - every ExecStart/ExecStartPre target actually exists in this repo;
  - every entrypoint module imports;
  - the preflight script exits non-zero on an unsafe/inconsistent
    configuration and zero on the documented read-only posture;
  - `us-stock-trading-live.service` is not enabled by the installer;
  - every path and command the runbook names exists.
"""
import configparser
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = REPO_ROOT / "deploy" / "systemd"
SCRIPTS_DIR = REPO_ROOT / "scripts"
RUNBOOK = REPO_ROOT / "docs" / "deployment" / "ORACLE_KIS_MIGRATION_RUNBOOK.md"

SERVICE_UNITS = [
    "us-stock-trading-shadow.service",
    "us-stock-trading-reconcile.service",
    "us-stock-trading-live.service",
]
TIMER_UNITS = [
    "us-stock-trading-shadow.timer",
    "us-stock-trading-reconcile.timer",
]
ENTRYPOINTS = [
    "preflight_kis_live.py",
    "run_shadow_mode.py",
    "run_reconciliation.py",
    "run_live_buy_entry.py",
]


def _parse_unit(name):
    parser = configparser.ConfigParser(strict=False)
    # systemd allows repeated keys (e.g. several ExecStartPre lines);
    # configparser's strict mode does not, hence strict=False.
    parser.read(UNIT_DIR / name, encoding="utf-8")
    return parser


class TestUnitFilesExist:
    @pytest.mark.parametrize("unit", SERVICE_UNITS + TIMER_UNITS)
    def test_unit_file_exists(self, unit):
        assert (UNIT_DIR / unit).is_file()

    @pytest.mark.parametrize("unit", SERVICE_UNITS + TIMER_UNITS)
    def test_unit_file_parses(self, unit):
        parser = _parse_unit(unit)
        assert parser.has_section("Unit")
        assert parser.has_section("Install")

    @pytest.mark.parametrize("unit", SERVICE_UNITS)
    def test_service_section_present(self, unit):
        assert _parse_unit(unit).has_section("Service")

    @pytest.mark.parametrize("unit", TIMER_UNITS)
    def test_timer_section_present(self, unit):
        assert _parse_unit(unit).has_section("Timer")


class TestServiceHardening:
    @pytest.mark.parametrize("unit", SERVICE_UNITS)
    def test_required_hardening_directives(self, unit):
        service = _parse_unit(unit)["Service"]
        assert service.get("Restart") == "on-failure"
        assert service.get("RestartSec") == "10"
        assert service.get("NoNewPrivileges") == "true"
        assert service.get("PrivateTmp") == "true"
        assert service.get("ProtectSystem") == "full"
        assert service.get("ReadWritePaths")

    @pytest.mark.parametrize("unit", SERVICE_UNITS)
    def test_uses_the_readonly_environment_file(self, unit):
        service = _parse_unit(unit)["Service"]
        assert service.get("EnvironmentFile") == "/etc/us-stock-trading/live-readonly.env"

    @pytest.mark.parametrize("unit", SERVICE_UNITS)
    def test_runs_preflight_before_start(self, unit):
        raw = (UNIT_DIR / unit).read_text(encoding="utf-8")
        assert "ExecStartPre=" in raw
        assert "preflight_kis_live.py" in raw


class TestExecStartTargetsExist:
    @pytest.mark.parametrize("unit", SERVICE_UNITS)
    def test_every_exec_target_exists_in_this_repo(self, unit):
        raw = (UNIT_DIR / unit).read_text(encoding="utf-8")
        targets = re.findall(r"^ExecStart(?:Pre)?=(.+)$", raw, flags=re.MULTILINE)
        assert targets, f"{unit} declares no ExecStart"
        for line in targets:
            script = [token for token in line.split() if token.endswith(".py")]
            assert script, f"{unit}: no python entrypoint in {line!r}"
            name = Path(script[0]).name
            assert (SCRIPTS_DIR / name).is_file(), f"{unit} references missing script {name}"

    @pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
    def test_entrypoint_imports_cleanly(self, entrypoint):
        result = subprocess.run(
            [sys.executable, "-c",
             f"import runpy, sys; sys.argv=['x','--help']; "
             f"runpy.run_path(r'{SCRIPTS_DIR / entrypoint}', run_name='not_main')"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
    def test_entrypoint_is_executable(self, entrypoint):
        path = SCRIPTS_DIR / entrypoint
        assert path.stat().st_mode & 0o111, f"{entrypoint} is not executable"


class TestInstallerScript:
    def test_installer_exists_and_is_valid_bash(self):
        installer = SCRIPTS_DIR / "install_oracle_services.sh"
        assert installer.is_file()
        result = subprocess.run(["bash", "-n", str(installer)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_installer_enables_only_the_readonly_services(self):
        raw = (SCRIPTS_DIR / "install_oracle_services.sh").read_text(encoding="utf-8")
        enable_lines = [
            line.strip() for line in raw.splitlines()
            if "systemctl enable" in line and not line.strip().startswith("#")
        ]
        assert any("us-stock-trading-shadow.timer" in line for line in enable_lines)
        assert any("us-stock-trading-reconcile.timer" in line for line in enable_lines)
        # The live unit is installed but must never be enabled here.
        assert not any("us-stock-trading-live.service" in line for line in enable_lines)
        assert "systemctl disable us-stock-trading-live.service" in raw

    def test_installer_sets_root_trading_0640_on_the_env_file(self):
        raw = (SCRIPTS_DIR / "install_oracle_services.sh").read_text(encoding="utf-8")
        assert 'chmod 0640 "${ENV_FILE}"' in raw
        assert 'chown root:"${SERVICE_GROUP}" "${ENV_FILE}"' in raw


READONLY_ENV = {
    "EXECUTION_BROKER": "kis",
    "KIS_ENV": "live",
    "KIS_APP_KEY": "fake-key",
    "KIS_APP_SECRET": "fake-secret",
    "KIS_ACCOUNT_NO": "12345678",
    "KIS_ACCOUNT_PRODUCT_CD": "01",
    "KIS_ALLOWED_ACCOUNT_NO": "12345678",
    "KIS_ACCOUNT_READ_ENABLED": "true",
    "KIS_LIVE_ORDER_ENABLED": "false",
    "ALPACA_ORDER_ENABLED": "false",
    "ALPACA_PAPER_ORDER_ENABLED": "false",
    "LIVE_ROLLOUT_ENABLED": "false",
    "ENTRY_DISABLED": "true",
    "LIVE_ENABLE_PARTIAL_PROFIT": "false",
    "LIVE_ENABLE_TRAILING_STOP": "false",
    "LIVE_ENABLE_TIME_STOP": "false",
    "LIVE_ENABLE_EOD_EXIT": "false",
}


@pytest.fixture
def preflight_env(tmp_path, monkeypatch):
    import subprocess as sp

    head = sp.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True,
                  text=True, check=True).stdout.strip()
    env = dict(READONLY_ENV)
    env["VALIDATED_COMMIT"] = head
    env["DEPLOYED_COMMIT"] = head
    env["TRADING_LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECON.json"))
    monkeypatch.setenv("KIS_ACCOUNT_ALIAS", "kis-test")
    from execution import idempotency
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    return env


def _run_preflight(env):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import importlib

        module = importlib.import_module("preflight_kis_live")
        importlib.reload(module)
        return module.run_preflight(env=env)
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


class TestPreflight:
    def test_readonly_posture_passes(self, preflight_env):
        results = _run_preflight(preflight_env)
        assert results.failures == [], results.render()

    @pytest.mark.parametrize("flag", [
        "KIS_LIVE_ORDER_ENABLED", "ALPACA_ORDER_ENABLED", "LIVE_ROLLOUT_ENABLED",
        "LIVE_ENABLE_PARTIAL_PROFIT", "LIVE_ENABLE_TRAILING_STOP",
        "LIVE_ENABLE_TIME_STOP", "LIVE_ENABLE_EOD_EXIT",
    ])
    def test_any_enabled_order_flag_fails(self, preflight_env, flag):
        env = dict(preflight_env)
        env[flag] = "true"
        results = _run_preflight(env)
        assert results.failures, f"{flag}=true should have failed preflight"

    def test_entry_not_disabled_fails(self, preflight_env):
        env = dict(preflight_env)
        env["ENTRY_DISABLED"] = "false"
        results = _run_preflight(env)
        assert any(name == "entry_disabled" for name, _, _ in results.failures)

    def test_live_flag_true_with_entry_disabled_true_is_detected(self, preflight_env):
        env = dict(preflight_env)
        env["KIS_LIVE_ORDER_ENABLED"] = "true"
        env["ENTRY_DISABLED"] = "true"
        results = _run_preflight(env)
        assert any(name == "flag_consistency" for name, _, _ in results.failures)

    def test_commit_mismatch_is_detected(self, preflight_env):
        env = dict(preflight_env)
        env["DEPLOYED_COMMIT"] = "0" * 40
        results = _run_preflight(env)
        assert any(name == "commit_match" for name, _, _ in results.failures)

    def test_missing_required_variable_is_detected(self, preflight_env):
        env = dict(preflight_env)
        del env["KIS_APP_KEY"]
        results = _run_preflight(env)
        assert any(name == "required_env" for name, _, _ in results.failures)

    def test_preflight_never_prints_a_secret(self, preflight_env):
        env = dict(preflight_env)
        env["KIS_APP_SECRET"] = "SUPERSECRETVALUE12345"
        env["KIS_ACCOUNT_NO"] = "98765432"
        env["KIS_ALLOWED_ACCOUNT_NO"] = "98765432"
        rendered = _run_preflight(env).render()
        assert "SUPERSECRETVALUE12345" not in rendered
        assert "98765432" not in rendered

    def test_cli_exit_code_is_nonzero_when_unsafe(self, tmp_path):
        import os

        env = dict(os.environ)
        env.update(READONLY_ENV)
        env["KIS_LIVE_ORDER_ENABLED"] = "true"
        env["VALIDATED_COMMIT"] = "abc"
        env["DEPLOYED_COMMIT"] = "abc"
        env["STATE_STORE_DB_FILE"] = str(tmp_path / "TEST_STATE.db")
        env["TRADING_LOG_DIR"] = str(tmp_path / "logs")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "preflight_kis_live.py")],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
        )
        assert result.returncode != 0
        assert "PREFLIGHT FAILED" in result.stdout


class TestRunbookMatchesTheRepository:
    def test_runbook_exists(self):
        assert RUNBOOK.is_file()

    def test_every_repo_path_the_runbook_names_exists(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        referenced = set(re.findall(r"(?:scripts|deploy)/[A-Za-z0-9_./-]+", text))
        assert referenced, "the runbook names no scripts/ or deploy/ path at all"
        missing = [path for path in sorted(referenced) if not (REPO_ROOT / path).exists()]
        assert missing == [], f"runbook references paths that do not exist: {missing}"

    def test_runbook_documents_the_service_lifecycle_commands(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for required in [
            "install_oracle_services.sh",
            "preflight_kis_live.py",
            "us-stock-trading-shadow",
            "us-stock-trading-reconcile",
            "us-stock-trading-live",
            "journalctl",
            "systemctl is-enabled",
        ]:
            assert required in text, f"runbook does not mention {required!r}"
