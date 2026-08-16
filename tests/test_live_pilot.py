"""T9: the real-time pilot harness.

Three properties matter more than any individual assertion here:

  1. the pilot NEVER arms itself -- the posture is read from the same
     three flags the Order Gate reads, and no code path in live_pilot/
     or scripts/ writes one;
  2. an OBSERVE session cannot reach an order-submitting method, proven
     both statically (live_pilot/armed.py is the only module that
     imports the order path, and nothing imports it unless the posture
     is ARMED) and at run time (a broker double that raises on every
     mutating method survives a whole tick untouched);
  3. every tick that STARTED is recorded -- a stage that raises produces
     a tick row with an `error`, not a missing row.
"""
import ast
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from execution import idempotency
from live_pilot import armed, observe, posture, preflight, recorder, runner
from state_store import db as state_db

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
PILOT_DIR = REPO_ROOT / "live_pilot"

NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)  # a Thursday, mid regular session
ACCOUNT_ID = "12345678"


# ---------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECON.json"))
    monkeypatch.setenv("LIVE_PILOT_LOG_DIR", str(tmp_path / "pilot"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEMPOTENCY.lock")
    for flag in ("KIS_LIVE_ORDER_ENABLED", "LIVE_ROLLOUT_ENABLED", "ENTRY_DISABLED",
                 "LIVE_PILOT_ACK_LIVE_ENV"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("KIS_ENV", "paper")
    monkeypatch.setenv("KIS_ALLOWED_ACCOUNT_NO", ACCOUNT_ID)
    state_db.open_db().close()
    yield


def write_snapshot(tmp_path, *, checked_at=None, clean=True, mismatch=0, unknown=0,
                   halt=False):
    stamp = checked_at or datetime.now(timezone.utc)
    (tmp_path / "RECON.json").write_text(json.dumps({
        "schema_version": 1, "checked_at": stamp.isoformat(), "clean": clean,
        "mismatch_count": mismatch, "unknown_count": unknown, "halt": halt,
    }), encoding="utf-8")


class _Snapshot:
    def __init__(self, account_id=ACCOUNT_ID):
        self.account_id = account_id
        # ORACLE-CASH-01: a real balance read reports no cash figure at
        # all. Preflight must render that as UNAVAILABLE rather than
        # printing a bare None, and must not fail the gate over it --
        # orderable cash is established per candidate at entry time.
        self.usd_cash = None
        self.usd_orderable_cash = None
        self.usd_available_for_new_order = None
        self.cash_status = "UNAVAILABLE"
        self.cash_source = "TTTS3012R_DOES_NOT_PROVIDE"


class _ReadOnlyBroker:
    """Records every method reached. Any state-mutating method raises, so
    a test can prove an OBSERVE tick never got near one."""

    def __init__(self, account_id=ACCOUNT_ID):
        self.calls = []
        self._account_id = account_id

    def get_account_snapshot(self):
        self.calls.append("get_account_snapshot")
        return _Snapshot(self._account_id)

    def get_positions(self):
        self.calls.append("get_positions")
        return []

    def get_open_orders(self):
        self.calls.append("get_open_orders")
        return []

    def get_current_price(self, instrument):
        self.calls.append("get_current_price")
        return 100.0

    def submit_order(self, *a, **k):  # pragma: no cover -- must never run
        raise AssertionError("an OBSERVE pilot reached submit_order()")

    def cancel_order(self, *a, **k):  # pragma: no cover -- must never run
        raise AssertionError("an OBSERVE pilot reached cancel_order()")


class _FailingBroker(_ReadOnlyBroker):
    def get_account_snapshot(self):
        raise RuntimeError("KIS unreachable")


# ---------------------------------------------------------------------
# Posture: the pilot never arms itself
# ---------------------------------------------------------------------
class TestPosture:
    def test_default_environment_is_observe(self):
        assert posture.resolve_posture({}).posture == posture.POSTURE_OBSERVE

    def test_all_three_flags_arm_it(self):
        decision = posture.resolve_posture({
            "KIS_LIVE_ORDER_ENABLED": "true", "LIVE_ROLLOUT_ENABLED": "true",
        })
        assert decision.posture == posture.POSTURE_ARMED
        assert decision.armed is True

    @pytest.mark.parametrize("env,expected_fragment", [
        ({}, "KIS_LIVE_ORDER_ENABLED"),
        ({"KIS_LIVE_ORDER_ENABLED": "true"}, "LIVE_ROLLOUT_ENABLED"),
        ({"KIS_LIVE_ORDER_ENABLED": "true", "LIVE_ROLLOUT_ENABLED": "true",
          "ENTRY_DISABLED": "true"}, "ENTRY_DISABLED"),
    ])
    def test_a_partial_configuration_degrades_to_observe(self, env, expected_fragment):
        decision = posture.resolve_posture(env)
        assert decision.posture == posture.POSTURE_OBSERVE
        assert expected_fragment in decision.reason

    def test_posture_is_re_read_every_call_not_cached(self, monkeypatch):
        assert posture.resolve_posture().posture == posture.POSTURE_OBSERVE
        monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "true")
        monkeypatch.setenv("LIVE_ROLLOUT_ENABLED", "true")
        assert posture.resolve_posture().posture == posture.POSTURE_ARMED
        monkeypatch.setenv("ENTRY_DISABLED", "true")
        assert posture.resolve_posture().posture == posture.POSTURE_OBSERVE

    @pytest.mark.parametrize("env", [
        {"KIS_LIVE_ORDER_ENABLED": "true", "ENTRY_DISABLED": "true"},
        {"KIS_LIVE_ORDER_ENABLED": "true"},
        {"LIVE_ROLLOUT_ENABLED": "true"},
    ])
    def test_half_enabled_postures_are_reported_as_contradictory(self, env):
        assert posture.contradictory_posture(env) is not None

    def test_a_coherent_posture_is_not_contradictory(self):
        assert posture.contradictory_posture({}) is None
        assert posture.contradictory_posture({
            "KIS_LIVE_ORDER_ENABLED": "true", "LIVE_ROLLOUT_ENABLED": "true"}) is None


# ---------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------
class TestPreflightGates:
    def _rows(self, report):
        return {row["check"]: row for row in report.rows}

    def test_a_clean_paper_configuration_passes(self, tmp_path, monkeypatch):
        write_snapshot(tmp_path)
        monkeypatch.setattr(preflight, "check_scan_universe",
                            lambda report: report.ok("scan_universe", "stubbed"))
        monkeypatch.setattr(preflight, "check_watchlist",
                            lambda report, scan_enabled: report.ok("watchlist", "stubbed"))
        report = preflight.run_preflight(broker=_ReadOnlyBroker(), log_dir=tmp_path / "pilot")
        assert report.passed, report.render()

    @pytest.mark.parametrize("value", ["", "PAPER_TRADING", "prod", "Live "])
    def test_an_unrecognised_kis_env_refuses(self, value):
        report = preflight.PreflightReport()
        preflight.check_kis_env(report, {"KIS_ENV": value})
        assert report.failures
        assert report.failures[0]["reason_code"] in (
            "KIS_ENV_INVALID", "LIVE_ENV_NOT_ACKNOWLEDGED")

    def test_live_env_without_the_acknowledgement_refuses(self):
        report = preflight.PreflightReport()
        preflight.check_kis_env(report, {"KIS_ENV": "live"})
        assert report.failures[0]["reason_code"] == "LIVE_ENV_NOT_ACKNOWLEDGED"

    def test_live_env_with_the_acknowledgement_passes_this_gate(self):
        report = preflight.PreflightReport()
        preflight.check_kis_env(report, {"KIS_ENV": "live",
                                         preflight.ACK_LIVE_ENV: "true"})
        assert not report.failures

    def test_an_unconfirmed_value_this_posture_uses_blocks_a_live_session(self,
                                                                            monkeypatch):
        """The gate now asks what THIS posture depends on. An OBSERVE
        value left pending must still refuse a live session -- that is
        the half of the old blanket rule worth keeping."""
        from brokers import kis_broker
        from live_pilot import posture as posture_module

        rewritten = tuple(
            entry._replace(live_status=kis_broker.LIVE_RESPONSE_PENDING)
            if entry.name == "price_field_last" else entry
            for entry in kis_broker.VERIFICATION_MATRIX
        )
        monkeypatch.setattr(kis_broker, "VERIFICATION_MATRIX", rewritten)
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        assert report.failures[0]["reason_code"] == "LIVE_RESPONSE_PENDING"
        assert "price_field_last" in report.failures[0]["detail"]

    def test_order_only_values_no_longer_block_observe(self):
        """OBSERVE never reaches the order or cancel endpoints, so the
        values that describe them cannot be a reason to refuse it. ARMED
        is still blocked by them -- reported as a warning here."""
        from live_pilot import posture as posture_module

        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        assert not report.failures, report.render()
        warned = [r for r in report.warnings
                  if r["check"] == "armed_response_requirements"]
        assert warned and warned[0]["reason_code"] == "BLOCKED_FOR_ARMED_ONLY"

    def test_unconfirmed_kis_values_do_not_block_a_paper_session(self, monkeypatch):
        from brokers import kis_broker
        from live_pilot import posture as posture_module

        rewritten = tuple(
            entry._replace(live_status=kis_broker.LIVE_RESPONSE_PENDING)
            for entry in kis_broker.VERIFICATION_MATRIX
        )
        monkeypatch.setattr(kis_broker, "VERIFICATION_MATRIX", rewritten)
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "paper", posture=posture_module.POSTURE_OBSERVE)
        assert not report.failures
        assert self._rows(report)["live_response_pending"]["status"] == preflight.RESULT_INFO

    def test_no_environment_variable_can_skip_the_pending_gate(self, monkeypatch):
        from brokers import kis_broker
        from live_pilot import posture as posture_module

        rewritten = tuple(
            entry._replace(live_status=kis_broker.LIVE_RESPONSE_PENDING)
            if entry.name == "price_path" else entry
            for entry in kis_broker.VERIFICATION_MATRIX
        )
        monkeypatch.setattr(kis_broker, "VERIFICATION_MATRIX", rewritten)
        for name in ("LIVE_RESPONSE_PENDING_OK", "SKIP_PREFLIGHT",
                     "LIVE_PILOT_SKIP_PENDING", "LIVE_PILOT_FORCE",
                     "SKIP_LIVE_RESPONSE_CHECK", "FORCE_OBSERVE"):
            monkeypatch.setenv(name, "true")
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        assert report.failures

    def test_halt_blocks(self, monkeypatch):
        from operations import kill_switch

        monkeypatch.setattr(kill_switch, "is_halted", lambda: True)
        report = preflight.PreflightReport()
        preflight.check_kill_switch(report)
        assert report.failures[0]["reason_code"] == "HALT_ACTIVE"

    def test_entry_off_blocks(self, monkeypatch):
        from operations import kill_switch

        monkeypatch.setattr(kill_switch, "is_halted", lambda: False)
        monkeypatch.setattr(kill_switch, "is_entry_allowed", lambda: False)
        report = preflight.PreflightReport()
        preflight.check_kill_switch(report)
        assert report.failures[0]["reason_code"] == "ENTRY_OFF"

    @pytest.mark.parametrize("value", [None, 0, [], {}, "false"])
    def test_a_non_boolean_halt_answer_blocks(self, monkeypatch, value):
        """Every one of these is falsy. Coercing them would read as 'not
        halted', which is the most dangerous misreading available."""
        from operations import kill_switch

        monkeypatch.setattr(kill_switch, "is_halted", lambda: value)
        report = preflight.PreflightReport()
        preflight.check_kill_switch(report)
        assert report.failures[0]["reason_code"] == "HALT_STATUS_INVALID"

    def test_an_unreadable_halt_blocks(self, monkeypatch):
        from operations import kill_switch

        def _boom():
            raise OSError("state file gone")

        monkeypatch.setattr(kill_switch, "is_halted", _boom)
        report = preflight.PreflightReport()
        preflight.check_kill_switch(report)
        assert report.failures[0]["reason_code"] == "HALT_STATUS_UNAVAILABLE"

    def test_a_missing_reconciliation_snapshot_blocks(self):
        from reconciliation import freshness

        report = preflight.PreflightReport()
        preflight.check_reconciliation(report)
        assert report.failures[0]["reason_code"] == freshness.REASON_SNAPSHOT_MISSING

    def test_a_stale_reconciliation_snapshot_blocks(self, tmp_path):
        from reconciliation import freshness

        write_snapshot(tmp_path,
                       checked_at=datetime.now(timezone.utc) - timedelta(days=3))
        report = preflight.PreflightReport()
        preflight.check_reconciliation(report)
        assert report.failures[0]["reason_code"] == freshness.REASON_SNAPSHOT_STALE

    def test_a_fresh_clean_snapshot_passes(self, tmp_path):
        write_snapshot(tmp_path)
        report = preflight.PreflightReport()
        preflight.check_reconciliation(report)
        assert not report.failures

    def test_no_broker_blocks(self):
        report = preflight.PreflightReport()
        preflight.check_account(report, {}, None)
        assert report.failures[0]["reason_code"] == "BROKER_UNAVAILABLE"

    def test_an_account_read_failure_blocks(self):
        report = preflight.PreflightReport()
        preflight.check_account(report, {"KIS_ALLOWED_ACCOUNT_NO": ACCOUNT_ID},
                                _FailingBroker())
        assert report.failures[0]["reason_code"] == "ACCOUNT_READ_FAILED"

    def test_an_unlisted_account_blocks(self):
        report = preflight.PreflightReport()
        preflight.check_account(report, {"KIS_ALLOWED_ACCOUNT_NO": "99999999"},
                                _ReadOnlyBroker())
        assert report.failures[0]["reason_code"] == "ACCOUNT_MISMATCH"

    def test_the_mismatch_message_never_prints_a_full_account_number(self):
        report = preflight.PreflightReport()
        preflight.check_account(report, {"KIS_ALLOWED_ACCOUNT_NO": "99999999"},
                                _ReadOnlyBroker())
        detail = report.failures[0]["detail"]
        assert ACCOUNT_ID not in detail and "99999999" not in detail

    def test_an_unconfigured_allow_list_blocks(self):
        report = preflight.PreflightReport()
        preflight.check_account(report, {}, _ReadOnlyBroker())
        assert report.failures[0]["reason_code"] == "ACCOUNT_UNCONFIGURED"

    def test_an_empty_scan_universe_blocks(self, monkeypatch):
        import daily_candidate_scanner

        monkeypatch.setattr(daily_candidate_scanner, "load_scan_universe",
                            lambda: (__import__("pandas").DataFrame({"symbol": []}),
                                     Path("universe_tradable.csv")))
        report = preflight.PreflightReport()
        preflight.check_scan_universe(report)
        assert report.failures[0]["reason_code"] == "UNIVERSE_EMPTY"

    def test_an_empty_watchlist_blocks_when_scanning_is_off(self, monkeypatch):
        import paper_strategy_order as pso

        monkeypatch.setattr(pso, "load_watchlist", lambda: [])
        report = preflight.PreflightReport()
        preflight.check_watchlist(report, scan_enabled=False)
        assert report.failures[0]["reason_code"] == "WATCHLIST_EMPTY"

    def test_an_empty_watchlist_is_tolerated_when_a_scan_will_fill_it(self, monkeypatch):
        import paper_strategy_order as pso

        monkeypatch.setattr(pso, "load_watchlist", lambda: [])
        report = preflight.PreflightReport()
        preflight.check_watchlist(report, scan_enabled=True)
        assert not report.failures

    def test_a_contradictory_flag_posture_blocks(self):
        report = preflight.PreflightReport()
        preflight.check_flag_consistency(report, {"KIS_LIVE_ORDER_ENABLED": "true",
                                                  "ENTRY_DISABLED": "true"})
        assert report.failures[0]["reason_code"] == "CONTRADICTORY_POSTURE"

    def test_an_unwritable_log_dir_blocks(self, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        report = preflight.PreflightReport()
        preflight.check_log_dir(report, blocker / "pilot")
        assert report.failures[0]["reason_code"] == "LOG_DIR_UNWRITABLE"

    def test_an_info_row_is_not_a_failure(self):
        report = preflight.PreflightReport()
        report.info("x", "SOME_CODE", "detail")
        assert report.passed is True

    def test_the_shared_single_run_lock_is_released_again(self):
        """Held for a whole session it would starve reconciliation, the
        Shadow timer and the health report."""
        report = preflight.PreflightReport()
        preflight.check_no_other_run(report)
        assert not report.failures
        with idempotency.single_run_lock(timeout=1.0):
            pass  # still free afterwards


# ---------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------
class TestRecorder:
    def test_a_tick_is_one_json_line(self, tmp_path):
        recorder.record_tick({"tick_seq": 1, "started_at": NOW.isoformat()},
                             directory=tmp_path)
        target = recorder.tick_path(for_date=NOW.date(), directory=tmp_path)
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["tick_seq"] == 1

    def test_ticks_append_and_never_truncate(self, tmp_path):
        for seq in range(1, 4):
            recorder.record_tick({"tick_seq": seq, "started_at": NOW.isoformat()},
                                 directory=tmp_path)
        ticks, unreadable = recorder.read_ticks(for_date=NOW.date(), directory=tmp_path)
        assert [t["tick_seq"] for t in ticks] == [1, 2, 3]
        assert unreadable == []

    def test_a_torn_line_is_counted_not_silently_dropped(self, tmp_path):
        recorder.record_tick({"tick_seq": 1, "started_at": NOW.isoformat()},
                             directory=tmp_path)
        target = recorder.tick_path(for_date=NOW.date(), directory=tmp_path)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write('{"tick_seq": 2, "star\n')
        ticks, unreadable = recorder.read_ticks(for_date=NOW.date(), directory=tmp_path)
        assert len(ticks) == 1
        assert unreadable == [2]
        report = recorder.build_report(ticks, unreadable_lines=unreadable,
                                       for_date=NOW.date())
        assert report["unreadable_lines"] == [2]

    def test_free_text_is_redacted_before_it_reaches_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIS_ACCOUNT_NO", "87654321")
        recorder.record_tick({
            "tick_seq": 1, "started_at": NOW.isoformat(),
            "error": "gate blocked for account 87654321",
            "app_secret": "s3cr3t",
        }, directory=tmp_path)
        raw = recorder.tick_path(for_date=NOW.date(),
                                 directory=tmp_path).read_text(encoding="utf-8")
        assert "87654321" not in raw
        assert "s3cr3t" not in raw

    def test_the_report_tallies_entries_exits_and_errors(self):
        ticks = [
            {"tick_seq": 1, "started_at": NOW.isoformat(), "session": "regular",
             "posture": "OBSERVE", "scan": {"ran": True, "order_candidates": 4},
             "entry": {"outcomes": [
                 {"symbol": "AAPL", "result": "BLOCKED", "reason_code": "GATE:LIVE_FLAG",
                  "hypothetical": "WOULD_APPROVE"},
                 {"symbol": "MSFT", "result": "INFO",
                  "reason_code": "BELOW_SCORE_THRESHOLD", "hypothetical": None},
             ], "submitted": []},
             "exit": {"outcomes": [{"symbol": "AAPL", "decision": "SELL",
                                    "reason_code": "STOP_LOSS"}]},
             "error": None},
            {"tick_seq": 2, "started_at": NOW.isoformat(), "session": "closed",
             "posture": "OBSERVE", "skipped": True, "skip_reason": "session=closed",
             "entry": None, "exit": None, "error": "boom"},
        ]
        report = recorder.build_report(ticks, for_date=NOW.date())
        assert report["tick_count"] == 2
        assert report["sessions"] == {"regular": 1, "closed": 1}
        assert report["entry"]["evaluations"] == 2
        assert report["entry"]["distinct_symbols"] == 2
        assert report["entry"]["results"] == {"BLOCKED": 1, "INFO": 1}
        assert report["entry"]["hypothetical"] == {"WOULD_APPROVE": 1}
        assert report["exit"]["decisions"] == {"SELL": 1}
        assert report["scan"]["passes"] == 1
        assert len(report["errors"]) == 1
        assert report["skips"] == {"session=closed": 1}

    def test_the_report_is_a_pure_function_of_the_ticks(self):
        ticks = [{"tick_seq": 1, "started_at": NOW.isoformat(), "session": "regular"}]
        first = recorder.build_report(ticks, for_date=NOW.date())
        second = recorder.build_report(ticks, for_date=NOW.date())
        assert first == second

    def test_write_report_rebuilds_from_disk_and_leaves_no_temp_file(self, tmp_path):
        recorder.record_tick({"tick_seq": 1, "started_at": NOW.isoformat(),
                              "session": "regular"}, directory=tmp_path)
        target, report = recorder.write_report(for_date=NOW.date(), directory=tmp_path)
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8"))["tick_count"] == 1
        assert report["generated_at"] is not None
        assert list(tmp_path.glob("*.tmp")) == []

    def test_a_report_for_a_day_with_no_ticks_is_still_written(self, tmp_path):
        target, report = recorder.write_report(for_date=NOW.date(), directory=tmp_path)
        assert target.exists()
        assert report["tick_count"] == 0


# ---------------------------------------------------------------------
# OBSERVE dispatch reuses the shadow entrypoints
# ---------------------------------------------------------------------
class TestObserveDispatch:
    def test_it_loads_the_real_shadow_entrypoints(self):
        entry = observe.load_entrypoint(observe.ENTRY_ENTRYPOINT)
        exits = observe.load_entrypoint(observe.EXIT_ENTRYPOINT)
        assert callable(entry.run_once)
        assert callable(exits.run_once)

    def test_loading_an_entrypoint_does_not_execute_its_main(self):
        module = observe.load_entrypoint(observe.ENTRY_ENTRYPOINT)
        assert module.__name__ != "__main__"

    def test_a_missing_entrypoint_fails_closed(self, tmp_path):
        with pytest.raises(observe.EntrypointUnavailable):
            observe.load_entrypoint("no_such_entrypoint", scripts_dir=tmp_path)

    def test_entry_outcomes_are_normalised(self, monkeypatch):
        module = observe.load_entrypoint(observe.ENTRY_ENTRYPOINT)
        monkeypatch.setattr(module, "run_once", lambda **k: [
            {"symbol": "AAPL", "result": "BLOCKED", "reason_code": "GATE:LIVE_FLAG",
             "hypothetical": "WOULD_APPROVE", "run_id": "r1", "extra": "dropped"},
        ])
        section = observe.evaluate_entries(broker=None, watchlist=["AAPL"], now=NOW)
        assert section["mode"] == "OBSERVE"
        assert section["submitted"] == []
        assert section["outcomes"][0]["hypothetical"] == "WOULD_APPROVE"
        assert "extra" not in section["outcomes"][0]

    def test_exit_outcomes_are_normalised(self, monkeypatch):
        module = observe.load_entrypoint(observe.EXIT_ENTRYPOINT)
        monkeypatch.setattr(module, "run_once", lambda **k: {
            "status": "ok", "halt": False,
            "evaluated": [{"symbol": "AAPL", "position_id": "p1", "decision": "SELL",
                           "result": "APPROVED", "reason_code": "STOP_LOSS",
                           "exit_classification": "RISK_REDUCTION"}],
        })
        section = observe.evaluate_exits(broker=None, now=NOW)
        assert section["mode"] == "OBSERVE"
        assert section["outcomes"][0]["decision"] == "SELL"

    def test_zero_open_positions_is_a_result_not_an_error(self, monkeypatch):
        module = observe.load_entrypoint(observe.EXIT_ENTRYPOINT)
        monkeypatch.setattr(module, "run_once", lambda **k: {"status": "ok",
                                                             "halt": False,
                                                             "evaluated": []})
        section = observe.evaluate_exits(broker=None, now=NOW)
        assert section["evaluations"] == 0
        assert section["error"] is None


# ---------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------
class TestRunTick:
    def test_outside_the_allowed_sessions_nothing_is_evaluated(self, monkeypatch):
        broker = _ReadOnlyBroker()
        monkeypatch.setattr(runner, "current_session", lambda now=None: "closed")
        row = runner.run_tick(tick_seq=1, broker=broker, now=NOW, sessions=("regular",))
        assert row["skipped"] is True
        assert row["status"] == runner.TICK_IDLE
        assert row["entry"] is None and row["exit"] is None
        assert broker.calls == []

    def test_an_observe_tick_evaluates_both_sides(self, monkeypatch):
        monkeypatch.setattr(runner, "current_session", lambda now=None: "regular")
        monkeypatch.setattr(observe, "evaluate_entries",
                            lambda **k: {"mode": "OBSERVE", "evaluations": 2,
                                         "outcomes": [], "submitted": [], "error": None})
        monkeypatch.setattr(observe, "evaluate_exits",
                            lambda **k: {"mode": "OBSERVE", "evaluations": 0,
                                         "outcomes": [], "error": None})
        row = runner.run_tick(tick_seq=1, broker=_ReadOnlyBroker(), now=NOW,
                              sessions=("regular",), watchlist=["AAPL"])
        assert row["posture"] == posture.POSTURE_OBSERVE
        assert row["entry"]["evaluations"] == 2
        assert row["exit"]["mode"] == "OBSERVE"
        assert row["error"] is None

    def test_an_observe_tick_never_reaches_an_order_method(self, monkeypatch):
        """Run time, not static: the real shadow entrypoint drives the
        real broker double for a whole tick. analyze_stock is stubbed
        below the score threshold so the pass is deterministic and makes
        no network call."""
        import paper_strategy_order as pso

        monkeypatch.setattr(runner, "current_session", lambda now=None: "regular")
        monkeypatch.setattr(pso, "analyze_stock", lambda symbol: None)
        entry_module = observe.load_entrypoint(observe.ENTRY_ENTRYPOINT)
        monkeypatch.setattr(entry_module.pso, "analyze_stock", lambda symbol: None)
        broker = _ReadOnlyBroker()
        row = runner.run_tick(tick_seq=1, broker=broker, now=NOW, sessions=("regular",),
                              watchlist=["AAPL"])
        assert row["entry"]["error"] is None, row["entry"]
        assert "submit_order" not in broker.calls
        assert "cancel_order" not in broker.calls
        assert all(o["reason_code"] == "BELOW_SCORE_THRESHOLD"
                   for o in row["entry"]["outcomes"])

    def test_a_failing_stage_produces_a_recorded_tick_not_an_exception(self, monkeypatch):
        monkeypatch.setattr(runner, "current_session", lambda now=None: "regular")

        def _boom(**_k):
            raise RuntimeError("stage exploded")

        monkeypatch.setattr(observe, "evaluate_entries", _boom)
        monkeypatch.setattr(observe, "evaluate_exits", _boom)
        row = runner.run_tick(tick_seq=1, broker=_ReadOnlyBroker(), now=NOW,
                              sessions=("regular",), watchlist=["AAPL"])
        assert "stage exploded" in row["error"]
        assert row["entry"]["evaluations"] == 0
        assert row["finished_at"]

    def test_a_fatal_repository_fault_is_never_swallowed(self, monkeypatch):
        """It means this process may still hold the SQLite write lock;
        only exiting releases it, so it must reach the entrypoint."""
        from execution.order_repository import FatalRepositoryConnectionError

        monkeypatch.setattr(runner, "current_session", lambda now=None: "regular")

        def _fatal(**_k):
            raise FatalRepositoryConnectionError("poisoned connection")

        monkeypatch.setattr(observe, "evaluate_entries", _fatal)
        with pytest.raises(FatalRepositoryConnectionError):
            runner.run_tick(tick_seq=1, broker=_ReadOnlyBroker(), now=NOW,
                            sessions=("regular",), watchlist=["AAPL"])

    def test_a_failed_scan_does_not_end_the_tick(self, monkeypatch):
        import daily_candidate_scanner

        monkeypatch.setattr(runner, "current_session", lambda now=None: "regular")
        monkeypatch.setattr(observe, "evaluate_entries",
                            lambda **k: {"mode": "OBSERVE", "evaluations": 0,
                                         "outcomes": [], "submitted": [], "error": None})
        monkeypatch.setattr(observe, "evaluate_exits",
                            lambda **k: {"mode": "OBSERVE", "evaluations": 0,
                                         "outcomes": [], "error": None})

        def _boom(**_k):
            raise RuntimeError("yfinance down")

        monkeypatch.setattr(daily_candidate_scanner, "scan", _boom)
        row = runner.run_tick(tick_seq=1, broker=_ReadOnlyBroker(), now=NOW,
                              sessions=("regular",), scan_due=True, watchlist=[])
        assert row["scan"]["ran"] is False
        assert "yfinance down" in row["scan"]["error"]
        assert row["entry"] is not None

    def test_the_scanner_pass_never_pages_slack(self, monkeypatch):
        import daily_candidate_scanner

        seen = {}

        class _Buckets:
            candidates = strong_candidates = order_candidates = []

        def _scan(preset_name=None, send_slack=True, scan_limit=None):
            seen["send_slack"] = send_slack
            return _Buckets()

        monkeypatch.setattr(daily_candidate_scanner, "scan", _scan)
        section = runner.run_scan()
        assert seen["send_slack"] is False
        assert section["ran"] is True


# ---------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------
class TestRunLoop:
    def _quiet_tick(self, monkeypatch):
        monkeypatch.setattr(runner, "current_session", lambda now=None: "closed")

    def test_max_ticks_stops_the_loop_and_writes_the_report(self, tmp_path, monkeypatch):
        self._quiet_tick(monkeypatch)
        slept = []
        summary = runner.run_loop(
            broker=_ReadOnlyBroker(), interval=5, max_ticks=3, directory=tmp_path,
            sessions=("regular",), scan_interval=0, sleep=slept.append,
            now_fn=lambda: NOW,
        )
        assert summary["ticks"] == 3
        assert summary["recorded"] == 3
        assert summary["stopped_because"] == "max_ticks"
        assert Path(summary["report_path"]).exists()
        # No sleep after the LAST tick: the loop breaks before it.
        assert slept == [5, 5]

    def test_until_stops_the_loop(self, tmp_path, monkeypatch):
        self._quiet_tick(monkeypatch)
        summary = runner.run_loop(
            broker=_ReadOnlyBroker(), interval=0, directory=tmp_path,
            sessions=("regular",), scan_interval=0, sleep=lambda _s: None,
            now_fn=lambda: NOW, until=NOW - timedelta(seconds=1),
        )
        assert summary["ticks"] == 0
        assert summary["stopped_because"] == "until"

    def test_a_stop_signal_ends_the_loop_after_the_current_tick(self, tmp_path,
                                                               monkeypatch):
        self._quiet_tick(monkeypatch)
        stop = runner.StopSignal()

        def _sleep(_seconds):
            stop.requested = True
            stop.signal_name = "SIGTERM"

        summary = runner.run_loop(
            broker=_ReadOnlyBroker(), interval=1, directory=tmp_path,
            sessions=("regular",), scan_interval=0, sleep=_sleep, stop=stop,
            now_fn=lambda: NOW, max_ticks=None,
        )
        assert summary["ticks"] == 1
        assert summary["stopped_because"] == "signal:SIGTERM"
        assert Path(summary["report_path"]).exists()

    def test_the_report_is_written_even_when_a_tick_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "current_session", lambda now=None: "regular")

        def _boom(**_k):
            raise RuntimeError("stage exploded")

        monkeypatch.setattr(observe, "evaluate_entries", _boom)
        monkeypatch.setattr(observe, "evaluate_exits", _boom)
        monkeypatch.setattr(runner, "load_watchlist", lambda: ["AAPL"])
        summary = runner.run_loop(
            broker=_ReadOnlyBroker(), interval=0, max_ticks=1, directory=tmp_path,
            sessions=("regular",), scan_interval=0, sleep=lambda _s: None,
            now_fn=lambda: NOW,
        )
        assert len(summary["report"]["errors"]) == 1

    def test_scanning_respects_its_own_interval(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "current_session", lambda now=None: "regular")
        monkeypatch.setattr(runner, "load_watchlist", lambda: [])
        monkeypatch.setattr(observe, "evaluate_entries",
                            lambda **k: {"mode": "OBSERVE", "evaluations": 0,
                                         "outcomes": [], "submitted": [], "error": None})
        monkeypatch.setattr(observe, "evaluate_exits",
                            lambda **k: {"mode": "OBSERVE", "evaluations": 0,
                                         "outcomes": [], "error": None})
        scans = []
        monkeypatch.setattr(runner, "run_scan",
                            lambda **k: (scans.append(1) or {"ran": True,
                                                             "order_candidates": 0}))
        summary = runner.run_loop(
            broker=_ReadOnlyBroker(), interval=0, max_ticks=3, directory=tmp_path,
            sessions=("regular",), scan_interval=900, sleep=lambda _s: None,
            now_fn=lambda: NOW,
        )
        # The clock never advances, so only the first tick's scan is due.
        assert len(scans) == 1
        assert summary["ticks"] == 3

    def test_scan_interval_zero_disables_scanning(self, tmp_path, monkeypatch):
        self._quiet_tick(monkeypatch)
        called = []
        monkeypatch.setattr(runner, "run_scan", lambda **k: called.append(1))
        runner.run_loop(broker=_ReadOnlyBroker(), interval=0, max_ticks=2,
                        directory=tmp_path, sessions=("regular",), scan_interval=0,
                        sleep=lambda _s: None, now_fn=lambda: NOW)
        assert called == []


class TestPilotLock:
    def test_a_second_pilot_is_refused(self, tmp_path):
        target = tmp_path / "pilot.lock"
        with runner.PilotLock(target):
            with pytest.raises(runner.PilotLockError):
                with runner.PilotLock(target):
                    pass  # pragma: no cover

    def test_the_lock_is_released_on_exit(self, tmp_path):
        target = tmp_path / "pilot.lock"
        with runner.PilotLock(target):
            pass
        with runner.PilotLock(target):
            pass


# ---------------------------------------------------------------------
# ARMED dispatch: a caller, never a second gate
# ---------------------------------------------------------------------
class TestArmedDispatch:
    def test_entry_delegates_to_the_existing_live_cycle(self, monkeypatch):
        """The three result lists do not share a shape -- `submitted` is
        bare symbols while `blocked`/`skipped` are (symbol, reason)
        pairs. See kis_live_trading.py:464 vs :272/:303."""
        import kis_live_trading as klt

        seen = {}

        def _cycle(*, broker, now=None):
            seen["called"] = True
            return {"submitted": ["AAPL"],
                    "blocked": [("MSFT", "GATE:CASH")],
                    "skipped": [("TSLA", "did not meet score threshold")]}

        monkeypatch.setattr(klt, "run_live_buy_entry_cycle", _cycle)
        section = armed.entry_cycle(broker=_ReadOnlyBroker(), now=NOW)
        assert seen["called"] is True
        assert section["mode"] == "ARMED"
        assert section["submitted"] == ["AAPL"]
        by_symbol = {o["symbol"]: o for o in section["outcomes"]}
        assert sorted(by_symbol) == ["AAPL", "MSFT", "TSLA"]
        assert by_symbol["AAPL"]["result"] == "SUBMITTED"
        assert by_symbol["MSFT"]["reason_code"] == "GATE:CASH"
        assert by_symbol["TSLA"]["result"] == "SKIPPED"
        assert by_symbol["TSLA"]["reason_code"] == "did not meet score threshold"

    def test_a_skipped_pair_never_leaks_its_reason_into_the_symbol_field(self,
                                                                        monkeypatch):
        """The shape mismatch above, asserted directly: reading `skipped`
        as bare symbols put the whole tuple in `symbol`."""
        import kis_live_trading as klt

        monkeypatch.setattr(klt, "run_live_buy_entry_cycle", lambda **_k: {
            "submitted": [], "blocked": [],
            "skipped": [("TSLA", "not in live_rollout.allowed_symbols")],
        })
        section = armed.entry_cycle(broker=_ReadOnlyBroker(), now=NOW)
        assert section["outcomes"][0]["symbol"] == "TSLA"

    def test_a_structural_refusal_is_recorded_not_raised(self, monkeypatch):
        import kis_live_trading as klt

        def _cycle(*, broker, now=None):
            raise klt.KISLiveTradingError("ENTRY_OFF is set")

        monkeypatch.setattr(klt, "run_live_buy_entry_cycle", _cycle)
        section = armed.entry_cycle(broker=_ReadOnlyBroker(), now=NOW)
        assert section["error"].startswith("CYCLE_REFUSED")
        assert section["outcomes"] == []

    def test_exit_delegates_to_the_existing_exit_tick(self, monkeypatch):
        import kis_position_manager as kpm

        seen = {}

        def _sync(*, kis_broker, broker_adapter, now=None, conn=None):
            # The real shapes: kis_position_manager.py:332 appends a bare
            # symbol to `managed`, :273/:304 append (symbol, reason).
            seen["adapter"] = broker_adapter
            return {"synced_fills": ["AAPL"], "managed": ["AAPL"],
                    "reconciliation_blocked": [("NVDA", "qty mismatch")],
                    "skipped": [("MSFT", "no KIS fill yet")]}

        monkeypatch.setattr(kpm, "sync_kis_fills_and_manage_exits", _sync)
        section = armed.exit_cycle(broker=_ReadOnlyBroker(), now=NOW, adapter=object())
        assert section["mode"] == "ARMED"
        assert seen["adapter"] is not None
        assert section["synced_fills"] == ["AAPL"]
        by_symbol = {o["symbol"]: o for o in section["outcomes"]}
        assert sorted(by_symbol) == ["AAPL", "MSFT", "NVDA"]
        assert by_symbol["AAPL"]["decision"] == "MANAGED"
        assert by_symbol["AAPL"]["reason_code"] == "MANAGED"
        assert by_symbol["NVDA"]["reason_code"] == "qty mismatch"
        assert by_symbol["MSFT"]["reason_code"] == "no KIS fill yet"

    def test_an_aborted_exit_tick_is_recorded_not_raised(self, monkeypatch):
        import kis_position_manager as kpm

        def _sync(**_k):
            raise kpm.KISPositionManagerError("KIS position read failed")

        monkeypatch.setattr(kpm, "sync_kis_fills_and_manage_exits", _sync)
        section = armed.exit_cycle(broker=_ReadOnlyBroker(), now=NOW, adapter=object())
        assert section["error"].startswith("TICK_ABORTED")

    def test_the_exit_adapter_inherits_the_rollout_allow_list(self, monkeypatch):
        from config.live_rollout_config import LiveRolloutConfig

        monkeypatch.setenv("LIVE_ROLLOUT_ALLOWED_SYMBOLS", "AAPL,MSFT")
        rollout = LiveRolloutConfig.from_env()
        adapter = armed.build_adapter(_ReadOnlyBroker(), rollout=rollout)
        assert adapter.allowed_symbols == rollout.allowed_symbols
        assert adapter.max_price_deviation_percent == rollout.max_price_deviation_percent

    def test_the_armed_dispatch_adds_no_gate_of_its_own(self):
        """Every check must live in the shared live path. A gate here
        could disagree with the one the live service enforces."""
        source = (PILOT_DIR / "armed.py").read_text(encoding="utf-8")
        body = "\n".join(l for l in source.splitlines()
                         if not l.strip().startswith("#")).split('"""')[-1]
        for banned in ("KIS_LIVE_ORDER_ENABLED", "LIVE_ROLLOUT_ENABLED", "ENTRY_DISABLED",
                       "is_halted(", "evaluate_buy_gate(", "evaluate_sell_gate("):
            assert banned not in body, f"armed.py re-implements a gate: {banned}"


# ---------------------------------------------------------------------
# Structural safety
# ---------------------------------------------------------------------
class TestStructuralSafety:
    ORDER_CAPABLE = {
        "execution.execution_engine", "brokers.kis_broker_adapter",
        "kis_position_manager", "kis_live_trading",
    }
    # armed.py is the sanctioned dispatch and imports these LAZILY, inside
    # functions, so importing the module still reaches nothing.
    OBSERVE_SIDE = ["__init__.py", "posture.py", "preflight.py", "recorder.py",
                    "observe.py", "runner.py"]

    def _module_level_imports(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in tree.body:  # module level only
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{a.name}" for a in node.names)
        return imported

    @pytest.mark.parametrize("name", OBSERVE_SIDE)
    def test_no_observe_side_module_imports_an_order_path(self, name):
        imported = self._module_level_imports(PILOT_DIR / name)
        assert not (imported & self.ORDER_CAPABLE), (
            f"{name} imports {imported & self.ORDER_CAPABLE} at module scope")

    @pytest.mark.parametrize("name", OBSERVE_SIDE)
    def test_no_observe_side_module_imports_the_armed_dispatch_at_module_scope(self, name):
        imported = self._module_level_imports(PILOT_DIR / name)
        assert "live_pilot.armed" not in imported
        assert not any(i.endswith(".armed") for i in imported), name

    @pytest.mark.parametrize("name", OBSERVE_SIDE)
    def test_no_observe_side_module_calls_an_order_submitting_method(self, name):
        source = (PILOT_DIR / name).read_text(encoding="utf-8")
        body = "\n".join(l for l in source.splitlines()
                         if not l.strip().startswith("#")).split('"""')[-1]
        for forbidden in ("submit_order(", "cancel_order(", "submit_buy_order(",
                          "submit_sell_order(", "check_and_manage("):
            assert forbidden not in body, f"{name} calls {forbidden}"

    def test_the_armed_module_imports_the_order_path_lazily(self):
        """Inside functions, not at module scope: importing live_pilot
        must not drag the execution engine in."""
        imported = self._module_level_imports(PILOT_DIR / "armed.py")
        assert not (imported & self.ORDER_CAPABLE)

    def test_importing_the_package_does_not_import_the_execution_engine(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; import live_pilot; import live_pilot.runner; "
             "print('execution.execution_engine' in sys.modules)"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"

    @pytest.mark.parametrize("name", OBSERVE_SIDE + ["armed.py"])
    def test_no_pilot_module_writes_a_safety_flag(self, name):
        """`os.environ[FLAG] = ...` anywhere here would let the harness
        arm itself, which is the one thing it must never do."""
        tree = ast.parse((PILOT_DIR / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                attr = getattr(func, "attr", None)
                if attr in ("setenv", "putenv"):
                    pytest.fail(f"{name}:{node.lineno} sets an environment variable")
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Subscript):
                        continue
                    value = target.value
                    name_id = getattr(value, "attr", getattr(value, "id", ""))
                    assert name_id != "environ", (
                        f"{name}:{node.lineno} assigns into os.environ")


class TestPilotEntrypoint:
    def test_it_exists_and_is_executable(self):
        path = SCRIPTS_DIR / "run_live_pilot.py"
        assert path.is_file()
        assert path.stat().st_mode & 0o111

    def test_help_succeeds(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "run_live_pilot.py"), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert "usage" in result.stdout.lower()

    def test_it_imports_cleanly(self):
        result = subprocess.run(
            [sys.executable, "-c",
             f"import runpy, sys; sys.argv=['x','--help']; "
             f"runpy.run_path(r'{SCRIPTS_DIR / 'run_live_pilot.py'}', run_name='not_main')"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr

    def test_there_is_no_way_to_skip_preflight(self):
        """Checked against the parser's real options, not the prose --
        the docstring names these switches precisely to say they do not
        exist."""
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            import run_live_pilot
        finally:
            sys.path.remove(str(SCRIPTS_DIR))

        options = set()
        for action in run_live_pilot.build_parser()._actions:
            options.update(action.option_strings)
        for banned in ("--skip-preflight", "--force", "--no-preflight", "--arm"):
            assert banned not in options

    def test_the_entrypoint_never_writes_a_safety_flag(self):
        tree = ast.parse((SCRIPTS_DIR / "run_live_pilot.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript):
                        value = target.value
                        assert getattr(value, "attr", getattr(value, "id", "")) != "environ"

    def test_a_failed_preflight_refuses_to_start(self, monkeypatch, tmp_path):
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            import run_live_pilot
        finally:
            sys.path.remove(str(SCRIPTS_DIR))

        monkeypatch.setattr(run_live_pilot.runner, "build_broker",
                            lambda: _ReadOnlyBroker())
        entered = []
        monkeypatch.setattr(run_live_pilot.runner, "run_loop",
                            lambda **k: entered.append(1))
        # No reconciliation snapshot exists in this tmp environment.
        code = run_live_pilot.main(["--log-dir", str(tmp_path), "--once"])
        assert code == run_live_pilot.EXIT_PREFLIGHT_REFUSED
        assert entered == []

    def test_report_only_rebuilds_without_touching_the_broker(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            import run_live_pilot
        finally:
            sys.path.remove(str(SCRIPTS_DIR))

        recorder.record_tick({"tick_seq": 1, "started_at": NOW.isoformat(),
                              "session": "regular"}, directory=tmp_path)

        def _no_broker():  # pragma: no cover -- must never run
            raise AssertionError("--report-only constructed a broker")

        monkeypatch.setattr(run_live_pilot.runner, "build_broker", _no_broker)
        code = run_live_pilot.main(["--report-only", "--date", NOW.date().isoformat(),
                                    "--log-dir", str(tmp_path)])
        assert code == run_live_pilot.EXIT_OK
        assert recorder.report_path(for_date=NOW.date(), directory=tmp_path).exists()


class TestStarterScript:
    SCRIPT = SCRIPTS_DIR / "start_live_pilot.sh"

    def test_it_exists_and_is_executable_bash(self):
        assert self.SCRIPT.is_file()
        assert self.SCRIPT.stat().st_mode & 0o111
        result = subprocess.run(["bash", "-n", str(self.SCRIPT)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    ORDER_FLAGS = ("KIS_LIVE_ORDER_ENABLED", "LIVE_ROLLOUT_ENABLED", "ENTRY_DISABLED",
                   "ALPACA_ORDER_ENABLED", "LIVE_ENABLE_PARTIAL_PROFIT",
                   "LIVE_ENABLE_TRAILING_STOP", "LIVE_ENABLE_TIME_STOP",
                   "LIVE_ENABLE_EOD_EXIT")

    def test_it_exports_no_order_flag(self):
        raw = self.SCRIPT.read_text(encoding="utf-8")
        for flag in self.ORDER_FLAGS:
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert f"export {flag}" not in stripped, stripped

    def test_the_environment_it_hands_to_python_has_no_flag_it_did_not_receive(
            self, tmp_path):
        """Behavioural, not textual: the script is run for real with a
        stub interpreter that prints the environment it was given. The
        one echo line that mentions the flags is a report, and this is
        what proves it stayed one."""
        stub = tmp_path / "fake-python"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "env | grep -E '^(KIS_ENV|KIS_LIVE_ORDER_ENABLED|LIVE_ROLLOUT_ENABLED|"
            "ENTRY_DISABLED|ALPACA_ORDER_ENABLED|LIVE_ENABLE_[A-Z_]+)=' || true\n",
            encoding="utf-8",
        )
        os.chmod(stub, 0o755)
        env = {k: v for k, v in os.environ.items() if k not in self.ORDER_FLAGS}
        env["PYTHON_BIN"] = str(stub)
        env["KIS_ENV"] = "paper"
        result = subprocess.run(["bash", str(self.SCRIPT), "--preflight-only"],
                                capture_output=True, text=True, env=env,
                                cwd=str(REPO_ROOT))
        assert result.returncode == 0, result.stderr
        handed_over = [line for line in result.stdout.splitlines()
                       if "=" in line and not line.startswith("order flags")]
        assert "KIS_ENV=paper" in handed_over
        for flag in self.ORDER_FLAGS:
            assert not [line for line in handed_over if line.startswith(f"{flag}=")], (
                f"the starter script introduced {flag}")

    def test_it_defaults_to_the_paper_account(self):
        raw = self.SCRIPT.read_text(encoding="utf-8")
        assert 'KIS_ENV="${KIS_ENV:-paper}"' in raw

    def test_it_rejects_an_unknown_kis_env(self, tmp_path):
        env = dict(os.environ, KIS_ENV="production")
        result = subprocess.run(["bash", str(self.SCRIPT), "--preflight-only"],
                                capture_output=True, text=True, env=env,
                                cwd=str(REPO_ROOT))
        assert result.returncode != 0
        assert "KIS_ENV must be" in result.stderr

    def test_live_without_the_acknowledgement_is_refused_before_python_runs(self):
        env = dict(os.environ, KIS_ENV="live")
        env.pop("LIVE_PILOT_ACK_LIVE_ENV", None)
        env["PYTHON_BIN"] = "/nonexistent/python-that-would-fail-differently"
        result = subprocess.run(["bash", str(self.SCRIPT), "--preflight-only"],
                                capture_output=True, text=True, env=env,
                                cwd=str(REPO_ROOT))
        assert result.returncode != 0
        assert "LIVE_PILOT_ACK_LIVE_ENV" in result.stderr

    def test_it_delegates_to_the_python_entrypoint(self):
        raw = self.SCRIPT.read_text(encoding="utf-8")
        assert "run_live_pilot.py" in raw
        assert 'exec "${PYTHON_BIN}" "${PILOT_SCRIPT}" "$@"' in raw
