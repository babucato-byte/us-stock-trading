"""ORB5 live / ORB15 shadow for PREMARKET, and the entry-quality gate.

Pinned here
-----------
* the 5-minute opening range, the exact first-breakout timestamp, the
  breakout age and the session-high age, all from bars up to the
  decision instant only;
* recent volume over 5/10/15/30 minutes, the time-bucket RVOL with a
  fake prior-session loader, UNAVAILABLE when history is short, the
  5m/15m decay indicator, and provider provenance on every snapshot;
* the ORB15 shadow: it evaluates, it persists, it cannot order, and it
  shares no mutable state with the live ORB5 view;
* the gate: a configured threshold blocks in the precision watch (before
  any broker code runs) with an S6 reason code, an unconfigured gate
  changes nothing, a missing measurement refuses rather than passes;
* Slack: Korean labels for the S6 codes, the code preserved, the block
  routed to stock-live-trading once per symbol/reason/day, the shadow
  producing no live-trading message, and a failing notifier changing
  nothing;
* reporting: shadow rows are never counted as fills;
* S1-S5 untouched: none of their scanners reference entry_quality.
"""

import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_sessions  # noqa: E402
from operations import live_notifications as ln  # noqa: E402
from operations import slack_presentation as sp  # noqa: E402
from s6_live import entry_quality as eq  # noqa: E402

T0 = datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc)   # 04:00 ET


def _bars(closes, volumes=None, *, start=T0, highs=None, lows=None):
    out = []
    for i, close in enumerate(closes):
        vol = volumes[i] if volumes else 1000.0
        hi = highs[i] if highs else close + 0.05
        lo = lows[i] if lows else close - 0.05
        out.append(eq.SimpleBar(minute=start + timedelta(minutes=i), open=close,
                                high=hi, low=lo, close=close, volume=vol))
    return out


class TestOpeningRangeAndBreakout:
    def test_the_five_minute_range_is_the_first_five_bars(self):
        bars = _bars([10, 10.2, 10.1, 10.3, 10.0, 10.5, 10.6, 10.7, 10.8])
        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5, now=bars[-1].minute)
        assert q.or_high == pytest.approx(10.35)
        assert q.or_low == pytest.approx(9.95)
        assert q.range_end == bars[4].minute

    def test_the_first_breakout_is_the_first_post_range_close_above_the_high(self):
        closes = [10, 10.2, 10.1, 10.3, 10.0, 10.2, 10.6, 10.7, 10.8, 10.75]
        bars = _bars(closes)
        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5, now=bars[-1].minute)
        assert q.first_breakout_at == bars[6].minute
        assert q.breakout_age_minutes == pytest.approx(3.0)
        assert q.breakout_age_bars == 3

    def test_no_signal_before_the_range_completes(self):
        bars = _bars([10, 10.2, 10.1])
        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5, now=bars[-1].minute)
        assert q.first_breakout_at is None
        assert "first_breakout_at" in q.unavailable

    def test_session_high_age_measures_the_last_new_high(self):
        closes = [10, 10.2, 10.1, 10.3, 10.0, 10.6, 10.9, 10.7, 10.6, 10.5]
        bars = _bars(closes)
        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5, now=bars[-1].minute)
        assert q.post_range_high == pytest.approx(10.95)
        assert q.last_session_high_at == bars[6].minute
        assert q.minutes_since_session_high == pytest.approx(3.0)

    def test_future_bars_are_never_used(self):
        closes = [10, 10.2, 10.1, 10.3, 10.0, 10.6, 10.9, 12.0]
        bars = _bars(closes)
        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5, now=bars[6].minute)
        assert q.bar_count == 7
        assert q.current_price == pytest.approx(10.9)


