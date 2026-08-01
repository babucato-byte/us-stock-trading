"""CODEX-060: single_run_lock() owns its lock file's whole lifecycle.

The lock was acquired correctly but the file was never removed, so every
process that took it -- including `scripts/preflight_kis_live.py`, which
runs with cwd=<repo root> -- left `KIS_ORDER_IDEMPOTENCY.lock` behind in
the repository.

The fix is a lifecycle, not a cleanup: the path is resolved per call (so a
subprocess can be pointed somewhere else), removal is conditional on this
context actually having ACQUIRED the lock, and it is conditional again on
the path still naming the very file we locked. Exclusion itself is still
decided by the kernel's flock, never by the file's existence -- a stale
file left by a killed process must not block the next run.
"""
import fcntl
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from execution import idempotency
from execution.idempotency import IdempotencyError

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
ROOT_LOCK = REPO_ROOT / "KIS_ORDER_IDEMPOTENCY.lock"

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


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    monkeypatch.delenv(idempotency.LOCK_FILE_ENV_VAR, raising=False)
    yield


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "KIS_ORDER_IDEMPOTENCY.lock"


# --------------------------------------------------------------- path resolution


class TestLockPathResolution:
    def test_the_default_is_the_unchanged_operational_path(self, monkeypatch):
        monkeypatch.setattr(idempotency, "_LOCK_FILE", idempotency.DEFAULT_LOCK_FILE)
        assert idempotency.get_single_run_lock_file() == ROOT_LOCK

    def test_the_environment_variable_wins(self, tmp_path, monkeypatch):
        target = tmp_path / "elsewhere" / "run.lock"
        monkeypatch.setenv(idempotency.LOCK_FILE_ENV_VAR, str(target))
        assert idempotency.get_single_run_lock_file() == target.resolve()

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_a_blank_value_falls_back_to_the_default(self, blank, tmp_path, monkeypatch):
        monkeypatch.setenv(idempotency.LOCK_FILE_ENV_VAR, blank)
        assert idempotency.get_single_run_lock_file() == tmp_path / "KIS_ORDER_IDEMPOTENCY.lock"

    def test_a_relative_value_resolves_against_the_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(idempotency.LOCK_FILE_ENV_VAR, "sub/run.lock")
        assert idempotency.get_single_run_lock_file() == (tmp_path / "sub" / "run.lock").resolve()

    def test_a_missing_parent_directory_is_created(self, tmp_path, monkeypatch):
        target = tmp_path / "deep" / "nested" / "run.lock"
        monkeypatch.setenv(idempotency.LOCK_FILE_ENV_VAR, str(target))
        with idempotency.single_run_lock():
            assert target.exists()
        assert not target.exists()

    def test_an_unusable_parent_fails_clearly(self, tmp_path, monkeypatch):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv(idempotency.LOCK_FILE_ENV_VAR, str(blocker / "run.lock"))
        with pytest.raises(IdempotencyError) as excinfo:
            with idempotency.single_run_lock():
                pass
        assert "single-run lock" in str(excinfo.value)


# ------------------------------------------------------------------- lifecycle


class TestLockFileLifecycle:
    def test_control_the_file_really_exists_while_held(self, lock_path):
        """Without this the cleanup assertions below would pass even if the
        lock file were never created at all."""
        with idempotency.single_run_lock():
            assert lock_path.exists()

    def test_normal_exit_removes_it(self, lock_path):
        with idempotency.single_run_lock():
            pass
        assert not lock_path.exists()

    def test_an_exception_removes_it_and_is_preserved(self, lock_path):
        sentinel = RuntimeError("boom")
        with pytest.raises(RuntimeError) as excinfo:
            with idempotency.single_run_lock():
                assert lock_path.exists()
                raise sentinel
        assert excinfo.value is sentinel
        assert not lock_path.exists()

    def test_keyboard_interrupt_removes_it(self, lock_path):
        with pytest.raises(KeyboardInterrupt):
            with idempotency.single_run_lock():
                raise KeyboardInterrupt
        assert not lock_path.exists()

    def test_system_exit_removes_it(self, lock_path):
        with pytest.raises(SystemExit):
            with idempotency.single_run_lock():
                raise SystemExit(2)
        assert not lock_path.exists()

    def test_repeated_use_leaves_nothing_behind(self, lock_path):
        for _ in range(5):
            with idempotency.single_run_lock():
                pass
            assert not lock_path.exists()

    def test_a_failed_acquisition_does_not_delete_the_holders_file(self, lock_path):
        """The one case where removal would be actively destructive."""
        holder = open(lock_path, "a+")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            with pytest.raises(IdempotencyError):
                with idempotency.single_run_lock(timeout=0.2):
                    pytest.fail("the lock must not have been granted")
            assert lock_path.exists(), "the holder's lock file was deleted by a blocked waiter"
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()


