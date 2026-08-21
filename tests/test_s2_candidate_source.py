"""S2 joins the shared buy cycle as a SOURCE, not as a second pipeline.

The rule comes from `run_live_buy_entry_cycle` itself: only the source is
pluggable, and "a second candidate source must never mean a second
pipeline: two pipelines are two ideas of what is safe, and they diverge
silently." So the tests here are about the two questions a source is
allowed to answer, and about refusing cleanly rather than raising --
an exception from a source would abort the cycle and take S1's entries
down with it.

Each refusal is checked separately because an operator reading "no S2
candidates" needs to know which one it was. A stood-down strategy and a
quiet market must not look the same.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s2_live import candidate_source as cs  # noqa: E402
from scanners.publish import candidates as publisher  # noqa: E402

DAY = "2026-08-20"


class Signal:
    def __init__(self, symbol, score):
        self.symbol, self.scanner_score, self.signal_price = symbol, score, 100.0
        self.scanner_name, self.scanner_version = "accumulation", "v1"
        self.signal_id, self.scanner_run_id = f"s-{symbol}", "run"
        self.volume = self.avg_volume = self.volume_multiple = None
        self.price_change_pct = self.hma200 = self.hma200_slope = None
        self.hma89 = self.vwap = None
        self.market_data_provider = self.market_data_feed = None
        self.data_timestamp = self.feature_timestamp = None
        self.source_timeframe = self.timestamp = None
        self.reasons, self.metrics = [], {}


class Rollout:
    def __init__(self, allowed=None):
        self.allowed_symbols = frozenset(allowed or [])


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "c"))

    def publish(symbols, day=DAY, session="REGULAR"):
        publisher.publish([Signal(s, 90.0 - i) for i, s in enumerate(symbols)],
                          strategy_id=cs.STRATEGY_ID, trading_day=day,
                          session=session)
    return publish


def source(**kw):
    kw.setdefault("trading_day", DAY)
    kw.setdefault("session", "REGULAR")
    return cs.S2CandidateSource(**kw)


class TestItAnswersOnlyTheTwoSourceQuestions:
    def test_it_offers_the_published_symbols_in_rank_order(self, published):
        published(["AAA", "BBB", "CCC"])
        assert source().symbols() == ["AAA", "BBB", "CCC"]
        assert source().allowed_symbols() == {"AAA", "BBB", "CCC"}

    def test_it_exposes_no_submit_path(self):
        """A source answers questions. It does not place orders."""
        for name in ("submit", "submit_order", "buy", "place_order",
                     "submit_fn"):
            assert not hasattr(cs.S2CandidateSource, name), name

    def test_it_imports_no_broker(self):
        banned = {"kis_broker", "brokers", "execution_engine",
                  "kis_live_trading", "kis_broker_adapter"}
        src = (REPO_ROOT / "s2_live" / "candidate_source.py").read_text()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, f"imports {name}"

    def test_it_matches_the_shape_s1s_source_uses(self):
        """So the buy cycle needs no branch for which strategy it serves
        -- a branch there would be the second pipeline."""
        from s1_live import candidate_source as s1cs

        for method in ("symbols", "allowed_symbols", "describe"):
            assert callable(getattr(cs.S2CandidateSource, method))
            assert callable(getattr(s1cs.S1CandidateSource, method))
        assert isinstance(cs.S2CandidateSource.name, str)


class TestEachRefusalIsItsOwn:
    def test_a_stood_down_strategy_refuses_with_its_own_reason(self, published):
        from config import scanner_live_mode

        published(["AAA"])
        modes = dict(scanner_live_mode.SCANNER_LIVE_MODE)
        modes["accumulation"] = scanner_live_mode.MODE_DISCOVERY_ONLY
        src = source(modes=modes)
        assert src.symbols() == []
        assert "not LIMITED_LIVE" in src.describe()["refusal"]

    @pytest.mark.parametrize("session", ["PREMARKET", "AFTER_HOURS",
                                         "OVERNIGHT_DAYTIME", None])
    def test_a_session_outside_the_rollout_refuses(self, published, session):
        published(["AAA"], session=session or "REGULAR")
        src = source(session=session)
        assert src.symbols() == []
        assert "not enabled" in src.describe()["refusal"]

    def test_a_quiet_market_refuses_differently_from_a_stood_down_one(
            self, published):
        """Both give zero symbols, and they are not the same event."""
        src = source()
        assert src.symbols() == []
        # Now distinguishes the two zero states; see
        # TestQuietIsNotTheSameAsAbsent. With no run marker written, this
        # is the missing-producer case.
        assert src.describe()["refusal"] == cs.NO_PRODUCER_RUN

    def test_yesterdays_rows_are_not_reused(self, published):
        published(["AAA"], day="2026-08-19")
        src = cs.S2CandidateSource(trading_day="2026-08-19",
                                   session="REGULAR")
        assert src.symbols() == ["AAA"], "its own day is fine"

        stale = cs.S2CandidateSource(trading_day=DAY, session="REGULAR")
        assert stale.symbols() == []

    def test_an_unreadable_file_is_empty_not_an_exception(self, monkeypatch):
        """An exception here would abort the shared cycle and take S1's
        entries down with it."""
        monkeypatch.setattr(publisher, "read",
                            lambda *a, **k: (_ for _ in ()).throw(OSError))
        src = source()
        assert src.symbols() == []
        assert src.allowed_symbols() == frozenset()
        assert "could not be read" in src.describe()["refusal"]

    def test_another_strategys_rows_are_ignored(self, published, tmp_path,
                                                monkeypatch):
        publisher.publish([Signal("S1SYM", 90.0)],
                          strategy_id="S1_HMA_EARLY_TREND_V1",
                          trading_day=DAY, session="REGULAR")
        published(["S2SYM"])
        assert source().symbols() == ["S2SYM"]


class TestTheOperatorListOnlyTightens:
    def test_it_intersects_rather_than_replaces(self, published):
        published(["AAA", "BBB"])
        src = source(rollout=Rollout({"AAA", "ZZZ"}))
        assert src.allowed_symbols() == {"AAA"}, "intersection, not union"

    def test_an_empty_operator_list_does_not_tighten(self, published):
        published(["AAA", "BBB"])
        assert source(rollout=Rollout()).allowed_symbols() == {"AAA", "BBB"}


class TestTheDescriptionIsAuditable:
    def test_it_records_what_happened(self, published):
        published(["AAA", "BBB"])
        described = source().describe()
        assert described["source"] == cs.SOURCE_S2
        assert described["strategy_id"] == cs.STRATEGY_ID
        assert described["session"] == "REGULAR"
        assert described["candidates"] == 2
        assert described["refusal"] is None

    def test_a_refusal_is_named_in_the_description(self):
        assert source().describe()["refusal"] is not None


class TestS2GetsTheSameGatesAsS1:
    """The gap this class exists for.

    Two gates in the shared cycle were keyed on "is this the S1 source":
    the COMMON_STOCK classification and the KIS day-range execution-price
    check. That was the same thing as "is this a strategy" until S2
    existed. Left alone, S2 would have reached a real order WITHOUT the
    COMMON_STOCK gate and with the legacy 0.30% deviation check instead
    of the day-range one -- a new strategy silently getting weaker
    protection than the one it was modelled on.
    """

    class Source:
        def __init__(self, name):
            self.name = name

    def test_both_strategy_sources_are_recognised(self):
        import kis_live_trading as klt
        from s1_live import candidate_source as s1cs

        assert klt.is_strategy_source(self.Source(s1cs.SOURCE_S1)) is True
        assert klt.is_strategy_source(self.Source(cs.SOURCE_S2)) is True

    def test_the_legacy_watchlist_keeps_its_own_behaviour(self):
        """It ships with an operator-curated allow-list and the 0.30%
        check; changing that was never in scope."""
        import kis_live_trading as klt

        assert klt.is_strategy_source(self.Source("legacy_watchlist")) is False

    def test_an_unrecognised_source_gets_the_legacy_path(self):
        """Fails closed toward the behaviour a source was written
        against, rather than gates it has never been tested with."""
        import kis_live_trading as klt

        assert klt.is_strategy_source(self.Source("something_new")) is False
        assert klt.is_strategy_source(object()) is False

    def test_the_common_stock_gate_is_not_keyed_on_s1_any_more(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "== s1_candidate_source.SOURCE_S1" not in source, \
            "a gate still keyed on S1 alone would skip for S2"
        assert source.count("is_strategy_source(source)") >= 2


class TestStrategyAwareResolution:
    def test_s1_resolves_through_its_own_unchanged_resolver(self):
        """Delegated, not reimplemented -- S1 keeps running the code it
        has been running rather than one that happens to agree today."""
        import inspect

        body = inspect.getsource(cs.resolve_for_strategy)
        assert "s1_source.resolve(" in body

    def test_s2_resolves_to_its_own_source(self, published):
        published(["AAA"])
        src = cs.resolve_for_strategy(cs.STRATEGY_ID, trading_day=DAY,
                                      session="REGULAR")
        assert src.name == cs.SOURCE_S2
        assert src.symbols() == ["AAA"]

    def test_an_unknown_strategy_gets_nothing(self):
        """A default here would mean a typo silently trading somebody
        else's candidates."""
        src = cs.resolve_for_strategy("S9_TYPO", trading_day=DAY)
        assert src.symbols() == []
        assert src.allowed_symbols() == frozenset()
        assert "unknown strategy" in src.describe()["refusal"]

    def test_a_missing_trading_day_refuses_rather_than_guessing(self):
        src = cs.resolve_for_strategy(cs.STRATEGY_ID, trading_day=None)
        assert src.symbols() == []
        assert "refusing to guess" in src.describe()["refusal"]

    def test_resolution_never_raises(self):
        """S1's entries run through the same cycle; an exception raised
        on S2's behalf would stop them."""
        for sid in (None, "", 7, "S9", cs.STRATEGY_ID):
            src = cs.resolve_for_strategy(sid, trading_day=DAY,
                                          session="REGULAR")
            assert src.symbols() == [] or isinstance(src.symbols(), list)

    def test_a_refused_source_satisfies_the_interface(self):
        src = cs.RefusedSource("because", strategy_id="S9")
        assert src.symbols() == []
        assert src.allowed_symbols() == frozenset()
        assert src.candidate_row("ABC") is None
        assert src.describe()["refusal"] == "because"