class TestRecentVolume:
    def test_windows_sum_the_last_n_minutes(self):
        volumes = [100] * 5 + [50] * 25 + [200] * 5
        bars = _bars([10 + i * 0.01 for i in range(35)], volumes)
        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5, now=bars[-1].minute)
        assert q.recent_volume_5m == 1000
        assert q.recent_volume_10m == 1250
        assert q.recent_volume_15m == 1500
        assert q.recent_volume_30m == 2250
        # per-minute pace vs the opening range's 100/min
        assert q.rvol_5m == pytest.approx(2.0)
        assert q.rvol_15m == pytest.approx(1.0)
        assert q.volume_ratio_5m_15m == pytest.approx(2.0)
        assert q.dollar_volume_5m == pytest.approx(sum(b.close * 200 for b in bars[-5:]))

    def test_decay_is_flagged_when_the_last_five_minutes_fall_below_the_fifteen(self):
        volumes = [100] * 5 + [300] * 10 + [60] * 5
        bars = _bars([10 + i * 0.01 for i in range(20)], volumes)
        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5, now=bars[-1].minute)
        assert q.volume_ratio_5m_15m < 1.0
        assert q.volume_slope is not None and q.volume_slope < 0
        assert q.volume_decay is True

    def test_time_bucket_rvol_uses_the_same_clock_window_on_prior_sessions(self):
        volumes = [100] * 20
        bars = _bars([10 + i * 0.01 for i in range(20)], volumes)

        def baseline(*, symbol, session, window_end, window_minutes):
            assert session == "PREMARKET"
            return 50.0 * window_minutes, 4, "OK"   # prior sessions: 50/min

        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5,
                       now=bars[-1].minute, baseline=baseline)
        assert q.rvol_tb_5m == pytest.approx(2.0)
        assert q.rvol_tb_15m == pytest.approx(2.0)
        assert q.rvol_tb_baseline_days == 4 and q.rvol_tb_status == "OK"

    def test_missing_history_is_recorded_not_invented(self):
        bars = _bars([10 + i * 0.01 for i in range(20)])

        def baseline(**_kw):
            return None, 1, "INSUFFICIENT_HISTORY"

        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5,
                       now=bars[-1].minute, baseline=baseline)
        assert q.rvol_tb_5m is None
        assert q.rvol_tb_status == "INSUFFICIENT_HISTORY"
        assert q.unavailable["rvol_tb_5m"] == "INSUFFICIENT_HISTORY"

    def test_the_baseline_loader_counts_only_days_that_held_the_symbol(self):
        class _Store:
            def __init__(self, bars):
                self._bars = bars

            def bars(self, symbol, session):
                return self._bars

        class _Bar:
            def __init__(self, minute, volume):
                self.minute, self.volume = minute, volume

        # three prior days with 10/min in the window, one day without the symbol
        def loader(session, day):
            if day.endswith("03"):
                return None
            return _Store([_Bar(T0.replace(day=int(day[-2:])) + timedelta(minutes=m), 10.0)
                           for m in range(30)])
        value, days, status = eq.time_bucket_baseline(
            "X", "PREMARKET", window_end=T0 + timedelta(minutes=20), window_minutes=5,
            lookback_days=5, min_days=3, store_loader=loader)
        assert status == "OK" and days >= 3
        assert value == pytest.approx(50.0)

    def test_provenance_travels_with_the_snapshot(self):
        bars = _bars([10 + i * 0.01 for i in range(8)])
        q = eq.compute(bars, symbol="X", session="PREMARKET", orb_minutes=5,
                       now=bars[-1].minute + timedelta(seconds=30), provider="KIS_STREAM",
                       scanner_variant="S6_ORB5")
        assert q.provider == "KIS_STREAM"
        assert q.source_timestamp == bars[-1].minute
        assert q.data_age_seconds == pytest.approx(30.0)
        assert q.bar_interval_minutes == 1.0
        assert q.scanner_variant == "S6_ORB5"
        record = q.as_record()
        assert record["provider"] == "KIS_STREAM" and isinstance(record["source_timestamp"], str)

    def test_five_minute_bars_are_measured_as_five_minute_bars(self):
        bars = _bars([10 + i * 0.01 for i in range(8)], start=T0)
        bars = [eq.SimpleBar(minute=T0 + timedelta(minutes=5 * i), open=b.open, high=b.high,
                             low=b.low, close=b.close, volume=b.volume) for i, b in enumerate(bars)]
        q = eq.compute(bars, symbol="X", session="REGULAR", orb_minutes=15, now=bars[-1].minute)
        assert q.bar_interval_minutes == 5.0


