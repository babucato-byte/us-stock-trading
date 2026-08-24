"""The contract between the scanner node and the trading node.

The property that matters: the trading node must never act on a
manifest it has not checked, and must never STOP because the scanner
node did. A laptop is the optional half of this system -- if its uptime
became a dependency of the order path, the split would have made the
trading node less reliable rather than better informed.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from discovery import manifest as mf  # noqa: E402
from discovery import market_scan  # noqa: E402

DAY = "2026-08-24"
NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)


def _rows(n=3):
    return [{"symbol": f"SYM{i}", "rank": i, "observed_price": 10.0 + i,
             "volume": 1_000_000, "dollar_volume": 10_000_000.0,
             "first_stage_reason": "TODAY_DOLLAR_VOLUME"}
            for i in range(1, n + 1)]


def _doc(**over):
    doc = mf.build(trading_day=DAY, session="REGULAR", symbols=_rows(),
                   scanner_commit="abc123", scan_id="scan-1",
                   universe_size=12886, evaluated=9000,
                   duration_seconds=380.0,
                   generated_at=(NOW - timedelta(minutes=5)).isoformat())
    doc.update(over)
    return doc


class TestTheDocumentSaysWhereItCameFrom:
    def test_it_carries_its_provenance(self):
        doc = _doc()
        assert doc["schema_version"] == mf.SCHEMA_VERSION
        assert doc["source"] == mf.SOURCE_LAPTOP_MARKET_SCAN
        assert doc["scanner_commit"] == "abc123"
        assert doc["universe_size"] == 12886
        assert doc["first_stage_passed"] == 3

    def test_a_manifest_is_never_a_buy_signal(self):
        """It records what was OBSERVED, not what was decided. No field
        here says qualified, approved, or entry."""
        doc = _doc()
        text = json.dumps(doc).lower()
        for forbidden in ("qualified", "approved", "entry", "order", "buy",
                          "submit"):
            assert forbidden not in text, forbidden


class TestItIsWrittenAtomically:
    def test_a_reader_sees_the_old_file_or_the_new_one(self, tmp_path):
        """Never half of one. The scanner takes minutes to produce the
        next manifest, and a truncated document that happens to parse is
        worse than one that does not."""
        path = tmp_path / "manifest.json"
        mf.write(_doc(), path)
        first = mf.read(path)

        mf.write(_doc(symbols=_rows(7)), path)
        second = mf.read(path)

        assert len(first["symbols"]) == 3
        assert len(second["symbols"]) == 7
        assert list(tmp_path.glob(".manifest-*")) == [], "temp file left behind"

    def test_a_failed_write_leaves_no_debris(self, tmp_path, monkeypatch):
        path = tmp_path / "manifest.json"
        mf.write(_doc(), path)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(mf.os, "replace", boom)
        with pytest.raises(OSError):
            mf.write(_doc(symbols=_rows(9)), path)

        assert len(mf.read(path)["symbols"]) == 3      # untouched
        assert list(tmp_path.glob(".manifest-*")) == []


class TestTheTradingNodeChecksEverything:
    def test_a_good_manifest_validates(self):
        v = mf.validate(_doc(), trading_day=DAY, now=NOW)
        assert v["status"] == mf.VALID
        assert len(v["symbols"]) == 3
        assert v["age_seconds"] == pytest.approx(300, abs=1)

    def test_a_missing_manifest_is_not_an_error(self):
        v = mf.validate(None, trading_day=DAY, now=NOW)
        assert v["status"] == mf.MISSING
        assert v["symbols"] == []

    def test_yesterdays_manifest_is_refused(self):
        v = mf.validate(_doc(trading_day="2026-08-21"), trading_day=DAY,
                        now=NOW)
        assert v["status"] == mf.WRONG_TRADING_DAY

    def test_a_stale_manifest_is_refused(self):
        old = _doc(generated_at=(NOW - timedelta(hours=3)).isoformat())
        v = mf.validate(old, trading_day=DAY, now=NOW)
        assert v["status"] == mf.STALE

    def test_a_manifest_from_the_future_is_refused(self):
        """A clock fault on one of the two nodes. Acting on it means
        trusting whichever one is wrong."""
        ahead = _doc(generated_at=(NOW + timedelta(minutes=30)).isoformat())
        assert mf.validate(ahead, trading_day=DAY, now=NOW)["status"] == \
            mf.UNREADABLE

    def test_an_old_schema_is_refused(self):
        v = mf.validate(_doc(schema_version="something_else"),
                        trading_day=DAY, now=NOW)
        assert v["status"] == mf.SCHEMA_MISMATCH

    def test_duplicate_symbols_are_refused(self):
        rows = _rows(2) + [_rows(1)[0]]
        assert mf.validate(_doc(symbols=rows), trading_day=DAY,
                           now=NOW)["status"] == mf.DUPLICATE_SYMBOLS

    def test_an_empty_manifest_is_a_measurement_not_a_fault(self):
        """The market can genuinely offer nothing worth a precision
        scan. That is different from the scanner not running."""
        v = mf.validate(_doc(symbols=[]), trading_day=DAY, now=NOW)
        assert v["status"] == mf.EMPTY
        assert v["status"] != mf.MISSING

    def test_garbage_is_refused_without_raising(self):
        for junk in ("a string", 42, [], {"nope": 1}):
            assert mf.validate(junk, trading_day=DAY, now=NOW)["status"] in (
                mf.UNREADABLE, mf.SCHEMA_MISMATCH)

    def test_a_row_with_no_symbol_is_refused(self):
        assert mf.validate(_doc(symbols=[{"rank": 1}]), trading_day=DAY,
                           now=NOW)["status"] == mf.UNREADABLE

    def test_an_unreadable_file_reads_as_none(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text("{ this is not json")
        assert mf.read(path) is None
        assert mf.read(tmp_path / "absent.json") is None


class TestTheFirstStageRanksLiquidityNotTheDaysMove:
    """The day's gain is what S6's own gates judge. Pre-ranking on it
    would smuggle a momentum filter in ahead of the opening-range test
    that is supposed to make that call."""

    def test_it_ranks_by_todays_dollar_volume(self):
        measured = {
            "BIG": {"price": 10.0, "volume": 30_000_000,
                    "dollar_volume": 300_000_000.0, "avg_volume": 1e7,
                    "relative_volume": 3.0},
            "SMALL": {"price": 10.0, "volume": 1_000_000,
                      "dollar_volume": 10_000_000.0, "avg_volume": 1e6,
                      "relative_volume": 1.0},
        }
        rows = market_scan.rank(measured)
        assert [r["symbol"] for r in rows] == ["BIG", "SMALL"]
        assert rows[0]["rank"] == 1

    def test_a_thin_name_that_tripled_does_not_outrank_a_liquid_one(self):
        """Ten times its usual nothing is still nothing, and it would
        occupy a slot a tradeable name needs."""
        measured = {
            "LIQUID": {"price": 50.0, "volume": 10_000_000,
                       "dollar_volume": 500_000_000.0, "avg_volume": 1e7,
                       "relative_volume": 1.0},
            "THIN": {"price": 2.0, "volume": 4_000_000,
                     "dollar_volume": 8_000_000.0, "avg_volume": 100_000,
                     "relative_volume": 40.0},
        }
        rows = market_scan.rank(measured)
        assert rows[0]["symbol"] == "LIQUID"

    def test_acceleration_is_recorded_as_a_label_only(self):
        measured = {"A": {"price": 10.0, "volume": 3_000_000,
                          "dollar_volume": 30_000_000.0,
                          "avg_volume": 500_000, "relative_volume": 6.0}}
        row = market_scan.rank(measured)[0]
        assert "VOLUME_ACCELERATION" in row["first_stage_reason"]
        assert row["relative_volume"] == 6.0

    def test_illiquid_and_penny_names_are_dropped(self):
        measured = {
            "PENNY": {"price": 0.4, "volume": 100_000_000,
                      "dollar_volume": 40_000_000.0, "avg_volume": 1e7,
                      "relative_volume": 10.0},
            "QUIET": {"price": 100.0, "volume": 1_000,
                      "dollar_volume": 100_000.0, "avg_volume": 1_000,
                      "relative_volume": 1.0},
        }
        assert market_scan.rank(measured) == []

    def test_the_cap_is_not_the_old_three_hundred(self):
        """300 was the previous-day pool size. Reusing it here would
        import a limit that was never chosen for this stage."""
        assert market_scan.DEFAULT_MAX_SYMBOLS != 300

    def test_the_cap_is_honoured(self):
        measured = {f"S{i}": {"price": 10.0, "volume": 10_000_000,
                              "dollar_volume": 100_000_000.0 - i,
                              "avg_volume": 1e6, "relative_volume": 1.0}
                    for i in range(50)}
        assert len(market_scan.rank(measured, max_symbols=7)) == 7


class TestAStaleBarIsNeverRankedAsToday:
    def test_a_symbol_whose_last_bar_is_not_today_is_omitted(self):
        import pandas as pd

        def download(tickers):
            return {"OLD": pd.DataFrame(
                {"Close": [10.0], "Volume": [1_000_000]},
                index=pd.to_datetime(["2026-08-21"]))}

        measured = market_scan.fetch_today(["OLD"], trading_day=DAY,
                                           download=download, pause=0,
                                           backoff=0, max_rounds=1)
        assert measured == {}

    def test_todays_bar_is_measured(self):
        import pandas as pd

        def download(tickers):
            return {"NEW": pd.DataFrame(
                {"Close": [10.0, 12.0], "Volume": [1_000_000, 5_000_000]},
                index=pd.to_datetime(["2026-08-21", DAY]))}

        measured = market_scan.fetch_today(["NEW"], trading_day=DAY,
                                           download=download, pause=0,
                                           backoff=0, max_rounds=1)
        assert measured["NEW"]["dollar_volume"] == 60_000_000.0
        assert measured["NEW"]["relative_volume"] == 5.0

    def test_a_failed_batch_does_not_end_the_scan(self):
        calls = []

        def download(tickers):
            calls.append(tickers)
            raise RuntimeError("provider down")

        assert market_scan.fetch_today(["A", "B"], trading_day=DAY,
                                       download=download, pause=0,
                                       backoff=0, max_rounds=1) == {}
        assert calls, "the batch was attempted"


class TestTheScannerNodeCannotTrade:
    """§1's whole point. The laptop produces a list of symbols; the
    Oracle host places orders. Asserted against the parsed import and
    call graph rather than a promise in a docstring, because a call that
    does not exist cannot be reached by a path nobody thought of."""

    MODULES = ("discovery/manifest.py", "discovery/market_scan.py",
               "scripts/run_market_discovery.py")

    ORDERING_ROOTS = ("brokers", "broker", "execution", "kis_live_trading",
                      "live_pilot", "paper_strategy_order", "order_safety",
                      "order_intent_ledger", "s1_live", "s2_live", "s6_live",
                      "kis_position_manager", "reconciliation")

    ORDERING_CALLS = ("submit_order", "place_order", "authorize_and_execute",
                      "run_live_buy_entry_cycle", "get_orderable_usd",
                      "get_account_snapshot", "get_positions",
                      "get_open_orders", "reserve", "record_submission")

    def _tree(self, relative):
        import ast

        return ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))

    @pytest.mark.parametrize("relative", MODULES)
    def test_it_imports_no_order_module(self, relative):
        import ast

        roots = set()
        for node in ast.walk(self._tree(relative)):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        offending = roots.intersection(self.ORDERING_ROOTS)
        assert not offending, f"{relative} imports {sorted(offending)}"

    @pytest.mark.parametrize("relative", MODULES)
    def test_it_calls_nothing_that_could_order_or_read_the_account(self, relative):
        import ast

        names = set()
        for node in ast.walk(self._tree(relative)):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        offending = names.intersection(self.ORDERING_CALLS)
        assert not offending, f"{relative} calls {sorted(offending)}"

    def test_no_credential_is_read(self):
        """The scanner node holds no KIS credentials, so it must not
        even look for one -- a module that reads them is one deploy away
        from being handed them."""
        for relative in self.MODULES:
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            for secret in ("KIS_APP_KEY", "APP_SECRET", "ACCOUNT_NO",
                           "ALPACA_API_KEY", "access_token"):
                assert secret not in text, f"{relative} mentions {secret}"


class TestAThrottledPassIsLabelledNotHidden:
    """A 12,886-name run priced 4,038 before the provider answered
    YFRateLimitError for the rest. The top 600 of an arbitrary third is
    not the market's top 600, and a manifest that did not say so would
    carry that sampling bias with no way to see it."""

    def test_a_partial_pass_is_usable_but_marked(self):
        doc = _doc(coverage=0.31, complete=False)
        v = mf.validate(doc, trading_day=DAY, now=NOW)
        assert v["status"] == mf.PARTIAL
        assert v["symbols"], "a partial ranking is still usable"
        assert "31%" in v["detail"]

    def test_a_complete_pass_is_valid(self):
        v = mf.validate(_doc(coverage=0.94, complete=True),
                        trading_day=DAY, now=NOW)
        assert v["status"] == mf.VALID
        assert "94%" in v["detail"]

    def test_partial_is_not_refused(self):
        """Refusing it would send the trading node back to the previous
        day's ranking -- the staleness this whole split replaces."""
        assert mf.PARTIAL != mf.STALE
        assert mf.validate(_doc(coverage=0.2, complete=False),
                           trading_day=DAY, now=NOW)["symbols"]

    def test_coverage_is_computed_from_what_was_priced(self):
        measured = {f"S{i}": {"price": 10.0, "volume": 1_000_000,
                              "dollar_volume": 10_000_000.0,
                              "avg_volume": 1e6, "relative_volume": 1.0}
                    for i in range(2)}

        # Keyed to identity, not position: with the retry round, a
        # `tickers[:2]` stub would serve C and D on the second pass and
        # report full coverage for a scan that never priced them.
        servable = {"A", "B"}

        def download(tickers):
            import pandas as pd
            return {t: pd.DataFrame(
                {"Close": [10.0], "Volume": [1_000_000]},
                index=pd.to_datetime([DAY]))
                for t in tickers if t in servable}

        doc = market_scan.run(["A", "B", "C", "D"], trading_day=DAY,
                              session="REGULAR", download=download,
                              pause=0, backoff=0)
        assert doc["universe_size"] == 4
        assert doc["coverage"] == 0.5
        assert doc["complete"] is False

    def test_a_symbol_that_came_back_empty_is_retried(self):
        """The provider catches its own rate limit, logs it, and returns
        the ticker EMPTY -- a full pass produced 78 YFRateLimitError
        lines and zero exceptions, so an except-shaped retry was dead
        code while coverage sat at 30%. The retry keys off the measured
        absence of a row, which is what a caller can actually see."""
        rounds = []

        def download(tickers):
            import pandas as pd
            rounds.append(list(tickers))
            if len(rounds) == 1:
                return {}          # throttled: present, but no rows
            return {t: pd.DataFrame(
                {"Close": [10.0], "Volume": [1_000_000]},
                index=pd.to_datetime([DAY])) for t in tickers}

        measured = market_scan.fetch_today(["A"], trading_day=DAY,
                                           download=download, pause=0,
                                           backoff=0)
        assert len(rounds) == 2, "the empty symbol was not retried"
        assert "A" in measured

    def test_a_symbol_absent_every_round_is_simply_absent(self):
        """Delisted or halted. Different from throttled, and coverage is
        what reports it."""
        def download(tickers):
            return {}

        measured = market_scan.fetch_today(["GONE"], trading_day=DAY,
                                           download=download, pause=0,
                                           backoff=0, max_rounds=2)
        assert measured == {}

    def test_the_batch_size_reflects_what_a_full_pass_sustains(self):
        """400 was fastest in isolation and throttled a real full pass."""
        assert market_scan.BATCH_SIZE < 400
