"""S6 P0: execution liquidity gate, sizing cap, and the cash precheck.

Covers `s6_live/execution_liquidity.py`, `s6_live/cash_precheck.py`,
`s6_live/account_cash_cache.py`, the gating wired into
`s6_live/fast_watch.py::ActiveWatchSource.symbols()`, the sizing hook in
`s6_live/buy_intent_source.py::IntentQueueSource.liquidity_max_qty`, and
the same hook consumed generically (S1 unaffected) in
`kis_live_trading.py::run_live_buy_entry_cycle`.
"""

import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from s6_live import account_cash_cache, cash_precheck, execution_liquidity as el
from s6_live.entry_quality import EntryQuality

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _quality(**overrides):
    base = dict(
        symbol="RIG", session="PREMARKET", scanner_variant="S6_ORB5",
        orb_minutes=5, bar_count=10, recent_volume_5m=500.0,
        recent_volume_10m=800.0, recent_volume_15m=1200.0,
        dollar_volume_5m=3000.0, spread_bps=None,
    )
    base.update(overrides)
    return EntryQuality(**base)


class TestAssessLiquidityOk:
    def test_healthy_reading_passes(self):
        verdict, code, detail = el.assess(_quality())
        assert verdict == el.PASS
        assert code is None

    def test_kvyo_style_thin_but_real_reading_passes(self):
        # Production 2026-09-10T11:00-11:13Z: bar_count=9-10,
        # recent_volume_5m=170-270, dollar_volume_5m=$2,794-4,439.
        # KVYO filled and closed without incident -- the defaults must
        # not have blocked it.
        verdict, code, detail = el.assess(_quality(
            bar_count=9, recent_volume_5m=170.0, recent_volume_15m=170.0,
            dollar_volume_5m=2794.1))
        assert verdict == el.PASS


class TestAssessCatchesGenuineCollapse:
    def test_rig_post_entry_collapse_is_blocked(self):
        # Production 2026-09-10T09:13-09:26Z: RIG's own recent volume
        # collapsed to 3 shares / $17.37 over 5 minutes after entry.
        verdict, code, detail = el.assess(_quality(
            bar_count=15, recent_volume_5m=3.0, recent_volume_15m=3103.0,
            dollar_volume_5m=17.37))
        assert verdict == el.FAIL
        # The absolute-share floor is judged before the dollar floor
        # (THRESHOLD order in assess()); 3 shares fails it first.
        assert code == el.ABSOLUTE_LIQUIDITY_TOO_LOW

    def test_absolute_volume_floor_fires_before_dollar_floor(self):
        verdict, code, detail = el.assess(_quality(
            recent_volume_5m=5.0, dollar_volume_5m=500.0))
        assert verdict == el.FAIL
        assert code == el.ABSOLUTE_LIQUIDITY_TOO_LOW

    def test_too_few_bars_is_no_recent_trades(self):
        verdict, code, detail = el.assess(_quality(bar_count=1))
        assert verdict == el.FAIL
        assert code == el.NO_RECENT_TRADES


class TestAssessUnavailable:
    def test_missing_quality_snapshot_fails_open(self):
        # No SessionFeatures ever carried an entry_quality snapshot for
        # this evaluation path -- distinct from a market with no
        # trades. Blocking here would have refused every candidate in
        # `test_only_ready_active_symbols_are_offered`-style fixtures
        # that predate this gate and never set the field.
        verdict, code, detail = el.assess(None)
        assert verdict == el.PASS
        assert code is None

    def test_missing_recent_volume_is_unavailable_not_a_silent_pass(self):
        verdict, code, detail = el.assess(_quality(recent_volume_5m=None))
        assert verdict == el.UNAVAILABLE
        assert code == el.LIQUIDITY_DATA_UNAVAILABLE

    def test_spread_threshold_configured_but_unmeasurable_is_unavailable(self):
        # No bid/ask source exists anywhere in this codebase today; a
        # configured spread threshold must refuse, never silently pass.
        verdict, code, detail = el.assess(
            _quality(spread_bps=None), {**el.DEFAULT_THRESHOLDS, "max_spread_bps": 50.0})
        assert verdict == el.UNAVAILABLE
        assert code == el.LIQUIDITY_DATA_UNAVAILABLE

    def test_spread_disabled_by_default_never_blocks(self):
        assert el.DEFAULT_THRESHOLDS["max_spread_bps"] is None
        verdict, _code, _detail = el.assess(_quality(spread_bps=None))
        assert verdict == el.PASS


