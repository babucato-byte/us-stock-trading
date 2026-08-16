"""MEDIUM: one KIS token shared by every process on the box.

Oracle verification hit the real limit: KIS issues at most one token a
minute and answers the second with

    HTTP 403  EGW00133  "접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"

Each systemd unit is its own process, so a per-instance cache meant a
fresh issuance per unit and a guaranteed collision when two ran close
together.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from brokers import kis_token_cache
from brokers.kis_token_cache import KISTokenCache, app_key_fingerprint

REPO_ROOT = Path(__file__).resolve().parent.parent

SECRET = "SUPER-SECRET-APP-SECRET-VALUE"
APP_KEY = "APP-KEY-0123456789"


class _Config:
    kis_env = "live"
    base_url = "https://openapi.koreainvestment.com:9443"
    app_key = APP_KEY
    app_secret = SECRET
    account_no = "12345678"


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cache(tmp_path, clock):
    return KISTokenCache(path=tmp_path / "token.json", clock=clock)


def _issuer(counter, token="TOKEN-1", expires_in=86400):
    def _issue():
        counter["n"] += 1
        return token, "Bearer", expires_in
    return _issue


class TestBasicCaching:
    def test_a_cold_cache_issues_once(self, cache):
        counter = {"n": 0}
        assert cache.get_or_issue(_Config(), _issuer(counter)) == "TOKEN-1"
        assert counter["n"] == 1

    def test_a_second_call_reuses(self, cache):
        counter = {"n": 0}
        cache.get_or_issue(_Config(), _issuer(counter))
        assert cache.get_or_issue(_Config(), _issuer(counter)) == "TOKEN-1"
        assert counter["n"] == 1, "the cached token was not reused"

    def test_a_second_CACHE_OBJECT_reuses_too(self, tmp_path, clock):
        """Stands in for a second process: a brand-new cache object with
        no memory of the first must still find the token."""
        counter = {"n": 0}
        first = KISTokenCache(path=tmp_path / "token.json", clock=clock)
        second = KISTokenCache(path=tmp_path / "token.json", clock=clock)
        first.get_or_issue(_Config(), _issuer(counter))
        second.get_or_issue(_Config(), _issuer(counter))
        assert counter["n"] == 1

    def test_an_expired_token_is_reissued(self, cache, clock):
        counter = {"n": 0}
        cache.get_or_issue(_Config(), _issuer(counter, expires_in=3600))
        clock.now += 3600
        cache.get_or_issue(_Config(), _issuer(counter, token="TOKEN-2"))
        assert counter["n"] == 2

    def test_the_refresh_skew_reissues_early(self, cache, clock, monkeypatch):
        monkeypatch.setenv("KIS_TOKEN_REFRESH_SKEW_SECONDS", "300")
        counter = {"n": 0}
        cache.get_or_issue(_Config(), _issuer(counter, expires_in=3600))
        clock.now += 3600 - 299          # inside the 5-minute skew
        cache.get_or_issue(_Config(), _issuer(counter, token="TOKEN-2"))
        assert counter["n"] == 2, "the token was used inside its refresh skew"

    def test_just_outside_the_skew_still_reuses(self, cache, clock, monkeypatch):
        monkeypatch.setenv("KIS_TOKEN_REFRESH_SKEW_SECONDS", "300")
        counter = {"n": 0}
        cache.get_or_issue(_Config(), _issuer(counter, expires_in=3600))
        clock.now += 3600 - 301
        cache.get_or_issue(_Config(), _issuer(counter))
        assert counter["n"] == 1


class TestIdentityBinding:
    def _stored(self, cache, config=None):
        counter = {"n": 0}
        cache.get_or_issue(config or _Config(), _issuer(counter))
        return counter

    def test_a_different_app_key_does_not_reuse(self, cache):
        counter = self._stored(cache)

        class _Other(_Config):
            app_key = "A-COMPLETELY-DIFFERENT-KEY"

        cache.get_or_issue(_Other(), _issuer(counter, token="TOKEN-2"))
        assert counter["n"] == 2, "a token was replayed under another credential"

    def test_a_different_environment_does_not_reuse(self, cache):
        counter = self._stored(cache)

        class _Paper(_Config):
            kis_env = "paper"
            base_url = "https://openapivts.koreainvestment.com:29443"

        cache.get_or_issue(_Paper(), _issuer(counter, token="TOKEN-2"))
        assert counter["n"] == 2, "a live token was replayed against the paper server"

    def test_the_fingerprint_is_not_reversible(self):
        fingerprint = app_key_fingerprint(APP_KEY)
        assert APP_KEY not in fingerprint
        assert len(fingerprint) == 16
        assert app_key_fingerprint(APP_KEY) == fingerprint      # stable
        assert app_key_fingerprint("other") != fingerprint      # distinguishing


class TestCorruptionHandling:
    @pytest.mark.parametrize("content", [
        "", "   ", "{not json", "[]", "null", "42",
        '{"access_token": ""}',
        '{"access_token": "t"}',                       # no expiry
        '{"expires_at": 99999999999}',                 # no token
        '{"access_token": "t", "expires_at": "soon"}',  # non-numeric expiry
    ])
    def test_every_broken_shape_is_a_miss_not_a_token(self, tmp_path, clock, content):
        path = tmp_path / "token.json"
        path.write_text(content, encoding="utf-8")
        cache = KISTokenCache(path=path, clock=clock)
        counter = {"n": 0}
        assert cache.get_or_issue(_Config(), _issuer(counter)) == "TOKEN-1"
        assert counter["n"] == 1

    def test_a_corrupt_cache_is_replaced_by_a_good_one(self, tmp_path, clock):
        path = tmp_path / "token.json"
        path.write_text("{broken", encoding="utf-8")
        cache = KISTokenCache(path=path, clock=clock)
        counter = {"n": 0}
        cache.get_or_issue(_Config(), _issuer(counter))
        assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "TOKEN-1"

    def test_an_unreadable_cache_does_not_crash_the_caller(self, tmp_path, clock):
        path = tmp_path / "token.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o000)
        try:
            cache = KISTokenCache(path=path, clock=clock)
            counter = {"n": 0}
            assert cache.get_or_issue(_Config(), _issuer(counter)) == "TOKEN-1"
        finally:
            os.chmod(path, 0o600)


class TestRedaction:
    def test_the_secret_is_never_written(self, cache, tmp_path):
        counter = {"n": 0}
        cache.get_or_issue(_Config(), _issuer(counter))
        blob = (tmp_path / "token.json").read_text(encoding="utf-8")
        assert SECRET not in blob
        assert APP_KEY not in blob
        assert "12345678" not in blob, "the account number was persisted"

    def test_only_the_documented_fields_are_stored(self, cache, tmp_path):
        counter = {"n": 0}
        cache.get_or_issue(_Config(), _issuer(counter))
        stored = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
        assert set(stored) == {
            "access_token", "token_type", "expires_at", "created_at",
            "environment", "base_url", "app_key_fingerprint",
        }

    def test_the_file_is_owner_only(self, cache, tmp_path):
        counter = {"n": 0}
        cache.get_or_issue(_Config(), _issuer(counter))
        mode = (tmp_path / "token.json").stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)


_HERD_CHILD = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, sys.argv[1])
    from brokers.kis_token_cache import KISTokenCache

    class C:
        kis_env = "live"
        base_url = "https://openapi.koreainvestment.com:9443"
        app_key = "APP-KEY-0123456789"
        app_secret = "s"

    marker = sys.argv[3]

    def issue():
        # Record the issuance in a way every process can append to.
        fd = os.open(marker, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.write(fd, b"issued\\n")
        os.close(fd)
        return "SHARED-TOKEN", "Bearer", 86400

    cache = KISTokenCache(path=sys.argv[2])
    token = cache.get_or_issue(C(), issue)
    print(token, flush=True)
    """
)


