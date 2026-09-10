from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd


NOW = datetime(2026, 9, 8, 8, 20, tzinfo=timezone.utc)  # 04:20 ET
DAY = "2026-09-08"


def _env(tmp_path):
    return {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}


class TestActiveWatchStore:
    def test_add_update_dedupe_and_restart_persistence(self, tmp_path):
        from s6_live import active_watch

        first = active_watch.merge(DAY, [
            {"symbol": "aapl", "source": "KIS_COLLECTOR", "reason": "seed"},
            {"symbol": "AAPL", "source": "KIS_COLLECTOR", "reason": "same"},
        ], now=NOW, env=_env(tmp_path))
        assert len(first["entries"]) == 1
        added = first["entries"][0]["added_at"]
        active_watch.merge(DAY, [{"symbol": "AAPL", "source": "S6_FULL_DISCOVERY",
                                  "reason": "promoted"}],
                           now=NOW + timedelta(minutes=1), env=_env(tmp_path))
        restored = active_watch.read(DAY, now=NOW + timedelta(minutes=2),
                                     env=_env(tmp_path))
        assert restored["status"] == "ACTIVE"
        assert restored["entries"][0]["added_at"] == added
        assert restored["entries"][0]["source"] == "S6_FULL_DISCOVERY"

    def test_bounded_and_trading_day_isolated(self, tmp_path):
        from s6_live import active_watch

        rows = [{"symbol": f"X{i}", "source": "TEST"} for i in range(10)]
        state = active_watch.merge(DAY, rows, max_symbols=3, now=NOW,
                                   env=_env(tmp_path))
        assert len(state["entries"]) == 3
        assert state["dropped"] == 7
        assert active_watch.read("2026-09-09", now=NOW,
                                 env=_env(tmp_path))["status"] == "MISSING"

    def test_session_expiry_fails_closed(self, tmp_path):
        from s6_live import active_watch

        active_watch.merge(DAY, [{"symbol": "AAPL", "source": "TEST"}],
                           now=NOW, env=_env(tmp_path))
        after_open = datetime(2026, 9, 8, 13, 31, tzinfo=timezone.utc)
        assert active_watch.read(DAY, now=after_open,
                                 env=_env(tmp_path))["status"] == "EXPIRED"


class TestOfficialOriginAndClosedBars:
    def frame(self, start, periods=10):
        index = pd.date_range(start=start, periods=periods, freq="1min",
                              tz="America/New_York")
        return pd.DataFrame({"Open": [10] * periods, "High": [11] * periods,
                             "Low": [9] * periods, "Close": [10] * periods,
                             "Volume": [100] * periods}, index=index)

    def test_truncated_history_cannot_shift_orb_start(self):
        from scanners.base import session_range

        result = session_range.opening_range(
            self.frame("2026-09-08 06:00"), "PREMARKET", minutes=5,
            session_date=datetime(2026, 9, 8).date(),
            require_official_origin=True)
        assert result.complete is False
        assert result.origin_status == "OFFICIAL_ORIGIN_NOT_COVERED"

    def test_official_origin_is_persisted(self):
        from scanners.base import session_range

        result = session_range.opening_range(
            self.frame("2026-09-08 04:00"), "PREMARKET", minutes=5,
            session_date=datetime(2026, 9, 8).date(),
            require_official_origin=True)
        assert result.complete is True
        assert result.official_origin.hour == 4
        assert result.origin_covered is True

    def test_current_minute_is_not_a_closed_bar(self):
        from scanners.base import session_range

        frame = self.frame("2026-09-08 04:18", periods=3)
        closed = session_range.closed_bars(frame, now=NOW)
        assert [stamp.minute for stamp in closed.index] == [18, 19]

    def test_closed_bar_vwap_excludes_forming_minute_exactly(self):
        from market_data import realtime_bars
        from s6_live import kis_bar_features

        store = realtime_bars.RealtimeBarStore(stale_after_seconds=300)
        store.coverage_started_at = NOW.replace(minute=0)
        accumulator = realtime_bars.SessionAccumulator("AAPL", "PREMARKET")
        accumulator.add(price=10, size=1, at=NOW.replace(minute=0, second=5))
        accumulator.add(price=20, size=3, at=NOW.replace(minute=0, second=15))
        accumulator.add(price=100, size=10, at=NOW.replace(minute=2, second=5))
        store._accumulators[("AAPL", "PREMARKET")] = accumulator
        features = kis_bar_features.build_from_bars(
            "AAPL", store=store, session="PREMARKET",
            now=NOW.replace(minute=2, second=30), range_minutes=5,
            closed_bar_only=True)
        assert features.vwap == 17.5
        assert features.price == 20


