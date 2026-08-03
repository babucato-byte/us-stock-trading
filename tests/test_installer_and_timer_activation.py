"""Installing the units and arming a timer are separate programs.

Codex found two defects in the previous attempt at this:

  HIGH-1  the live unit correctly reports `static` (no [Install] section,
          so `systemctl enable` cannot create a boot symlink) -- and the
          installer treated `static` as a FAILURE, so it exited 1 on
          every real install.
  HIGH-2  that assertion ran AFTER `enable --now` on four timers, so the
          failing installer still left the Shadow timer armed and
          running while telling the operator the install had failed.

Both are exercised here against the installer's real control flow, with
`systemctl` replaced by tests/fake_systemctl.py -- whose responses were
measured on the Oracle host, not invented. A string search of the script
is what let HIGH-1 through in the first place.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install_oracle_services.sh"
ACTIVATOR = REPO_ROOT / "scripts" / "enable_oracle_shadow_timer.sh"
FAKE_SYSTEMCTL = Path(__file__).resolve().parent / "fake_systemctl.py"
UNIT_DIR = REPO_ROOT / "deploy" / "systemd"

LIVE_UNIT = "us-stock-trading-live.service"
TIMERS = (
    "us-stock-trading-reconcile.timer",
    "us-stock-trading-shadow.timer",
    "us-stock-trading-shadow-exit.timer",
    "us-stock-trading-health.timer",
)

def _repo_head():
    """The activation script checks the release checkout's HEAD against
    DEPLOYED_COMMIT, so the fixture env file has to name this one."""
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              timeout=60).stdout.strip() or "abc123"
    except Exception:                                    # noqa: BLE001
        return "abc123"


HEAD_COMMIT = _repo_head()

SAFE_ENV_FILE = (
    "KIS_LIVE_ORDER_ENABLED=false\n"
    "LIVE_ROLLOUT_ENABLED=false\n"
    "ALPACA_ORDER_ENABLED=false\n"
    "ENTRY_DISABLED=true\n"
    f"DEPLOYED_COMMIT={HEAD_COMMIT}\n"
    f"VALIDATED_COMMIT={HEAD_COMMIT}\n"
)


@pytest.fixture
def sandbox(tmp_path):
    """A fake host: unit dir, env file, shared dirs, stub systemctl."""
    host = tmp_path / "host"
    for sub in ("units", "etc", "logs", "shared/state", "shared/logs"):
        (host / sub).mkdir(parents=True, exist_ok=True)
    (host / "shared/state").chmod(0o700)
    env_file = host / "etc" / "live-readonly.env"
    env_file.write_text(SAFE_ENV_FILE, encoding="utf-8")

    systemctl = host / "systemctl"
    systemctl.write_text(
        f"#!/bin/sh\nexec {sys.executable} {FAKE_SYSTEMCTL} \"$@\"\n", encoding="utf-8")
    systemctl.chmod(0o755)

    analyze = host / "systemd-analyze"
    analyze.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    analyze.chmod(0o755)

    # `install -o/-g` needs privileges we do not have; a stub keeps the
    # real control flow while only the ownership change is neutralised.
    bin_dir = host / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("install", '#!/bin/sh\nargs=""\nwhile [ $# -gt 0 ]; do\n'
                    '  case "$1" in -o|-g) shift 2;; *) args="$args $1"; shift;; esac\n'
                    'done\nexec /usr/bin/install $args\n'),
        ("chown", "#!/bin/sh\nexit 0\n"),
        ("groupadd", "#!/bin/sh\nexit 0\n"),
        ("usermod", "#!/bin/sh\nexit 0\n"),
        ("getent", "#!/bin/sh\nexit 0\n"),
        ("stat", f'#!/bin/sh\n'
                 f'if [ "$1" = "-c" ]; then\n'
                 f'  case "$2" in\n'
                 f'    "%a") echo 700;;\n'
                 f'    "%U:%G") echo "$(id -un):$(id -un)";;\n'
                 f'  esac\nfi\n'),
    ):
        script = bin_dir / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    # migrations/preflight are their own tested programs; here they are
    # a stub so the installer's ORDERING is what is under test. The
    # "preflight fails -> nothing is installed" property gets its own
    # test below, driven through this same stub.
    python_stub = host / "python-stub"
    python_stub.write_text(
        "#!/bin/sh\n"
        'if [ -n "${STUB_PYTHON_FAIL:-}" ]; then\n'
        '  echo "PREFLIGHT FAILED (stub)" >&2\n  exit 1\nfi\nexit 0\n',
        encoding="utf-8")
    python_stub.chmod(0o755)

    return {
        "root": host,
        "python": python_stub,
        "units": host / "units",
        "env_file": env_file,
        "shared": host / "shared",
        "state": host / "shared" / "state",
        "systemctl": systemctl,
        "analyze": analyze,
        "state_json": host / "systemctl-state.json",
        "bin": bin_dir,
    }


def _env(sandbox, **extra):
    env = {
        **os.environ,
        "PATH": f"{sandbox['bin']}:{os.environ['PATH']}",
        "TRADING_RELEASE_ROOT": str(REPO_ROOT),
        "TRADING_SHARED_ROOT": str(sandbox["shared"]),
        "ENV_DIR": str(sandbox["env_file"].parent),
        "ENV_FILE": str(sandbox["env_file"]),
        "LOG_DIR": str(sandbox["root"] / "logs"),
        "UNIT_DIR": str(sandbox["units"]),
        "PYTHON_BIN": str(sandbox["python"]),
        "SYSTEMCTL_BIN": str(sandbox["systemctl"]),
        "SYSTEMD_ANALYZE_BIN": str(sandbox["analyze"]),
        "SERVICE_USER": os.environ.get("USER", "ubuntu"),
        "FAKE_SYSTEMCTL_STATE": str(sandbox["state_json"]),
        "FAKE_SYSTEMCTL_UNIT_DIR": str(sandbox["units"]),
    }
    env.update(extra)
    return env


def run_installer(sandbox, **extra):
    return subprocess.run(["bash", str(INSTALLER)], env=_env(sandbox, **extra),
                          capture_output=True, text=True, timeout=300)


def run_activator(sandbox, **extra):
    return subprocess.run(["bash", str(ACTIVATOR)], env=_env(sandbox, **extra),
                          capture_output=True, text=True, timeout=300)


def systemctl_state(sandbox):
    if not sandbox["state_json"].exists():
        return {"enabled": {}, "active": {}, "calls": []}
    return json.loads(sandbox["state_json"].read_text())


# =====================================================================
# The live unit's real systemd semantics.
# =====================================================================

class TestLiveUnitEnableability:
    def _sandbox_query(self, tmp_path, unit_text, verb, extra=()):
        root = tmp_path / "sysroot"
        (root / "etc/systemd/system").mkdir(parents=True)
        (root / "etc/systemd/system" / LIVE_UNIT).write_text(unit_text, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(FAKE_SYSTEMCTL), f"--root={root}", verb, LIVE_UNIT,
             *extra],
            capture_output=True, text=True,
            env={**os.environ, "FAKE_SYSTEMCTL_STATE": ""})
        links = list(root.rglob("*.wants/*"))
        return result, links

    WITHOUT_INSTALL = (
        "[Unit]\nDescription=x\n[Service]\nType=oneshot\nExecStart=/bin/true\n")
    WITH_INSTALL = WITHOUT_INSTALL + "[Install]\nWantedBy=multi-user.target\n"

    def test_no_install_section_reports_static(self, tmp_path):
        result, _ = self._sandbox_query(tmp_path, self.WITHOUT_INSTALL, "is-enabled")
        assert result.stdout.strip() == "static"
        assert result.returncode == 0

    def test_no_install_section_creates_no_symlink(self, tmp_path):
        _, links = self._sandbox_query(tmp_path, self.WITHOUT_INSTALL, "enable")
        assert links == [], links

    def test_control_an_install_section_reports_disabled(self, tmp_path):
        """The control: without this, "static" could mean anything."""
        result, _ = self._sandbox_query(tmp_path, self.WITH_INSTALL, "is-enabled")
        assert result.stdout.strip() == "disabled"

    def test_control_an_install_section_does_create_a_symlink(self, tmp_path):
        _, links = self._sandbox_query(tmp_path, self.WITH_INSTALL, "enable")
        assert len(links) == 1, links

    def test_the_shipped_live_unit_has_no_install_section(self):
        text = (UNIT_DIR / LIVE_UNIT).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert stripped != "[Install]"
            assert not stripped.startswith(("WantedBy=", "RequiredBy=",
                                            "Alias=", "Also="))


# =====================================================================
# HIGH-1: static must PASS, disabled must FAIL.
# =====================================================================

class TestInstallerAcceptsStaticAndRejectsDisabled:
    def test_a_normal_install_succeeds(self, sandbox):
        result = run_installer(sandbox)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "is-enabled=static" in result.stdout

    def test_the_installer_reports_static_as_the_expected_state(self, sandbox):
        result = run_installer(sandbox)
        assert "not enableable" in result.stdout or "0 symlinks" in result.stdout
        assert "ERROR" not in result.stderr, result.stderr

    def test_an_install_section_on_the_live_unit_fails_the_install(self, sandbox,
                                                                    tmp_path):
        """The regression that matters most: if [Install] ever comes
        back, the installer must refuse -- `disabled` is NOT a pass."""
        release = tmp_path / "release"
        shutil.copytree(REPO_ROOT, release, symlinks=True, ignore=shutil.ignore_patterns(
            ".git", "venv", "__pycache__", "*.pyc"))
        unit = release / "deploy" / "systemd" / LIVE_UNIT
        unit.write_text(unit.read_text(encoding="utf-8")
                        + "\n[Install]\nWantedBy=multi-user.target\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(release / "scripts" / "install_oracle_services.sh")],
            env=_env(sandbox, TRADING_RELEASE_ROOT=str(release)),
            capture_output=True, text=True, timeout=300)

        assert result.returncode == 1, result.stdout
        assert "[Install]" in result.stderr or "enableable" in result.stderr

    def test_the_install_section_check_runs_before_anything_is_installed(
            self, sandbox, tmp_path):
        release = tmp_path / "release2"
        shutil.copytree(REPO_ROOT, release, symlinks=True, ignore=shutil.ignore_patterns(
            ".git", "venv", "__pycache__", "*.pyc"))
        unit = release / "deploy" / "systemd" / LIVE_UNIT
        unit.write_text(unit.read_text(encoding="utf-8")
                        + "\n[Install]\nWantedBy=multi-user.target\n", encoding="utf-8")
        subprocess.run(
            ["bash", str(release / "scripts" / "install_oracle_services.sh")],
            env=_env(sandbox, TRADING_RELEASE_ROOT=str(release)),
            capture_output=True, text=True, timeout=300)
        assert list(sandbox["units"].glob("*")) == [], "units were installed anyway"


# =====================================================================
# HIGH-2: nothing is enabled, ever, by the installer.
# =====================================================================

class TestInstallerEnablesNothing:
    def test_no_enable_or_start_command_is_issued(self, sandbox):
        run_installer(sandbox)
        calls = systemctl_state(sandbox)["calls"]
        offenders = [c for c in calls
                     if c.startswith("enable ") or c.startswith("start ")]
        assert offenders == [], offenders

    def test_every_timer_ends_disabled_and_inactive(self, sandbox):
        result = run_installer(sandbox)
        assert result.returncode == 0, result.stdout + result.stderr
        state = systemctl_state(sandbox)
        for timer in TIMERS:
            assert not state["enabled"].get(timer), timer
            assert state["active"].get(timer, "inactive") != "active", timer
            assert f"{timer}: is-enabled=disabled" in result.stdout

    def test_the_live_unit_ends_static_and_inactive(self, sandbox):
        result = run_installer(sandbox)
        assert f"{LIVE_UNIT}: is-enabled=static is-active=inactive" in result.stdout

    def test_the_installer_names_the_separate_activation_step(self, sandbox):
        result = run_installer(sandbox)
        assert "enable_oracle_shadow_timer.sh" in result.stdout

    def test_a_late_failure_still_leaves_every_timer_off(self, sandbox):
        """HIGH-2 head on: a timer that was already running and cannot be
        stopped fails the FINAL check -- and the installer must not have
        armed anything on the way there."""
        sandbox["state_json"].write_text(json.dumps(
            {"enabled": {}, "active": {"us-stock-trading-shadow.timer": "active"},
             "calls": []}))
        result = run_installer(
            sandbox, FAKE_SYSTEMCTL_FAIL="stop:us-stock-trading-shadow.timer")
        assert result.returncode != 0, result.stdout
        state = systemctl_state(sandbox)
        for timer in TIMERS:
            assert not state["enabled"].get(timer), timer
        assert not [c for c in state["calls"] if c.startswith("enable ")], state["calls"]

    def test_no_enable_command_precedes_a_failure(self, sandbox):
        sandbox["state_json"].write_text(json.dumps(
            {"enabled": {}, "active": {"us-stock-trading-shadow.timer": "active"},
             "calls": []}))
        run_installer(sandbox, FAKE_SYSTEMCTL_FAIL="stop:us-stock-trading-shadow.timer")
        calls = systemctl_state(sandbox)["calls"]
        assert not [c for c in calls if c.startswith("enable ")], calls


# =====================================================================
# Ordering: verification before mutation.
# =====================================================================

class TestVerificationPrecedesInstallation:
    def test_a_bad_env_file_installs_nothing(self, sandbox):
        sandbox["env_file"].write_text(
            "KIS_LIVE_ORDER_ENABLED=true\nENTRY_DISABLED=true\n", encoding="utf-8")
        result = run_installer(sandbox)
        assert result.returncode == 1
        assert list(sandbox["units"].glob("*")) == []

    def test_a_missing_entry_disabled_installs_nothing(self, sandbox):
        sandbox["env_file"].write_text(
            "KIS_LIVE_ORDER_ENABLED=false\nENTRY_DISABLED=false\n", encoding="utf-8")
        result = run_installer(sandbox)
        assert result.returncode == 1
        assert list(sandbox["units"].glob("*")) == []

    def test_a_failing_preflight_installs_nothing(self, sandbox):
        """Verification order: preflight runs before any unit file is
        copied, so a refused posture leaves the host untouched."""
        result = run_installer(sandbox, STUB_PYTHON_FAIL="1")
        assert result.returncode != 0
        assert list(sandbox["units"].glob("*")) == []
        assert not systemctl_state(sandbox)["calls"]

    def test_a_missing_systemd_analyze_refuses_to_install(self, sandbox):
        result = run_installer(sandbox, SYSTEMD_ANALYZE_BIN="/nonexistent/analyze")
        assert result.returncode == 1
        assert "refusing to install unverified units" in result.stderr
        assert list(sandbox["units"].glob("*")) == []

    def test_the_successful_install_did_place_the_units(self, sandbox):
        run_installer(sandbox)
        installed = sorted(p.name for p in sandbox["units"].glob("*"))
        assert len(installed) == 10, installed
        assert LIVE_UNIT in installed


# =====================================================================
# DRY_RUN plan.
# =====================================================================

class TestDryRunPlan:
    def test_the_plan_contains_the_required_steps(self, sandbox):
        result = run_installer(sandbox, DRY_RUN="1")
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert "-m 0700" in out and "shared/state" in out
        assert "would verify" in out and "0700" in out
        assert "placeholders : none remaining" in out
        assert "systemd-analyze verify clean" in out
        assert "is-enabled=static" in out
        assert f"disable {LIVE_UNIT}" in out
        for timer in TIMERS:
            assert f"disable {timer}" in out

    def test_the_plan_never_enables_or_starts(self, sandbox):
        result = run_installer(sandbox, DRY_RUN="1")
        for line in result.stdout.splitlines():
            if not line.startswith("DRY_RUN: "):
                continue
            command = line[len("DRY_RUN: "):]
            assert " enable " not in f" {command} ", command
            assert not command.endswith(" start"), command
            assert "enable --now" not in command, command

    def test_a_dry_run_touches_no_unit_directory(self, sandbox):
        run_installer(sandbox, DRY_RUN="1")
        assert list(sandbox["units"].glob("*")) == []

    def test_the_dry_run_still_really_verifies_the_live_unit(self, sandbox, tmp_path):
        """The plan is not a separate code path: the sandbox check runs
        for real even in DRY_RUN, so a reintroduced [Install] is caught."""
        release = tmp_path / "release3"
        shutil.copytree(REPO_ROOT, release, symlinks=True, ignore=shutil.ignore_patterns(
            ".git", "venv", "__pycache__", "*.pyc"))
        unit = release / "deploy" / "systemd" / LIVE_UNIT
        unit.write_text(unit.read_text(encoding="utf-8")
                        + "\n[Install]\nWantedBy=multi-user.target\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(release / "scripts" / "install_oracle_services.sh")],
            env=_env(sandbox, TRADING_RELEASE_ROOT=str(release), DRY_RUN="1"),
            capture_output=True, text=True, timeout=300)
        assert result.returncode == 1, result.stdout


# =====================================================================
# The separate activation script.
# =====================================================================

class TestShadowTimerActivation:
    def _install_first(self, sandbox):
        result = run_installer(sandbox)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_it_refuses_without_the_approval_variable(self, sandbox):
        self._install_first(sandbox)
        result = run_activator(sandbox)
        assert result.returncode == 1
        assert "ALLOW_SHADOW_TIMER_ENABLE" in result.stderr
        state = systemctl_state(sandbox)
        assert not state["enabled"].get("us-stock-trading-shadow.timer")
        assert state["active"].get("us-stock-trading-shadow.timer", "inactive") != "active"

    @pytest.mark.parametrize("value", ["", "1", "yes", "TRUE", "true ", "false"])
    def test_only_the_exact_string_true_is_accepted(self, sandbox, value):
        self._install_first(sandbox)
        result = run_activator(sandbox, ALLOW_SHADOW_TIMER_ENABLE=value)
        assert result.returncode == 1, value
        assert not systemctl_state(sandbox)["enabled"].get("us-stock-trading-shadow.timer")

    def test_it_refuses_when_an_order_flag_is_on(self, sandbox):
        self._install_first(sandbox)
        sandbox["env_file"].write_text(
            SAFE_ENV_FILE.replace("KIS_LIVE_ORDER_ENABLED=false",
                                  "KIS_LIVE_ORDER_ENABLED=true"), encoding="utf-8")
        result = run_activator(sandbox, ALLOW_SHADOW_TIMER_ENABLE="true")
        assert result.returncode == 1
        assert not systemctl_state(sandbox)["enabled"].get("us-stock-trading-shadow.timer")

    def test_it_refuses_on_a_commit_mismatch(self, sandbox):
        self._install_first(sandbox)
        sandbox["env_file"].write_text(
            SAFE_ENV_FILE.replace(f"VALIDATED_COMMIT={HEAD_COMMIT}",
                                  "VALIDATED_COMMIT=def456"), encoding="utf-8")
        result = run_activator(sandbox, ALLOW_SHADOW_TIMER_ENABLE="true")
        assert result.returncode == 1
        assert "VALIDATED_COMMIT" in result.stderr

    def test_it_refuses_when_the_live_unit_is_not_static(self, sandbox):
        self._install_first(sandbox)
        state = systemctl_state(sandbox)
        state["enabled"][LIVE_UNIT] = True          # simulate a stray enable
        sandbox["state_json"].write_text(json.dumps(state))
        result = run_activator(sandbox, ALLOW_SHADOW_TIMER_ENABLE="true")
        assert result.returncode == 1
        assert LIVE_UNIT in result.stderr

    def test_it_refuses_when_shared_state_is_not_0700(self, sandbox):
        self._install_first(sandbox)
        loose = sandbox["bin"] / "stat"
        loose.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then\n'
                         '  case "$2" in "%a") echo 770;; "%U:%G") echo "root:trading";; esac\n'
                         'fi\n', encoding="utf-8")
        loose.chmod(0o755)
        result = run_activator(sandbox, ALLOW_SHADOW_TIMER_ENABLE="true")
        assert result.returncode == 1
        assert "expected 700" in result.stderr

    def test_the_dry_run_plan_enables_nothing(self, sandbox):
        self._install_first(sandbox)
        result = run_activator(sandbox, ALLOW_SHADOW_TIMER_ENABLE="true", DRY_RUN="1")
        assert result.returncode == 0, result.stderr
        assert "DRY_RUN" in result.stdout
        assert not systemctl_state(sandbox)["enabled"].get("us-stock-trading-shadow.timer")

    def test_it_targets_only_the_shadow_timer(self):
        source = ACTIVATOR.read_text(encoding="utf-8")
        assert "us-stock-trading-reconcile.timer" not in source
        assert "us-stock-trading-health.timer" not in source
        assert "us-stock-trading-shadow-exit.timer" not in source
        # It may NAME the live unit (it asserts on its state) but must
        # never enable or start it.
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "enable" in stripped or "start" in stripped:
                assert "LIVE_UNIT" not in stripped or "is-enabled" in stripped, stripped

    def test_it_cannot_arm_live_order_placement(self):
        source = ACTIVATOR.read_text(encoding="utf-8")
        assert "run_live_buy_entry" not in source
        assert 'enable "${LIVE_UNIT}"' not in source
        assert 'start "${LIVE_UNIT}"' not in source


class TestActivationIsAtomic:
    def _install_first(self, sandbox):
        assert run_installer(sandbox).returncode == 0

    def test_a_failed_start_rolls_back_to_disabled(self, sandbox):
        self._install_first(sandbox)
        result = run_activator(
            sandbox, ALLOW_SHADOW_TIMER_ENABLE="true",
            FAKE_SYSTEMCTL_FAIL="start:us-stock-trading-shadow.timer")
        assert result.returncode == 1
        state = systemctl_state(sandbox)
        assert not state["enabled"].get("us-stock-trading-shadow.timer")
        assert state["active"].get("us-stock-trading-shadow.timer", "inactive") != "active"
        assert "rolling back" in result.stderr

    def test_a_failed_enable_rolls_back(self, sandbox):
        self._install_first(sandbox)
        result = run_activator(
            sandbox, ALLOW_SHADOW_TIMER_ENABLE="true",
            FAKE_SYSTEMCTL_FAIL="enable:us-stock-trading-shadow.timer")
        assert result.returncode == 1
        state = systemctl_state(sandbox)
        assert not state["enabled"].get("us-stock-trading-shadow.timer")

    def test_the_rollback_leaves_other_units_untouched(self, sandbox):
        self._install_first(sandbox)
        run_activator(sandbox, ALLOW_SHADOW_TIMER_ENABLE="true",
                      FAKE_SYSTEMCTL_FAIL="start:us-stock-trading-shadow.timer")
        state = systemctl_state(sandbox)
        for timer in TIMERS:
            assert not state["enabled"].get(timer), timer
        assert not state["enabled"].get(LIVE_UNIT)


# =====================================================================
# The state permission work must not regress.
# =====================================================================

class TestSharedStateStillHardened:
    def test_the_installer_creates_state_as_0700_owner_only(self):
        source = INSTALLER.read_text(encoding="utf-8")
        assert 'install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${SHARED_DIR}/state"' \
            in source

    def test_state_is_not_group_writable_anywhere_in_the_installer(self):
        for line in INSTALLER.read_text(encoding="utf-8").splitlines():
            if "${SHARED_DIR}/state" in line and "install -d" in line:
                assert "0770" not in line
                assert "${SERVICE_GROUP}" not in line

    def test_logs_stay_group_shared(self):
        source = INSTALLER.read_text(encoding="utf-8")
        assert 'install -d -m 0770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${LOG_DIR}"' \
            in source
