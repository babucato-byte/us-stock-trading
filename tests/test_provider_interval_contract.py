"""The interval a caller asks for must be one the provider serves.

The defect
----------
`realtime_features.build` hard-coded `intraday_interval="5m"`, which was
true of yfinance and became false on 2026-09-01, when
`pretrade_validation` began injecting `KISBarMarketDataProvider` -- a 1m
provider -- for PREMARKET, AFTER_HOURS and OVERNIGHT_DAYTIME.

Every extended-session fetch then took this path:

    get_intraday_bars(interval="5m")  -> MarketDataUnavailable
    get_symbol_data                   -> intraday = None   (logged at DEBUG)
    build                             -> market_data_asof = None
    precision_watch                   -> every gate UNAVAILABLE
                                      -> WATCHING, never READY

for four days, on three live sessions, while the logs said nothing an
operator would read as a fault. The exit side took the same view: a
position held into an extended session got vwap/ema9/ema21 = None, which
silently disarms VWAP_FAILURE, EMA_STRUCTURE_FAILURE and
VOLUME_DECAY_PRICE_WEAKNESS -- the exact three rules
`s6_live/realtime_features.py` was written to restore.

The fix, and what it deliberately is not
----------------------------------------
The interval now follows the PROVIDER: each one declares what it serves
and `build` asks whichever provider it was actually handed. It is NOT a
session-to-interval table -- that would be a second copy of the routing
in `provider_for_session`, and two mappings that must agree are how this
class of defect starts.

Nothing here changes what S6 decides. The ORB window is measured in
MINUTES, not in bars, so a 15-minute range is fifteen minutes at either
resolution; the thresholds, the rules and the ranking are untouched. 1m
is also the resolution the extended-session SCANNER already uses to
produce these candidates, so this makes entry re-validation ask its
question of the same measurement that raised it.
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data.kis_bar_provider import (  # noqa: E402
    KISBarMarketDataProvider, provider_for_session,
)
from s6_live import precision_watch as pw  # noqa: E402
from s6_live import pretrade_validation as ptv  # noqa: E402
from s6_live import realtime_features as rf  # noqa: E402
from scanners.base.market_data_provider import (  # noqa: E402
    NORMAL_SYMBOL_DATA_UNAVAILABLE, UNSUPPORTED_PROVIDER_CONTRACT,
    BarMarketDataProvider, CachingMarketDataProvider, MarketDataUnavailable,
    UnsupportedIntervalError, YahooFinanceMarketDataProvider,
)

ET = ZoneInfo("America/New_York")

EXTENDED = ("PREMARKET", "AFTER_HOURS", "OVERNIGHT_DAYTIME")

#: Where each extended session opens, in Eastern time. Taken from the
#: windows `scanners/base/session_range.py` already defines, so a fixture
#: cannot quietly test a session boundary the code does not use.
SESSION_OPEN_ET = {
    "PREMARKET": (4, 0),
    "AFTER_HOURS": (16, 0),
    "OVERNIGHT_DAYTIME": (20, 0),
}

#: 08:00 ET on 2026-08-31 -- inside premarket, with bars behind it.
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


# --- doubles ---------------------------------------------------------------

class _Broker:
    """Answers HHDFS76950200 with premarket-shaped rows."""

    def __init__(self, rows):
        self._rows = rows
        self.asked = []

    class config:
        @staticmethod
        def validate_read_allowed():
            return True

    def _get(self, path, tr_id, params):
        self.asked.append(params)
        return {"rt_cd": "0", "output2": self._rows}


def _rows(session="PREMARKET"):
    """Forty-five 1m bars from `session`'s own opening.

    Each session gets bars inside ITS window -- a premarket fixture
    would produce an empty slice for after-hours, and the test would
    then be measuring the fixture rather than the code.

    Priced so the strategy's own conditions are answerable: a rising
    tape (EMA9 > EMA21, price > VWAP), a 15-minute opening range that
    later bars break out of, and expanding volume after the range.
    """
    hour, minute0 = SESSION_OPEN_ET[session]
    rows = []
    for i in range(45):
        minute = hour * 60 + minute0 + i
        price = 100.0 + i * 0.05          # steadily rising
        volume = 100 if i < 15 else 400   # expansion after the range
        rows.append({
            "xymd": "20260831",
            "xhms": f"{minute // 60:02d}{minute % 60:02d}00",
            "open": f"{price:.4f}", "high": f"{price + 0.02:.4f}",
            "low": f"{price - 0.02:.4f}", "last": f"{price:.4f}",
            "evol": str(volume), "eamt": str(volume * price),
        })
    # The wire is newest-first; parse_rows re-sorts. Handing it in wire
    # order keeps the fixture honest about what KIS actually sends.
    return list(reversed(rows))


def _now_for(session):
    """One minute after that session's last fixture bar."""
    hour, minute0 = SESSION_OPEN_ET[session]
    opened = datetime(2026, 8, 31, hour, minute0, tzinfo=ET)
    return (opened + timedelta(minutes=45)).astimezone(timezone.utc)


