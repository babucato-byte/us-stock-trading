"""Only KIS-verified common stocks may be bought.

The gap this closes: a full-universe S1 scan returns ETFs alongside
equities -- a 600-name run produced IUSV, KBE, MILN, BLCV, LEMB, IVOV,
HYGV, JPIE and JPLD -- and nothing in the repository could tell them
apart, because universe.csv carries no security type. Buying one would
have taken on the leveraged/inverse exposure the pilot forbids.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s1_live import security_type as sectype  # noqa: E402
from s1_live import same_day_publisher as publisher  # noqa: E402
from s1_live import same_day_scan as sds  # noqa: E402

#: Fixed, for anything about WHICH DAY a scan belongs to.
NOW = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)

#: Relative, for anything about HOW OLD the security-type master is.
#:
#: `SecurityTypeIndex.validate()` compares the master's `asof` against
#: the real clock, so a fixture stamped with a fixed calendar date ages
#: in place: these tests passed for two weeks and then began failing
#: with "master is 14 days old (limit 14)" on the day the wall clock
#: crossed `MAX_CACHE_AGE_DAYS` past 2026-08-17. Nothing about the code
#: changed. Freshness is a property relative to now, and the fixture has
#: to say so.
#:
#: The limit itself is untouched -- the staleness test below still
#: builds a master deliberately older than it.
FRESH = datetime.now(timezone.utc)


def master(symbols, *, asof=None, source=sectype.SOURCE_KIS_MASTER):
    return {
        "source": source,
        "security_type_asof": (asof or FRESH).isoformat(),
        "counts": {},
        "symbols": symbols,
    }


def entry(symbol, sec_type, *, exchange="NASDAQ", raw="2", etp=None):
    return {"symbol": symbol, "security_type": sec_type, "security_type_raw": raw,
            "etp_type": etp, "exchange_market": exchange}


REAL_WORLD = master({
    "AAPL": entry("AAPL", sectype.COMMON_STOCK),
    "HRL": entry("HRL", sectype.COMMON_STOCK, exchange="NYSE"),
    "OFIX": entry("OFIX", sectype.COMMON_STOCK),
    "SPY": entry("SPY", sectype.ETP, raw="3", etp="ETF", exchange="AMEX"),
    "TQQQ": entry("TQQQ", sectype.ETP, raw="3", etp="ETF"),
    "IUSV": entry("IUSV", sectype.ETP, raw="3", etp="ETF"),
    "KBE": entry("KBE", sectype.ETP, raw="3", etp="ETF", exchange="AMEX"),
    "SPX": entry("SPX", sectype.INDEX, raw="1"),
    "WRNT": entry("WRNT", sectype.WARRANT, raw="4"),
    "OTCX": entry("OTCX", sectype.COMMON_STOCK, exchange="OTC"),
})


def index_from(payload, tmp_path, name="m.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return sectype.load_index(path)


class TestClassificationComesFromTheMaster:
    def test_stocks_are_eligible(self, tmp_path):
        idx = index_from(REAL_WORLD, tmp_path)
        for symbol in ("AAPL", "HRL", "OFIX"):
            verdict = idx.classify(symbol)
            assert verdict.security_type == sectype.COMMON_STOCK
            assert verdict.live_eligible is True
            assert verdict.ineligible_reason() is None

    @pytest.mark.parametrize("symbol", ["SPY", "TQQQ", "IUSV", "KBE"])
    def test_every_etp_is_refused_whole(self, symbol, tmp_path):
        """No ETF/leveraged/inverse sub-classification is attempted --
        none of them may be bought, so the question is never asked."""
        verdict = index_from(REAL_WORLD, tmp_path).classify(symbol)
        assert verdict.security_type == sectype.ETP
        assert verdict.live_eligible is False
        assert verdict.ineligible_reason() == sectype.REASON_NOT_COMMON_STOCK

    @pytest.mark.parametrize("symbol,expected", [
        ("SPX", sectype.INDEX), ("WRNT", sectype.WARRANT)])
    def test_index_and_warrant_are_refused(self, symbol, expected, tmp_path):
        verdict = index_from(REAL_WORLD, tmp_path).classify(symbol)
        assert verdict.security_type == expected
        assert verdict.live_eligible is False

    def test_an_unsupported_exchange_is_refused_even_for_a_stock(self, tmp_path):
        verdict = index_from(REAL_WORLD, tmp_path).classify("OTCX")
        assert verdict.security_type == sectype.COMMON_STOCK
        assert verdict.live_eligible is False
        assert verdict.ineligible_reason() == sectype.REASON_UNSUPPORTED_EXCHANGE

    def test_a_symbol_absent_from_the_master_is_unknown_not_a_stock(self, tmp_path):
        verdict = index_from(REAL_WORLD, tmp_path).classify("NEVERHEARDOF")
        assert verdict.security_type == sectype.UNKNOWN
        assert verdict.live_eligible is False
        assert verdict.ineligible_reason() == sectype.REASON_NOT_IN_MASTER

    def test_an_unrecognised_type_code_is_unknown(self, tmp_path):
        idx = index_from(master({"WEIRD": entry("WEIRD", "SOMETHING_NEW", raw="9")}), tmp_path)
        assert idx.classify("WEIRD").live_eligible is False

    def test_only_common_stock_is_ever_eligible(self):
        assert sectype.LIVE_ELIGIBLE_TYPES == frozenset({sectype.COMMON_STOCK})


class TestTheCacheItselfMustBeTrustworthy:
    def test_a_missing_cache_raises_rather_than_allowing_everything(self, tmp_path):
        with pytest.raises(sectype.SecurityTypeUnavailable):
            sectype.load_index(tmp_path / "absent.json")

    def test_a_stale_master_is_refused(self, tmp_path):
        old = master({"AAPL": entry("AAPL", sectype.COMMON_STOCK)},
                     asof=FRESH - timedelta(days=sectype.MAX_CACHE_AGE_DAYS + 1))
        path = tmp_path / "old.json"
        path.write_text(json.dumps(old))
        with pytest.raises(sectype.SecurityTypeUnavailable) as caught:
            sectype.load_index(path)
        assert sectype.REASON_CACHE_STALE in str(caught.value)

    def test_a_cache_from_another_source_is_refused(self, tmp_path):
        payload = master({"AAPL": entry("AAPL", sectype.COMMON_STOCK)}, source="YFINANCE")
        path = tmp_path / "wrong.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(sectype.SecurityTypeUnavailable) as caught:
            sectype.load_index(path)
        assert sectype.REASON_CACHE_WRONG_SOURCE in str(caught.value)

    def test_an_empty_master_is_refused(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text(json.dumps(master({})))
        with pytest.raises(sectype.SecurityTypeUnavailable):
            sectype.load_index(path)

    def test_yfinance_is_not_the_live_source(self):
        source = (REPO_ROOT / "s1_live" / "security_type.py").read_text()
        assert "quoteType" not in source.split('"""')[2], "must not gate on yfinance"
        assert sectype.SOURCE_KIS_MASTER == "KIS_MASTER"

    def test_no_name_heuristic_is_used(self):
        """A name rule is a guess and the spec forbids it."""
        import ast

        source = (REPO_ROOT / "s1_live" / "security_type.py").read_text()
        tree = ast.parse(source)
        body = [n for n in tree.body if not (isinstance(n, ast.Expr)
                                             and isinstance(n.value, ast.Constant))]
        code = "\n".join(ast.dump(n) for n in body)
        for token in ("english_name", "'ETF' in", "startswith", "endswith"):
            assert token not in code, token


