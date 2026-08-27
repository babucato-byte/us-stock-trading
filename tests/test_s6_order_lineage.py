"""Why was this bought? One row, and it must be able to say.

Reconstructing the DT entry took four sources -- a candidate JSONL, the
shadow audit trail, the order ledger and the position row -- and the
fact that mattered most was in none of them: the candidate's market data
was hours older than its `generated_at`. Both timestamps are columns
here for exactly that reason. A record that cannot express the failure
it exists to explain is not a record.

Nothing here places an order.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import lineage, precision_watch as pw  # noqa: E402
from s6_live import realtime_features as rf  # noqa: E402

NOW = datetime(2026, 8, 26, 20, 41, tzinfo=timezone.utc)
S6 = "S6_ORB_BREAKOUT_V1"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


class TestTheTwoTimestampsAreSeparate:
    def test_a_fresh_candidate_over_stale_data_is_visible_in_one_row(self, conn):
        """The DT entry, exactly: published 20:38, describing 15:55."""
        lineage.record(
            conn, symbol="DT", strategy_id=S6, internal_order_id="kislive-DT-1",
            candidate_generated_at="2026-08-26T20:38:49+00:00",
            market_data_asof="2026-08-26T19:55:00+00:00",
            rank=4, score=72.82, order_price=52.75, quantity=1,
            session="AFTER_HOURS", now=NOW)
        row = lineage.explain(conn, symbol="DT")[0]
        assert row["candidate_generated_at"] == "2026-08-26T20:38:49+00:00"
        assert row["market_data_asof"] == "2026-08-26T19:55:00+00:00"
        # The gap that allowed the trade, answerable from this row alone.
        published = datetime.fromisoformat(row["candidate_generated_at"])
        observed = datetime.fromisoformat(row["market_data_asof"])
        assert (published - observed) > timedelta(minutes=40)

    def test_the_order_and_the_candidate_are_both_recoverable(self, conn):
        lineage.record(conn, symbol="DT", strategy_id=S6,
                       internal_order_id="kislive-DT-5ecc2a6503f0",
                       broker_order_id="0030809002",
                       position_id="s6pos_e1a88ae94f624642",
                       generation_id="gen-1", candidate_id="cand-1",
                       rank=4, score=72.82, now=NOW)
        row = lineage.explain(conn, internal_order_id="kislive-DT-5ecc2a6503f0")[0]
        assert row["broker_order_id"] == "0030809002"
        assert row["position_id"] == "s6pos_e1a88ae94f624642"
        assert row["generation_id"] == "gen-1"
        assert row["rank"] == 4


class TestItTakesItsValuesFromTheWatch:
    def _watch(self, **overrides):
        feats = rf.SessionFeatures(
            symbol="DT", session="AFTER_HOURS",
            market_data_asof=datetime(2026, 8, 26, 19, 55, tzinfo=timezone.utc),
            built_at=NOW, price=52.75, **overrides)
        return pw.WatchEvaluation(
            symbol="DT", session="AFTER_HOURS", state=pw.READY_TO_BUY,
            conditions={n: pw.PASS for n in pw.CONDITION_ORDER},
            features=feats, evaluated_at=NOW)

    def test_from_watch_carries_the_decision_that_was_actually_made(self):
        fields = lineage.from_watch(
            self._watch(), candidate={"rank": 4, "score": 72.82,
                                      "generated_at": "2026-08-26T20:38:49+00:00"})
        assert fields["market_data_asof"] == "2026-08-26T19:55:00+00:00"
        assert fields["candidate_generated_at"] == "2026-08-26T20:38:49+00:00"
        assert fields["watch_state"] == pw.READY_TO_BUY
        assert fields["ready_evaluated_at"] == NOW.isoformat()

    def test_the_conditions_that_passed_are_kept(self, conn):
        fields = lineage.from_watch(self._watch(), candidate={})
        lineage.record(conn, symbol="DT", strategy_id=S6, now=NOW, **fields)
        row = lineage.explain(conn, symbol="DT")[0]
        assert pw.C_MARKET_DATA_FRESH in row["watch_conditions"]

    def test_a_watch_with_no_features_still_records(self):
        evaluation = pw.WatchEvaluation(symbol="DT", session="REGULAR",
                                        state=pw.WATCHING)
        fields = lineage.from_watch(evaluation, candidate={})
        assert fields["market_data_asof"] is None
        assert fields["watch_state"] == pw.WATCHING


class TestPaperworkNeverBlocksATrade:
    def test_a_broken_insert_returns_None_rather_than_raising(self, conn):
        conn.execute("DROP TABLE order_lineage")
        conn.commit()
        assert lineage.record(conn, symbol="DT", strategy_id=S6,
                              now=NOW) is None

    def test_explain_on_a_missing_table_returns_nothing(self, conn):
        conn.execute("DROP TABLE order_lineage")
        conn.commit()
        assert lineage.explain(conn, symbol="DT") == []

    def test_the_entry_path_records_it_without_depending_on_it(self, conn):
        """The order is already sent by the time lineage is written."""
        from s6_live import entry_lifecycle

        conn.execute("DROP TABLE order_lineage")
        conn.commit()
        position_id = entry_lifecycle.record_entry_submission(
            conn, symbol="DT", session="AFTER_HOURS",
            client_order_id="kislive-DT-1", now=NOW)
        assert position_id  # the position exists regardless

    def test_the_entry_path_writes_a_lineage_row(self, conn):
        from s6_live import entry_lifecycle

        entry_lifecycle.record_entry_submission(
            conn, symbol="DT", session="AFTER_HOURS",
            client_order_id="kislive-DT-1",
            candidate_row={"rank": 4, "score": 72.82,
                           "generated_at": "2026-08-26T20:38:49+00:00"},
            now=NOW)
        rows = lineage.explain(conn, symbol="DT")
        assert len(rows) == 1
        assert rows[0]["strategy_id"] == S6
        assert rows[0]["rank"] == 4
        assert rows[0]["internal_order_id"] == "kislive-DT-1"

    def test_it_is_never_read_by_the_order_path(self):
        """A record, not an input."""
        import inspect

        from execution import order_gate
        from s6_live import precision_watch

        for module in (order_gate, precision_watch):
            assert "lineage" not in inspect.getsource(module)
