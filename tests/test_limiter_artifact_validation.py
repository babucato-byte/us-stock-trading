"""Anything in the limiter's own temp namespace that it could not have
written stops the request.

Two files were reported as slipping through. Both were *seen* by the
cleanup pass and both were deliberately left alone -- which was correct --
but the pass then carried on and the HTTP request went out:

    .<state>.<pid>.<uuid>.temp     the ".tmp" filter skipped it outright
    .<state>.<pid>.<uuid>.tmp -> / a symlink was refused for deletion,
                                   then ignored

"Not safe to delete" and "safe to proceed" are different conclusions. A
file wearing this limiter's prefix is evidence that something other than
this limiter is writing into the shared state directory, and a shared
pacing budget that another writer is touching cannot be trusted -- so the
entry is preserved for an operator AND the transport is suppressed.

Every fault below is preceded by a control proving the same call really
does reach the session when the directory is clean.
"""
import errno
import json
import os
import socket
import stat
import time
from pathlib import Path

import pytest

from brokers import kis_rate_limiter
from brokers.kis_broker import KISBroker
from brokers.kis_rate_limiter import (
    CATEGORY_READ,
    CATEGORY_TOKEN,
    REASON_ARTIFACT_SCAN_FAILED,
    REASON_LIMITER_INVALIDATED,
    REASON_TEMP_ARTIFACT_INVALID,
    STATE_VERSION,
    KisRateLimiter,
    KISRateLimitArtifactScanError,
    KISRateLimitStateUnavailable,
    KISRateLimitTempArtifactError,
)

LIMITER_SOURCE = Path(kis_rate_limiter.__file__).read_text(encoding="utf-8")

DEAD_PID = 999999
UUID32 = "a" * 32


@pytest.fixture
def paced(monkeypatch):
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
    monkeypatch.setenv("KIS_TOKEN_MIN_INTERVAL_SECONDS", "3.0")
    monkeypatch.setenv("KIS_RATE_LIMIT_STALE_TEMP_MIN_AGE_SECONDS", "0")
    monkeypatch.setenv("KIS_RATE_LIMIT_MAX_CLOCK_SKEW_SECONDS", "5")


def _state(directory, name="rate.json"):
    path = Path(directory) / name
    path.write_text(json.dumps({CATEGORY_READ: time.time() - 3600,
                                CATEGORY_TOKEN: time.time() - 3600,
                                "version": STATE_VERSION}), encoding="utf-8")
    return path


@pytest.fixture
def state(tmp_path):
    return _state(tmp_path)


def _write(path, text="junk"):
    path.write_text(text, encoding="utf-8")
    return path


def _valid_temp_name(state_path, *, pid=DEAD_PID, token=UUID32):
    return f".{state_path.name}.{pid}.{token}.tmp"


def _limiter(state_path):
    """A real limiter with the sleeping stubbed out. These tests are about
    what the artifact scan permits, not about how long pacing waits --
    tests/test_kis_rate_limiting.py owns the intervals."""
    return KisRateLimiter(path=state_path, sleeper=lambda _seconds: None)


def _entries(directory):
    """Everything but the shared lock, which every attempt creates."""
    return sorted(p.name for p in Path(directory).iterdir()
                  if not p.name.endswith(".lock"))