class TestRequireLiveEligible:
    def test_it_returns_the_classification_for_a_stock(self, tmp_path):
        idx = index_from(REAL_WORLD, tmp_path)
        verdict = sectype.require_live_eligible("AAPL", index=idx)
        assert verdict.security_type == sectype.COMMON_STOCK

    @pytest.mark.parametrize("symbol", ["SPY", "TQQQ", "SPX", "WRNT", "OTCX", "NOPE"])
    def test_it_raises_for_everything_else(self, symbol, tmp_path):
        idx = index_from(REAL_WORLD, tmp_path)
        with pytest.raises(sectype.SecurityTypeUnavailable):
            sectype.require_live_eligible(symbol, index=idx)


class Candidate:
    def __init__(self, symbol, score, price=20.0):
        self.symbol, self.score, self.signal_price = symbol, score, price
        self.signal_day, self.trading_day = "2026-08-14", "2026-08-17"
        self.session, self.reasons, self.metrics = "REGULAR", [], {}

    def as_dict(self):
        return {"symbol": self.symbol, "score": self.score,
                "signal_price": self.signal_price}


def scan_with(candidates, status_evaluated=100):
    scan = sds.S1SameDayScan(trading_day="2026-08-17", signal_day="2026-08-14",
                             session="REGULAR")
    scan.candidates = list(candidates)
    scan.evaluated = status_evaluated
    return scan