class TestTheGate:
    def _quality(self, **over):
        base = dict(symbol="X", session="PREMARKET", scanner_variant="S6_ORB5", orb_minutes=5,
                    breakout_age_minutes=30.0, minutes_since_session_high=12.0, rvol_5m=0.7,
                    rvol_15m=1.3, volume_ratio_5m_15m=0.55, return_5m=-0.4)
        base.update(over)
        return eq.EntryQuality(**base)

    def test_nothing_configured_passes_and_changes_nothing(self):
        verdict, code, _ = eq.assess(self._quality(), {})
        assert verdict == eq.PASS and code is None
        verdict, code, _ = eq.assess(self._quality(), {"max_breakout_age_minutes": None})
        assert verdict == eq.PASS and code is None

    def test_the_first_failing_dimension_names_the_reason(self):
        verdict, code, detail = eq.assess(self._quality(), {"max_breakout_age_minutes": 20,
                                                            "min_rvol_5m": 1.0})
        assert verdict == eq.FAIL and code == eq.S6_BREAKOUT_STALE
        assert detail["failed"] == "max_breakout_age_minutes"
        verdict, code, _ = eq.assess(self._quality(breakout_age_minutes=5.0),
                                     {"max_breakout_age_minutes": 20, "min_volume_ratio_5m_15m": 0.8})
        assert code == eq.S6_VOLUME_DECAY

    def test_a_missing_measurement_refuses_rather_than_passes(self):
        verdict, code, detail = eq.assess(self._quality(rvol_5m=None), {"min_rvol_5m": 1.0})
        assert verdict == eq.UNAVAILABLE and code == eq.S6_QUALITY_UNAVAILABLE
        assert detail["missing"] == "rvol_5m"

    def test_each_reason_code_has_a_korean_label_and_keeps_its_code(self):
        expected = {
            "S6_BREAKOUT_STALE": "돌파 후 시간이 너무 경과함",
            "S6_RECENT_VOLUME_WEAK": "최근 거래량이 진입 기준에 미달",
            "S6_VOLUME_DECAY": "최근 거래량이 빠르게 감소함",
            "S6_SESSION_HIGH_STALE": "최근 고점 갱신이 오래됨",
            "S6_MOMENTUM_WEAKENING": "최근 상승 모멘텀이 약화됨",
            "S6_PREMARKET_LIQUIDITY_WEAK": "프리장 유동성이 부족함",
        }
        for code, korean in expected.items():
            assert sp.reason_label(code) == (korean, code)
        for code in eq.REASON_CODES:
            assert sp.reason_label(code)[1] == code


