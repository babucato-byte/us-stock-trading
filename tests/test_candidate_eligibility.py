"""Observed candidates and actionable ones, kept apart.

The case this comes from: on 2026-08-21 S6's only candidate was IEFA, an
ETP. The channel read "후보 수: 1" while the number of symbols an
operator could buy was zero, and the BUY gate would have refused it
correctly and silently -- correct behaviour that looks like an
opportunity.

Research candidates are KEPT. Dropping them would bias every study
toward the instruments that happen to be tradeable. They are excluded
from the actionable ranking only.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.notify import monitor  # noqa: E402
from scanners.publish import eligibility as el  # noqa: E402


class FakeIndex:
    def __init__(self, mapping):
        self.mapping = mapping

    def classify(self, symbol):
        if symbol not in self.mapping:
            raise KeyError(symbol)
        kind = self.mapping[symbol]
        return type("C", (), {
            "security_type": kind, "etp_type": "ETF" if kind == "ETP" else None,
            "exchange": "AMEX", "live_eligible": kind == "COMMON_STOCK",
            "asof": "2026-08-21T12:00:00+00:00"})()


INDEX = FakeIndex({"AAPL": "COMMON_STOCK", "MSFT": "COMMON_STOCK",
                   "IEFA": "ETP", "SPX": "INDEX"})


def row(symbol, score, **kw):
    base = {"symbol": symbol, "score": score, "price": 100.86,
            "range_high": 100.80, "range_low": 100.675, "vwap": 100.84,
            "ema9": 100.844, "ema21": 100.836}
    base.update(kw)
    return base


class TestClassification:
    @pytest.mark.parametrize("symbol,kind,eligible", [
        ("AAPL", "COMMON_STOCK", True),
        ("IEFA", "ETP", False),
        ("SPX", "INDEX", False),
    ])
    def test_each_type_is_recorded(self, symbol, kind, eligible):
        result = el.classify_symbol(symbol, index=INDEX)
        assert result["security_type"] == kind
        assert result["live_eligible"] is eligible

    def test_an_unclassifiable_symbol_fails_closed(self):
        """"We could not tell" is not a reason to treat something as
        ordinary stock -- and it is the direction the BUY gate fails in
        too, so a classification outage narrows what is shown."""
        result = el.classify_symbol("NOTLISTED", index=INDEX)
        assert result["security_type"] == el.UNKNOWN_TYPE
        assert result["live_eligible"] is False
        assert "classification_error" in result

    def test_the_eligible_set_is_read_from_the_buy_gate(self):
        """Restating it here would let the two drift into disagreeing
        about what may be traded."""
        from s1_live import security_type

        assert el.live_eligible_types() == frozenset(
            security_type.LIVE_ELIGIBLE_TYPES)


class TestTheTwoPopulations:
    def rows(self):
        return el.enrich([row("IEFA", 62.75), row("AAPL", 55.0),
                          row("SPX", 71.0), row("MSFT", 40.0)], index=INDEX)

    def test_observed_is_everything_not_the_remainder(self):
        """A study of "what the scanner found" that excluded the
        untradeable half would be a study of the tradeable half wearing
        the wrong name."""
        observed, live = el.split(self.rows())
        assert len(observed) == 4
        assert {r["symbol"] for r in live} == {"AAPL", "MSFT"}

    def test_every_row_carries_its_class(self):
        classes = {r["symbol"]: r["candidate_class"] for r in self.rows()}
        assert classes["AAPL"] == el.LIVE_ELIGIBLE
        assert classes["IEFA"] == el.OBSERVED
        assert classes["SPX"] == el.OBSERVED

    def test_research_candidates_are_kept_in_the_data(self):
        assert "IEFA" in {r["symbol"] for r in self.rows()}


class TestTheActionableRanking:
    def test_it_ranks_within_the_eligible_set(self):
        """"1위" must mean first among the ones that could be bought,
        not whatever survived a cut applied after ranking."""
        top = el.top_live(el.enrich(
            [row("SPX", 99.0), row("IEFA", 90.0), row("AAPL", 55.0),
             row("MSFT", 40.0)], index=INDEX))
        assert [r["symbol"] for r in top] == ["AAPL", "MSFT"]

    def test_a_higher_scoring_etp_does_not_displace_a_stock(self):
        top = el.top_live(el.enrich([row("IEFA", 99.0), row("AAPL", 1.0)],
                                    index=INDEX))
        assert [r["symbol"] for r in top] == ["AAPL"]

    def test_no_eligible_candidate_yields_an_empty_ranking(self):
        assert el.top_live(el.enrich([row("IEFA", 62.75)], index=INDEX)) == []


class TestTheDerivedMetrics:
    def test_the_breakout_is_normalised_by_range_width(self):
        """0.06% out of a 0.12%-wide range is a clean break; the same
        0.06% out of a 2%-wide range is noise. IEFA scored full
        entry-proximity marks on exactly that ambiguity."""
        metrics = el.derived_metrics(row("IEFA", 62.75))
        assert metrics["opening_range_width_pct"] == pytest.approx(0.124, abs=1e-2)
        assert metrics["breakout_pct"] == pytest.approx(0.056, abs=1e-2)
        assert metrics["normalized_breakout_by_range"] == pytest.approx(0.45, abs=0.1)

    @pytest.mark.parametrize("field", [
        "opening_range_width_pct", "breakout_pct",
        "normalized_breakout_by_range", "vwap_distance_pct", "ema_spread_pct"])
    def test_every_derived_field_is_present(self, field):
        assert field in el.derived_metrics(row("AAPL", 50.0))

    def test_missing_inputs_produce_none_not_zero(self):
        metrics = el.derived_metrics({"symbol": "X"})
        assert all(v is None for v in metrics.values())

    def test_a_zero_width_range_does_not_divide(self):
        metrics = el.derived_metrics(row("X", 1.0, range_high=100.0,
                                         range_low=100.0))
        assert metrics["normalized_breakout_by_range"] is None


class TestTheMessageSeparatesTheCounts:
    def test_both_counts_are_shown(self):
        text = monitor.format_scan(
            scanner_name="orb", session="REGULAR", trading_day="d",
            scanned=299, candidates=1, status="SUCCESS", variant="S6-R",
            live_candidates=0)
        assert "후보 수: 1" in text
        assert "실거래 가능 후보: 0" in text

    def test_a_research_only_result_says_so(self):
        """The exact case: candidates exist and none is tradeable."""
        text = monitor.format_scan(
            scanner_name="orb", session="REGULAR", trading_day="d",
            scanned=299, candidates=1, status="SUCCESS", live_candidates=0)
        assert "COMMON_STOCK 없음" in text

    def test_a_tradeable_result_does_not_carry_the_warning(self):
        text = monitor.format_scan(
            scanner_name="orb", session="REGULAR", trading_day="d",
            scanned=299, candidates=2, status="SUCCESS", live_candidates=1,
            top=[dict(row("AAPL", 55.0), security_type="COMMON_STOCK")])
        assert "실거래 가능 후보: 1" in text
        assert "COMMON_STOCK 없음" not in text
        assert "실거래 가능 상위 후보: AAPL" in text

    def test_a_research_top_block_is_labelled_as_research(self):
        text = monitor.format_scan(
            scanner_name="orb", session="REGULAR", trading_day="d",
            scanned=299, candidates=1, status="SUCCESS", live_candidates=0,
            top=[dict(row("IEFA", 62.75), security_type="ETP")])
        assert "상위 후보 (연구용): IEFA [ETP]" in text

    def test_a_scan_with_no_live_count_is_unchanged(self):
        """S1 and S2 pass no live count and must render as before."""
        text = monitor.format_scan(
            scanner_name="hma_early_trend", session="REGULAR",
            trading_day="d", scanned=10, candidates=0, status="SUCCESS")
        assert "실거래 가능 후보" not in text


class TestItCannotWidenWhatMayBeTraded:
    def test_the_buy_gate_is_not_bypassed_or_duplicated(self):
        import ast

        source = (REPO_ROOT / "scanners" / "publish" / "eligibility.py").read_text()
        banned = {"kis_broker", "brokers", "execution", "order_gate",
                  "kis_live_trading"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, name

    def test_a_classification_outage_narrows_rather_than_widens(self, monkeypatch):
        class Broken:
            def classify(self, symbol):
                raise RuntimeError("master unavailable")

        rows = el.enrich([row("AAPL", 55.0)], index=Broken())
        assert rows[0]["live_eligible"] is False
        assert el.top_live(rows) == []
