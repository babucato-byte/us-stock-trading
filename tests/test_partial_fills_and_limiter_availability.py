"""Two regressions introduced by the venue-sweep and pacing work.

ORACLE-HIGH-1  the multi-venue merge deduplicated FILLS by odno, so a
               partially-filled order's 2-share and 3-share rows became
               one 2-share row -- silently undoing CODEX-045
ORACLE-HIGH-2  a permission error on the shared limiter state fell back
               to a LOCAL sleep and then sent the request anyway, which
               is not cross-process pacing at all
"""
import errno
import json
import os
import subprocess
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from brokers import kis_rate_limiter
from brokers.kis_broker import KISBroker
from brokers.kis_rate_limiter import (
    CATEGORY_CANCEL,
    CATEGORY_ORDER,
    CATEGORY_READ,
    CATEGORY_TOKEN,
    KisRateLimiter,
    KISRateLimitStateUnavailable,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("KIS_ENV", "live")
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNT_READ_ENABLED", "true")
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")


class _Session:
    def __init__(self, per_venue):
        self.per_venue = per_venue
        self.venues = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        class R:
            def __init__(self, body):
                self.status_code = 200
                self._b = body
                self.text = str(body)

            def json(self):
                return self._b

        if url.endswith("/oauth2/tokenP"):
            return R({"access_token": "t", "expires_in": 86400})
        code = (params or {}).get("OVRS_EXCG_CD")
        self.venues.append(code)
        return R(self.per_venue.get(code, {"rt_cd": "0", "output": []}))


def _fill(odno, qty, price="10.0", **extra):
    row = {"odno": odno, "ft_ccld_qty": str(qty), "ft_ccld_unpr3": str(price)}
    row.update(extra)
    return row


def _fills_from(nasd=(), nyse=(), amex=()):
    return {
        "NASD": {"rt_cd": "0", "output": list(nasd)},
        "NYSE": {"rt_cd": "0", "output": list(nyse)},
        "AMEX": {"rt_cd": "0", "output": list(amex)},
    }


def _get_fills(env_unused, per_venue):
    session = _Session(per_venue)
    broker = KISBroker(session=session)
    return broker.get_fills(start_date="20260701", end_date="20260729")


# =====================================================  ORACLE-HIGH-1

class TestPartialFillsSurviveTheMerge:
    def test_control_a_single_fill_still_comes_back(self, env):
        fills = _get_fills(env, _fills_from(nasd=[_fill("1001", 2)]))
        assert len(fills) == 1

    def test_two_fills_for_one_order_are_both_kept(self, env):
        """The regression: 2 + 3 became 2."""
        fills = _get_fills(env, _fills_from(
            nasd=[_fill("1001", 2), _fill("1001", 3)]))
        assert len(fills) == 2, "a partial fill was deduplicated away"
        total = sum(int(f["ft_ccld_qty"]) for f in fills)
        assert total == 5, total

    def test_an_exactly_duplicated_row_is_removed_once(self, env):
        """Pagination can echo the SAME execution -- that one is a dupe."""
        row = _fill("1001", 2)
        fills = _get_fills(env, _fills_from(nasd=[dict(row), dict(row)]))
        assert len(fills) == 1
        assert sum(int(f["ft_ccld_qty"]) for f in fills) == 2

    def test_same_quantity_different_sequence_is_two_executions(self, env):
        fills = _get_fills(env, _fills_from(nasd=[
            _fill("1001", 2, ccld_seq="1"),
            _fill("1001", 2, ccld_seq="2"),
        ]))
        assert len(fills) == 2
        assert sum(int(f["ft_ccld_qty"]) for f in fills) == 4

    def test_same_quantity_different_time_is_two_executions(self, env):
        fills = _get_fills(env, _fills_from(nasd=[
            _fill("1001", 2, ccld_tm="090000"),
            _fill("1001", 2, ccld_tm="093000"),
        ]))
        assert len(fills) == 2

    def test_same_odno_on_two_venues_stays_separate(self, env):
        fills = _get_fills(env, _fills_from(
            nasd=[_fill("1001", 2)], nyse=[_fill("1001", 3)]))
        assert len(fills) == 2
        assert sum(int(f["ft_ccld_qty"]) for f in fills) == 5
        assert {f["kis_exchange_code"] for f in fills} == {"NASD", "NYSE"}

    def test_tagging_does_not_change_execution_identity(self, env):
        """The venue tags this module adds must not make two identical
        rows look different (or two different rows look the same)."""
        row = _fill("1001", 2)
        fills = _get_fills(env, _fills_from(nasd=[dict(row), dict(row), dict(row)]))
        assert len(fills) == 1

    def test_open_orders_still_dedupe_by_order_number(self, env):
        """Orders keep the odno rule -- only FILLS changed."""
        session = _Session({
            "NASD": {"rt_cd": "0", "output": [{"odno": "1001"}, {"odno": "1001"}]},
            "NYSE": {"rt_cd": "0", "output": []},
            "AMEX": {"rt_cd": "0", "output": []},
        })
        orders = KISBroker(session=session).get_open_orders()
        assert len(orders) == 1

    def test_the_same_order_number_on_two_venues_is_two_orders(self, env):
        session = _Session({
            "NASD": {"rt_cd": "0", "output": [{"odno": "1001"}]},
            "NYSE": {"rt_cd": "0", "output": [{"odno": "1001"}]},
            "AMEX": {"rt_cd": "0", "output": []},
        })
        assert len(KISBroker(session=session).get_open_orders()) == 2


class TestDownstreamQuantitiesAreCorrect:
    """The fills feed CODEX-045's partial-fill accounting; these assert
    the numbers that accounting produces are right again."""

    def _sum(self, fills):
        return sum(int(f["ft_ccld_qty"]) for f in fills)

    def test_partially_filled_is_detected(self, env):
        fills = _get_fills(env, _fills_from(nasd=[_fill("1001", 2), _fill("1001", 3)]))
        ordered, filled = 10, self._sum(fills)
        assert filled == 5
        assert 0 < filled < ordered
        assert ordered - filled == 5

    def test_exactly_filled_is_detected(self, env):
        fills = _get_fills(env, _fills_from(nasd=[_fill("1001", 2), _fill("1001", 3)]))
        assert self._sum(fills) == 5   # ordered == 5 -> FILLED

    def test_overfill_is_visible_rather_than_clamped(self, env):
        fills = _get_fills(env, _fills_from(nasd=[_fill("1001", 2), _fill("1001", 3)]))
        ordered = 4
        filled = self._sum(fills)
        assert filled == 5
        assert filled > ordered, "an overfill must stay visible, not be clamped"

    def test_weighted_average_price(self, env):
        fills = _get_fills(env, _fills_from(nasd=[
            _fill("1001", 2, price="10.0"), _fill("1001", 3, price="12.0")]))
        qty = sum(Decimal(f["ft_ccld_qty"]) for f in fills)
        notional = sum(Decimal(f["ft_ccld_qty"]) * Decimal(f["ft_ccld_unpr3"])
                       for f in fills)
        assert qty == 5
        assert notional / qty == Decimal("11.2"), "a simple mean would give 11.0"

    def test_the_existing_partial_fill_guard_still_holds(self, env):
        """CODEX-045's own case: 2 ordered, 1 filled must not look full."""
        fills = _get_fills(env, _fills_from(nasd=[_fill("kis-999", 1)]))
        assert self._sum(fills) == 1
        assert self._sum(fills) < 2


# =====================================================  ORACLE-HIGH-2

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
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "3.0")
    monkeypatch.setenv("KIS_TOKEN_MIN_INTERVAL_SECONDS", "3.0")
    monkeypatch.setenv("KIS_ORDER_MIN_INTERVAL_SECONDS", "3.0")