class TestPrecisionWatchIntegration:
    """The gate lives inside the precision watch, ahead of every broker
    call, and an unconfigured gate leaves the watch's answer unchanged."""

    def _features(self, quality, *, session="PREMARKET"):
        from s6_live import realtime_features as rf

        return rf.SessionFeatures(
            symbol="X", session=session, market_data_asof=T0 + timedelta(minutes=20),
            built_at=T0 + timedelta(minutes=20), price=10.9, vwap=10.5, ema9=10.8, ema21=10.6,
            volume=5000.0, volume_status=rf.VOLUME_OK, volume_expansion=2.0,
            range_high=10.35, range_low=9.95, extension_pct=5.3, bar_count=20,
            range_minutes=5, entry_quality=quality)

    class _Config:
        def __init__(self, quality):
            self._q = quality

        def require_int(self, key):
            return {"orb_minutes": 15}[key]

        def require_bool(self, key):
            return True

        def require_float(self, key):
            return {"volume_expansion_min": 1.2, "max_extension_above_or_high_pct": 6.0}[key]

        def get(self, key, default=None):
            return {"orb_minutes_by_session": {"PREMARKET": 5},
                    "supported_orb_minutes": [5, 15, 30],
                    "entry_quality": {"PREMARKET": self._q, "default": {}}}.get(key, default)

    def test_unconfigured_gate_leaves_a_ready_candidate_ready(self):
        from s6_live import precision_watch as pw

        q = eq.EntryQuality(symbol="X", session="PREMARKET", scanner_variant="S6_ORB5",
                            orb_minutes=5, breakout_age_minutes=45.0)
        out = pw.evaluate("X", session="PREMARKET", now=T0 + timedelta(minutes=20),
                          features=self._features(q), config=self._Config({}))
        assert out.ready, out.blocking
        assert out.conditions[pw.C_ENTRY_QUALITY] == pw.PASS
        assert out.detail["scanner_variant"] == "S6_ORB5"

    def test_a_configured_gate_blocks_with_the_s6_code_before_any_broker_code(self):
        from s6_live import precision_watch as pw

        q = eq.EntryQuality(symbol="X", session="PREMARKET", scanner_variant="S6_ORB5",
                            orb_minutes=5, breakout_age_minutes=45.0)
        out = pw.evaluate("X", session="PREMARKET", now=T0 + timedelta(minutes=20),
                          features=self._features(q),
                          config=self._Config({"max_breakout_age_minutes": 20}))
        assert not out.ready
        assert out.blocking == [pw.C_ENTRY_QUALITY]
        assert out.detail["entry_quality_reason"] == eq.S6_BREAKOUT_STALE
        assert out.state == "INVALIDATED", "a stale breakout only gets older"

    def test_a_recoverable_quality_failure_keeps_watching(self):
        from s6_live import precision_watch as pw

        q = eq.EntryQuality(symbol="X", session="PREMARKET", scanner_variant="S6_ORB5",
                            orb_minutes=5, breakout_age_minutes=3.0, rvol_5m=0.4)
        out = pw.evaluate("X", session="PREMARKET", now=T0 + timedelta(minutes=20),
                          features=self._features(q),
                          config=self._Config({"min_rvol_5m": 1.0}))
        assert out.state == "WATCHING"
        assert out.detail["entry_quality_reason"] == eq.S6_RECENT_VOLUME_WEAK

    def test_the_watch_builds_the_premarket_view_at_five_minutes(self, monkeypatch):
        from s6_live import precision_watch as pw
        from s6_live import realtime_features as rf

        seen = {}

        def fake_build(symbol, *, session, now, provider, range_minutes):
            seen["range_minutes"] = range_minutes
            return self._features(None, session=session)

        monkeypatch.setattr(rf, "build", fake_build)
        pw.evaluate("X", session="PREMARKET", now=T0, config=self._Config({}))
        assert seen["range_minutes"] == 5
        pw.evaluate("X", session="REGULAR", now=T0, config=self._Config({}))
        assert seen["range_minutes"] == 15

    def test_the_precision_watch_imports_no_execution_code(self):
        source = (REPO_ROOT / "s6_live" / "precision_watch.py").read_text()
        assert "execution_engine" not in source
        assert "submit_" not in source


class TestSessionRouting:
    def test_every_live_session_runs_orb5_with_an_orb15_shadow(self):
        """The all-session policy: no live S6 session is left on ORB15."""
        for session in ("OVERNIGHT_DAYTIME", "PREMARKET", "REGULAR", "AFTER_HOURS"):
            assert s6_sessions.orb_minutes_for(session) == 5, session
            assert s6_sessions.scanner_variant_for(session) == "S6_ORB5", session
            assert s6_sessions.shadow_orb_minutes_for(session) == 15, session
        # The unscoped default is untouched: the override is what moves.
        assert s6_sessions.SCAN_SESSIONS == {"OVERNIGHT_DAYTIME", "PREMARKET",
                                             "REGULAR", "AFTER_HOURS"}

    def test_the_scanner_uses_the_session_range_and_labels_the_variant(self):
        from scanners.orb.scanner import OpeningRangeBreakoutScanner as ORBScanner
        from scanners.base import config as scanner_config

        scanner = ORBScanner(scanner_config.load_config("orb", scanner_name="orb"))
        for session in ("OVERNIGHT_DAYTIME", "PREMARKET", "REGULAR", "AFTER_HOURS"):
            assert scanner.orb_minutes(session) == 5, session
        # A session with no override still reads the global default.
        assert scanner.orb_minutes() == 15
        assert scanner.orb_minutes("UNKNOWN_SESSION") == 15

    def test_an_unsupported_override_is_refused(self):
        class _Cfg:
            def require_int(self, key):
                return 15

            def get(self, key, default=None):
                return {"orb_minutes_by_session": {"PREMARKET": 7},
                        "supported_orb_minutes": [5, 15, 30]}.get(key, default)
        with pytest.raises(ValueError):
            s6_sessions.orb_minutes_for("PREMARKET", config=_Cfg())

    def test_published_rows_carry_the_scanner_variant(self):
        from scanners.publish import candidates

        class _Signal:
            symbol = "X"
            scanner_score = 50.0
            signal_price = 10.0
            scanner_name = "orb"
            scanner_version = "orb_v1.0"
            signal_id = "sig-1"
            metrics = {"orb_minutes": 5, "scanner_variant": "S6_ORB5",
                       "opening_range_high": 10.3, "opening_range_low": 9.9}
        rows = candidates.build_rows([_Signal()], strategy_id="S6_ORB_BREAKOUT_V1",
                                     trading_day="2026-09-08", session="PREMARKET",
                                     variant="S6-P")
        assert rows[0].scanner_variant == "S6_ORB5"
        assert rows[0].range_minutes == 5
        assert rows[0].variant == "S6-P"


