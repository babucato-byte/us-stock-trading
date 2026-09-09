"""FULL_SCAN_PUBLICATION_BLOCK fix: a scanner PASS admitted to S6
active-watch mid-scan, before the run publishes its manifest -- and only
ever to the WATCHING tier, never to an order.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

NOW = datetime(2026, 9, 9, 8, 2, 12, tzinfo=timezone.utc)
DAY = "2026-09-09"
SESSION = "PREMARKET"


def _env(tmp_path):
    return {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}


class TestRecordAndReadProvisional:
    def test_roundtrip(self, tmp_path):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "race", "trading_day": DAY, "scan_id": "20260909_ADHOC_f58e30",
            "scanner_variant": "S6-P", "full_scan_started_at": "2026-09-09T08:02:12Z",
            "symbol_evaluated_at": "2026-09-09T08:44:49Z",
            "candidate_discovered_at": "2026-09-09T08:44:49Z",
        }, now=NOW, env=_env(tmp_path))
        rows = active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path))
        assert len(rows) == 1
        assert rows[0]["symbol"] == "RACE"  # normalised
        assert rows[0]["provisional"] is True
        assert rows[0]["final_generation_published"] is False
        assert rows[0]["source"] == active_watch.PROVISIONAL_SOURCE

    def test_missing_file_returns_empty(self, tmp_path):
        from s6_live import active_watch

        assert active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path)) == []

    def test_wrong_session_scope_not_visible(self, tmp_path):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, "PREMARKET", {"symbol": "RACE"},
                                              now=NOW, env=_env(tmp_path))
        assert active_watch._read_provisional(DAY, "REGULAR", env=_env(tmp_path)) == []
        assert active_watch._read_provisional("2026-09-08", "PREMARKET", env=_env(tmp_path)) == []

    def test_duplicate_pass_is_idempotent_and_keeps_first_discovery_time(self, tmp_path):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "RACE", "candidate_discovered_at": "2026-09-09T08:44:49Z",
        }, now=NOW, env=_env(tmp_path))
        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "RACE", "candidate_discovered_at": "2026-09-09T08:50:00Z",
        }, now=NOW + timedelta(minutes=6), env=_env(tmp_path))
        rows = active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path))
        assert len(rows) == 1
        assert rows[0]["candidate_discovered_at"] == "2026-09-09T08:44:49Z"

    def test_deterministic_discovery_order(self, tmp_path):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "BBB", "candidate_discovered_at": "2026-09-09T08:10:00Z",
        }, now=NOW, env=_env(tmp_path))
        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "AAA", "candidate_discovered_at": "2026-09-09T08:05:00Z",
        }, now=NOW, env=_env(tmp_path))
        rows = active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path))
        assert [r["symbol"] for r in rows] == ["AAA", "BBB"]

    def test_restart_persistence_reparses_after_fresh_read(self, tmp_path):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {"symbol": "RACE"},
                                              now=NOW, env=_env(tmp_path))
        # Simulate a process restart: nothing but the file on disk survives.
        rows = active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path))
        assert rows and rows[0]["symbol"] == "RACE"

    def test_never_raises_on_corrupted_file(self, tmp_path):
        from s6_live import active_watch

        path = active_watch._provisional_path(DAY, SESSION, env=_env(tmp_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json{{{", encoding="utf-8")
        assert active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path)) == []
        # A write attempt against a corrupted file must also not raise --
        # it detects the invalid scope and starts a fresh, correct file.
        active_watch.record_provisional_pass(DAY, SESSION, {"symbol": "RACE"},
                                              now=NOW, env=_env(tmp_path))
        assert active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path))[0]["symbol"] == "RACE"


class TestProvisionalIdempotencyIsScopedToOneScan:
    """The docstring's own claim ("within one scan") was not actually
    enforced: a later PASS for the same symbol under a DIFFERENT
    (newer) scan_id inherited the OLDER scan's discovery instant,
    because the idempotency check compared only symbols, never scan_id.
    """

    def test_a_new_scan_generation_does_not_inherit_the_old_ones_discovery_time(self, tmp_path):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "META", "scan_id": "old-run",
        }, now=datetime(2026, 9, 9, 10, 23, tzinfo=timezone.utc), env=_env(tmp_path))
        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "META", "scan_id": "new-run",
            "candidate_discovered_at": "2026-09-09T12:02:33.434897+00:00",
        }, now=datetime(2026, 9, 9, 12, 2, 33, tzinfo=timezone.utc), env=_env(tmp_path))
        rows = active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path))
        row = next(r for r in rows if r["symbol"] == "META")
        assert row["scan_id"] == "new-run"
        assert row["candidate_discovered_at"] == "2026-09-09T12:02:33.434897+00:00"

    def test_a_repeated_pass_within_the_same_scan_still_keeps_the_first_time(self, tmp_path):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "META", "scan_id": "run-1",
            "candidate_discovered_at": "2026-09-09T08:44:49Z",
        }, now=NOW, env=_env(tmp_path))
        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "META", "scan_id": "run-1",
            "candidate_discovered_at": "2026-09-09T08:50:00Z",
        }, now=NOW + timedelta(minutes=6), env=_env(tmp_path))
        rows = active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path))
        assert rows[0]["candidate_discovered_at"] == "2026-09-09T08:44:49Z"


class TestRefreshIncorporatesProvisional:
    def _patch_sources(self, monkeypatch, *, subscribed=(), final_rows=(), market_session=SESSION):
        monkeypatch.setattr(
            "market_data.collector_status.describe",
            lambda path=None, *, env=None, now=None, **kw: {
                "subscribed_symbols": list(subscribed), "market_session": market_session,
                "state": "RUNNING", "last_heartbeat_at": None, "subscription_count": len(subscribed),
                "subscription_requested": len(subscribed), "collector_started_at": "2026-09-09T08:00:00Z",
            })
        monkeypatch.setattr("scanners.publish.candidates.read", lambda *a, **k: list(final_rows))
        monkeypatch.setattr("scanners.publish.scan_cycle.state",
                            lambda *a, **k: SimpleNamespace(running=True, run_id="run-1"))

    def test_pass_visible_before_full_publication(self, tmp_path, monkeypatch):
        from s6_live import active_watch

        self._patch_sources(monkeypatch, final_rows=[])  # scan still running: nothing published yet
        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "RACE", "trading_day": DAY, "scan_id": "run-1",
            "candidate_discovered_at": "2026-09-09T08:44:49Z",
        }, now=NOW, env=_env(tmp_path))
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        assert state["status"] == "ACTIVE"
        symbols = [r["symbol"] for r in state["entries"]]
        assert "RACE" in symbols
        race = next(r for r in state["entries"] if r["symbol"] == "RACE")
        assert race["provisional"] is True
        assert race["final_generation_published"] is False

    def test_full_scan_completion_not_required_for_watching(self, tmp_path, monkeypatch):
        # The exact assertion the task calls for: WATCHING must not
        # require the scan to have finished.
        from s6_live import active_watch

        self._patch_sources(monkeypatch, final_rows=[])
        active_watch.record_provisional_pass(DAY, SESSION, {"symbol": "RACE", "scan_id": "run-1"},
                                              now=NOW, env=_env(tmp_path))
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        assert any(r["symbol"] == "RACE" for r in state["entries"])

    def test_final_publication_reconciles_and_supersedes_provisional_content(self, tmp_path, monkeypatch):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "RACE", "candidate_discovered_at": "2026-09-09T08:44:49Z",
        }, now=NOW, env=_env(tmp_path))
        self._patch_sources(monkeypatch, final_rows=[{
            "symbol": "RACE", "strategy_id": "S6_ORB_BREAKOUT_V1",
            "generated_at": "2026-09-09T08:52:11Z", "scanner_run_id": "run-1",
            "provenance": {"candidate_discovered_at": "2026-09-09T08:44:49Z"},
        }])
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        race = next(r for r in state["entries"] if r["symbol"] == "RACE")
        assert race["source"] == "S6_FULL_DISCOVERY"
        assert race["final_generation_published"] is True
        assert race["provisional"] is False

    def test_provisional_never_produces_a_second_row_for_same_symbol(self, tmp_path, monkeypatch):
        from s6_live import active_watch

        active_watch.record_provisional_pass(DAY, SESSION, {"symbol": "RACE"},
                                              now=NOW, env=_env(tmp_path))
        self._patch_sources(monkeypatch, final_rows=[{
            "symbol": "RACE", "strategy_id": "S6_ORB_BREAKOUT_V1",
            "generated_at": "2026-09-09T08:52:11Z", "scanner_run_id": "run-1",
        }])
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        assert [r["symbol"] for r in state["entries"]].count("RACE") == 1

    def test_cap_respected_deterministic_ordering_explicit_drop_count(self, tmp_path, monkeypatch):
        from s6_live import active_watch

        for i in range(5):
            active_watch.record_provisional_pass(DAY, SESSION, {
                "symbol": f"P{i}", "scan_id": "run-1",
                "candidate_discovered_at": f"2026-09-09T08:{10+i:02d}:00Z",
            }, now=NOW, env=_env(tmp_path))
        self._patch_sources(monkeypatch, final_rows=[])
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        # merge()'s own cap machinery -- exercised here through the
        # provisional path, not reimplemented.
        entries = active_watch.merge(
            DAY, [{"symbol": r["symbol"], "source": r["source"]} for r in state["entries"]],
            max_symbols=3, now=NOW, env=_env(tmp_path))
        assert len(entries["entries"]) == 3
        assert entries["dropped"] == 2
        assert [e["symbol"] for e in entries["entries"]] == ["P0", "P1", "P2"]

    def test_no_watching_entry_carries_a_ready_or_order_marker(self, tmp_path, monkeypatch):
        # active_watch admits to WATCHING only -- it must never itself
        # carry any field implying READY/order eligibility.
        from s6_live import active_watch

        self._patch_sources(monkeypatch, final_rows=[])
        active_watch.record_provisional_pass(DAY, SESSION, {"symbol": "RACE", "scan_id": "run-1"},
                                              now=NOW, env=_env(tmp_path))
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        race = next(r for r in state["entries"] if r["symbol"] == "RACE")
        assert "ready" not in {k.lower() for k in race}
        assert "status" not in race or race.get("status") not in ("READY",)
        assert "order" not in {k.lower() for k in race}


class TestAdmitS6PassProvisionallyHook:
    def _signal(self, symbol="RACE"):
        class _Sig:
            pass
        sig = _Sig()
        sig.symbol = symbol
        sig.timestamp = "2026-09-09T08:44:49Z"
        sig.metrics = {"symbol_evaluated_at": "2026-09-09T08:44:49Z"}
        return sig

    def test_admits_for_s6_scan_session(self, tmp_path, monkeypatch):
        from scanners import runner
        from s6_live import active_watch

        monkeypatch.setenv("S6_ACTIVE_WATCH_DIR", str(tmp_path))
        runner._admit_s6_pass_provisionally(
            self._signal(), trading_day=DAY, session=SESSION,
            scan_id="run-1", full_scan_started_at="2026-09-09T08:02:12Z")
        rows = active_watch._read_provisional(DAY, SESSION, env={"S6_ACTIVE_WATCH_DIR": str(tmp_path)})
        assert rows and rows[0]["symbol"] == "RACE"

    def test_non_s6_session_is_a_silent_noop(self, tmp_path, monkeypatch):
        from scanners import runner
        from s6_live import active_watch

        monkeypatch.setenv("S6_ACTIVE_WATCH_DIR", str(tmp_path))
        runner._admit_s6_pass_provisionally(
            self._signal(), trading_day=DAY, session="NOT_A_REAL_SESSION",
            scan_id="run-1", full_scan_started_at="2026-09-09T08:02:12Z")
        assert active_watch._read_provisional(
            DAY, "NOT_A_REAL_SESSION", env={"S6_ACTIVE_WATCH_DIR": str(tmp_path)}) == []

    def test_hook_failure_never_raises(self, tmp_path, monkeypatch):
        from scanners import runner

        monkeypatch.setattr("s6_live.active_watch.record_provisional_pass",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
        monkeypatch.setenv("S6_ACTIVE_WATCH_DIR", str(tmp_path))
        runner._admit_s6_pass_provisionally(  # must not raise
            self._signal(), trading_day=DAY, session=SESSION,
            scan_id="run-1", full_scan_started_at="2026-09-09T08:02:12Z")

    def test_writer_failure_is_logged_not_silent(self, tmp_path, caplog, monkeypatch):
        from s6_live import active_watch

        monkeypatch.setenv("S6_ACTIVE_WATCH_DIR", str(tmp_path / "file"))
        # A file in place of the store directory makes mkdir fail.
        (tmp_path / "file").write_text("not a directory", encoding="utf-8")
        active_watch.record_provisional_pass(DAY, SESSION, {"symbol": "RACE"})
        assert "S6 provisional PASS admission failed" in caplog.text

    def test_no_slack_call_on_provisional_admission(self, tmp_path, monkeypatch):
        # Requirement: no Slack noise for a provisional PASS.
        import slack_utils
        from scanners import runner

        monkeypatch.setenv("S6_ACTIVE_WATCH_DIR", str(tmp_path))
        calls = []
        monkeypatch.setattr(slack_utils, "send_slack_message",
                            lambda *a, **k: calls.append((a, k)), raising=False)
        runner._admit_s6_pass_provisionally(
            self._signal(), trading_day=DAY, session=SESSION,
            scan_id="run-1", full_scan_started_at="2026-09-09T08:02:12Z")
        assert calls == []


class TestRaceRegression:
    """Replays 2026-09-09's real PREMARKET incident: RACE PASSed at
    08:44:49Z but was invisible to active-watch until full publication at
    08:52:11Z -- a ~7.4-minute FULL_SCAN_PUBLICATION_BLOCK. Proves the
    fix makes WATCHING available within the same tick the PASS happens,
    independent of when (or whether) the run completes."""

    def test_race_watching_available_before_full_scan_completion(self, tmp_path, monkeypatch):
        from s6_live import active_watch

        scan_started = datetime(2026, 9, 9, 8, 2, 12, tzinfo=timezone.utc)
        race_pass_at = datetime(2026, 9, 9, 8, 44, 49, tzinfo=timezone.utc)
        full_scan_end = datetime(2026, 9, 9, 8, 52, 11, tzinfo=timezone.utc)

        monkeypatch.setattr(
            "market_data.collector_status.describe",
            lambda path=None, *, env=None, now=None, **kw: {
                "subscribed_symbols": [], "market_session": SESSION, "state": "RUNNING",
                "last_heartbeat_at": None, "subscription_count": 0, "subscription_requested": 0,
                "collector_started_at": scan_started.isoformat(),
            })
        # The scan is STILL RUNNING at this instant -- nothing published yet.
        monkeypatch.setattr("scanners.publish.candidates.read", lambda *a, **k: [])
        monkeypatch.setattr("scanners.publish.scan_cycle.state",
                            lambda *a, **k: SimpleNamespace(
                                running=True, run_id="20260909_ADHOC_f58e30"))

        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "RACE", "trading_day": DAY, "scan_id": "20260909_ADHOC_f58e30",
            "scanner_variant": "S6-P", "full_scan_started_at": scan_started.isoformat(),
            "symbol_evaluated_at": race_pass_at.isoformat(),
            "candidate_discovered_at": race_pass_at.isoformat(),
        }, now=race_pass_at, env=_env(tmp_path))

        # A fast-watch tick one minute after the PASS -- well before the
        # scan (which in reality did not finish for another ~6.5 minutes)
        # completed.
        first_tick_after_pass = race_pass_at + timedelta(minutes=1)
        assert first_tick_after_pass < full_scan_end

        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=first_tick_after_pass, env=_env(tmp_path))
        assert state["status"] == "ACTIVE"
        symbols = [r["symbol"] for r in state["entries"]]
        assert "RACE" in symbols, (
            "RACE must be WATCHING-eligible one minute after its PASS, "
            "not still waiting ~6.5 more minutes for full-scan completion"
        )
        # The important assertion per the task: this does NOT force READY.
        race = next(r for r in state["entries"] if r["symbol"] == "RACE")
        assert race.get("provisional") is True

    def test_latency_improvement_is_on_the_order_of_seconds_not_minutes(self, tmp_path):
        from s6_live import active_watch

        race_pass_at = datetime(2026, 9, 9, 8, 44, 49, tzinfo=timezone.utc)
        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "RACE", "candidate_discovered_at": race_pass_at.isoformat(),
        }, now=race_pass_at, env=_env(tmp_path))
        rows = active_watch._read_provisional(DAY, SESSION, env=_env(tmp_path))
        admitted_at = datetime.fromisoformat(rows[0]["candidate_discovered_at"])
        # OLD behaviour (observed in production): 08:52:11 - 08:44:49 = 442s.
        old_delay_seconds = (datetime(2026, 9, 9, 8, 52, 11, tzinfo=timezone.utc) - race_pass_at).total_seconds()
        new_delay_seconds = (admitted_at - race_pass_at).total_seconds()
        assert old_delay_seconds == 442.0
        assert new_delay_seconds == 0.0
        assert new_delay_seconds < old_delay_seconds


class TestMultiplePassAdmission:
    def test_all_eligible_admitted_up_to_cap_deterministic_no_duplicates(self, tmp_path, monkeypatch):
        from s6_live import active_watch

        for sym, ts in [("AAA", "08:10:00"), ("BBB", "08:20:00"), ("CCC", "08:30:00")]:
            active_watch.record_provisional_pass(DAY, SESSION, {
                "symbol": sym, "scan_id": "run-1",
                "candidate_discovered_at": f"2026-09-09T{ts}Z",
            }, now=NOW, env=_env(tmp_path))
            # A repeated PASS for one of them (idempotency under multiple PASS).
        active_watch.record_provisional_pass(DAY, SESSION, {
            "symbol": "AAA", "scan_id": "run-1",
            "candidate_discovered_at": "2026-09-09T09:00:00Z",
        }, now=NOW, env=_env(tmp_path))

        monkeypatch.setattr(
            "market_data.collector_status.describe",
            lambda path=None, *, env=None, now=None, **kw: {
                "subscribed_symbols": [], "market_session": SESSION, "state": "RUNNING",
                "last_heartbeat_at": None, "subscription_count": 0, "subscription_requested": 0,
                "collector_started_at": None,
            })
        monkeypatch.setattr("scanners.publish.candidates.read", lambda *a, **k: [])
        monkeypatch.setattr("scanners.publish.scan_cycle.state",
                            lambda *a, **k: SimpleNamespace(running=True, run_id="run-1"))

        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        symbols = [r["symbol"] for r in state["entries"]]
        assert symbols == ["AAA", "BBB", "CCC"]
        assert len(symbols) == len(set(symbols))


class TestLiveProvisionalSafety:
    def _sources(self, monkeypatch, *, state):
        monkeypatch.setattr("market_data.collector_status.describe",
                            lambda *a, **k: {"subscribed_symbols": [],
                                             "market_session": SESSION,
                                             "state": "RUNNING",
                                             "subscription_count": 0,
                                             "subscription_requested": 0})
        monkeypatch.setattr("scanners.publish.candidates.read", lambda *a, **k: [])
        monkeypatch.setattr("scanners.publish.scan_cycle.state", lambda *a, **k: state)

    def test_running_scan_does_not_block_its_matching_provisional_source(self, tmp_path, monkeypatch):
        from s6_live import active_watch
        active_watch.record_provisional_pass(DAY, SESSION,
                                             {"symbol": "RACE", "scan_id": "live-1"},
                                             now=NOW, env=_env(tmp_path))
        self._sources(monkeypatch, state=SimpleNamespace(running=True, run_id="live-1"))
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        assert [r["symbol"] for r in state["entries"]] == ["RACE"]

    def test_aborted_scan_provisional_cannot_remain_order_candidate(self, tmp_path, monkeypatch):
        from s6_live import active_watch
        active_watch.record_provisional_pass(DAY, SESSION,
                                             {"symbol": "RACE", "scan_id": "dead-1"},
                                             now=NOW, env=_env(tmp_path))
        self._sources(monkeypatch, state=SimpleNamespace(running=False, run_id=None))
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        assert state["entries"] == []

    def test_new_provisional_cannot_evict_existing_authoritative_watch_entry(self, tmp_path, monkeypatch):
        """The task's §10: a fresh PASS when all 41 WebSocket slots are
        full is admitted to the (decoupled, larger) LOGICAL watchlist as
        a REST-backed entry -- it does not evict an established
        WebSocket-backed entry, and it does not become a 42nd
        subscription (that count is the collector's own read-only fact,
        untouched by this admission)."""
        from s6_live import active_watch
        active_watch.record_provisional_pass(DAY, SESSION,
                                             {"symbol": "NEW", "scan_id": "live-1"},
                                             now=NOW, env=_env(tmp_path))
        self._sources(monkeypatch, state=SimpleNamespace(running=True, run_id="live-1"))
        # All 41 physical WebSocket slots already occupied by established,
        # authoritative (collector-backed) watch entries.
        existing = [{"symbol": f"K{i}", "source": "KIS_COLLECTOR"}
                    for i in range(active_watch.MAX_SUBSCRIPTIONS)]
        active_watch.merge(DAY, existing, session=SESSION, now=NOW, env=_env(tmp_path))
        monkeypatch.setattr(
            "market_data.collector_status.describe",
            lambda *a, **k: {"subscribed_symbols": [f"K{i}" for i in range(active_watch.MAX_SUBSCRIPTIONS)],
                             "market_session": SESSION, "state": "RUNNING",
                             "subscription_count": active_watch.MAX_SUBSCRIPTIONS,
                             "subscription_requested": active_watch.MAX_SUBSCRIPTIONS})
        state = active_watch.refresh_from_existing_sources(
            DAY, session=SESSION, trading_day=DAY, now=NOW, env=_env(tmp_path))
        symbols = [r["symbol"] for r in state["entries"]]
        # The 41 established entries are undisturbed, in their original
        # order -- nothing was evicted.
        assert symbols[:active_watch.MAX_SUBSCRIPTIONS] == \
            [f"K{i}" for i in range(active_watch.MAX_SUBSCRIPTIONS)]
        # NEW is admitted anyway: the logical cap has room even though
        # the physical WebSocket cap does not.
        assert "NEW" in symbols
        assert state["dropped"] == 0
        new_row = next(r for r in state["entries"] if r["symbol"] == "NEW")
        assert new_row["transport_source"] == active_watch.TRANSPORT_REST
        assert new_row["strategy_source"] == active_watch.PROVISIONAL_SOURCE
        # The collector's own subscription count -- the actual physical
        # fact -- is exactly what it was: admitting NEW logically cannot
        # and did not request a 42nd WebSocket subscription.
        assert state["metadata"]["subscription_count"] == active_watch.MAX_SUBSCRIPTIONS
        assert state["websocket_backed"] == active_watch.MAX_SUBSCRIPTIONS
        assert state["rest_backed"] == 1
