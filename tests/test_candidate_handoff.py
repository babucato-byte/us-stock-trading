"""One directory, or a refusal. The producer/consumer hand-off.

What went wrong
---------------
`s6_scan.sh` runs in the scanner CHECKOUT and never read the release's
shared env, so `SCANNER_CANDIDATE_DIR` was unset and `candidate_dir()`
fell back to `analytics_dir()/candidates`. In production that resolved
to two different absolute paths:

    producer  /home/ubuntu/trading/logs/scanners/candidates
    consumer  /home/ubuntu/releases/us-stock-trading/shared/state/candidates

Neither side errored. The scan published successfully into a directory
nobody reads, and the executor would have found an empty one and called
it a quiet market. S2 had the identical split.

The fix is two-sided and both sides are tested here: the shell resolves
the store from the release env and refuses without it, and Python refuses
to guess a path at all. Either alone would leave the other free to
reintroduce it.

The integration test at the bottom is the one that would actually have
caught this: it publishes as the producer and reads as the consumer,
through their real modules, and asserts the candidate arrives.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_sessions  # noqa: E402
from config import scanner_live_mode as slm  # noqa: E402
from scanners.base import run_context  # noqa: E402
from scanners.publish import candidates as publisher  # noqa: E402

CRON_DIR = REPO_ROOT / "deploy" / "cron"
SHARED_ENV_SH = CRON_DIR / "shared_env.sh"

DAY = "2026-08-24"
T0 = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)


class Signal:
    def __init__(self, symbol, score=70.0, price=100.0):
        self.symbol, self.scanner_score, self.signal_price = symbol, score, price
        self.scanner_name, self.scanner_version = "orb", "orb_v1.0"
        self.signal_id, self.scanner_run_id = f"s-{symbol}", "run-1"
        self.volume = self.avg_volume = self.volume_multiple = None
        self.price_change_pct = self.hma200 = self.hma200_slope = None
        self.hma89 = self.vwap = None
        self.market_data_provider = self.market_data_feed = None
        self.data_timestamp = self.feature_timestamp = None
        self.source_timeframe = self.timestamp = None
        self.reasons = []
        self.metrics = {"opening_range_high": 99.5, "opening_range_low": 99.0,
                        "orb_minutes": 15, "vwap": 100.0, "price": price}


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    """Every test here decides its own environment.

    Without this the developer's shell or the autouse analytics fixture
    could supply one of the two variables and quietly turn a refusal test
    into a resolution test.
    """
    monkeypatch.delenv(publisher.CANDIDATE_DIR_ENV, raising=False)
    monkeypatch.delenv("TRADING_PROJECT_ROOT", raising=False)


# ====================================================================
# C. an explicit directory is used exactly as given
# ====================================================================
class TestExplicitConfigurationWins:
    def test_the_configured_path_is_used_verbatim(self, monkeypatch, tmp_path):
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "store"))
        assert publisher.candidate_dir() == tmp_path / "store"

    def test_surrounding_whitespace_is_not_a_different_directory(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, f"  {tmp_path}  ")
        assert publisher.candidate_dir() == tmp_path

    def test_it_beats_the_project_root(self, monkeypatch, tmp_path):
        shared = tmp_path / "shared" / "state" / "candidates"
        shared.mkdir(parents=True)
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "sha"))
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "other"))
        assert publisher.candidate_dir() == tmp_path / "other"


# ====================================================================
# A/B. the shared store, and the refusal to invent one
# ====================================================================
class TestTheSharedStoreIsResolvedOrRefused:
    def test_a_project_root_beside_a_shared_store_resolves(self, monkeypatch,
                                                            tmp_path):
        shared = tmp_path / "shared" / "state" / "candidates"
        shared.mkdir(parents=True)
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "abc123"))
        assert publisher.candidate_dir() == shared

    def test_a_project_root_without_one_is_refused(self, monkeypatch, tmp_path):
        """The production case. It used to return a checkout-local path."""
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "trading"))
        with pytest.raises(publisher.CandidateHandoffMisconfigured):
            publisher.candidate_dir()

    def test_nothing_configured_is_refused(self):
        with pytest.raises(publisher.CandidateHandoffMisconfigured):
            publisher.candidate_dir()

    def test_the_refusal_carries_a_reason_code(self):
        assert (publisher.CandidateHandoffMisconfigured.reason_code
                == run_context.PUBLICATION_CONFIG_ERROR)

    def test_it_never_creates_the_directory_to_make_itself_right(
            self, monkeypatch, tmp_path):
        """Existence is the check. Creating it would manufacture a second
        empty store on any host whose layout differs -- which is the
        failure, not the fix."""
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "abc123"))
        with pytest.raises(publisher.CandidateHandoffMisconfigured):
            publisher.candidate_dir()
        assert not (tmp_path / "shared").exists()

    def test_it_matches_the_other_two_stores(self, monkeypatch, tmp_path):
        """The KIS and S1 stores resolve `<releases>/shared/state` from
        the same TRADING_PROJECT_ROOT. This is that path plus the
        publisher's own subdirectory, not a third convention."""
        from market_data import candidate_store

        monkeypatch.delenv(candidate_store.CANDIDATE_DIR_ENV, raising=False)
        shared = tmp_path / "shared" / "state"
        (shared / "candidates").mkdir(parents=True)
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "abc123"))
        assert publisher.candidate_dir().parent == candidate_store.candidate_dir()


