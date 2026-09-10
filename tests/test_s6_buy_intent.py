"""S6 READY -> durable BUY_INTENT -> decoupled execution worker.

Production evidence 2026-09-09/10: the shared qualify->KIS->submit
sequence (`kis_live_trading.run_live_buy_entry_cycle`) costs ~44-45s
PER READY CANDIDATE, and S6's fast-watch tick used to call it inline,
so even one READY candidate pushed the tick past the 60s cron interval
and OVERLAP_SKIPPED the next one. `s6_live/buy_intent.py` and
`s6_live/buy_intent_source.py` decouple "S6 decided READY" from "the
shared cycle actually runs" via a durable queue; `scripts/
run_live_buy_entry.py`'s strategy dispatch wires it up without
touching the shared cycle or S1's call site at all.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from s6_live import buy_intent
from s6_live.buy_intent_source import IntentQueueSource

DAY = "2026-09-10"
SESSION = "REGULAR"
NOW = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)


def _env(tmp_path):
    return {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}


def _row(symbol, **over):
    base = {
        "symbol": symbol, "price": 22.74, "range_high": 23.0,
        "strategy_id": "S6_ORB_BREAKOUT_V1",
        "provenance": {"signal_id": f"s6aw-{symbol.lower()}",
                       "signal_timestamp": NOW.isoformat()},
    }
    base.update(over)
    return base


class TestBuyIntentStore:
    """A: S6 READY -> durable BUY_INTENT. C: duplicate READY does not
    duplicate the intent."""

    def test_write_then_read_roundtrip(self, tmp_path):
        env = _env(tmp_path)
        written = buy_intent.write_ready(DAY, SESSION, [_row("AAPL"), _row("MSFT")],
                                         now=NOW, env=env)
        assert written == 2
        pending = buy_intent.read_ready(DAY, SESSION, env=env)
        assert sorted(pending) == ["AAPL", "MSFT"]
        assert pending["AAPL"]["candidate"]["symbol"] == "AAPL"

    def test_a_symbol_already_pending_is_not_duplicated(self, tmp_path):
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("AAPL", price=200.0)], now=NOW, env=env)
        later = NOW.replace(minute=1)
        buy_intent.write_ready(DAY, SESSION, [_row("AAPL", price=201.0)], now=later, env=env)
        pending = buy_intent.read_ready(DAY, SESSION, env=env)
        assert len(pending) == 1
        # first_ready_at is retained from the EARLIEST observation, so
        # READY -> intent latency is never measured from a re-refresh.
        assert pending["AAPL"]["first_ready_at"] == NOW.isoformat()
        # but the candidate data itself is refreshed to the latest read
        assert pending["AAPL"]["candidate"]["price"] == 201.0

    def test_claim_atomically_empties_the_store(self, tmp_path):
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("AAPL")], now=NOW, env=env)
        claimed = buy_intent.claim_ready(DAY, SESSION, env=env)
        assert sorted(claimed) == ["AAPL"]
        assert buy_intent.read_ready(DAY, SESSION, env=env) == {}

    def test_a_second_claim_gets_nothing_left_over(self, tmp_path):
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("AAPL")], now=NOW, env=env)
        buy_intent.claim_ready(DAY, SESSION, env=env)
        assert buy_intent.claim_ready(DAY, SESSION, env=env) == {}

    def test_a_write_after_a_claim_is_not_lost(self, tmp_path):
        """The producer (fast-watch) and consumer (execution worker)
        never race each other out of existence: a write landing after
        a claim starts a fresh queue, not a corrupted one."""
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("AAPL")], now=NOW, env=env)
        buy_intent.claim_ready(DAY, SESSION, env=env)
        buy_intent.write_ready(DAY, SESSION, [_row("MSFT")], now=NOW, env=env)
        assert sorted(buy_intent.read_ready(DAY, SESSION, env=env)) == ["MSFT"]

    def test_a_malformed_row_without_a_symbol_is_ignored(self, tmp_path):
        env = _env(tmp_path)
        written = buy_intent.write_ready(DAY, SESSION, [{"price": 1.0}, _row("AAPL")],
                                         now=NOW, env=env)
        assert written == 1
        assert list(buy_intent.read_ready(DAY, SESSION, env=env)) == ["AAPL"]


class TestIntentQueueSource:
    """The execution worker's source implements the SAME interface
    `run_live_buy_entry_cycle` already accepts from ActiveWatchSource,
    so the shared cycle runs unmodified against claimed intents."""

    def test_symbols_claims_the_queue(self, tmp_path):
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("UNH")], now=NOW, env=env)
        rollout = SimpleNamespace(allowed_symbols=frozenset())
        source = IntentQueueSource(trading_day=DAY, session=SESSION,
                                   rollout=rollout, now=NOW, env=env)
        assert source.symbols() == ["UNH"]
        # a second call must not re-claim (the queue is already empty)
        assert source.symbols() == ["UNH"]
        assert buy_intent.read_ready(DAY, SESSION, env=env) == {}

    def test_allowed_symbols_claims_first_when_called_before_symbols(self, tmp_path):
        """Production evidence 2026-09-09: kis_live_trading.run_live_buy_
        entry_cycle calls source.allowed_symbols() BEFORE source.symbols()
        (kis_live_trading.py:740 then :743). An earlier version of
        IntentQueueSource populated its claim only inside .symbols(), so
        .allowed_symbols() always saw an empty claim -- every real,
        already-queued candidate (IOT, OWL, UMC, VIST that morning) was
        refused as "not in live_rollout.allowed_symbols" with NO operator
        restriction actually configured. allowed_symbols() must claim the
        queue itself when asked first."""
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("UNH"), _row("HUM")], now=NOW, env=env)
        rollout = SimpleNamespace(allowed_symbols=frozenset())  # no operator restriction
        source = IntentQueueSource(trading_day=DAY, session=SESSION,
                                   rollout=rollout, now=NOW, env=env)
        # allowed_symbols() first, exactly as run_live_buy_entry_cycle does.
        allowed = source.allowed_symbols()
        assert allowed == frozenset({"UNH", "HUM"})
        # symbols() afterward must still see the SAME claim, not re-claim
        # (the queue is already empty at this point).
        assert sorted(source.symbols()) == ["HUM", "UNH"]

    def test_allowed_symbols_respects_operator_allow_list(self, tmp_path):
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("UNH"), _row("HUM")], now=NOW, env=env)
        rollout = SimpleNamespace(allowed_symbols=frozenset({"UNH"}))
        source = IntentQueueSource(trading_day=DAY, session=SESSION,
                                   rollout=rollout, now=NOW, env=env)
        source.symbols()
        assert source.allowed_symbols() == frozenset({"UNH"})

    def test_candidate_row_and_claimed_symbols(self, tmp_path):
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("UNH")], now=NOW, env=env)
        rollout = SimpleNamespace(allowed_symbols=frozenset())
        source = IntentQueueSource(trading_day=DAY, session=SESSION,
                                   rollout=rollout, now=NOW, env=env)
        source.symbols()
        assert source.candidate_row("UNH")["symbol"] == "UNH"
        assert source.claimed_symbols() == ["UNH"]

    def test_score_none_qualification_works_through_the_intent_path(self, tmp_path):
        """M: an S6 candidate with no scanner rank (score=None, exactly
        what fast_watch always produces) still qualifies via the
        S6_SCORE_NOT_APPLICABLE sentinel, unchanged, reached through
        this new source."""
        env = _env(tmp_path)
        buy_intent.write_ready(DAY, SESSION, [_row("UNH", score=None)], now=NOW, env=env)
        rollout = SimpleNamespace(allowed_symbols=frozenset())
        source = IntentQueueSource(trading_day=DAY, session=SESSION,
                                   rollout=rollout, now=NOW, env=env)
        source.symbols()
        result = source.qualify("UNH")
        from s6_live.qualification import S6_SCORE_NOT_APPLICABLE

        assert result.qualified is True
        assert result.score == S6_SCORE_NOT_APPLICABLE

    def test_name_matches_the_s6_strategy_source_identity(self, tmp_path):
        from s6_live.candidate_source import SOURCE_S6

        source = IntentQueueSource(trading_day=DAY, session=SESSION,
                                   rollout=SimpleNamespace(allowed_symbols=frozenset()),
                                   now=NOW, env=_env(tmp_path))
        assert source.name == SOURCE_S6

    def test_signal_valid_seconds_matches_the_existing_s6_policy(self, tmp_path):
        from s6_live.candidate_source import SIGNAL_VALID_SECONDS

        source = IntentQueueSource(trading_day=DAY, session=SESSION,
                                   rollout=SimpleNamespace(allowed_symbols=frozenset()),
                                   now=NOW, env=_env(tmp_path))
        assert source.signal_valid_seconds() == SIGNAL_VALID_SECONDS


class TestRunOnceStrategyDispatch:
    """B: the S6 fast-watch tick must not wait for slow KIS execution --
    proven here by showing it never even calls the shared cycle. N: S1
    is untouched -- it still reaches the shared cycle exactly as
    before."""

    def _fake_active_watch_source(self, ready_symbols, rows):
        return SimpleNamespace(
            _trading_day=DAY, _session=SESSION,
            symbols=lambda: ready_symbols,
            candidate_row=lambda s: rows.get(s),
        )

    def test_s6_strategy_never_calls_the_shared_execution_cycle(self, monkeypatch):
        from scripts import run_live_buy_entry as runner

        rows = {"UNH": _row("UNH")}
        fake_source = self._fake_active_watch_source(["UNH"], rows)
        monkeypatch.setitem(runner.SOURCE_FACTORIES, "s6",
                            lambda rollout, now, broker=None: fake_source)

        def _must_not_be_called(*a, **k):
            raise AssertionError("run_live_buy_entry_cycle must not be "
                                 "called by the fast-watch tick")
        monkeypatch.setattr(runner.klt, "run_live_buy_entry_cycle", _must_not_be_called)
        monkeypatch.setattr(runner, "_funnel", lambda *a, **k: None)

        written = {}

        def _fake_write_intents(source, *, now):
            written["call"] = (source, now)
            return ["UNH"], 1
        monkeypatch.setattr(runner, "_s6_write_intents", _fake_write_intents)

        results = runner.run_once(broker=SimpleNamespace(), strategy="s6")
        assert results["submitted"] == []
        assert results["ready_written"] == ["UNH"]
        assert written["call"][0] is fake_source

    def test_s6_write_intents_persists_ready_rows(self, tmp_path, monkeypatch):
        from scripts import run_live_buy_entry as runner

        monkeypatch.setenv("S6_ACTIVE_WATCH_DIR", str(tmp_path))
        rows = {"UNH": _row("UNH")}
        fake_source = self._fake_active_watch_source(["UNH"], rows)

        ready, written = runner._s6_write_intents(fake_source, now=NOW)
        assert ready == ["UNH"]
        assert written == 1
        assert sorted(buy_intent.read_ready(DAY, SESSION, env=None)) == ["UNH"]

    def test_s1_strategy_still_reaches_the_shared_execution_cycle(self, monkeypatch):
        from scripts import run_live_buy_entry as runner

        calls = {"n": 0}

        def _record(*a, **k):
            calls["n"] += 1
            return {"submitted": [], "blocked": [], "skipped": []}
        monkeypatch.setattr(runner.klt, "run_live_buy_entry_cycle", _record)
        monkeypatch.setattr(runner, "_funnel", lambda *a, **k: None)

        runner.run_once(broker=SimpleNamespace(), strategy="s1")
        assert calls["n"] == 1

    def test_s6_buy_worker_strategy_reaches_the_shared_execution_cycle_with_the_claimed_source(
            self, monkeypatch):
        from scripts import run_live_buy_entry as runner

        fake_intent_source = SimpleNamespace(
            claimed_symbols=lambda: ["UNH"], intent_metadata=lambda s: {})
        monkeypatch.setitem(runner.SOURCE_FACTORIES, "s6_buy_worker",
                            lambda rollout, now, broker=None: fake_intent_source)

        seen = {}
        def _record(*, broker, candidate_source):
            seen["source"] = candidate_source
            return {"submitted": ["UNH"], "blocked": [], "skipped": []}
        monkeypatch.setattr(runner.klt, "run_live_buy_entry_cycle", _record)
        monkeypatch.setattr(runner, "_execution_funnel", lambda *a, **k: None)

        results = runner.run_once(broker=SimpleNamespace(), strategy="s6_buy_worker")
        assert seen["source"] is fake_intent_source
        assert results["submitted"] == ["UNH"]


class TestS1Untouched:
    """N: S1's own candidate source and call site are unmodified by
    this decoupling -- S1 never went through fast_watch/buy_intent at
    all, so nothing about it needed to change."""

    def test_s1_factory_still_yields_the_cycle_default(self):
        from scripts import run_live_buy_entry as runner

        assert runner.SOURCE_FACTORIES["s1"](None, NOW, broker=None) is None

    def test_s1_executors_call_site_is_unmodified(self):
        import inspect

        import s1_live.executor as s1_executor

        source = inspect.getsource(s1_executor)
        assert "klt.run_live_buy_entry_cycle(broker=broker, now=now)" in source


class TestFunnelDoesNotMisreadTheHandoffAsADefect:
    """ready>0/submitted=0 is the s6 fast-watch tick's intended steady
    state now (READY only ever produces a BUY_INTENT there) -- it must
    not be logged as EXECUTION_DEFECT_SUSPECTED/ENTRY_YIELDED_EXPECTED,
    the way an s1 tick with the same shape correctly still is."""

    def _source_with_one_ready(self):
        from types import SimpleNamespace

        return SimpleNamespace(evaluations={
            "BH": SimpleNamespace(ready=True, state="READY", blocking=()),
        })

    def test_s6_fast_watch_tick_does_not_classify_the_handoff_as_a_defect(
            self, monkeypatch, caplog):
        import logging

        from scripts import run_live_buy_entry as runner

        called = {"n": 0}
        monkeypatch.setattr(
            runner, "_classify_no_submission",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ("X", 40, "X"))

        with caplog.at_level(logging.INFO, logger="live_buy_entry"):
            runner._funnel(
                self._source_with_one_ready(),
                {"submitted": [], "blocked": [], "skipped": []},
                since=NOW, expect_no_submission=True)
        assert called["n"] == 0
        assert any("ENTRY_READY_HANDED_OFF" in r.message for r in caplog.records)

    def test_other_strategies_still_classify_ready_with_no_submission(self, monkeypatch):
        from scripts import run_live_buy_entry as runner

        called = {"n": 0}
        monkeypatch.setattr(
            runner, "_classify_no_submission",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ("X", 40, "X"))

        runner._funnel(
            self._source_with_one_ready(),
            {"submitted": [], "blocked": [], "skipped": []},
            since=NOW)  # expect_no_submission defaults to False
        assert called["n"] == 1


class TestExecutionFunnelAnnouncesBlocks:
    """A worker tick's own blocks (INSUFFICIENT_CASH, RISK_BLOCKED,
    SESSION_BLOCKED, ...) must reach stock-live-trading exactly like a
    fast-watch tick's blocks always have -- `_execution_funnel` used to
    log FUNNEL_EXECUTION_SYMBOL and stop there, never calling
    `_announce_blocks`, so every block the decoupled worker itself
    produced went completely unannounced."""

    def test_execution_funnel_calls_announce_blocks(self, monkeypatch):
        from scripts import run_live_buy_entry as runner

        seen = []
        monkeypatch.setattr(runner, "_announce_blocks", lambda blocked: seen.append(list(blocked)))

        source = SimpleNamespace(intent_metadata=lambda s: {})
        results = {"submitted": [], "skipped": [],
                  "blocked": [("BH", "insufficient KIS orderable cash for even 1 share "
                                     "available=16.00 required=356.83 shortfall=340.83")]}
        runner._execution_funnel(source, ["BH"], results, since=NOW)
        assert seen == [[("BH", "insufficient KIS orderable cash for even 1 share "
                               "available=16.00 required=356.83 shortfall=340.83")]]

    def test_execution_funnel_with_no_blocks_calls_announce_blocks_with_empty(self):
        from scripts import run_live_buy_entry as runner

        source = SimpleNamespace(intent_metadata=lambda s: {})
        calls = {"n": 0}

        def _record(blocked):
            calls["n"] += 1
            assert list(blocked) == []
        import unittest.mock
        with unittest.mock.patch.object(runner, "_announce_blocks", _record):
            runner._execution_funnel(
                source, [], {"submitted": [], "blocked": [], "skipped": []}, since=NOW)
        assert calls["n"] == 1


class TestExecutionWorkerFailureAlert:
    """An unhandled crash in the execution worker must not be silent --
    claimed BUY_INTENTs sit unprocessed until the next minute, and an
    operator needs to know the worker itself is failing, not just infer
    it from a gap in FUNNEL_EXECUTION lines."""

    def test_worker_failure_sends_a_live_alert(self, monkeypatch):
        from scripts import run_live_buy_entry as runner

        monkeypatch.setattr(runner, "refusal_reason", lambda: None)
        monkeypatch.setattr(runner, "_s1_is_falling_behind", lambda: False)

        def _boom(*, strategy):
            raise RuntimeError("boom")
        monkeypatch.setattr(runner, "run_once", _boom)

        alerted = {}
        import operations.alerts as alerts
        monkeypatch.setattr(alerts, "send_alert",
                            lambda message: alerted.setdefault("message", message) or True)

        status = runner.main(["--strategy", "s6_buy_worker"])
        assert status == runner.EXIT_ERROR
        assert "execution worker failed" in alerted["message"]

    def test_s1_failure_does_not_send_the_worker_alert(self, monkeypatch):
        """Scoped to the new worker strategy only -- S1's own failure
        modes are unrelated to this task and already have their own
        (different) monitoring; this must not change S1's behavior."""
        from scripts import run_live_buy_entry as runner

        monkeypatch.setattr(runner, "refusal_reason", lambda: None)
        monkeypatch.setattr(runner, "_s1_is_falling_behind", lambda: False)

        def _boom(*, strategy):
            raise RuntimeError("boom")
        monkeypatch.setattr(runner, "run_once", _boom)

        alerted = {"n": 0}
        import operations.alerts as alerts
        monkeypatch.setattr(alerts, "send_alert",
                            lambda message: alerted.__setitem__("n", alerted["n"] + 1))

        status = runner.main(["--strategy", "s1"])
        assert status == runner.EXIT_ERROR
        assert alerted["n"] == 0
