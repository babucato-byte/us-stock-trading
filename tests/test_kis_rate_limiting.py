"""HIGH-2: KIS calls are paced, and only reads are ever retried.

Oracle read-only verification found reconciliation failing on every run
with EGW00201 ("초당 거래건수를 초과하였습니다") while an independent
probe that put 3 seconds between the SAME endpoints succeeded on all of
them. A control also ruled out token timing: a token request followed
immediately by ONE read succeeded, so the problem is consecutive reads.

Every test here drives a virtual clock -- nothing really sleeps.
"""
import json

import pytest

from brokers import kis_rate_limiter
from brokers.kis_rate_limiter import (
    CATEGORY_CANCEL,
    CATEGORY_ORDER,
    CATEGORY_READ,
    CATEGORY_TOKEN,
    RATE_LIMIT_MSG_CD,
    KisRateLimiter,
    KISRateLimitError,
    KISRateLimitSignal,
)

RATE_LIMITED_BODY = {
    "rt_cd": "1", "msg_cd": RATE_LIMIT_MSG_CD,
    "msg1": "초당 거래건수를 초과하였습니다.", "message": RATE_LIMIT_MSG_CD,
}


class Clock:
    """A virtual clock: wall time advances only when something sleeps."""

    def __init__(self):
        self.now = 1_000.0
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
def limiter(tmp_path, clock, monkeypatch):
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
    monkeypatch.setenv("KIS_TOKEN_MIN_INTERVAL_SECONDS", "60.0")
    monkeypatch.setenv("KIS_ORDER_MIN_INTERVAL_SECONDS", "1.0")
    monkeypatch.setenv("KIS_RATE_LIMIT_MAX_RETRIES", "3")
    monkeypatch.setenv("KIS_RATE_LIMIT_BASE_BACKOFF_SECONDS", "3.0")
    monkeypatch.setenv("KIS_RATE_LIMIT_MAX_BACKOFF_SECONDS", "15.0")
    instance = KisRateLimiter(path=tmp_path / "rate.json", clock=clock.time,
                              sleeper=clock.sleep)
    instance._wall = clock.time
    return instance


class TestRateLimitDetection:
    @pytest.mark.parametrize("body", [
        RATE_LIMITED_BODY,
        {"msg_cd": RATE_LIMIT_MSG_CD},
        {"message": RATE_LIMIT_MSG_CD},
        {"error_code": RATE_LIMIT_MSG_CD},
    ])
    def test_the_oracle_response_is_recognized(self, body):
        assert kis_rate_limiter.is_rate_limited(body) is True

    @pytest.mark.parametrize("body", [
        {"rt_cd": "0", "output": {"last": "308.9100"}},
        {"msg_cd": "MCA00000"},
        {"error_code": "EGW00133"},   # the token 1/minute limit, not this
        None, [], "text", 42,
    ])
    def test_everything_else_is_not(self, body):
        assert kis_rate_limiter.is_rate_limited(body) is False


class TestPacing:
    def test_the_first_call_does_not_wait(self, limiter, clock):
        assert limiter.wait(category=CATEGORY_READ) == 0.0
        assert clock.slept == []

    def test_consecutive_reads_are_spaced(self, limiter, clock):
        """The exact reconciliation sequence: balance -> open orders ->
        fills, which on Oracle went out with no spacing at all."""
        start = clock.now
        stamps = []
        for _ in range(3):
            limiter.wait(category=CATEGORY_READ)
            stamps.append(clock.now - start)
        assert stamps == [0.0, 3.0, 6.0], stamps

    def test_an_elapsed_interval_is_not_re_waited(self, limiter, clock):
        limiter.wait(category=CATEGORY_READ)
        clock.now += 10.0          # something else took a while
        assert limiter.wait(category=CATEGORY_READ) == 0.0

    def test_categories_have_independent_budgets(self, limiter, clock):
        """A read must not consume the token budget or vice versa."""
        limiter.wait(category=CATEGORY_TOKEN)
        assert limiter.wait(category=CATEGORY_READ) == 0.0
        assert clock.slept == []

    def test_the_token_category_uses_its_own_longer_interval(self, limiter, clock):
        limiter.wait(category=CATEGORY_TOKEN)
        limiter.wait(category=CATEGORY_TOKEN)
        assert clock.slept == [60.0]

    def test_a_future_timestamp_blocks_rather_than_bursting(self, limiter, clock, tmp_path):
        """ORACLE-HIGH-02 corrected this test's own expectation. It used
        to accept "does not crash"; an independent probe then showed the
        call returning immediately with no wait, which silently disables
        pacing. A future timestamp is now a hard stop."""
        limiter.wait(category=CATEGORY_READ)
        state = tmp_path / "rate.json"
        state.write_text(json.dumps({CATEGORY_READ: clock.now + 500}), encoding="utf-8")
        before = list(clock.slept)
        with pytest.raises(kis_rate_limiter.KISRateLimitStateInvalid) as excinfo:
            limiter.wait(category=CATEGORY_READ)
        assert excinfo.value.detail == "future_timestamp"
        assert clock.slept == before, "a request was allowed through"