# ====================================================================
# D. the .ran marker and the .jsonl live together
# ====================================================================
class TestTheMarkerAndTheFileAreOnePair:
    @pytest.fixture
    def store(self, monkeypatch, tmp_path):
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "s"))
        return tmp_path / "s"

    def test_both_land_in_the_shared_directory(self, store):
        publisher.publish([Signal("AAPL")], strategy_id=s6_sessions.STRATEGY_ID,
                          trading_day=DAY, session="REGULAR", variant="S6-R")
        publisher.mark_run(DAY, "REGULAR",
                           strategy_id=s6_sessions.STRATEGY_ID, candidates=1)
        rows = publisher.candidates_path(DAY, "REGULAR")
        marker = publisher.run_marker_path(DAY, "REGULAR")
        assert rows.parent == marker.parent == store
        assert rows.exists() and marker.exists()

    def test_the_marker_is_derived_from_the_file_not_recomputed(self, store):
        """One resolution, so the two cannot end up in different places
        if the directory ever moves."""
        rows = publisher.candidates_path(DAY, "REGULAR")
        marker = publisher.run_marker_path(DAY, "REGULAR")
        assert str(marker) == str(rows) + publisher.RUN_MARKER_SUFFIX

    def test_the_scan_cycle_lock_is_in_the_same_directory(self, store):
        """The consumer has to be able to SEE the lock, so it lives with
        the files it protects rather than beside the producer."""
        from scanners.publish import scan_cycle

        assert scan_cycle.cycle_path(DAY, "REGULAR", "orb").parent == store


# ====================================================================
# E/F. the round trip, through the real producer and consumer
# ====================================================================
class TestTheHandoffActuallyDelivers:
    @pytest.fixture
    def shared(self, monkeypatch, tmp_path):
        """One shared store, configured the way production configures it."""
        store = tmp_path / "releases" / "shared" / "state" / "candidates"
        store.mkdir(parents=True)
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(store))
        return store

    def _publish(self, symbols, session="REGULAR", variant="S6-R", day=DAY):
        publisher.publish([Signal(s) for s in symbols],
                          strategy_id=s6_sessions.STRATEGY_ID, trading_day=day,
                          session=session, variant=variant)
        publisher.mark_run(day, session, strategy_id=s6_sessions.STRATEGY_ID,
                           candidates=len(symbols), status="OK")

    def _source(self, session="REGULAR", day=DAY):
        from s6_live import candidate_source

        modes = dict(slm.SCANNER_LIVE_MODE)
        modes["orb"] = slm.MODE_LIMITED_LIVE
        return candidate_source.S6CandidateSource(
            trading_day=day, session=session, modes=modes)

    def test_what_the_producer_wrote_is_what_the_consumer_reads(self, shared):
        """The test that would have caught the production split."""
        self._publish(["AAPL", "MSFT"])
        source = self._source()
        assert source.symbols() == ["AAPL", "MSFT"]
        assert source.qualify("AAPL").qualified is True
        assert source.describe()["refusal"] is None

    def test_the_two_sides_resolve_the_identical_absolute_path(self, shared):
        from s6_live import final_check
        from scanners.publish import scan_cycle

        self._publish(["AAPL"])
        producer = str(publisher.candidate_dir())
        lock = str(scan_cycle.cycle_path(DAY, "REGULAR", "orb").parent)
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)

        assert producer == lock == report["candidate_dir"] == str(shared)
        assert os.path.isabs(producer)

    def test_a_producer_writing_elsewhere_is_not_silently_read(
            self, shared, monkeypatch, tmp_path):
        """The split itself: publish into a private directory, then read
        as the consumer. The consumer must report a MISSING PRODUCER, not
        a quiet market."""
        from s6_live import candidate_source

        private = tmp_path / "checkout" / "logs" / "scanners" / "candidates"
        private.mkdir(parents=True)
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(private))
        self._publish(["AAPL"])

        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(shared))
        source = self._source()
        assert source.symbols() == []
        assert candidate_source.NO_PRODUCER_RUN in source.describe()["refusal"]

    # F. the existing per-row checks are untouched by any of this.
    def test_the_variant_check_still_applies(self, shared):
        self._publish(["AAPL"], variant="S6-O")
        assert self._source().symbols() == []

    def test_the_session_check_still_applies(self, shared):
        self._publish(["AAPL"], session="OVERNIGHT_DAYTIME", variant="S6-O")
        assert self._source(session="REGULAR").symbols() == []

    def test_the_trading_day_check_still_applies(self, shared):
        self._publish(["AAPL"], day="2026-08-21")
        assert self._source().symbols() == []


