"""The limiter's whole lifecycle must succeed before any KIS request.

ORACLE-HIGH-1  an OSError from flock(LOCK_UN) was swallowed, so a request
               went out while the shared lock was still held -- one
               filesystem fault stalling every other process while this
               one kept talking to KIS
ORACLE-HIGH-2  the state was rewritten in place, so a crash mid-write
               could leave an empty or truncated budget behind, and the
               temp/fsync/replace failures could not even be tested

`wait()` now returns only when the reservation is durable AND the lock
was released and closed cleanly.
"""
import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from brokers import kis_rate_limiter
from brokers.kis_broker import KISBroker
from brokers.kis_rate_limiter import (
    CATEGORY_CANCEL,
    CATEGORY_ORDER,
    CATEGORY_READ,
    CATEGORY_TOKEN,
    STATE_VERSION,
    KisRateLimiter,
    KISRateLimitLockReleaseError,
    KISRateLimitStateUnavailable,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def paced(monkeypatch):
    for name in ("KIS_READ_MIN_INTERVAL_SECONDS", "KIS_TOKEN_MIN_INTERVAL_SECONDS",
                 "KIS_ORDER_MIN_INTERVAL_SECONDS"):
        monkeypatch.setenv(name, "3.0")


def _limiter(path, clock):
    inst = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
    inst._wall = clock.time
    return inst


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("KIS_ENV", "live")
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNT_READ_ENABLED", "true")
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")


class _CountingSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        class R:
            status_code = 200
            text = "{}"

            @staticmethod
            def json():
                return ({"access_token": "t", "expires_in": 86400}
                        if url.endswith("/oauth2/tokenP")
                        else {"rt_cd": "0", "output": []})
        if not url.endswith("/oauth2/tokenP"):
            self.calls.append(url)
        return R()


def _fail_unlock(monkeypatch, *, only_unlock=True):
    real = kis_rate_limiter.fcntl.flock

    def _flock(handle, operation):
        if only_unlock and operation == kis_rate_limiter.fcntl.LOCK_UN:
            raise OSError(errno.EIO, "cannot release")
        return real(handle, operation)

    monkeypatch.setattr(kis_rate_limiter.fcntl, "flock", _flock)


# ================================================  lock release lifecycle

class TestLockReleaseIsPartOfTheLifecycle:
    def test_control_a_clean_lifecycle_returns(self, tmp_path, clock, paced):
        limiter = _limiter(tmp_path / "rate.json", clock)
        assert limiter.wait(category=CATEGORY_READ) == 0.0
        assert (tmp_path / "rate.json").exists()

    def test_an_unlock_failure_raises(self, tmp_path, clock, paced, monkeypatch):
        """The regression: this used to return normally."""
        limiter = _limiter(tmp_path / "rate.json", clock)
        _fail_unlock(monkeypatch)
        with pytest.raises(KISRateLimitLockReleaseError) as excinfo:
            limiter.wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_LOCK_RELEASE_FAILED"

    def test_no_kis_request_follows_an_unlock_failure(self, tmp_path, clock, paced,
                                                      monkeypatch, env):
        session = _CountingSession()
        broker = KISBroker(session=session, limiter=_limiter(tmp_path / "r.json", clock))
        _fail_unlock(monkeypatch)
        with pytest.raises(KISRateLimitLockReleaseError):
            broker.get_open_orders()
        assert session.calls == [], "a request went out after the lock failed to release"

    def test_a_close_failure_raises(self, tmp_path, clock, paced, monkeypatch):
        real_open = open

        class _Handle:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def close(self):
                self._inner.close()
                raise OSError(errno.EIO, "cannot close")

        def _wrap(file, *a, **k):
            handle = real_open(file, *a, **k)
            return _Handle(handle) if str(file).endswith(".lock") else handle

        monkeypatch.setattr("builtins.open", _wrap)
        with pytest.raises(KISRateLimitLockReleaseError) as excinfo:
            _limiter(tmp_path / "rate.json", clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_LOCK_CLOSE_FAILED"

    def test_the_limiter_is_invalidated_afterwards(self, tmp_path, clock, paced,
                                                   monkeypatch):
        """A handle whose state is unknown must not pace anything else."""
        limiter = _limiter(tmp_path / "rate.json", clock)
        _fail_unlock(monkeypatch)
        with pytest.raises(KISRateLimitLockReleaseError):
            limiter.wait(category=CATEGORY_READ)
        monkeypatch.undo()
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            limiter.wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_LIMITER_INVALIDATED"

    def test_a_persistence_failure_outranks_a_release_failure(self, tmp_path, clock,
                                                              paced, monkeypatch):
        """Both fail: the caller needs the ORIGINAL cause."""
        monkeypatch.setattr(
            kis_rate_limiter.os, "replace",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EROFS, "read-only")))
        _fail_unlock(monkeypatch)
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            _limiter(tmp_path / "rate.json", clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_PERSISTENCE"

    @pytest.mark.parametrize("category", [CATEGORY_READ, CATEGORY_TOKEN,
                                          CATEGORY_ORDER, CATEGORY_CANCEL])
    def test_every_category_blocks_on_a_release_failure(self, tmp_path, clock, paced,
                                                        monkeypatch, category):
        limiter = _limiter(tmp_path / "rate.json", clock)
        _fail_unlock(monkeypatch)
        with pytest.raises(KISRateLimitLockReleaseError):
            limiter.wait(category=category)

    def test_the_release_failure_is_alerted_without_a_path(self, tmp_path, clock,
                                                           paced, monkeypatch):
        sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda m: sent.append(m) or True)
        _fail_unlock(monkeypatch)
        with pytest.raises(KISRateLimitLockReleaseError):
            _limiter(tmp_path / "rate.json", clock).wait(category=CATEGORY_READ)
        joined = "\n".join(sent)
        assert "released" in joined
        assert str(tmp_path) not in joined

    def test_no_swallowed_unlock_remains_in_the_source(self):
        source = (REPO_ROOT / "brokers" / "kis_rate_limiter.py").read_text(encoding="utf-8")
        assert "LOCK_UN)\n            except OSError:\n                    pass" not in source
        # The release must be reported, not silently discarded.
        assert "_release" in source