class TestActiveWatchSource:
    def test_only_ready_active_symbols_are_offered(self, monkeypatch):
        from s6_live import fast_watch, precision_watch, realtime_features

        rollout = type("Rollout", (), {"allowed_symbols": frozenset()})()
        source = fast_watch.ActiveWatchSource(
            trading_day=DAY, session="PREMARKET", rollout=rollout, now=NOW,
            budget_seconds=10)
        source._state = {"status": "ACTIVE", "capacity": 41, "entries": [
            {"symbol": "AAPL", "added_at": NOW.isoformat(),
             "discovery_generation": "g1"},
            {"symbol": "MSFT", "added_at": NOW.isoformat()},
        ]}
        feats = realtime_features.SessionFeatures(
            symbol="AAPL", session="PREMARKET", market_data_asof=NOW - timedelta(minutes=1),
            price=11, vwap=10, ema9=10.5, ema21=10.2, volume=1000,
            volume_status=realtime_features.VOLUME_OK, volume_expansion=2,
            scanner_volume_expansion=2, range_high=10, range_low=9,
            extension_pct=1, range_minutes=5, closed_bar_only=True)

        def evaluate(symbol, **kwargs):
            return precision_watch.WatchEvaluation(
                symbol=symbol, session="PREMARKET",
                state=(precision_watch.READY_TO_BUY if symbol == "AAPL"
                       else precision_watch.WATCHING),
                conditions={name: precision_watch.PASS
                            for name in precision_watch.CONDITION_ORDER},
                features=feats, evaluated_at=NOW)

        monkeypatch.setattr(precision_watch, "evaluate", evaluate)
        assert source.symbols() == ["AAPL"]
        assert source.candidate_row("AAPL")["scanner_variant"] == "S6_ORB5"
        assert source.candidate_row("MSFT") is None

    def test_the_fast_watch_branch_does_not_invoke_s1_to_s5(self):
        from pathlib import Path
        source = Path("scripts/run_live_buy_entry.py").read_text(encoding="utf-8")
        marker = "if session in s6_sessions.SCAN_SESSIONS:"
        branch = source[source.index(marker):source.index(
            "source = S6CandidateSource", source.index(marker))]
        for scanner in ("hma_early_trend", "accumulation", "breakout_ready",
                        "premarket_momentum", "gap_pullback"):
            assert scanner not in branch


def test_rvol_half_is_observation_only():
    from scanners.base import config
    cfg = config.load_config("orb", scanner_name="orb")
    assert cfg.get("entry_quality")["PREMARKET"]["min_rvol_5m"] is None


def test_latency_derivation_keeps_missing_unknown():
    from s6_live import latency
    row = {"candidate_discovered_at": NOW.isoformat(),
           "candidate_published_at": (NOW + timedelta(seconds=4)).isoformat(),
           "source_consumed_at": (NOW + timedelta(seconds=6)).isoformat()}
    result = latency.derive(row)
    assert result["publication_latency_seconds"] == 4
    assert result["consumer_latency_seconds"] == 2
    assert result["execution_latency_seconds"] is None


def test_migration_27_has_every_required_latency_timestamp():
    from state_store import db, migrations
    conn = db.open_db()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(order_lineage)")}
    finally:
        conn.close()
    # >= , not ==: this asserts migration 27 has been applied, not that
    # it is the last one -- a later migration (schema version 28+)
    # legitimately moves CURRENT_SCHEMA_VERSION past 27 without undoing
    # anything this test checks.
    assert migrations.CURRENT_SCHEMA_VERSION >= 27
    assert {"full_scan_started_at", "symbol_evaluated_at",
            "candidate_discovered_at", "watchlist_added_at",
            "fast_watch_evaluated_at", "candidate_published_at",
            "source_consumed_at", "precision_watch_started_at", "ready_at",
            "execution_gate_at", "broker_submit_at", "fill_at"} <= columns


