"""What a scan found, written down. Not what to buy.

The boundary is the point of this file. The scanner runtime observes and
the trading runtime orders, and this module is the only thing that
crosses between them -- in one direction, as a record. So the tests that
matter most are the ones asserting what it CANNOT do: reach a broker,
reach an account, or re-open Candidate Decision.

The rest is about a record being reproducible. A candidate row that
cannot say which scanner version, which config and which session produced
it is not evidence six weeks later, it is a ticker.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.publish import candidates as pub  # noqa: E402

MODULE = REPO_ROOT / "scanners" / "publish" / "candidates.py"


class Signal:
    """Enough of a ScannerSignal to publish."""

    def __init__(self, symbol, score, price=100.0, **kw):
        self.symbol, self.scanner_score, self.signal_price = symbol, score, price
        self.scanner_name = kw.get("scanner_name", "accumulation")
        self.scanner_version = kw.get("scanner_version", "accumulation_v1.0")
        self.signal_id = kw.get("signal_id", f"sig-{symbol}")
        self.scanner_run_id = kw.get("run_id")
        self.volume = kw.get("volume", 3_000_000)
        self.avg_volume = kw.get("avg_volume", 1_000_000)
        self.volume_multiple = kw.get("volume_multiple", 3.0)
        self.price_change_pct = kw.get("price_change_pct", 1.2)
        self.hma200 = kw.get("hma200", 95.0)
        self.hma200_slope = kw.get("hma200_slope", 0.4)
        self.hma89 = kw.get("hma89", 98.0)
        self.vwap = kw.get("vwap", 99.5)
        self.market_data_provider = "alpaca"
        self.market_data_feed = "iex"
        self.data_timestamp = "2026-08-19T20:00:00+00:00"
        self.feature_timestamp = "2026-08-19T20:00:01+00:00"
        self.source_timeframe = "1d"
        self.timestamp = "2026-08-19T20:00:02+00:00"
        self.reasons = kw.get("reasons", ["volume 3.0x", "price > HMA200"])
        self.metrics = kw.get("metrics", {"config_fingerprint": "abc123"})


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(pub.CANDIDATE_DIR_ENV, str(tmp_path / "cand"))
    return tmp_path / "cand"


class TestItCannotReachAnOrder:
    def test_it_imports_no_broker_and_no_account(self):
        banned = {"kis_broker", "brokers", "execution", "order_gate",
                  "kis_live_trading", "execution_engine", "kis_position_manager",
                  "s1_live", "position_store", "exit_runtime"}
        for node in ast.walk(ast.parse(MODULE.read_text())):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, f"imports {name}"

    def test_it_does_not_reopen_candidate_decision(self):
        """`candidate_decision.publish()` always raises, on purpose. This
        module writes an observation instead -- publishing a candidate is
        not selecting one -- and must not route around that gate."""
        # Naming the gate in prose is fine and useful -- what must not
        # exist is a code path THROUGH it. Checked against the import
        # graph and the calls, not against the docstring that explains
        # why the gate is there.
        tree = ast.parse(MODULE.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                assert not any("candidate_decision" in n for n in names)
            if isinstance(node, ast.Attribute):
                assert node.attr != "select_candidates"

    def test_candidate_decision_is_still_disabled(self):
        policy = json.loads(
            (REPO_ROOT / "scanners" / "candidate_decision.json").read_text())
        assert policy["params"]["enabled"] is False

    def test_every_row_states_that_publication_is_not_selection(self, store):
        rows = pub.publish([Signal("ABC", 88.0)], strategy_id="S2",
                           trading_day="2026-08-19", session="REGULAR")
        assert rows[0].provenance["candidate_decision"] == "DISABLED"
        assert rows[0].provenance["published_by"] == "scanner_runtime"


class TestTheRecordIsReproducible:
    def test_it_carries_every_field_the_handoff_needs(self, store):
        row = pub.publish([Signal("ABC", 88.0)], strategy_id="S2_VOL",
                          trading_day="2026-08-19", session="REGULAR",
                          run_id="run-1")[0].as_dict()
        for field in ("strategy_id", "scanner_run_id", "trading_day", "session",
                      "generated_at", "symbol", "rank", "score", "price",
                      "volume", "avg_volume", "volume_multiple",
                      "price_change_pct", "hma200", "hma200_slope",
                      "provenance"):
            assert field in row, field
        assert row["strategy_id"] == "S2_VOL"
        assert row["session"] == "REGULAR"
        assert row["scanner_run_id"] == "run-1"

    def test_provenance_identifies_the_exact_scanner_that_judged(self, store):
        prov = pub.publish([Signal("ABC", 88.0)], strategy_id="S2",
                           trading_day="d", session="REGULAR")[0].provenance
        assert prov["scanner_version"] == "accumulation_v1.0"
        assert prov["config_fingerprint"] == "abc123"
        assert prov["signal_id"] == "sig-ABC"
        assert prov["market_data_feed"] == "iex"
        assert prov["reasons"]

    def test_ranks_match_the_order_the_monitor_prints(self, store):
        """Two different "rank 1"s for one run would make the channel and
        the file disagree about the same scan."""
        rows = pub.publish(
            [Signal("CCC", 50.0), Signal("AAA", 90.0), Signal("BBB", 90.0)],
            strategy_id="S2", trading_day="d", session="REGULAR")
        assert [(r.rank, r.symbol) for r in rows] == [
            (1, "AAA"), (2, "BBB"), (3, "CCC")], "score desc, symbol tie-break"

    def test_rank_is_a_position_not_a_recommendation(self, store):
        """Rank 1 of a weak day is still a weak candidate."""
        rows = pub.publish([Signal("WEAK", 3.0)], strategy_id="S2",
                           trading_day="d", session="REGULAR")
        assert rows[0].rank == 1 and rows[0].score == 3.0


class TestSessionsAreKeptApart:
    def test_each_session_gets_its_own_file(self, store):
        for session in ("PREMARKET", "REGULAR"):
            pub.publish([Signal("ABC", 80.0)], strategy_id="S2",
                        trading_day="2026-08-19", session=session)
        assert pub.read("2026-08-19", "PREMARKET")
        assert pub.read("2026-08-19", "REGULAR")
        assert (store / "2026-08-19-PREMARKET.jsonl").exists()
        assert (store / "2026-08-19-REGULAR.jsonl").exists()

    def test_one_sessions_result_does_not_appear_in_another(self, store):
        pub.publish([Signal("MORNING", 80.0)], strategy_id="S2",
                    trading_day="d", session="PREMARKET")
        pub.publish([Signal("AFTERNOON", 80.0)], strategy_id="S2",
                    trading_day="d", session="REGULAR")
        assert [r["symbol"] for r in pub.read("d", "PREMARKET")] == ["MORNING"]
        assert [r["symbol"] for r in pub.read("d", "REGULAR")] == ["AFTERNOON"]

    def test_a_missing_file_reads_as_empty_not_an_error(self, store):
        assert pub.read("1999-01-01", "REGULAR") == []


class TestItCannotFailAScan:
    def test_an_unwritable_directory_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(pub, "candidates_path",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        rows = pub.publish([Signal("ABC", 80.0)], strategy_id="S2",
                           trading_day="d", session="REGULAR")
        assert rows, "the rows are still returned; only the write was lost"

    def test_an_unparseable_row_is_skipped_not_fatal(self, store):
        pub.publish([Signal("GOOD", 80.0)], strategy_id="S2",
                    trading_day="d", session="REGULAR")
        path = pub.candidates_path("d", "REGULAR")
        path.write_text(path.read_text() + "{not json\n", encoding="utf-8")
        assert [r["symbol"] for r in pub.read("d", "REGULAR")] == ["GOOD"]

    def test_nothing_is_written_for_an_empty_scan(self, store):
        assert pub.publish([], strategy_id="S2", trading_day="d",
                           session="REGULAR") == []
        assert not pub.candidates_path("d", "REGULAR").exists()


class TestNumbersAreRealOrAbsent:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nan_and_inf_become_none(self, store, bad):
        """A NaN in a JSON file is not valid JSON, and a NaN in a
        comparison answers False to every question -- including the ones a
        risk check asks."""
        rows = pub.publish([Signal("ABC", 80.0, volume_multiple=bad)],
                           strategy_id="S2", trading_day="d", session="REGULAR")
        assert rows[0].volume_multiple is None
        json.loads(json.dumps(rows[0].as_dict()))  # round-trips

    def test_a_missing_metric_is_none_not_zero(self, store):
        """Zero is a measurement. Absent is not, and reporting one as the
        other would put a fabricated value in front of a consumer."""
        signal = Signal("ABC", 80.0)
        signal.vwap = None
        rows = pub.publish([signal], strategy_id="S2", trading_day="d",
                           session="REGULAR")
        assert rows[0].vwap is None


class TestOnlyStrategiesWithAConsumerPublish:
    def test_the_strategies_with_a_consumer_publish(self):
        """S6 joined when it took over the fast-turnover slot. S2 still
        publishes although it stood down to DISCOVERY_ONLY: the rows are
        the month-1 dataset, and the live-mode table -- not the
        publisher -- is what stops them being traded."""
        from scanners import runner

        assert set(runner.PUBLISHING_SCANNERS) == {
            "hma_early_trend", "accumulation", "orb"}

    def test_scanners_with_no_consumer_do_not_publish(self):
        """A hand-off file whose only reader is a future
        misunderstanding is worse than no file."""
        from scanners import runner

        for name in ("breakout_ready", "premarket_momentum", "gap_pullback"):
            assert name not in runner.PUBLISHING_SCANNERS

    def test_a_failed_scanner_publishes_nothing(self, store):
        """A failed scanner's signal list is a partial answer, and a
        partial answer in the hand-off file is indistinguishable from a
        complete one once the run is over."""
        from scanners import runner

        class Outcome:
            scanner_name, failed = "accumulation", True
            signals = [Signal("ABC", 80.0)]

        class Report:
            outcomes = [Outcome()]
            trading_day, session, run_id = "d", "REGULAR", "r"

        assert runner.publish_report_candidates(Report()) == 0
        assert pub.read("d", "REGULAR") == []

    def test_a_successful_scanner_publishes(self, store):
        from scanners import runner

        class Outcome:
            scanner_name, failed = "accumulation", False
            signals = [Signal("ABC", 80.0)]

        class Report:
            outcomes = [Outcome()]
            trading_day, session, run_id = "d", "REGULAR", "r"

        assert runner.publish_report_candidates(Report()) == 1
        rows = pub.read("d", "REGULAR")
        assert rows[0]["strategy_id"] == "S2_VOLUME_ACCUMULATION_V1"