# ====================================================================
# §4. a producer that cannot reach the store says so, loudly
# ====================================================================
class TestAMisconfiguredProducerIsNotAQuietDay:
    def _report(self, **kw):
        from scanners.base.scanner_base import ScanOutcome
        from scanners.runner import RunReport

        outcome = ScanOutcome(scanner_name="orb", scanner_version="orb_v1.0",
                              config_fingerprint="fp", trading_day=DAY)
        outcome.signals = [Signal("AAPL")]

        report = RunReport(trading_day=DAY, started_at=T0.isoformat(),
                           provider="fake", universe_size=1, run_id="run-1",
                           session="REGULAR")
        report.outcomes = [outcome]
        for key, value in kw.items():
            setattr(report, key, value)
        return report

    def test_publication_raises_rather_than_writing_somewhere_private(self):
        from scanners.runner import publish_report_candidates

        with pytest.raises(publisher.CandidateHandoffMisconfigured):
            publish_report_candidates(self._report())

    def test_the_run_records_a_producer_config_error(self):
        from scanners import runner

        report = self._report()
        runner._publish_safely(report)
        assert report.publication_status == run_context.PUBLICATION_CONFIG_ERROR
        assert report.publication_detail
        assert report.published_rows == 0

    def test_that_status_reaches_the_manifest(self):
        from scanners import runner

        report = self._report()
        runner._publish_safely(report)
        assert (report.to_manifest()["publication_status"]
                == run_context.PUBLICATION_CONFIG_ERROR)

    def test_a_healthy_hand_off_records_ok(self, monkeypatch, tmp_path):
        from scanners import runner

        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "s"))
        report = self._report()
        runner._publish_safely(report)
        assert report.publication_status == run_context.PUBLICATION_OK
        assert report.published_rows == 1

    def test_a_run_with_no_publisher_is_not_applicable(self, tmp_path):
        from scanners import runner
        from scanners.runner import RunReport

        from scanners.base.scanner_base import ScanOutcome

        outcome = ScanOutcome(scanner_name="breakout_ready",
                              scanner_version="v1", config_fingerprint="fp",
                              trading_day=DAY)
        outcome.signals = [Signal("AAPL")]
        report = RunReport(trading_day=DAY, started_at=T0.isoformat(),
                           provider="fake", universe_size=1, session="REGULAR")
        report.outcomes = [outcome]
        runner._publish_safely(report)
        assert (report.publication_status
                == run_context.PUBLICATION_NOT_APPLICABLE)

    def test_a_config_error_is_a_nonzero_exit(self, monkeypatch, tmp_path):
        """A cron entry cannot act on a log warning."""
        from scanners import runner

        report = self._report()

        monkeypatch.setattr(runner, "run_scanners",
                            lambda **kw: (runner._publish_safely(report)
                                          or report))
        monkeypatch.setattr(runner, "print_report", lambda r: None)

        class Args:
            limit = None
            no_store = False
            daily_lookback_days = 400
            intraday_interval = "1m"
            intraday_lookback_days = 5
            profile = None
            universe = None
            active_pool_size = 50
            no_eligibility = False

        assert runner._run_and_report(Args(), names=["orb"], symbols=None,
                                      day=DAY, session="REGULAR") == 1

    def test_an_unresolvable_store_does_not_read_as_an_overlap(self):
        """Two different faults. Reporting the second as the first loses
        the day's signals AND sends the operator to the wrong place."""
        from scanners.publish import scan_cycle

        with scan_cycle.hold_all(DAY, "REGULAR", scanners=["orb"]) as cycle:
            assert cycle.unresolved is True
            assert cycle.skipped is False

    def test_the_scan_still_runs_when_the_store_is_unresolvable(
            self, monkeypatch):
        """The signals belong in the analytics dataset either way, and
        there is no hand-off left to protect."""
        from scanners import runner

        ran = []
        monkeypatch.setattr(runner, "run_scanners",
                            lambda **kw: ran.append(kw) or self._report(
                                publication_status=run_context.PUBLICATION_CONFIG_ERROR))
        monkeypatch.setattr(runner, "print_report", lambda r: None)
        monkeypatch.setattr(runner, "us_trading_day", lambda: DAY)
        assert runner.main(["--scanners", "orb", "--session", "REGULAR",
                            "--trading-day", DAY,
                            "--ignore-market-calendar"]) == 1
        assert len(ran) == 1


