"""Which feed sent it, and how far behind it was.

Two measurements that exist because assuming either one cost a session.

The collector reported "41 of 41 subscribed" and collected nothing: KIS
accepted a realtime registration this account is not entitled to and
sent no data. `SUBSCRIBE SUCCESS` establishes only that a subscription
was ACCEPTED; readiness requires a message actually received. An earlier
probe had subscribed both feeds at once and the trades were credited to
the wrong one, which is how the collector came to be configured for a
feed that sends nothing.

And "delayed" turned out to mean about seventy seconds rather than
fifteen minutes. That number is measured continuously rather than
trusted, because a freshness rule built on a constant nobody re-checks
is the zero-volume assumption all over again.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import kis_hdfscnt0 as wire  # noqa: E402
from market_data import realtime_bars as rb  # noqa: E402

NOW = datetime(2026, 8, 28, 14, 39, 26, tzinfo=timezone.utc)


class TestSubscribeSuccessIsNotDataFlowing:
    def test_accepted_with_messages_is_flowing(self):
        assert wire.feed_capability(accepted=True, messages=1124,
                                    seconds_observed=70) == wire.DATA_FLOWING

    def test_accepted_and_silent_for_long_enough_is_suspected(self):
        """RBAQ: accepted, seventy seconds, zero trades on NVDA."""
        assert wire.feed_capability(accepted=True, messages=0,
                                    seconds_observed=70) == wire.ENTITLEMENT_SUSPECTED

    def test_silence_early_on_is_not_yet_a_verdict(self):
        """An illiquid premarket name can go minutes without a print.
        Calling that an entitlement problem is the same guess pointed the
        other way."""
        assert wire.feed_capability(accepted=True, messages=0,
                                    seconds_observed=5) == wire.SUBSCRIPTION_ACCEPTED

    def test_a_refused_subscription_is_failed(self):
        assert wire.feed_capability(accepted=False, messages=0,
                                    seconds_observed=70) == wire.FEED_FAILED

    def test_suspected_is_never_stated_as_concluded(self):
        """Only KIS can say whether it is entitlement."""
        assert "SUSPECTED" in wire.ENTITLEMENT_SUSPECTED


class TestEveryMessageCarriesItsFeed:
    def test_the_prefix_identifies_the_feed(self):
        assert wire.feed_of("DNASAAPL") == wire.FEED_DELAYED
        assert wire.feed_of("RBAQAAPL") == wire.FEED_REALTIME
        assert wire.feed_of("DNYSGE") == wire.FEED_DELAYED
        assert wire.feed_of("RBAYGE") == wire.FEED_REALTIME

    def test_an_unrecognised_key_is_not_guessed(self):
        assert wire.feed_of("XXXX") is None
        assert wire.feed_of("") is None
        assert wire.feed_of(None) is None

    def test_a_parsed_trade_records_its_tr_id(self):
        body = "^".join(["x"] * len(wire.FIELDS))
        record = wire.parse_trades(f"0|{wire.TR_TRADE}|001|{body}")[0]
        assert record["tr_id"] == wire.TR_TRADE

    def test_the_store_records_which_feed_a_symbol_arrived_on(self):
        store = rb.RealtimeBarStore()
        values = {name: "" for name in wire.FIELDS}
        values.update({"SYMB": "AAPL", "RSYM": "DNASAAPL",
                       wire.FIELD_PRICE: "100", wire.FIELD_TRADE_SIZE: "5",
                       wire.FIELD_LOCAL_DATE: "20260828",
                       wire.FIELD_LOCAL_TIME: "103815"})
        values["layout_mismatch"] = False
        store.add_trade(values, session="REGULAR", now=NOW)
        assert store.feeds_seen["AAPL"] == wire.FEED_DELAYED
        assert store.describe(now=NOW)["feeds_seen"]["AAPL"] == wire.FEED_DELAYED


class TestTheLagIsMeasuredNotAssumed:
    def test_it_reports_a_distribution(self):
        lag = rb.FeedLag()
        base = NOW
        for seconds in (60, 70, 80, 500):
            lag.observe(market_timestamp=base - timedelta(seconds=seconds),
                        received_at=base)
        described = lag.describe()
        assert described["samples"] == 4
        assert described["median"] in (70.0, 80.0)
        assert described["max"] == 500.0

    def test_an_empty_sample_reports_none_not_zero(self):
        """Zero lag would read as a perfectly fresh feed."""
        assert rb.FeedLag().describe() == {"samples": 0, "median": None,
                                           "p95": None, "max": None}

    def test_a_negative_lag_is_discarded(self):
        """Data cannot arrive before it happened; a negative sample means
        the clocks disagree, and it would corrupt the median a freshness
        rule is built on."""
        lag = rb.FeedLag()
        assert lag.observe(market_timestamp=NOW + timedelta(seconds=30),
                           received_at=NOW) is None
        assert lag.describe()["samples"] == 0

    def test_the_window_is_bounded(self):
        lag = rb.FeedLag()
        for i in range(rb.FeedLag.WINDOW + 200):
            lag.observe(market_timestamp=NOW - timedelta(seconds=60),
                        received_at=NOW)
        assert lag.describe()["samples"] == rb.FeedLag.WINDOW

    def test_the_store_exposes_it(self):
        store = rb.RealtimeBarStore()
        described = store.describe(now=NOW)
        assert "feed_lag_seconds" in described
        assert "symbols_with_data" in described

    def test_the_observed_constant_is_recorded_but_not_relied_on(self):
        """It is documentation of one measurement, and the running
        statistics are what a threshold should consult."""
        assert wire.OBSERVED_FEED_LAG_SECONDS == 70.0
        source = (REPO_ROOT / "market_data" / "realtime_bars.py").read_text(
            encoding="utf-8")
        assert "OBSERVED_FEED_LAG_SECONDS" not in source


class TestSymbolsWithDataIsTheNumberThatMatters:
    def test_it_counts_symbols_that_actually_traded(self):
        """41 of 41 subscribed was true and meant nothing."""
        store = rb.RealtimeBarStore()
        values = {name: "" for name in wire.FIELDS}
        values.update({"SYMB": "AAPL", "RSYM": "DNASAAPL",
                       wire.FIELD_PRICE: "100", wire.FIELD_TRADE_SIZE: "5",
                       wire.FIELD_LOCAL_DATE: "20260828",
                       wire.FIELD_LOCAL_TIME: "103815"})
        values["layout_mismatch"] = False
        store.add_trade(values, session="REGULAR", now=NOW)
        assert store.describe(now=NOW)["symbols_with_data"] == 1

    def test_it_is_zero_before_any_trade(self):
        assert rb.RealtimeBarStore().describe(now=NOW)["symbols_with_data"] == 0
