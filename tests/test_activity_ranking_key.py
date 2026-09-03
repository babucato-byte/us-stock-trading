"""The ranking has one key, because it is one fact.

2026-09-02, same day, same release, same directory, thirty minutes apart:

    13:22Z  premarket  provider kis       active universe: 300 of 300
    13:52Z  open       provider yfinance  FAILED_NO_UNIVERSE

Nothing was stale and nothing was missing. `ActivityStore.load()` was
keyed by `provider_name`, and the provider is chosen by SESSION: KIS
inside PREMARKET / AFTER_HOURS / OVERNIGHT_DAYTIME, the bulk fallback in
REGULAR. The daily profile runs at 16:17 ET -- AFTER_HOURS -- so it wrote
`activity/kis.json`. The open profile runs at 09:52 ET -- REGULAR -- so
it read `activity/yfinance.json`, which no job has ever written.

The ranking is "which symbols traded the most dollars yesterday". That is
the same answer whoever fetched the bars, and keying it by feed made a
shared answer look like several private ones.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import activity as act  # noqa: E402

TODAY = date(2026, 9, 2)
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture(autouse=True)
def analytics(monkeypatch, tmp_path):
    monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
    return tmp_path


def _write(store, symbols, day=YESTERDAY):
    for i, sym in enumerate(symbols):
        store.note(sym, trading_day=day.isoformat(), price=10.0 + i,
                   avg_volume=1_000_000 + i)
    return store.save()


class TestDailyWritesAndEveryProfileReads:
    def test_the_daily_provider_writes_a_ranking(self):
        path = _write(act.ActivityStore("kis"), ["AAA", "BBB", "CCC"])
        assert path is not None and path.exists()

    def test_the_open_profile_reads_what_daily_wrote(self):
        """The incident, directly: kis writes, yfinance reads."""
        _write(act.ActivityStore("kis"), ["AAA", "BBB", "CCC"])
        reader = act.ActivityStore.load("yfinance")
        assert reader.active_symbols(limit=10, today=TODAY) == [
            "CCC", "BBB", "AAA"]

    def test_premarket_still_reads_it_too(self):
        _write(act.ActivityStore("kis"), ["AAA", "BBB"])
        assert act.ActivityStore.load("kis").active_symbols(
            limit=10, today=TODAY)

    def test_every_provider_resolves_the_same_file(self):
        assert act.store_path("kis") == act.store_path("yfinance")
        assert act.store_path().name == f"{act.RANKING_KEY}.json"

    def test_the_producing_provider_is_still_recorded(self):
        """Filed in one place, still attributable."""
        _write(act.ActivityStore("kis"), ["AAA"])
        payload = json.loads(act.store_path().read_text())
        assert payload["provider"] == "kis"


class TestPreCanonicalRankingsKeepWorking:
    def test_an_existing_provider_keyed_file_is_still_read(self, analytics):
        """The deploy itself must not empty the universe."""
        legacy = analytics / act.ACTIVITY_SUBDIR
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "kis.json").write_text(json.dumps({
            "provider": "kis", "updated_at": "2026-09-02T09:50:00+00:00",
            "symbols": {"AAA": {"symbol": "AAA", "trading_day":
                                YESTERDAY.isoformat(), "price": 10.0,
                                "avg_volume": 5_000_000,
                                "dollar_volume": 50_000_000.0}}}))
        loaded = act.ActivityStore.load("yfinance")
        assert loaded.active_symbols(limit=10, today=TODAY) == ["AAA"]

    def test_the_canonical_file_wins_when_both_exist(self, analytics):
        directory = analytics / act.ACTIVITY_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "kis.json").write_text(json.dumps({
            "provider": "kis", "symbols": {"OLD": {
                "symbol": "OLD", "trading_day": YESTERDAY.isoformat(),
                "price": 1.0, "avg_volume": 1, "dollar_volume": 1.0}}}))
        _write(act.ActivityStore("kis"), ["NEW"])
        assert act.ActivityStore.load("kis").active_symbols(
            limit=10, today=TODAY) == ["NEW"]


class TestItStillFailsLoudlyWhenGenuinelyMissing:
    def test_no_ranking_at_all_yields_an_empty_pool(self):
        """FAILED_NO_UNIVERSE must remain reachable -- an empty pool is an
        operational fact, not a market with no active names."""
        assert act.ActivityStore.load("yfinance").active_symbols(
            limit=10, today=TODAY) == []

    def test_an_unreadable_ranking_does_not_invent_symbols(self, analytics):
        directory = analytics / act.ACTIVITY_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{act.RANKING_KEY}.json").write_text("{not json")
        assert act.ActivityStore.load("kis").active_symbols(
            limit=10, today=TODAY) == []

    def test_a_stale_trading_day_is_rejected(self):
        """Ranking older than the age window must not be used."""
        stale = TODAY - timedelta(days=act.DEFAULT_MAX_AGE_DAYS + 3)
        _write(act.ActivityStore("kis"), ["AAA"], day=stale)
        assert act.ActivityStore.load("kis").active_symbols(
            limit=10, today=TODAY) == []

    def test_a_fresh_trading_day_is_accepted(self):
        _write(act.ActivityStore("kis"), ["AAA"], day=YESTERDAY)
        assert act.ActivityStore.load("kis").active_symbols(
            limit=10, today=TODAY) == ["AAA"]


class TestEligibilityIsDeliberatelyUnchanged:
    def test_eligibility_remains_provider_keyed(self):
        """Whether a provider can serve a symbol genuinely differs
        between providers; that key is correct as it is."""
        from scanners.base import eligibility as elig

        assert elig.store_path("kis") != elig.store_path("yfinance")