def _limiter(path, clock):
    inst = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
    inst._wall = clock.time
    return inst


class TestSharedLimiterFailuresBlock:
    def test_control_a_writable_state_paces_normally(self, tmp_path, clock, paced):
        limiter = _limiter(tmp_path / "rate.json", clock)
        assert limiter.wait(category=CATEGORY_READ) == 0.0
        assert limiter.wait(category=CATEGORY_READ) == 3.0

    def test_an_unwritable_directory_blocks(self, tmp_path, clock, paced):
        """The regression: this used to sleep locally and continue."""
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            limiter = _limiter(locked / "sub" / "rate.json", clock)
            with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
                limiter.wait(category=CATEGORY_READ)
            assert excinfo.value.reason_code == "KIS_RATE_LIMIT_STATE_UNAVAILABLE"
            assert clock.slept == [], "a local fallback sleep happened"
        finally:
            os.chmod(locked, 0o700)

    def test_an_unopenable_lock_file_blocks(self, tmp_path, clock, paced, monkeypatch):
        path = tmp_path / "rate.json"
        real_open = open

        def _deny(file, *a, **k):
            if str(file).endswith(".lock"):
                raise PermissionError(errno.EACCES, "denied")
            return real_open(file, *a, **k)

        monkeypatch.setattr("builtins.open", _deny)
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            _limiter(path, clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_LOCK_FAILED"
        assert clock.slept == []

    def test_a_failing_flock_blocks(self, tmp_path, clock, paced, monkeypatch):
        monkeypatch.setattr(
            "fcntl.flock",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EAGAIN, "busy")),
        )
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            _limiter(tmp_path / "rate.json", clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_LOCK_FAILED"

    def test_an_unreadable_state_file_blocks(self, tmp_path, clock, paced):
        """The file exists but cannot be opened for reading."""
        path = tmp_path / "rate.json"
        path.write_text('{"READ": 900}', encoding="utf-8")
        os.chmod(path, 0o000)
        try:
            with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
                _limiter(path, clock).wait(category=CATEGORY_READ)
            assert excinfo.value.reason_code == "KIS_RATE_LIMIT_STATE_UNAVAILABLE"
            assert clock.slept == []
        finally:
            os.chmod(path, 0o600)

    def test_a_failing_write_blocks_before_transport(self, tmp_path, clock, paced,
                                                     monkeypatch):
        """The budget must be durable BEFORE the request goes out."""
        path = tmp_path / "rate.json"
        monkeypatch.setattr(
            "json.dump",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EROFS, "read-only")),
        )
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            _limiter(path, clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_PERSISTENCE"

    def test_a_failing_fsync_blocks(self, tmp_path, clock, paced, monkeypatch):
        monkeypatch.setattr(
            "os.fsync",
            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "no fsync")),
        )
        with pytest.raises(KISRateLimitStateUnavailable) as excinfo:
            _limiter(tmp_path / "rate.json", clock).wait(category=CATEGORY_READ)
        assert excinfo.value.reason_code == "KIS_RATE_LIMIT_PERSISTENCE"

    @pytest.mark.parametrize("category", [CATEGORY_READ, CATEGORY_TOKEN,
                                          CATEGORY_ORDER, CATEGORY_CANCEL])
    def test_every_category_fails_closed(self, tmp_path, clock, paced, category):
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            with pytest.raises(KISRateLimitStateUnavailable):
                _limiter(locked / "x" / "rate.json", clock).wait(category=category)
        finally:
            os.chmod(locked, 0o700)

    def test_no_local_fallback_remains_in_the_source(self):
        source = (REPO_ROOT / "brokers" / "kis_rate_limiter.py").read_text(encoding="utf-8")
        assert "_wait_without_shared_state" not in source
        assert "pacing locally" not in source

    def test_no_kis_request_is_sent_when_the_limiter_is_unavailable(
            self, tmp_path, clock, paced, env):
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            session = _Session({})
            broker = KISBroker(session=session,
                               limiter=_limiter(locked / "x" / "rate.json", clock))
            with pytest.raises(KISRateLimitStateUnavailable):
                broker.get_open_orders()
            assert session.venues == [], "a KIS request went out anyway"
        finally:
            os.chmod(locked, 0o700)

    def test_two_processes_both_block(self, tmp_path, clock, paced):
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            blocked = 0
            for _ in range(2):
                try:
                    _limiter(locked / "x" / "rate.json", clock).wait(category=CATEGORY_READ)
                except KISRateLimitStateUnavailable:
                    blocked += 1
            assert blocked == 2
            assert clock.slept == [], "a local fallback sleep happened"
        finally:
            os.chmod(locked, 0o700)

    def test_the_alert_carries_no_path_or_os_detail(self, tmp_path, clock, paced,
                                                    monkeypatch):
        sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda m: sent.append(m) or True)
        locked = tmp_path / "secretdir"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            with pytest.raises(KISRateLimitStateUnavailable):
                _limiter(locked / "x" / "rate.json", clock).wait(category=CATEGORY_READ)
        finally:
            os.chmod(locked, 0o700)
        joined = "\n".join(sent)
        assert joined, "no operator alert was raised"
        assert "secretdir" not in joined
        assert str(locked) not in joined
        assert "READ" in joined


