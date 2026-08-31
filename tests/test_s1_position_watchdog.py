"""A held position with nothing evaluating it must be noticed.

The failure this encodes happened on 2026-08-18: the executor cron was
paused to free a contended rate limiter, a TX position had already filled,
and for about an hour a real holding had no exit evaluation running.
Nothing raised.

The second property matters as much as the first: it must stay quiet when
silence is CORRECT, because a watchdog that fires every evening gets muted.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import run_s1_position_watchdog as wd  # noqa: E402

from s1_live import position_store as ps  # noqa: E402
from state_store import db as sdb  # noqa: E402

NOW = datetime(2026, 8, 18, 17, 30, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s.db"))
    monkeypatch.setenv("S1_LIVE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(wd, "ROOT", str(REPO_ROOT))
    conn = sdb.open_db()
    sdb.init_db(conn)
    yield tmp_path, conn
    conn.close()


def hold(conn, symbol="TX"):
    return ps.open_position(conn, symbol=symbol, strategy_id="hma_early_trend",
                            signal_id="sig", entry_price=53.68, quantity=1)


def write_ticks(tmp_path, day, stamps):
    directory = tmp_path / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"cycles-{day}.jsonl"
    with path.open("w") as fh:
        for stamp in stamps:
            fh.write(json.dumps({"started_at": stamp.isoformat(),
                                 "trading_day": day}) + "\n")
    return path


def in_session(monkeypatch, value=True):
    monkeypatch.setattr(wd, "ticks_expected_now", lambda: value)


def same_day(monkeypatch, day="2026-08-18"):
    import scanners.base.trading_calendar as cal
    monkeypatch.setattr(cal, "us_trading_day", lambda *a, **k: day)
    return day


class TestItStaysQuietWhenSilenceIsCorrect:
    def test_no_position_means_nothing_to_watch(self, env, monkeypatch):
        tmp_path, conn = env
        in_session(monkeypatch)
        result = wd.check(now=NOW)
        assert result["status"] == wd.STATUS_NO_POSITION
        assert result["open_positions"] == []

    def test_outside_the_session_a_held_position_is_not_unmanaged(self, env, monkeypatch):
        """Overnight there is no tick due and nothing to sell into."""
        tmp_path, conn = env
        hold(conn)
        in_session(monkeypatch, False)
        assert wd.check(now=NOW)["status"] == wd.STATUS_SESSION_IDLE

    def test_an_unreadable_clock_produces_silence_not_a_false_alarm(self, monkeypatch):
        import market_hours

        def boom(*a, **k):
            raise RuntimeError("no clock")

        monkeypatch.setattr(market_hours, "get_market_state", boom)
        assert wd.ticks_expected_now() is False

    def test_a_recent_tick_is_healthy(self, env, monkeypatch):
        tmp_path, conn = env
        hold(conn)
        in_session(monkeypatch)
        day = same_day(monkeypatch)
        write_ticks(tmp_path, day, [NOW - timedelta(minutes=5)])
        result = wd.check(now=NOW)
        assert result["status"] == wd.STATUS_HEALTHY
        assert result["silence_minutes"] == pytest.approx(5.0, abs=0.1)


class TestItFiresOnTheFailureThatHappened:
    def test_a_paused_executor_with_a_held_position_is_stale(self, env, monkeypatch):
        """The 2026-08-18 shape: position filled, then ticks stopped."""
        tmp_path, conn = env
        hold(conn, "TX")
        in_session(monkeypatch)
        day = same_day(monkeypatch)
        write_ticks(tmp_path, day, [NOW - timedelta(minutes=61)])
        result = wd.check(now=NOW)
        assert result["status"] == wd.STATUS_STALE
        assert "TX" in result["detail"]
        assert result["silence_minutes"] == pytest.approx(61.0, abs=0.1)

    def test_no_tick_at_all_while_holding_is_stale(self, env, monkeypatch):
        tmp_path, conn = env
        hold(conn)
        in_session(monkeypatch)
        same_day(monkeypatch)
        result = wd.check(now=NOW)
        assert result["status"] == wd.STATUS_STALE
        assert result["newest_tick_at"] is None

    def test_the_newest_tick_is_what_counts_not_the_first(self, env, monkeypatch):
        tmp_path, conn = env
        hold(conn)
        in_session(monkeypatch)
        day = same_day(monkeypatch)
        write_ticks(tmp_path, day, [NOW - timedelta(hours=3), NOW - timedelta(minutes=2)])
        assert wd.check(now=NOW)["status"] == wd.STATUS_HEALTHY

    def test_the_boundary_is_the_configured_limit(self, env, monkeypatch):
        tmp_path, conn = env
        hold(conn)
        in_session(monkeypatch)
        day = same_day(monkeypatch)
        write_ticks(tmp_path, day, [NOW - timedelta(minutes=30)])
        assert wd.check(now=NOW, max_silence_minutes=40)["status"] == wd.STATUS_HEALTHY
        assert wd.check(now=NOW, max_silence_minutes=20)["status"] == wd.STATUS_STALE

    def test_the_default_allows_more_than_two_intervals(self):
        """A single tick has been measured at ~11.5 minutes, so one 15-min
        interval of headroom is not enough."""
        assert wd.DEFAULT_MAX_SILENCE_MINUTES >= 30

    def test_malformed_log_lines_are_skipped_not_fatal(self, env, monkeypatch):
        tmp_path, conn = env
        hold(conn)
        in_session(monkeypatch)
        day = same_day(monkeypatch)
        path = write_ticks(tmp_path, day, [NOW - timedelta(minutes=3)])
        with path.open("a") as fh:
            fh.write("not json\n{}\n{\"started_at\": \"nonsense\"}\n")
        assert wd.check(now=NOW)["status"] == wd.STATUS_HEALTHY


class TestEscalationBlocksEntryNotExit:
    def test_it_escalates_to_entry_disabled_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "ks.json"))
        import kill_switch_state as kss

        # Escalation is for a LIVE strategy. S1 became DISCOVERY_ONLY on
        # 2026-08-31; the behaviour under test is what happens while a
        # strategy IS live, so that is what is set up.
        monkeypatch.setattr(wd, "s1_is_live", lambda: True)
        assert wd.escalate({"detail": "test"}) is True
        assert kss.get_state() == kss.ENTRY_DISABLED
        assert kss.is_entry_allowed() is False
        kss.activate(kss.ACTIVE, reason="cleanup", activated_by="pytest")

    def test_it_never_halts_all_trading(self):
        """ALL_TRADING_DISABLED would take away the exit path the position
        needs most."""
        source = (REPO_ROOT / "scripts" / "run_s1_position_watchdog.py").read_text()
        assert "ALL_TRADING_DISABLED" not in source.split('"""')[2]
        assert "ENTRY_DISABLED" in source

    def test_it_does_not_override_an_existing_non_active_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "ks.json"))
        import kill_switch_state as kss

        kss.activate(kss.MANUAL_REVIEW, reason="operator", activated_by="ops")
        assert wd.escalate({"detail": "test"}) is False
        assert kss.get_state() == kss.MANUAL_REVIEW
        kss.activate(kss.ACTIVE, reason="cleanup", activated_by="pytest")

    def test_report_only_mode_touches_nothing(self, env, monkeypatch, tmp_path):
        tmp_path_env, conn = env
        hold(conn)
        in_session(monkeypatch)
        day = same_day(monkeypatch)
        write_ticks(tmp_path_env, day, [NOW - timedelta(hours=2)])
        monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path_env / "ks.json"))
        import kill_switch_state as kss

        code = wd.main(["--no-escalate", "--max-silence-minutes", "40"])
        assert code == 1, "a stale check still reports failure"
        assert kss.get_state() == "ACTIVE", "report-only must not escalate"