class TestCrossProcessSharing:
    def test_a_second_limiter_honours_the_first_ones_timestamp(self, tmp_path, clock,
                                                               monkeypatch):
        """Separate systemd units are separate PROCESSES -- the budget
        must live in the shared file, not in each process."""
        monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
        path = tmp_path / "rate.json"
        first = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
        first._wall = clock.time
        second = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
        second._wall = clock.time

        first.wait(category=CATEGORY_READ)          # no wait
        waited = second.wait(category=CATEGORY_READ)
        assert waited == 3.0, "the second process ignored the shared budget"

    def test_four_concurrent_processes_still_serialize(self, tmp_path, clock, monkeypatch):
        monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
        path = tmp_path / "rate.json"
        limiters = []
        for _ in range(4):
            instance = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
            instance._wall = clock.time
            limiters.append(instance)
        start = clock.now
        for instance in limiters:
            instance.wait(category=CATEGORY_READ)
        assert clock.now - start == 9.0, "four processes did not serialize"

    def test_a_corrupt_state_file_blocks_the_request(self, tmp_path, clock, monkeypatch):
        """ORACLE-HIGH-02: this test previously asserted only that the
        file was rewritten, which the probe showed happened WITHOUT any
        wait -- corrupt state was indistinguishable from a fresh budget.
        Corrupt now stops the request outright."""
        monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
        path = tmp_path / "rate.json"
        path.write_text("{not json", encoding="utf-8")
        instance = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
        instance._wall = clock.time
        with pytest.raises(kis_rate_limiter.KISRateLimitStateInvalid):
            instance.wait(category=CATEGORY_READ)
        assert clock.slept == []

    def test_the_state_file_holds_no_secret(self, limiter, tmp_path):
        limiter.wait(category=CATEGORY_READ)
        limiter.wait(category=CATEGORY_TOKEN)
        blob = (tmp_path / "rate.json").read_text(encoding="utf-8")
        state = json.loads(blob)
        # Structurally incapable of holding a secret: the only keys are
        # category names plus the schema version, and every value is a
        # number.
        assert set(state) <= set(kis_rate_limiter.CATEGORIES) | {"version"}
        assert state["version"] == kis_rate_limiter.STATE_VERSION
        assert all(isinstance(v, (int, float)) for v in state.values()), state
        for forbidden in ("appkey", "appsecret", "bearer", "cano", "access_token"):
            assert forbidden not in blob.lower()


