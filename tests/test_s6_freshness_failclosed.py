"""The eight ways an S6 candidate must not reach a BUY.

Each is a way stale or foreign data could be traded on, and each has to
fail CLOSED -- the source yields nothing rather than yielding something
the gate downstream might accept. The two zero cases are separated
because they demand opposite responses: a clean zero is waited out, a
missing producer is fixed, and they look identical in a count.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import scanner_live_mode as slm  # noqa: E402
from s6_live import candidate_source as cs  # noqa: E402
from scanners.publish import candidates as publisher  # noqa: E402

DAY = "2026-08-24"


class Signal:
    def __init__(self, symbol, score=70.0):
        self.symbol, self.scanner_score, self.signal_price = symbol, score, 100.0
        self.scanner_name, self.scanner_version = "orb", "orb_v1.0"
        self.signal_id, self.scanner_run_id = f"s-{symbol}", "run"
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
    monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "c"))

    def publish(symbols, day=DAY, session="REGULAR", variant="S6-R"):
        publisher.publish([Signal(s) for s in symbols],
                          strategy_id=cs.STRATEGY_ID, trading_day=day,
                          session=session, variant=variant)
    return publish


def source(**kw):
    kw.setdefault("trading_day", DAY)
    kw.setdefault("session", "REGULAR")
    kw.setdefault("modes", live_modes())
    return cs.S6CandidateSource(**kw)


class TestTheEightCases:
    def test_A_a_fresh_candidate_is_consumable(self, store):
        store(["AAPL"])
        src = source()
        assert src.symbols() == ["AAPL"]
        assert src.describe()["refusal"] is None
        assert src.qualify("AAPL").qualified is True

    def test_B_a_stale_candidate_is_blocked(self, store):
        """Published for a day that is not this one."""
        store(["AAPL"], day="2026-08-21")
        assert source().symbols() == []
        assert source().qualify("AAPL").qualified is False

    def test_C_a_wrong_trading_day_is_blocked(self, store):
        store(["AAPL"], day=DAY)
        stale = cs.S6CandidateSource(trading_day="2026-08-25",
                                     session="REGULAR", modes=live_modes())
        assert stale.symbols() == []

    def test_D_a_session_mismatch_is_blocked(self, store):
        """An AFTER_HOURS row must not be consumed during REGULAR: it is
        a breakout of a range nobody in this session traded against."""
        store(["AAPL"], session="AFTER_HOURS", variant="S6-A")
        assert source().symbols() == []

    def test_E_a_variant_mismatch_is_blocked(self, store):
        """Same session file, wrong variant -- a path is not a
        guarantee, so the variant is re-checked."""
        store(["AAPL"], session="REGULAR", variant="S6-O")
        src = source()
        assert src.symbols() == []
        assert "S6-R" in src.describe()["refusal"]

    def test_F_a_missing_run_marker_reports_producer_missing(self, store):
        """No scan ran. Waiting will not help."""
        src = source()
        assert src.symbols() == []
        assert src.describe()["refusal"] == cs.NO_PRODUCER_RUN

    def test_G_a_marked_run_with_no_candidates_is_a_clean_zero(self, store):
        """The scan ran and nothing qualified. Wait."""
        publisher.mark_run(DAY, "REGULAR", strategy_id=cs.STRATEGY_ID,
                           candidates=0)
        src = source()
        assert src.symbols() == []
        assert src.describe()["refusal"] == cs.NO_CANDIDATE

    def test_H_a_previous_sessions_candidate_is_not_reused(self, store):
        """Yesterday's REGULAR row must not serve today's REGULAR."""
        store(["OLD"], day="2026-08-21", session="REGULAR")
        store(["NEW"], day=DAY, session="REGULAR")
        assert source().symbols() == ["NEW"]
        assert "OLD" not in source().allowed_symbols()

    def test_the_two_zero_cases_are_distinguishable(self, store):
        """They look identical in a count and demand opposite
        responses -- which is how S6 sat at NO_CANDIDATE for a session
        with no producer at all."""
        assert cs.NO_CANDIDATE != cs.NO_PRODUCER_RUN
        assert "producer is missing" in cs.NO_PRODUCER_RUN


class TestDataFailuresFailClosed:
    def test_a_malformed_candidate_file_yields_nothing(self, store, tmp_path):
        store(["AAPL"])
        path = publisher.candidates_path(DAY, "REGULAR")
        path.write_text("{not json\n", encoding="utf-8")
        assert source().symbols() == []

    def test_an_unreadable_directory_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(publisher, "read",
                            lambda *a, **k: (_ for _ in ()).throw(OSError))
        src = source()
        assert src.symbols() == []
        assert "could not be read" in src.describe()["refusal"]

    def test_a_row_without_a_range_does_not_qualify(self, store):
        """The exit's primary signal is re-entry INTO the range; a
        position opened without one could never produce it."""
        from s6_live import qualification

        verdict = qualification.qualify_s6("AAPL", candidate_row={
            "strategy_id": cs.STRATEGY_ID, "price": 100.0,
            "provenance": {"signal_id": "s"}})
        assert verdict.qualified is False
        assert verdict.reason_code == "CANDIDATE_HAS_NO_RANGE"

    def test_a_row_without_a_usable_price_does_not_qualify(self):
        from s6_live import qualification

        verdict = qualification.qualify_s6("AAPL", candidate_row={
            "strategy_id": cs.STRATEGY_ID, "price": None, "range_high": 99.5,
            "provenance": {"signal_id": "s"}})
        assert verdict.qualified is False

    def test_a_missing_security_master_narrows_what_is_actionable(self):
        """The classification outage direction: fewer symbols shown as
        tradeable, never more."""
        from scanners.publish import eligibility

        class Broken:
            def classify(self, symbol):
                raise RuntimeError("master unavailable")

        rows = eligibility.enrich([{"symbol": "AAPL", "score": 70.0}],
                                  index=Broken())
        assert rows[0]["live_eligible"] is False
        assert eligibility.top_live(rows) == []

    def test_none_of_these_can_raise_into_the_shared_cycle(self, monkeypatch):
        """S1's entries run through the same cycle; an exception raised
        on S6's behalf would stop them."""
        monkeypatch.setattr(publisher, "read",
                            lambda *a, **k: (_ for _ in ()).throw(OSError))
        for call in (lambda: source().symbols(),
                     lambda: source().allowed_symbols(),
                     lambda: source().describe(),
                     lambda: source().qualify("AAPL")):
            call()


class TestExitsAreNotBlockedByAnyOfThis:
    def test_the_exit_path_reads_no_candidate_file(self):
        """A stale or missing candidate must never stop a held position
        from leaving -- entry fail-closed and exit continuity are
        different rules."""
        import ast

        for module in ("s6_live/exit_policy.py", "s6_live/exit_runtime.py"):
            source_text = (REPO_ROOT / module).read_text()
            for node in ast.walk(ast.parse(source_text)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [str(getattr(node, "module", "") or "")]
                    names += [a.name for a in node.names]
                    for name in names:
                        assert "candidate" not in name.lower(), \
                            f"{module} imports {name}"

    def test_synthetic_success_is_not_a_production_pass(self):
        """§9: these tests prove the CODE fails closed. They do not
        prove the market was observed, and the evaluator must still
        report those checks unmeasured."""
        from s6_live import readiness

        verdict = readiness.evaluate(crontab="")
        for name in readiness.MARKET_DEPENDENT:
            assert verdict.checks[name].status == readiness.NOT_MEASURED
