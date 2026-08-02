"""The four HIGH findings from the independent Oracle re-verification.

ORACLE-HIGH-01  account reads filtered on OVRS_EXCG_CD=NASD, hiding every
                NYSE and AMEX position, order and fill from reconciliation
ORACLE-HIGH-02  an empty / truncated / future rate-limit state granted a
                READ immediately, silently disabling cross-process pacing
ORACLE-HIGH-03  a rate-limited ORDER was recorded as a confirmed REJECTED
ORACLE-HIGH-04  a token cache created in the FUTURE was used verbatim
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from brokers import kis_rate_limiter, kis_token_cache
from brokers.kis_broker import (
    KISAccountSweepError,
    KISAmbiguousResponseError,
    KISBroker,
)
from brokers.kis_rate_limiter import (
    CATEGORY_READ,
    RATE_LIMIT_MSG_CD,
    KisRateLimiter,
    KISRateLimitStateInvalid,
)
from brokers.kis_token_cache import KISTokenCache
from domain.exchange import supported_kis_order_exchange_codes

REPO_ROOT = Path(__file__).resolve().parent.parent
ALL_VENUES = ["NASD", "NYSE", "AMEX"]

RATE_LIMITED = {"rt_cd": "1", "msg_cd": RATE_LIMIT_MSG_CD,
                "msg1": "초당 거래건수를 초과하였습니다."}


# =====================================================  ORACLE-HIGH-01

class _SweepSession:
    """Returns a different row per venue so a missing leg is visible."""

    def __init__(self, fail_on=None, per_venue=None):
        self.fail_on = fail_on
        self.per_venue = per_venue or {}
        self.exchange_params = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        class _R:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        if url.endswith("/oauth2/tokenP"):
            return _R(200, {"access_token": "t", "expires_in": 86400})
        code = (params or {}).get("OVRS_EXCG_CD")
        self.exchange_params.append(code)
        if code == self.fail_on:
            return _R(500, {"rt_cd": "1", "msg_cd": "EGW00999", "msg1": "boom"})
        return _R(200, self.per_venue.get(code, {"rt_cd": "0", "output": [], "output1": [],
                                                 "output2": {}}))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("KIS_ENV", "live")
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNT_READ_ENABLED", "true")
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")


class TestAccountReadsSweepEveryVenue:
    def test_the_supported_code_list_is_derived_centrally(self):
        assert supported_kis_order_exchange_codes() == ("NASD", "NYSE", "AMEX")

    @pytest.mark.parametrize("call", [
        lambda b: b.get_account_snapshot(),
        lambda b: b.get_positions(),
        lambda b: b.get_open_orders(),
        lambda b: b.get_fills(start_date="20260701", end_date="20260729"),
    ])
    def test_each_of_the_four_reads_queries_all_three(self, env, call):
        """The regression: all four used to send NASD and nothing else."""
        session = _SweepSession()
        call(KISBroker(session=session))
        assert session.exchange_params == ALL_VENUES, session.exchange_params

    def test_no_read_sends_only_nasd(self, env):
        session = _SweepSession()
        broker = KISBroker(session=session)
        broker.get_positions()
        broker.get_open_orders()
        broker.get_fills(start_date="20260701", end_date="20260729")
        assert session.exchange_params.count("NASD") == 3
        assert session.exchange_params.count("NYSE") == 3
        assert session.exchange_params.count("AMEX") == 3

    def test_nyse_positions_are_no_longer_invisible(self, env):
        session = _SweepSession(per_venue={
            "NASD": {"rt_cd": "0", "output1": [
                {"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "5", "pchs_avg_pric": "300",
                 "evlu_pfls_amt": "1"}], "output2": {}},
            "NYSE": {"rt_cd": "0", "output1": [
                {"ovrs_pdno": "BBVA", "ovrs_cblc_qty": "10", "pchs_avg_pric": "27",
                 "evlu_pfls_amt": "2"}], "output2": {}},
            "AMEX": {"rt_cd": "0", "output1": [], "output2": {}},
        })
        positions = KISBroker(session=session).get_positions()
        symbols = sorted(p.symbol for p in positions)
        assert symbols == ["AAPL", "BBVA"], "a NYSE holding was dropped"

    def test_open_orders_merge_across_venues(self, env):
        session = _SweepSession(per_venue={
            "NASD": {"rt_cd": "0", "output": [{"odno": "A"}]},
            "NYSE": {"rt_cd": "0", "output": [{"odno": "B"}]},
            "AMEX": {"rt_cd": "0", "output": [{"odno": "C"}]},
        })
        orders = KISBroker(session=session).get_open_orders()
        assert sorted(o["odno"] for o in orders) == ["A", "B", "C"]

    def test_a_row_repeated_within_one_venue_is_merged(self, env):
        """Pagination can echo the same order inside ONE venue's response;
        two copies would look like two live orders to reconciliation.

        Across venues is a different matter -- see
        test_the_same_order_number_on_two_venues_is_two_orders: KIS filters
        by venue, so the same odno arriving under two different filters is
        two orders, and identity is (venue, odno) per the directive."""
        session = _SweepSession(per_venue={
            "NASD": {"rt_cd": "0", "output": [{"odno": "DUP"}, {"odno": "DUP"}]},
            "NYSE": {"rt_cd": "0", "output": []},
            "AMEX": {"rt_cd": "0", "output": []},
        })
        orders = KISBroker(session=session).get_open_orders()
        assert len(orders) == 1

    def test_rows_keep_the_venue_they_came_from(self, env):
        session = _SweepSession(per_venue={
            "NASD": {"rt_cd": "0", "output": []},
            "NYSE": {"rt_cd": "0", "output": [{"odno": "B"}]},
            "AMEX": {"rt_cd": "0", "output": []},
        })
        orders = KISBroker(session=session).get_open_orders()
        assert orders[0]["kis_exchange_code"] == "NYSE"
        assert orders[0]["canonical_exchange"] == "NYSE"

    def test_an_empty_account_is_not_an_error(self, env):
        session = _SweepSession()
        assert KISBroker(session=session).get_open_orders() == []
        assert KISBroker(session=session).get_positions() == []

    @pytest.mark.parametrize("failing", ALL_VENUES)
    def test_one_failing_venue_fails_the_whole_read(self, env, failing):
        """A partial account must never be returned as a complete one."""
        session = _SweepSession(fail_on=failing)
        with pytest.raises(KISAccountSweepError) as excinfo:
            KISBroker(session=session).get_positions()
        assert excinfo.value.exchange_code == failing
        assert excinfo.value.reason_code == "KIS_EXCHANGE_LEG_FAILED"

    def test_a_partial_failure_leaves_no_fresh_snapshot(self, env):
        """NASDAQ ok, NYSE down, AMEX ok -> reconciliation records
        nothing, so the order gates stay shut."""
        from reconciliation import snapshot as reconciliation_snapshot

        session = _SweepSession(fail_on="NYSE")
        broker = KISBroker(session=session)
        with pytest.raises(reconciliation_snapshot.ReconciliationUnavailableError) as excinfo:
            reconciliation_snapshot.build_snapshot(
                broker=broker, conn=None, account_id="1", symbol=None, now=None,
                source="test",
            )
        assert excinfo.value.reason_code == "KIS_EXCHANGE_LEG_FAILED"


class TestNoHardcodedNasdInReads:
    def test_the_account_read_methods_contain_no_nasd_literal(self):
        import ast
        import inspect

        from brokers import kis_broker

        for name in ("get_account_snapshot", "get_positions", "get_open_orders",
                     "get_fills"):
            source = inspect.getsource(getattr(kis_broker.KISBroker, name))
            literals = [
                node.value for node in ast.walk(ast.parse(textwrap.dedent(source)))
                if isinstance(node, ast.Constant) and node.value == "NASD"
            ]
            assert literals == [], f"{name} still hardcodes NASD"


# =====================================================  ORACLE-HIGH-02

class Clock:
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


def _limiter(path, clock):
    instance = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
    instance._wall = clock.time
    return instance


@pytest.fixture
def paced(monkeypatch):
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
    monkeypatch.setenv("KIS_RATE_LIMIT_MAX_CLOCK_SKEW_SECONDS", "5")


class TestLimiterInvalidStateFailsClosed:
    def test_a_missing_file_is_a_legitimate_first_run(self, tmp_path, clock, paced):
        limiter = _limiter(tmp_path / "absent.json", clock)
        assert limiter.wait(category=CATEGORY_READ) == 0.0

    @pytest.mark.parametrize("content,label", [
        ("", "empty"),
        ("   \n", "whitespace"),
        ('{"READ": 100', "truncated"),
        ("[]", "array"),
        ('"a string"', "string"),
        ("null", "null"),
        ("123", "number"),
    ])
    def test_a_corrupt_file_blocks_rather_than_waving_it_through(
            self, tmp_path, clock, paced, content, label):
        """The exact fail-open: each of these returned waited=0.0s."""
        path = tmp_path / "rate.json"
        path.write_text(content, encoding="utf-8")
        limiter = _limiter(path, clock)
        with pytest.raises(KISRateLimitStateInvalid) as excinfo:
            limiter.wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_STATE_INVALID"
        assert clock.slept == [], f"{label}: a request was allowed through"

    @pytest.mark.parametrize("value", [
        "not-a-number", None, True, float("nan"), float("inf"), float("-inf"), -1.0,
    ])
    def test_an_unusable_timestamp_blocks(self, tmp_path, clock, paced, value):
        path = tmp_path / "rate.json"
        path.write_text(json.dumps({CATEGORY_READ: value}), encoding="utf-8")
        with pytest.raises(KISRateLimitStateInvalid):
            _limiter(path, clock).wait(category=CATEGORY_READ)

    def test_a_future_timestamp_blocks(self, tmp_path, clock, paced):
        path = tmp_path / "rate.json"
        path.write_text(json.dumps({CATEGORY_READ: clock.now + 3600}), encoding="utf-8")
        with pytest.raises(KISRateLimitStateInvalid) as excinfo:
            _limiter(path, clock).wait(category=CATEGORY_READ)
        assert excinfo.value.detail == "future_timestamp"
        assert clock.slept == []

    def test_skew_within_tolerance_waits_the_full_interval(self, tmp_path, clock, paced):
        """Slightly ahead is tolerated as clock noise -- but it buys no
        head start: the caller still waits the whole interval."""
        path = tmp_path / "rate.json"
        path.write_text(json.dumps({CATEGORY_READ: clock.now + 2}), encoding="utf-8")
        waited = _limiter(path, clock).wait(category=CATEGORY_READ)
        assert waited == 3.0

    def test_a_valid_state_still_paces_normally(self, tmp_path, clock, paced):
        path = tmp_path / "rate.json"
        limiter = _limiter(path, clock)
        limiter.wait(category=CATEGORY_READ)
        assert limiter.wait(category=CATEGORY_READ) == 3.0

    def test_no_kis_request_is_made_when_the_state_is_invalid(self, tmp_path, clock,
                                                              paced, env):
        """Fail-closed means transport 0, not "log and continue"."""
        path = tmp_path / "rate.json"
        path.write_text("{broken", encoding="utf-8")
        session = _SweepSession()
        broker = KISBroker(session=session, limiter=_limiter(path, clock))
        with pytest.raises(KISRateLimitStateInvalid) as excinfo:
            broker.get_open_orders()
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_STATE_INVALID"
        assert session.exchange_params == [], "a KIS request went out anyway"

    def test_two_processes_both_refuse_a_corrupt_state(self, tmp_path, clock, paced):
        path = tmp_path / "rate.json"
        path.write_text('{"READ": ', encoding="utf-8")
        for _ in range(2):
            with pytest.raises(KISRateLimitStateInvalid):
                _limiter(path, clock).wait(category=CATEGORY_READ)
        assert clock.slept == []


# =====================================================  ORACLE-HIGH-03

class _OrderSession:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.order_calls = 0

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        class _R:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        if url.endswith("/oauth2/tokenP"):
            return _R(200, {"access_token": "t", "expires_in": 86400})
        self.order_calls += 1
        return _R(self.status, self.body)


def _order_bits():
    from datetime import datetime, timezone

    from domain.instrument import build_instrument
    from domain.order_intent import OrderIntent

    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    intent = OrderIntent(
        internal_order_id="o1", signal_id="s1", strategy_id="st", symbol="AAPL",
        exchange="NASDAQ", side="buy", quantity=1, order_type="limit", limit_price=100.0,
        stop_price=95.0, target_price=110.0, created_at=now,
    )
    return intent, build_instrument("AAPL", exchange="NASDAQ")


class TestRateLimitedOrderIsUnknownNotRejected:
    def _broker(self, session, monkeypatch):
        monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "true")
        return KISBroker(session=session)

    def test_order_rate_limit_is_ambiguous(self, env, monkeypatch):
        """The regression: this used to return status=REJECTED, durably
        asserting a rejection KIS never confirmed."""
        from execution import authorization as authz

        intent, instrument = _order_bits()
        session = _OrderSession(RATE_LIMITED)
        broker = self._broker(session, monkeypatch)
        monkeypatch.setattr(authz, "consume", lambda *a, **k: None)

        with pytest.raises(KISAmbiguousResponseError) as excinfo:
            broker.submit_order(intent, instrument, authorization=object())
        assert RATE_LIMIT_MSG_CD in str(excinfo.value)
        assert "reconciliation" in str(excinfo.value).lower()
        assert session.order_calls == 1, "the order was re-sent"

    def test_cancel_rate_limit_is_ambiguous(self, env, monkeypatch):
        from execution import authorization as authz

        intent, instrument = _order_bits()
        session = _OrderSession(RATE_LIMITED)
        broker = self._broker(session, monkeypatch)
        monkeypatch.setattr(authz, "consume", lambda *a, **k: None)

        with pytest.raises(KISAmbiguousResponseError):
            broker.cancel_order(intent, instrument, "kis-1", authorization=object())
        assert session.order_calls == 1, "the cancel was re-sent"

    def test_a_genuine_rejection_is_still_rejected(self, env, monkeypatch):
        """The distinction must survive: only EGW00201 is ambiguous."""
        from execution import authorization as authz

        intent, instrument = _order_bits()
        session = _OrderSession({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "bad order"})
        broker = self._broker(session, monkeypatch)
        monkeypatch.setattr(authz, "consume", lambda *a, **k: None)

        record = broker.submit_order(intent, instrument, authorization=object())
        assert record.status == "REJECTED"
        assert record.error_code == "EGW00123"

    def test_the_engine_turns_it_into_UNKNOWN(self):
        """The ambiguous type is the engine's existing UNKNOWN pathway --
        asserted here so the two halves cannot drift apart."""
        import inspect

        from execution import execution_engine

        for name in ("_submit_new_order", "_cancel_inner"):
            source = inspect.getsource(getattr(execution_engine, name))
            assert "KISAmbiguousResponseError" in source
            assert "_force_unknown" in source


class TestReadRateLimitRegression:
    def test_reads_still_retry_with_backoff(self, tmp_path, clock, monkeypatch):
        """ORDER/CANCEL classification must not have changed READ."""
        monkeypatch.setenv("KIS_RATE_LIMIT_BASE_BACKOFF_SECONDS", "3.0")
        monkeypatch.setenv("KIS_RATE_LIMIT_MAX_BACKOFF_SECONDS", "15.0")
        monkeypatch.setenv("KIS_RATE_LIMIT_MAX_RETRIES", "3")
        monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "0")
        limiter = _limiter(tmp_path / "r.json", clock)
        attempts = {"n": 0}

        def _flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise kis_rate_limiter.KISRateLimitSignal()
            return "ok"

        assert limiter.call_with_retry(_flaky, category=CATEGORY_READ) == "ok"
        assert [s for s in clock.slept if s] == [3.0, 6.0]


# =====================================================  ORACLE-HIGH-04

class _TokenClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class _Config:
    kis_env = "live"
    base_url = "https://openapi.koreainvestment.com:9443"
    app_key = "APP-KEY"
    app_secret = "SECRET"


def _write_cache(path, clock, **overrides):
    payload = {
        "access_token": "FUTURE-TOKEN", "token_type": "Bearer",
        "created_at": clock.now, "expires_at": clock.now + 86400,
        "environment": "live",
        "base_url": "https://openapi.koreainvestment.com:9443",
        "app_key_fingerprint": kis_token_cache.app_key_fingerprint("APP-KEY"),
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


class TestFutureTokenIsRefused:
    def _cache(self, tmp_path, clock):
        return KISTokenCache(path=tmp_path / "token.json", clock=clock)

    @pytest.mark.parametrize("ahead,usable", [
        (0, True), (4, True), (5, True), (6, False), (3600, False),
    ])
    def test_created_at_skew_boundary(self, tmp_path, monkeypatch, ahead, usable):
        monkeypatch.setenv("KIS_TOKEN_MAX_CLOCK_SKEW_SECONDS", "5")
        clock = _TokenClock()
        path = tmp_path / "token.json"
        _write_cache(path, clock, created_at=clock.now + ahead)
        counter = {"n": 0}

        def _issue():
            counter["n"] += 1
            return "FRESH", "Bearer", 86400

        token = KISTokenCache(path=path, clock=clock).get_or_issue(_Config(), _issue)
        if usable:
            assert token == "FUTURE-TOKEN" and counter["n"] == 0
        else:
            assert token == "FRESH", "a future-dated token was used"
            assert counter["n"] == 1

    @pytest.mark.parametrize("overrides,label", [
        ({"created_at": None}, "missing created_at"),
        ({"created_at": "soon"}, "non-numeric created_at"),
        ({"created_at": float("nan")}, "NaN created_at"),
        ({"created_at": float("inf")}, "inf created_at"),
        ({"expires_at": float("nan")}, "NaN expires_at"),
        ({"expires_at": float("inf")}, "inf expires_at"),
    ])
    def test_unusable_time_fields_are_a_miss(self, tmp_path, overrides, label):
        clock = _TokenClock()
        path = tmp_path / "token.json"
        _write_cache(path, clock, **overrides)
        counter = {"n": 0}
        token = KISTokenCache(path=path, clock=clock).get_or_issue(
            _Config(), lambda: (counter.__setitem__("n", counter["n"] + 1), "FRESH",
                                "Bearer", 86400)[1:])
        assert token == "FRESH", label
        assert counter["n"] == 1

    def test_expiry_before_creation_is_refused(self, tmp_path):
        clock = _TokenClock()
        path = tmp_path / "token.json"
        _write_cache(path, clock, expires_at=clock.now - 10)
        counter = {"n": 0}
        token = KISTokenCache(path=path, clock=clock).get_or_issue(
            _Config(), lambda: (counter.__setitem__("n", counter["n"] + 1), "FRESH",
                                "Bearer", 86400)[1:])
        assert token == "FRESH"

    def test_an_implausible_lifetime_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIS_TOKEN_MAX_LIFETIME_SECONDS", "90000")
        clock = _TokenClock()
        path = tmp_path / "token.json"
        _write_cache(path, clock, expires_at=clock.now + 86400 * 30)
        counter = {"n": 0}
        token = KISTokenCache(path=path, clock=clock).get_or_issue(
            _Config(), lambda: (counter.__setitem__("n", counter["n"] + 1), "FRESH",
                                "Bearer", 86400)[1:])
        assert token == "FRESH", "a 30-day 'KIS token' was accepted"

    def test_the_replacement_is_written_and_reusable(self, tmp_path):
        clock = _TokenClock()
        path = tmp_path / "token.json"
        _write_cache(path, clock, created_at=clock.now + 9999)
        cache = KISTokenCache(path=path, clock=clock)
        cache.get_or_issue(_Config(), lambda: ("FRESH", "Bearer", 86400))
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["access_token"] == "FRESH"
        assert stored["created_at"] <= clock.now
        counter = {"n": 0}
        again = cache.get_or_issue(
            _Config(), lambda: (counter.__setitem__("n", counter["n"] + 1), "X",
                                "Bearer", 86400)[1:])
        assert again == "FRESH" and counter["n"] == 0

    def test_no_token_value_reaches_the_log(self, tmp_path, caplog):
        clock = _TokenClock()
        path = tmp_path / "token.json"
        _write_cache(path, clock, created_at=clock.now + 9999)
        with caplog.at_level("WARNING"):
            KISTokenCache(path=path, clock=clock).get_or_issue(
                _Config(), lambda: ("FRESH", "Bearer", 86400))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "FUTURE-TOKEN" not in blob
        assert "SECRET" not in blob
        assert "future" in blob.lower()


_HERD = textwrap.dedent(
    """
    import json, os, sys
    sys.path.insert(0, sys.argv[1])
    from brokers.kis_token_cache import KISTokenCache

    class C:
        kis_env = "live"
        base_url = "https://openapi.koreainvestment.com:9443"
        app_key = "APP-KEY"
        app_secret = "SECRET"

    marker = sys.argv[3]

    def issue():
        fd = os.open(marker, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.write(fd, b"issued\\n")
        os.close(fd)
        return "REPLACEMENT", "Bearer", 86400

    print(KISTokenCache(path=sys.argv[2]).get_or_issue(C(), issue), flush=True)
    """
)


class TestConcurrentRefreshOfAFutureCache:
    def test_ten_processes_replace_it_once(self, tmp_path):
        """A poisoned cache must not become ten token requests either."""
        import time as _time

        path = tmp_path / "token.json"
        marker = tmp_path / "issued.log"
        marker.write_text("", encoding="utf-8")
        now = _time.time()
        path.write_text(json.dumps({
            "access_token": "FUTURE-TOKEN", "token_type": "Bearer",
            "created_at": now + 86400, "expires_at": now + 86400 * 2,
            "environment": "live",
            "base_url": "https://openapi.koreainvestment.com:9443",
            "app_key_fingerprint": kis_token_cache.app_key_fingerprint("APP-KEY"),
        }), encoding="utf-8")

        children = [
            subprocess.Popen([sys.executable, "-c", _HERD, str(REPO_ROOT), str(path),
                              str(marker)], stdout=subprocess.PIPE, text=True)
            for _ in range(10)
        ]
        outputs = [c.communicate(timeout=180)[0].strip().splitlines()[-1]
                   for c in children]
        assert all(out == "REPLACEMENT" for out in outputs), outputs
        issued = len([ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln])
        assert issued == 1, f"{issued} processes issued a token, expected 1"