class TestTheHandoffDirectoryMustBeShared:
    """The gap that made the whole path silently dead.

    The scanner runtime wrote to its own checkout and the trading runtime
    read a directory INSIDE the release -- different paths, and the
    reader's changes on every deploy. Neither side errored: the scanner
    published successfully, the executor found an empty directory, and
    both reported success.
    """

    def test_a_release_scoped_default_is_refused(self, monkeypatch):
        # Both must be cleared: analytics_dir() prefers
        # SCANNER_ANALYTICS_DIR and only falls back to the project root,
        # so leaving it set would never reach the release path at all.
        monkeypatch.delenv(publisher.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.delenv("SCANNER_ANALYTICS_DIR", raising=False)
        monkeypatch.setenv(
            "TRADING_PROJECT_ROOT",
            "/home/ubuntu/releases/us-stock-trading/abc123")
        with pytest.raises(publisher.CandidateHandoffMisconfigured,
                           match="shared directory"):
            publisher.candidate_dir()

    def test_an_explicit_shared_directory_is_accepted(self, monkeypatch,
                                                      tmp_path):
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path))
        monkeypatch.setenv(
            "TRADING_PROJECT_ROOT",
            "/home/ubuntu/releases/us-stock-trading/abc123")
        assert publisher.candidate_dir() == tmp_path

    def test_a_non_release_default_still_works(self, monkeypatch, tmp_path):
        """The scanner runtime is a working checkout and needs no env."""
        monkeypatch.delenv(publisher.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "a"))
        assert "candidates" in str(publisher.candidate_dir())


