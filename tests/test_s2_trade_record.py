"""What an S2 position leaves behind.

The rule under test is the one that decides what belongs in the record:
a field earns its place by being impossible or misleading to recompute
later. The volume peak is gone the moment volume falls; a stop
recomputed next month uses next month's config, which is exactly the
thing §7 expects to change; and what happened after the exit is
unanswerable unless someone asked at the time.

Shadow trades are recorded the same way as live ones. "The ones we took
did better than the ones we skipped" is only a finding if both sides
were measured identically -- measuring only the live ones makes every
review a study of survivors.
"""

import ast
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_hours import EASTERN  # noqa: E402
from s2_live import exit_policy as ex  # noqa: E402
from s2_live import trade_record as tr  # noqa: E402

BASE = 1_000_000
T0 = datetime(2026, 8, 19, 10, 0, tzinfo=EASTERN)


class Features:
    def __init__(self, price=None, hma200=95.0, hma200_slope=0.4,
                 vwap=100.0, volume=6 * BASE):
        self.price, self.hma200, self.hma200_slope = price, hma200, hma200_slope
        self.vwap, self.volume = vwap, volume


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(tr.TRADE_DIR_ENV, str(tmp_path / "trades"))
    return tmp_path / "trades"


def state(**kw):
    kw.setdefault("symbol", "ABC")
    kw.setdefault("entry_price", 100.0)
    kw.setdefault("entry_volume_multiple", 6.0)
    kw.setdefault("baseline_volume", BASE)
    return ex.S2PositionState(**kw)


class TestEveryRequiredFieldIsPresent:
    @pytest.mark.parametrize("field", [
        "entry_volume_multiple", "peak_volume_multiple",
        "current_volume_multiple", "volume_decay_ratio",
        "price_at_volume_peak", "current_price", "vwap",
        "mfe_pct", "mae_pct", "exit_reason"])
    def test_the_volume_and_price_fields(self, field):
        record = tr.from_decision(
            state(peak_volume_multiple=8.0, price_at_volume_peak=112.0),
            ex.decide(state(peak_volume_multiple=8.0,
                            price_at_volume_peak=112.0),
                      features=Features(price=108.0), now=T0),
            trading_day="2026-08-19", session="REGULAR", now=T0)
        assert field in record.as_dict()

    @pytest.mark.parametrize("field", [
        "entry_price", "effective_stop", "structural_stop", "hard_stop",
        "time_to_stop_minutes", "time_to_peak_minutes",
        "post_stop_return_30m", "post_stop_return_1h"])
    def test_the_stop_and_timing_fields(self, field):
        record = tr.S2TradeRecord(symbol="ABC", trading_day="d")
        assert field in record.as_dict()

    def test_the_stops_are_captured_as_they_stood(self):
        """Recomputed later they would use a config that §7 expects to
        have changed."""
        decision = ex.decide(state(peak_volume_multiple=6.0),
                             features=Features(price=105.0), now=T0)
        record = tr.from_decision(state(), decision, trading_day="d",
                                  session="REGULAR", now=T0)
        assert record.hard_stop == pytest.approx(97.0)
        assert record.effective_stop == pytest.approx(97.0)
        assert record.max_loss_pct == 3.0

    def test_the_volume_peak_is_captured_before_it_is_lost(self):
        held = state(peak_volume_multiple=8.0, price_at_volume_peak=112.0)
        decision = ex.decide(held, features=Features(price=104.0,
                                                     volume=2 * BASE), now=T0)
        record = tr.from_decision(held, decision, trading_day="d",
                                  session="REGULAR", now=T0)
        assert record.peak_volume_multiple == 8.0
        assert record.price_at_volume_peak == 112.0
        assert record.volume_decay_ratio is not None


