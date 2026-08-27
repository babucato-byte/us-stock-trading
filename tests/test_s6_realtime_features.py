"""The intraday view S6 entry and exit share.

Two distinctions this file is mostly about
------------------------------------------
1. `market_data_asof` is when the MARKET was last observed -- the newest
   bar's timestamp. `built_at` is when this object was made. DT's
   candidate rows carried a fresh `generated_at` every fifteen minutes
   while the price, volume, VWAP and EMAs inside them were bit-identical
   for three hours. One timestamp said fresh; the data was not.

2. "Nothing traded" and "nobody published what traded" are different.
   A provider without extended-hours volume returns 0 for every bar,
   which is indistinguishable from a genuinely untraded session unless
   asked separately -- and S6's volume condition is unanswerable in
   either case, which is the part that decides.

No network access: the provider is a fake serving frames.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import realtime_features as rf  # noqa: E402

ET = "America/New_York"
NOW = datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)  # 14:00 ET


def _frame(start="2026-08-26 09:30", periods=40, price=52.0, volume=100000,
           freq="5min"):
    index = pd.date_range(start=start, periods=periods, freq=freq, tz=ET)
    return pd.DataFrame({
        "Open": [price] * periods, "High": [price + 0.2] * periods,
        "Low": [price - 0.2] * periods, "Close": [price] * periods,
        "Volume": ([volume] * periods if not isinstance(volume, list)
                   else volume),
    }, index=index)


class _Data:
    def __init__(self, intraday, symbol="DT"):
        self.symbol, self.intraday, self.daily = symbol, intraday, None


class _Provider:
    def __init__(self, intraday, raises=None):
        self._intraday, self._raises = intraday, raises

    def get_symbol_data(self, symbol, **kwargs):
        if self._raises:
            raise self._raises
        return _Data(self._intraday, symbol)


class TestFreshnessIsAboutTheMarketNotTheClock:
    def test_market_data_asof_is_the_newest_bar_not_now(self):
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame()))
        assert feats.market_data_asof is not None
        assert feats.market_data_asof != feats.built_at
        assert feats.built_at == NOW

    def test_a_stale_feed_is_stale_however_recently_it_was_asked(self):
        """The DT failure: asked constantly, answered with old bars."""
        old = _frame(start="2026-08-26 09:30", periods=10)
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(old))
        assert feats.age_seconds(NOW) > rf.DEFAULT_MAX_BAR_AGE_SECONDS
        assert feats.is_stale(NOW) is True

    def test_a_current_feed_is_not_stale(self):
        recent = _frame(start="2026-08-26 13:30", periods=6)
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(recent))
        assert feats.is_stale(NOW) is False

    def test_an_unknown_age_counts_as_stale(self):
        feats = rf.SessionFeatures(symbol="DT", session="REGULAR")
        assert feats.age_seconds(NOW) is None
        assert feats.is_stale(NOW) is True


class TestVolumeUnavailableIsNotZero:
    def test_a_session_of_all_zero_volume_is_unavailable_not_zero(self):
        """A feed that omits extended-hours volume looks exactly like
        this, and S6's volume condition cannot be evaluated either way."""
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame(volume=0)))
        assert feats.volume_status == rf.VOLUME_DATA_UNAVAILABLE
        assert feats.volume_available is False
        assert "volume" in feats.unavailable

    def test_a_quiet_latest_bar_in_a_traded_session_is_confirmed_zero(self):
        volumes = [100000] * 39 + [0]
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame(volume=volumes)))
        assert feats.volume_status == rf.VOLUME_ZERO_CONFIRMED

    def test_a_traded_session_reports_volume_ok(self):
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame()))
        assert feats.volume_status == rf.VOLUME_OK
        assert feats.volume_available is True

    def test_volume_expansion_is_absent_when_volume_is(self):
        """The rule must report UNAVAILABLE rather than compute a ratio
        of zeros."""
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame(volume=0)))
        assert feats.volume_expansion is None
        assert "volume_expansion" in feats.unavailable


class TestTheValuesTheDeadRulesNeeded:
    def test_vwap_ema9_and_ema21_are_all_supplied(self):
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame()))
        assert feats.vwap is not None
        assert feats.ema9 is not None
        assert feats.ema21 is not None
        assert feats.unavailable == {} or "vwap" not in feats.unavailable

    def test_volume_expansion_is_recomputed_not_frozen(self):
        """entry_volume_expansion was carried unchanged from entry to
        exit; this is measured from the bars each tick."""
        volumes = [10000] * 3 + [50000] * 37
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame(volume=volumes)))
        assert feats.volume_expansion is not None
        assert feats.volume_expansion > 1.0


class TestAFailedViewSaysSo:
    def test_no_intraday_bars_names_every_missing_input(self):
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(None))
        for name in ("price", "vwap", "ema9", "ema21", "volume",
                     "volume_expansion"):
            assert name in feats.unavailable
        assert feats.error

    def test_a_provider_that_raises_does_not_propagate(self):
        """A caller handed an exception has to invent a fallback, and the
        fallback is always 'carry on'."""
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(None, raises=RuntimeError("feed down")))
        assert feats.error and "feed down" in feats.error
        assert feats.price is None

    def test_a_session_with_no_bars_is_named_separately(self):
        """'the session has published nothing yet' is not 'the feed is
        broken', and an operator needs to know which."""
        feats = rf.build("DT", session="OVERNIGHT_DAYTIME", now=NOW,
                         provider=_Provider(_frame()))
        assert feats.error and "OVERNIGHT_DAYTIME" in feats.error

    def test_status_of_answers_per_input(self):
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame(volume=0)))
        assert feats.status_of("volume") == rf.UNAVAILABLE
        assert feats.status_of("price") == rf.AVAILABLE


class TestTheRecordShape:
    def test_as_record_carries_the_observability_fields(self):
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame()))
        record = feats.as_record(NOW)
        for key in ("symbol", "session", "market_data_asof", "age_seconds",
                    "price", "vwap", "ema9", "ema21", "volume",
                    "volume_status", "volume_expansion", "range_high",
                    "range_low", "extension_pct", "unavailable"):
            assert key in record, key

    def test_extension_is_measured_from_the_range_high(self):
        feats = rf.build("DT", session="REGULAR", now=NOW,
                         provider=_Provider(_frame(price=52.0)))
        if feats.range_high:
            expected = (feats.price / feats.range_high - 1.0) * 100.0
            assert feats.extension_pct == pytest.approx(expected)