class TestProductionRegressions:
    """§10/§11: replay real production `entry_quality` snapshots (shadow-
    signal log, 2026-09-10) at the exact instants strategy judged READY.
    Not hardcoded to any one symbol -- `assess()` never sees a symbol
    name, only the measurements.
    """

    def test_rig_at_its_actual_buy_decision_instant_is_not_caught(self):
        # 2026-09-10T08:39:07Z / 08:40:09Z, the two ticks bracketing
        # RIG's real submission (08:40:08): bar_count=9,
        # recent_volume_5m=3,664, dollar_volume_5m=$21,250.05. A single
        # snapshot gate cannot see the collapse that had not happened
        # yet -- see the module docstring's "What this does NOT catch".
        verdict, code, detail = el.assess(_quality(
            bar_count=9, recent_volume_5m=3664.0, recent_volume_15m=5366.0,
            dollar_volume_5m=21250.05))
        assert verdict == el.PASS

    def test_rig_forty_five_minutes_later_is_caught(self):
        # 2026-09-10T09:13-09:26Z, the same position still open:
        # recent_volume_5m collapsed to 3, dollar_volume_5m to $17.37.
        # Had the position needed a SECOND entry at this moment (a
        # scale-in, a re-entry), this blocks it.
        verdict, code, detail = el.assess(_quality(
            bar_count=15, recent_volume_5m=3.0, recent_volume_15m=3103.0,
            dollar_volume_5m=17.37))
        assert verdict == el.FAIL

    def test_kvyo_good_trade_regression(self):
        # 2026-09-10T11:00-11:13Z, KVYO's actual entry window: filled,
        # closed without incident.
        verdict, _code, _detail = el.assess(_quality(
            bar_count=9, recent_volume_5m=170.0, recent_volume_15m=390.0,
            dollar_volume_5m=2794.1))
        assert verdict == el.PASS

    def test_cop_at_its_actual_buy_decision_instant_passes(self):
        # COP's real BUY was submitted 2026-09-10T12:35:11Z, well after
        # its thin 11:14-11:24 ticks -- the shadow log's closest prior
        # reading (11:59:24Z) shows recent_volume_5m=7,857,
        # dollar_volume_5m=$1,082,144.61, comfortably healthy. COP's own
        # earlier thin ticks (rv5 as low as 1, $137.70) correctly fail
        # this gate, but that is not the reading its actual order used.
        verdict, _code, _detail = el.assess(_quality(
            bar_count=34, recent_volume_5m=7857.0, recent_volume_15m=8100.0,
            dollar_volume_5m=1082144.61))
        assert verdict == el.PASS

    def test_cop_an_earlier_thin_tick_correctly_blocks(self):
        # 2026-09-10T11:20Z, COP's thinnest recorded tick this session,
        # well before its actual order: recent_volume_5m=1 share. This
        # SHOULD fail -- it is a genuinely thin reading, not a false
        # positive -- it simply was not the reading COP's real order
        # was ever sized against.
        verdict, code, _detail = el.assess(_quality(
            bar_count=17, recent_volume_5m=1.0, recent_volume_15m=7.0,
            dollar_volume_5m=137.7))
        assert verdict == el.FAIL
        assert code == el.ABSOLUTE_LIQUIDITY_TOO_LOW