class TestExitReasonUsesTheAgreedVocabulary:
    def test_a_sell_records_its_reason(self):
        held = state(peak_volume_multiple=6.0, price_at_volume_peak=110.0)
        decision = ex.decide(held, features=Features(price=104.0,
                                                     volume=2 * BASE), now=T0)
        record = tr.from_decision(held, decision, trading_day="d",
                                  session="REGULAR", now=T0)
        assert record.exit_reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert record.exit_reason in ex.EXIT_REASONS

    def test_a_hold_records_no_exit(self):
        """An open position has not exited, and writing a reason for it
        would make an unfinished trade look closed."""
        held = state(peak_volume_multiple=6.0)
        decision = ex.decide(held, features=Features(price=108.0), now=T0)
        record = tr.from_decision(held, decision, trading_day="d",
                                  session="REGULAR", now=T0)
        assert record.exit_reason is None
        assert record.exit_time is None
        assert record.exit_price is None

    def test_time_to_stop_is_measured_from_entry(self):
        held = state(peak_volume_multiple=6.0, price_at_volume_peak=110.0)
        decision = ex.decide(held, features=Features(price=104.0,
                                                     volume=2 * BASE),
                             now=T0 + timedelta(minutes=45))
        record = tr.from_decision(held, decision, trading_day="d",
                                  session="REGULAR", entry_time=T0,
                                  now=T0 + timedelta(minutes=45))
        assert record.time_to_stop_minutes == pytest.approx(45.0)


class TestShadowTradesAreMeasuredTheSameWay:
    def test_a_candidate_never_bought_is_recorded(self, store):
        record = tr.S2TradeRecord(symbol="SKIPPED", trading_day="2026-08-19",
                                  session="REGULAR", live=False,
                                  entry_price=100.0)
        assert tr.append(record) is True
        rows = tr.read("2026-08-19")
        assert rows[0]["live"] is False
        assert rows[0]["symbol"] == "SKIPPED"

    def test_live_and_shadow_share_one_schema(self, store):
        for live in (True, False):
            tr.append(tr.S2TradeRecord(symbol="ABC", trading_day="d",
                                       live=live, entry_price=100.0))
        rows = tr.read("d")
        assert set(rows[0]) == set(rows[1]), "same fields either way"
        assert {r["live"] for r in rows} == {True, False}


class TestExcursions:
    def test_a_favourable_move_is_positive(self):
        assert tr.excursion_pct(100.0, 110.0) == pytest.approx(10.0)

    def test_an_adverse_move_is_negative(self):
        assert tr.excursion_pct(100.0, 97.0) == pytest.approx(-3.0)

    def test_it_does_not_clamp_for_the_caller(self):
        """MAE and post-exit returns need the sign; clamping belongs
        where MFE is computed, not in a shared helper."""
        assert tr.excursion_pct(100.0, 90.0) < 0

    @pytest.mark.parametrize("entry,extreme", [
        (None, 110.0), (100.0, None), (0.0, 110.0), (float("nan"), 110.0)])
    def test_unmeasurable_is_none_not_zero(self, entry, extreme):
        assert tr.excursion_pct(entry, extreme) is None


class TestItCannotDisturbTrading:
    def test_a_failed_write_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(tr, "trades_path",
                            lambda *a: (_ for _ in ()).throw(OSError("full")))
        assert tr.append(tr.S2TradeRecord(symbol="A", trading_day="d")) is False

    def test_an_unparseable_row_is_skipped(self, store):
        tr.append(tr.S2TradeRecord(symbol="GOOD", trading_day="d"))
        path = tr.trades_path("d")
        path.write_text(path.read_text() + "{not json\n", encoding="utf-8")
        assert [r["symbol"] for r in tr.read("d")] == ["GOOD"]

    def test_a_missing_file_reads_as_empty(self, store):
        assert tr.read("1999-01-01") == []

    def test_it_imports_no_policy_and_no_broker(self):
        """A measurement that feeds back into the thing it measures stops
        being a measurement."""
        banned = {"brokers", "kis_broker", "execution_engine", "order_gate",
                  "s2_exit_v0", "position_limits", "kis_live_trading"}
        source = (REPO_ROOT / "s2_live" / "trade_record.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, f"imports {name}"

    def test_the_record_round_trips_as_json(self, store):
        held = state(peak_volume_multiple=6.0, price_at_volume_peak=110.0)
        decision = ex.decide(held, features=Features(price=104.0,
                                                     volume=2 * BASE), now=T0)
        record = tr.from_decision(held, decision, trading_day="d",
                                  session="REGULAR", now=T0)
        json.loads(json.dumps(record.as_dict(), default=str))