def test_s1_s6_comparison_is_read_only_and_scoped():
    from scanners.analytics import s6_comparison
    signals = [{"signal_id": "s1-a", "scanner_name": "hma_early_trend",
                "symbol": "AAPL", "timestamp": "2026-09-08T08:10:00+00:00"},
               {"signal_id": "s1-x", "scanner_name": "hma_early_trend",
                "symbol": "MSFT", "timestamp": "2026-09-08T08:11:00+00:00"}]
    report = s6_comparison.build(
        DAY, s6_candidates=[{"symbol": "AAPL", "generated_at":
                             "2026-09-08T08:09:00+00:00"}],
        signals=signals, performance={"s1-a": {"return_30m": -1,
                                                "mfe_30m": .2,
                                                "mae_30m": -1.2}},
        fills=[{"symbol": "AAPL", "entry_time": NOW.isoformat()}])
    s1 = next(r for r in report["rows"] if r["label"] == "S1")
    assert s1["candidate_symbols"] == 1
    assert s1["fill_conversion"] == 1
    assert s1["false_positive_proxy"] == 1
    assert report["routing_effect"] == "NONE_READ_ONLY"


class TestSparseBarsDoNotDistortTimestamps:
    """Premarket bars only exist for minutes that traded.

    The gap between two rows is therefore a liquidity measurement, not the
    width of a bar, and the two must never be substituted for each other.
    """

    def test_published_signal_time_is_the_bar_close_not_the_next_print(self):
        from types import SimpleNamespace

        from s6_live import fast_watch, precision_watch, realtime_features

        bar_open = NOW - timedelta(minutes=1)
        quality = SimpleNamespace(
            first_breakout_at=NOW - timedelta(minutes=6),
            source_timestamp=bar_open,
            # What a real sparse premarket frame infers: the median gap
            # between traded minutes, here 19 minutes.
            bar_interval_minutes=19.0,
            as_record=lambda: {"orb_minutes": 5})
        feats = realtime_features.SessionFeatures(
            symbol="AAPL", session="PREMARKET", market_data_asof=bar_open,
            price=11, vwap=10, ema9=10.5, ema21=10.2, volume=1000,
            volume_status=realtime_features.VOLUME_OK, volume_expansion=2,
            range_high=10, range_low=9, range_minutes=5,
            closed_bar_only=True, entry_quality=quality)
        evaluation = precision_watch.WatchEvaluation(
            symbol="AAPL", session="PREMARKET", state=precision_watch.READY_TO_BUY,
            conditions={}, features=feats, evaluated_at=NOW)
        rollout = type("Rollout", (), {"allowed_symbols": frozenset()})()
        source = fast_watch.ActiveWatchSource(
            trading_day=DAY, session="PREMARKET", rollout=rollout, now=NOW)
        source._state = {"status": "ACTIVE", "entries": []}

        stamp = source._row_from(evaluation)["provenance"]["signal_timestamp"]
        assert stamp == (bar_open + timedelta(minutes=1)).isoformat()
        assert datetime.fromisoformat(stamp) <= NOW

    def test_a_known_bar_width_keeps_bars_the_gap_inference_would_drop(self):
        from scanners.base import session_range as srange

        # 04:00, 04:01 and 04:20 ET traded; nothing else. At 04:21:30 every
        # one of those one-minute bars has closed.
        index = pd.to_datetime(["2026-09-08 04:00", "2026-09-08 04:01",
                                "2026-09-08 04:20"]).tz_localize("America/New_York")
        frame = pd.DataFrame({"Open": [1.0, 1.1, 1.2], "High": [1.0, 1.1, 1.3],
                              "Low": [1.0, 1.1, 1.2], "Close": [1.0, 1.1, 1.25],
                              "Volume": [10, 10, 40]}, index=index)
        moment = datetime(2026, 9, 8, 8, 21, 30, tzinfo=timezone.utc)

        assert len(srange.closed_bars(frame, now=moment, interval_seconds=60)) == 3
        # The gap inference reads the 19-minute hole as the bar width and
        # discards the newest closed bar.
        assert len(srange.closed_bars(frame, now=moment)) == 2


