"""What to stream before any candidate exists.

The circular dependency this removes: the collector took its watchlist
from the CURRENT session's published candidates, but discovering a
premarket candidate needs premarket data and premarket data is what the
collector supplies. On 2026-08-28 that produced a collector declining to
start every five minutes while the scanner rejected 593 of 593 symbols
for DATA_ERROR -- a session that looked like it had nothing to trade
when nothing had been measured.

The ceiling is not a preference. One appkey streams 41 symbols; the 42nd
was refused with OPSP0008 MAX SUBSCRIBE OVER. A six-hundred name
universe cannot be watched, so the pool must be chosen well rather than
wide, and chosen without the data it is being chosen to collect.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import bootstrap_watchlist as bootstrap  # noqa: E402
from market_data import kis_hdfscnt0 as wire  # noqa: E402

WRAPPER = (REPO_ROOT / "deploy" / "cron" / "s6_realtime_collector.sh").read_text(
    encoding="utf-8")


class TestTheMeasuredCeiling:
    def test_the_cap_is_recorded_from_measurement(self):
        assert wire.MAX_SUBSCRIPTIONS == 41
        source = (REPO_ROOT / "market_data" / "kis_hdfscnt0.py").read_text(
            encoding="utf-8")
        assert "OPSP0008" in source
        assert "MEASURED" in source

    def test_one_connection_per_appkey_is_recorded_too(self):
        source = (REPO_ROOT / "market_data" / "kis_hdfscnt0.py").read_text(
            encoding="utf-8")
        assert "OPSP8996" in source
        assert wire.ONE_CONNECTION_PER_APPKEY is True

    def test_the_pool_never_exceeds_the_cap(self, monkeypatch):
        monkeypatch.setattr(bootstrap, "prior_session_symbols",
                            lambda **k: [f"P{i}" for i in range(100)])
        monkeypatch.setattr(bootstrap, "manifest_symbols",
                            lambda **k: [f"M{i}" for i in range(100)])
        monkeypatch.setattr(bootstrap, "_exchange_for", lambda s: "NAS")
        pairs, why = bootstrap.build(session="PREMARKET", trading_day="2026-08-28",
                                     prior_session="AFTER_HOURS",
                                     prior_trading_day="2026-08-27")
        assert len(pairs) <= wire.MAX_SUBSCRIPTIONS
        assert why["cap"] == wire.MAX_SUBSCRIPTIONS


class TestItNeverUsesTheCurrentSessionsCandidates:
    def test_the_wrapper_no_longer_reads_published_candidates(self):
        """That read is the circular dependency itself."""
        assert "publisher.read" not in WRAPPER
        assert "bootstrap.build" in WRAPPER

    def test_the_builder_takes_a_PRIOR_session_explicitly(self):
        """It cannot accidentally read the current one: the only session
        it queries is the one the caller names as prior."""
        import inspect

        signature = inspect.signature(bootstrap.build)
        assert "prior_session" in signature.parameters
        assert "prior_trading_day" in signature.parameters

    def test_premarket_seeds_from_the_previous_days_after_hours(self):
        assert '"PREMARKET": ("AFTER_HOURS"' in WRAPPER
        assert "timedelta(days=1)" in WRAPPER

    def test_no_prior_session_still_produces_a_pool(self, monkeypatch):
        """A first run, or a day after a holiday, must not leave the
        collector with nothing to do."""
        monkeypatch.setattr(bootstrap, "manifest_symbols",
                            lambda **k: ["AAA", "BBB"])
        monkeypatch.setattr(bootstrap, "_exchange_for", lambda s: "NAS")
        pairs, why = bootstrap.build(session="PREMARKET",
                                     trading_day="2026-08-28")
        assert [s for s, _e in pairs] == ["AAA", "BBB"]
        assert why["from_prior_session"] == 0


class TestTheMixIsDeliberate:
    def test_prior_candidates_do_not_take_the_whole_pool(self, monkeypatch):
        """A gap is exactly the thing yesterday did not know about, so
        room is left for names that had no reason to be interesting."""
        monkeypatch.setattr(bootstrap, "prior_session_symbols",
                            lambda **k: [f"P{i}" for i in range(100)][:k["limit"]])
        monkeypatch.setattr(bootstrap, "manifest_symbols",
                            lambda **k: [f"M{i}" for i in range(100)][:k["limit"]])
        monkeypatch.setattr(bootstrap, "_exchange_for", lambda s: "NAS")
        _pairs, why = bootstrap.build(session="PREMARKET",
                                      trading_day="2026-08-28",
                                      prior_session="AFTER_HOURS",
                                      prior_trading_day="2026-08-27")
        assert why["from_prior_session"] > 0
        assert why["from_manifest"] > 0

    def test_prior_candidates_come_back_in_rank_order(self, monkeypatch):
        rows = [{"symbol": "C", "rank": 3}, {"symbol": "A", "rank": 1},
                {"symbol": "B", "rank": 2}]
        monkeypatch.setattr("scanners.publish.candidates.read",
                            lambda day, session: rows)
        assert bootstrap.prior_session_symbols(
            trading_day="d", session="AFTER_HOURS", limit=3) == ["A", "B", "C"]

    def test_a_duplicate_is_not_streamed_twice(self, monkeypatch):
        monkeypatch.setattr(bootstrap, "prior_session_symbols",
                            lambda **k: ["AAA"])
        monkeypatch.setattr(bootstrap, "manifest_symbols",
                            lambda limit, exclude=(): [s for s in ["AAA", "BBB"]
                                                       if s not in exclude])
        monkeypatch.setattr(bootstrap, "_exchange_for", lambda s: "NAS")
        pairs, _why = bootstrap.build(session="PREMARKET",
                                      trading_day="2026-08-28",
                                      prior_session="AFTER_HOURS",
                                      prior_trading_day="2026-08-27")
        assert [s for s, _e in pairs] == ["AAA", "BBB"]


class TestItDegradesWithoutInventing:
    def test_an_unusable_manifest_yields_nothing_rather_than_stale_names(self):
        """Seeding the stream with names nobody re-derived today would be
        the staleness the manifest exists to replace."""
        source = (REPO_ROOT / "market_data" / "bootstrap_watchlist.py").read_text(
            encoding="utf-8")
        assert "manifest unusable" in source
        assert "return []" in source

    def test_an_unreadable_prior_session_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(
            "scanners.publish.candidates.read",
            lambda day, session: (_ for _ in ()).throw(RuntimeError("gone")))
        assert bootstrap.prior_session_symbols(
            trading_day="d", session="AFTER_HOURS", limit=5) == []

    def test_a_symbol_that_cannot_be_addressed_is_skipped(self, monkeypatch):
        monkeypatch.setattr(bootstrap, "prior_session_symbols",
                            lambda **k: ["GOOD", "WEIRD"])
        monkeypatch.setattr(bootstrap, "manifest_symbols", lambda **k: [])
        monkeypatch.setattr(bootstrap, "_exchange_for",
                            lambda s: "NAS" if s == "GOOD" else None)
        pairs, _why = bootstrap.build(session="PREMARKET",
                                      trading_day="2026-08-28",
                                      prior_session="AFTER_HOURS",
                                      prior_trading_day="2026-08-27")
        assert [s for s, _e in pairs] == ["GOOD"]

    def test_the_choice_is_explained(self):
        """Whoever reads a premarket funnel needs to know where the pool
        came from, not just how big it was."""
        import inspect

        source = inspect.getsource(bootstrap.build)
        for key in ("from_prior_session", "from_manifest", "total", "cap"):
            assert key in source, key


class TestOneBadSymbolDoesNotCostTheSession:
    """The first live bootstrap produced symbols, then died on a single
    exchange spelling -- `exchange_registry` says "NASDAQ" where the KIS
    wire wants "NAS" -- and the collector never started. Both halves of
    that are fixed: the spelling is known, and an unknown one is skipped
    rather than fatal."""

    def test_every_spelling_the_registry_produces_is_mapped(self):
        for name in ("NASDAQ", "NAS", "NASD"):
            assert wire.tr_key("AAPL", name) == "RBAQAAPL", name
        for name in ("NYSE", "NYS", "NEW YORK STOCK EXCHANGE"):
            assert wire.tr_key("GE", name) == "RBAYGE", name
        for name in ("AMEX", "AMS", "NYSE AMERICAN", "NYSE MKT"):
            assert wire.tr_key("BTG", name) == "RBAABTG", name

    def test_the_delayed_table_covers_the_same_spellings(self):
        assert wire.tr_key("AAPL", "NASDAQ", wire.FEED_DELAYED) == "DNASAAPL"

    def test_a_genuinely_unknown_exchange_is_still_refused(self):
        import pytest

        with pytest.raises(ValueError):
            wire.tr_key("AAPL", "LSE")

    def test_the_collector_skips_rather_than_dies(self):
        runner = (REPO_ROOT / "scripts" / "run_realtime_bar_collector.py").read_text(
            encoding="utf-8")
        block = runner[runner.index("for symbol, exchange in symbols:"):]
        assert "except ValueError" in block[:600]
        assert "continue" in block[:700]

    def test_it_stops_at_the_measured_cap(self):
        runner = (REPO_ROOT / "scripts" / "run_realtime_bar_collector.py").read_text(
            encoding="utf-8")
        assert "subscribed >= wire.MAX_SUBSCRIPTIONS" in runner

    def test_subscribing_nothing_is_reported_not_silent(self):
        runner = (REPO_ROOT / "scripts" / "run_realtime_bar_collector.py").read_text(
            encoding="utf-8")
        assert "no symbol could be subscribed" in runner