class TestOrb15Shadow:
    def _store(self):
        """A read-only stand-in for the collector store: 25 rising minute
        bars for X, a LIVE feed, no gaps."""
        from market_data import realtime_bars as rb

        bars = []
        price = 10.0
        for i in range(25):
            price += 0.03
            minute = T0 + timedelta(minutes=i)
            bars.append(rb.Bar(symbol="X", session="PREMARKET", minute=minute,
                               open=price - 0.01, high=price + 0.02, low=price - 0.03,
                               close=price, volume=200.0, trade_count=4,
                               first_trade_at=minute + timedelta(seconds=5),
                               last_trade_at=minute + timedelta(seconds=50)))

        class _Accumulator:
            volume = 200.0 * 25
            trade_count = 100

            @property
            def vwap(self):
                return sum(b.close * b.volume for b in bars) / self.volume

            def volume_cross_check(self):
                return {"agrees": True}

        class _Store:
            gaps = []

            def bars(self, symbol, session):
                return list(bars) if symbol == "X" else []

            def accumulator(self, symbol, session):
                return _Accumulator() if symbol == "X" else None

            def feed_status(self, *, now=None):
                return "LIVE"

        return _Store()

    def test_it_evaluates_and_persists_without_ordering(self, tmp_path, monkeypatch):
        from s6_live import range_shadow

        store = self._store()
        now = T0 + timedelta(minutes=25)
        record = range_shadow.evaluate_symbol("X", store=store, session="PREMARKET", now=now,
                                              shadow_minutes=15)
        assert record is not None
        assert record["scanner_variant"] == "S6_ORB15_SHADOW"
        assert record["range_minutes"] == 15
        assert record["shadow"] is True and record["order_capable"] is False
        assert "state" in record and "blocking" in record
        assert range_shadow.append(record, trading_day="2026-09-08",
                                   env={"RANGE_SHADOW_DIR": str(tmp_path)})
        rows = range_shadow.read("2026-09-08", env={"RANGE_SHADOW_DIR": str(tmp_path)})
        assert rows and rows[0]["symbol"] == "X"

    def test_the_shadow_module_has_no_order_path(self):
        source = (REPO_ROOT / "s6_live" / "range_shadow.py").read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        forbidden = [m for m in imported if m.startswith("execution") or m.startswith("brokers")]
        assert forbidden == []
        assert "submit_" not in source and "notify(" not in source
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert not {"publish", "submit_order", "submit_buy_order", "record_submission"} & calls

    def test_live_and_shadow_views_share_no_mutable_state(self, monkeypatch):
        from s6_live import kis_bar_features

        store = self._store()
        now = T0 + timedelta(minutes=25)
        live = kis_bar_features.build_from_bars("X", store=store, session="PREMARKET", now=now, range_minutes=5)
        shadow = kis_bar_features.build_from_bars("X", store=store, session="PREMARKET", now=now, range_minutes=15)
        assert live.range_minutes == 5 and shadow.range_minutes == 15
        assert live.range_high != shadow.range_high
        assert live.entry_quality is not shadow.entry_quality
        assert live.entry_quality.scanner_variant == "S6_ORB5"
        assert shadow.entry_quality.scanner_variant == "S6_ORB15"
        with pytest.raises(Exception):
            live.range_minutes = 99   # frozen

    def test_record_cycle_is_silent_when_the_live_range_is_already_fifteen(self, tmp_path):
        from s6_live import range_shadow

        class _Source:
            _session = "REGULAR"
            evaluations = {"X": object()}
        assert range_shadow.record_cycle(_Source(), trading_day="2026-09-08", now=T0,
                                         env={"RANGE_SHADOW_DIR": str(tmp_path)}) == 0

    def test_the_shadow_never_reaches_a_live_trading_webhook(self, tmp_path, monkeypatch):
        import slack_utils
        from s6_live import range_shadow

        calls = []
        monkeypatch.setattr(slack_utils, "_send", lambda url, msg: calls.append(url) or True)
        store = self._store()

        class _Source:
            _session = "PREMARKET"
            evaluations = {"X": None}
        written = range_shadow.record_cycle(_Source(), trading_day="2026-09-08",
                                            now=T0 + timedelta(minutes=25),
                                            env={"RANGE_SHADOW_DIR": str(tmp_path)}, store=store)
        assert written == 1
        assert calls == []