class _DailyOnly(BarMarketDataProvider):
    """A fallback that serves daily bars and nothing else."""

    name = provider_name = "stub-daily"

    def get_daily_bars(self, symbol, lookback_days=400):
        index = pd.date_range("2026-08-24", periods=5, freq="D", tz="UTC")
        return pd.DataFrame(
            {"Open": [100.0] * 5, "High": [101.0] * 5, "Low": [99.0] * 5,
             "Close": [100.0] * 5, "Volume": [1_000_000.0] * 5}, index=index)

    def get_intraday_bars(self, symbol, interval="1m", lookback_days=5,
                          include_prepost=True):
        raise MarketDataUnavailable(f"{symbol}: stub serves no intraday bars")


class _RecordingKIS(KISBarMarketDataProvider):
    """A real KIS provider that remembers which interval it was asked for."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.intervals = []

    def get_intraday_bars(self, symbol, interval="1m", lookback_days=5,
                          include_prepost=True):
        self.intervals.append(interval)
        return super().get_intraday_bars(
            symbol, interval=interval, lookback_days=lookback_days,
            include_prepost=include_prepost)


def _kis(session="PREMARKET", **kwargs):
    return _RecordingKIS(broker=_Broker(_rows(session)), fallback=_DailyOnly(),
                         exchange_for=lambda s: "NASDAQ", **kwargs)


# --- 1. each variant asks for the interval its provider serves --------------

class TestEachVariantAsksForAServedInterval:
    @pytest.mark.parametrize("session", EXTENDED)
    def test_extended_sessions_request_1m_from_the_kis_provider(self, session):
        """S6-P, S6-A and S6-O. The provider serves 1m; the ask is 1m."""
        provider = _kis(session)
        rf.build("AAPL", session=session, now=_now_for(session),
                 provider=provider)
        assert provider.intervals == ["1m"], session

    def test_regular_remains_on_yfinance_5m(self):
        """S6-R is untouched. Its path is validated and in production."""
        provider = provider_for_session(
            "REGULAR", broker=object(), fallback=YahooFinanceMarketDataProvider())
        assert isinstance(provider, YahooFinanceMarketDataProvider)
        assert rf._interval_for(provider, None) == "5m"

    def test_the_default_for_an_undeclared_provider_is_still_5m(self):
        """Nothing that did not opt in changes resolution."""
        assert rf._interval_for(object(), None) == "5m"
        assert rf.DEFAULT_INTRADAY_INTERVAL == "5m"

    def test_an_explicit_interval_still_wins(self):
        """A caller naming an interval is making a measurement decision."""
        assert rf._interval_for(_kis(), "5m") == "5m"

    def test_the_cache_wrapper_carries_the_contract_through(self):
        """Production wraps every provider in this; the base class's 5m
        leaking through it would restore the defect in exactly the
        configuration that runs live."""
        assert CachingMarketDataProvider(
            _kis()).preferred_intraday_interval == "1m"
        assert CachingMarketDataProvider(
            YahooFinanceMarketDataProvider()).preferred_intraday_interval == "5m"

    def test_the_guard_and_the_declaration_are_the_same_list(self):
        """Declared once, enforced from the declaration -- so a future
        edit cannot widen one without the other."""
        provider = _kis()
        for served in provider.supported_intraday_intervals:
            assert provider.serves_intraday_interval(served)
        assert not provider.serves_intraday_interval("5m")
        assert provider.preferred_intraday_interval in \
            provider.supported_intraday_intervals


# --- 2. the entry path actually receives features --------------------------

FEATURE_FIELDS = ("market_data_asof", "price", "vwap", "ema9", "ema21",
                  "volume", "volume_expansion")


class TestTheEntryFeaturePathIsRestored:
    @pytest.mark.parametrize("session", EXTENDED)
    def test_every_required_feature_is_present(self, session):
        feats = rf.build("AAPL", session=session, now=_now_for(session),
                         provider=_kis(session))
        for field in FEATURE_FIELDS:
            assert getattr(feats, field) is not None, f"{session}: {field}"
        assert feats.error is None
        assert feats.volume_status == rf.VOLUME_OK
        assert feats.bar_count > 0

    @pytest.mark.parametrize("session", EXTENDED)
    def test_precision_watch_evaluates_rather_than_abstains(self, session):
        """The production symptom was every gate UNAVAILABLE. These
        conditions must become evaluated FACTS -- PASS or FAIL -- which
        is a different claim from "the candidate is READY"."""
        evaluation = pw.evaluate("AAPL", session=session,
                                 now=_now_for(session),
                                 provider=_kis(session))
        for condition in (pw.C_MARKET_DATA_ASOF, pw.C_MARKET_DATA_FRESH,
                          pw.C_PRICE, pw.C_VWAP_AVAILABLE, pw.C_EMA_AVAILABLE,
                          pw.C_VOLUME_VALID):
            assert evaluation.conditions[condition] != pw.UNAVAILABLE, (
                f"{session}: {condition}")
        assert evaluation.detail["market_data_asof"] is not None

    def test_the_pre_fix_wiring_is_what_produced_universal_unavailability(self):
        """The regression, pinned. Asking 5m of a 1m provider must still
        be recognisable as the failure it was."""
        evaluation = pw.evaluate("AAPL", session="PREMARKET", now=NOW,
                                 provider=_kis(), features=rf.build(
                                     "AAPL", session="PREMARKET", now=NOW,
                                     provider=_kis(), intraday_interval="5m"))
        assert evaluation.conditions[pw.C_MARKET_DATA_ASOF] == pw.UNAVAILABLE
        assert evaluation.state != pw.READY_TO_BUY


# --- 3. the exit path too ---------------------------------------------------

class TestTheExitFeaturePathIsRestored:
    @pytest.mark.parametrize("session", EXTENDED)
    def test_held_position_features_carry_the_exit_rule_inputs(self, session):
        """VWAP_FAILURE, EMA_STRUCTURE_FAILURE and
        VOLUME_DECAY_PRICE_WEAKNESS are predicates over these three. A
        None here is a rule that cannot fire on a position holding real
        money."""
        features_fn = rf.make_features_fn(session=session,
                                          now=_now_for(session),
                                          provider=_kis(session))
        feats = features_fn("AAPL")
        assert feats.vwap is not None
        assert feats.ema9 is not None and feats.ema21 is not None
        assert feats.volume is not None
        assert feats.volume_status == rf.VOLUME_OK

    def test_the_runtime_hands_the_exit_path_a_session_provider(self):
        source = (REPO_ROOT / "scripts" / "run_s6_runtime.py").read_text()
        assert "realtime_features.make_features_fn(" in source
        assert "provider_for_session(session, broker=broker)" in source


# --- 4. an unsupported contract cannot be silent ---------------------------

class TestAnUnsupportedContractIsNotAQuietSymbol:
    def test_the_two_causes_are_named_apart(self):
        assert UNSUPPORTED_PROVIDER_CONTRACT != NORMAL_SYMBOL_DATA_UNAVAILABLE

    def test_an_unsupported_interval_is_reported_at_warning(self, caplog):
        """DEBUG is what hid this. The line has to reach a level an
        operator sees, and has to say it is a wiring fault."""
        with caplog.at_level(logging.WARNING):
            data = _kis().get_symbol_data("AAPL", intraday_interval="5m")
        assert data.intraday is None
        assert data.intraday_unavailable_reason == UNSUPPORTED_PROVIDER_CONTRACT
        assert any(UNSUPPORTED_PROVIDER_CONTRACT in r.getMessage()
                   for r in caplog.records if r.levelno >= logging.WARNING)

    def test_an_ordinary_missing_symbol_stays_at_debug(self, caplog):
        """A thin book must not start shouting. The distinction is the
        whole point."""
        with caplog.at_level(logging.WARNING):
            data = _DailyOnly().get_symbol_data("THIN", intraday_interval="5m")
        assert data.intraday is None
        assert data.intraday_unavailable_reason == NORMAL_SYMBOL_DATA_UNAVAILABLE
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_the_features_name_the_contract_fault(self):
        feats = rf.build("AAPL", session="PREMARKET", now=NOW,
                         provider=_kis(), intraday_interval="5m")
        assert UNSUPPORTED_PROVIDER_CONTRACT in (feats.error or "")
        assert feats.unavailable["price"] == UNSUPPORTED_PROVIDER_CONTRACT

    def test_it_is_still_only_a_symbol_level_failure(self):
        """No new global gate. One symbol must never take a cycle down."""
        assert issubclass(UnsupportedIntervalError, MarketDataUnavailable)
        provider = _kis()
        provider.get_symbol_data("AAPL", intraday_interval="5m")  # no raise

    def test_the_error_carries_what_was_asked_and_what_is_served(self):
        with pytest.raises(UnsupportedIntervalError) as caught:
            _kis().get_intraday_bars("AAPL", interval="5m")
        assert caught.value.requested == "5m"
        assert "1m" in caught.value.supported


# --- 5. semantics are untouched --------------------------------------------

class TestTheStrategyIsUnchanged:
    def test_no_threshold_moved(self):
        from scanners.base import config as scanner_config

        cfg = scanner_config.load_config("orb", scanner_name="orb")
        assert cfg.require_int("orb_minutes") == 15
        assert cfg.require_bool("require_close_breakout") is True
        assert cfg.require_bool("require_price_above_vwap") is True
        assert cfg.require_bool("require_ema9_above_ema21") is True
        assert cfg.require_float("volume_expansion_min") == 1.2
        assert cfg.require_float("retest_tolerance_pct") == 0.3
        assert cfg.require_int("min_post_range_bars") == 3
        assert cfg.require_float("max_extension_above_or_high_pct") == 6.0

    def test_the_opening_range_is_fifteen_minutes_at_either_resolution(self):
        """The ORB window is wall-clock, not a bar count, so changing the
        source resolution cannot change the range it measures. This is
        why no threshold needed retuning."""
        from scanners.base import session_range as srange

        index = pd.to_datetime(
            [datetime(2026, 8, 31, 4, i, tzinfo=ET) for i in range(45)])
        minute_bars = pd.DataFrame(
            {"Open": range(45), "High": [i + 1 for i in range(45)],
             "Low": list(range(45)), "Close": list(range(45)),
             "Volume": [100.0] * 45}, index=index)
        window = srange.opening_range(minute_bars, "PREMARKET", minutes=15,
                                      session_date=datetime(2026, 8, 31).date())
        assert window.minutes == 15
        assert (window.range_end - window.range_start).total_seconds() == 14 * 60

    def test_the_condition_set_is_unchanged(self):
        assert pw.CONDITION_ORDER == (
            pw.C_MARKET_DATA_ASOF, pw.C_MARKET_DATA_FRESH, pw.C_PRICE,
            pw.C_VWAP_AVAILABLE, pw.C_EMA_AVAILABLE, pw.C_PRICE_ABOVE_VWAP,
            pw.C_EMA_STRUCTURE, pw.C_BREAKOUT, pw.C_VOLUME_VALID,
            pw.C_VOLUME_EXPANSION, pw.C_EXTENSION, pw.C_REENTRY)

    def test_the_kis_authoritative_session_set_is_unchanged(self):
        assert ptv.provider_for("REGULAR", broker=object(),
                                fallback="untouched") == "untouched"
        for session in EXTENDED:
            assert isinstance(
                ptv.provider_for(session, broker=object(), fallback=object()),
                KISBarMarketDataProvider)


# --- 6. no cross-session bar reuse -----------------------------------------

class TestNoCrossSessionBarReuse:
    def test_each_session_slices_its_own_bars(self):
        """The chart response reaches back across midnight and across
        session boundaries. Every variant must see only its own."""
        from scanners.base import session_range as srange

        stamps = [datetime(2026, 8, 31, h, m, tzinfo=ET)
                  for h, m in ((4, 30), (8, 0), (10, 0), (14, 0),
                               (16, 30), (19, 0), (21, 0))]
        frame = pd.DataFrame(
            {"Open": [1.0] * 7, "High": [1.0] * 7, "Low": [1.0] * 7,
             "Close": [1.0] * 7, "Volume": [10.0] * 7},
            index=pd.to_datetime(stamps))
        day = datetime(2026, 8, 31).date()

        premarket = srange.slice_session_bars(frame, "PREMARKET", session_date=day)
        regular = srange.slice_session_bars(frame, "REGULAR", session_date=day)
        after = srange.slice_session_bars(frame, "AFTER_HOURS", session_date=day)

        assert [s.hour for s in premarket.index] == [4, 8]
        assert [s.hour for s in regular.index] == [10, 14]
        assert [s.hour for s in after.index] == [16, 19]

    def test_the_features_report_the_session_they_were_built_for(self):
        for session in EXTENDED:
            feats = rf.build("AAPL", session=session, now=_now_for(session),
                             provider=_kis(session))
            assert feats.session == session

    def test_the_chart_fetch_is_still_filtered_to_one_trading_day(self):
        """`parse_rows` drops bars from other dates. The 120-bar response
        crosses midnight, and a session VWAP over two days is wrong in a
        way that looks entirely reasonable."""
        from market_data import kis_minute_chart

        rows = [{"xymd": "20260831", "xhms": "040000", "last": "100",
                 "open": "100", "high": "100", "low": "100", "evol": "10"},
                {"xymd": "20260830", "xhms": "200000", "last": "99",
                 "open": "99", "high": "99", "low": "99", "evol": "10"}]
        parsed = kis_minute_chart.parse_rows(rows, trading_day="2026-08-31")
        assert [p["at"].date().isoformat() for p in parsed] == ["2026-08-31"]