class _Session:
    """Records every non-token URL the broker actually reaches for."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None,
                timeout=None):
        token = url.endswith("/oauth2/tokenP")
        if not token:
            self.calls.append(url)
        payload = ({"access_token": "t", "expires_in": 86400} if token
                   else {"rt_cd": "0", "output": []})

        class R:
            status_code = 200
            text = "{}"

            @staticmethod
            def json():
                return payload

        return R()


@pytest.fixture
def broker_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_ENV", "live")
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_READ_ENABLED", "true")
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")
    monkeypatch.setenv("KIS_TOKEN_CACHE_FILE", str(tmp_path / "token-cache.json"))


# --------------------------------------------------------------------------
# The name is validated against the whole string, not a prefix of it.
# --------------------------------------------------------------------------

class TestFilenameIsFullyMatched:
    def test_the_module_uses_fullmatch(self):
        """`match()` would accept ".<state>.<pid>.<uuid>.tmpEXTRA" and `$`
        alone would accept a trailing newline."""
        assert "_TEMP_PATTERN.fullmatch(" in LIMITER_SOURCE
        assert "_TEMP_PATTERN.match(" not in LIMITER_SOURCE

    def test_no_suffix_only_membership_test_remains(self):
        """Membership is decided by the namespace prefix. Deciding it with
        `endswith(".tmp")` is what let the ".temp" artifact through."""
        assert 'endswith(".tmp")' not in LIMITER_SOURCE

    def test_a_trailing_newline_is_not_a_valid_temp(self, state, paced):
        odd = state.with_name(_valid_temp_name(state) + "\n")
        _write(odd)
        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.detail == "malformed_filename"
        assert odd.exists()


# --------------------------------------------------------------------------
# Malformed names that carry our prefix.
# --------------------------------------------------------------------------

MALFORMED = {
    # The reported reproduction: a ".temp" suffix.
    "wrong_suffix": ".{state}.123.{uuid}.temp",
    "extra_suffix": ".{state}.123.{uuid}.tmp.extra",
    "non_numeric_pid": ".{state}.notpid.{uuid}.tmp",
    "empty_pid": ".{state}..{uuid}.tmp",
    "bad_uuid": ".{state}.123.notauuid.tmp",
    "missing_uuid": ".{state}.123.tmp",
    "short_uuid": ".{state}.123.abc123.tmp",
    "uppercase_uuid": ".{state}.123.{upper}.tmp",
    "negative_pid": ".{state}.-1.{uuid}.tmp",
    "no_dot_prefix_suffix": ".{state}.123.{uuid}.tmp.",
}


class TestMalformedOwnPrefixArtifacts:
    def test_control_a_clean_directory_permits_the_request(self, state, paced):
        assert _limiter(state).wait(category=CATEGORY_READ) == 0.0

    @pytest.mark.parametrize("label", sorted(MALFORMED))
    def test_it_is_invalid_blocks_and_survives(self, state, paced, label):
        name = MALFORMED[label].format(
            state=state.name, uuid=UUID32, upper=UUID32.upper()[:32])
        bad = _write(state.with_name(name))

        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)

        assert excinfo.value.reason_code == REASON_TEMP_ARTIFACT_INVALID
        assert excinfo.value.detail == "malformed_filename"
        assert bad.exists(), "an artifact of unknown origin was deleted"
        assert bad.read_text(encoding="utf-8") == "junk"

    @pytest.mark.parametrize("label", sorted(MALFORMED))
    def test_no_request_reaches_kis(self, state, paced, broker_env, label):
        name = MALFORMED[label].format(
            state=state.name, uuid=UUID32, upper=UUID32.upper()[:32])

        control = _Session()
        KISBroker(session=control,
                  limiter=_limiter(state)).get_open_orders()
        assert control.calls, "the control never reached the session"

        _write(state.with_name(name))
        session = _Session()
        broker = KISBroker(session=session, limiter=_limiter(state))
        with pytest.raises(KISRateLimitTempArtifactError):
            broker.get_open_orders()
        assert session.calls == [], "a request went out past an invalid artifact"

    def test_the_wait_does_not_return_normally(self, state, paced):
        """`wait()` returning any float at all is a licence to transport."""
        _write(state.with_name(f".{state.name}.123.{UUID32}.temp"))
        limiter = _limiter(state)
        try:
            result = limiter.wait(category=CATEGORY_READ)
        except KISRateLimitTempArtifactError:
            result = None
        assert result is None


# --------------------------------------------------------------------------
# Entries that are not regular files.
# --------------------------------------------------------------------------

class TestNonRegularArtifacts:
    def test_a_symlink_is_invalid_unfollowed_and_untouched(self, state, paced,
                                                            tmp_path):
        outside = tmp_path.parent / "artifact-precious.txt"
        outside.write_text("do not delete", encoding="utf-8")
        link = state.with_name(_valid_temp_name(state, token="d" * 32))
        os.symlink(outside, link)

        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)

        assert excinfo.value.reason_code == REASON_TEMP_ARTIFACT_INVALID
        assert excinfo.value.detail == "symlink"
        assert os.path.islink(link), "the symlink was deleted"
        assert outside.exists() and outside.read_text(encoding="utf-8") == "do not delete"
        link.unlink()
        outside.unlink()

    def test_a_symlink_inside_the_directory_is_also_invalid(self, state, paced):
        target = _write(state.with_name("inside.txt"), "keep me")
        link = state.with_name(_valid_temp_name(state, token="e" * 32))
        os.symlink(target, link)
        with pytest.raises(KISRateLimitTempArtifactError):
            _limiter(state).wait(category=CATEGORY_READ)
        assert target.read_text(encoding="utf-8") == "keep me"
        assert os.path.islink(link)
        link.unlink()

    def test_a_broken_symlink_is_invalid(self, state, paced):
        """lstat() succeeds on a dangling link; stat() would not. Reaching
        for the target at all is the thing to avoid."""
        link = state.with_name(_valid_temp_name(state, token="f" * 32))
        os.symlink(state.parent / "nothing-here", link)
        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.detail == "symlink"
        assert os.path.islink(link)
        link.unlink()

    def test_a_live_pid_symlink_is_invalid_before_the_pid_is_considered(
            self, state, paced, tmp_path):
        outside = tmp_path.parent / "artifact-live-target.txt"
        outside.write_text("x", encoding="utf-8")
        link = state.with_name(_valid_temp_name(state, pid=os.getpid()))
        os.symlink(outside, link)
        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        # Not "live temp": the file type decides first.
        assert excinfo.value.reason_code == REASON_TEMP_ARTIFACT_INVALID
        assert excinfo.value.detail == "symlink"
        link.unlink()
        outside.unlink()

    def test_a_directory_is_invalid(self, state, paced):
        fake = state.with_name(_valid_temp_name(state, token="b" * 32))
        fake.mkdir()
        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.detail == "non_regular_file"
        assert fake.is_dir()
        fake.rmdir()

    def test_a_fifo_is_invalid(self, state, paced):
        fifo = state.with_name(_valid_temp_name(state, token="c" * 32))
        os.mkfifo(fifo)
        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.detail == "non_regular_file"
        assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
        fifo.unlink()

    def test_a_unix_socket_is_invalid(self, state, paced):
        name = _valid_temp_name(state, token="9" * 32)
        # bound relative to the directory: an absolute sun_path can exceed
        # the ~104 byte limit under a long pytest tmp dir.
        previous = os.getcwd()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        os.chdir(state.parent)
        try:
            server.bind(name)
        finally:
            os.chdir(previous)
        try:
            with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
                _limiter(state).wait(category=CATEGORY_READ)
            assert excinfo.value.detail == "non_regular_file"
            assert stat.S_ISSOCK(os.lstat(state.with_name(name)).st_mode)
        finally:
            server.close()
            state.with_name(name).unlink()

    def test_no_request_reaches_kis_past_a_symlink(self, state, paced, broker_env,
                                                   tmp_path):
        control = _Session()
        KISBroker(session=control,
                  limiter=_limiter(state)).get_open_orders()
        assert control.calls, "the control never reached the session"

        outside = tmp_path.parent / "artifact-transport-target.txt"
        outside.write_text("intact", encoding="utf-8")
        link = state.with_name(_valid_temp_name(state, token="7" * 32))
        os.symlink(outside, link)

        session = _Session()
        broker = KISBroker(session=session, limiter=_limiter(state))
        with pytest.raises(KISRateLimitTempArtifactError):
            broker.get_open_orders()
        assert session.calls == []
        assert outside.read_text(encoding="utf-8") == "intact"
        link.unlink()
        outside.unlink()


# --------------------------------------------------------------------------
# Unrelated files must not be swept up in any of this.
# --------------------------------------------------------------------------

class TestUnrelatedFiles:
    @pytest.mark.parametrize("name", [
        "user-data.tmp",
        "other-service.temp",
        "notes.txt",
        ".kis-rate-limit-token.json.123." + UUID32 + ".tmp",
        ".other.json.123.notauuid.tmp",
        "rate.json.123." + UUID32 + ".tmp",      # no leading dot: not ours
        ".rate.jsonx.123." + UUID32 + ".tmp",    # prefix must end with the dot
    ])
    def test_they_neither_block_nor_disappear(self, tmp_path, paced, name):
        state = _state(tmp_path, "kis-rate-limit-read.json")
        stranger = _write(tmp_path / name, "not ours")
        _limiter(state).wait(category=CATEGORY_READ)
        assert stranger.exists(), f"{name} was deleted"
        assert stranger.read_text(encoding="utf-8") == "not ours"

    def test_a_read_limiter_ignores_token_and_order_artifacts(self, tmp_path, paced):
        read_state = _state(tmp_path, "kis-rate-limit-read.json")
        token_bad = _write(
            tmp_path / f".kis-rate-limit-token.json.123.{UUID32}.temp")
        order_stale = _write(
            tmp_path / f".kis-rate-limit-order.json.{DEAD_PID}.{UUID32}.tmp")

        _limiter(read_state).wait(category=CATEGORY_READ)

        assert token_bad.exists(), "another category's artifact was removed"
        assert order_stale.exists(), "another category's orphan was removed"

    def test_the_token_limiter_blocks_on_its_own_artifact(self, tmp_path, paced):
        token_state = _state(tmp_path, "kis-rate-limit-token.json")
        _write(tmp_path / f".kis-rate-limit-token.json.123.{UUID32}.temp")
        with pytest.raises(KISRateLimitTempArtifactError):
            _limiter(token_state).wait(category=CATEGORY_TOKEN)

    def test_a_well_formed_temp_of_a_longer_state_name_is_not_ours(self, tmp_path,
                                                                    paced):
        """".rate.json.bak..." starts with ".rate.json." but is a valid
        temporary of the state file "rate.json.bak"."""
        state = _state(tmp_path, "rate.json")
        theirs = _write(
            tmp_path / f".rate.json.bak.{DEAD_PID}.{UUID32}.tmp", "theirs")
        _limiter(state).wait(category=CATEGORY_READ)
        assert theirs.exists()


# --------------------------------------------------------------------------
# Several entries at once, and what happens afterwards.
# --------------------------------------------------------------------------

class TestMixedAndSubsequentBehaviour:
    def test_a_valid_orphan_is_not_cleaned_while_an_alien_entry_exists(
            self, state, paced, tmp_path):
        orphan = _write(state.with_name(_valid_temp_name(state)))
        malformed = _write(state.with_name(f".{state.name}.123.{UUID32}.temp"))
        outside = tmp_path.parent / "artifact-mixed-target.txt"
        outside.write_text("intact", encoding="utf-8")
        link = state.with_name(_valid_temp_name(state, token="d" * 32))
        os.symlink(outside, link)
        before = _entries(tmp_path)

        with pytest.raises(KISRateLimitTempArtifactError):
            _limiter(state).wait(category=CATEGORY_READ)

        assert _entries(tmp_path) == before, "a partial cleanup ran anyway"
        assert orphan.exists() and malformed.exists() and os.path.islink(link)
        assert outside.read_text(encoding="utf-8") == "intact"
        link.unlink()
        outside.unlink()

    def test_the_invalid_verdict_outranks_the_live_verdict(self, state, paced):
        _write(state.with_name(_valid_temp_name(state, pid=os.getpid())))
        _write(state.with_name(f".{state.name}.123.{UUID32}.temp"))
        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == REASON_TEMP_ARTIFACT_INVALID

    def test_the_limiter_stays_invalidated_until_it_is_replaced(self, state, paced):
        """The operator has to look at the file; a limiter that quietly
        recovered would hide that the directory had been written to."""
        bad = _write(state.with_name(f".{state.name}.123.{UUID32}.temp"))
        limiter = _limiter(state)
        with pytest.raises(KISRateLimitTempArtifactError):
            limiter.wait(category=CATEGORY_READ)

        bad.unlink()
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            limiter.wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == REASON_LIMITER_INVALIDATED

        # A fresh instance, i.e. the next run, works again.
        assert _limiter(state).wait(category=CATEGORY_READ) == 0.0

    def test_the_block_repeats_for_every_new_instance(self, state, paced):
        _write(state.with_name(f".{state.name}.123.{UUID32}.temp"))
        for _ in range(3):
            with pytest.raises(KISRateLimitTempArtifactError):
                _limiter(state).wait(category=CATEGORY_READ)

    def test_removing_the_artifact_restores_normal_operation(self, state, paced):
        bad = _write(state.with_name(f".{state.name}.123.{UUID32}.temp"))
        orphan = _write(state.with_name(_valid_temp_name(state)))
        with pytest.raises(KISRateLimitTempArtifactError):
            _limiter(state).wait(category=CATEGORY_READ)
        bad.unlink()
        _limiter(state).wait(category=CATEGORY_READ)
        assert not orphan.exists(), "the orphan was not cleaned once unblocked"

    def test_a_zero_interval_still_refuses_an_invalid_artifact(self, state,
                                                               monkeypatch):
        """Pacing switched off is not a reason to tolerate a stranger in
        the state directory."""
        monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "0")
        _write(state.with_name(f".{state.name}.123.{UUID32}.temp"))
        with pytest.raises(KISRateLimitTempArtifactError):
            _limiter(state).wait(category=CATEGORY_READ)


# --------------------------------------------------------------------------
# The scan itself failing.
# --------------------------------------------------------------------------

class TestScanFailuresFailClosed:
    def test_an_unlistable_directory_blocks(self, state, paced, monkeypatch):
        monkeypatch.setattr(
            Path, "iterdir",
            lambda self: (_ for _ in ()).throw(PermissionError(errno.EACCES, "no")))
        with pytest.raises(KISRateLimitArtifactScanError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == REASON_ARTIFACT_SCAN_FAILED

    def test_an_unstattable_entry_blocks(self, state, paced, monkeypatch):
        _write(state.with_name(_valid_temp_name(state)))
        real_lstat = os.lstat

        def _lstat(target, *args, **kwargs):
            if isinstance(target, str) and target.endswith(".tmp"):
                raise PermissionError(errno.EACCES, "denied")
            return real_lstat(target, *args, **kwargs)

        monkeypatch.setattr(kis_rate_limiter.os, "lstat", _lstat)
        with pytest.raises(KISRateLimitArtifactScanError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == REASON_ARTIFACT_SCAN_FAILED

    def test_a_vanished_entry_is_not_an_error(self, state, paced, monkeypatch):
        """It cannot be blocked on and cannot be deleted; there is nothing
        left to decide."""
        _write(state.with_name(_valid_temp_name(state)))
        real_lstat = os.lstat

        def _lstat(target, *args, **kwargs):
            if isinstance(target, str) and target.endswith(".tmp"):
                raise FileNotFoundError(errno.ENOENT, "gone")
            return real_lstat(target, *args, **kwargs)

        monkeypatch.setattr(kis_rate_limiter.os, "lstat", _lstat)
        _limiter(state).wait(category=CATEGORY_READ)

    def test_no_request_reaches_kis_after_a_scan_failure(self, state, paced,
                                                         broker_env, monkeypatch):
        control = _Session()
        KISBroker(session=control,
                  limiter=_limiter(state)).get_open_orders()
        assert control.calls

        monkeypatch.setattr(
            Path, "iterdir",
            lambda self: (_ for _ in ()).throw(PermissionError(errno.EACCES, "no")))
        session = _Session()
        broker = KISBroker(session=session, limiter=_limiter(state))
        with pytest.raises(KISRateLimitArtifactScanError):
            broker.get_open_orders()
        assert session.calls == []


# --------------------------------------------------------------------------
# The entry changing underneath the scan.
# --------------------------------------------------------------------------

class TestTypeChangeBetweenScanAndCleanup:
    def _swap_at_cleanup(self, monkeypatch, swap):
        """Runs `swap` when the cleanup opens its directory descriptor --
        i.e. after classification, before the re-check and the unlink."""
        real_open = os.open
        done = []

        def _open(*args, **kwargs):
            if not done:
                done.append(True)
                swap()
            return real_open(*args, **kwargs)

        monkeypatch.setattr(kis_rate_limiter.os, "open", _open)

    def test_a_regular_file_swapped_for_a_symlink_is_not_unlinked(
            self, state, paced, monkeypatch, tmp_path):
        outside = tmp_path.parent / "artifact-toctou-target.txt"
        outside.write_text("do not delete", encoding="utf-8")
        orphan = _write(state.with_name(_valid_temp_name(state)))

        def _swap():
            orphan.unlink()
            os.symlink(outside, orphan)

        self._swap_at_cleanup(monkeypatch, _swap)

        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.detail == "type_changed"
        assert os.path.islink(orphan), "the swapped-in symlink was unlinked"
        assert outside.exists() and outside.read_text(encoding="utf-8") == "do not delete"
        orphan.unlink()
        outside.unlink()

    def test_a_replaced_inode_is_not_unlinked(self, state, paced, monkeypatch):
        """Same name, different file: the scan's verdict was about the
        inode it saw, not about the name."""
        orphan = _write(state.with_name(_valid_temp_name(state)))
        replacement = _write(state.with_name("replacement.txt"), "new content")

        def _swap():
            os.replace(replacement, orphan)

        self._swap_at_cleanup(monkeypatch, _swap)

        with pytest.raises(KISRateLimitTempArtifactError) as excinfo:
            _limiter(state).wait(category=CATEGORY_READ)
        assert excinfo.value.detail == "type_changed"
        assert orphan.read_text(encoding="utf-8") == "new content"

    def test_an_entry_that_vanishes_before_the_unlink_is_fine(self, state, paced,
                                                              monkeypatch):
        orphan = _write(state.with_name(_valid_temp_name(state)))
        self._swap_at_cleanup(monkeypatch, orphan.unlink)
        _limiter(state).wait(category=CATEGORY_READ)
        assert not orphan.exists()

    def test_the_unlink_goes_through_a_directory_descriptor(self):
        """A path-based unlink can have a parent component swapped between
        the check and the call."""
        assert "os.unlink(artifact.name, dir_fd=dir_fd)" in LIMITER_SOURCE
        assert "os.lstat(artifact.name, dir_fd=dir_fd)" in LIMITER_SOURCE


# --------------------------------------------------------------------------
# What an operator is told.
# --------------------------------------------------------------------------

class TestAlerting:
    def _capture(self, monkeypatch):
        sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda m: sent.append(m) or True)
        return sent

    def test_the_alert_carries_the_classification_and_no_path(self, state, paced,
                                                              monkeypatch):
        sent = self._capture(monkeypatch)
        _write(state.with_name(f".{state.name}.123.{UUID32}.temp"))
        with pytest.raises(KISRateLimitTempArtifactError):
            _limiter(state).wait(category=CATEGORY_READ)
        joined = "\n".join(sent)
        assert "INVALID_TEMP_ARTIFACT" in joined
        assert "malformed_filename" in joined
        assert "transport_suppressed=true" in joined
        assert CATEGORY_READ in joined
        assert str(state.parent) not in joined

    def test_the_alert_never_names_the_symlink_target(self, state, paced,
                                                      monkeypatch, tmp_path):
        sent = self._capture(monkeypatch)
        outside = tmp_path.parent / "artifact-secret-target.txt"
        outside.write_text("x", encoding="utf-8")
        link = state.with_name(_valid_temp_name(state, token="8" * 32))
        os.symlink(outside, link)
        with pytest.raises(KISRateLimitTempArtifactError):
            _limiter(state).wait(category=CATEGORY_READ)
        joined = "\n".join(sent)
        assert "symlink" in joined
        assert str(outside) not in joined
        assert outside.name not in joined
        link.unlink()
        outside.unlink()

    def test_the_alert_masks_the_file_name(self, state, paced, monkeypatch):
        sent = self._capture(monkeypatch)
        name = f".{state.name}.123.{UUID32}.temp"
        _write(state.with_name(name))
        with pytest.raises(KISRateLimitTempArtifactError):
            _limiter(state).wait(category=CATEGORY_READ)
        assert name not in "\n".join(sent)