class TestConcurrentProcesses:
    def test_ten_processes_issue_at_most_one_token(self, tmp_path):
        """The real herd: ten cold starts at once must not become ten
        token requests -- that is exactly what tripped EGW00133."""
        cache_path = tmp_path / "token.json"
        marker = tmp_path / "issued.log"
        marker.write_text("", encoding="utf-8")

        children = [
            subprocess.Popen(
                [sys.executable, "-c", _HERD_CHILD, str(REPO_ROOT), str(cache_path),
                 str(marker)],
                stdout=subprocess.PIPE, text=True,
            )
            for _ in range(10)
        ]
        outputs = [child.communicate(timeout=120)[0].strip() for child in children]

        assert all(out == "SHARED-TOKEN" for out in outputs), outputs
        issuances = len([ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln])
        assert issuances == 1, f"{issuances} processes issued a token, expected exactly 1"

    def test_the_cache_survives_for_the_next_process(self, tmp_path):
        cache_path = tmp_path / "token.json"
        marker = tmp_path / "issued.log"
        marker.write_text("", encoding="utf-8")
        for _ in range(3):
            result = subprocess.run(
                [sys.executable, "-c", _HERD_CHILD, str(REPO_ROOT), str(cache_path),
                 str(marker)],
                capture_output=True, text=True, timeout=120,
            )
            assert result.stdout.strip() == "SHARED-TOKEN", result.stderr[-300:]
        issuances = len([ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln])
        assert issuances == 1


class TestBrokerUsesTheCache:
    def test_two_brokers_share_one_issuance(self, tmp_path, monkeypatch, clock):
        """A second KISBroker stands in for a second service process."""
        from brokers.kis_broker import KISBroker

        monkeypatch.setenv("KIS_ENV", "live")
        monkeypatch.setenv("KIS_APP_KEY", APP_KEY)
        monkeypatch.setenv("KIS_APP_SECRET", SECRET)
        monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
        monkeypatch.setenv("KIS_ACCOUNT_READ_ENABLED", "true")
        monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")

        issued = {"n": 0}

        class _Session:
            def request(self, method, url, headers=None, params=None, json=None, timeout=None):
                class _R:
                    status_code = 200
                    text = ""

                    @staticmethod
                    def json():
                        return ({"access_token": "T", "expires_in": 86400}
                                if url.endswith("/oauth2/tokenP")
                                else {"rt_cd": "0", "output": {"last": "1.0"}})
                if url.endswith("/oauth2/tokenP"):
                    issued["n"] += 1
                return _R()

        shared = KISTokenCache(path=tmp_path / "token.json", clock=clock)
        for _ in range(2):
            broker = KISBroker(session=_Session(), token_cache=shared)
            broker._auth_headers("TR")
        assert issued["n"] == 1, "each broker issued its own token"