def test_the_rest_fallback_is_bounded_by_the_measured_budget(monkeypatch):
    """A tick evaluates what it can afford and defers the rest.

    The active watch may hold up to 41 symbols; the provider must never be
    asked for all of them because the list happens to be full.
    """
    from s6_live import fast_watch, precision_watch, realtime_features

    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    def build(symbol, **kwargs):
        clock["t"] += 4.0          # one measured REST chart read
        return realtime_features.SessionFeatures(
            symbol=symbol, session="PREMARKET", market_data_asof=NOW,
            range_minutes=5, closed_bar_only=True)

    monkeypatch.setattr(realtime_features, "build", build)
    monkeypatch.setattr(precision_watch, "evaluate", lambda symbol, **kw:
                        precision_watch.WatchEvaluation(
                            symbol=symbol, session="PREMARKET",
                            state=precision_watch.WATCHING, conditions={},
                            evaluated_at=NOW))

    rollout = type("Rollout", (), {"allowed_symbols": frozenset()})()
    source = fast_watch.ActiveWatchSource(
        trading_day=DAY, session="PREMARKET", rollout=rollout, now=NOW,
        budget_seconds=10, env={"S6_ACTIVE_WATCH_DIR": "/nonexistent"})
    source._state = {"status": "ACTIVE", "entries": [
        {"symbol": f"SYM{i}", "added_at": NOW.isoformat()} for i in range(41)]}

    assert source.symbols() == []
    assert source.validation_report["validated"] == 3
    assert len(source.waiting_for_data) == 38


def test_migration_preserves_a_position_and_reopening_is_a_no_op(tmp_path):
    """The new S6 columns are nullable, an existing row stays readable,
    and a second open applies nothing (the ledger, as for every migration
    in this file, is what makes the chain idempotent)."""
    from state_store import db as sdb, migrations
    from s6_live import position_store

    path = tmp_path / "state.db"
    conn = sdb.open_db(str(path))
    try:
        conn.execute(
            "INSERT INTO s6_positions (position_id, strategy_id, symbol, quantity, "
            "entry_price, status, submitted_at, created_at, updated_at) "
            "VALUES ('p1','S6_ORB_BREAKOUT_V1','AAPL',1,10.5,'OPEN',"
            "'2026-09-08T08:00:00+00:00','2026-09-08T08:00:00+00:00',"
            "'2026-09-08T08:00:00+00:00')")
        conn.commit()
        applied = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()

    conn = sdb.open_db(str(path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations"
                            ).fetchone()[0] == applied
        assert conn.execute("SELECT MAX(version) FROM schema_migrations"
                            ).fetchone()[0] == migrations.CURRENT_SCHEMA_VERSION
        row = position_store.load(conn, "p1")
        assert row["symbol"] == "AAPL"
        assert row["scanner_variant"] is None
        assert row["entry_quality_json"] is None
    finally:
        conn.close()


# --- all-session fast-watch expansion ----------------------------------------

SESSIONS = ("OVERNIGHT_DAYTIME", "PREMARKET", "REGULAR", "AFTER_HOURS")

#: The canonical open of each session, and a moment inside it.
SESSION_CASES = {
    "OVERNIGHT_DAYTIME": (20, datetime(2026, 9, 9, 1, 30, tzinfo=timezone.utc)),
    "PREMARKET": (4, datetime(2026, 9, 9, 9, 30, tzinfo=timezone.utc)),
    "REGULAR": (9, datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)),
    "AFTER_HOURS": (16, datetime(2026, 9, 9, 21, 0, tzinfo=timezone.utc)),
}