class TestPublisherDropsIneligibleWithoutReranking:
    def test_etfs_are_dropped_and_stocks_promoted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "store"))
        idx = index_from(REAL_WORLD, tmp_path)
        scan = scan_with([Candidate("IUSV", 90.0), Candidate("AAPL", 80.0),
                          Candidate("KBE", 70.0), Candidate("HRL", 60.0, price=24.0)])
        out = publisher.publish_scan(
            scan, index=idx, market_data_provider="yahoo",
            config_fingerprint="fp", scanner_version="hma_early_trend_v1.0",
            generated_at=NOW)
        assert [r["symbol"] for r in out.published] == ["AAPL", "HRL"]
        assert [r["rank"] for r in out.published] == [1, 2], "renumbered, not reshuffled"
        assert out.drop_reasons() == {sectype.REASON_NOT_COMMON_STOCK: 2}

    def test_the_scans_own_ranking_is_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "store"))
        idx = index_from(REAL_WORLD, tmp_path)
        scan = scan_with([Candidate("HRL", 95.0, price=24.0), Candidate("AAPL", 55.0)])
        out = publisher.publish_scan(
            scan, index=idx, market_data_provider="yahoo", config_fingerprint="fp",
            scanner_version="v1", generated_at=NOW)
        assert [r["symbol"] for r in out.published] == ["HRL", "AAPL"]

    def test_an_unavailable_scan_publishes_nothing(self, tmp_path, monkeypatch):
        """A scan that could not run must not leave a file that reads as
        a genuine zero."""
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "store"))
        idx = index_from(REAL_WORLD, tmp_path)
        broken = scan_with([], status_evaluated=0)
        assert broken.status == sds.STATUS_DATA_UNAVAILABLE
        with pytest.raises(publisher.SameDayPublishRefused):
            publisher.publish_scan(broken, index=idx, market_data_provider="yahoo",
                                   config_fingerprint="fp", scanner_version="v1")

    def test_all_ineligible_writes_no_file_but_reports_why(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "store"))
        idx = index_from(REAL_WORLD, tmp_path)
        scan = scan_with([Candidate("IUSV", 90.0), Candidate("SPY", 80.0)])
        out = publisher.publish_scan(scan, index=idx, market_data_provider="yahoo",
                                     config_fingerprint="fp", scanner_version="v1")
        assert out.count == 0 and out.manifest is None
        assert out.drop_reasons() == {sectype.REASON_NOT_COMMON_STOCK: 2}

    def test_unknown_symbols_are_dropped_not_published(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "store"))
        idx = index_from(REAL_WORLD, tmp_path)
        scan = scan_with([Candidate("MYSTERY", 99.0), Candidate("AAPL", 50.0)])
        out = publisher.publish_scan(scan, index=idx, market_data_provider="yahoo",
                                     config_fingerprint="fp", scanner_version="v1",
                                     generated_at=NOW)
        assert [r["symbol"] for r in out.published] == ["AAPL"]
        assert out.drop_reasons() == {sectype.REASON_NOT_IN_MASTER: 1}

    def test_the_published_file_loads_back_through_the_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "store"))
        from s1_live import store

        idx = index_from(REAL_WORLD, tmp_path)
        scan = scan_with([Candidate("AAPL", 80.0)])
        publisher.publish_scan(scan, index=idx, market_data_provider="yahoo",
                               config_fingerprint="fp",
                               scanner_version="hma_early_trend_v1.0", generated_at=NOW)
        loaded = store.load(expected_trading_day="2026-08-17",
                            expected_scanner="hma_early_trend", expected_provider="yahoo")
        assert loaded is not None
        assert loaded.symbols == frozenset({"AAPL"})

    def test_a_wrong_trading_day_will_not_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S1_LIVE_CANDIDATE_DIR", str(tmp_path / "store"))
        from s1_live import store

        idx = index_from(REAL_WORLD, tmp_path)
        publisher.publish_scan(scan_with([Candidate("AAPL", 80.0)]), index=idx,
                               market_data_provider="yahoo", config_fingerprint="fp",
                               scanner_version="v1", generated_at=NOW)
        assert store.load(expected_trading_day="2026-08-18",
                          expected_scanner="hma_early_trend",
                          expected_provider="yahoo") is None

    def test_the_publisher_only_ever_names_s1(self):
        """S2..S6 must not reach the order path through this module."""
        assert publisher.SCANNER_NAME == "hma_early_trend"
        source = (REPO_ROOT / "s1_live" / "same_day_publisher.py").read_text()
        for other in ("accumulation", "breakout_ready", "gap_pullback",
                      "premarket_momentum"):
            assert other not in source, other


