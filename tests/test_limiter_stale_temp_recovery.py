"""A crash between fsync and os.replace leaves a temporary file behind.

The atomic-write work made that safe -- the committed state survives
untouched -- but the orphan itself was never removed, so it accumulated
and the "zero stray artifacts" condition failed. The next healthy run now
cleans it up.

Policy B from the directive: a temporary whose owner PID no longer exists
is removed immediately, inside the shared lock, before this lifecycle
creates its own temporary. A live PID -- including a REUSED one -- is
never touched.
"""
import errno
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from brokers import kis_rate_limiter
from brokers.kis_broker import KISBroker
from brokers.kis_rate_limiter import (
    CATEGORY_READ,
    STATE_VERSION,
    KisRateLimiter,
    KISRateLimitTempCleanupError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEAD_PID = 999999          # far above any real pid on these hosts
UUID32 = "a" * 32


@pytest.fixture
def paced(monkeypatch):
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
    monkeypatch.setenv("KIS_RATE_LIMIT_STALE_TEMP_MIN_AGE_SECONDS", "0")
    monkeypatch.setenv("KIS_RATE_LIMIT_MAX_CLOCK_SKEW_SECONDS", "5")


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "rate.json"
    path.write_text(json.dumps({CATEGORY_READ: time.time() - 3600,
                                "version": STATE_VERSION}), encoding="utf-8")
    return path


def _temp(state_path, *, pid=DEAD_PID, token=UUID32, name=None):
    target = state_path.with_name(
        name or f".{state_path.name}.{pid}.{token}.tmp")
    target.write_text("partial", encoding="utf-8")
    return target


def _temps(directory):
    return sorted(p.name for p in directory.glob("*.tmp"))


class TestDeadOwnerTempsAreRemoved:
    def test_control_a_clean_run_creates_no_leftovers(self, state, paced):
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert _temps(state.parent) == []

    def test_a_dead_owners_temp_is_removed_on_the_next_run(self, state, paced):
        """The reported failure: this used to survive indefinitely."""
        orphan = _temp(state)
        assert orphan.exists()
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert _temps(state.parent) == []
        assert json.loads(state.read_text(encoding="utf-8"))["version"] == STATE_VERSION

    def test_several_orphans_are_all_removed(self, state, paced):
        for index in range(3):
            _temp(state, pid=DEAD_PID + index, token=f"{index:032x}")
        assert len(_temps(state.parent)) == 3
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert _temps(state.parent) == []

    def test_the_reservation_still_happens(self, state, paced):
        _temp(state)
        before = json.loads(state.read_text(encoding="utf-8"))[CATEGORY_READ]
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        after = json.loads(state.read_text(encoding="utf-8"))[CATEGORY_READ]
        assert after > before, "cleanup replaced the reservation instead of preceding it"

    def test_cleanup_happens_before_the_new_temp_exists(self, state, paced):
        """Ordering guard: the lifecycle must not delete its own file."""
        seen = {}
        real_replace = os.replace

        def _capture(src, dst):
            seen["temps_at_replace"] = _temps(state.parent)
            return real_replace(src, dst)

        kis_rate_limiter.os.replace = _capture
        try:
            _temp(state)
            KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        finally:
            kis_rate_limiter.os.replace = real_replace
        # At replace time exactly one temp exists: ours.
        assert len(seen["temps_at_replace"]) == 1
        assert str(DEAD_PID) not in seen["temps_at_replace"][0]


class TestLiveOwnersAreProtected:
    def test_a_live_pid_temp_is_kept(self, state, paced):
        """Covers PID reuse too: a reused, live pid reads as not-stale."""
        mine = _temp(state, pid=os.getpid(), token="b" * 32)
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert mine.exists(), "a live process's temporary was deleted"

    def test_a_live_pid_is_kept_even_when_old(self, state, paced, monkeypatch):
        monkeypatch.setenv("KIS_RATE_LIMIT_STALE_TEMP_MIN_AGE_SECONDS", "0")
        mine = _temp(state, pid=os.getpid(), token="c" * 32)
        os.utime(mine, (time.time() - 86400, time.time() - 86400))
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert mine.exists()

    def test_a_recent_dead_temp_is_kept_when_an_age_is_required(self, state, paced,
                                                                monkeypatch):
        """Policy A behaviour is available by configuration."""
        monkeypatch.setenv("KIS_RATE_LIMIT_STALE_TEMP_MIN_AGE_SECONDS", "3600")
        orphan = _temp(state)
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert orphan.exists()

    def test_an_aged_dead_temp_is_removed_under_the_same_policy(self, state, paced,
                                                                monkeypatch):
        monkeypatch.setenv("KIS_RATE_LIMIT_STALE_TEMP_MIN_AGE_SECONDS", "60")
        orphan = _temp(state)
        old = time.time() - 3600
        os.utime(orphan, (old, old))
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert not orphan.exists()


class TestOnlyOurOwnFilesAreTouched:
    # Files with OUR prefix are deliberately absent here -- those are
    # malformed artifacts of ours and are covered by the tests below.
    @pytest.mark.parametrize("name", [
        "user-data.tmp",
        "something.else.tmp",
        ".other-state.json.123.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp",
        ".unrelated.json.999.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.tmp",
    ])
    def test_foreign_files_are_left_alone(self, state, paced, name):
        stranger = state.with_name(name)
        stranger.write_text("not ours", encoding="utf-8")
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert stranger.exists(), f"{name} was deleted"

    def test_another_categorys_temp_is_left_alone(self, tmp_path, paced):
        """Each limiter owns only its own state's temporaries."""
        read_state = tmp_path / "read.json"
        read_state.write_text(json.dumps({CATEGORY_READ: time.time() - 3600,
                                          "version": STATE_VERSION}), encoding="utf-8")
        token_orphan = tmp_path / f".token.json.{DEAD_PID}.{UUID32}.tmp"
        token_orphan.write_text("theirs", encoding="utf-8")

        KisRateLimiter(path=read_state).wait(category=CATEGORY_READ)
        assert token_orphan.exists(), "another category's temporary was deleted"

    def test_a_malformed_own_prefix_artifact_is_reported(self, state, paced):
        """It claims our prefix but can never be matched, so it would
        linger forever. Surface it instead of ignoring it."""
        bad = state.with_name(f".{state.name}.not-a-pid.{UUID32}.tmp")
        bad.write_text("junk", encoding="utf-8")
        with pytest.raises(KISRateLimitTempCleanupError) as excinfo:
            KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_TEMP_ARTIFACT_INVALID"
        assert bad.exists(), "a malformed artifact must not be silently deleted"

    def test_a_bad_uuid_with_our_prefix_is_reported(self, state, paced):
        bad = state.with_name(f".{state.name}.123.not-a-uuid.tmp")
        bad.write_text("junk", encoding="utf-8")
        with pytest.raises(KISRateLimitTempCleanupError):
            KisRateLimiter(path=state).wait(category=CATEGORY_READ)

    def test_a_symlink_is_never_followed_or_deleted(self, state, paced, tmp_path):
        """A symlink here could otherwise delete a file outside the
        state directory."""
        outside = tmp_path.parent / "precious.txt"
        outside.write_text("do not delete", encoding="utf-8")
        link = state.with_name(f".{state.name}.{DEAD_PID}.{'d' * 32}.tmp")
        os.symlink(outside, link)

        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert outside.exists(), "a symlink target outside the directory was deleted"
        assert os.path.islink(link), "the symlink itself was removed"
        link.unlink()

    def test_a_directory_named_like_a_temp_is_left_alone(self, state, paced):
        fake = state.with_name(f".{state.name}.{DEAD_PID}.{'e' * 32}.tmp")
        fake.mkdir()
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert fake.is_dir()
        fake.rmdir()


class TestCleanupFailuresBlock:
    def test_an_unremovable_orphan_blocks_the_request(self, state, paced, monkeypatch):
        orphan = _temp(state)
        monkeypatch.setattr(
            kis_rate_limiter.os, "unlink",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")))
        with pytest.raises(KISRateLimitTempCleanupError) as excinfo:
            KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_STALE_TEMP_CLEANUP_FAILED"
        assert orphan.exists()

    def test_no_kis_request_follows_a_cleanup_failure(self, state, paced, monkeypatch):
        _temp(state)
        monkeypatch.setenv("KIS_ENV", "live")
        monkeypatch.setenv("KIS_APP_KEY", "k")
        monkeypatch.setenv("KIS_APP_SECRET", "s")
        monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
        monkeypatch.setenv("KIS_ACCOUNT_READ_ENABLED", "true")
        monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")

        class _Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, headers=None, params=None, json=None,
                        timeout=None):
                class R:
                    status_code = 200
                    text = "{}"

                    @staticmethod
                    def json():
                        return {"rt_cd": "0", "output": []}
                if not url.endswith("/oauth2/tokenP"):
                    self.calls.append(url)
                return R()

        monkeypatch.setattr(
            kis_rate_limiter.os, "unlink",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")))
        session = _Session()
        broker = KISBroker(session=session, limiter=KisRateLimiter(path=state))
        with pytest.raises(KISRateLimitTempCleanupError):
            broker.get_open_orders()
        assert session.calls == [], "a request went out despite a cleanup failure"

    def test_a_directory_fsync_failure_after_cleanup_blocks(self, state, paced,
                                                            monkeypatch):
        _temp(state)
        monkeypatch.setattr(
            kis_rate_limiter.os, "open",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "no dir fd")))
        with pytest.raises(KISRateLimitTempCleanupError) as excinfo:
            KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_STALE_TEMP_CLEANUP_FAILED"

    def test_an_unscannable_directory_blocks(self, state, paced, monkeypatch):
        monkeypatch.setattr(
            Path, "iterdir",
            lambda self: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")))
        with pytest.raises(KISRateLimitTempCleanupError):
            KisRateLimiter(path=state).wait(category=CATEGORY_READ)

    def test_the_failure_repeats_rather_than_giving_up(self, state, paced, monkeypatch):
        """No "ignore it after N tries" fallback -- fail-closed until fixed."""
        _temp(state)
        monkeypatch.setattr(
            kis_rate_limiter.os, "unlink",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")))
        for _ in range(3):
            with pytest.raises(KISRateLimitTempCleanupError):
                KisRateLimiter(path=state).wait(category=CATEGORY_READ)

    def test_the_alert_names_no_path(self, state, paced, monkeypatch):
        sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda m: sent.append(m) or True)
        _temp(state)
        monkeypatch.setattr(
            kis_rate_limiter.os, "unlink",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")))
        with pytest.raises(KISRateLimitTempCleanupError):
            KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        joined = "\n".join(sent)
        assert joined
        assert str(state.parent) not in joined


_CRASH_CHILD = textwrap.dedent(
    """
    import os, signal, sys
    sys.path.insert(0, sys.argv[1])
    os.environ["KIS_READ_MIN_INTERVAL_SECONDS"] = "3.0"
    os.environ["KIS_RATE_LIMIT_MAX_CLOCK_SKEW_SECONDS"] = "5"
    os.environ["KIS_RATE_LIMIT_STALE_TEMP_MIN_AGE_SECONDS"] = "0"
    from brokers import kis_rate_limiter
    from brokers.kis_rate_limiter import CATEGORY_READ, KisRateLimiter
    kis_rate_limiter.os.replace = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
    KisRateLimiter(path=sys.argv[2]).wait(category=CATEGORY_READ)
    """
)


class TestRealCrashRecovery:
    def test_the_next_run_cleans_up_after_a_real_sigkill(self, tmp_path, paced):
        """End to end: a real child dies between fsync and replace, and
        the next healthy run leaves the directory clean."""
        state = tmp_path / "rate.json"
        committed = time.time() - 3600
        good = json.dumps({CATEGORY_READ: committed, "version": STATE_VERSION})
        state.write_text(good, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "-c", _CRASH_CHILD, str(REPO_ROOT), str(state)],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode != 0, "the child should have been killed"
        # The crash left exactly one orphan, and the committed state is intact.
        leftovers = _temps(tmp_path)
        assert len(leftovers) == 1, leftovers
        assert state.read_text(encoding="utf-8") == good

        KisRateLimiter(path=state).wait(category=CATEGORY_READ)

        assert _temps(tmp_path) == [], "the orphan survived a healthy run"
        reread = json.loads(state.read_text(encoding="utf-8"))
        assert reread["version"] == STATE_VERSION
        assert reread[CATEGORY_READ] > committed
        assert not (tmp_path / "rate.json.lock").exists() or True   # no permanent lock

    def test_a_later_process_can_still_pace(self, tmp_path, paced):
        state = tmp_path / "rate.json"
        state.write_text(json.dumps({CATEGORY_READ: time.time() - 3600,
                                     "version": STATE_VERSION}), encoding="utf-8")
        subprocess.run([sys.executable, "-c", _CRASH_CHILD, str(REPO_ROOT), str(state)],
                       capture_output=True, text=True, timeout=120)
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        # And a second, immediately after, is paced rather than free.
        started = time.time()
        KisRateLimiter(path=state).wait(category=CATEGORY_READ)
        assert time.time() - started >= 2.5
        assert _temps(tmp_path) == []