class TestThresholdsFor:
    def test_no_config_block_keeps_module_defaults(self):
        thresholds = el.thresholds_for({}, "PREMARKET")
        assert thresholds == el.DEFAULT_THRESHOLDS

    def test_session_override_merges_over_defaults(self):
        config = {"execution_liquidity": {"PREMARKET": {"min_bar_count": 7}}}
        thresholds = el.thresholds_for(config, "PREMARKET")
        assert thresholds["min_bar_count"] == 7
        assert thresholds["min_recent_volume"] == el.DEFAULT_THRESHOLDS["min_recent_volume"]

    def test_default_block_applies_when_session_unset(self):
        config = {"execution_liquidity": {"default": {"min_dollar_volume": 5.0}}}
        thresholds = el.thresholds_for(config, "AFTER_HOURS")
        assert thresholds["min_dollar_volume"] == 5.0


class TestLiquidityCappedQty:
    def test_caps_down_to_fraction_of_recent_volume(self):
        capped, code, detail = el.liquidity_capped_qty(
            _quality(recent_volume_15m=100.0), 50)
        assert code is None
        assert capped == 10  # 10% of 100

    def test_never_raises_above_requested(self):
        capped, code, detail = el.liquidity_capped_qty(
            _quality(recent_volume_15m=100000.0), 5)
        assert code is None
        assert capped == 5

    def test_zero_cap_blocks_outright_not_silently(self):
        capped, code, detail = el.liquidity_capped_qty(
            _quality(recent_volume_15m=2.0), 5)
        assert capped == 0
        assert code == el.ORDER_TOO_LARGE_FOR_LIQUIDITY

    def test_missing_quality_passes_the_cash_based_qty_through_unchanged(self):
        # Fails open like assess() -- no snapshot is not "measured
        # oversized", and cash/risk/broker checks remain the authority.
        capped, code, detail = el.liquidity_capped_qty(None, 5)
        assert capped == 5
        assert code is None

    def test_non_positive_requested_qty_is_a_no_op(self):
        capped, code, detail = el.liquidity_capped_qty(_quality(), 0)
        assert capped == 0
        assert code is None

    def test_fraction_disabled_never_caps(self):
        thresholds = {**el.DEFAULT_THRESHOLDS, "max_qty_fraction_of_recent_volume": None}
        capped, code, detail = el.liquidity_capped_qty(None, 5, thresholds)
        assert capped == 5
        assert code is None


class TestAccountCashCache:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_path):
        self.env = {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}

    def test_write_then_read_within_ttl(self):
        account_cash_cache.write(1234.56, now=NOW, env=self.env)
        cached = account_cash_cache.read(now=NOW + timedelta(seconds=10),
                                         max_age_seconds=90, env=self.env)
        assert cached["available_usd"] == 1234.56

    def test_stale_beyond_max_age_is_a_miss(self):
        account_cash_cache.write(1234.56, now=NOW, env=self.env)
        cached = account_cash_cache.read(now=NOW + timedelta(seconds=200),
                                         max_age_seconds=90, env=self.env)
        assert cached is None

    def test_absent_cache_is_a_miss_not_an_error(self):
        assert account_cash_cache.read(now=NOW, env=self.env) is None

    def test_unconfigured_root_fails_safe(self):
        assert account_cash_cache.read(now=NOW, env={}) is None
        account_cash_cache.write(1.0, now=NOW, env={})  # must not raise


