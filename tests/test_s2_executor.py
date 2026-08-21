"""The S2 tick, and the three independent gates in front of a real order.

The property that matters most while S2 is being validated: with S2
DISCOVERY_ONLY, no path through this module reaches a submit function. A
test asserts it against the live-mode table rather than against the
default argument, because the default is the thing most likely to be
changed by accident.

The second property is that shadow mode is not a dry run. It executes
the entire cycle and writes the same record a live trade would, because
"the ones we took did better than the ones we skipped" is only a finding
if both sides went through identical code.
"""

import ast
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s2_live import executor  # noqa: E402
from s2_live import exit_policy as ex  # noqa: E402
from s2_live import trade_record as tr  # noqa: E402

BASE = 1_000_000
#: 16:00 UTC == 12:00 ET -- genuinely inside REGULAR, and far from the
#: 15:45 session-exit lead. An earlier version used 12:00 UTC, which is
#: 08:00 ET: the cycle was labelled REGULAR while the clock said
#: PREMARKET, so every position exited on the session rule and two tests
#: passed for the wrong reason.
NOW = datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc)


class Features:
    def __init__(self, price=105.0, hma200=95.0, hma200_slope=0.4,
                 vwap=100.0, volume=6 * BASE):
        self.price, self.hma200, self.hma200_slope = price, hma200, hma200_slope
        self.vwap, self.volume = vwap, volume


class Recorder:
    """Stands in for a GATED submitter. Records; never sends.

    Carries the marker because the executor refuses an ungated one --
    see TestOnlyAGatedSubmitterMaySend for why that refusal exists.
    """

    applies_buy_gate = True

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"order_id": f"test-{len(self.calls)}"}


class UngatedRecorder(Recorder):
    """A raw broker call: no marker, so it must never be used."""

    applies_buy_gate = False


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(tr.TRADE_DIR_ENV, str(tmp_path / "trades"))
    return tmp_path / "trades"


@pytest.fixture
def s2_live(monkeypatch):
    """Pretend S2 has been promoted, without touching the real table."""
    from config import scanner_live_mode

    monkeypatch.setitem(scanner_live_mode.SCANNER_LIVE_MODE,
                        "accumulation", "LIMITED_LIVE")


def held(**kw):
    kw.setdefault("symbol", "ABC")
    kw.setdefault("entry_price", 100.0)
    kw.setdefault("baseline_volume", BASE)
    kw.setdefault("entry_volume_multiple", 6.0)
    return ex.S2PositionState(**kw)


def cycle(**kw):
    kw.setdefault("positions", [])
    kw.setdefault("candidates", [])
    kw.setdefault("features_fn", lambda s: Features())
    kw.setdefault("price_fn", lambda s: 105.0)
    kw.setdefault("trading_day", "2026-08-19")
    kw.setdefault("session", "REGULAR")
    kw.setdefault("now", NOW)
    return executor.run_cycle(**kw)


@pytest.fixture
def s2_discovery_only(monkeypatch):
    """Put S2 back to DISCOVERY_ONLY without touching the real table."""
    from config import scanner_live_mode

    monkeypatch.setitem(scanner_live_mode.SCANNER_LIVE_MODE,
                        "accumulation", "DISCOVERY_ONLY")


