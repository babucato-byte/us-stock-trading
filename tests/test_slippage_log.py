"""What an order actually cost, against what it was supposed to cost.

The reason this log had to be built carefully rather than quickly: the
existing `order_state_events` LOOK like a latency source. On the real OWL
order they read

    CREATED       15:17:16.246
    VALIDATING    15:13:06.630
    APPROVED      15:13:06.630
    SUBMITTING    15:13:06.630
    ACCEPTED      15:13:06.630
    FILLED        17:49:59.128

-- four transitions sharing the cycle's start timestamp, a CREATED
stamped after the step that follows it, and a fill time that is really
when reconciliation noticed. Every latency computed from that is
fiction, and fiction in an execution-quality log is what a future
argument for IOC or ASK-laddering would be built on.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import slippage_log  # noqa: E402

NOW = datetime(2026, 8, 28, 15, 13, 6, tzinfo=timezone.utc)
S6 = "S6_ORB_BREAKOUT_V1"


@pytest.fixture
def env(tmp_path):
    return {"SLIPPAGE_LOG_DIR": str(tmp_path)}


def _record(**overrides):
    fields = dict(symbol="OWL", side="buy", session="REGULAR", strategy_id=S6)
    fields.update(overrides)
    return slippage_log.build_record(**fields)


class TestSlippageMeansTheSameThingOnBothSides:
    """A buy filling above signal is adverse; a sell filling above is
    favourable. One signed number with an implicit convention is how the
    sign gets read backwards at the worst moment."""

    def test_a_buy_filling_above_its_signal_is_adverse(self):
        bps, adverse = slippage_log.slippage_bps(
            signal_price=100.0, fill_price=100.5, side="buy")
        assert bps == pytest.approx(50.0)
        assert adverse is True

    def test_a_sell_filling_above_its_signal_is_favourable(self):
        bps, adverse = slippage_log.slippage_bps(
            signal_price=100.0, fill_price=100.5, side="sell")
        assert bps == pytest.approx(50.0)  # same raw direction
        assert adverse is False            # opposite interpretation

    def test_a_buy_filling_below_its_signal_is_favourable(self):
        bps, adverse = slippage_log.slippage_bps(
            signal_price=100.0, fill_price=99.5, side="buy")
        assert bps == pytest.approx(-50.0)
        assert adverse is False

    def test_an_unknown_side_gets_a_number_but_no_verdict(self):
        bps, adverse = slippage_log.slippage_bps(
            signal_price=100.0, fill_price=100.5, side=None)
        assert bps == pytest.approx(50.0)
        assert adverse is slippage_log.UNKNOWN


class TestAMissingMeasurementIsNotAZero:
    def test_no_signal_price_gives_no_slippage(self):
        """The OWL/RIG/SBS case. The fill price is real, the signal price
        was never recorded, and the honest answer is that we do not know."""
        record = _record(fill_price=12.1381)
        assert record["slippage_bps"] is slippage_log.UNKNOWN
        assert record["slippage_adverse"] is slippage_log.UNKNOWN

    def test_no_fill_price_gives_no_slippage(self):
        assert _record(signal_price=12.0)["slippage_bps"] is slippage_log.UNKNOWN

    def test_a_zero_signal_price_is_not_divided_by(self):
        record = _record(signal_price=0.0, fill_price=12.0)
        assert record["slippage_bps"] is slippage_log.UNKNOWN

    def test_a_nonsense_price_does_not_raise(self):
        assert _record(signal_price="n/a", fill_price=12.0)["slippage_bps"] is None


class TestLatenciesTheEvidenceCannotSupport:
    def test_missing_stamps_give_unknown_not_zero(self):
        """Zero would read as an instantaneous submission."""
        record = _record(fill_price=12.0)
        assert record["signal_to_gate_ms"] is slippage_log.UNKNOWN
        assert record["gate_to_submit_ms"] is slippage_log.UNKNOWN
        assert record["submit_to_fill_ms"] is slippage_log.UNKNOWN

    def test_a_negative_interval_is_unknown_not_negative(self):
        """The real inversion: CREATED stamped 15:17:16, the VALIDATING
        that follows it stamped 15:13:06. A negative latency is evidence
        the stamps are unreliable, not evidence of a fast step."""
        record = _record(fill_price=12.0, signal_at=NOW + timedelta(minutes=4),
                         gate_at=NOW)
        assert record["signal_to_gate_ms"] is slippage_log.UNKNOWN

    def test_a_real_interval_is_measured(self):
        record = _record(fill_price=12.0, gate_at=NOW,
                         submit_at=NOW + timedelta(milliseconds=250))
        assert record["gate_to_submit_ms"] == pytest.approx(250.0)

    def test_the_moment_a_fill_was_noticed_is_kept_apart_from_the_fill(self):
        """OWL's FILLED transition is stamped 17:49:59 -- when the
        reconciliation pass ran, two hours after the fill. Recording that
        as `fill_at` would report a two-hour execution latency."""
        record = _record(fill_price=12.0, submit_at=NOW,
                         fill_detected_at=NOW + timedelta(hours=2))
        assert record["submit_to_fill_ms"] is slippage_log.UNKNOWN
        assert record["fill_detected_at"] is not None
        assert record["fill_at"] is None


class TestTheLogNeverCostsATrade:
    def test_an_unwritable_directory_is_swallowed(self, tmp_path):
        blocked = tmp_path / "wall"
        blocked.write_text("not a directory")
        ok = slippage_log.append(_record(), trading_day="2026-08-28",
                                 env={"SLIPPAGE_LOG_DIR": str(blocked)})
        assert ok is False

    def test_reading_a_day_that_was_never_written_is_empty(self, env):
        assert slippage_log.read("2026-08-28", env=env) == []

    def test_a_corrupt_line_does_not_lose_the_good_ones(self, env):
        slippage_log.append(_record(fill_price=12.0), trading_day="2026-08-28",
                            env=env)
        path = slippage_log.log_path("2026-08-28", env=env)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        slippage_log.append(_record(symbol="SBS", fill_price=4.0),
                            trading_day="2026-08-28", env=env)
        assert [r["symbol"] for r in slippage_log.read("2026-08-28", env=env)] \
            == ["OWL", "SBS"]

    def test_it_holds_no_execution_or_broker_imports(self):
        """§2: no execution influence, no order-DB contention, no broker
        calls. Enforced against the source, not merely intended."""
        source = (REPO_ROOT / "s6_live" / "slippage_log.py").read_text()
        for forbidden in ("import kis_broker", "from brokers", "execution_engine",
                          "order_gate", "state_store", "submit_buy", "submit_sell"):
            assert forbidden not in source, forbidden


class TestSummariesDoNotAverageInTheGaps:
    def test_unmeasurable_orders_are_counted_but_not_averaged(self, env):
        """Folding a missing measurement in as zero reports perfect
        execution for every order nobody could measure."""
        slippage_log.append(_record(fill_price=12.0), trading_day="D", env=env)
        slippage_log.append(_record(signal_price=100.0, fill_price=100.5),
                            trading_day="D", env=env)
        out = slippage_log.summarise("D", env=env)
        assert out["orders"] == 2
        assert out["measured"] == 1
        assert out["unmeasurable"] == 1
        assert out["median_bps"] == pytest.approx(50.0)

    def test_a_day_with_nothing_measurable_reports_no_median(self, env):
        slippage_log.append(_record(fill_price=12.0), trading_day="D", env=env)
        out = slippage_log.summarise("D", env=env)
        assert out["measured"] == 0
        assert out["median_bps"] is slippage_log.UNKNOWN
        assert out["worst_adverse_bps"] is slippage_log.UNKNOWN

    def test_the_worst_adverse_fill_is_the_one_reported(self, env):
        for fill in (100.1, 100.9, 99.0):
            slippage_log.append(
                _record(signal_price=100.0, fill_price=fill),
                trading_day="D", env=env)
        assert slippage_log.summarise("D", env=env)["worst_adverse_bps"] \
            == pytest.approx(90.0)


class TestTheRecordSaysWhereItCameFrom:
    def test_a_live_record_is_marked_live(self):
        assert _record(fill_price=12.0)["evidence"] == "LIVE"

    def test_a_reconstructed_record_says_so(self):
        """So a reader can tell a measured row from a rebuilt one without
        having to know which trades predate the log."""
        record = _record(fill_price=12.0, evidence="BACKFILL_FROM_POSITION_STORE")
        assert record["evidence"] == "BACKFILL_FROM_POSITION_STORE"

    def test_it_round_trips_through_the_file(self, env):
        slippage_log.append(_record(fill_price=12.1381, qty_filled=1),
                            trading_day="2026-08-28", env=env)
        rows = slippage_log.read("2026-08-28", env=env)
        assert rows[0]["symbol"] == "OWL"
        assert rows[0]["fill_price"] == pytest.approx(12.1381)
        assert json.dumps(rows[0])  # stays serialisable


class TestTheLiveStampIsTakenAtTheMoment:
    """The defect this log exists to avoid, guarded at its source.

    `current` is the cycle's start and every symbol in the loop shares
    it. Reusing it as the submit time is precisely how `order_state_events`
    ended up with four transitions carrying one timestamp -- and a
    latency log built on it would report every submission as instant.
    """

    def test_the_submit_stamp_is_not_the_cycle_start(self):
        import inspect

        import kis_live_trading

        source = inspect.getsource(kis_live_trading.run_live_buy_entry_cycle)
        assert "submit_at = datetime.now(timezone.utc)" in source
        assert "submit_at=submit_at" in source
        assert "submit_at=current" not in source

    def test_it_records_after_the_broker_answers(self):
        import inspect

        import kis_live_trading

        source = inspect.getsource(kis_live_trading.run_live_buy_entry_cycle)
        submit = source.index("submit_at = datetime.now")
        record = source.index("_record_slippage(")
        assert submit < record

    def test_the_recorder_swallows_everything(self):
        """A broken observation must not cost a trade."""
        import kis_live_trading

        kis_live_trading._record_slippage(
            now=NOW, symbol=None, side=object(), session=None,
            strategy_id=S6, submit_at="not a time")

    def test_no_fill_price_is_claimed_at_acceptance(self):
        """The broker has ACCEPTED, not filled. Recording the limit price
        as the fill would report zero slippage on every single order."""
        import inspect

        import kis_live_trading

        call = inspect.getsource(kis_live_trading.run_live_buy_entry_cycle)
        start = call.index("_record_slippage(")
        assert "fill_price=" not in call[start:start + 900]


class TestTheTwoHalvesMeetLater:
    """Slippage needs a signal price and a fill price, and they are known
    at different times -- the first when the order is accepted, the
    second when the broker reports the fill, which for OWL was two hours
    later through reconciliation. Neither half alone is a measurement."""

    def test_an_accepted_order_alone_has_no_slippage(self, env):
        slippage_log.append(
            _record(signal_price=12.0, internal_order_id="kislive-OWL-1"),
            trading_day="D", env=env)
        merged = slippage_log.read_merged("D", env=env)
        assert merged[0]["slippage_bps"] is slippage_log.UNKNOWN

    def test_the_fill_completes_it(self, env):
        slippage_log.append(
            _record(signal_price=12.0, submit_at=NOW,
                    internal_order_id="kislive-OWL-1"),
            trading_day="D", env=env)
        slippage_log.attach_fill(
            internal_order_id="kislive-OWL-1", fill_price=12.06, qty_filled=1,
            fill_at=NOW + timedelta(seconds=3), trading_day="D", env=env)
        merged = slippage_log.read_merged("D", env=env)
        assert len(merged) == 1
        assert merged[0]["fill_price"] == pytest.approx(12.06)
        assert merged[0]["slippage_bps"] == pytest.approx(50.0)
        assert merged[0]["slippage_adverse"] is True
        assert merged[0]["submit_to_fill_ms"] == pytest.approx(3000.0)

    def test_the_original_line_is_never_rewritten(self, env):
        """Append-only. The entry path writes to this file too, and a
        read-modify-write against it would race with a live order."""
        slippage_log.append(_record(signal_price=12.0,
                                    internal_order_id="kislive-OWL-1"),
                            trading_day="D", env=env)
        slippage_log.attach_fill(internal_order_id="kislive-OWL-1",
                                 fill_price=12.06, trading_day="D", env=env)
        raw = slippage_log.read("D", env=env)
        assert len(raw) == 2
        assert raw[0]["fill_price"] is None
        assert raw[1]["evidence"] == slippage_log.EVIDENCE_FILL

    def test_a_fill_whose_acceptance_is_on_the_previous_day_is_kept(self, env):
        """An order accepted before midnight UTC and filled after it. It
        is still evidence and must not be silently dropped."""
        slippage_log.attach_fill(internal_order_id="kislive-OWL-1",
                                 fill_price=12.06, trading_day="D", env=env)
        merged = slippage_log.read_merged("D", env=env)
        assert len(merged) == 1
        assert merged[0]["evidence"] == slippage_log.EVIDENCE_FILL

    def test_a_fill_for_no_order_is_refused(self, env):
        assert slippage_log.attach_fill(internal_order_id=None, fill_price=1.0,
                                        trading_day="D", env=env) is False

    def test_summaries_use_the_completed_rows(self, env):
        slippage_log.append(_record(signal_price=100.0,
                                    internal_order_id="kislive-OWL-1"),
                            trading_day="D", env=env)
        assert slippage_log.summarise("D", env=env)["measured"] == 0
        slippage_log.attach_fill(internal_order_id="kislive-OWL-1",
                                 fill_price=100.5, trading_day="D", env=env)
        out = slippage_log.summarise("D", env=env)
        assert out["orders"] == 1 and out["measured"] == 1
        assert out["median_bps"] == pytest.approx(50.0)


class TestTheFillPathHandsOverTheSecondHalf:
    def test_opening_a_position_records_its_fill(self, tmp_path, monkeypatch):
        import tempfile

        monkeypatch.setenv("SLIPPAGE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
        from s6_live import position_store as s6ps
        from state_store.db import open_db

        with open_db() as conn:
            pid = s6ps.record_submission(conn, symbol="OWL", variant="S6-R",
                                         entry_session="REGULAR",
                                         client_order_id="kislive-OWL-1",
                                         now=NOW)
            s6ps.open_from_fill(conn, pid, quantity=1,
                                average_fill_price=12.1381, venue="NYSE",
                                now=NOW)
        rows = slippage_log.read(NOW.strftime("%Y-%m-%d"),
                                 env={"SLIPPAGE_LOG_DIR": str(tmp_path)})
        assert [r["evidence"] for r in rows] == [slippage_log.EVIDENCE_FILL]
        assert rows[0]["fill_price"] == pytest.approx(12.1381)
        assert rows[0]["internal_order_id"] == "kislive-OWL-1"

    def test_a_broken_slippage_log_does_not_stop_a_position_opening(
            self, tmp_path, monkeypatch):
        """The position is real. Losing the measurement of it is not a
        reason to fail the promotion to OPEN."""
        import tempfile

        wall = tmp_path / "wall"
        wall.write_text("not a directory")
        monkeypatch.setenv("SLIPPAGE_LOG_DIR", str(wall))
        monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
        from s6_live import position_store as s6ps
        from state_store.db import open_db

        with open_db() as conn:
            pid = s6ps.record_submission(conn, symbol="OWL", variant="S6-R",
                                         entry_session="REGULAR",
                                         client_order_id="kislive-OWL-1",
                                         now=NOW)
            assert s6ps.open_from_fill(conn, pid, quantity=1,
                                       average_fill_price=12.1381,
                                       venue="NYSE", now=NOW) is True
            assert s6ps.load(conn, pid)["status"] == "OPEN"


class TestTheTwoLegsOfATradeStayApart:
    """A position's entry and exit can carry the same client order id --
    the backfill produced exactly that -- so matching on the id alone
    lets the sell overwrite the buy and half the trades vanish."""

    def test_both_legs_survive_a_shared_order_id(self, env):
        for side, price in (("buy", 12.13), ("sell", 12.11)):
            slippage_log.append(
                _record(side=side, fill_price=price,
                        internal_order_id="kislive-OWL-1"),
                trading_day="D", env=env)
        merged = slippage_log.read_merged("D", env=env)
        assert sorted(r["side"] for r in merged) == ["buy", "sell"]

    def test_a_fill_lands_on_the_leg_it_belongs_to(self, env):
        for side in ("buy", "sell"):
            slippage_log.append(
                _record(side=side, signal_price=12.0,
                        internal_order_id="kislive-OWL-1"),
                trading_day="D", env=env)
        slippage_log.attach_fill(internal_order_id="kislive-OWL-1",
                                 fill_price=12.06, side="buy",
                                 trading_day="D", env=env)
        merged = {r["side"]: r for r in slippage_log.read_merged("D", env=env)}
        assert merged["buy"]["fill_price"] == pytest.approx(12.06)
        assert merged["sell"]["fill_price"] is None

    def test_the_backfill_does_not_reuse_the_entry_id_for_the_exit(self):
        """`client_order_id` names the entry order. The exit's own id was
        never recorded, and borrowing the entry's would assert something
        untrue about which order produced the sale."""
        source = (REPO_ROOT / "scripts" / "backfill_slippage.py").read_text()
        sell = source[source.index('side="sell"'):]
        assert "internal_order_id" not in sell[:400]