class TestItPlacesNoOrders:
    def test_the_watchdog_cannot_submit(self):
        import ast

        source = (REPO_ROOT / "scripts" / "run_s1_position_watchdog.py").read_text()
        forbidden = {"execution_engine", "kis_broker", "brokers"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [getattr(node, "module", "") or ""] + [a.name for a in node.names]
                for name in names:
                    assert str(name).split(".")[0] not in forbidden, name
        for banned in ("submit_order", "submit_buy_order", "submit_sell_order"):
            assert banned not in source, banned


class TestANonLiveStrategyDoesNotDisableTheAccount:
    """The escalation is account-wide: it stops S6's entries too.

    On 2026-08-31 it did exactly that for forty minutes over an S1
    reading that was false. Once S1 is DISCOVERY_ONLY it holds no real
    position and cannot place a real order, so an S1 cycle going quiet
    is a paper-side problem -- worth reporting, never worth disabling
    the one strategy that is actually trading.
    """

    def test_a_non_live_strategy_reports_without_escalating(self, tmp_path,
                                                            monkeypatch):
        monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "ks.json"))
        import kill_switch_state as kss

        monkeypatch.setattr(wd, "s1_is_live", lambda: False)
        assert wd.escalate({"detail": "stale"}) is False
        assert kss.get_state() == "ACTIVE"
        assert kss.is_entry_allowed() is True

    def test_the_production_table_has_s1_not_live(self):
        """So in production today this path does not escalate."""
        from config import scanner_live_mode

        assert scanner_live_mode.is_limited_live("hma_early_trend") is False
        assert wd.s1_is_live() is False

    def test_an_unreadable_table_keeps_the_escalation(self, monkeypatch):
        """Fails closed: a config that cannot be read must not silently
        disarm the watchdog."""
        from config import scanner_live_mode

        monkeypatch.setattr(
            scanner_live_mode, "is_limited_live",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreadable")))
        assert wd.s1_is_live() is True

    def test_the_stale_status_is_still_produced(self):
        """Not escalating is not the same as not noticing."""
        source = (REPO_ROOT / "scripts"
                  / "run_s1_position_watchdog.py").read_text()
        assert "STATUS_STALE" in source
        assert "notify_monitor" in source
