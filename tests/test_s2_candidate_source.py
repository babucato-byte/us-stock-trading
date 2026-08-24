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


def live_modes():
    """A mode table in which S2 IS live.

    Supplied explicitly because S2 stood down to DISCOVERY_ONLY when S6
    took the fast-turnover slot. These tests are about what the SOURCE
    does -- ranking, staleness, session isolation, qualification -- not
    about which strategy is live this week, so they state the posture
    they exercise rather than inheriting today's.
    """
    from config import scanner_live_mode

    modes = dict(scanner_live_mode.SCANNER_LIVE_MODE)
    modes["accumulation"] = scanner_live_mode.MODE_LIMITED_LIVE
    return modes


def source(**kw):
    kw.setdefault("trading_day", DAY)
    kw.setdefault("session", "REGULAR")
    kw.setdefault("modes", live_modes())
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
                                   session="REGULAR", modes=live_modes())
        assert src.symbols() == ["AAA"], "its own day is fine"

        stale = cs.S2CandidateSource(trading_day=DAY, session="REGULAR",
                                     modes=live_modes())
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
                                      session="REGULAR", modes=live_modes())
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

    def test_a_release_with_no_shared_store_is_refused(self, monkeypatch,
                                                       tmp_path):
        """The release path is a tmp_path, not the real production one.

        This used to name `/home/ubuntu/releases/us-stock-trading/abc123`
        literally. On a laptop that resolves to nothing and the refusal
        fires; on the production host its sibling
        `.../shared/state/candidates` genuinely EXISTS, so `candidate_dir`
        correctly returned it and the test failed on the one machine
        whose layout it was describing. A test that asserts "there is no
        shared store here" has to own the directory it is talking about.
        """
        monkeypatch.delenv(publisher.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.delenv("SCANNER_ANALYTICS_DIR", raising=False)
        release = tmp_path / "releases" / "us-stock-trading" / "abc123"
        release.mkdir(parents=True)
        assert not release.parent.joinpath(
            *publisher.SHARED_STATE_PARTS).exists()
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(release))
        with pytest.raises(publisher.CandidateHandoffMisconfigured,
                           match="no shared store"):
            publisher.candidate_dir()

    def test_a_release_beside_a_shared_store_resolves_to_it(self, monkeypatch,
                                                            tmp_path):
        """`<releases>/<sha>` -> `<releases>/shared/state/candidates`, the
        same sibling resolution the KIS and S1 stores already use."""
        monkeypatch.delenv(publisher.CANDIDATE_DIR_ENV, raising=False)
        shared = tmp_path / "shared" / "state" / "candidates"
        shared.mkdir(parents=True)
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "abc123"))
        assert publisher.candidate_dir() == shared

    def test_an_explicit_shared_directory_is_accepted(self, monkeypatch,
                                                      tmp_path):
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path))
        monkeypatch.setenv(
            "TRADING_PROJECT_ROOT",
            "/home/ubuntu/releases/us-stock-trading/abc123")
        assert publisher.candidate_dir() == tmp_path

    def test_a_checkout_without_a_shared_store_is_refused(self, monkeypatch,
                                                           tmp_path):
        """This is the case that broke production, and it used to pass.

        The reasoning was "the scanner runtime is a working checkout and
        needs no env", so `candidate_dir()` fell back to
        `analytics_dir()/candidates`. On the host that resolved to
        `/home/ubuntu/trading/logs/scanners/candidates` while the trading
        runtime read `.../shared/state/candidates` -- a producer writing
        where no consumer looks, with no error on either side.
        """
        monkeypatch.delenv(publisher.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "a"))
        with pytest.raises(publisher.CandidateHandoffMisconfigured):
            publisher.candidate_dir()

    def test_nothing_configured_at_all_is_refused(self, monkeypatch):
        monkeypatch.delenv(publisher.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.delenv("TRADING_PROJECT_ROOT", raising=False)
        with pytest.raises(publisher.CandidateHandoffMisconfigured,
                           match="refusing to guess"):
            publisher.candidate_dir()


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
                                      session="REGULAR", modes=live_modes())
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
        regular = cs.S2CandidateSource(trading_day=DAY, session="REGULAR",
                                       modes=live_modes())
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


#: Every method the shared BUY cycle actually calls on a source.
#:
#: Derived from the call sites in kis_live_trading, not from memory --
#: `qualify` was missed precisely because reading the class definition
#: showed symbols()/allowed_symbols()/describe() and the fourth method is
#: only reached once a real candidate exists. It cost a live session.
SOURCE_CONTRACT = ("symbols", "allowed_symbols", "describe", "qualify")