# ====================================================================
# B/G. the cron scripts resolve it, and refuse without it
# ====================================================================
def _resolve(env_file, **env):
    """Run `resolve_shared_candidate_dir` and report what it decided."""
    script = (f'. "{SHARED_ENV_SH}"\n'
              'if resolve_shared_candidate_dir; then\n'
              '  echo "OK=$SCANNER_CANDIDATE_DIR"\n'
              'else\n'
              '  echo "REFUSED"\n'
              'fi\n')
    environment = dict(os.environ)
    environment.pop("SCANNER_CANDIDATE_DIR", None)
    environment["SCANNER_SHARED_ENV"] = str(env_file)
    environment.update({k: str(v) for k, v in env.items()})
    result = subprocess.run(["bash", "-c", script], capture_output=True,
                            text=True, env=environment, timeout=60)
    return result.stdout.strip(), result.stderr.strip()


class TestTheCronScriptsResolveTheSharedStore:
    @pytest.fixture
    def release(self, tmp_path):
        store = tmp_path / "shared" / "state" / "candidates"
        store.mkdir(parents=True)
        env_file = tmp_path / "kis-readonly.env"
        env_file.write_text(
            "DEPLOYED_COMMIT=abc123\n"
            f"SCANNER_CANDIDATE_DIR={store}\n"
            f"TRADING_PROJECT_ROOT={tmp_path}/abc123\n", encoding="utf-8")
        return env_file, store

    def test_it_takes_the_directory_from_the_release_env(self, release):
        env_file, store = release
        out, _ = _resolve(env_file)
        assert out == f"OK={store}"

    def test_it_takes_ONLY_that_variable(self, release, tmp_path):
        """Sourcing the whole file would point the scanner's analytics,
        logs and caches at the release directory."""
        env_file, _ = release
        script = (f'. "{SHARED_ENV_SH}"\nresolve_shared_candidate_dir\n'
                  'echo "ROOT=${TRADING_PROJECT_ROOT:-unset}"\n'
                  'echo "COMMIT=${DEPLOYED_COMMIT:-unset}"\n')
        environment = dict(os.environ)
        for key in ("SCANNER_CANDIDATE_DIR", "TRADING_PROJECT_ROOT",
                    "DEPLOYED_COMMIT"):
            environment.pop(key, None)
        environment["SCANNER_SHARED_ENV"] = str(env_file)
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, env=environment, timeout=60)
        assert "ROOT=unset" in result.stdout
        assert "COMMIT=unset" in result.stdout

    def test_a_missing_env_file_refuses(self, tmp_path):
        out, err = _resolve(tmp_path / "absent.env")
        assert out == "REFUSED"
        assert "PRODUCER_CONFIG_ERROR" in err

    def test_an_env_file_without_the_key_refuses(self, tmp_path):
        env_file = tmp_path / "e.env"
        env_file.write_text("DEPLOYED_COMMIT=abc\n", encoding="utf-8")
        out, err = _resolve(env_file)
        assert out == "REFUSED"
        assert "PRODUCER_CONFIG_ERROR" in err

    def test_a_store_that_does_not_exist_refuses(self, tmp_path):
        env_file = tmp_path / "e.env"
        env_file.write_text(
            f"SCANNER_CANDIDATE_DIR={tmp_path}/gone\n", encoding="utf-8")
        out, err = _resolve(env_file)
        assert out == "REFUSED"
        assert "does not exist" in err

    def test_an_operator_override_wins(self, tmp_path):
        env_file = tmp_path / "e.env"
        env_file.write_text("SCANNER_CANDIDATE_DIR=/from/file\n",
                            encoding="utf-8")
        script = (f'export SCANNER_CANDIDATE_DIR=/from/operator\n'
                  f'. "{SHARED_ENV_SH}"\n'
                  'resolve_shared_candidate_dir && echo "$SCANNER_CANDIDATE_DIR"\n')
        environment = dict(os.environ)
        environment["SCANNER_SHARED_ENV"] = str(env_file)
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, env=environment, timeout=60)
        assert result.stdout.strip() == "/from/operator"

    @pytest.mark.parametrize("script", ["s6_scan.sh", "s2_regular_scan.sh"])
    def test_both_producers_resolve_before_scanning(self, script):
        """G: S2 uses the same canonical store as S6. It is
        DISCOVERY_ONLY, and its research hand-off still has to land
        where a reader can find it."""
        text = (CRON_DIR / script).read_text(encoding="utf-8")
        assert ". \"$SCRIPT_DIR/shared_env.sh\"" in text
        assert "resolve_shared_candidate_dir || exit 1" in text
        # The scan must come AFTER the refusal.
        assert (text.index("resolve_shared_candidate_dir")
                < text.index("run_scanners.py"))

    @pytest.mark.parametrize("script", ["s6_scan.sh", "s2_regular_scan.sh"])
    def test_both_pass_the_resolved_store_to_python(self, script):
        text = (CRON_DIR / script).read_text(encoding="utf-8")
        assert 'SCANNER_CANDIDATE_DIR="$SCANNER_CANDIDATE_DIR"' in text
        assert 'TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT"' in text

    @pytest.mark.parametrize("script",
                             ["shared_env.sh", "s6_scan.sh",
                              "s2_regular_scan.sh"])
    def test_they_parse(self, script):
        assert subprocess.run(["bash", "-n", str(CRON_DIR / script)],
                              capture_output=True, timeout=60).returncode == 0


