"""The collector as a service: one of it, restartable, and never in the
trading path.

Three properties, each of which failed somewhere else in this system
before it was written down:

  * ONE collector. Two writing one snapshot file would each persist
    their own view of the session and the last writer would win --
    producing a volume belonging to no measurement anyone made.
  * A dropped socket reconnects. KIS closes idle connections; a
    collector that exited on the first drop would leave the rest of the
    session with no volume, which is precisely the state that stops S6
    trading premarket.
  * It never touches a trading resource. The 2026-08-27 starvation was
    a market-data-shaped workload competing for the broker rate-limit
    lock, and this is the workload that shape describes.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "_collector", REPO_ROOT / "scripts" / "run_realtime_bar_collector.py")
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)

WRAPPER = (REPO_ROOT / "deploy" / "cron" / "s6_realtime_collector.sh").read_text(
    encoding="utf-8")
RUNNER = (REPO_ROOT / "scripts" / "run_realtime_bar_collector.py").read_text(
    encoding="utf-8")


class TestOnlyOneCollectorRuns:
    def test_the_lock_is_held_for_the_process_lifetime(self, tmp_path):
        lock = tmp_path / "collector.lock"
        handle = collector.acquire_singleton(lock)
        assert handle is not None
        with pytest.raises(collector.AlreadyRunning):
            collector.acquire_singleton(lock)
        handle.close()

    def test_releasing_lets_the_next_one_start(self, tmp_path):
        lock = tmp_path / "collector.lock"
        first = collector.acquire_singleton(lock)
        first.close()
        second = collector.acquire_singleton(lock)
        assert second is not None
        second.close()

    def test_the_lock_records_the_owning_pid(self, tmp_path):
        import os

        lock = tmp_path / "collector.lock"
        handle = collector.acquire_singleton(lock)
        assert lock.read_text().strip() == str(os.getpid())
        handle.close()

    def test_a_refused_start_is_not_reported_as_success(self):
        assert "return 2" in RUNNER
        block = RUNNER[RUNNER.index("except AlreadyRunning"):]
        assert "refusing to start" in block[:300]

    def test_the_wrapper_also_checks_before_starting(self):
        assert "pgrep -f \"run_realtime_bar_collector.py\"" in WRAPPER


class TestItSurvivesADroppedFeed:
    def test_it_reconnects_rather_than_exiting(self):
        assert "reconnecting" in RUNNER
        assert "reconnects" in RUNNER

    def test_a_reconnect_resumes_the_existing_accumulator(self):
        """Restarting the accumulation would reset the session's volume
        to zero, which is the reading that must never come from our own
        process management."""
        assert "resume=store" in RUNNER
        assert "resume if resume is not None else _load(path)" in RUNNER

    def test_the_backoff_is_bounded(self):
        assert "min(5.0 * reconnects, 30.0)" in RUNNER

    def test_the_keepalive_is_answered(self):
        assert "is_pingpong(message)" in RUNNER
        block = RUNNER[RUNNER.index("is_pingpong(message)"):]
        assert "ws.send_text(message)" in block[:200]


class TestItIsNotInTheTradingPath:
    def test_it_never_takes_the_broker_rate_limit_lock(self):
        for forbidden in ("kis_rate_limiter", "get_limiter", "call_with_retry"):
            assert forbidden not in RUNNER, forbidden

    def test_it_never_reaches_an_execution_path(self):
        for forbidden in ("submit_buy_order", "submit_sell_order",
                          "execution_engine", "order_gate", "KISBroker"):
            assert forbidden not in RUNNER, forbidden

    def test_it_does_not_open_the_order_database(self):
        assert "state_store" not in RUNNER
        assert "order_repository" not in RUNNER

    def test_the_wrapper_labels_it_for_the_lock_telemetry(self):
        """It should not appear as an owner, but if it ever does the
        telemetry must name it rather than reporting UNKNOWN."""
        assert "KIS_LOCK_OWNER=S6_COLLECTOR" in WRAPPER


class TestItRunsVerifiedCode:
    def test_it_resolves_the_release_like_every_other_cron(self):
        assert "resolve_release_root || exit 1" in WRAPPER

    def test_there_is_no_mutable_fallback(self):
        code = "\n".join(l for l in WRAPPER.splitlines()
                         if not l.strip().startswith("#"))
        assert "/home/ubuntu/trading" not in code

    def test_it_loads_the_credentials_before_the_release_check(self):
        code = "\n".join(l for l in WRAPPER.splitlines()
                         if not l.strip().startswith("#"))
        assert code.index("ENV_FILE=") < code.index("resolve_release_root")


class TestTheWatchlistFollowsTheScanner:
    def test_symbols_come_from_the_published_candidates(self):
        """A hand-maintained list drifts from what S6 is actually
        watching, and the drift is invisible until a candidate has no
        volume for a reason nobody can find."""
        assert "publisher.read" in WRAPPER
        assert "scan_session.session_at()" in WRAPPER

    def test_no_candidates_means_no_collector(self):
        assert "no published candidates for this session" in WRAPPER

    def test_the_symbol_count_is_bounded(self):
        assert "out[:40]" in WRAPPER