class TestSessionOrigin:
    def frame(self, start, periods=12):
        index = pd.date_range(start=start, periods=periods, freq="1min",
                              tz="America/New_York")
        return pd.DataFrame({"Open": [10] * periods, "High": [11] * periods,
                             "Low": [9] * periods, "Close": [10] * periods,
                             "Volume": [100] * periods}, index=index)

    def test_each_session_anchors_on_its_own_canonical_open(self):
        from scanners.base import session_range as sr

        expected = {"OVERNIGHT_DAYTIME": 20, "PREMARKET": 4,
                    "REGULAR": 9, "AFTER_HOURS": 16}
        for session, hour in expected.items():
            origin = sr.official_origin(session, datetime(2026, 9, 9).date())
            assert origin.hour == hour, session
        # 09:30, not 09:00 -- the regular open is not a whole hour.
        assert sr.official_origin("REGULAR", datetime(2026, 9, 9).date()).minute == 30

    def test_no_session_borrows_another_sessions_range(self):
        """REGULAR bars must not build an AFTER_HOURS opening range."""
        from scanners.base import session_range as sr

        regular = self.frame("2026-09-09 09:30")
        result = sr.opening_range(regular, "AFTER_HOURS", minutes=5,
                                  session_date=datetime(2026, 9, 9).date(),
                                  require_official_origin=True)
        assert result.complete is False
        assert result.range_high is None

    def test_a_missing_official_origin_fails_closed_in_every_session(self):
        from scanners.base import session_range as sr

        late = {"OVERNIGHT_DAYTIME": "2026-09-09 22:30", "PREMARKET": "2026-09-09 06:00",
                "REGULAR": "2026-09-09 11:00", "AFTER_HOURS": "2026-09-09 18:00"}
        for session, start in late.items():
            result = sr.opening_range(
                self.frame(start), session, minutes=5,
                session_date=datetime(2026, 9, 9).date(),
                require_official_origin=True)
            assert result.origin_status == "OFFICIAL_ORIGIN_NOT_COVERED", session
            assert result.complete is False, session
            assert result.range_high is None and result.range_low is None, session