class TestSlackForS6Blocks:
    def test_the_block_message_is_compact_and_routed_to_live_trading(self, monkeypatch):
        import slack_utils

        calls = []
        monkeypatch.setattr(slack_utils, "_send", lambda url, msg: calls.append((url, msg)) or True)
        monkeypatch.setenv("KIS_LIVE_SLACK_WEBHOOK_URL", "https://hooks.test/LIVE_TRADING")
        monkeypatch.setenv("KIS_LIVE_SLACK_ALERT_WEBHOOK_URL", "https://hooks.test/LIVE_ALERTS")
        fields = ln.order_blocked_fields(symbol="PLUG", reason_code="S6_VOLUME_DECAY",
                                         strategy_id="S6_ORB_BREAKOUT_V1", session="PREMARKET")
        fields.update({"orb_minutes": 5, "breakout_age_minutes": 18, "minutes_since_session_high": 12,
                       "rvol_5m": 0.72, "rvol_15m": 1.31})
        assert ln.notify(ln.ORDER_BLOCKED, fields, track_health=False) is True
        url, text = calls[0]
        assert url == "https://hooks.test/LIVE_TRADING"
        assert text.splitlines()[0] == "[매수 차단]"
        assert "세션: 프리장 (PREMARKET)" in text and "ORB: 5분" in text
        assert "사유: 최근 거래량이 빠르게 감소함" in text
        assert "원인 코드: S6_VOLUME_DECAY" in text
        assert "돌파 후 경과: 18분" in text and "최근 고점 경과: 12분" in text
        assert "최근 5분 RVOL: 0.72x" in text and "최근 15분 RVOL: 1.31x" in text
        assert "rvol_tb" not in text and "volume_slope" not in text

    def test_the_same_block_is_sent_once_per_symbol_reason_day(self):
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            sent = []
            for _ in range(3):
                ln.notify(ln.ORDER_BLOCKED,
                          ln.order_blocked_fields(symbol="PLUG", reason_code="S6_BREAKOUT_STALE"),
                          send_fn=lambda m: sent.append(m) or True, track_health=False,
                          dedupe_conn=conn)
            assert len(sent) == 1
        finally:
            conn.close()

    def test_the_fill_message_carries_orb_and_compact_context(self):
        text = sp.buy_filled({"symbol": "XYZ", "strategy_id": "S6_ORB_BREAKOUT_V1",
                              "session": "PREMARKET", "filled_qty": 3, "fill_price": 10.0,
                              "orb_minutes": 5, "breakout_age_minutes": 4,
                              "recent_volume_state": "유지"})
        assert "ORB: 5분" in text and "돌파 후 경과: 4분" in text and "최근 거래량: 유지" in text
        assert text.count("[") == 1

    def test_the_entry_runner_announces_only_quality_blocks_and_never_raises(self, monkeypatch):
        from scripts import run_live_buy_entry as runner

        class _Eval:
            def __init__(self, blocking, detail, session="PREMARKET"):
                self.blocking, self.detail, self.session = blocking, detail, session

        class _Source:
            evaluations = {
                "PLUG": _Eval(["ENTRY_QUALITY"], {"entry_quality_reason": "S6_VOLUME_DECAY",
                                                  "range_minutes": 5,
                                                  "entry_quality": {"rvol_5m": 0.7}}),
                "OK": _Eval([], {}),
                "VWAP": _Eval(["PRICE_ABOVE_VWAP"], {}),
            }
        sent = []
        monkeypatch.setattr(ln, "notify", lambda event, fields=None, **k: sent.append((event, fields)) or True)
        runner._announce_quality_blocks(_Source(), since=T0)
        assert [(e, f["symbol"], f["reason_code"]) for e, f in sent] == [
            (ln.ORDER_BLOCKED, "PLUG", "S6_VOLUME_DECAY")]
        assert sent[0][1]["orb_minutes"] == 5 and sent[0][1]["rvol_5m"] == 0.7

        def boom(*a, **k):
            raise RuntimeError("slack down")
        monkeypatch.setattr(ln, "notify", boom)
        runner._announce_quality_blocks(_Source(), since=T0)   # swallowed

    def test_the_entry_runner_announces_liquidity_and_cash_precheck_blocks(self, monkeypatch):
        from scripts import run_live_buy_entry as runner
        from s6_live import execution_liquidity as el, cash_precheck

        class _Source:
            liquidity_blocked = {
                "RIG": (el.ABSOLUTE_LIQUIDITY_TOO_LOW,
                       {"bar_count": 15, "recent_volume": 3.0, "dollar_volume": 17.37}),
            }
            cash_precheck_blocked = {
                "KVYO": (cash_precheck.INSUFFICIENT_CASH_PRECHECK,
                        {"available_cash": 2.0, "required_for_1_share": 16.44,
                         "shortfall": 14.44}),
            }
        sent = []
        monkeypatch.setattr(ln, "notify", lambda event, fields=None, **k: sent.append((event, fields)) or True)
        runner._announce_liquidity_blocks(_Source())
        assert [(e, f["symbol"], f["reason_code"]) for e, f in sent] == [
            (ln.ORDER_BLOCKED, "RIG", el.ABSOLUTE_LIQUIDITY_TOO_LOW),
            (ln.ORDER_BLOCKED, "KVYO", cash_precheck.INSUFFICIENT_CASH_PRECHECK),
        ]
        assert "17.37" in sent[0][1]["detail"]
        assert "shortfall=14.44" in sent[1][1]["detail"]

        def boom(*a, **k):
            raise RuntimeError("slack down")
        monkeypatch.setattr(ln, "notify", boom)
        runner._announce_liquidity_blocks(_Source())  # swallowed

    def test_no_blocks_sends_nothing(self, monkeypatch):
        from scripts import run_live_buy_entry as runner

        class _Source:
            liquidity_blocked = {}
            cash_precheck_blocked = {}
        sent = []
        monkeypatch.setattr(ln, "notify", lambda *a, **k: sent.append(1) or True)
        runner._announce_liquidity_blocks(_Source())
        assert sent == []


