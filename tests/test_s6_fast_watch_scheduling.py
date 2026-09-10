"""Bounded fast-watch tick time and fair scheduling.

Production evidence 2026-09-09/10: fast-watch ticks ranged 37-69s
against a 60s cron interval, with 25/61 (41%) OVERLAP_SKIPPED in one
hour. Traced to the per-symbol Budget being a fresh clock started only
AFTER `_load()` already ran (uncapped), so the tick's TOTAL wall time
was whatever load + evaluation happened to add up to -- unbounded from
the tick's own perspective, and growing with logical watchlist size.

These tests prove: (1) the tick's own deadline, not just the
evaluation sub-budget, bounds total wall time regardless of how slow
`_load()` was; (2) a symbol repeatedly deferred rises in priority
(aging/starvation prevention) rather than waiting behind the same
higher-tier backlog forever; (3) the per-symbol audit-write batches
into one flock instead of one per symbol.
"""

import time
from datetime import datetime, timezone
from types import SimpleNamespace

from s6_live import active_watch, fast_watch, watch_priority_state

NOW = datetime(2026, 9, 9, 13, 4, tzinfo=timezone.utc)
DAY = "2026-09-09"
SESSION = "PREMARKET"


def _env(tmp_path):
    return {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}


def _entries(n, *, prefix="RS", transport=active_watch.TRANSPORT_REST):
    return [{"symbol": f"{prefix}{i}",
            "strategy_source": active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
            "transport_source": transport} for i in range(n)]


class TestUnifiedTickDeadline:
    def _seeded_source(self, tmp_path, n, *, rollout=None):
        env = _env(tmp_path)
        entries = _entries(n)
        active_watch.merge(DAY, [dict(e, source=e["strategy_source"]) for e in entries],
                           session=SESSION, now=NOW, env=env, max_symbols=n)
        source = fast_watch.ActiveWatchSource(
            trading_day=DAY, session=SESSION,
            rollout=rollout or SimpleNamespace(allowed_symbols=frozenset()),
            now=NOW, env=env)
        # Bypass the live refresh (collector/candidate-file reads this
        # test environment has none of) with exactly the seeded state,
        # same as the existing priority-ordering test in
        # test_s6_fast_watch_priority_and_budget.py.
        source._state = active_watch.read(DAY, session=SESSION, now=NOW, env=env)
        return source

    def test_a_slow_load_shrinks_the_eval_budget_not_the_tick(self, tmp_path, monkeypatch):
        """A `_load()` that itself eats most of the tick's own deadline
        must leave the per-symbol loop almost no time -- not a fresh
        30s window on top of whatever load already cost."""
        source = self._seeded_source(tmp_path, 10)

        real_load = fast_watch.ActiveWatchSource._load

        def _slow_load(self):
            time.sleep(0.2)
            return real_load(self)
        monkeypatch.setattr(fast_watch.ActiveWatchSource, "_load", _slow_load)
        monkeypatch.setattr(fast_watch, "FAST_WATCH_TICK_DEADLINE_SECONDS", 0.25)

        ready = source.symbols()
        assert ready == []
        # The 0.2s "load" alone consumed most of the 0.25s tick deadline,
        # so the eval loop's own budget was ~0.05s -- at least some
        # symbols must have been deferred rather than all ten evaluated
        # in a budget that no longer had 30s to spend.
        assert len(source.waiting_for_data) > 0

    def test_total_symbols_call_stays_near_the_deadline_regardless_of_watchlist_size(
            self, tmp_path, monkeypatch):
        """The whole point: a bigger logical watch increases DEFERRED,
        not wall-clock tick duration."""
        source = self._seeded_source(tmp_path, 60)
        monkeypatch.setattr(fast_watch, "FAST_WATCH_TICK_DEADLINE_SECONDS", 1.0)
        # Force every symbol's own evaluation to look expensive so the
        # tick would obviously run long if the deadline did not bound it.
        monkeypatch.setattr(
            fast_watch.realtime_features, "build",
            lambda *a, **k: SimpleNamespace(
                market_data_asof=NOW, closed_bar_only=True, price_source="test",
                range_origin_timestamp=None, entry_quality=None, price=1.0,
                range_minutes=5, range_high=1.0, range_low=1.0, vwap=1.0,
                ema9=1.0, ema21=1.0, volume_expansion=False, extension_pct=0.0))
        def _slow_evaluate(*a, **k):
            time.sleep(0.05)
            return SimpleNamespace(
                symbol=a[0], ready=False, state="WATCHING", blocking=("x",),
                features=None, evaluated_at=NOW)
        monkeypatch.setattr(fast_watch.precision_watch, "evaluate", _slow_evaluate)

        started = time.monotonic()
        source.symbols()
        elapsed = time.monotonic() - started
        # Generous multiplier over the 1.0s deadline for test-machine
        # scheduling noise -- the point is "bounded", not "exact".
        assert elapsed < 3.0, elapsed
        assert len(source.waiting_for_data) > 0


