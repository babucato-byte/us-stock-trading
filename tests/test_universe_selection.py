"""The scan universe must be filled to its size, not filtered down to
whatever survives.

On 2026-08-24 the runner took the activity ranking's top 300, then
removed the 98 that carried a `provider_unavailable` record, and scanned
202 -- while the run report still said `universe=300`. A third of the
intended coverage disappeared with no top-up and no number anywhere
saying so. MARA sat at activity rank 306, six places outside a cut that
was only nominally 300.

Two separate defects, tested separately here:
  * the fill stopped at the ranking position instead of at 300 eligible
  * one failed fetch benched a symbol for the rest of the day

Neither is a strategy question. Nothing below changes what a symbol has
to do to become a candidate.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import eligibility as elig  # noqa: E402
from scanners.base import universe_selection as us  # noqa: E402

TODAY = date(2026, 8, 24)


class _Activity:
    """A ranking, in order, most liquid first."""

    def __init__(self, symbols):
        self._symbols = list(symbols)

    def active_symbols(self, *, limit=300, today=None, **kw):
        return self._symbols[:limit]


class _Eligibility:
    """Skips exactly the symbols it was told to."""

    def __init__(self, skip=()):
        self._skip = {s.upper() for s in skip}

    def should_skip(self, symbol, *, today=None):
        return str(symbol).upper() in self._skip


def _ranking(n, prefix="S"):
    return [f"{prefix}{i:04d}" for i in range(1, n + 1)]


class TestTheUniverseIsFilledToItsSize:
    def test_an_ineligible_name_is_replaced_not_merely_dropped(self):
        ranking = _ranking(50)
        # The first ten are unavailable, exactly like a provider outage.
        selection = us.eligible_top(_Activity(ranking),
                                    _Eligibility(ranking[:10]),
                                    limit=20, today=TODAY)
        assert len(selection.symbols) == 20
        assert selection.symbols == ranking[10:30]
        assert selection.skipped_ineligible == 10

    def test_the_production_shape_recovers_the_full_pool(self):
        """98 of the top 300 ineligible: 202 scanned before, 300 after."""
        ranking = _ranking(2000)
        blocked = ranking[:98]
        before = [s for s in ranking[:300] if s not in set(blocked)]
        after = us.eligible_top(_Activity(ranking), _Eligibility(blocked),
                                limit=300, today=TODAY)

        assert len(before) == 202
        assert len(after.symbols) == 300
        assert after.depth_reached == 398

    def test_a_name_past_the_nominal_cut_is_reached(self):
        """MARA sat at rank 306 while the cut was nominally 300."""
        ranking = _ranking(500)
        mara = ranking[305]                      # 1-indexed rank 306
        selection = us.eligible_top(_Activity(ranking),
                                    _Eligibility(ranking[:98]),
                                    limit=300, today=TODAY)
        assert mara in selection.symbols
        assert selection.rank_of(mara) == 306

    def test_order_is_still_the_ranking(self):
        ranking = _ranking(40)
        selection = us.eligible_top(_Activity(ranking),
                                    _Eligibility({"S0003", "S0007"}),
                                    limit=10, today=TODAY)
        assert selection.symbols == [s for s in ranking
                                     if s not in {"S0003", "S0007"}][:10]

    def test_every_symbol_carries_its_rank_and_source(self):
        selection = us.eligible_top(_Activity(_ranking(10)), _Eligibility(),
                                    limit=3, today=TODAY)
        assert selection.source_of("S0001") == us.SOURCE_PREVIOUS_DAY
        assert selection.rank_of("S0002") == 2

    def test_a_short_ranking_is_reported_not_padded(self):
        selection = us.eligible_top(_Activity(_ranking(5)), _Eligibility(),
                                    limit=300, today=TODAY)
        assert len(selection.symbols) == 5
        assert selection.depth_exhausted is True

    def test_the_walk_is_bounded(self):
        """Everything ineligible must not walk 10,000 names forever."""
        ranking = _ranking(10_000)
        selection = us.eligible_top(_Activity(ranking),
                                    _Eligibility(ranking), limit=300,
                                    today=TODAY)
        assert selection.symbols == []
        assert selection.considered <= 300 * us.DEFAULT_DEPTH_MULTIPLE

    def test_a_zero_limit_selects_nothing(self):
        selection = us.eligible_top(_Activity(_ranking(10)), _Eligibility(),
                                    limit=0, today=TODAY)
        assert selection.symbols == []


class TestTheIntradaySupplementIsAdditiveOnly:
    def test_new_names_are_appended_and_labelled(self):
        selection = us.eligible_top(_Activity(_ranking(5)), _Eligibility(),
                                    limit=5, today=TODAY)
        us.merge_supplement(selection, ["NEW1", "NEW2"])

        assert selection.symbols[-2:] == ["NEW1", "NEW2"]
        assert selection.source_of("NEW1") == us.SOURCE_INTRADAY_SUPPLEMENT
        assert selection.supplement_added == 2

    def test_a_duplicate_keeps_its_original_provenance(self):
        """Relabelling a name the ranking already found would inflate the
        supplement's apparent contribution in the one comparison this
        provenance exists to support."""
        selection = us.eligible_top(_Activity(_ranking(5)), _Eligibility(),
                                    limit=5, today=TODAY)
        us.merge_supplement(selection, ["S0002", "NEW1"])

        assert selection.symbols.count("S0002") == 1
        assert selection.source_of("S0002") == us.SOURCE_PREVIOUS_DAY
        assert selection.supplement_added == 1

    def test_the_supplement_is_capped(self):
        selection = us.eligible_top(_Activity(_ranking(3)), _Eligibility(),
                                    limit=3, today=TODAY)
        us.merge_supplement(selection, [f"N{i}" for i in range(200)], limit=50)
        assert selection.supplement_added == 50

    def test_an_empty_supplement_changes_nothing(self):
        selection = us.eligible_top(_Activity(_ranking(3)), _Eligibility(),
                                    limit=3, today=TODAY)
        before = list(selection.symbols)
        us.merge_supplement(selection, [])
        assert selection.symbols == before
        assert selection.supplement_added == 0


class TestOneFailedFetchDoesNotBenchASymbolAllDay:
    """`provider_unavailable` used to be honoured by `should_skip` for a
    full day, so a single timeout removed a name from every remaining
    scan of the session. A failed round trip is a statement about the
    round trip, not about the symbol -- and a symbol skipped here never
    reaches a strategy gate at all, so the two must not be confusable."""

    def _store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        return elig.EligibilityStore("yfinance")

    def test_the_first_failure_is_retried_next_tick(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)
        store.note_ineligible("MARA", elig.PROVIDER_UNAVAILABLE, today=TODAY)
        assert store.should_skip("MARA", today=TODAY) is False

    def test_it_takes_a_run_of_failures_to_bench_it(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)
        for attempt in range(1, elig.PROVIDER_FAILURE_QUARANTINE_THRESHOLD):
            store.note_ineligible("MARA", elig.PROVIDER_UNAVAILABLE, today=TODAY)
            assert store.should_skip("MARA", today=TODAY) is False, attempt

        store.note_ineligible("MARA", elig.PROVIDER_UNAVAILABLE, today=TODAY)
        assert store.should_skip("MARA", today=TODAY) is True

    def test_a_successful_fetch_clears_the_run(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, monkeypatch)
        store.note_ineligible("MARA", elig.PROVIDER_UNAVAILABLE, today=TODAY)
        store.note_ineligible("MARA", elig.PROVIDER_UNAVAILABLE, today=TODAY)
        store.note_eligible("MARA", today=TODAY)
        store.note_ineligible("MARA", elig.PROVIDER_UNAVAILABLE, today=TODAY)

        assert store.get("MARA").consecutive_failures == 1
        assert store.should_skip("MARA", today=TODAY) is False

    def test_the_counter_is_consecutive_not_cumulative(self, tmp_path,
                                                       monkeypatch):
        store = self._store(tmp_path, monkeypatch)
        for _ in range(5):
            store.note_ineligible("AAA", elig.PROVIDER_UNAVAILABLE, today=TODAY)
            store.note_eligible("AAA", today=TODAY)
        store.note_ineligible("AAA", elig.PROVIDER_UNAVAILABLE, today=TODAY)
        assert store.get("AAA").consecutive_failures == 1

    def test_a_different_reason_does_not_share_the_counter(self, tmp_path,
                                                           monkeypatch):
        """Only a provider refusal is retried. A structural verdict --
        short history, an unsupported ticker -- is about the symbol and
        keeps its original dwell time."""
        store = self._store(tmp_path, monkeypatch)
        store.note_ineligible("BBB", elig.UNSUPPORTED_SYMBOL, today=TODAY)
        assert store.should_skip("BBB", today=TODAY) is True

    def test_the_run_survives_a_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        store = elig.EligibilityStore("yfinance")
        for _ in range(elig.PROVIDER_FAILURE_QUARANTINE_THRESHOLD):
            store.note_ineligible("CCC", elig.PROVIDER_UNAVAILABLE, today=TODAY)
        store.save()

        reloaded = elig.EligibilityStore.load("yfinance")
        assert reloaded.get("CCC").consecutive_failures == \
            elig.PROVIDER_FAILURE_QUARANTINE_THRESHOLD
        assert reloaded.should_skip("CCC", today=TODAY) is True

    def test_a_record_written_before_this_field_existed_still_loads(
            self, tmp_path, monkeypatch):
        """The deployed store has thousands of rows with no
        `consecutive_failures` key."""
        import json

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        path = elig.store_path("yfinance")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"provider": "yfinance", "symbols": {
            "OLD": {"symbol": "OLD", "eligible": False,
                    "reason": elig.PROVIDER_UNAVAILABLE, "provider": "yfinance",
                    "last_checked": "2026-08-24", "next_check": "2026-08-25"}}}))

        store = elig.EligibilityStore.load("yfinance")
        assert store.get("OLD").consecutive_failures == 0
        assert store.should_skip("OLD", today=TODAY) is False


class TestTheSupplementIsBoundedAndCached:
    """One symbol is one round trip -- the provider has no batch
    endpoint. A market-wide sweep every 15 minutes would cost more
    fetches than the scan it supplements, so this reads a window below
    the cut, once per session."""

    class _Provider:
        def __init__(self, volumes, fail=()):
            self.volumes = volumes
            self.fail = set(fail)
            self.calls = []

        def get_daily_bars(self, symbol, lookback_days=400):
            import pandas as pd
            self.calls.append(symbol)
            if symbol in self.fail:
                raise RuntimeError("provider says no")
            price, volume = self.volumes.get(symbol, (0.0, 0.0))
            return pd.DataFrame(
                {"Close": [price], "Volume": [volume]},
                index=pd.to_datetime(["2026-08-24"]))

    def test_only_names_below_the_cut_are_considered(self, monkeypatch):
        from scanners.base import intraday_supplement as sup

        ranking = _ranking(400)
        got = sup.window_candidates(_Activity(ranking), _Eligibility(),
                                    already=ranking[:300], cut=300,
                                    window=50, today=TODAY)
        assert got == ranking[300:350]

    def test_an_ineligible_window_name_is_not_fetched(self, monkeypatch):
        from scanners.base import intraday_supplement as sup

        ranking = _ranking(400)
        got = sup.window_candidates(_Activity(ranking),
                                    _Eligibility({"S0305"}),
                                    already=ranking[:300], cut=300,
                                    window=50, today=TODAY)
        assert "S0305" not in got

    def test_it_ranks_by_todays_dollar_volume(self):
        from scanners.base import intraday_supplement as sup

        provider = self._Provider({"A": (10.0, 5_000_000),    # 50M
                                   "B": (10.0, 30_000_000),   # 300M
                                   "C": (10.0, 1_000_000)})   # 10M
        chosen = sup.select(provider, ["A", "B", "C"],
                            trading_day="2026-08-24", size=2)
        assert chosen == ["B", "A"]

    def test_an_illiquid_name_is_left_out(self):
        from scanners.base import intraday_supplement as sup

        provider = self._Provider({"A": (1.0, 100.0)})   # 100 USD
        assert sup.select(provider, ["A"], trading_day="2026-08-24",
                          size=10) == []

    def test_a_fetch_failure_drops_only_that_symbol(self):
        from scanners.base import intraday_supplement as sup

        provider = self._Provider({"A": (10.0, 30_000_000),
                                   "B": (10.0, 20_000_000)}, fail={"A"})
        assert sup.select(provider, ["A", "B"], trading_day="2026-08-24",
                          size=5) == ["B"]

    def test_a_stale_bar_is_not_treated_as_today(self):
        """A symbol whose last daily bar is Friday's did not trade today,
        and ranking it on Friday's volume is the exact staleness this
        exists to correct."""
        from scanners.base import intraday_supplement as sup

        provider = self._Provider({"A": (10.0, 30_000_000)})
        assert sup.select(provider, ["A"], trading_day="2026-08-25",
                          size=5) == []

    def test_it_is_computed_once_per_session(self, tmp_path, monkeypatch):
        from scanners.base import intraday_supplement as sup

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        ranking = _ranking(320)
        provider = self._Provider({s: (10.0, 30_000_000) for s in ranking})

        first = sup.load_or_build(provider, _Activity(ranking), _Eligibility(),
                                  trading_day="2026-08-24", session="REGULAR",
                                  already=ranking[:300], cut=300, size=5,
                                  today=TODAY)
        calls_after_first = len(provider.calls)
        second = sup.load_or_build(provider, _Activity(ranking), _Eligibility(),
                                   trading_day="2026-08-24", session="REGULAR",
                                   already=ranking[:300], cut=300, size=5,
                                   today=TODAY)

        assert first == second and len(first) == 5
        assert len(provider.calls) == calls_after_first, "recomputed on tick 2"

    def test_size_zero_makes_no_call_at_all(self, tmp_path, monkeypatch):
        from scanners.base import intraday_supplement as sup

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        provider = self._Provider({})
        assert sup.load_or_build(provider, _Activity(_ranking(320)),
                                 _Eligibility(), trading_day="2026-08-24",
                                 session="REGULAR", already=[], cut=300,
                                 size=0, today=TODAY) == []
        assert provider.calls == []

    def test_it_is_off_by_default(self):
        """Turning it on for every profile at once would change what
        --active-pool-size means for S1 and S2 too."""
        from scanners.base import intraday_supplement as sup

        assert sup.DEFAULT_SUPPLEMENT_SIZE == 0
        assert sup.S6_SUPPLEMENT_SIZE == 50

    def test_a_supplement_failure_is_not_a_scan_failure(self, tmp_path,
                                                        monkeypatch):
        from scanners.base import intraday_supplement as sup

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))

        class _Broken:
            def active_symbols(self, **kw):
                raise RuntimeError("store on fire")

        assert sup.load_or_build(self._Provider({}), _Broken(), _Eligibility(),
                                 trading_day="2026-08-24", session="REGULAR",
                                 already=[], cut=300, size=5,
                                 today=TODAY) == []