class TestSessionScopedWatchlist:
    def test_each_session_keeps_its_own_watchlist(self, tmp_path):
        from s6_live import active_watch

        for session in SESSIONS:
            active_watch.merge("2026-09-09", [{"symbol": session[:4], "source": "T"}],
                               session=session, now=SESSION_CASES[session][1],
                               env=_env(tmp_path))
        for session in SESSIONS:
            state = active_watch.read("2026-09-09", session=session,
                                      now=SESSION_CASES[session][1], env=_env(tmp_path))
            assert state["status"] == "ACTIVE", session
            assert [r["symbol"] for r in state["entries"]] == [session[:4]], session

    def test_a_prior_sessions_watchlist_is_never_read_as_this_one(self, tmp_path):
        from s6_live import active_watch

        active_watch.merge("2026-09-09", [{"symbol": "AAPL", "source": "T"}],
                           session="PREMARKET", now=SESSION_CASES["PREMARKET"][1],
                           env=_env(tmp_path))
        # REGULAR asks for its own file, which does not exist. It gets nothing
        # rather than the premarket admissions.
        state = active_watch.read("2026-09-09", session="REGULAR",
                                  now=SESSION_CASES["REGULAR"][1], env=_env(tmp_path))
        assert state["status"] == "MISSING"
        assert state["entries"] == []

    def test_expiry_is_each_sessions_own_close(self, tmp_path):
        from s6_live import active_watch

        closes = {"PREMARKET": 9, "REGULAR": 16, "AFTER_HOURS": 20}
        for session, hour in closes.items():
            state = active_watch.merge(
                "2026-09-09", [{"symbol": "AAPL", "source": "T"}], session=session,
                now=SESSION_CASES[session][1], env=_env(tmp_path))
            expiry = datetime.fromisoformat(state["expires_at"])
            assert expiry.astimezone(ZoneInfo("America/New_York")).hour == hour, session

    def test_the_overnight_watchlist_survives_midnight(self, tmp_path):
        """The scope key is the session's start date, not the trading day.

        OVERNIGHT_DAYTIME opens at 20:00 ET; `us_trading_day` rolls at
        midnight underneath it, and keying on that would restart admission
        halfway through the session.
        """
        from s6_live import active_watch

        before = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)    # 21:00 ET 09-08
        after = datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc)     # 01:00 ET 09-09
        scope_before = active_watch.session_scope("OVERNIGHT_DAYTIME", before)
        scope_after = active_watch.session_scope("OVERNIGHT_DAYTIME", after)
        assert scope_before == scope_after == "2026-09-08"

        active_watch.merge(scope_before, [{"symbol": "AAPL", "source": "T"}],
                           session="OVERNIGHT_DAYTIME", now=before, env=_env(tmp_path))
        state = active_watch.read(scope_after, session="OVERNIGHT_DAYTIME",
                                  now=after, env=_env(tmp_path))
        assert state["status"] == "ACTIVE"
        assert [r["symbol"] for r in state["entries"]] == ["AAPL"]
        # and it stops being current at the session's own 04:00 close
        assert datetime.fromisoformat(state["expires_at"]) == datetime(
            2026, 9, 9, 8, 0, tzinfo=timezone.utc)

    def test_the_physical_cap_is_the_measured_provider_limit_in_every_session(self, tmp_path):
        """MAX_SUBSCRIPTIONS is a read-only reference to the physical
        WebSocket ceiling -- unchanged, and no longer what merge()'s own
        cap defaults to."""
        from market_data import kis_hdfscnt0
        from s6_live import active_watch

        assert active_watch.MAX_SUBSCRIPTIONS == kis_hdfscnt0.MAX_SUBSCRIPTIONS == 41
        assert active_watch.MAX_LOGICAL_WATCH_SYMBOLS > active_watch.MAX_SUBSCRIPTIONS

    def test_the_logical_cap_is_decoupled_from_the_physical_one(self, tmp_path):
        """The defect this task exists to fix: merge() used to clamp ANY
        requested capacity down to the 41-symbol WebSocket ceiling, so a
        caller asking for more logical room than that could never get
        it. 60 additions with a requested cap of 99 (both above the
        physical 41) must ALL be admitted in every session."""
        from s6_live import active_watch

        for session in SESSIONS:
            state = active_watch.merge(
                "2026-09-09", [{"symbol": f"S{i}", "source": "T"} for i in range(60)],
                session=session, max_symbols=99, now=SESSION_CASES[session][1],
                env=_env(tmp_path))
            assert len(state["entries"]) == 60, session
            assert state["dropped"] == 0, session

    def test_the_logical_cap_itself_still_bounds_admission(self, tmp_path):
        """Decoupled does not mean unlimited: a request above
        MAX_LOGICAL_WATCH_SYMBOLS is still clamped to it."""
        from s6_live import active_watch

        state = active_watch.merge(
            "2026-09-09",
            [{"symbol": f"S{i}", "source": "T"}
             for i in range(active_watch.MAX_LOGICAL_WATCH_SYMBOLS + 30)],
            session="PREMARKET", max_symbols=10**6,
            now=SESSION_CASES["PREMARKET"][1], env=_env(tmp_path))
        assert len(state["entries"]) == active_watch.MAX_LOGICAL_WATCH_SYMBOLS
        assert state["dropped"] == 30