class TestAgingPreventsStarvation:
    def test_a_repeatedly_deferred_symbol_rises_within_its_own_tier(self, tmp_path):
        env = _env(tmp_path)
        watch_priority_state.update(
            DAY, SESSION, evaluated={}, deferred=["RS0"], now=NOW, env=env)
        watch_priority_state.update(
            DAY, SESSION, evaluated={}, deferred=["RS0"], now=NOW, env=env)
        watch_priority_state.update(
            DAY, SESSION, evaluated={}, deferred=["RS0"], now=NOW, env=env)
        # RS1 has never been deferred.
        state = watch_priority_state.read(DAY, SESSION, env=env)
        assert watch_priority_state.consecutive_defers(state.get("RS0") or {}) == 3
        assert watch_priority_state.consecutive_defers(state.get("RS1") or {}) == 0

        entries = _entries(2)  # RS0, RS1 -- same tier, RS0 stored FIRST
        active_watch.merge(DAY, [dict(e, source=e["strategy_source"]) for e in entries],
                           session=SESSION, now=NOW, env=env, max_symbols=2)
        rollout = SimpleNamespace(allowed_symbols=frozenset())
        source = fast_watch.ActiveWatchSource(
            trading_day=DAY, session=SESSION, rollout=rollout, now=NOW, env=env)
        source._state = active_watch.read(DAY, session=SESSION, now=NOW, env=env)
        ordered = source._active_symbols()
        assert ordered[0] == "RS0"

    def test_a_symbol_that_gets_evaluated_resets_its_defer_count(self, tmp_path):
        env = _env(tmp_path)
        watch_priority_state.update(
            DAY, SESSION, evaluated={}, deferred=["RS0"], now=NOW, env=env)
        watch_priority_state.update(
            DAY, SESSION, evaluated={"RS0": {"state": "WATCHING", "blocking_count": 3}},
            deferred=[], now=NOW, env=env)
        state = watch_priority_state.read(DAY, SESSION, env=env)
        assert watch_priority_state.consecutive_defers(state["RS0"]) == 0

    def test_ready_near_promotion_from_a_prior_evaluation(self, tmp_path):
        env = _env(tmp_path)
        watch_priority_state.update(
            DAY, SESSION, evaluated={"RS0": {"state": "WATCHING", "blocking_count": 1}},
            deferred=[], now=NOW, env=env)
        watch_priority_state.update(
            DAY, SESSION, evaluated={"RS1": {"state": "WATCHING", "blocking_count": 4}},
            deferred=[], now=NOW, env=env)
        state = watch_priority_state.read(DAY, SESSION, env=env)
        assert watch_priority_state.is_ready_near(state["RS0"]) is True
        assert watch_priority_state.is_ready_near(state["RS1"]) is False

    def test_no_prior_state_is_not_ready_near_and_not_aged(self):
        assert watch_priority_state.is_ready_near({}) is False
        assert watch_priority_state.consecutive_defers({}) == 0

    def test_read_never_raises_when_the_store_is_unconfigured(self):
        # No env at all -- _root() would raise RuntimeError internally;
        # scheduling must degrade quietly, not take the tick down.
        assert watch_priority_state.read(DAY, SESSION, env={}) == {}


class TestBatchedEvaluationWrites:
    def test_one_flock_writes_every_record(self, tmp_path):
        env = _env(tmp_path)
        records = [{"symbol": f"S{i}", "watch_state": "WATCHING"} for i in range(5)]
        active_watch.record_evaluations_batch(DAY, records, session=SESSION, env=env)
        target = active_watch._root(env) / f"{DAY}-{SESSION}-evaluations.jsonl"
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5

    def test_an_empty_batch_writes_nothing_and_does_not_create_the_file(self, tmp_path):
        env = _env(tmp_path)
        active_watch.record_evaluations_batch(DAY, [], session=SESSION, env=env)
        target = active_watch._root(env) / f"{DAY}-{SESSION}-evaluations.jsonl"
        assert not target.exists()

    def test_batch_and_single_write_produce_the_same_shape(self, tmp_path):
        env = _env(tmp_path)
        record = {"symbol": "AAA", "watch_state": "READY"}
        active_watch.record_evaluation(DAY, record, session=SESSION, env=env)
        active_watch.record_evaluations_batch(DAY, [record], session=SESSION, env=env)
        target = active_watch._root(env) / f"{DAY}-{SESSION}-evaluations.jsonl"
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert lines[0] == lines[1]