class Broker:
    def __init__(self, cash=None, raises=False):
        self._cash = cash
        self._raises = raises
        self.calls = 0

    def get_account_cash_usd(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError("KIS account read failed")
        return self._cash


class TestCashPrecheck:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_path):
        self.env = {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}

    def test_no_broker_is_unavailable_and_fails_open(self):
        status, detail = cash_precheck.check("RIG", 5.79, broker=None, now=NOW, env=self.env)
        assert status == cash_precheck.UNAVAILABLE

    def test_sufficient_cash_passes(self):
        broker = Broker(cash=1000.0)
        status, detail = cash_precheck.check("RIG", 5.79, broker=broker, now=NOW, env=self.env)
        assert status == cash_precheck.OK
        assert broker.calls == 1

    def test_insufficient_cash_blocks_with_full_breakdown(self):
        broker = Broker(cash=2.00)
        status, detail = cash_precheck.check("RIG", 5.79, broker=broker, now=NOW, env=self.env)
        assert status == cash_precheck.BLOCKED
        assert detail["symbol"] == "RIG"
        assert detail["available_cash"] == 2.00
        assert detail["required_for_1_share"] == 5.79
        assert detail["shortfall"] == pytest.approx(3.79)
        assert "cash_state_timestamp" in detail

    def test_failed_read_is_unavailable_never_a_fabricated_zero(self):
        broker = Broker(raises=True)
        status, detail = cash_precheck.check("RIG", 5.79, broker=broker, now=NOW, env=self.env)
        assert status == cash_precheck.UNAVAILABLE

    def test_second_call_within_ttl_reuses_the_cache(self):
        broker = Broker(cash=1000.0)
        cash_precheck.check("RIG", 5.79, broker=broker, now=NOW, env=self.env)
        cash_precheck.check("KVYO", 16.44, broker=broker,
                            now=NOW + timedelta(seconds=5), env=self.env)
        assert broker.calls == 1

    def test_expired_cache_makes_one_fresh_call(self):
        broker = Broker(cash=1000.0)
        cash_precheck.check("RIG", 5.79, broker=broker, now=NOW, env=self.env)
        cash_precheck.check("KVYO", 16.44, broker=broker,
                            now=NOW + timedelta(seconds=200), env=self.env,
                            max_age_seconds=90)
        assert broker.calls == 2


# -- integration: the gate wired into the live fast-watch tick -------------

def _feats(entry_quality=None, **overrides):
    from s6_live import realtime_features

    base = dict(
        symbol="RIG", session="PREMARKET", market_data_asof=NOW - timedelta(minutes=1),
        price=5.79, vwap=5.77, ema9=5.76, ema21=5.74, volume=1000,
        volume_status=realtime_features.VOLUME_OK, volume_expansion=2,
        scanner_volume_expansion=2, range_high=5.76, range_low=5.71,
        extension_pct=1, range_minutes=5, closed_bar_only=True,
        entry_quality=entry_quality,
    )
    base.update(overrides)
    return realtime_features.SessionFeatures(**base)


def _ready_evaluate(symbol, feats):
    from s6_live import precision_watch

    def evaluate(sym, **kwargs):
        return precision_watch.WatchEvaluation(
            symbol=sym, session="PREMARKET",
            state=(precision_watch.READY_TO_BUY if sym == symbol
                   else precision_watch.WATCHING),
            conditions={name: precision_watch.PASS
                        for name in precision_watch.CONDITION_ORDER},
            features=feats, evaluated_at=NOW)
    return evaluate