class TestQuietIsNotTheSameAsAbsent:
    def test_a_scan_that_ran_and_found_nothing_says_so(self, published):
        publisher.mark_run(DAY, "REGULAR", strategy_id=cs.STRATEGY_ID,
                           candidates=0)
        src = source()
        assert src.symbols() == []
        assert src.describe()["refusal"] == cs.NO_CANDIDATE

    def test_a_missing_producer_says_something_different(self, published):
        src = source()
        assert src.symbols() == []
        assert src.describe()["refusal"] == cs.NO_PRODUCER_RUN

    def test_the_two_refusals_are_not_interchangeable(self):
        """One is waited out; the other is fixed. A shared phrasing is
        how a missing producer waits forever."""
        assert cs.NO_CANDIDATE != cs.NO_PRODUCER_RUN
        assert "producer is missing" in cs.NO_PRODUCER_RUN
        assert "producer" not in cs.NO_CANDIDATE

    def test_the_marker_is_written_even_for_an_empty_scan(self, published):
        from scanners import runner

        class Outcome:
            scanner_name, failed = "accumulation", False
            signals = []

        class Report:
            outcomes = [Outcome()]
            trading_day, session, run_id = DAY, "REGULAR", "run-1"

        assert runner.publish_report_candidates(Report()) == 0
        assert publisher.scan_ran(DAY, "REGULAR") is True


