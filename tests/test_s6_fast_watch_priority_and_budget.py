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

    def test_published_discovery_is_tier_one(self):
        assert fast_watch.ActiveWatchSource._priority_tier(
            self._entry("A", strategy_source="S6_FULL_DISCOVERY",
                        transport_source=active_watch.TRANSPORT_REST)) == 1

    def test_websocket_backed_no_signal_is_tier_two(self):
        assert fast_watch.ActiveWatchSource._priority_tier(
            self._entry("A", strategy_source=active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
                        transport_source=active_watch.TRANSPORT_WEBSOCKET)) == 2

    def test_rest_backed_no_signal_is_tier_three_the_lowest(self):
        assert fast_watch.ActiveWatchSource._priority_tier(
            self._entry("A", strategy_source=active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
                        transport_source=active_watch.TRANSPORT_REST)) == 3

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
        assert calls["ssl_append"] == 1, "the required audit record must always be written"
        assert calls["quality_blocks"] == 0, "Slack must be skipped once over budget"
        assert calls["range_shadow"] == 0, "ORB15 shadow must be skipped once over budget"

    def test_within_budget_runs_everything_as_before(self, tmp_path, monkeypatch):
        from scripts import run_live_buy_entry as runner

        calls = {"quality_blocks": 0, "range_shadow": 0}
        monkeypatch.setattr(runner, "_announce_quality_blocks",
                            lambda *a, **k: calls.__setitem__(
                                "quality_blocks", calls["quality_blocks"] + 1))

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
