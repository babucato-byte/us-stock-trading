"""S6 active-watch cap decoupling + generation precedence.

Confirmed production defects this fixes
----------------------------------------
A. `active_watch.MAX_SYMBOLS` aliased the physical WebSocket transport
   ceiling (kis_hdfscnt0.MAX_SUBSCRIPTIONS, 41), so the LOGICAL watchlist
   could never hold more names than the physical stream can carry --
   even though most of those names only need bounded REST evaluation,
   never a subscription.

B. A live, CURRENT-run provisional PASS could be discarded in favour of
   an OLDER, already-superseded completed-manifest row for the same
   symbol, because the same-cycle dedup let whichever row was appended
   LAST win, and the completed-manifest loop ran after the provisional
   one.

C. A pure collector-membership (transport) fact could overwrite a
   symbol's S6 discovery identity (scan_id, provisional,
   final_generation_published, candidate_discovered_at, ...), because
   one `source` field carried both "why watched" and "where data comes
   from", and the collector's own admission unconditionally rewrote it.

Real production case (2026-09-09, exactly reproduced in
TestMETARegression below): META PASSed under the CURRENTLY RUNNING scan
20260909_ADHOC_9036c7 and was written to the provisional store within
milliseconds -- but the merged watchlist still showed the OLD run
20260909_ADHOC_064ad8, provisional=false, source=KIS_COLLECTOR.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

NOW = datetime(2026, 9, 9, 12, 2, 33, tzinfo=timezone.utc)
DAY = "2026-09-09"
SESSION = "PREMARKET"
OLD_RUN = "20260909_ADHOC_064ad8"
CURRENT_RUN = "20260909_ADHOC_9036c7"


def _env(tmp_path):
    return {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}


def _patch_sources(monkeypatch, *, subscribed=(), final_rows=(),
                   running=True, run_id=CURRENT_RUN, market_session=SESSION):
    monkeypatch.setattr(
        "market_data.collector_status.describe",
        lambda *a, **k: {
            "subscribed_symbols": list(subscribed), "market_session": market_session,
            "state": "CONNECTED_ACTIVE", "last_heartbeat_at": None,
            "subscription_count": len(subscribed), "subscription_requested": len(subscribed),
            "collector_started_at": "2026-09-09T08:00:00Z",
        })
    monkeypatch.setattr("scanners.publish.candidates.read", lambda *a, **k: list(final_rows))
    monkeypatch.setattr("scanners.publish.scan_cycle.state",
                        lambda *a, **k: SimpleNamespace(running=running, run_id=run_id))


class TestPhysicalCapUnchanged:
    def test_the_physical_constant_still_equals_the_measured_transport_limit(self):
        from market_data import kis_hdfscnt0
        from s6_live import active_watch

        assert active_watch.MAX_SUBSCRIPTIONS == kis_hdfscnt0.MAX_SUBSCRIPTIONS == 41

    def test_active_watch_never_imports_the_collector_or_broker(self):
        """The physical cap is enforced by bootstrap_watchlist (pre-session
        selection) and run_realtime_bar_collector (refuses subscription
        42) -- neither of which this module can reach, so nothing here
        can cause a 42nd subscription."""
        import ast
        from pathlib import Path

        for path in ("s6_live/active_watch.py", "s6_live/fast_watch.py"):
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)
            forbidden = ("brokers", "execution", "scripts.run_realtime_bar_collector",
                        "market_data.bootstrap_watchlist")
            assert not [m for m in modules if m.startswith(forbidden)], (path, modules)


class TestLogicalCapDecoupled:
    def test_websocket_backed_count_never_exceeds_the_physical_ceiling(self, tmp_path, monkeypatch):
        """100 logically-admitted symbols, only 41 of them collector-
        subscribed: the WS-backed count in the merged payload is exactly
        41, never more, regardless of how large the logical watchlist is."""
        from s6_live import active_watch

        subscribed = [f"W{i}" for i in range(41)]
        _patch_sources(monkeypatch, subscribed=subscribed, final_rows=[
            {"symbol": s, "strategy_id": "S6_ORB_BREAKOUT_V1",
             "generated_at": "2026-09-09T11:00:00Z", "scanner_run_id": OLD_RUN}
            for s in [f"W{i}" for i in range(41)] + [f"R{i}" for i in range(59)]
        ], running=False, run_id=None)
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        assert len(state["entries"]) == 100
        assert state["websocket_backed"] == 41
        assert state["rest_backed"] == 59
        assert state["dropped"] == 0

    def test_a_symbol_beyond_41_websocket_slots_is_admitted_as_rest_backed(self, tmp_path, monkeypatch):
        """The task's §10 exactly: 41 WebSocket slots full, a fresh PASS
        for a 42nd symbol enters the LOGICAL watchlist as REST_BACKED,
        not as a rejected admission, and remains eligible for later
        fast-watch evaluation (visible through fast_watch, not just the
        raw store)."""
        from s6_live import active_watch, fast_watch

        active_watch.record_provisional_pass(
            DAY, SESSION, {"symbol": "XYZ", "scan_id": CURRENT_RUN,
                           "candidate_discovered_at": NOW.isoformat()},
            now=NOW, env=_env(tmp_path))
        subscribed = [f"W{i}" for i in range(41)]
        _patch_sources(monkeypatch, subscribed=subscribed)
        rollout = SimpleNamespace(allowed_symbols=frozenset())
        source = fast_watch.ActiveWatchSource(
            trading_day=DAY, session=SESSION, rollout=rollout, now=NOW,
            env=_env(tmp_path))
        symbols = source.allowed_symbols()
        assert "XYZ" in symbols
        info = source.describe()
        assert info["watchlist_size"] == 42
        assert info["websocket_backed"] == 41
        assert info["rest_backed"] == 1


class TestMETARegression:
    """Exactly the 2026-09-09 production incident: a symbol with a STALE
    identity from an old, already-superseded run, re-PASSing under the
    currently-running scan, and also collector-subscribed this cycle."""

    def test_current_provisional_outranks_old_final_even_when_also_websocket_backed(
            self, tmp_path, monkeypatch):
        from s6_live import active_watch

        # META's OLD, already-completed generation (run 064ad8).
        active_watch.record_provisional_pass(
            DAY, SESSION, {"symbol": "META", "scan_id": OLD_RUN},
            now=datetime(2026, 9, 9, 10, 23, tzinfo=timezone.utc), env=_env(tmp_path))
        _patch_sources(
            monkeypatch, subscribed=["META"], final_rows=[{
                "symbol": "META", "strategy_id": "S6_ORB_BREAKOUT_V1",
                "generated_at": "2026-09-09T11:35:14.990489Z", "scanner_run_id": OLD_RUN,
            }],
            running=False, run_id=None)  # that old run has finished
        old_state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY,
            now=datetime(2026, 9, 9, 11, 35, 20, tzinfo=timezone.utc), env=_env(tmp_path))
        old_row = next(r for r in old_state["entries"] if r["symbol"] == "META")
        assert old_row["strategy_source"] == "S6_FULL_DISCOVERY"
        assert old_row["scan_id"] == OLD_RUN
        assert old_row["provisional"] is False

        # Minutes later: a NEW scan is running and META PASSes again,
        # written to the provisional store within the same instant.
        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "META", "scan_id": CURRENT_RUN,
            "full_scan_started_at": "2026-09-09T12:02:23.528026+00:00",
            "symbol_evaluated_at": "2026-09-09T12:02:33.434897+00:00",
            "candidate_discovered_at": "2026-09-09T12:02:33.434897+00:00",
        }, now=NOW, env=_env(tmp_path))
        # The new scan is running; candidates.read() still returns only
        # the OLD manifest (the new one has not published -- it can't,
        # while it holds the lock).
        _patch_sources(monkeypatch, subscribed=["META"], final_rows=[{
            "symbol": "META", "strategy_id": "S6_ORB_BREAKOUT_V1",
            "generated_at": "2026-09-09T11:35:14.990489Z", "scanner_run_id": OLD_RUN,
        }], running=True, run_id=CURRENT_RUN)

        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))

        rows = [r for r in state["entries"] if r["symbol"] == "META"]
        assert len(rows) == 1, "exactly one META row -- no duplicate slot"
        row = rows[0]
        assert row["scan_id"] == CURRENT_RUN, "the OLD run must not outrank the live one"
        assert row["provisional"] is True
        assert row["final_generation_published"] is False
        assert row["strategy_source"] == active_watch.PROVISIONAL_SOURCE
        assert row["candidate_discovered_at"] == "2026-09-09T12:02:33.434897+00:00"
        # Transport is a live, independent fact: META IS collector-backed
        # this cycle, and that must be visible even though its STRATEGY
        # identity stayed with the fresh provisional PASS.
        assert row["transport_source"] == active_watch.TRANSPORT_WEBSOCKET

    def test_current_final_supersedes_current_provisional_once_the_scan_completes(
            self, tmp_path, monkeypatch):
        """CURRENT FINAL > CURRENT PROVISIONAL: once the producing scan
        finishes and publishes, the reconciled row replaces the
        provisional one -- same symbol, no duplicate."""
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "META", "scan_id": CURRENT_RUN,
            "candidate_discovered_at": "2026-09-09T12:02:33.434897+00:00",
        }, now=NOW, env=_env(tmp_path))
        _patch_sources(monkeypatch, subscribed=["META"], final_rows=[],
                       running=True, run_id=CURRENT_RUN)
        mid_scan = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        mid_row = next(r for r in mid_scan["entries"] if r["symbol"] == "META")
        assert mid_row["provisional"] is True

        # The scan completes: its manifest now contains META under the
        # SAME run id, and the lock is released.
        _patch_sources(monkeypatch, subscribed=["META"], final_rows=[{
            "symbol": "META", "strategy_id": "S6_ORB_BREAKOUT_V1",
            "generated_at": "2026-09-09T12:10:00Z", "scanner_run_id": CURRENT_RUN,
        }], running=False, run_id=None)
        after = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY,
            now=NOW.replace(minute=10), env=_env(tmp_path))
        rows = [r for r in after["entries"] if r["symbol"] == "META"]
        assert len(rows) == 1
        row = rows[0]
        assert row["provisional"] is False
        assert row["final_generation_published"] is True
        assert row["strategy_source"] == "S6_FULL_DISCOVERY"
        assert row["scan_id"] == CURRENT_RUN


class TestCollectorNeverOverwritesGenerationIdentity:
    def test_a_transport_only_refresh_leaves_established_identity_untouched(self, tmp_path):
        """§6 exactly: once a symbol carries an S6 discovery identity, a
        LATER cycle offering only a bare collector-membership addition
        (no strategy_source at all) must not touch scan_id/provisional/
        final_generation_published/candidate_discovered_at -- only
        transport_source, updated_at and expires_at move."""
        from s6_live import active_watch

        active_watch.merge(DAY, [{
            "symbol": "AAPL", "strategy_source": active_watch.FULL_DISCOVERY_SOURCE,
            "scan_id": OLD_RUN, "provisional": False, "final_generation_published": True,
            "candidate_discovered_at": "2026-09-09T08:44:49Z",
            "full_scan_started_at": "2026-09-09T08:00:00Z",
        }], session=SESSION, now=NOW, env=_env(tmp_path))

        # A LATER cycle: pure transport fact, no strategy claim at all.
        state = active_watch.merge(DAY, [{
            "symbol": "AAPL", "transport_source": active_watch.TRANSPORT_WEBSOCKET,
        }], session=SESSION, now=NOW.replace(minute=30), env=_env(tmp_path))

        row = next(r for r in state["entries"] if r["symbol"] == "AAPL")
        assert row["scan_id"] == OLD_RUN
        assert row["provisional"] is False
        assert row["final_generation_published"] is True
        assert row["candidate_discovered_at"] == "2026-09-09T08:44:49Z"
        assert row["strategy_source"] == active_watch.FULL_DISCOVERY_SOURCE
        assert row["transport_source"] == active_watch.TRANSPORT_WEBSOCKET

    def test_a_bare_collector_admission_with_no_prior_identity_gets_a_baseline(self, tmp_path):
        """A symbol with NO S6 discovery claim at all, watched only
        because the collector streams it, still needs SOME
        strategy_source -- the collector-membership baseline -- but that
        baseline must never masquerade as an S6 discovery (provisional
        and final_generation_published both stay False)."""
        from s6_live import active_watch

        state = active_watch.merge(DAY, [{
            "symbol": "TSLA", "transport_source": active_watch.TRANSPORT_WEBSOCKET,
        }], session=SESSION, now=NOW, env=_env(tmp_path))
        row = next(r for r in state["entries"] if r["symbol"] == "TSLA")
        assert row["strategy_source"] == active_watch.COLLECTOR_MEMBERSHIP_SOURCE
        assert row["provisional"] is False
        assert row["final_generation_published"] is False
        assert row["scan_id"] is None


class TestAbortSafetyWithGenerationPrecedence:
    def test_an_aborted_scans_provisional_never_competes_and_old_final_stands(
            self, tmp_path, monkeypatch):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "META", "scan_id": "dead-run",
            "candidate_discovered_at": "2026-09-09T12:05:00Z",
        }, now=NOW, env=_env(tmp_path))
        # The scan that wrote it is gone -- no lock held, no run id.
        _patch_sources(monkeypatch, subscribed=[], final_rows=[{
            "symbol": "META", "strategy_id": "S6_ORB_BREAKOUT_V1",
            "generated_at": "2026-09-09T11:35:14Z", "scanner_run_id": OLD_RUN,
        }], running=False, run_id=None)
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        rows = [r for r in state["entries"] if r["symbol"] == "META"]
        assert len(rows) == 1
        # The dead scan's provisional content must not appear anywhere --
        # the old, already-published final row is what stands.
        assert rows[0]["scan_id"] == OLD_RUN
        assert rows[0]["provisional"] is False


class TestRestartSafety:
    def test_strategy_and_transport_fields_survive_a_simulated_restart(self, tmp_path):
        """Nothing but the file on disk survives a restart: a fresh
        `read()` call (no in-memory state) must reproduce the same
        strategy/transport split a live process just wrote."""
        from s6_live import active_watch

        active_watch.merge(DAY, [{
            "symbol": "NVDA", "strategy_source": active_watch.PROVISIONAL_SOURCE,
            "transport_source": active_watch.TRANSPORT_REST,
            "scan_id": CURRENT_RUN, "provisional": True,
            "final_generation_published": False,
        }], session=SESSION, now=NOW, env=_env(tmp_path))

        restored = active_watch.read(DAY, session=SESSION, now=NOW, env=_env(tmp_path))
        row = next(r for r in restored["entries"] if r["symbol"] == "NVDA")
        assert row["strategy_source"] == active_watch.PROVISIONAL_SOURCE
        assert row["transport_source"] == active_watch.TRANSPORT_REST
        assert row["scan_id"] == CURRENT_RUN
        assert row["provisional"] is True