# ----------------------------------------------------------------- stale files


class TestStaleLockFiles:
    def test_a_stale_file_with_no_holder_does_not_block(self, lock_path):
        """A SIGKILL leaves the path behind but no flock. Existence is not
        evidence of a running instance."""
        lock_path.write_text("", encoding="utf-8")
        stale_identity = os.stat(lock_path).st_ino
        with idempotency.single_run_lock(timeout=0.5):
            assert os.stat(lock_path).st_ino == stale_identity, "the stale file was re-locked in place"
        assert not lock_path.exists(), "the stale file was not cleaned up on the way out"

    def test_a_killed_holder_releases_the_lock_to_the_next_run(self, tmp_path, lock_path):
        """Not a mock: a real child takes the flock and is SIGKILLed, so no
        finally block ever runs."""
        child = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(
                """
                import fcntl, sys, time
                fh = open(sys.argv[1], "a+")
                fcntl.flock(fh, fcntl.LOCK_EX)
                print("HELD", flush=True)
                time.sleep(120)
                """
            ), str(lock_path)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert child.stdout.readline().strip() == "HELD"
            with pytest.raises(IdempotencyError):
                with idempotency.single_run_lock(timeout=0.2):
                    pytest.fail("a live holder must block us")
        finally:
            child.kill()
            child.wait(timeout=10)

        assert lock_path.exists(), "the killed process left its file behind, as expected"
        with idempotency.single_run_lock(timeout=2.0):
            pass
        assert not lock_path.exists()


# -------------------------------------------------------------------- identity


class TestOwnershipIdentity:
    def test_a_replaced_lock_file_is_not_deleted(self, lock_path, caplog):
        """If the path comes to name a DIFFERENT file while we hold ours,
        that file belongs to somebody else. Warn, never delete."""
        with caplog.at_level("WARNING"):
            with idempotency.single_run_lock():
                os.unlink(lock_path)
                replacement = open(lock_path, "a+")
                replacement_ino = os.stat(lock_path).st_ino
                replacement.close()
        assert lock_path.exists(), "another process's lock file was deleted"
        assert os.stat(lock_path).st_ino == replacement_ino
        assert any("replaced by a different file" in r.message for r in caplog.records)
        lock_path.unlink()

    def test_a_lock_file_removed_by_someone_else_is_not_an_error(self, lock_path):
        with idempotency.single_run_lock():
            os.unlink(lock_path)
        assert not lock_path.exists()


# ----------------------------------------------------------- real two-process


_HOLDER = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    os.environ["TRADING_SINGLE_RUN_LOCK_FILE"] = sys.argv[2]
    from execution import idempotency
    with idempotency.single_run_lock(timeout=5.0):
        print("HELD", flush=True)
        sys.stdin.readline()
    print("RELEASED", flush=True)
    """
)

_TAKER = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, sys.argv[1])
    os.environ["TRADING_SINGLE_RUN_LOCK_FILE"] = sys.argv[2]
    from execution import idempotency
    try:
        with idempotency.single_run_lock(timeout=float(sys.argv[3])):
            print("ACQUIRED", flush=True)
    except idempotency.IdempotencyError:
        print("BLOCKED", flush=True)
    """
)