class TestRetryPolicy:
    def test_a_read_retries_with_the_documented_backoff(self, limiter, clock):
        attempts = {"n": 0}

        def _flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise KISRateLimitSignal()
            return "ok"

        assert limiter.call_with_retry(_flaky, category=CATEGORY_READ) == "ok"
        backoffs = [s for s in clock.slept if s in (3.0, 6.0, 12.0)]
        assert backoffs == [3.0, 6.0], backoffs

    def test_a_read_that_never_clears_becomes_KIS_RATE_LIMIT(self, limiter):
        def _always():
            raise KISRateLimitSignal()

        with pytest.raises(KISRateLimitError) as excinfo:
            limiter.call_with_retry(_always, category=CATEGORY_READ, describe="balance")
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT"
        assert excinfo.value.attempts == 4     # first try + 3 retries

    @pytest.mark.parametrize("category", [CATEGORY_ORDER, CATEGORY_CANCEL])
    def test_orders_and_cancels_are_NEVER_retried(self, limiter, category):
        """A rate-limited order may already have reached KIS. Re-sending
        it could double the position, so one attempt only."""
        attempts = {"n": 0}

        def _rate_limited():
            attempts["n"] += 1
            raise KISRateLimitSignal()

        with pytest.raises(KISRateLimitError):
            limiter.call_with_retry(_rate_limited, category=category)
        assert attempts["n"] == 1, "an order/cancel was re-sent after a rate limit"

    def test_a_non_rate_limit_error_is_not_retried(self, limiter):
        attempts = {"n": 0}

        def _broken():
            attempts["n"] += 1
            raise ValueError("something else")

        with pytest.raises(ValueError):
            limiter.call_with_retry(_broken, category=CATEGORY_READ)
        assert attempts["n"] == 1

    def test_backoff_is_capped(self, limiter, monkeypatch):
        monkeypatch.setenv("KIS_RATE_LIMIT_MAX_RETRIES", "5")
        monkeypatch.setenv("KIS_RATE_LIMIT_MAX_BACKOFF_SECONDS", "10.0")
        assert kis_rate_limiter.backoff_delays() == [3.0, 6.0, 10.0, 10.0, 10.0]


class _RateLimitedThenOk:
    """A session that returns EGW00201 for the first N reads."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        class _Response:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        if url.endswith("/oauth2/tokenP"):
            return _Response(200, {"access_token": "t", "expires_in": 86400})
        self.calls += 1
        if self.calls <= self.fail_times:
            return _Response(500, RATE_LIMITED_BODY)
        return _Response(200, {"rt_cd": "0", "output": {"last": "308.9100"}})


@pytest.fixture
def kis_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_ENV", "live")
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNT_READ_ENABLED", "true")
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
    monkeypatch.setenv("KIS_RATE_LIMIT_BASE_BACKOFF_SECONDS", "3.0")


class TestBrokerIntegration:
    def _broker(self, session, tmp_path, clock):
        from brokers.kis_broker import KISBroker

        limiter = KisRateLimiter(path=tmp_path / "rate.json", clock=clock.time,
                                 sleeper=clock.sleep)
        limiter._wall = clock.time
        return KISBroker(session=session, limiter=limiter)

    def test_a_rate_limited_read_recovers(self, kis_env, tmp_path, clock):
        from domain.instrument import build_instrument

        session = _RateLimitedThenOk(fail_times=2)
        broker = self._broker(session, tmp_path, clock)
        price = broker.get_current_price(build_instrument("AAPL", exchange="NASDAQ"))
        assert price == 308.91
        assert session.calls == 3

    def test_a_persistent_rate_limit_raises_KIS_RATE_LIMIT(self, kis_env, tmp_path, clock):
        from domain.instrument import build_instrument

        session = _RateLimitedThenOk(fail_times=99)
        broker = self._broker(session, tmp_path, clock)
        with pytest.raises(KISRateLimitError) as excinfo:
            broker.get_current_price(build_instrument("AAPL", exchange="NASDAQ"))
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT"

    def test_consecutive_reads_through_the_broker_are_spaced(self, kis_env, tmp_path, clock):
        """The reconciliation sequence, end to end."""
        session = _RateLimitedThenOk(fail_times=0)
        broker = self._broker(session, tmp_path, clock)
        start = clock.now
        for _ in range(3):
            broker._get("/x", "TR", {})
        assert clock.now - start == 6.0


class TestReconciliationFailsClosed:
    def test_a_rate_limited_read_leaves_the_snapshot_unrecorded(self):
        """CODEX-044 stays intact: a failed read must not refresh the
        clean timestamp the order gates rely on."""
        from reconciliation import snapshot as reconciliation_snapshot

        class _Broker:
            config = type("C", (), {"account_no": "1"})()

            def get_positions(self):
                raise KISRateLimitError("limited", category=CATEGORY_READ, attempts=4)

        with pytest.raises(reconciliation_snapshot.ReconciliationUnavailableError) as excinfo:
            reconciliation_snapshot.build_snapshot(
                broker=_Broker(), conn=None, account_id="1", symbol=None,
                now=None, source="test",
            )
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT"