class TestActiveWatchSourceLiquidityGate:
    """Strategy PASS, execution FAIL (§4): the precision watch says READY,
    but the symbol never reaches `symbols()`'s return list or the
    BUY_INTENT row."""

    def _source(self, tmp_path, *, broker=None):
        from s6_live import fast_watch

        rollout = type("Rollout", (), {"allowed_symbols": frozenset()})()
        source = fast_watch.ActiveWatchSource(
            trading_day="2026-09-10", session="PREMARKET", rollout=rollout,
            now=NOW, budget_seconds=10, env={"S6_ACTIVE_WATCH_DIR": str(tmp_path)},
            broker=broker)
        source._state = {"status": "ACTIVE", "capacity": 41, "entries": [
            {"symbol": "RIG", "added_at": NOW.isoformat()},
        ]}
        return source

    def test_thin_liquidity_blocks_admission_strategy_still_passed(self, tmp_path, monkeypatch):
        feats = _feats(entry_quality=_quality(
            bar_count=15, recent_volume_5m=3.0, dollar_volume_5m=17.37))
        monkeypatch.setattr("s6_live.precision_watch.evaluate", _ready_evaluate("RIG", feats))
        source = self._source(tmp_path)

        assert source.symbols() == []
        assert source.candidate_row("RIG") is None
        assert "RIG" in source.liquidity_blocked
        code, detail = source.liquidity_blocked["RIG"]
        assert code == el.ABSOLUTE_LIQUIDITY_TOO_LOW
        # The precision watch itself still says READY -- this is an
        # execution block, not a strategy INVALIDATED.
        assert source.evaluations["RIG"].ready

    def test_healthy_liquidity_and_no_broker_still_admits(self, tmp_path, monkeypatch):
        # No broker configured -> cash precheck is UNAVAILABLE -> fails
        # open (§9); liquidity alone decides here.
        feats = _feats(entry_quality=_quality())
        monkeypatch.setattr("s6_live.precision_watch.evaluate", _ready_evaluate("RIG", feats))
        source = self._source(tmp_path, broker=None)

        assert source.symbols() == ["RIG"]
        assert source.candidate_row("RIG") is not None
        assert not source.liquidity_blocked
        assert not source.cash_precheck_blocked

    def test_insufficient_cash_blocks_admission_after_liquidity_passes(self, tmp_path, monkeypatch):
        feats = _feats(entry_quality=_quality())
        monkeypatch.setattr("s6_live.precision_watch.evaluate", _ready_evaluate("RIG", feats))
        source = self._source(tmp_path, broker=Broker(cash=1.0))

        assert source.symbols() == []
        assert source.candidate_row("RIG") is None
        assert not source.liquidity_blocked
        code, detail = source.cash_precheck_blocked["RIG"]
        assert code == cash_precheck.INSUFFICIENT_CASH_PRECHECK
        assert detail["available_cash"] == 1.0
        assert detail["required_for_1_share"] == pytest.approx(5.79)

    def test_carried_entry_quality_feeds_the_buy_intent_sizing_cap(self, tmp_path, monkeypatch):
        """The row written to BUY_INTENT carries the snapshot the
        execution worker's sizing hook (`IntentQueueSource.liquidity_max_qty`)
        later reads -- no second computation, no new REST call (§6/§12)."""
        feats = _feats(entry_quality=_quality(recent_volume_15m=1200.0))
        monkeypatch.setattr("s6_live.precision_watch.evaluate", _ready_evaluate("RIG", feats))
        source = self._source(tmp_path, broker=Broker(cash=1000.0))

        assert source.symbols() == ["RIG"]
        row = source.candidate_row("RIG")
        assert row["entry_quality"]["recent_volume_15m"] == 1200.0


class TestIntentQueueSourceLiquidityMaxQty:
    def test_caps_using_the_carried_snapshot(self, tmp_path):
        from s6_live import buy_intent, buy_intent_source

        env = {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}
        buy_intent.write_ready("2026-09-10", "PREMARKET", [{
            "symbol": "RIG", "price": 5.79,
            "entry_quality": _quality(recent_volume_15m=100.0).as_record(),
        }], now=NOW, env=env)
        rollout = type("Rollout", (), {"allowed_symbols": frozenset()})()
        source = buy_intent_source.IntentQueueSource(
            trading_day="2026-09-10", session="PREMARKET", rollout=rollout,
            now=NOW, env=env)
        source.symbols()  # claims the queue -- see its own docstring

        assert source.liquidity_max_qty("RIG", 50) == 10  # 10% of 100

    def test_no_carried_entry_quality_passes_qty_through(self, tmp_path):
        from s6_live import buy_intent, buy_intent_source

        env = {"S6_ACTIVE_WATCH_DIR": str(tmp_path)}
        buy_intent.write_ready("2026-09-10", "PREMARKET",
                               [{"symbol": "RIG", "price": 5.79}], now=NOW, env=env)
        rollout = type("Rollout", (), {"allowed_symbols": frozenset()})()
        source = buy_intent_source.IntentQueueSource(
            trading_day="2026-09-10", session="PREMARKET", rollout=rollout,
            now=NOW, env=env)
        source.symbols()
        assert source.candidate_row("RIG") is not None  # genuinely claimed

        assert source.liquidity_max_qty("RIG", 50) == 50
