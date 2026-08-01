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
    "us-stock-trading-migrate.service",
    "us-stock-trading-reconcile.service",
    "us-stock-trading-shadow.service",
    "us-stock-trading-shadow-exit.service",
    "us-stock-trading-health.service",
    "us-stock-trading-live.service",
]
TIMER_UNITS = [
    "us-stock-trading-reconcile.timer",
    "us-stock-trading-shadow.timer",
    "us-stock-trading-shadow-exit.timer",
    "us-stock-trading-health.timer",
]
ENTRYPOINTS = [
    "preflight_kis_live.py",
    "run_migrations.py",
    "run_reconciliation.py",
    "run_shadow_mode.py",
    "run_shadow_exit_evaluation.py",
    "run_health_report.py",
    "run_live_buy_entry.py",
]
# Units whose ExecStartPre must run the preflight. Two deliberate
# exceptions:
#   - migrate: preflight CHECKS the schema version, so gating migrations
#     behind it would be circular;
#   - health: a health report that refuses to run when the deployment is
#     unhealthy is useless precisely when it is needed.
PREFLIGHT_UNITS = [
    u for u in SERVICE_UNITS
    if u not in ("us-stock-trading-migrate.service", "us-stock-trading-health.service")
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

    @pytest.mark.parametrize("unit", PREFLIGHT_UNITS)
    def test_runs_preflight_before_start(self, unit):
        raw = (UNIT_DIR / unit).read_text(encoding="utf-8")
        assert "ExecStartPre=" in raw
        assert "preflight_kis_live.py" in raw

    @pytest.mark.parametrize("unit", SERVICE_UNITS)
    def test_additional_hardening_directives(self, unit):
        service = _parse_unit(unit)["Service"]
        assert service.get("TimeoutStartSec") == "300"
        assert service.get("UMask") == "0027"
        assert service.get("ProtectHome") is not None
        assert service.get("User") == "ubuntu"
        assert service.get("Group") == "trading"
        assert service.get("WorkingDirectory") == "/home/ubuntu/trading-release"

    @pytest.mark.parametrize("unit", SERVICE_UNITS)
    def test_read_write_paths_cover_the_data_and_log_locations(self, unit):
        paths = _parse_unit(unit)["Service"]["ReadWritePaths"].split()
        assert "/home/ubuntu/trading-release" in paths
        assert "/var/log/us-stock-trading" in paths

    @pytest.mark.parametrize("unit", SERVICE_UNITS)
    def test_preconditions_guard_missing_files(self, unit):
        raw = (UNIT_DIR / unit).read_text(encoding="utf-8")
        conditions = re.findall(r"^ConditionPathExists=(.+)$", raw, flags=re.MULTILINE)
        assert "/etc/us-stock-trading/live-readonly.env" in conditions
        assert any(c.endswith("venv/bin/python") for c in conditions)


class TestServiceOrdering:
    """migration -> preflight -> reconciliation -> shadow services."""

    @pytest.mark.parametrize("unit", [u for u in SERVICE_UNITS
                                       if u != "us-stock-trading-migrate.service"])
    def test_every_unit_requires_the_migration_unit(self, unit):
        section = _parse_unit(unit)["Unit"]
        assert "us-stock-trading-migrate.service" in section["Requires"]
        assert "us-stock-trading-migrate.service" in section["After"]

    @pytest.mark.parametrize("unit", [
        "us-stock-trading-shadow.service",
        "us-stock-trading-shadow-exit.service",
        "us-stock-trading-live.service",
    ])
    def test_shadow_and_live_units_require_reconciliation(self, unit):
        section = _parse_unit(unit)["Unit"]
        assert "us-stock-trading-reconcile.service" in section["Requires"]
        assert "us-stock-trading-reconcile.service" in section["After"]

    def test_a_failed_reconciliation_blocks_the_shadow_units(self):
        # `Requires=` is precisely systemd's "if that unit failed, do not
        # start this one" relationship, so asserting it IS the assertion
        # that a failed reconciliation blocks shadow startup.
        for unit in ("us-stock-trading-shadow.service", "us-stock-trading-shadow-exit.service"):
            assert "us-stock-trading-reconcile.service" in _parse_unit(unit)["Unit"]["Requires"]

    @pytest.mark.parametrize("timer,unit", [
        ("us-stock-trading-reconcile.timer", "us-stock-trading-reconcile.service"),
        ("us-stock-trading-shadow.timer", "us-stock-trading-shadow.service"),
        ("us-stock-trading-shadow-exit.timer", "us-stock-trading-shadow-exit.service"),
        ("us-stock-trading-health.timer", "us-stock-trading-health.service"),
    ])
    def test_each_timer_targets_its_service(self, timer, unit):
        assert _parse_unit(timer)["Timer"]["Unit"] == unit

    def test_the_live_service_has_no_timer(self):
        for timer in TIMER_UNITS:
            assert _parse_unit(timer)["Timer"]["Unit"] != "us-stock-trading-live.service"


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

    @pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
    def test_entrypoint_help_succeeds(self, entrypoint):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / entrypoint), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert "usage" in result.stdout.lower()