class TestTheSourceContractIsFixedInCode:
    """A source missing a method must fail HERE, not at the first
    candidate. The gap was invisible for as long as S2 found nothing,
    which is the worst possible time for it to surface."""

    def test_the_contract_matches_what_the_cycle_calls(self):
        """Derived from the source file, so adding a call to the cycle
        without adding it here fails."""
        import ast

        cycle = (REPO_ROOT / "kis_live_trading.py").read_text()
        called = set()
        for node in ast.walk(ast.parse(cycle)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "source"):
                called.add(node.func.attr)
        assert called <= set(SOURCE_CONTRACT), (
            f"the cycle calls {called - set(SOURCE_CONTRACT)} on a source; "
            "add it to SOURCE_CONTRACT and implement it on every source")

    @pytest.mark.parametrize("method", SOURCE_CONTRACT)
    def test_s1s_source_satisfies_the_contract(self, method):
        from s1_live import candidate_source as s1cs

        assert callable(getattr(s1cs.S1CandidateSource, method, None)), method

    @pytest.mark.parametrize("method", SOURCE_CONTRACT)
    def test_s2s_source_satisfies_the_contract(self, method):
        assert callable(getattr(cs.S2CandidateSource, method, None)), method

    @pytest.mark.parametrize("method", SOURCE_CONTRACT)
    def test_the_refusal_source_satisfies_the_contract(self, method):
        """It is handed to the cycle like any other source."""
        assert callable(getattr(cs.RefusedSource, method, None)), method

    @pytest.mark.parametrize("method", SOURCE_CONTRACT)
    def test_the_legacy_source_satisfies_the_contract(self, method):
        from s1_live import candidate_source as s1cs

        assert callable(getattr(s1cs.LegacyWatchlistSource, method, None))

    def test_a_source_missing_a_method_is_caught(self):
        """The check that would have failed before the live session."""
        class Incomplete:
            name = "incomplete"

            def symbols(self):
                return []

            def allowed_symbols(self):
                return frozenset()

            def describe(self):
                return {}

        missing = [m for m in SOURCE_CONTRACT
                   if not callable(getattr(Incomplete, m, None))]
        assert missing == ["qualify"]


class TestS2Qualification:
    def test_a_published_candidate_qualifies(self, published):
        published(["ABC"])
        q = source().qualify("ABC")
        assert q.qualified is True
        assert q.strategy_id == "S2_VOLUME_ACCUMULATION_V1"
        assert q.price == 100.0
        assert q.source_signal_id == "s-ABC"
        assert q.entry_reason == "s2_volume_accumulation_candidate"

    def test_a_symbol_that_is_not_a_candidate_is_refused(self, published):
        published(["ABC"])
        q = source().qualify("NOTME")
        assert q.qualified is False
        assert q.reason_code == "NOT_AN_S2_CANDIDATE"

    def test_the_wrong_trading_day_is_refused(self, published):
        published(["ABC"], day="2026-08-19")
        q = cs.S2CandidateSource(trading_day=DAY, session="REGULAR",
                                 modes=live_modes()).qualify("ABC")
        assert q.qualified is False

    def test_the_wrong_session_is_refused(self, published):
        published(["ABC"], session="AFTER_HOURS")
        q = source().qualify("ABC")
        assert q.qualified is False

    def test_a_row_without_a_usable_price_is_refused(self):
        from s2_live import qualification

        q = qualification.qualify_s2("ABC", candidate_row={
            "strategy_id": "S2_VOLUME_ACCUMULATION_V1", "price": None,
            "provenance": {"signal_id": "s"}})
        assert q.reason_code == "UNUSABLE_CANDIDATE_ROW"

    def test_another_strategys_row_is_refused(self):
        from s2_live import qualification

        q = qualification.qualify_s2("ABC", candidate_row={
            "strategy_id": "S1_HMA_EARLY_TREND_V1", "price": 100.0,
            "provenance": {"signal_id": "s"}})
        assert q.reason_code == "CANDIDATE_BELONGS_TO_ANOTHER_STRATEGY"

    def test_no_second_score_is_applied(self):
        """Requiring the legacy score too would make the thing that
        trades "S2 AND legacy score"."""
        import inspect

        body = inspect.getsource(cs.S2CandidateSource.qualify)
        assert "analyze" in body and "score_threshold" in body
        assert "qualify_s2" in body

    def test_the_refused_source_qualifies_nothing(self):
        q = cs.RefusedSource("because").qualify("ABC")
        assert q.qualified is False


class TestTheBabaCase:
    """The row that actually reached production on 2026-08-21.

    NOT a claim that these values should be bought. The negative
    price_change_pct is normal: S2 has an 8% ceiling and deliberately no
    floor, and whether to buy is decided later by execution-time
    confirmation. What this fixes is that a real row travels from
    publication through qualification into the shared cycle without a
    runtime error.
    """

    def row(self):
        return {
            "strategy_id": "S2_VOLUME_ACCUMULATION_V1", "symbol": "BABA",
            "rank": 1, "score": 20.538396739356727,
            "price": 122.27559661865234, "volume_multiple": 1.7119950507877528,
            "price_change_pct": -6.323758705155025, "session": "REGULAR",
            "trading_day": "2026-08-21",
            "provenance": {"signal_id": "s-BABA", "scanner_name": "accumulation"},
        }

    def test_it_qualifies_without_a_runtime_error(self):
        from s2_live import qualification

        q = qualification.qualify_s2("BABA", candidate_row=self.row())
        assert q.qualified is True
        assert q.price == pytest.approx(122.2756, abs=1e-3)
        assert q.score == pytest.approx(20.5384, abs=1e-3)

    def test_a_negative_price_change_does_not_disqualify_the_candidate(self):
        """The scanner has no floor. Changing that to avoid this row
        would be tuning the strategy to today's data."""
        import json

        config = json.loads(
            (REPO_ROOT / "scanners" / "accumulation" / "config.json").read_text())
        assert "price_change_min_pct" not in config["params"]
        assert config["params"]["price_change_max_pct"] == 8.0

    def test_buying_is_still_decided_later(self):
        """Qualification says "this is a candidate", not "buy it"."""
        from s2_live import entry_policy, qualification

        q = qualification.qualify_s2("BABA", candidate_row=self.row())
        assert q.qualified is True

        class F:
            hma200, hma200_slope = 95.0, 0.4

        # Price has not confirmed: still at the signal price.
        verdict = entry_policy.confirm(current_price=q.price,
                                       signal_price=q.price,
                                       session="REGULAR", features=F())
        assert verdict.allowed is False
        assert verdict.reason == entry_policy.REASON_PRICE_NOT_CONFIRMED