# ====================================================================
# H. S2 is still DISCOVERY_ONLY with no executor
# ====================================================================
class TestS2IsUnchanged:
    def test_s2_has_no_executor_wired(self):
        """Publishing is not permission to trade. S2 gained a shared
        directory here and nothing else."""
        import kis_live_trading as klt
        from s2_live import candidate_source

        assert klt.STRATEGY_SOURCES  # the set exists
        # S2 is a strategy SOURCE (so it gets the stricter gates), but
        # its live mode is what decides whether it can offer anything.
        assert candidate_source.SOURCE_S2 in klt.STRATEGY_SOURCES
        assert slm.SCANNER_LIVE_MODE["accumulation"] == slm.MODE_DISCOVERY_ONLY

    def test_the_s2_source_refuses_while_discovery_only(self, monkeypatch,
                                                        tmp_path):
        from s2_live import candidate_source

        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "s"))
        source = candidate_source.S2CandidateSource(trading_day=DAY,
                                                    session="REGULAR")
        assert source.symbols() == []

    def test_every_live_mode_is_unchanged(self):
        assert slm.SCANNER_LIVE_MODE == {
            "hma_early_trend": slm.MODE_LIMITED_LIVE,
            "accumulation": slm.MODE_DISCOVERY_ONLY,
            "breakout_ready": slm.MODE_DISCOVERY_ONLY,
            "premarket_momentum": slm.MODE_DISCOVERY_ONLY,
            "gap_pullback": slm.MODE_DISCOVERY_ONLY,
            # `orb` was promoted to LIMITED_LIVE beside S1. Pinned to the
            # intended posture so an ACCIDENTAL change still trips here.
            "orb": slm.MODE_LIMITED_LIVE,
        }

    def test_the_rollout_limits_are_unchanged(self):
        from config.live_rollout_config import LiveRolloutConfig

        rollout = LiveRolloutConfig.from_env()
        assert rollout.max_quantity_per_order == 1
        assert rollout.max_open_positions == 1
        assert rollout.allow_fractional is False