class TestShadowServicesCannotOrder:
    """Structural, not behavioural: a Shadow entrypoint that cannot even
    REACH the order path is a stronger guarantee than one that merely
    checks a flag before using it."""

    @pytest.mark.parametrize("entrypoint", ["run_shadow_mode.py", "run_shadow_exit_evaluation.py"])
    def test_shadow_entrypoint_does_not_import_the_execution_engine(self, entrypoint):
        import ast

        tree = ast.parse((SCRIPTS_DIR / entrypoint).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        forbidden = {
            "execution.execution_engine", "execution.execution_engine.submit_buy_order",
            "brokers.kis_broker_adapter", "kis_position_manager",
        }
        assert not (imported & forbidden), f"{entrypoint} imports {imported & forbidden}"

    @pytest.mark.parametrize("entrypoint", ["run_shadow_mode.py", "run_shadow_exit_evaluation.py"])
    def test_shadow_entrypoint_calls_no_order_submitting_method(self, entrypoint):
        source = (SCRIPTS_DIR / entrypoint).read_text(encoding="utf-8")
        code_lines = [
            line for line in source.splitlines()
            if not line.strip().startswith("#")
        ]
        # Strip the module docstring, which legitimately discusses these.
        body = "\n".join(code_lines).split('"""')[-1]
        for forbidden in ("submit_order(", "cancel_order(", "submit_buy_order(",
                          "submit_sell_order(", "check_and_manage("):
            assert forbidden not in body, f"{entrypoint} calls {forbidden}"

    def test_shadow_exit_uses_the_same_decision_function_the_live_path_uses(self):
        source = (SCRIPTS_DIR / "run_shadow_exit_evaluation.py").read_text(encoding="utf-8")
        assert "lifecycle.decide_exit(" in source, (
            "the Shadow exit service must reuse positions.lifecycle.decide_exit(), "
            "not re-implement the exit rules"
        )


class TestInstallerScript:
    def test_installer_exists_and_is_valid_bash(self):
        installer = SCRIPTS_DIR / "install_oracle_services.sh"
        assert installer.is_file()
        result = subprocess.run(["bash", "-n", str(installer)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_installer_enables_only_the_readonly_timers(self):
        raw = (SCRIPTS_DIR / "install_oracle_services.sh").read_text(encoding="utf-8")
        enable_block = re.search(r"ENABLE_TIMERS=\((.*?)\)", raw, flags=re.DOTALL)
        assert enable_block, "installer has no ENABLE_TIMERS list"
        enabled = enable_block.group(1).split()
        assert set(enabled) == set(TIMER_UNITS)
        # The live unit must never appear in anything the installer enables.
        assert "us-stock-trading-live.service" not in enabled

    def test_installer_never_enables_or_starts_the_live_service(self):
        raw = (SCRIPTS_DIR / "install_oracle_services.sh").read_text(encoding="utf-8")
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "us-stock-trading-live.service" not in stripped:
                continue
            assert "systemctl enable" not in stripped, f"installer enables the live unit: {stripped}"
            assert "systemctl start" not in stripped, f"installer starts the live unit: {stripped}"
        assert "systemctl disable us-stock-trading-live.service" in raw
        assert "systemctl stop us-stock-trading-live.service" in raw

    def test_installer_never_flips_a_live_order_flag(self):
        raw = (SCRIPTS_DIR / "install_oracle_services.sh").read_text(encoding="utf-8")
        for flag in ("KIS_LIVE_ORDER_ENABLED", "LIVE_ROLLOUT_ENABLED", "ENTRY_DISABLED",
                     "ALPACA_ORDER_ENABLED"):
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or flag not in stripped:
                    continue
                # Reading/validating the flag is fine; assigning it is not.
                assert not re.search(rf"^{flag}=", stripped), (
                    f"installer assigns {flag}: {stripped}"
                )

    def test_installer_runs_migration_and_preflight_before_enabling_anything(self):
        raw = (SCRIPTS_DIR / "install_oracle_services.sh").read_text(encoding="utf-8")
        migrate_at = raw.index("run_migrations.py")
        preflight_at = raw.rindex("preflight_kis_live.py")
        enable_at = raw.index('systemctl enable --now "${timer}"')
        assert migrate_at < enable_at
        assert preflight_at < enable_at

    def test_installer_refuses_a_live_enabled_environment_file(self):
        raw = (SCRIPTS_DIR / "install_oracle_services.sh").read_text(encoding="utf-8")
        assert "only deploys the read-only posture" in raw
        assert "ENTRY_DISABLED" in raw

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

    def test_migration_behind_the_code_fails_preflight(self, preflight_env, tmp_path, monkeypatch):
        # A database that has NOT had the migrations applied must block
        # every service from starting.
        import sqlite3

        stale = tmp_path / "STALE.db"
        conn = sqlite3.connect(str(stale))
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
            "description TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(stale))

        from state_store import db as state_db
        monkeypatch.setattr(state_db, "init_db", lambda conn, **kwargs: 0)
        results = _run_preflight(preflight_env)
        assert any(name == "db_migrations" for name, _, _ in results.failures)

    def test_missing_entrypoint_fails_preflight(self, preflight_env, monkeypatch):
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            import importlib

            module = importlib.import_module("preflight_kis_live")
            monkeypatch.setattr(
                module, "REQUIRED_ENTRYPOINTS",
                module.REQUIRED_ENTRYPOINTS + ("definitely_not_here.py",),
            )
            results = module.run_preflight(env=preflight_env)
        finally:
            sys.path.remove(str(SCRIPTS_DIR))
        assert any(name == "entrypoints_exist" for name, _, _ in results.failures)

    def test_missing_unit_file_fails_preflight(self, preflight_env, monkeypatch):
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            import importlib

            module = importlib.import_module("preflight_kis_live")
            monkeypatch.setattr(
                module, "REQUIRED_UNITS",
                module.REQUIRED_UNITS + ("us-stock-trading-nonexistent.service",),
            )
            results = module.run_preflight(env=preflight_env)
        finally:
            sys.path.remove(str(SCRIPTS_DIR))
        assert any(name == "units_exist" for name, _, _ in results.failures)


class TestCommitExactMatch:
    """CODEX-051: a deployment commit is a full 40-character lowercase hex
    SHA that names a real commit, or preflight fails. Codex reproduced a
    single character 'f' passing as an exact match."""

    def _head(self):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True,
            text=True, check=True,
        ).stdout.strip()

    def _check(self, preflight_env, validated, deployed=None):
        env = dict(preflight_env)
        env["VALIDATED_COMMIT"] = validated
        env["DEPLOYED_COMMIT"] = deployed if deployed is not None else validated
        results = _run_preflight(env)
        return [name for name, _, _ in results.failures]

    def test_full_matching_sha_passes(self, preflight_env):
        assert "commit_match" not in self._check(preflight_env, self._head())

    def test_full_but_different_sha_fails(self, preflight_env):
        assert "commit_match" in self._check(preflight_env, "0" * 40)

    def test_single_character_prefix_fails(self, preflight_env):
        # The exact value Codex reproduced as passing.
        assert "commit_match" in self._check(preflight_env, self._head()[:1])

    def test_seven_character_short_sha_fails(self, preflight_env):
        assert "commit_match" in self._check(preflight_env, self._head()[:7])

    def test_thirty_nine_characters_fails(self, preflight_env):
        assert "commit_match" in self._check(preflight_env, self._head()[:39])

    def test_forty_one_characters_fails(self, preflight_env):
        assert "commit_match" in self._check(preflight_env, self._head() + "a")

    def test_uppercase_sha_fails(self, preflight_env):
        assert "commit_match" in self._check(preflight_env, self._head().upper())

    @pytest.mark.parametrize("value", ["HEAD", "refs/heads/main", "", "  "])
    def test_non_sha_values_fail(self, preflight_env, value):
        assert "commit_match" in self._check(preflight_env, value)

    def test_whitespace_padded_sha_fails(self, preflight_env):
        assert "commit_match" in self._check(preflight_env, " " + self._head())

    def test_nonexistent_but_well_formed_sha_fails(self, preflight_env):
        assert "commit_match" in self._check(preflight_env, "0" * 39 + "1")

    def test_validated_and_deployed_agree_but_head_differs_fails(self, preflight_env):
        # Both env vars are a valid, real commit -- just not the one that
        # is actually checked out.
        parent = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=str(REPO_ROOT), capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        assert "commit_match" in self._check(preflight_env, parent)

    def test_validated_and_deployed_differ_fails(self, preflight_env):
        parent = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=str(REPO_ROOT), capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        assert "commit_match" in self._check(preflight_env, self._head(), parent)

    def test_no_prefix_comparison_remains_in_the_source(self):
        source = (SCRIPTS_DIR / "preflight_kis_live.py").read_text(encoding="utf-8")
        for banned in ("head.startswith(", "deployed.startswith(", "validated.startswith("):
            assert banned not in source, f"preflight still uses {banned}"

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
            "run_migrations.py",
            "run_reconciliation.py",
            "run_shadow_mode.py",
            "run_shadow_exit_evaluation.py",
            "run_health_report.py",
            "us-stock-trading-migrate",
            "us-stock-trading-shadow",
            "us-stock-trading-shadow-exit",
            "us-stock-trading-reconcile",
            "us-stock-trading-health",
            "us-stock-trading-live",
            "journalctl",
            "systemctl is-enabled",
            "systemctl list-timers",
            "audit_integrity_report",
        ]:
            assert required in text, f"runbook does not mention {required!r}"

    def test_runbook_covers_the_required_deployment_sequence(self):
        """The stage HEADINGS must appear in deployment order. Individual
        command names are checked for presence, not position -- the
        runbook legitimately lists a file (the unit inventory) before the
        step that runs it."""
        text = RUNBOOK.read_text(encoding="utf-8")
        ordered_headings = [
            "## 4. 백업",
            "## 5. 신규 릴리스 디렉터리 배포",
            "## 6. 별도 가상환경 준비",
            "## 7. 환경변수 설정",
            "## 8. 스키마 마이그레이션",
            "## 13. KIS 잔고·미체결 대조",
            "## 14. Shadow Mode 실행",
            "### 15.1 유닛 설치",
            "### 15.2 사전 검증",
            "### 15.3 단독 실행 확인",
            "### 15.4 시작·확인",
            "### 15.5 live 서비스가 비활성인지 확인",
            "### 15.7 정지",
            "## 16. 실주문 비활성 상태 최종 확인",
            "## 롤백 절차",
        ]
        last = -1
        for heading in ordered_headings:
            index = text.find(heading)
            assert index != -1, f"runbook is missing the {heading!r} stage"
            assert index > last, f"runbook has {heading!r} out of order"
            last = index

        for command in [
            "run_migrations.py", "preflight_kis_live.py", "run_reconciliation.py",
            "run_shadow_mode.py", "run_shadow_exit_evaluation.py", "run_health_report.py",
            "install_oracle_services.sh", "systemctl list-timers", "journalctl",
        ]:
            assert command in text, f"runbook never names {command!r}"

    def test_runbook_names_every_unit_file_that_exists(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for unit in SERVICE_UNITS + TIMER_UNITS:
            assert unit in text, f"runbook does not mention the {unit} unit"
