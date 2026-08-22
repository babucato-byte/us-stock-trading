"""The activation gate, evaluated in one place.

The distinction this file defends: NOT_MEASURED is not PASS. "The market
was closed so we could not check" and "we checked and it was fine" are
different facts and only one permits promotion. A weekend must
manufacture neither a pass nor a failure.

The second property is that the evaluator PROMOTES NOTHING. A report
that flipped the live-mode flag would make promotion a consequence of
running a report.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import readiness as r  # noqa: E402

CRON = ("2,17,32,47 * * * 1-5 /home/ubuntu/s6_scan.sh\n"
        "7,22,37,52 * * * 1-5 /home/ubuntu/s6_exec.sh\n")


def all_observed(**overrides):
    seen = {"s1_healthy": True, "regression_healthy": True,
            "candidate_freshness_verified": True,
            "regular_market_tick_verified": True,
            "common_stock_dry_run_verified": True,
            "account_rows": []}
    seen.update(overrides)
    return seen


class TestUnmeasuredIsNotPass:
    def test_a_weekend_evaluation_is_not_ready(self):
        """The expected state right now, and it must be reached by
        honest arithmetic rather than by a calendar rule."""
        verdict = r.evaluate(crontab=CRON)
        assert verdict.verdict == r.NOT_READY
        assert verdict.ready is False
        assert verdict.failures() == [], "nothing is broken"
        assert set(r.MARKET_DEPENDENT) <= set(verdict.unmeasured())

    def test_an_absent_observation_is_unmeasured_not_false(self):
        verdict = r.evaluate(crontab=CRON, observations={})
        for name in r.MARKET_DEPENDENT:
            assert verdict.checks[name].status == r.NOT_MEASURED

    def test_an_explicit_none_is_also_unmeasured(self):
        verdict = r.evaluate(crontab=CRON, observations={
            "regular_market_tick_verified": None})
        assert verdict.checks["regular_market_tick_verified"].status == \
            r.NOT_MEASURED

    def test_an_observed_failure_blocks_rather_than_waits(self):
        """FAIL and NOT_MEASURED need different operator responses: one
        is broken, the other is simply not yet known."""
        verdict = r.evaluate(crontab=CRON, observations=all_observed(
            s1_healthy=False))
        assert verdict.verdict == r.BLOCKED
        assert "s1_healthy" in verdict.failures()

    def test_a_probe_that_raises_is_unmeasured(self, monkeypatch):
        """An evaluator whose own failure read as a pass would answer
        confidently about a check it never ran."""
        monkeypatch.setattr(r, "_runtime_loaded",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        verdict = r.evaluate(crontab=CRON)
        assert verdict.checks["runtime_loaded"].status == r.NOT_MEASURED
        assert verdict.ready is False


class TestReadyRequiresEverything:
    def test_every_check_must_pass(self, tmp_path, monkeypatch):
        import tempfile

        monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
        from state_store.db import open_db

        with open_db() as conn:
            verdict = r.evaluate(conn=conn, crontab=CRON,
                                 observations=all_observed())
        assert verdict.verdict == r.READY
        assert verdict.ready is True
        assert verdict.unmeasured() == []

    @pytest.mark.parametrize("missing", sorted(r.MARKET_DEPENDENT))
    def test_one_unmeasured_check_prevents_ready(self, missing, monkeypatch):
        import tempfile

        monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
        from state_store.db import open_db

        seen = all_observed()
        seen.pop(missing)
        with open_db() as conn:
            verdict = r.evaluate(conn=conn, crontab=CRON, observations=seen)
        assert verdict.ready is False
        assert missing in verdict.unmeasured()

    def test_every_required_condition_is_evaluated(self):
        verdict = r.evaluate(crontab=CRON)
        assert set(verdict.checks) == set(r.CHECKS)
        assert len(r.CHECKS) == 14


class TestTheInstalledConditionsAreRealChecks:
    def test_a_missing_cron_fails_rather_than_waits(self):
        verdict = r.evaluate(crontab="")
        assert verdict.checks["scanner_cron_active"].status == r.FAIL
        assert verdict.verdict == r.BLOCKED

    def test_a_commented_out_cron_does_not_count(self):
        verdict = r.evaluate(crontab="# 2,17 * * * 1-5 /home/ubuntu/s6_scan.sh")
        assert verdict.checks["scanner_cron_active"].status == r.FAIL

    def test_no_crontab_supplied_is_unmeasured(self):
        verdict = r.evaluate()
        assert verdict.checks["scanner_cron_active"].status == r.NOT_MEASURED

    def test_the_sell_check_rejects_a_private_broker_call(self, monkeypatch):
        """The check that would catch S6 growing its own submitter."""
        import inspect

        monkeypatch.setattr(inspect, "getsource",
                            lambda module: "adapter.submit_order(...)")
        assert r._common_sell_ready().status == r.FAIL

    def test_the_wiring_checks_actually_pass_today(self):
        for probe in (r._runtime_loaded, r._fill_sync_ready,
                      r._exit_runtime_ready, r._common_sell_ready,
                      r._restart_ready):
            assert probe().status == r.PASS, probe.__name__


class TestItPromotesNothing:
    def test_evaluating_does_not_change_the_live_mode(self):
        from config import scanner_live_mode

        before = scanner_live_mode.SCANNER_LIVE_MODE["orb"]
        r.evaluate(crontab=CRON, observations=all_observed())
        assert scanner_live_mode.SCANNER_LIVE_MODE["orb"] == before
        assert before == "DISCOVERY_ONLY"

    def test_it_writes_no_configuration(self):
        """Local dicts are fine -- the evaluator builds its own result.
        What must not exist is a write INTO another module's table, which
        is what promotion would look like."""
        import ast

        source = (REPO_ROOT / "s6_live" / "readiness.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Subscript) and \
                        isinstance(target.value, ast.Attribute):
                    raise AssertionError(
                        f"writes into {ast.dump(target.value)[:60]}")
                if isinstance(target, ast.Attribute):
                    raise AssertionError("assigns to a module attribute")

    def test_the_report_serialises(self):
        import json

        json.loads(json.dumps(r.evaluate(crontab=CRON).as_dict()))