class TestAllSessionFastWatch:
    def test_the_source_accepts_every_s6_session(self, monkeypatch):
        from config import s6_sessions
        from s6_live import active_watch, fast_watch

        seen = {}

        def refresh(session_date, *, session, trading_day=None, now=None, env=None):
            seen[session] = (session_date, trading_day)
            return {"status": "ACTIVE", "entries": [], "capacity": 41}

        monkeypatch.setattr(active_watch, "refresh_from_existing_sources", refresh)
        monkeypatch.setattr(fast_watch.scanner_live_mode, "require_limited_live",
                            lambda name: None)
        rollout = type("Rollout", (), {"allowed_symbols": frozenset()})()
        for session in SESSIONS:
            source = fast_watch.ActiveWatchSource(
                trading_day="2026-09-09", session=session, rollout=rollout,
                now=SESSION_CASES[session][1])
            assert source._load()["status"] == "ACTIVE", session
            assert source.describe()["scanner_variant"] == "S6_ORB5", session
            assert source.describe()["shadow_variant"] == "S6_ORB15_SHADOW", session
        assert set(seen) == set(SESSIONS)
        assert seen["OVERNIGHT_DAYTIME"][0] == "2026-09-08"   # session start date
        assert seen["OVERNIGHT_DAYTIME"][1] == "2026-09-09"   # trading day

    def test_a_session_s6_does_not_scan_is_refused(self):
        from s6_live import fast_watch

        rollout = type("Rollout", (), {"allowed_symbols": frozenset()})()
        source = fast_watch.ActiveWatchSource(
            trading_day="2026-09-09", session="CLOSED", rollout=rollout, now=NOW)
        assert source._load()["status"] == "WRONG_SESSION"
        assert source.symbols() == []

    def test_one_owner_and_no_extra_process(self):
        """The 1-minute BUY cron is the only caller; nothing else runs it."""
        import subprocess

        hits = subprocess.run(
            ["grep", "-rl", "ActiveWatchSource", "--include=*.py", "--include=*.sh",
             "scripts", "deploy", "s6_live", "kis_live_trading.py"],
            capture_output=True, text=True).stdout.split()
        assert sorted(hits) == ["s6_live/fast_watch.py", "scripts/run_live_buy_entry.py"]

    def test_the_engine_has_no_order_path_in_any_session(self):
        import ast

        for path in ("s6_live/fast_watch.py", "s6_live/active_watch.py",
                     "s6_live/range_shadow.py"):
            tree = ast.parse(open(path).read())
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)
            assert not [m for m in modules
                        if m.split(".")[0] in ("brokers", "execution")], path
            calls = {n.func.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            assert not [c for c in calls if "submit" in c or "cancel" in c], path


class TestClosedBarsInEverySession:
    def test_sparse_extended_hours_gaps_do_not_hide_a_closed_bar(self):
        from scanners.base import session_range as sr

        # An overnight symbol that trades at 20:00, 20:01 and then not again
        # until 21:30. At 21:31:30 all three bars have closed.
        index = pd.to_datetime(["2026-09-08 20:00", "2026-09-08 20:01",
                                "2026-09-08 21:30"]).tz_localize("America/New_York")
        frame = pd.DataFrame({"Open": [1.0, 1.1, 1.2], "High": [1.0, 1.1, 1.3],
                              "Low": [1.0, 1.1, 1.2], "Close": [1.0, 1.1, 1.25],
                              "Volume": [10, 10, 40]}, index=index)
        moment = datetime(2026, 9, 9, 1, 31, 30, tzinfo=timezone.utc)
        assert len(sr.closed_bars(frame, now=moment, interval_seconds=60)) == 3

    def test_the_forming_minute_never_enters_a_range_in_any_session(self):
        from market_data import realtime_bars
        from s6_live import kis_bar_features

        for session, (hour, _) in SESSION_CASES.items():
            eastern = ZoneInfo("America/New_York")
            minute = 30 if session == "REGULAR" else 0
            open_et = datetime(2026, 9, 9, hour, minute, tzinfo=eastern)
            store = realtime_bars.RealtimeBarStore(stale_after_seconds=3600)
            store.coverage_started_at = open_et.astimezone(timezone.utc)
            acc = realtime_bars.SessionAccumulator("AAPL", session)
            for offset, price in ((0, 10), (1, 11), (2, 99)):
                acc.add(price=price, size=10,
                        at=(open_et + timedelta(minutes=offset)).astimezone(timezone.utc))
            store._accumulators[("AAPL", session)] = acc
            now = (open_et + timedelta(minutes=2, seconds=30)).astimezone(timezone.utc)
            feats = kis_bar_features.build_from_bars(
                "AAPL", store=store, session=session, now=now, range_minutes=5,
                closed_bar_only=True)
            # The 99 print belongs to the still-forming minute and is excluded.
            assert feats.price == 11, session
            assert feats.closed_bar_only is True, session
            assert feats.range_minutes == 5, session


class TestShadowInEverySession:
    def test_orb15_is_order_incapable_and_session_scoped(self):
        from config import s6_sessions
        from market_data import realtime_bars
        from s6_live import range_shadow

        for session, (hour, _) in SESSION_CASES.items():
            eastern = ZoneInfo("America/New_York")
            minute = 30 if session == "REGULAR" else 0
            open_et = datetime(2026, 9, 9, hour, minute, tzinfo=eastern)
            store = realtime_bars.RealtimeBarStore(stale_after_seconds=3600)
            store.coverage_started_at = open_et.astimezone(timezone.utc)
            acc = realtime_bars.SessionAccumulator("AAPL", session)
            for offset in range(20):
                acc.add(price=10 + offset * 0.1, size=10,
                        at=(open_et + timedelta(minutes=offset)).astimezone(timezone.utc))
            store._accumulators[("AAPL", session)] = acc
            now = (open_et + timedelta(minutes=21)).astimezone(timezone.utc)
            record = range_shadow.evaluate_symbol(
                "AAPL", store=store, session=session, now=now,
                shadow_minutes=s6_sessions.shadow_orb_minutes_for(session),
                trading_day="2026-09-09")
            assert record["order_capable"] is False, session
            assert record["shadow"] is True, session
            assert record["session"] == session, session
            assert record["scanner_variant"] == "S6_ORB15_SHADOW", session
            assert record["range_minutes"] == 15, session
            assert record["bar_interval_minutes"] == 1.0, session
            assert record["official_session_origin"] is not None, session
            assert record["signal_timestamp"] is not None, session

    def test_shadow_rows_of_one_session_are_not_read_as_another(self):
        from s6_live import range_shadow

        rows = [{"symbol": "AAPL", "session": "REGULAR", "ready": True,
                 "evaluated_at": "2026-09-09T15:00:00+00:00"},
                {"symbol": "MSFT", "session": "AFTER_HOURS", "ready": True,
                 "evaluated_at": "2026-09-09T21:00:00+00:00"}]
        assert set(range_shadow.first_ready(rows, session="REGULAR")) == {"AAPL"}
        assert set(range_shadow.first_ready(rows, session="AFTER_HOURS")) == {"MSFT"}
        assert set(range_shadow.first_ready(rows)) == {"AAPL", "MSFT"}


def test_no_entry_quality_hard_gate_is_armed_in_any_session():
    from scanners.base import config
    from s6_live import entry_quality as eq

    cfg = config.load_config("orb", scanner_name="orb")
    for session in SESSIONS:
        thresholds = eq.thresholds_for(cfg, session)
        assert not [k for k, v in thresholds.items() if v is not None], session
        quality = eq.EntryQuality(symbol="AAPL", session=session,
                                  scanner_variant="S6_ORB5", orb_minutes=5,
                                  rvol_5m=0.01, breakout_age_minutes=999)
        assert eq.assess(quality, thresholds)[0] == "PASS", session


def test_session_readiness_reports_orb5_and_the_shadow_everywhere():
    from operations import session_readiness, slack_presentation

    titles = {"OVERNIGHT_DAYTIME": "데이장", "PREMARKET": "프리장",
              "REGULAR": "정규장", "AFTER_HOURS": "애프터장"}
    for session in SESSIONS:
        status = session_readiness._s6_status(session)
        assert status["fast_watch_active"] is True, session
        assert status["live_variant"] == "S6_ORB5", session
        lines = slack_presentation._s6_status_lines(status)
        assert lines[0].startswith("S6: ORB5"), session
        assert any("빠른 감시" in line for line in lines), session
        assert any("ORB15: 비교 관찰" == line for line in lines), session
        assert slack_presentation.SESSION_TITLES[session] == titles[session]


def test_a_shadow_row_invents_no_signal_time_when_there_is_no_snapshot():
    """A stale or empty view has no decision instant to report.

    `market_data_asof` survives a session roll, so using it as a fallback
    stamped an OVERNIGHT_DAYTIME row with a premarket timestamp.
    """
    from market_data import realtime_bars
    from s6_live import range_shadow

    store = realtime_bars.RealtimeBarStore(stale_after_seconds=60)
    acc = realtime_bars.SessionAccumulator("AAPL", "OVERNIGHT_DAYTIME")
    acc.add(price=10, size=10, at=datetime(2026, 9, 8, 8, 35, tzinfo=timezone.utc))
    store._accumulators[("AAPL", "OVERNIGHT_DAYTIME")] = acc

    record = range_shadow.evaluate_symbol(
        "AAPL", store=store, session="OVERNIGHT_DAYTIME",
        now=datetime(2026, 9, 9, 1, 45, tzinfo=timezone.utc),
        shadow_minutes=15, trading_day="2026-09-08")
    assert record["features_error"]
    assert record["signal_timestamp"] is None
    assert record["theoretical_entry_at"] is None
    assert record["order_capable"] is False
