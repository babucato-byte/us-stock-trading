"""The daily Slack health report certifies facts, not git cleanliness.

Every scenario the 2026-09-08 audit listed as a false positive is a
case here: each one must surface as FAIL or WARN, and the message must
never call the system NORMAL over it. The paper-trading label and the
Alpaca CSV are gone; the performance block reads the live state DB.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import trading_health_check as hc  # noqa: E402

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)      # Tuesday 05:00 ET
SHA = "dca80eac1757b7f7f8ea29fcf4bdee8225e58ac4"
DAY = "2026-09-04"   # the last completed trading day before NOW (Monday holiday)

CRON = "\n".join(f"* * * * * /x/{job}" for job in hc.REQUIRED_CRON_JOBS)
PS = f"/home/ubuntu/releases/us-stock-trading/{SHA}/venv/bin/python scripts/run_realtime_bar_collector.py --symbols X\n"


def _manifest(profile=None, session=None, status="SUCCESS", started="2026-09-04T13:52:12+00:00"):
    return json.dumps({"profile": profile, "run_status": status, "started_at": started,
                       "run_id": f"20260904_{session or profile}"})


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A complete, healthy production-shaped world in tmp."""
    root = tmp_path / "scanner"
    (root / "logs" / "scanners" / "runs").mkdir(parents=True)
    (root / "logs" / "scanners" / "signals").mkdir(parents=True)
    (root / "logs" / "cron").mkdir(parents=True)
    (root / "realtime_bars").mkdir(parents=True)
    (root / "logs" / "scanners" / "runs" / f"{DAY}.jsonl").write_text(
        "\n".join([_manifest(profile="premarket"), _manifest(profile="open"),
                   _manifest(profile="daily"),
                   _manifest(session="OVERNIGHT_DAYTIME"), _manifest(session="REGULAR")]) + "\n")
    (root / "logs" / "scanners" / "signals" / f"{DAY}.jsonl").write_text("{}\n{}\n")
    (root / "realtime_bars" / "collector_status.json").write_text(json.dumps({
        "state": "CONNECTED_NO_TRADES", "connection_state": "CONNECTED",
        "subscription_requested": 32, "subscription_count": 32}))
    (root / "logs" / "cron" / "s6_buy_entry.log").write_text(
        f"{(NOW - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')} tick sha={SHA}\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "RECONCILIATION.json").write_text(json.dumps({
        "checked_at": (NOW - timedelta(minutes=3)).isoformat(), "clean": True,
        "mismatch_count": 0, "unknown_count": 0, "halt": False}))
    (state / "KIS_TOKEN_CACHE.json").write_text(json.dumps({
        "expires_at": NOW.timestamp() + 3600, "created_at": NOW.timestamp() - 600}))
    (state / "OPS_HALT.json").write_text(json.dumps({"halted": False}))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(state / "OPS_HALT.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(state / "KILL_SWITCH.json"))
    monkeypatch.delenv(hc_ev_name(), raising=False)
    env = {
        "KIS_ENV": "live", "EXECUTION_BROKER": "kis", "KIS_LIVE_ORDER_ENABLED": "true",
        "LIVE_ROLLOUT_ENABLED": "true", "DEPLOYED_COMMIT": SHA, "VALIDATED_COMMIT": SHA,
        "SCANNER_DATA_ROOT": str(root), "RECONCILIATION_STATE_FILE": str(state / "RECONCILIATION.json"),
        "KIS_TOKEN_CACHE_FILE": str(state / "KIS_TOKEN_CACHE.json"),
        "KIS_ACCOUNT_NO": "44000096", "KIS_ALLOWED_ACCOUNT_NO": "44000096",
        "ENTRY_DISABLED": "false", "STATE_STORE_DB_FILE": str(state / "TRADING_STATE.db"),
    }
    monkeypatch.setattr(hc, "_run", lambda cmd, cwd=None: SHA if cmd[:2] == ["git", "rev-parse"] else "")
    monkeypatch.setattr(hc, "_last_trading_day", lambda now: DAY)
    return {"env": env, "root": root, "state": state}


def hc_ev_name():
    from brokers import route_evidence

    return route_evidence.EVIDENCE_FILE_ENV


def report(world, **overrides):
    env = dict(world["env"])
    env.update(overrides.pop("env", {}))
    kwargs = dict(now=NOW, crontab_text=CRON, ps_text=PS)
    kwargs.update(overrides)
    return hc.build_report(env, **kwargs)


class TestAHealthyWorldIsNormal:
    def test_normal_with_no_failures(self, world):
        rep = report(world)
        assert rep["failed"] == [], rep["checks"]
        assert rep["overall"] == "NORMAL"

    def test_the_message_has_no_paper_label_and_no_git_verdict(self, world):
        text = hc.format_message(report(world))
        assert "페이퍼" not in text
        assert "Git 변경 파일" not in text
        assert "Overall: NORMAL" in text
        assert "DAYTIME:" in text and "BUY " in text
        source = (REPO_ROOT / "trading_health_check.py").read_text()
        assert "performance_trades.csv" not in source
        assert "페이퍼" not in source.split('"""', 2)[2]   # allowed only in the docstring history

    def test_daytime_routes_are_reported_from_the_matrix(self, world):
        rep = report(world)
        legs = {c["name"]: c for c in rep["checks"] if c["name"].startswith("daytime:")}
        assert legs["daytime:sell"]["detail"] == "VERIFIED"
        assert legs["daytime:buy"]["detail"] in ("VERIFIED", "LIVE_RESPONSE_PENDING")
        assert legs["daytime:buy"]["verdict"] in ("OK", "WARN")


class TestEveryAuditedFalsePositiveIsCaught:
    def test_1_scanner_not_run(self, world):
        (world["root"] / "logs" / "scanners" / "runs" / f"{DAY}.jsonl").unlink()
        rep = report(world)
        assert "scanner" in rep["failed"] and rep["overall"] == "ATTENTION"

    def test_2_scanner_ran_but_never_succeeded(self, world):
        (world["root"] / "logs" / "scanners" / "runs" / f"{DAY}.jsonl").write_text(
            _manifest(profile="open", status="FAILED_NO_UNIVERSE") + "\n")
        rep = report(world)
        assert "scanner:open" in rep["failed"]

    def test_3_entry_runner_silent(self, world):
        (world["root"] / "logs" / "cron" / "s6_buy_entry.log").write_text(
            f"{(NOW - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')} tick\n")
        assert "entry_runner" in report(world)["failed"]

    def test_4_kis_token_missing_is_a_warning_not_normal(self, world):
        rep = report(world, env={"KIS_TOKEN_CACHE_FILE": "/nonexistent"})
        assert "kis_token" in rep["warned"]
        assert "WARN: " in hc.format_message(rep)

    def test_5_reconciliation_not_clean(self, world):
        (world["state"] / "RECONCILIATION.json").write_text(json.dumps({
            "checked_at": NOW.isoformat(), "clean": False, "mismatch_count": 1,
            "unknown_count": 0, "halt": False}))
        assert "reconciliation" in report(world)["failed"]

    def test_5b_reconciliation_unknown_orders(self, world):
        (world["state"] / "RECONCILIATION.json").write_text(json.dumps({
            "checked_at": NOW.isoformat(), "clean": True, "mismatch_count": 0,
            "unknown_count": 2, "halt": False}))
        assert "reconciliation" in report(world)["failed"]

    def test_6_collector_dead(self, world):
        rep = report(world, ps_text="")
        assert "collector:process" in rep["failed"]

    def test_6b_collector_disconnected(self, world):
        (world["root"] / "realtime_bars" / "collector_status.json").write_text(json.dumps({
            "state": "DISCONNECTED", "connection_state": "DISCONNECTED",
            "subscription_requested": 32, "subscription_count": 0}))
        assert "collector:connected" in report(world)["failed"]

    def test_7_cron_job_missing(self, world):
        rep = report(world, crontab_text=CRON.replace("s6_buy_entry.sh", "x.sh"))
        assert "cron" in rep["failed"]
        assert "s6_buy_entry.sh" in [c for c in rep["checks"] if c["name"] == "cron"][0]["detail"]

    def test_8_not_live_is_never_shown_as_live(self, world):
        rep = report(world, env={"KIS_ENV": "paper"})
        assert "trading_mode" in rep["failed"]
        assert "NOT LIVE" in hc.format_message(rep)

    def test_9_release_drift(self, world):
        rep = report(world, env={"DEPLOYED_COMMIT": "0" * 40})
        assert "release" in rep["failed"]

    def test_10_kill_switch_on(self, world):
        rep = report(world, env={"ENTRY_DISABLED": "true"})
        assert "kill_switch" in rep["failed"]

    def test_11_account_mismatch(self, world):
        rep = report(world, env={"KIS_ALLOWED_ACCOUNT_NO": "00000000"})
        assert "kis_account" in rep["failed"]

    def test_recent_errors_are_surfaced(self, world):
        (world["root"] / "logs" / "cron" / "s6_scan.log").write_text(
            f"{NOW.strftime('%Y-%m-%d %H:%M:%S')},000 ERROR scanners.runner boom\n")
        rep = report(world)
        assert "recent_errors" in rep["warned"]


class TestLivePerformanceComesFromTheStateDb:
    def test_closed_trades_wins_losses_and_pnl(self, world, monkeypatch):
        monkeypatch.setenv("STATE_STORE_DB_FILE", world["env"]["STATE_STORE_DB_FILE"])
        from state_store.db import open_db
        from s6_live import position_store as ps

        with open_db() as conn:
            for symbol, entry, exit_price in (("AAA", 10.0, 11.0), ("BBB", 10.0, 9.5)):
                pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                           entry_session="REGULAR", range_high=9.0,
                                           range_low=8.0, now=NOW)
                ps.open_from_fill(conn, pid, quantity=2, average_fill_price=entry, now=NOW)
                ps.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=NOW)
                ps.close_position(conn, pid, reason="RANGE_REENTRY", exit_price=exit_price, now=NOW)
            pid = ps.record_submission(conn, symbol="CCC", variant="S6-R", entry_session="REGULAR",
                                       range_high=9.0, range_low=8.0, now=NOW)
            ps.abandon_submission(conn, pid, reason="BUY_NEVER_FILLED", now=NOW)
        perf = hc.live_performance(world["env"]["STATE_STORE_DB_FILE"])
        assert perf["available"] is True
        assert perf["closed_trades"] == 2 and perf["wins"] == 1 and perf["losses"] == 1
        assert perf["realized_pnl_usd"] == pytest.approx(1.0)
        assert perf["open_positions"] == 0 and perf["buy_never_filled"] == 1
        text = hc.format_message(report(world))
        assert "LIVE 실거래 성과" in text and "청산 거래: 2건" in text

    def test_a_missing_db_says_so_rather_than_zero(self, world):
        perf = hc.live_performance("/nonexistent/TRADING_STATE.db")
        assert perf["available"] is False
        assert "성과 조회 불가" in hc.format_message(report(world))


class TestItIsReadOnly:
    def test_no_writes_or_order_calls(self):
        source = (REPO_ROOT / "trading_health_check.py").read_text()
        for forbidden in ("submit_order", "kis_live_trading", "execution_engine",
                          "write_text(", "os.replace", "ALTER TABLE", "run_migrations"):
            assert forbidden not in source, forbidden
        assert "mode=ro" in source
