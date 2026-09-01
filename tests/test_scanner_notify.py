"""Scanner Slack notifications (track B).

The three properties that matter, in order of how badly a regression
would hurt:

1. A Slack problem must never change a scan's result or exit code.
2. A successful run must never produce a message. A channel that fires
   on success is a channel nobody reads, which silently disables every
   alert in it.
3. No symbol or score may appear in an alert.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import run_context  # noqa: E402
from scanners.notify import slack as notify  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_candidate_store(tmp_path, monkeypatch):
    """A candidate store of this module's own, never the shared one.

    `runner.main` takes the publishing scanners' cycle locks before it
    reaches anything these tests control, and `candidate_dir` resolves
    those locks from the environment. On a release host that is the LIVE
    store: on 2026-09-01 a host gate run collided with the live `orb`
    scan --

        [SCAN CYCLE] skipped -- orb: a orb scan started at
        2026-09-01T06:17:29 (pid 3697118) is still running

    -- and the test read a refusal as its own result. These files never
    went red for it only because they assert == 0, which is also what a
    refused overlap returns; they were insensitive, not correct.

    The direction that matters more is the other one: without this, a
    test run can take the live S6 cycle lock and stand a real scan down.
    """
    store = tmp_path / "candidates"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SCANNER_CANDIDATE_DIR", str(store))



class FakeOutcome:
    def __init__(self, name, failed=False, breaker=False):
        self.scanner_name = name
        self.failed = failed
        self.circuit_breaker_triggered = breaker
        self.consecutive_error_peak = 25 if breaker else 0


class FakeReport:
    def __init__(self, status, *, profile="daily", outcomes=None, breaker=False):
        self.status = status
        self.profile = profile
        self.trading_day = "2026-08-17"
        self.run_id = "20260817_DAILY_abc123"
        self.provider = "yfinance"
        self.universe_size = 13372
        self.fetch_failures = 0
        self.outcomes = outcomes or []
        self.circuit_breaker_triggered = breaker
        self.consecutive_error_peak = 25 if breaker else 0
        self.construction_failures = {}
        self.skipped_reason = None


class Recorder:
    def __init__(self, result=True):
        self.messages = []
        self.result = result

    def __call__(self, message):
        self.messages.append(message)
        return self.result


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))
    monkeypatch.delenv(notify.ENABLED_ENV, raising=False)
    return tmp_path


class TestWhenToAlert:
    @pytest.mark.parametrize("status", sorted(run_context.FAILURE_STATUSES))
    def test_every_failure_status_alerts(self, status):
        assert notify.should_alert(FakeReport(status)) is True

    @pytest.mark.parametrize("status", [
        run_context.SUCCESS, run_context.SKIPPED_MARKET_CLOSED])
    def test_success_and_holiday_are_silent(self, status):
        assert notify.should_alert(FakeReport(status)) is False

    def test_success_with_zero_candidates_is_silent(self):
        """A quiet market is the most common outcome of a scanning day."""
        report = FakeReport(run_context.SUCCESS)
        report.signal_count = 0
        assert notify.should_alert(report) is False

    def test_plain_partial_is_silent(self):
        assert notify.should_alert(FakeReport(run_context.PARTIAL)) is False

    def test_partial_with_a_tripped_breaker_alerts(self):
        """The breaker is what distinguishes 'broken' from 'unlucky'."""
        assert notify.should_alert(
            FakeReport(run_context.PARTIAL, breaker=True)) is True


class TestMessageContent:
    def test_no_symbol_or_score_appears(self):
        report = FakeReport(run_context.FAILED,
                            outcomes=[FakeOutcome("orb", failed=True)])
        message = notify.format_run_alert(report)
        assert "orb" in message, "the failing scanner name is operational, not a candidate"
        for forbidden in ("AAPL", "scanner_score", "signal_price", "candidate:"):
            assert forbidden not in message

    def test_the_message_states_the_order_path_is_unaffected(self):
        message = notify.format_run_alert(FakeReport(run_context.FAILED_PROVIDER))
        assert "Candidate Decision: disabled" in message

    def test_a_tripped_breaker_is_named(self):
        message = notify.format_run_alert(
            FakeReport(run_context.PARTIAL, breaker=True,
                       outcomes=[FakeOutcome("orb", breaker=True)]))
        assert "circuit breaker" in message.lower()

    def test_cli_exit_two_explains_itself(self):
        message = notify.format_cli_alert("run_scanner_report.py weekly", 2,
                                          "2026-08-17")
        assert "exit 2" in message
        assert "cron" in message


class TestBestEffort:
    def test_a_raising_sender_does_not_propagate(self):
        def boom(message):
            raise RuntimeError("slack is down")

        assert notify.notify_run(FakeReport(run_context.FAILED), sender=boom) is False

    def test_a_sender_returning_false_does_not_propagate(self):
        assert notify.notify_run(FakeReport(run_context.FAILED),
                                 sender=lambda m: False) is False

    def test_an_unwritable_state_dir_still_sends(self, monkeypatch):
        """Suppression fails OPEN. Losing a duplicate is a nuisance;
        losing the alert because a state file was unwritable is the
        failure this module exists to prevent."""
        monkeypatch.setattr(notify, "_state_path",
                            lambda day: (_ for _ in ()).throw(OSError("read-only")))
        recorder = Recorder()
        assert notify.notify_run(FakeReport(run_context.FAILED), sender=recorder) is True
        assert len(recorder.messages) == 1

    def test_a_malformed_report_does_not_raise(self):
        assert notify.notify_run(object(), sender=Recorder()) is False


class TestDeduplication:
    def test_the_same_failure_alerts_once(self):
        recorder = Recorder()
        report = FakeReport(run_context.FAILED)
        assert notify.notify_run(report, sender=recorder) is True
        assert notify.notify_run(report, sender=recorder) is False
        assert len(recorder.messages) == 1

    def test_a_different_profile_alerts_separately(self):
        recorder = Recorder()
        notify.notify_run(FakeReport(run_context.FAILED, profile="daily"), sender=recorder)
        notify.notify_run(FakeReport(run_context.FAILED, profile="open"), sender=recorder)
        assert len(recorder.messages) == 2

    def test_a_failed_send_is_not_recorded_as_sent(self):
        """Otherwise a Slack outage would permanently suppress the retry."""
        report = FakeReport(run_context.FAILED)
        assert notify.notify_run(report, sender=lambda m: False) is False
        recorder = Recorder()
        assert notify.notify_run(report, sender=recorder) is True


class TestOffSwitch:
    def test_disabled_sends_nothing(self):
        recorder = Recorder()
        env = {notify.ENABLED_ENV: "false"}
        assert notify.notify_run(FakeReport(run_context.FAILED),
                                 sender=recorder, env=env) is False
        assert notify.send_report("hello", sender=recorder, env=env) is False
        assert recorder.messages == []

    def test_unset_means_enabled(self):
        recorder = Recorder()
        assert notify.notify_run(FakeReport(run_context.FAILED),
                                 sender=recorder, env={}) is True


class TestCliFailures:
    def test_exit_zero_is_silent(self):
        recorder = Recorder()
        assert notify.notify_cli_failure("x", 0, sender=recorder) is False
        assert recorder.messages == []

    @pytest.mark.parametrize("code", [1, 2])
    def test_nonzero_alerts(self, code):
        recorder = Recorder()
        assert notify.notify_cli_failure(f"cmd{code}", code, trading_day="2026-08-17",
                                         sender=recorder) is True
        assert f"exit {code}" in recorder.messages[0]


class TestRunnerIntegration:
    @staticmethod
    def _run_once(monkeypatch, tmp_path, sender=None, exploding=False, store=False):
        from scanners import runner
        from scanners.base.market_data_provider import StaticMarketDataProvider
        from tests import scanner_fixtures as fx

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))
        if exploding:
            def boom(report):
                raise RuntimeError("slack blew up")
            monkeypatch.setattr(notify, "notify_run", boom)
        elif sender is not None:
            monkeypatch.setattr(notify, "_send", sender)

        bundle = fx.uptrend_bundle("TEST", volumes=fx.volume_surge())
        monkeypatch.setattr(
            runner, "default_provider",
            lambda **kw: StaticMarketDataProvider(daily={"TEST": bundle.daily}))
        argv = ["--scanners", "hma_early_trend", "--symbols", "TEST",
                "--trading-day", "2026-08-17",
                "--ignore-market-calendar", "--no-eligibility"]
        if not store:
            # NOTE: --no-store also suppresses the run manifest
            # (runner.py gates `_record_manifest` on `if store:`), so a
            # test that inspects the manifest must run with storing on.
            argv.append("--no-store")
        return runner.main(argv)

    def test_the_exit_code_is_unchanged_when_slack_explodes(self, monkeypatch, tmp_path):
        """The load-bearing test for track B-1: the notification runs
        after the exit code is decided and cannot alter it.

        Compared against the SAME run without the explosion rather than
        against a hardcoded 0/1 -- an assertion that the code is "0 or 1"
        would pass no matter what this module did.
        """
        clean = self._run_once(monkeypatch, tmp_path / "clean")
        broken = self._run_once(monkeypatch, tmp_path / "broken", exploding=True)
        assert broken == clean

    def test_a_stored_run_is_identical_whether_slack_works_or_not(
            self, monkeypatch, tmp_path):
        """Not just the exit code: the stored data must match too.

        This is the assertion that actually protects month 1. A
        notification that could alter what lands in the analytics store
        would make the dataset depend on whether Slack was reachable.
        """
        import json

        VOLATILE = {"run_id", "started_at", "recorded_at", "duration_seconds",
                    "scanner_run_id", "timestamp", "feature_timestamp",
                    "stored_at", "signal_id"}

        def scrub(rows):
            cleaned = []
            for row in rows:
                item = {key: value for key, value in row.items() if key not in VOLATILE}
                for scanner in item.get("scanners") or []:
                    scanner.pop("duration_seconds", None)
                cleaned.append(item)
            return cleaned

        def read(root, kind):
            path = root / "analytics" / kind / "2026-08-17.jsonl"
            return scrub([json.loads(line) for line in
                          path.read_text().splitlines() if line.strip()])

        self._run_once(monkeypatch, tmp_path / "clean", store=True)
        self._run_once(monkeypatch, tmp_path / "broken", exploding=True, store=True)

        for kind in ("runs", "signals"):
            clean, broken = read(tmp_path / "clean", kind), read(tmp_path / "broken", kind)
            assert clean, f"the {kind} file should not be empty"
            assert broken == clean, f"{kind} diverged when Slack failed"

    def test_a_successful_run_sends_nothing(self, monkeypatch, tmp_path):
        recorder = Recorder()
        code = self._run_once(monkeypatch, tmp_path, sender=recorder)
        assert code == 0
        assert recorder.messages == []

    def test_a_market_holiday_sends_nothing(self, monkeypatch, tmp_path):
        """A holiday is a correct no-op, not an incident."""
        from scanners import runner

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))
        recorder = Recorder()
        monkeypatch.setattr(notify, "_send", recorder)
        monkeypatch.setattr("market_guard.is_us_trading_day", lambda: False)

        assert runner.main(["--profile", "daily", "--trading-day", "2026-08-17"]) == 0
        assert recorder.messages == []


class TestWeeklySlackFormat:
    def _report(self):
        return {
            "report": "weekly", "start_day": "2026-08-10", "end_day": "2026-08-16",
            "generated_at": "2026-08-16T08:00:00+00:00", "hit_horizon": "return_1d",
            "trading_days": ["2026-08-11", "2026-08-12"], "total_signals": 42,
            "scanners": [{
                "scanner_name": "hma_early_trend",
                "scanner_version": "hma_early_trend_v1.0",
                "market_data_provider": "yfinance", "signal_count": 20,
                "hit_rate": 55.0, "avg_return_1d": 1.2, "avg_return_3d": 2.0,
                "avg_return_5d": 3.1, "avg_mfe": 4.0, "avg_mae": -1.5,
                "median_mfe": 3.0, "median_mae": -1.0, "mfe_mae_ratio": 2.67,
                "median_return_1d": 1.0,
                "best_candidate": {"symbol": "SECRET", "return_1d": 20.0},
                "worst_candidate": {"symbol": "ALSOSECRET", "return_1d": -9.0},
                "maturity": {"return_5d": {"n": 8, "pct_of_signals": 40.0}},
            }],
            "experiment_splits": [],
        }

    def test_candidate_symbols_are_excluded(self):
        """Track B-2: the file report keeps them, Slack does not."""
        from scanners.analytics import weekly_report

        text = weekly_report.format_slack(self._report())
        assert "SECRET" not in text
        assert "ALSOSECRET" not in text

    def test_the_statistics_are_included(self):
        from scanners.analytics import weekly_report

        text = weekly_report.format_slack(self._report())
        for expected in ("hma_early_trend", "yfinance", "55.00", "2.67"):
            assert expected in text

    def test_run_health_is_shown(self):
        from scanners.analytics import weekly_report

        text = weekly_report.format_slack(
            self._report(),
            run_health={"runs": 12, "failed": ["2026-08-12 open: FAILED_PROVIDER"],
                        "partial": [], "circuit_breaker_runs": 1,
                        "skipped_market_closed": 0, "statuses": {}})
        assert "실행 12회" in text
        assert "FAILED_PROVIDER" in text

    def test_an_empty_week_says_so(self):
        from scanners.analytics import weekly_report

        empty = dict(self._report(), scanners=[], total_signals=0, trading_days=[])
        text = weekly_report.format_slack(empty)
        assert "신호가 없습니다" in text

    def test_the_file_report_still_contains_the_candidates(self):
        """The Slack restriction must not have narrowed the file report."""
        from scanners.analytics import weekly_report

        text = weekly_report.format_report(self._report())
        assert "SECRET" in text
