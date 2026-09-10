"""Fast-watch evaluation priority (P0-P3) and the hard per-tick budget.

Production evidence 2026-09-09: a 73.7-second tick against a 60-second
cron interval caused the next cron trigger to be OVERLAP_SKIPPED, and a
fresh current-run provisional PASS (META) waited 96.1s to be visible to
fast-watch -- over the mandatory 60,000ms bound -- because ordinary
backlog was evaluated ahead of it and non-critical post-processing
(ORB15 shadow, entry-quality Slack) ran unconditionally regardless of
how long the tick had already taken.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from s6_live import active_watch, fast_watch

NOW = datetime(2026, 9, 9, 13, 4, tzinfo=timezone.utc)
DAY = "2026-09-09"
SESSION = "PREMARKET"


def _env(tmp_path):
    return {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}


class TestPriorityTiers:
    def _entry(self, symbol, *, strategy_source=None, transport_source=None):
        return {"symbol": symbol, "strategy_source": strategy_source,
                "transport_source": transport_source}

    def test_current_run_provisional_is_tier_zero(self):
        assert fast_watch.ActiveWatchSource._priority_tier(
            self._entry("A", strategy_source="S6_PROVISIONAL_PASS",
                        transport_source=active_watch.TRANSPORT_REST)) == 0

    def test_ready_near_is_tier_one_regardless_of_strategy_source(self):
        """A symbol the LAST evaluation left one condition away from
        READY is scheduled right after a fresh provisional PASS -- ahead
        of a merely-discovered symbol with no such recent promise."""
        assert fast_watch.ActiveWatchSource._priority_tier(
            self._entry("A", strategy_source=active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
                        transport_source=active_watch.TRANSPORT_REST),
            ready_near=True) == 1

    def test_published_discovery_is_tier_two(self):
        assert fast_watch.ActiveWatchSource._priority_tier(
            self._entry("A", strategy_source="S6_FULL_DISCOVERY",
                        transport_source=active_watch.TRANSPORT_REST)) == 2

    def test_websocket_backed_no_signal_is_tier_three(self):
        assert fast_watch.ActiveWatchSource._priority_tier(
            self._entry("A", strategy_source=active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
                        transport_source=active_watch.TRANSPORT_WEBSOCKET)) == 3

    def test_rest_backed_no_signal_is_tier_four_the_lowest(self):
        assert fast_watch.ActiveWatchSource._priority_tier(
            self._entry("A", strategy_source=active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
                        transport_source=active_watch.TRANSPORT_REST)) == 4

    def test_tier_groups_map_to_hot_warm_cold(self):
        assert fast_watch.ActiveWatchSource.tier_group(0) == "HOT"
        assert fast_watch.ActiveWatchSource.tier_group(1) == "HOT"
        assert fast_watch.ActiveWatchSource.tier_group(2) == "WARM"
        assert fast_watch.ActiveWatchSource.tier_group(3) == "WARM"
        assert fast_watch.ActiveWatchSource.tier_group(4) == "COLD"

    def test_a_fresh_pass_is_scheduled_ahead_of_a_deep_backlog(self, tmp_path, monkeypatch):
        """The exact production scenario: 80 established collector-
        membership entries (mixed transport), one fresh current-run
        provisional PASS appended at the tail of the STORED file order.
        Evaluation order must put the fresh PASS first regardless."""
        entries = []
        for i in range(40):
            entries.append({"symbol": f"WS{i}", "strategy_source": active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
                            "transport_source": active_watch.TRANSPORT_WEBSOCKET})
        for i in range(39):
            entries.append({"symbol": f"RS{i}", "strategy_source": active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
                            "transport_source": active_watch.TRANSPORT_REST})
        # The fresh PASS lands LAST in stored order (as observed in
        # production: appended after 80 pre-existing entries).
        entries.append({"symbol": "META", "strategy_source": active_watch.PROVISIONAL_SOURCE,
                        "transport_source": active_watch.TRANSPORT_WEBSOCKET})

        active_watch.merge(DAY, [dict(e, source=e["strategy_source"],
                                      transport_source=e["transport_source"])
                                 for e in entries],
                           session=SESSION, now=NOW, env=_env(tmp_path),
                           max_symbols=len(entries))

        rollout = SimpleNamespace(allowed_symbols=frozenset())
        source = fast_watch.ActiveWatchSource(
            trading_day=DAY, session=SESSION, rollout=rollout, now=NOW, env=_env(tmp_path))
        source._state = active_watch.read(DAY, session=SESSION, now=NOW, env=_env(tmp_path))
        ordered = source._active_symbols()
        assert ordered[0] == "META", ordered[:3]


class TestHardTickBudget:
    def test_shadow_recorder_defers_before_any_audit_write_at_deadline(self, monkeypatch, caplog):
        from scripts import run_live_buy_entry as runner
        import s6_live.shadow_signal_log as ssl

        class Source:
            _session = SESSION
            evaluations = {"A": SimpleNamespace(ready=False, blocking=(), state="WATCHING",
                                                  features=None, detail={}, evaluated_at=NOW)}
            candidate_row = lambda self, symbol: None

        calls = []
        monkeypatch.setattr(ssl, "append", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(runner, "_shadow_budget_remaining", lambda *a, **k: 0)
        runner._record_shadow_signals(Source(), {"blocked": (), "skipped": (), "submitted": ()}, since=NOW)
        assert calls == []

    def test_shadow_recorder_stops_between_symbols_when_append_spends_deadline(self, monkeypatch):
        from scripts import run_live_buy_entry as runner
        import s6_live.shadow_signal_log as ssl

        class Source:
            _session = SESSION
            evaluations = {s: SimpleNamespace(ready=False, blocking=(), state="WATCHING",
                                               features=None, detail={}, evaluated_at=NOW)
                           for s in ("A", "B")}
            candidate_row = lambda self, symbol: None

        remaining = iter((1, 1, 0))
        monkeypatch.setattr(runner, "_shadow_budget_remaining", lambda *a, **k: next(remaining))
        monkeypatch.setattr(runner, "_shadow_deferred_budget", lambda **k: None)
        calls = []
        monkeypatch.setattr(ssl, "append", lambda record, **k: calls.append(record))
        runner._record_shadow_signals(Source(), {"blocked": (), "skipped": (), "submitted": ()}, since=NOW)
        assert [r["symbol"] for r in calls] == ["A"]

    def test_budget_exceeded_skips_non_critical_work_not_the_audit_record(self, tmp_path, monkeypatch):
        from scripts import run_live_buy_entry as runner

        calls = {"quality_blocks": 0, "range_shadow": 0, "ssl_append": 0}
        monkeypatch.setattr(runner, "_announce_quality_blocks",
                            lambda *a, **k: calls.__setitem__(
                                "quality_blocks", calls["quality_blocks"] + 1))

        class FakeEvaluation:
            ready = False
            blocking = ()
            state = "WATCHING"
            features = None
            detail = {}
            evaluated_at = NOW

        class FakeSource:
            _session = SESSION
            evaluations = {"AAPL": FakeEvaluation()}

            def candidate_row(self, symbol):
                return None

        import s6_live.shadow_signal_log as ssl
        monkeypatch.setattr(ssl, "append",
                            lambda *a, **k: calls.__setitem__(
                                "ssl_append", calls["ssl_append"] + 1))
        import s6_live.range_shadow as range_shadow
        monkeypatch.setattr(range_shadow, "record_cycle",
                            lambda *a, **k: calls.__setitem__(
                                "range_shadow", calls["range_shadow"] + 1))

        # A tick that has ALREADY run past the hard budget by the time
        # post-processing starts.
        long_ago = NOW - __import__("datetime").timedelta(
            seconds=runner._TICK_HARD_BUDGET_SECONDS + 5)
        runner._record_shadow_signals(FakeSource(), {"blocked": (), "skipped": (),
                                                      "submitted": ()}, since=long_ago)
        assert calls["ssl_append"] == 0, "optional audit work must defer at deadline"
        assert calls["quality_blocks"] == 0, "Slack must be skipped once over budget"
        assert calls["range_shadow"] == 0, "ORB15 shadow must be skipped once over budget"

    def test_budget_exceeded_also_skips_the_closed_bar_shadow(self, tmp_path, monkeypatch):
        """Production evidence 2026-09-09 (post-fix): a live py-spy trace
        caught a tick still running 6+ minutes inside
        _record_closed_bar_shadow -> closed_bar_shadow.compare ->
        kis_bar_features.build_from_bars -> entry_quality.compute ->
        time_bucket_baseline -> load_store -> json.loads. That call sat
        in its own try/except AFTER the budget check above, so it was
        never actually covered by the hard tick budget -- this is the
        gap that let the fix in this file still overrun."""
        from scripts import run_live_buy_entry as runner

        calls = {"closed_bar_shadow": 0}
        monkeypatch.setattr(runner, "_record_closed_bar_shadow",
                            lambda *a, **k: calls.__setitem__(
                                "closed_bar_shadow", calls["closed_bar_shadow"] + 1))

        class FakeSource:
            _session = SESSION
            evaluations = {}

            def candidate_row(self, symbol):
                return None

        long_ago = NOW - __import__("datetime").timedelta(
            seconds=runner._TICK_HARD_BUDGET_SECONDS + 5)
        runner._record_shadow_signals(FakeSource(), {"blocked": (), "skipped": (),
                                                      "submitted": ()}, since=long_ago)
        assert calls["closed_bar_shadow"] == 0, \
            "the closed-bar shadow comparison must be skipped once over budget"

    def test_within_budget_runs_everything_as_before(self, tmp_path, monkeypatch):
        from scripts import run_live_buy_entry as runner

        calls = {"quality_blocks": 0, "range_shadow": 0, "closed_bar_shadow": 0}
        monkeypatch.setattr(runner, "_announce_quality_blocks",
                            lambda *a, **k: calls.__setitem__(
                                "quality_blocks", calls["quality_blocks"] + 1))
        monkeypatch.setattr(runner, "_record_closed_bar_shadow",
                            lambda *a, **k: calls.__setitem__(
                                "closed_bar_shadow", calls["closed_bar_shadow"] + 1))

        class FakeSource:
            _session = SESSION
            evaluations = {}

            def candidate_row(self, symbol):
                return None

        import s6_live.range_shadow as range_shadow
        monkeypatch.setattr(range_shadow, "record_cycle",
                            lambda *a, **k: calls.__setitem__(
                                "range_shadow", calls["range_shadow"] + 1))

        runner._record_shadow_signals(
            FakeSource(), {"blocked": (), "skipped": (), "submitted": ()},
            since=datetime.now(timezone.utc))
        assert calls["quality_blocks"] == 1
        assert calls["range_shadow"] == 1
        assert calls["closed_bar_shadow"] == 1

    def test_the_hard_budget_leaves_headroom_under_the_cron_interval(self):
        from scripts import run_live_buy_entry as runner

        assert runner._TICK_HARD_BUDGET_SECONDS < 60.0
        assert runner._TICK_HARD_BUDGET_SECONDS >= 40.0


class TestLazySchedulersBaseImport:
    """The other half of the latency fix: scanners.base's package
    __init__ used to eagerly import pandas/yfinance/pandas_market_
    calendars for every submodule import, including
    scanners.base.session_range's plain date arithmetic. Measured cold:
    11 seconds, once per (fresh, cron-launched) entry-runner process."""

    def test_existing_eager_names_still_resolve_correctly(self):
        from scanners.base import BaseScanner, ScannerConfig, load_config

        assert BaseScanner is not None
        assert ScannerConfig is not None
        assert callable(load_config)

    def test_an_unknown_name_still_raises_attributeerror(self):
        import scanners.base as b

        try:
            b.DefinitelyNotARealExport
        except AttributeError:
            pass
        else:
            raise AssertionError("expected AttributeError")

    def test_session_range_is_reachable_without_the_heavy_names(self):
        from scanners.base import session_range as srange

        assert srange.window_for("PREMARKET") is not None


class TestClosedBarShadowPerSymbolBudget:
    """`_record_closed_bar_shadow` sits after the tick-level gate but
    runs its OWN per-symbol loop of entry-quality baseline computations
    -- a second live py-spy trace on 2026-09-09 (after the first fix
    was deployed) caught a REGULAR-session tick still running 6+
    minutes inside exactly this loop, one symbol at a time. A gate
    checked only once, before the whole call, would not have stopped
    an overrun starting on the very first symbol -- this budget is
    re-checked before each symbol instead."""

    def _patched(self, monkeypatch, *, compare_side_effect=None):
        from scripts import run_live_buy_entry as runner
        import s6_live.closed_bar_shadow as closed_bar_shadow
        import s6_live.kis_bar_features as kis_bar_features

        calls = {"compare": [], "compare_readiness": []}
        monkeypatch.setattr(kis_bar_features, "load_store",
                            lambda *a, **k: object())

        def _compare(symbol, **k):
            if compare_side_effect:
                compare_side_effect()
            calls["compare"].append(symbol)
            return None

        monkeypatch.setattr(closed_bar_shadow, "compare", _compare)
        monkeypatch.setattr(closed_bar_shadow, "compare_readiness",
                            lambda symbol, **k: calls["compare_readiness"].append(symbol))
        return runner, calls

    def test_already_over_budget_reaches_no_symbol(self, monkeypatch):
        runner, calls = self._patched(monkeypatch)
        long_ago = NOW - __import__("datetime").timedelta(
            seconds=runner._TICK_HARD_BUDGET_SECONDS + 5)
        runner._record_closed_bar_shadow(
            None, ["AAA", "BBB", "CCC"], session=SESSION, day=DAY, since=long_ago)
        assert calls["compare"] == []
        assert calls["compare_readiness"] == []

    def test_within_budget_reaches_every_symbol(self, monkeypatch):
        runner, calls = self._patched(monkeypatch)
        runner._record_closed_bar_shadow(
            None, ["AAA", "BBB", "CCC"], session=SESSION, day=DAY,
            since=datetime.now(timezone.utc))
        assert calls["compare"] == ["AAA", "BBB", "CCC"]
        assert calls["compare_readiness"] == ["AAA", "BBB", "CCC"]

    def test_budget_exhausted_mid_loop_stops_before_the_next_symbol(self, monkeypatch):
        import time

        def _slow():
            time.sleep(0.15)

        runner, calls = self._patched(monkeypatch, compare_side_effect=_slow)
        monkeypatch.setattr(runner, "_TICK_HARD_BUDGET_SECONDS", 0.05)
        runner._record_closed_bar_shadow(
            None, ["AAA", "BBB", "CCC"], session=SESSION, day=DAY,
            since=datetime.now(timezone.utc))
        # AAA is reached (elapsed ~0 < budget), its slow compare() then
        # pushes elapsed past the budget, so BBB is never started.
        assert calls["compare"] == ["AAA"]
        assert calls["compare_readiness"] == ["AAA"]


class TestRangeShadowDeadline:
    """The SAME "unbounded per-symbol research loop" shape as
    _record_closed_bar_shadow, found the same way: a live py-spy trace
    on 2026-09-09 caught scripts/run_live_buy_entry.py's tick still
    running ~4s/symbol inside range_shadow.record_cycle, well past the
    tick's own 50s budget, because that loop had no deadline of its
    own to check -- only fixed for closed-bar shadow in the prior
    round. `deadline` defaults to None (unlimited) so every existing
    caller that does not pass one keeps its exact prior behaviour."""

    def _source(self, symbols):
        from types import SimpleNamespace

        return SimpleNamespace(
            _session="PREMARKET",
            evaluations={s: SimpleNamespace() for s in symbols})

    def test_no_deadline_processes_every_symbol_as_before(self, monkeypatch):
        from s6_live import range_shadow

        seen = []
        monkeypatch.setattr(range_shadow, "evaluate_symbol",
                            lambda symbol, **k: seen.append(symbol) or None)
        written = range_shadow.record_cycle(
            self._source(["AAA", "BBB", "CCC"]), trading_day=DAY, now=NOW,
            store=object())
        assert seen == ["AAA", "BBB", "CCC"]
        assert written == 0

    def test_deadline_stops_before_the_next_symbol(self, monkeypatch):
        from s6_live import range_shadow

        seen = []
        monkeypatch.setattr(range_shadow, "evaluate_symbol",
                            lambda symbol, **k: seen.append(symbol) or None)
        calls = {"n": 0}

        def _deadline():
            calls["n"] += 1
            return calls["n"] > 1  # true from the second check onward
        range_shadow.record_cycle(
            self._source(["AAA", "BBB", "CCC"]), trading_day=DAY, now=NOW,
            store=object(), deadline=_deadline)
        assert seen == ["AAA"]

    def test_run_live_buy_entry_wires_the_tick_budget_as_the_deadline(self, monkeypatch):
        """The call site passes _shadow_budget_remaining, not a fresh
        clock -- so this deadline agrees with the audit-write loop's
        own budget rather than tracking a second, independent one."""
        from scripts import run_live_buy_entry as runner
        import s6_live.range_shadow as range_shadow

        captured = {}
        monkeypatch.setattr(
            range_shadow, "record_cycle",
            lambda *a, deadline=None, **k: captured.setdefault("deadline", deadline))
        monkeypatch.setattr(runner, "_announce_quality_blocks", lambda *a, **k: None)

        class FakeSource:
            _session = SESSION
            evaluations = {}

            def candidate_row(self, symbol):
                return None

        runner._record_shadow_signals(
            FakeSource(), {"blocked": (), "skipped": (), "submitted": ()},
            since=datetime.now(timezone.utc))
        assert callable(captured["deadline"])
        assert captured["deadline"]() is False  # freshly within budget
