"""A scan generation is declared, complete, and atomic.

Three failures shared one cause -- "which generation is current" was
INFERRED from the rows on disk rather than declared:

  * A COMPLETED scan that found nothing wrote no rows at all
    (`publish()` returns early on an empty set), so the newest rows
    stayed the PREVIOUS generation's. Fifteen candidates from generation
    20 remained live after generation 21 had authoritatively answered
    "none". Absence of rows was doing duty for two opposite facts.

  * An append loop is not atomic. A write that dies halfway leaves rows
    that look complete and are not.

  * Because a generation could not be shown to be complete, a consumer
    could not safely keep the previous one while a new scan ran -- so an
    OVERNIGHT_DAYTIME scan, which takes about an hour, left the entry
    funnel with zero candidates for most of every hour with a perfectly
    good generation sitting on disk.

What does NOT change: cross-variant reuse stays forbidden, a partial
in-progress answer is never consumable, FAILED is never read as zero,
and serving a candidate list never makes its market evidence READY.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_sessions  # noqa: E402
from config import scanner_live_mode as slm  # noqa: E402
from s6_live import candidate_source as cs  # noqa: E402
from scanners.publish import candidates as publisher  # noqa: E402
from scanners.publish import generations as gen  # noqa: E402
from scanners.publish import scan_cycle  # noqa: E402

DAY = "2026-09-03"
SESSION = "OVERNIGHT_DAYTIME"
VARIANT = "S6-O"
ORB = s6_sessions.SCANNER_NAME
NOW = datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)


class Signal:
    def __init__(self, symbol, run_id="gen-1"):
        self.symbol, self.scanner_score, self.signal_price = symbol, 70.0, 100.0
        self.scanner_name, self.scanner_version = "orb", "orb_v1.0"
        self.signal_id, self.scanner_run_id = f"s-{symbol}", run_id
        self.volume = self.avg_volume = self.volume_multiple = None
        self.price_change_pct = self.hma200 = self.hma200_slope = None
        self.hma89 = self.vwap = None
        self.market_data_provider = self.market_data_feed = None
        self.data_timestamp = self.feature_timestamp = None
        self.source_timeframe = self.timestamp = None
        self.reasons = []
        self.metrics = {"opening_range_high": 99.5, "opening_range_low": 99.0,
                        "orb_minutes": 15, "vwap": 100.0, "price": 100.0}


def live_modes():
    modes = dict(slm.SCANNER_LIVE_MODE)
    modes["orb"] = slm.MODE_LIMITED_LIVE
    return modes


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "handoff"))

    def publish(symbols, *, generation_id="gen-1", day=DAY, session=SESSION,
                variant=VARIANT, status=gen.STATUS_COMPLETED,
                completed_at=None, declare=True):
        publisher.publish([Signal(s, run_id=generation_id) for s in symbols],
                          strategy_id=cs.STRATEGY_ID, trading_day=day,
                          session=session, variant=variant,
                          run_id=generation_id)
        publisher.mark_run(day, session, strategy_id=cs.STRATEGY_ID,
                           candidates=len(symbols),
                           status=scan_cycle.STATUS_OK, run_id=generation_id)
        if declare:
            gen.publish(day, session, generation_id=generation_id,
                        variant=variant, strategy_id=cs.STRATEGY_ID,
                        status=status, candidate_count=len(symbols),
                        completed_at=(completed_at
                                      or datetime.now(timezone.utc).isoformat()))
    return publish


def source(**kw):
    kw.setdefault("trading_day", DAY)
    kw.setdefault("session", SESSION)
    kw.setdefault("modes", live_modes())
    return cs.S6CandidateSource(**kw)


# 1. fresh previous COMPLETED same-session generation served during IN_PROGRESS
class TestSameSessionContinuity:
    def test_a_fresh_previous_generation_is_served_while_a_scan_runs(self, store):
        store(["AAPL", "MSFT"])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB) as held:
            assert held.acquired
            assert sorted(source().symbols()) == ["AAPL", "MSFT"]

    def test_it_says_so(self, store):
        store(["AAPL"])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            described = source().describe()
        assert described.get("refusal") in (None, "")

    # 2. stale previous generation refused
    def test_a_stale_previous_generation_is_refused(self, store):
        old = (datetime.now(timezone.utc)
               - timedelta(seconds=cs._GENERATION_MAX_AGE_SECONDS + 120))
        store(["AAPL"], completed_at=old.isoformat())
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert source().symbols() == []

    def test_the_freshness_bound_is_the_existing_market_data_one(self):
        from s6_live import realtime_features as rf

        assert cs._GENERATION_MAX_AGE_SECONDS == float(
            rf.DEFAULT_MAX_BAR_AGE_SECONDS) == 900.0

    # 4. FAILED generation never consumed
    def test_a_failed_generation_is_never_served(self, store):
        store(["AAPL"], status=gen.STATUS_FAILED)
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert source().symbols() == []

    def test_a_failed_generation_is_not_read_as_zero(self, store):
        """It is the absence of a result, not a result."""
        store(["AAPL"], status=gen.STATUS_FAILED)
        record = gen.current(DAY, SESSION)
        assert record["status"] == gen.STATUS_FAILED
        assert gen.is_consumable(record) is False

    # 10. cross-variant candidate always rejected
    def test_another_variants_generation_is_never_served(self, store):
        store(["AAPL"], variant="S6-R")
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert source().symbols() == []

    def test_is_consumable_checks_day_session_and_variant(self):
        record = {"status": gen.STATUS_COMPLETED, "trading_day": DAY,
                  "session": SESSION, "variant": VARIANT}
        assert gen.is_consumable(record, trading_day=DAY, session=SESSION,
                                 variant=VARIANT) is True
        assert gen.is_consumable(record, variant="S6-R") is False
        assert gen.is_consumable(record, session="REGULAR") is False
        assert gen.is_consumable(record, trading_day="2026-09-02") is False


# 3. IN_PROGRESS partial payload never consumed
class TestPartialOutputIsNeverConsumed:
    def test_rows_from_the_running_scan_are_not_offered(self, store):
        store(["AAPL"], generation_id="gen-1")
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            # The running scan appends its own rows, but declares nothing.
            publisher.publish([Signal("TSLA", run_id="gen-2")],
                              strategy_id=cs.STRATEGY_ID, trading_day=DAY,
                              session=SESSION, variant=VARIANT, run_id="gen-2")
            offered = source().symbols()
        assert "TSLA" not in offered, "a partial generation is not an answer"
        assert offered == ["AAPL"]

    def test_undeclared_rows_alone_are_refused(self, store):
        store(["AAPL"], declare=False)
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert source().symbols() == []


# 5 / 7. newest COMPLETED replaces previous; zero supersedes
class TestTheDeclaredGenerationWins:
    def test_the_newest_completed_generation_replaces_the_previous(self, store):
        store(["AAPL"], generation_id="gen-1")
        store(["NVDA"], generation_id="gen-2")
        assert source().symbols() == ["NVDA"]

    def test_a_completed_zero_generation_supersedes_a_non_zero_one(self, store):
        """generation 20 = 15 candidates, generation 21 = COMPLETED, 0.
        The consumer must see 0."""
        store(["AAPL", "MSFT"], generation_id="gen-20")
        store([], generation_id="gen-21")
        assert source().symbols() == [], (
            "an empty scan writes no rows; without a declaration the "
            "previous generation's candidates stay newest")

    def test_the_zero_generation_is_recorded_explicitly(self, store):
        store([], generation_id="gen-21")
        record = gen.current(DAY, SESSION)
        assert record["status"] == gen.STATUS_COMPLETED
        assert record["candidate_count"] == 0

    # 7. missing producer != completed zero
    def test_a_missing_producer_is_not_a_completed_zero(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "empty"))
        assert gen.current(DAY, SESSION) is None, (
            "no record at all -- the producer has not run")
        described = source().describe()
        assert described.get("refusal")


# 8 / 9. atomicity
class TestPublicationIsAtomic:
    def test_a_torn_record_cannot_become_visible(self, store, tmp_path):
        """os.replace is atomic: a reader sees one record or the other."""
        store(["AAPL"], generation_id="gen-1")
        path = gen.manifest_path(DAY, SESSION)
        first = json.loads(path.read_text())
        store(["NVDA"], generation_id="gen-2")
        second = json.loads(path.read_text())
        assert first["generation_id"] == "gen-1"
        assert second["generation_id"] == "gen-2"
        assert not list(path.parent.glob("*.tmp")), "no temp file left behind"

    def test_a_publication_failure_leaves_the_previous_generation_intact(
            self, store, monkeypatch):
        store(["AAPL"], generation_id="gen-1")
        monkeypatch.setattr(gen.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        assert gen.publish(DAY, SESSION, generation_id="gen-2",
                           variant=VARIANT, candidate_count=9) is False
        record = gen.current(DAY, SESSION)
        assert record["generation_id"] == "gen-1", (
            "the previous completed generation must be untouched")
        assert source().symbols() == ["AAPL"]

    def test_the_record_is_written_after_the_rows(self):
        """Declared last, so a generation is visible only once complete."""
        source_text = (REPO_ROOT / "scanners/runner.py").read_text()
        block = source_text[source_text.index("rows = candidate_publisher.publish("):]
        assert block.index("generations.publish(") > 0
        assert "candidate_count=len(rows)" in block[:2000]


# 11 / 12. the contract this must NOT change
class TestWhatMustNotChange:
    def test_serving_a_generation_does_not_make_it_ready(self):
        """Precision Watch still revalidates current market data."""
        from s6_live import precision_watch as pw

        for condition in (pw.C_MARKET_DATA_ASOF, pw.C_MARKET_DATA_FRESH,
                          pw.C_PRICE, pw.C_VWAP_AVAILABLE, pw.C_EMA_AVAILABLE,
                          pw.C_VOLUME_VALID, pw.C_VOLUME_EXPANSION,
                          pw.C_BREAKOUT, pw.C_EXTENSION, pw.C_REENTRY):
            assert condition in pw.CONDITION_ORDER

    def test_no_strategy_threshold_moved(self):
        from config import s6_sessions as s6

        assert s6.REGULAR_ORB_MINUTES == 15
        assert s6.VARIANT_BY_SESSION["OVERNIGHT_DAYTIME"] == "S6-O"
        assert s6.VARIANT_BY_SESSION["REGULAR"] == "S6-R"

    def test_the_generation_module_places_no_orders(self):
        text = (REPO_ROOT / "scanners/publish/generations.py").read_text()
        for forbidden in ("submit_order", "KISBroker", "broker"):
            assert forbidden not in text
