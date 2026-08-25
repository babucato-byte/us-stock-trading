"""The S6 bootstrap asks S6, not S1.

The defect this pins
--------------------
`live_pilot/bootstrap.py` had exactly one candidate source: S1's
published store, re-scored with `paper_strategy_order.analyze_stock`.
Allow-listing an S6 breakout symbol and running the bootstrap therefore
produced `CANDIDATE_SYMBOL_NOT_PUBLISHED` -- correctly, because S6's
symbol is not in S1's published set and never will be. The only ways
past it were to put an S6 symbol into S1's store (trading on reasoning
no strategy produced) or to lower a threshold. Neither is acceptable, so
the source became pluggable instead.
"""

from datetime import datetime, timezone

import pytest

from live_pilot import candidate_sources

NOW = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
COMMIT = "abc123"


class _FakeLiveSource:
    """Stands in for `s6_live.candidate_source.S6CandidateSource`."""

    def __init__(self, rows=(), refusal=None, qualification=None):
        self._rows = list(rows)
        self._refusal = refusal
        self._qualification = qualification

    def candidate_row(self, symbol):
        for row in self._rows:
            if str(row.get("symbol", "")).upper() == symbol.upper():
                return row
        return None

    def symbols(self):
        return [r["symbol"] for r in self._rows]

    def qualify(self, symbol):
        return self._qualification


class _Qualification:
    def __init__(self, *, qualified=True, price=58.5, score=92.09,
                 reason_code=None, detail=""):
        self.qualified = qualified
        self.price = price
        self.score = score
        self.strategy_id = "S6_ORB_BREAKOUT_V1"
        self.entry_reason = "s6_session_range_breakout"
        self.source_signal_id = "sig-s6-1"
        self.reason_code = reason_code
        self.detail = detail


def _source(monkeypatch, live_source):
    src = candidate_sources.S6CandidateSource(
        trading_day="2026-08-25", session="REGULAR", rollout=None,
        valid_for_seconds=120)
    monkeypatch.setattr(src, "_source", lambda: live_source)
    monkeypatch.setattr(
        candidate_sources, "build_kis_instrument",
        lambda s: (type("I", (), {"exchange": "NASDAQ", "symbol": s})(), None))
    monkeypatch.setattr(
        candidate_sources, "build_signal",
        lambda **kw: type("S", (), dict(kw, signal_id="sig-1"))())
    return src


ROW = {"symbol": "LGN", "range_high": 57.95, "range_low": 57.1,
       "variant": "S6-R", "rank": 1}


class TestItReadsS6sOwnRow:
    def test_a_published_qualifying_row_is_selected(self, monkeypatch):
        src = _source(monkeypatch, _FakeLiveSource(
            rows=[ROW], qualification=_Qualification()))
        selection = src.select("LGN", deployed_commit=COMMIT, now=NOW)
        assert selection.signal.strategy_id == "S6_ORB_BREAKOUT_V1"
        assert selection.analysis["score"] == 92.09
        assert selection.analysis["price"] == 58.5

    def test_the_range_travels_with_the_candidate(self, monkeypatch):
        """An S6 position without its range could never detect the
        re-entry its exit policy is built on."""
        src = _source(monkeypatch, _FakeLiveSource(
            rows=[ROW], qualification=_Qualification()))
        selection = src.select("LGN", deployed_commit=COMMIT, now=NOW)
        assert selection.analysis["range_high"] == 57.95
        assert selection.analysis["variant"] == "S6-R"

    def test_no_second_score_is_applied(self, monkeypatch):
        """The published row IS the qualified candidate. Re-scoring it
        with an unrelated analyser would make the thing that trades
        'S6 AND something else'."""
        called = []
        import paper_strategy_order as pso

        monkeypatch.setattr(pso, "analyze_stock",
                            lambda s: called.append(s) or {"score": 0})
        src = _source(monkeypatch, _FakeLiveSource(
            rows=[ROW], qualification=_Qualification()))
        src.select("LGN", deployed_commit=COMMIT, now=NOW)
        assert called == []


class TestItRefusesRatherThanImprovises:
    def test_a_symbol_s6_did_not_publish_is_refused(self, monkeypatch):
        src = _source(monkeypatch, _FakeLiveSource(rows=[ROW]))
        with pytest.raises(candidate_sources.CandidateSourceBlocked) as excinfo:
            src.select("TWO", deployed_commit=COMMIT, now=NOW)
        assert candidate_sources.CANDIDATE_SYMBOL_NOT_PUBLISHED in \
            excinfo.value.reason_codes

    def test_the_sources_own_refusal_is_carried_through(self, monkeypatch):
        """'S6 is not LIMITED_LIVE' and 'the scan found nothing' need
        different operator responses, so the reason is not flattened."""
        src = _source(monkeypatch, _FakeLiveSource(
            rows=[], refusal="S6 is not LIMITED_LIVE"))
        with pytest.raises(candidate_sources.CandidateSourceBlocked) as excinfo:
            src.select("LGN", deployed_commit=COMMIT, now=NOW)
        assert candidate_sources.S6_SOURCE_REFUSED in excinfo.value.reason_codes
        assert "not LIMITED_LIVE" in str(excinfo.value)

    def test_a_row_that_does_not_qualify_is_refused(self, monkeypatch):
        src = _source(monkeypatch, _FakeLiveSource(
            rows=[ROW],
            qualification=_Qualification(
                qualified=False, reason_code="CANDIDATE_HAS_NO_RANGE",
                detail="the row carries no range high")))
        with pytest.raises(candidate_sources.CandidateSourceBlocked) as excinfo:
            src.select("LGN", deployed_commit=COMMIT, now=NOW)
        assert "CANDIDATE_HAS_NO_RANGE" in excinfo.value.reason_codes


class TestTheRouteIsWiredUp:
    def test_both_strategies_have_a_bootstrap_source(self):
        from live_pilot import bootstrap

        assert sorted(bootstrap.SOURCE_FACTORIES) == ["s1", "s6"]

    def test_the_default_is_still_s1(self):
        from config import strategy_registry
        from live_pilot import bootstrap

        source = bootstrap.default_source(rollout=None, now=NOW)
        assert source.slot == strategy_registry.SLOT_S1

    def test_the_s6_factory_builds_an_s6_source(self):
        from config import strategy_registry
        from live_pilot import bootstrap

        source = bootstrap.s6_source(rollout=None, now=NOW, session="REGULAR")
        assert source.slot == strategy_registry.SLOT_S6