class TestTheOrderPathRechecksSecurityType:
    def test_the_buy_cycle_calls_require_live_eligible(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "s1_security_type.require_live_eligible" in source

    def test_the_recheck_sits_before_the_order_is_built(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        # The call site, not the docstring mention -- otherwise this
        # passes for the wrong reason (the docstring precedes everything).
        gate = source.index("s1_security_type.require_live_eligible")
        for later in ("build_signal(", "submit_buy_order"):
            assert gate < source.index(later), later

    def test_the_gate_is_keyed_on_the_s1_source_and_s1_is_the_live_source(self):
        """Scoping the gate to S1 must not leave a live path without it.

        In the live configuration S1_LIVE_SOURCE_ENABLED=true, so the
        resolved source IS the S1 source and the gate always applies to
        anything that can trade. The legacy source keeps the operator-list
        mechanism it shipped with.
        """
        from s1_live import candidate_source as cs

        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "s1_candidate_source.SOURCE_S1" in source, "gate keyed on the S1 source"
        assert cs.S1CandidateSource.name == cs.SOURCE_S1
        assert cs.SOURCE_S1 == "s1_live"

    def test_a_refusal_records_a_reason_code_rather_than_vanishing(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        # The CALL, not the docstring that describes it -- a bare
        # `index("require_live_eligible")` lands in the module docstring.
        block = source[source.index("s1_security_type.require_live_eligible"):][:1600]
        assert "_persist_blocked_record" in block
        assert "INSTRUMENT_BLOCKED" in block
        assert 'results["skipped"]' in block

    def test_every_refusal_reason_is_distinguishable(self):
        """§8 of the spec: no-candidate, ineligible-instrument, unknown-type
        and insufficient-budget must not collapse into one message."""
        assert len({sectype.REASON_NOT_IN_MASTER, sectype.REASON_NOT_COMMON_STOCK,
                    sectype.REASON_UNSUPPORTED_EXCHANGE, sectype.REASON_CACHE_STALE,
                    sectype.REASON_CACHE_UNAVAILABLE,
                    sectype.REASON_CACHE_WRONG_SOURCE}) == 6

    def test_the_operator_intersection_policy_is_unchanged(self):
        """An empty operator list must not block a verified S1 set, and a
        non-empty one must still restrict it."""
        from s1_live import candidate_source as cs

        source = (REPO_ROOT / "s1_live" / "candidate_source.py").read_text()
        assert "allowed_symbols" in source
        assert hasattr(cs.S1CandidateSource, "allowed_symbols")