class TestReconciliationBlocksOnLimiterFailure:
    def test_no_fresh_snapshot_when_the_limiter_is_unavailable(self, tmp_path, clock,
                                                               paced, env):
        from reconciliation import snapshot as recon

        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            broker = KISBroker(session=_Session({}),
                               limiter=_limiter(locked / "x" / "rate.json", clock))
            with pytest.raises(recon.ReconciliationUnavailableError) as excinfo:
                recon.build_snapshot(broker=broker, conn=None, account_id="1",
                                     symbol=None, now=None, source="test")
            assert excinfo.value.reason_code == "KIS_RATE_LIMIT_STATE_UNAVAILABLE"
        finally:
            os.chmod(locked, 0o700)


_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, sys.argv[1])
    os.environ["KIS_READ_MIN_INTERVAL_SECONDS"] = "3.0"
    from brokers.kis_rate_limiter import (
        CATEGORY_READ, KisRateLimiter, KISRateLimitStateUnavailable,
    )
    try:
        KisRateLimiter(path=sys.argv[2]).wait(category=CATEGORY_READ)
        print("ALLOWED", flush=True)
    except KISRateLimitStateUnavailable:
        print("BLOCKED", flush=True)
    """
)


class TestRealProcessesBlock:
    def test_four_processes_all_block_on_the_same_permission_error(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            target = locked / "sub" / "rate.json"
            children = [
                subprocess.Popen([sys.executable, "-c", _CHILD, str(REPO_ROOT),
                                  str(target)], stdout=subprocess.PIPE, text=True)
                for _ in range(4)
            ]
            outs = [c.communicate(timeout=120)[0].strip().splitlines()[-1]
                    for c in children]
            assert outs == ["BLOCKED"] * 4, outs
        finally:
            os.chmod(locked, 0o700)
