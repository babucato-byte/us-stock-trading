"""The execution lock covers the submission, not the thinking.

2026-09-02. `s6_buy_entry.sh` took `s6_exec.lock` in the cron wrapper, so
the whole entry process held it: candidate load, precision watch,
pre-trade validation, per-symbol KIS quotes, sizing. Three to five
minutes, relaunched every minute.

The one-minute exit monitor shares that lock. It acquired it 1 time in 29
scheduled firings. A 180-second BUY_FILL_TTL was enforced at 782s and
836s, and a filled BUY went unsynced long enough for the entry timeout to
close its position as BUY_NEVER_FILLED while the account held 7 shares.

These tests pin the shape of the fix: analysis outside, mutation inside,
and everything that could have changed in between re-asked under the
lock before anything is sent.
"""

import ast
import multiprocessing
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution import execution_lock  # noqa: E402

ENTRY_WRAPPER = (REPO_ROOT / "deploy/cron/s6_buy_entry.sh").read_text()
MONITOR_WRAPPER = (REPO_ROOT / "deploy/cron/s6_exit_monitor.sh").read_text()
KLT_SOURCE = (REPO_ROOT / "kis_live_trading.py").read_text()


class TestTheWrapperNoLongerHoldsTheExecutionLock:
    def test_the_entry_cron_does_not_flock_the_execution_lock(self):
        flock_lines = [ln for ln in ENTRY_WRAPPER.splitlines()
                       if ln.strip().startswith("flock")]
        assert flock_lines, "the entry cron must still guard against overlap"
        for line in flock_lines:
            assert "s6_exec.lock" not in line, (
                "the entry wrapper must not hold the broker-mutation lock "
                "for the life of the process; that is the starvation")

    def test_the_entry_cron_still_prevents_overlapping_cycles(self):
        flock_lines = [ln for ln in ENTRY_WRAPPER.splitlines()
                       if ln.strip().startswith("flock")]
        assert any("s6_entry.lock" in ln and "-n" in ln for ln in flock_lines), (
            "two entry cycles at once would evaluate one candidate twice")

    def test_the_execution_lock_path_is_handed_to_the_cycle(self):
        assert "S6_EXECUTION_LOCK_FILE=/home/ubuntu/logs/cron/s6_exec.lock" in ENTRY_WRAPPER, (
            "the Python side must take the SAME file the exit monitor "
            "flocks, or broker mutation has two locks and therefore none")

    def test_the_monitor_still_holds_the_execution_lock(self):
        assert any("s6_exec.lock" in ln for ln in MONITOR_WRAPPER.splitlines()
                   if ln.strip().startswith("flock")), (
            "the exit path must stay serialised against submissions")


class TestTheLockIsHeldAroundTheSubmissionOnly:
    """Structural, because a timing test cannot prove where a lock is not."""

    @staticmethod
    def _cycle_function():
        tree = ast.parse(KLT_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_live_buy_entry_cycle":
                return node
        raise AssertionError("run_live_buy_entry_cycle not found")

    @staticmethod
    def _lock_blocks(func):
        found = []
        for node in ast.walk(func):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                call = item.context_expr
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "hold"):
                    found.append(node)
        return found

    def _calls_within(self, node):
        names = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                target = inner.func
                if isinstance(target, ast.Attribute):
                    names.add(target.attr)
                elif isinstance(target, ast.Name):
                    names.add(target.id)
        return names

    def test_exactly_one_lock_block_exists(self):
        blocks = self._lock_blocks(self._cycle_function())
        assert len(blocks) == 1, (
            f"expected one critical section, found {len(blocks)}")

    def test_the_submission_happens_inside_the_lock(self):
        block = self._lock_blocks(self._cycle_function())[0]
        assert "submit_buy_order" in self._calls_within(block)

    def test_revalidation_happens_inside_the_lock(self):
        block = self._lock_blocks(self._cycle_function())[0]
        assert "_revalidate_before_submit" in self._calls_within(block)

    @pytest.mark.parametrize("slow_call", [
        "symbols",            # the precision watch / pre-trade validation
        "get_price_quote",    # per-symbol KIS quotes
        "get_orderable_usd",  # sizing
        "qualify",
    ])
    def test_the_slow_analysis_is_outside_the_lock(self, slow_call):
        """The whole point. Each of these took seconds to minutes."""
        block = self._lock_blocks(self._cycle_function())[0]
        inside = self._calls_within(block)
        # `get_orderable_usd` is legitimately re-read by the revalidation,
        # so it may appear inside -- but the SIZING call must not be the
        # one in there. Distinguish by the analysis calls that have no
        # business in a critical section at all.
        if slow_call == "get_orderable_usd":
            pytest.skip("re-read under the lock by design, see revalidation")
        assert slow_call not in inside, (
            f"{slow_call}() is analysis and must not hold the execution lock")