class TestReportingAndPersistence:
    def test_shadow_rows_are_never_counted_as_fills(self):
        from s6_live import entry_outcomes as eo

        bars = [{"minute": (T0 + timedelta(minutes=i)).isoformat(), "high": 10 + i * 0.1,
                 "low": 9.9 + i * 0.1} for i in range(70)]
        rows = eo.build("2026-09-08",
                        fills=[{"symbol": "VG", "entry_time": T0.isoformat(), "entry_price": 10.0,
                                "exit_price": 10.5, "quantity": 9, "closed_at": (T0 + timedelta(minutes=40)).isoformat(),
                                "exit_reason": "RANGE_REENTRY", "range_minutes": 5,
                                "position_id": "p1", "scanner_variant": "S6_ORB5"}],
                        shadow_ready={"VG": {"evaluated_at": (T0 + timedelta(minutes=10)).isoformat(),
                                             "price": 11.0, "scanner_variant": "S6_ORB15_SHADOW",
                                             "range_minutes": 15}},
                        bars_for=lambda symbol: bars)
        real = [r for r in rows if not r["shadow"]]
        shadow = [r for r in rows if r["shadow"]]
        assert len(real) == 1 and len(shadow) == 1
        assert real[0]["realized_return_pct"] == pytest.approx(5.0)
        assert real[0]["holding_minutes"] == pytest.approx(40.0)
        assert real[0]["mfe_15m"] == pytest.approx((11.5 / 10.0 - 1.0) * 100.0)
        assert shadow[0]["realized_pnl"] is None and shadow[0]["order_capable"] is False
        summary = eo.summarise(rows)
        # Buckets are per (session, variant) so four sessions never collapse
        # into one number, and a shadow row can never land in a fill bucket.
        assert summary["PREMARKET|S6_ORB5"]["count"] == 1
        assert summary["PREMARKET|S6_ORB5"]["shadow"] is False
        assert summary["PREMARKET|S6_ORB15_SHADOW"]["shadow"] is True

    def test_the_decision_snapshot_is_written_to_the_position(self, monkeypatch):
        from s6_live import entry_lifecycle, position_store
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            q = eq.EntryQuality(symbol="VG", session="PREMARKET", scanner_variant="S6_ORB5",
                                orb_minutes=5, breakout_age_minutes=4.0, volume_decay=False)

            class _Feats:
                range_minutes = 5
                entry_quality = q

            class _Watch:
                features = _Feats()
                detail = {"scanner_variant": "S6_ORB5"}
                state = "READY_TO_BUY"
                evaluated_at = T0
            monkeypatch.setattr(entry_lifecycle, "_record_lineage", lambda *a, **k: None)
            pid = entry_lifecycle.record_entry_submission(
                conn, symbol="VG", session="PREMARKET", client_order_id="c1",
                candidate_row={"range_minutes": 5, "range_high": 14.73}, watch=_Watch(), now=T0)
            row = position_store.load(conn, pid) if hasattr(position_store, "load") else None
            if row is None:
                row = dict(conn.execute("SELECT scanner_variant, entry_quality_json, range_minutes "
                                        "FROM s6_positions WHERE position_id = ?", (pid,)).fetchone()
                           and dict(zip(("scanner_variant", "entry_quality_json", "range_minutes"),
                                        conn.execute("SELECT scanner_variant, entry_quality_json, range_minutes FROM s6_positions WHERE position_id = ?", (pid,)).fetchone())))
            assert row["scanner_variant"] == "S6_ORB5"
            assert row["range_minutes"] == 5
            snapshot = json.loads(row["entry_quality_json"])
            assert snapshot["breakout_age_minutes"] == 4.0
            assert snapshot["watch_state"] == "READY_TO_BUY"
        finally:
            conn.close()

    def test_migration_26_is_idempotent(self):
        from state_store import migrations
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(s6_positions)")]
            assert "scanner_variant" in cols and "entry_quality_json" in cols
            assert migrations.CURRENT_SCHEMA_VERSION >= 26
        finally:
            conn.close()