class TestRealConcurrentProcesses:
    def _taker(self, lock_file, timeout):
        return subprocess.run(
            [sys.executable, "-c", _TAKER, str(REPO_ROOT), str(lock_file), str(timeout)],
            capture_output=True, text=True, timeout=60,
        )

    def test_exclusion_ownership_and_cleanup_across_processes(self, tmp_path):
        lock_file = tmp_path / "cross-process.lock"

        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(REPO_ROOT), str(lock_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "HELD"
            held_identity = os.stat(lock_file).st_ino

            # B is refused while A holds it...
            blocked = self._taker(lock_file, 0.3)
            assert "BLOCKED" in blocked.stdout, blocked.stdout + blocked.stderr[-400:]
            # ...and did not take A's file with it.
            assert lock_file.exists(), "a blocked process deleted the holder's lock file"
            assert os.stat(lock_file).st_ino == held_identity

            holder.stdin.write("\n")
            holder.stdin.flush()
            assert holder.stdout.readline().strip() == "RELEASED"
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)

        # A cleaned up after itself, and C can still run.
        assert not lock_file.exists(), "the holder did not remove its lock file on exit"
        acquired = self._taker(lock_file, 5.0)
        assert "ACQUIRED" in acquired.stdout, acquired.stdout + acquired.stderr[-400:]
        assert not lock_file.exists()

    def test_only_one_of_many_concurrent_processes_gets_in(self, tmp_path):
        """Removal-on-exit must not open a window where two processes both
        believe they hold the lock."""
        lock_file = tmp_path / "contended.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(REPO_ROOT), str(lock_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "HELD"
            results = [self._taker(lock_file, 0.3).stdout.strip() for _ in range(4)]
            assert results == ["BLOCKED"] * 4, results
        finally:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.stdin.close()
            holder.wait(timeout=30)
        assert not lock_file.exists()


# ------------------------------------------------------------------- preflight


def _preflight_env(tmp_path, *, commit):
    env = dict(os.environ)
    env.update(READONLY_ENV)
    env["VALIDATED_COMMIT"] = commit
    env["DEPLOYED_COMMIT"] = commit
    env["TRADING_LOG_DIR"] = str(tmp_path / "logs")
    env["STATE_STORE_DB_FILE"] = str(tmp_path / "TEST_STATE.db")
    env["RECONCILIATION_STATE_FILE"] = str(tmp_path / "RECON.json")
    env["KIS_ACCOUNT_ALIAS"] = "kis-test"
    env["TRADING_SINGLE_RUN_LOCK_FILE"] = str(tmp_path / "preflight-single-run.lock")
    return env


def _head_commit():
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True,
        check=True,
    ).stdout.strip()


class TestPreflightLeavesNoLockBehind:
    """The originally reported symptom, checked end to end through the
    real script in a real child process."""

    def _run(self, env):
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "preflight_kis_live.py")],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120,
        )

    def test_a_passing_preflight_leaves_no_lock(self, tmp_path):
        root_lock_before = ROOT_LOCK.exists()
        env = _preflight_env(tmp_path, commit=_head_commit())
        result = self._run(env)
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-800:]
        assert "single_run_lock" in result.stdout, "the lock check did not even run"
        assert not Path(env["TRADING_SINGLE_RUN_LOCK_FILE"]).exists()
        assert ROOT_LOCK.exists() is root_lock_before, (
            "preflight left a lock file in the repository root"
        )

    def test_a_failing_preflight_leaves_no_lock(self, tmp_path):
        root_lock_before = ROOT_LOCK.exists()
        env = _preflight_env(tmp_path, commit=_head_commit())
        env["DEPLOYED_COMMIT"] = "0" * 40  # a commit that is not the validated one
        result = self._run(env)
        assert result.returncode != 0, result.stdout[-2000:]
        assert "PREFLIGHT FAILED" in result.stdout
        assert not Path(env["TRADING_SINGLE_RUN_LOCK_FILE"]).exists()
        assert ROOT_LOCK.exists() is root_lock_before, (
            "a failing preflight left a lock file in the repository root"
        )

    def test_preflight_uses_the_lock_path_it_is_given(self, tmp_path):
        """Proves the isolation above is real -- without the variable the
        script would fall back to the repository root."""
        source = (SCRIPTS_DIR / "preflight_kis_live.py").read_text(encoding="utf-8")
        assert "single_run_lock" in source
        assert idempotency.LOCK_FILE_ENV_VAR not in source, (
            "the script must not special-case the variable itself -- resolution "
            "belongs in idempotency.get_single_run_lock_file()"
        )