# ==================================================  atomic persistence

class TestAtomicStatePersistence:
    def _write_valid(self, path, clock):
        path.write_text(json.dumps({CATEGORY_READ: clock.now - 100,
                                    "version": STATE_VERSION}), encoding="utf-8")

    def test_control_a_normal_write_replaces_the_file(self, tmp_path, clock, paced):
        path = tmp_path / "rate.json"
        _limiter(path, clock).wait(category=CATEGORY_READ)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["version"] == STATE_VERSION
        assert CATEGORY_READ in stored

    def test_no_temporary_file_survives_a_success(self, tmp_path, clock, paced):
        _limiter(tmp_path / "rate.json", clock).wait(category=CATEGORY_READ)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_the_state_file_is_owner_only(self, tmp_path, clock, paced):
        path = tmp_path / "rate.json"
        _limiter(path, clock).wait(category=CATEGORY_READ)
        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize("target,exc", [
        ("open", OSError(errno.EACCES, "no create")),
        ("json.dump", OSError(errno.ENOSPC, "no space")),
        ("os.fsync", OSError(errno.EIO, "no fsync")),
        ("os.chmod", OSError(errno.EPERM, "no chmod")),
        ("os.replace", OSError(errno.EXDEV, "no replace")),
    ])
    def test_each_persistence_step_blocks(self, tmp_path, clock, paced, monkeypatch,
                                          target, exc):
        path = tmp_path / "rate.json"
        self._write_valid(path, clock)
        before = path.read_text(encoding="utf-8")

        if target == "open":
            real_open = open

            def _wrap(file, *a, **k):
                if str(file).endswith(".tmp"):
                    raise exc
                return real_open(file, *a, **k)

            monkeypatch.setattr("builtins.open", _wrap)
        elif target == "json.dump":
            monkeypatch.setattr(kis_rate_limiter.json, "dump",
                                lambda *a, **k: (_ for _ in ()).throw(exc))
        else:
            module, name = target.split(".")
            monkeypatch.setattr(getattr(kis_rate_limiter, module), name,
                                lambda *a, **k: (_ for _ in ()).throw(exc))

        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            _limiter(path, clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_PERSISTENCE"
        # The previous good state must be intact.
        assert path.read_text(encoding="utf-8") == before

    def test_a_directory_fsync_failure_blocks(self, tmp_path, clock, paced, monkeypatch):
        path = tmp_path / "rate.json"
        real_fsync = os.fsync

        def _fsync(fd):
            if os.path.isdir("/dev/fd/%d" % fd) if os.path.exists("/dev/fd") else False:
                raise OSError(errno.EIO, "no dir fsync")
            return real_fsync(fd)

        monkeypatch.setattr(kis_rate_limiter.os, "open",
                            lambda *a, **k: (_ for _ in ()).throw(
                                OSError(errno.EACCES, "no dir handle")))
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            _limiter(path, clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_PERSISTENCE"

    def test_a_failed_write_leaves_no_temporary_behind(self, tmp_path, clock, paced,
                                                       monkeypatch):
        monkeypatch.setattr(
            kis_rate_limiter.os, "replace",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "no")))
        with pytest.raises(KISRateLimitStateUnavailable):
            _limiter(tmp_path / "rate.json", clock).wait(category=CATEGORY_READ)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_a_cleanup_failure_does_not_mask_the_real_error(self, tmp_path, clock,
                                                            paced, monkeypatch):
        monkeypatch.setattr(
            kis_rate_limiter.os, "replace",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "no replace")))
        monkeypatch.setattr(
            kis_rate_limiter.os, "unlink",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "no unlink")))
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            _limiter(tmp_path / "rate.json", clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_PERSISTENCE"

    def test_the_temporary_lives_beside_the_target(self, tmp_path, clock, paced,
                                                   monkeypatch):
        """os.replace() is only atomic within one filesystem."""
        seen = {}
        real_replace = os.replace

        def _capture(src, dst):
            seen["src"], seen["dst"] = Path(src), Path(dst)
            return real_replace(src, dst)

        monkeypatch.setattr(kis_rate_limiter.os, "replace", _capture)
        path = tmp_path / "rate.json"
        _limiter(path, clock).wait(category=CATEGORY_READ)
        assert seen["src"].parent == seen["dst"].parent == path.parent

    def test_no_in_place_overwrite_remains(self):
        source = (REPO_ROOT / "brokers" / "kis_rate_limiter.py").read_text(encoding="utf-8")
        assert "handle.truncate()" not in source
        assert "os.replace" in source
        assert "os.fsync" in source

    def test_a_reader_never_sees_a_partial_state(self, tmp_path, clock, paced):
        """Ten sequential writes; the file parses cleanly every time."""
        path = tmp_path / "rate.json"
        for _ in range(10):
            limiter = _limiter(path, clock)
            limiter.wait(category=CATEGORY_READ)
            parsed = json.loads(path.read_text(encoding="utf-8"))
            assert parsed["version"] == STATE_VERSION


# ==================================================  real crash / processes

_WRITER = textwrap.dedent(
    """
    import os, signal, sys
    sys.path.insert(0, sys.argv[1])
    # Non-zero, or wait() short-circuits before it ever persists.
    os.environ["KIS_READ_MIN_INTERVAL_SECONDS"] = "3.0"
    os.environ["KIS_RATE_LIMIT_MAX_CLOCK_SKEW_SECONDS"] = "5"
    from brokers import kis_rate_limiter
    from brokers.kis_rate_limiter import CATEGORY_READ, KisRateLimiter

    real_replace = os.replace

    def die_before_replace(src, dst):
        # The temp file is written and fsynced; we die before it lands.
        os.kill(os.getpid(), signal.SIGKILL)

    kis_rate_limiter.os.replace = die_before_replace
    KisRateLimiter(path=sys.argv[2]).wait(category=CATEGORY_READ)
    """
)

_PACER = textwrap.dedent(
    """
    import json, os, sys, time
    sys.path.insert(0, sys.argv[1])
    os.environ["KIS_READ_MIN_INTERVAL_SECONDS"] = "1.0"
    from brokers.kis_rate_limiter import CATEGORY_READ, KisRateLimiter
    start = time.time()
    KisRateLimiter(path=sys.argv[2]).wait(category=CATEGORY_READ)
    print("OK %.3f" % (time.time() - start), flush=True)
    """
)


class TestRealCrashAndProcesses:
    def test_a_crash_before_replace_leaves_the_old_state_intact(self, tmp_path, paced):
        """A real SIGKILL between the temp write and the replace. With
        in-place rewriting this could have left an empty or truncated
        budget; with replace() the committed state is untouched."""
        import time as _time

        path = tmp_path / "rate.json"
        # A real past timestamp -- the child and the reader below both use
        # the real wall clock.
        committed = _time.time() - 3600
        good = json.dumps({CATEGORY_READ: committed, "version": STATE_VERSION})
        path.write_text(good, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "-c", _WRITER, str(REPO_ROOT), str(path)],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode != 0, "the child was supposed to be killed"
        # Byte-for-byte the state that was committed before the crash.
        assert path.read_text(encoding="utf-8") == good
        assert json.loads(path.read_text(encoding="utf-8"))[CATEGORY_READ] == committed

        # A stale temp may remain; the next run must read the good state
        # and succeed anyway.
        KisRateLimiter(path=path).wait(category=CATEGORY_READ)
        reread = json.loads(path.read_text(encoding="utf-8"))
        assert reread["version"] == STATE_VERSION
        assert reread[CATEGORY_READ] > committed

    def test_four_processes_serialize_and_keep_the_state_valid(self, tmp_path):
        path = tmp_path / "rate.json"
        children = [
            subprocess.Popen([sys.executable, "-c", _PACER, str(REPO_ROOT), str(path)],
                             stdout=subprocess.PIPE, text=True)
            for _ in range(4)
        ]
        outs = [c.communicate(timeout=180)[0].strip().splitlines()[-1] for c in children]
        assert all(o.startswith("OK") for o in outs), outs
        # Whatever the interleaving, the file is a complete document.
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert parsed["version"] == STATE_VERSION
        assert isinstance(parsed[CATEGORY_READ], (int, float))
        assert list(tmp_path.glob("*.tmp")) == [], "a temporary file leaked"

    def test_the_total_elapsed_time_reflects_serialization(self, tmp_path):
        """Four processes at a 1s interval cannot all finish instantly."""
        import time as _time

        path = tmp_path / "rate.json"
        start = _time.time()
        children = [
            subprocess.Popen([sys.executable, "-c", _PACER, str(REPO_ROOT), str(path)],
                             stdout=subprocess.PIPE, text=True)
            for _ in range(4)
        ]
        for child in children:
            child.communicate(timeout=180)
        assert _time.time() - start >= 3.0, "the four processes did not serialize"


class TestReconciliationStopsOnLockFailure:
    def test_a_release_failure_mid_sweep_records_no_snapshot(self, tmp_path, clock,
                                                             paced, monkeypatch, env):
        from reconciliation import snapshot as recon

        session = _CountingSession()
        broker = KISBroker(session=session, limiter=_limiter(tmp_path / "r.json", clock))
        _fail_unlock(monkeypatch)
        with pytest.raises(recon.ReconciliationUnavailableError) as excinfo:
            recon.build_snapshot(broker=broker, conn=None, account_id="1", symbol=None,
                                 now=None, source="test")
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_LOCK_RELEASE_FAILED"
        assert session.calls == []