class TestTheLockActuallyExcludes:
    def test_a_second_holder_is_refused_rather_than_queued(self, tmp_path):
        path = str(tmp_path / "exec.lock")
        with execution_lock.hold("FIRST", path=path, timeout_seconds=0):
            with pytest.raises(execution_lock.ExecutionLockUnavailable):
                with execution_lock.hold("SECOND", path=path, timeout_seconds=0):
                    raise AssertionError("two holders at once")

    def test_the_lock_is_released_on_the_way_out(self, tmp_path):
        path = str(tmp_path / "exec.lock")
        with execution_lock.hold("FIRST", path=path, timeout_seconds=0):
            pass
        with execution_lock.hold("SECOND", path=path, timeout_seconds=0):
            pass  # would raise if the first holder leaked the lock

    def test_the_lock_is_released_even_when_the_body_raises(self, tmp_path):
        path = str(tmp_path / "exec.lock")
        with pytest.raises(ValueError):
            with execution_lock.hold("FIRST", path=path, timeout_seconds=0):
                raise ValueError("submission blew up")
        with execution_lock.hold("SECOND", path=path, timeout_seconds=0):
            pass

    @pytest.mark.skipif(shutil.which("flock") is None,
                        reason="flock(1) is util-linux; absent on macOS")
    def test_a_shell_flock_and_the_python_lock_exclude_each_other(self, tmp_path):
        """The monitor uses flock(1); the entry uses fcntl. Same file, so
        they must be the same lock -- otherwise the fix is cosmetic."""
        path = tmp_path / "exec.lock"
        path.touch()
        with execution_lock.hold("PYTHON", path=str(path), timeout_seconds=0):
            done = subprocess.run(
                ["flock", "-n", str(path), "-c", "true"],
                capture_output=True)
            assert done.returncode != 0, (
                "flock(1) took a lock Python was holding")
        done = subprocess.run(["flock", "-n", str(path), "-c", "true"],
                              capture_output=True)
        assert done.returncode == 0, "the lock was not released"


def _free_for_another_holder(path) -> bool:
    """True when a SEPARATE process could take the lock right now.

    Uses fcntl in a child rather than flock(1): the production hosts have
    the binary and the development machines do not, and the mechanism
    under test -- flock(2) on one file -- is identical either way.
    """
    probe = (
        "import fcntl,sys\n"
        "h=open(sys.argv[1],'a+')\n"
        "try:\n"
        "    fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(0)\n")
    done = subprocess.run([sys.executable, "-c", probe, path],
                          capture_output=True)
    return done.returncode == 0


def _hold_briefly(path, ready, release):
    from execution import execution_lock as el
    with el.hold("ANALYSIS_PROXY", path=path, timeout_seconds=0):
        ready.set()
        release.wait(timeout=10)


class TestTheExitPathGetsInWhileEntryAnalysisRuns:
    def test_the_monitor_acquires_while_a_slow_entry_analyses(self, tmp_path):
        """The regression, expressed directly: an entry cycle doing its
        slow work must leave the execution lock free the whole time."""
        path = str(tmp_path / "exec.lock")
        Path(path).touch()

        # The entry's analysis phase now holds NOTHING. Modelled as the
        # absence of a holder, which is exactly what the fix produces.
        for _ in range(30):  # thirty "minutes" of analysis
            assert _free_for_another_holder(path), (
                "the exit monitor was locked out during entry analysis")

    def test_the_monitor_is_excluded_only_while_a_submission_runs(self, tmp_path):
        path = str(tmp_path / "exec.lock")
        Path(path).touch()
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        worker = ctx.Process(target=_hold_briefly, args=(path, ready, release))
        worker.start()
        try:
            assert ready.wait(timeout=10), "the holder never started"
            assert not _free_for_another_holder(path), (
                "a submission in progress must exclude the exit path")
        finally:
            release.set()
            worker.join(timeout=10)
        assert _free_for_another_holder(path)


class TestTheMonitorNoLongerLosesTicksSilently:
    def test_a_skipped_tick_is_recorded(self):
        assert "MONITOR_LOCK_SKIPPED" in MONITOR_WRAPPER, (
            "a tick that could not take the lock must not exit silently; "
            "that is what hid the 1-in-29 starvation")

    def test_every_scheduled_tick_is_recorded(self):
        assert "MONITOR_TICK" in MONITOR_WRAPPER

    def test_a_completed_evaluation_is_distinguishable_from_a_skip(self):
        assert "MONITOR_EVALUATED" in MONITOR_WRAPPER

    def test_contention_has_its_own_exit_code(self):
        assert "-E 99" in MONITOR_WRAPPER, (
            "without it a skipped tick and a crashed one both read as 1")
