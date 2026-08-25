"""Deciding offline which symbols are worth a provider call.

A full pass over the raw 12,886-name universe priced 57% at a fixed
fetch interval and 47% with an adaptive one, while the same code priced
90% over 4,000 names. The binding variable is how many symbols are
requested. Roughly 7,200 of those names can never be an S6 entry and
`universe.csv` already says so, at no network cost.

The two kinds of mistake are not symmetric, and the tests are weighted
accordingly. Wrongly INCLUDING an ETF costs one call and the trading
node's security-master check catches it -- it already rejected AGG and
VCSH in production. Wrongly EXCLUDING a common stock removes it from
discovery entirely and nothing downstream can recover it.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from discovery import eligible_universe as eu  # noqa: E402
from discovery import provider_health as ph  # noqa: E402

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class TestWhatCanNeverBeAnS6Entry:
    @pytest.mark.parametrize("name,reason", [
        ("iShares Core U.S. Aggregate Bond ETF", "ETP"),
        ("Vanguard Short-Term Corporate Bond ETF", "ETP"),
        ("Invesco QQQ Trust, Series 1", "ETP"),
        ("State Street SPDR S&P 500 ETF Trust", "ETP"),
        ("VanEck Semiconductor ETF", "ETP"),
        ("ProShares UltraPro QQQ", "ETP"),
        ("Direxion Daily Semiconductors Bull 3X", "ETP"),
        ("Global X Lithium & Battery Tech ETF", "ETP"),
        ("Acme Corp 7.5% Series A Preferred Stock", "PREFERRED"),
        ("Acme Corp Depositary Shares", "PREFERRED"),
        ("Acme Acquisition Corp Warrant", "WARRANT"),
        ("Acme Corp Rights", "RIGHT"),
        ("Acme Acquisition Corp Units", "UNIT"),
        ("Acme Corp 5.00% Senior Notes due 2030", "NOTE"),
        ("Acme Closed-End Fund", "CLOSED_END_FUND"),
    ])
    def test_it_is_excluded_with_the_right_reason(self, name, reason):
        assert eu.classify(name, "NASDAQ") == reason

    def test_a_non_us_venue_is_excluded(self):
        """ARCA and BATS are ETF venues, and the trading node already
        refuses ARCA outright -- fetching them buys nothing an order
        could use."""
        assert eu.classify("Anything Inc.", "ARCA") == eu.REASON_NON_US_EXCHANGE
        assert eu.classify("Anything Inc.", "BATS") == eu.REASON_NON_US_EXCHANGE
        assert eu.classify("Anything Inc.", "OTC") == eu.REASON_NON_US_EXCHANGE

    def test_an_untradable_row_is_excluded(self):
        assert eu.classify("Acme Inc. Common Stock", "NYSE",
                           tradable=False) == eu.REASON_NOT_TRADABLE


class TestRealCommonStockSurvives:
    """The expensive mistake. A name dropped here is never looked at
    again, by anything."""

    @pytest.mark.parametrize("name,exchange", [
        ("Apple Inc. Common Stock", "NASDAQ"),
        ("MARA Holdings, Inc. Common Stock", "NASDAQ"),
        ("BitMine Immersion Technologies, Inc.", "AMEX"),
        ("UiPath, Inc.", "NYSE"),
        ("GUIDEWIRE SOFTWARE, INC.", "NYSE"),
        ("Sadot Group Inc. Common Stock", "NASDAQ"),
        ("Expion360 Inc. Common Stock", "NASDAQ"),
        ("The9 Limited American Depository Shares", "NASDAQ"),
        ("Robinhood Markets, Inc. Class A Common Stock", "NASDAQ"),
        ("Strategy Inc Common Stock Class A", "NASDAQ"),
        ("Rigetti Computing, Inc. Common Stock", "NASDAQ"),
        ("Mettler-Toledo International", "NYSE"),
    ])
    def test_it_is_kept(self, name, exchange):
        assert eu.classify(name, exchange) == eu.ELIGIBLE

    def test_a_ticker_that_looks_exotic_is_judged_on_its_name(self):
        """BMNR, SDOT and XPON all look like warrants or units and all
        three are ordinary common stock. Suffix-guessing would have lost
        every one of them."""
        for symbol_shaped_name in ("BitMine Immersion Technologies, Inc.",
                                   "Sadot Group Inc. Common Stock",
                                   "Expion360 Inc. Common Stock"):
            assert eu.classify(symbol_shaped_name, "NASDAQ") == eu.ELIGIBLE

    def test_a_missing_name_is_kept_when_the_venue_is_right(self):
        """A gap in the metadata is not evidence about the issue."""
        assert eu.classify(None, "NYSE") == eu.ELIGIBLE
        assert eu.classify("", "NASDAQ") == eu.ELIGIBLE
        assert eu.classify(None, "ARCA") == eu.REASON_NON_US_EXCHANGE


class TestTheCacheIsBuiltFromRowsAndCounted:
    ROWS = [
        {"symbol": "AAPL", "name": "Apple Inc. Common Stock",
         "exchange": "NASDAQ", "tradable": True},
        {"symbol": "AGG", "name": "iShares Core ETF",
         "exchange": "ARCA", "tradable": True},
        {"symbol": "FOOW", "name": "Foo Corp Warrant",
         "exchange": "NASDAQ", "tradable": True},
        {"symbol": "PATH", "name": "UiPath, Inc.",
         "exchange": "NYSE", "tradable": True},
    ]

    def test_the_counts_add_up(self):
        doc = eu.build(self.ROWS)
        assert doc["source_universe_count"] == 4
        assert doc["eligible_count"] == 2
        assert doc["excluded_count"] == 2
        assert doc["eligible_count"] + doc["excluded_count"] == \
            doc["source_universe_count"]

    def test_every_exclusion_has_a_named_reason(self):
        doc = eu.build(self.ROWS)
        assert sum(doc["exclude_reason_counts"].values()) == doc["excluded_count"]
        assert set(doc["exclude_reason_counts"]) <= {
            "ETP", "PREFERRED", "WARRANT", "RIGHT", "UNIT", "NOTE",
            "CLOSED_END_FUND", eu.REASON_NON_US_EXCHANGE,
            eu.REASON_NOT_TRADABLE, eu.REASON_NO_METADATA}

    def test_a_symbol_is_counted_once(self):
        doc = eu.build(self.ROWS)
        assert len(doc["symbols"]) == len(set(doc["symbols"]))


class TestTheCacheIsNotRebuiltEveryHour:
    """Security type and listing venue are not intraday facts. A symbol
    does not become an ETF at lunchtime, and rebuilding this hourly
    would re-derive an unchanged answer over 12,887 rows for nothing."""

    def _doc(self, **over):
        doc = eu.build(TestTheCacheIsBuiltFromRowsAndCounted.ROWS)
        doc.update(over)
        return doc

    def test_a_fresh_cache_is_reused(self):
        doc = self._doc(generated_at=(NOW - timedelta(hours=2)).isoformat())
        assert eu.is_stale(doc, now=NOW) is False

    def test_an_old_cache_is_rebuilt(self):
        doc = self._doc(generated_at=(NOW - timedelta(hours=30)).isoformat())
        assert eu.is_stale(doc, now=NOW) is True

    def test_a_newer_universe_file_forces_a_rebuild(self):
        """The cache would otherwise describe a different set of symbols
        than the one the scan is about to walk."""
        doc = self._doc(generated_at=(NOW - timedelta(hours=1)).isoformat())
        assert eu.is_stale(doc, now=NOW,
                           universe_mtime=NOW - timedelta(minutes=10)) is True
        assert eu.is_stale(doc, now=NOW,
                           universe_mtime=NOW - timedelta(hours=5)) is False

    def test_a_missing_or_foreign_cache_is_rebuilt(self):
        assert eu.is_stale(None, now=NOW) is True
        assert eu.is_stale({"schema_version": "old"}, now=NOW) is True
        assert eu.is_stale(self._doc(generated_at="not a date"),
                           now=NOW) is True

    def test_it_round_trips_through_disk(self, tmp_path):
        path = tmp_path / "eligible.json"
        eu.write(self._doc(), path)
        loaded = eu.read(path)
        assert loaded["eligible_count"] == 2
        assert eu.is_stale(loaded, now=NOW) is False


class TestProviderFailuresAreSeparated:
    """"The provider refused us" and "this symbol does not trade" arrive
    looking identical -- an empty row either way -- and have opposite
    fixes."""

    @pytest.mark.parametrize("message,category", [
        ("YFRateLimitError('Too Many Requests. Rate limited.')", ph.RATE_LIMIT),
        ("$ABI: possibly delisted; no price data found", ph.DATA_UNAVAILABLE),
        ("Quote not found for symbol: EFC.PRC", ph.DATA_UNAVAILABLE),
        ("HTTP Error 404", ph.DATA_UNAVAILABLE),
        ("OperationalError('unable to open database file')", ph.LOCAL_DB_ERROR),
        ("database is locked", ph.LOCAL_DB_ERROR),
        ("getaddrinfo thread failed", ph.NETWORK_RESOURCE_ERROR),
        ("can't start new thread", ph.NETWORK_RESOURCE_ERROR),
        ("Connection timed out after 10001 milliseconds",
         ph.NETWORK_RESOURCE_ERROR),
        ("TypeError(\"'NoneType' object is not subscriptable\")",
         ph.PROVIDER_INTERNAL_ERROR),
    ])
    def test_each_observed_failure_is_classified(self, message, category):
        assert ph.classify(message) == category

    def test_a_rate_limit_is_never_read_as_a_dead_symbol(self):
        """The distinction the coverage number depends on."""
        assert ph.classify("Rate limited. Try after a while.") != \
            ph.DATA_UNAVAILABLE

    def test_an_unknown_message_says_so(self):
        for junk in ("something new", "", None, 42):
            assert ph.classify(junk) == ph.UNCLASSIFIED

    def test_the_counter_tallies_without_suppressing(self):
        import logging

        logger = logging.getLogger("test-provider")
        with ph.capture("test-provider") as failures:
            logger.warning("YFRateLimitError: Too Many Requests")
            logger.warning("$FOO: possibly delisted")
            logger.warning("$BAR: possibly delisted")
        assert failures.summary() == {ph.DATA_UNAVAILABLE: 2, ph.RATE_LIMIT: 1}
        assert logger.handlers == [], "the handler outlived the pass"

    def test_the_counter_never_raises_into_what_it_counts(self):
        counter = ph.ProviderFailureCounter()

        class Bad:
            def getMessage(self):
                raise RuntimeError("boom")

        counter.emit(Bad())          # must not propagate
        assert counter.counts == {}