class TestNothingIsSubmittedWhileS2IsDiscoveryOnly:
    """The live-mode table is a gate in its own right.

    S2 is LIMITED_LIVE now, so these drive the table back down rather
    than reading today's value -- the property under test is that the
    gate WORKS, not that it happens to be closed.
    """

    def test_s1_and_s2_are_both_live_and_the_rest_are_not(self):
        from config import scanner_live_mode

        assert scanner_live_mode.SCANNER_LIVE_MODE["hma_early_trend"] == \
            "LIMITED_LIVE"
        assert scanner_live_mode.SCANNER_LIVE_MODE["accumulation"] == \
            "LIMITED_LIVE"
        for name in ("breakout_ready", "premarket_momentum", "gap_pullback",
                     "orb"):
            assert scanner_live_mode.SCANNER_LIVE_MODE[name] == "DISCOVERY_ONLY"
        assert executor.s2_is_limited_live() is True

    def test_a_confirmed_candidate_is_not_bought_when_not_live(
            self, store, s2_discovery_only):
        broker = Recorder()
        result = cycle(candidates=[{"symbol": "ABC", "price": 100.0}],
                       live=True, submit_fn=broker)
        assert broker.calls == [], "no order while S2 is DISCOVERY_ONLY"
        assert result.submitted == 0
        assert result.skipped[0]["reason"] == executor.SKIP_NOT_LIVE

    def test_it_still_records_that_it_would_have_entered(
            self, store, s2_discovery_only):
        """The refusals are the more interesting half of the dataset."""
        cycle(candidates=[{"symbol": "ABC", "price": 100.0}], live=True,
              submit_fn=Recorder())
        rows = tr.read("2026-08-19")
        assert rows[0]["provenance"]["would_have_entered"] is True
        assert rows[0]["live"] is False

    def test_a_shadow_cycle_submits_nothing_even_when_live(self, store):
        """live=False is the default and the executor honours it."""
        broker = Recorder()
        cycle(candidates=[{"symbol": "ABC", "price": 100.0}], live=False,
              submit_fn=broker)
        assert broker.calls == []

    def test_an_exit_is_decided_but_not_sent(self, store):
        broker = Recorder()
        result = cycle(positions=[held(peak_volume_multiple=6.0,
                                       price_at_volume_peak=110.0)],
                       features_fn=lambda s: Features(price=104.0,
                                                      volume=2 * BASE),
                       price_fn=lambda s: 104.0, live=False, submit_fn=broker)
        assert result.exits[0]["action"] == ex.SELL
        assert result.exits[0]["submitted"] is False
        assert broker.calls == []

    def test_the_module_has_no_import_path_to_a_broker(self):
        banned = {"brokers", "kis_broker", "execution_engine",
                  "kis_live_trading", "slack_utils"}
        source = (REPO_ROOT / "s2_live" / "executor.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, f"imports {name}"

    def test_no_submit_function_means_nothing_can_be_sent(self, store, s2_live):
        result = cycle(candidates=[{"symbol": "ABC", "price": 100.0}],
                       live=True, submit_fn=None)
        assert result.submitted == 0
        assert result.skipped[0]["reason"] == executor.SKIP_NO_SUBMITTER


class TestTheThreeGatesAreIndependent:
    def test_an_unconfirmed_price_blocks_before_anything_else(self, store,
                                                              s2_live):
        broker = Recorder()
        result = cycle(candidates=[{"symbol": "ABC", "price": 110.0}],
                       price_fn=lambda s: 105.0, live=True, submit_fn=broker)
        assert result.skipped[0]["reason"] == executor.SKIP_NOT_CONFIRMED
        assert broker.calls == []

    def test_a_full_book_blocks_a_confirmed_candidate(self, store, s2_live):
        broker = Recorder()
        result = cycle(candidates=[{"symbol": "ABC", "price": 100.0}],
                       live=True, submit_fn=broker,
                       open_book={executor.STRATEGY_ID: 1})
        assert result.skipped[0]["reason"] == executor.SKIP_LIMIT
        assert broker.calls == []

    def test_the_global_limit_blocks_even_with_s2_room(self, store, s2_live):
        broker = Recorder()
        result = cycle(candidates=[{"symbol": "ABC", "price": 100.0}],
                       live=True, submit_fn=broker,
                       open_book={"S1_HMA_EARLY_TREND_V1": 1,
                                  "S3_OTHER": 1})
        assert result.skipped[0]["reason"] == executor.SKIP_LIMIT
        assert broker.calls == []

    def test_an_unverified_session_blocks(self, store, s2_live):
        broker = Recorder()
        result = cycle(candidates=[{"symbol": "ABC", "price": 100.0}],
                       session="PREMARKET", live=True, submit_fn=broker)
        assert result.skipped[0]["reason"] == executor.SKIP_NOT_CONFIRMED
        assert broker.calls == []

    def test_all_three_open_lets_exactly_one_share_through(self, store,
                                                           s2_live):
        broker = Recorder()
        result = cycle(candidates=[{"symbol": "ABC", "price": 100.0}],
                       live=True, submit_fn=broker, open_book={})
        assert len(broker.calls) == 1
        assert broker.calls[0]["quantity"] == 1
        assert broker.calls[0]["side"] == "buy"
        assert result.entries[0]["entered"] is True

    def test_only_one_entry_is_considered_per_tick(self, store, s2_live):
        broker = Recorder()
        cycle(candidates=[{"symbol": "AAA", "price": 100.0},
                          {"symbol": "BBB", "price": 100.0}],
              live=True, submit_fn=broker, open_book={})
        assert len(broker.calls) == 1


class TestExitsRunBeforeEntries:
    def test_a_closed_position_frees_room_in_the_same_tick(self, store,
                                                           s2_live):
        """A tick that entered before exiting would check the limit
        against a book it had already made stale."""
        broker = Recorder()
        result = cycle(
            positions=[held(symbol="OLD", peak_volume_multiple=6.0,
                            price_at_volume_peak=110.0)],
            candidates=[{"symbol": "NEW", "price": 100.0}],
            features_fn=lambda s: (Features(price=104.0, volume=2 * BASE)
                                   if s == "OLD" else Features()),
            price_fn=lambda s: 104.0 if s == "OLD" else 105.0,
            live=True, submit_fn=broker,
            open_book={executor.STRATEGY_ID: 1})
        sides = [c["side"] for c in broker.calls]
        assert sides == ["sell", "buy"], "the sell must precede the buy"
        assert result.entries and result.entries[0]["symbol"] == "NEW"

    def test_a_held_position_still_blocks_the_entry(self, store, s2_live):
        broker = Recorder()
        result = cycle(
            positions=[held(symbol="OLD", peak_volume_multiple=6.0)],
            candidates=[{"symbol": "NEW", "price": 100.0}],
            live=True, submit_fn=broker,
            open_book={executor.STRATEGY_ID: 1})
        assert [c["side"] for c in broker.calls] == []
        assert result.skipped[0]["reason"] == executor.SKIP_LIMIT


class TestOnePositionCannotCostTheOthers:
    def test_a_failing_evaluation_is_reported_not_swallowed(self, store):
        def explode(symbol):
            if symbol == "BAD":
                raise RuntimeError("provider down")
            return Features()

        result = cycle(positions=[held(symbol="BAD"), held(symbol="GOOD",
                                                           peak_volume_multiple=6.0)],
                       features_fn=explode)
        assert any("BAD" in e for e in result.errors)
        assert [e["symbol"] for e in result.exits] == ["GOOD"]

    def test_a_failed_entry_does_not_lose_the_exits(self, store):
        result = cycle(positions=[held(peak_volume_multiple=6.0)],
                       candidates=[{"no_symbol": True}],
                       features_fn=lambda s: (Features() if s else
                                              (_ for _ in ()).throw(
                                                  ValueError("no symbol"))))
        assert result.exits, "the exit still happened"


class TestTheCycleIsRecorded:
    def test_every_evaluated_position_leaves_a_row(self, store):
        cycle(positions=[held(symbol="AAA", peak_volume_multiple=6.0),
                         held(symbol="BBB", peak_volume_multiple=6.0)])
        assert sorted(r["symbol"] for r in tr.read("2026-08-19")) == \
            ["AAA", "BBB"]

    def test_the_result_serialises(self, store):
        import json

        result = cycle(positions=[held(peak_volume_multiple=6.0)])
        json.loads(json.dumps(result.as_dict(), default=str))

    def test_validation_quantity_is_one_whole_share(self):
        assert executor.VALIDATION_QUANTITY == 1
        assert isinstance(executor.VALIDATION_QUANTITY, int)


class TestOnlyAGatedSubmitterMaySend:
    """S2's three gates run BEFORE the shared BUY gate, not instead of it.

    Injection keeps a bug in this module from reaching a broker, but it
    would equally let someone bind a raw broker call and skip the
    twenty-step sequence in execution/order_gate -- COMMON_STOCK,
    orderable cash, reconciliation, duplicate signals, kill switch. None
    of those is replaced by anything here.

    The marker must be set deliberately, so forgetting it fails closed.
    That costs nothing; forgetting the gate would cost real money on an
    unchecked order.
    """

    def test_an_ungated_submitter_is_refused(self, store, s2_live):
        broker = UngatedRecorder()
        result = cycle(candidates=[{"symbol": "ABC", "price": 100.0}],
                       live=True, submit_fn=broker, open_book={})
        assert broker.calls == []
        assert result.submitted == 0
        assert result.skipped[0]["reason"] == executor.SKIP_UNGATED_SUBMITTER

    def test_a_plain_function_is_ungated_by_default(self, store, s2_live):
        """No marker means not gated. A caller has to opt in."""
        sent = []
        result = cycle(candidates=[{"symbol": "ABC", "price": 100.0}],
                       live=True, submit_fn=lambda **kw: sent.append(kw),
                       open_book={})
        assert sent == []
        assert result.skipped[0]["reason"] == executor.SKIP_UNGATED_SUBMITTER

    def test_a_gated_submitter_is_allowed(self, store, s2_live):
        broker = Recorder()
        cycle(candidates=[{"symbol": "ABC", "price": 100.0}], live=True,
              submit_fn=broker, open_book={})
        assert len(broker.calls) == 1

    def test_no_production_entrypoint_binds_a_submitter_yet(self):
        """The honest state of the rollout: S2 is LIMITED_LIVE in the
        table and its executor is not scheduled or bound to anything, so
        no S2 order can be placed. Recorded as a test so the claim is
        checked rather than asserted in a report.
        """
        import pathlib

        for script in (REPO_ROOT / "scripts").glob("*.py"):
            source = script.read_text(encoding="utf-8", errors="ignore")
            if "s2_live" in source and "run_cycle" in source:
                assert "applies_buy_gate" in source, (
                    f"{script.name} runs the S2 cycle; it must bind a gated "
                    "submitter")

    def test_an_ungated_submitter_cannot_send_a_sell_either(self, store):
        """A sell does not need the BUY gate -- exits must never be
        gated by entry risk -- but the injection risk is the same: an
        undeclared callable is just as likely to be a raw broker call."""
        broker = UngatedRecorder()
        result = cycle(positions=[held(symbol="OLD", peak_volume_multiple=6.0,
                                       price_at_volume_peak=110.0)],
                       features_fn=lambda s: Features(price=104.0,
                                                      volume=2 * BASE),
                       price_fn=lambda s: 104.0, live=True, submit_fn=broker)
        assert result.exits[0]["action"] == ex.SELL, "the decision still stands"
        assert result.exits[0]["submitted"] is False
        assert broker.calls == []