class TestOtherScannersUntouched:
    @pytest.mark.parametrize("name", ["hma_early_trend", "accumulation", "breakout_ready",
                                      "premarket_momentum", "gap_pullback"])
    def test_no_s1_to_s5_scanner_references_entry_quality_or_the_range_override(self, name):
        source = (REPO_ROOT / "scanners" / name / "scanner.py").read_text()
        assert "entry_quality" not in source
        assert "orb_minutes" not in source
        config = json.loads((REPO_ROOT / "scanners" / name / "config.json").read_text())
        assert "entry_quality" not in json.dumps(config)

    def test_the_gate_is_reachable_only_through_the_s6_watch(self):
        users = []
        for path in REPO_ROOT.rglob("*.py"):
            if "tests" in path.parts or ".venv" in path.parts or "venv" in path.parts:
                continue
            if "entry_quality" in path.read_text(errors="ignore") and path.name != "entry_quality.py":
                users.append(str(path.relative_to(REPO_ROOT)))
        allowed = {"s6_live/", "scripts/replay_s6_premarket.py", "scripts/run_live_buy_entry.py",
                   "scripts/run_s6_entry_outcomes.py", "operations/", "scanners/publish/",
                   "config/s6_sessions.py", "state_store/"}
        for user in users:
            assert any(user.startswith(prefix) for prefix in allowed), user