class TestProducerToConsumerEndToEnd:
    """The whole path, with the real publisher and the real source.

    A scanner test alone would have passed throughout the outage: the
    scan worked, the publication worked, and nothing consumed it.
    """

    def test_a_regular_scan_reaches_the_executors_source(self, published):
        from scanners import runner

        class Outcome:
            scanner_name, failed = "accumulation", False
            signals = [Signal("ABC", 88.0), Signal("XYZ", 71.0)]

        class Report:
            outcomes = [Outcome()]
            trading_day, session, run_id = DAY, "REGULAR", "run-1"

        assert runner.publish_report_candidates(Report()) == 2

        src = cs.resolve_for_strategy(cs.STRATEGY_ID, trading_day=DAY,
                                      session="REGULAR")
        assert src.symbols() == ["ABC", "XYZ"], "rank order preserved"
        assert src.describe()["refusal"] is None

        row = src.candidate_row("ABC")
        assert row["strategy_id"] == cs.STRATEGY_ID
        assert row["session"] == "REGULAR"
        assert row["trading_day"] == DAY
        assert row["rank"] == 1
        assert row["provenance"]["candidate_decision"] == "DISABLED"

    def test_an_after_hours_row_is_not_consumed_in_regular(self, published):
        """§3: the executor must not pick up the 16:00 daily scan's
        output during the session."""
        from scanners import runner

        class Outcome:
            scanner_name, failed = "accumulation", False
            signals = [Signal("EVENING", 90.0)]

        class Report:
            outcomes = [Outcome()]
            trading_day, session, run_id = DAY, "AFTER_HOURS", "run-pm"

        runner.publish_report_candidates(Report())
        regular = cs.S2CandidateSource(trading_day=DAY, session="REGULAR")
        assert regular.symbols() == []
        assert "EVENING" not in regular.allowed_symbols()

    def test_no_broker_is_reachable_from_the_producer_path(self):
        import ast

        for module in ("scanners/publish/candidates.py", "scanners/runner.py"):
            source_text = (REPO_ROOT / module).read_text()
            for node in ast.walk(ast.parse(source_text)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [str(getattr(node, "module", "") or "")]
                    names += [a.name for a in node.names]
                    for name in names:
                        for segment in name.split("."):
                            assert segment not in {"kis_broker", "brokers",
                                                   "kis_live_trading"}, name
