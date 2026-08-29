"""Every READY candidate leaves a trace, including the refused ones.

Only candidates that became orders left any record, so a signal refused
at a gate simply vanished. That makes "is this gate blocking good
trades" unanswerable, and every proposed threshold an opinion — which is
exactly the position the improvement spec is in.

This log is the measurement that has to exist before any of those
changes is worth discussing. It is written after the decision, read by
people, and feeds nothing.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import shadow_signal_log as ssl  # noqa: E402

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)
DAY = "2026-08-31"


class _Features:
    price = 12.14
    vwap = 12.10
    ema9 = 12.12
    ema21 = 12.08
    range_high = 12.11
    range_low = 12.00
    extension_pct = 0.25
    volume = 5000.0
    volume_status = "VOLUME_OK"
    volume_expansion = 1.45
    bar_count = 18
    market_data_asof = datetime(2026, 8, 31, 13, 59, tzinfo=timezone.utc)
    price_source = "KIS_HDFSCNT0"
    volume_source = "KIS_HDFSCNT0"
    feed_status = "LIVE"
    gap_detected = False


class TestTheSignalIsRecordedAsItStood:
    def test_the_features_come_from_the_snapshot(self):
        row = ssl.build_record(symbol="owl", session="REGULAR",
                               outcome=ssl.OUTCOME_EXECUTABLE,
                               strategy_id="S6_ORB_BREAKOUT_V1",
                               features=_Features(), now=NOW)
        assert row["symbol"] == "OWL"
        assert row["vwap"] == 12.10
        assert row["volume_expansion"] == 1.45
        assert row["market_data_asof"] == "2026-08-31T13:59:00+00:00"

    def test_provenance_travels_with_the_signal(self):
        row = ssl.build_record(symbol="OWL", session="REGULAR",
                               outcome=ssl.OUTCOME_EXECUTABLE,
                               strategy_id="S6", features=_Features(), now=NOW)
        assert row["price_source"] == "KIS_HDFSCNT0"
        assert row["feed_status"] == "LIVE"
        assert row["gap_detected"] is False

    def test_candidate_rank_and_score_are_kept(self):
        row = ssl.build_record(
            symbol="OWL", session="REGULAR", outcome=ssl.OUTCOME_EXECUTABLE,
            strategy_id="S6", candidate={"rank": 3, "score": 88.5,
                                         "variant": "S6-R"}, now=NOW)
        assert row["candidate_rank"] == 3
        assert row["candidate_score"] == 88.5

    def test_missing_features_do_not_break_the_record(self):
        row = ssl.build_record(symbol="OWL", session="REGULAR",
                               outcome=ssl.OUTCOME_NOT_READY,
                               strategy_id="S6", features=None, now=NOW)
        assert row["symbol"] == "OWL"
        assert row["outcome"] == ssl.OUTCOME_NOT_READY


class TestOnlyTheFirstBlockerDecided:
    def test_it_is_stored_separately_from_the_full_map(self):
        """Every later gate's verdict is conditional on the earlier ones
        passing, so a flat list of failures invites reading them as
        independent reasons when only the first decided anything."""
        row = ssl.build_record(
            symbol="OWL", session="REGULAR", outcome=ssl.OUTCOME_BLOCKED,
            strategy_id="S6", first_blocked_by="CASH",
            gate_results={"SESSION": "PASS", "CASH": "FAIL"}, now=NOW)
        assert row["first_blocked_by"] == "CASH"
        assert row["gate_results"]["SESSION"] == "PASS"

    def test_the_watch_blocking_list_is_kept_whole(self):
        row = ssl.build_record(
            symbol="OWL", session="REGULAR", outcome=ssl.OUTCOME_NOT_READY,
            strategy_id="S6",
            watch_blocking=["VOLUME_EXPANSION", "EMA9_ABOVE_EMA21"], now=NOW)
        assert row["watch_blocking"] == ["VOLUME_EXPANSION", "EMA9_ABOVE_EMA21"]

    def test_reasons_are_counted_by_what_actually_decided(self, tmp_path):
        env = {"SHADOW_SIGNAL_DIR": str(tmp_path)}
        for reason in ("CASH", "CASH", "VOLUME_EXPANSION"):
            ssl.append(ssl.build_record(
                symbol="X", session="REGULAR", outcome=ssl.OUTCOME_BLOCKED,
                strategy_id="S6", first_blocked_by=reason, now=NOW),
                trading_day=DAY, env=env)
        assert ssl.blocked_reasons(DAY, env=env) == {"CASH": 2,
                                                     "VOLUME_EXPANSION": 1}


class TestItIsAppendOnlyAndSurvivesFailure:
    def test_rows_accumulate(self, tmp_path):
        env = {"SHADOW_SIGNAL_DIR": str(tmp_path)}
        for symbol in ("A", "B", "C"):
            ssl.append(ssl.build_record(
                symbol=symbol, session="REGULAR",
                outcome=ssl.OUTCOME_EXECUTABLE, strategy_id="S6", now=NOW),
                trading_day=DAY, env=env)
        assert [r["symbol"] for r in ssl.read(DAY, env=env)] == ["A", "B", "C"]

    def test_a_missing_day_is_empty_not_an_error(self, tmp_path):
        assert ssl.read("2020-01-01", env={"SHADOW_SIGNAL_DIR": str(tmp_path)}) == []

    def test_one_bad_line_does_not_discard_the_day(self, tmp_path):
        env = {"SHADOW_SIGNAL_DIR": str(tmp_path)}
        ssl.append(ssl.build_record(symbol="A", session="REGULAR",
                                    outcome=ssl.OUTCOME_EXECUTABLE,
                                    strategy_id="S6", now=NOW),
                   trading_day=DAY, env=env)
        path = ssl.log_path(DAY, env=env)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{ not json\n")
        ssl.append(ssl.build_record(symbol="B", session="REGULAR",
                                    outcome=ssl.OUTCOME_EXECUTABLE,
                                    strategy_id="S6", now=NOW),
                   trading_day=DAY, env=env)
        assert [r["symbol"] for r in ssl.read(DAY, env=env)] == ["A", "B"]

    def test_an_unwritable_path_returns_false_rather_than_raising(self, tmp_path):
        """Losing an observation must never cost a trade."""
        blocked = tmp_path / "ro"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            ok = ssl.append({"x": 1}, trading_day=DAY,
                            env={"SHADOW_SIGNAL_DIR": str(blocked)})
            assert ok is False
        finally:
            blocked.chmod(0o700)


class TestItCannotAffectATrade:
    def test_nothing_in_it_reaches_an_execution_path(self):
        source = (REPO_ROOT / "s6_live" / "shadow_signal_log.py").read_text(
            encoding="utf-8")
        for forbidden in ("submit_buy_order", "submit_sell_order",
                          "order_gate", "execution_engine", "KISBroker"):
            assert forbidden not in source, forbidden

    def test_it_does_not_write_to_the_order_database(self):
        """The trading path already contends on it; an observability
        write must never be what delays an order."""
        source = (REPO_ROOT / "s6_live" / "shadow_signal_log.py").read_text(
            encoding="utf-8")
        assert "state_store" not in source
        assert "order_repository" not in source

    def test_the_cycle_records_after_trading_and_swallows_failure(self):
        runner = (REPO_ROOT / "scripts" / "run_live_buy_entry.py").read_text(
            encoding="utf-8")
        body = runner[runner.index("def _record_shadow_signals"):]
        assert "except Exception" in body
        assert "already finished trading" in body

    def test_submitted_candidates_are_recorded_too(self):
        runner = (REPO_ROOT / "scripts" / "run_live_buy_entry.py").read_text(
            encoding="utf-8")
        assert "OUTCOME_SUBMITTED" in runner
