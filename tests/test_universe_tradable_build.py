"""T8: end-to-end filtered-universe build (universe_builder.py) and the
daily runner wiring (universe_daily_runner.py).

Every artifact is written under tmp_path and every provider/broker is a
double -- the real universe.csv, logs/ and state/ are never touched, per
PROJECT_CONSTITUTION §11 ("실제 운영 CSV와 로그 파일을 테스트 중 변경하지
않는다").
"""

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

import universe_budget as ub
import universe_builder
import universe_daily_runner as runner
import universe_filter as uf
from universe_metrics import StaticUniverseMetricsProvider

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

THRESHOLDS = uf.ScannerThresholds(
    min_price_usd=5.0, min_avg_dollar_volume_usd=20_000_000.0, source="test",
)

LISTING = [
    {"symbol": "AAPL", "name": "Apple", "exchange": "NASDAQ", "tradable": True, "shortable": True},
    {"symbol": "MSFT", "name": "Microsoft", "exchange": "NASDAQ", "tradable": True, "shortable": True},
    {"symbol": "BRKA", "name": "Berkshire A", "exchange": "NYSE", "tradable": True, "shortable": False},
    {"symbol": "PENNY", "name": "Penny", "exchange": "AMEX", "tradable": True, "shortable": False},
    {"symbol": "THIN", "name": "Thin", "exchange": "NYSE", "tradable": True, "shortable": False},
    {"symbol": "OTCX", "name": "Over the counter", "exchange": "OTC", "tradable": True, "shortable": False},
    {"symbol": "NODATA", "name": "No data", "exchange": "NASDAQ", "tradable": True, "shortable": False},
]

METRICS = {
    "AAPL": uf.SymbolMetrics("AAPL", 200.0, 50_000_000_000.0),
    "MSFT": uf.SymbolMetrics("MSFT", 400.0, 30_000_000_000.0),
    "BRKA": uf.SymbolMetrics("BRKA", 700_000.0, 20_000_000.0),   # 1 share >> ceiling
    "PENNY": uf.SymbolMetrics("PENNY", 1.5, 900_000_000.0),      # below price floor
    "THIN": uf.SymbolMetrics("THIN", 50.0, 1_000_000.0),         # below liquidity floor
    "OTCX": uf.SymbolMetrics("OTCX", 20.0, 900_000_000.0),       # unsupported venue
}


def _budget(cash=10_000.0, source="kis_balance"):
    return uf.UniverseBudget(
        available_cash_usd=cash, as_of=NOW.isoformat(), source=source)


def _paths(tmp_path):
    return {
        "output_path": tmp_path / "universe_tradable.csv",
        "report_path": tmp_path / "logs" / "universe_filter_report.json",
        "decisions_path": tmp_path / "logs" / "universe_decisions.csv",
    }


def _build(tmp_path, *, rows=None, metrics=None, budget=None, **kwargs):
    return universe_builder.build_tradable_universe(
        rows if rows is not None else LISTING,
        StaticUniverseMetricsProvider(METRICS if metrics is None else metrics),
        budget if budget is not None else _budget(),
        thresholds=THRESHOLDS,
        now=NOW,
        logger=lambda *_: None,
        **_paths(tmp_path), **kwargs,
    )


# -- the filtered universe file -----------------------------------------

def test_only_affordable_liquid_supported_symbols_are_written(tmp_path):
    result = _build(tmp_path)
    frame = pd.read_csv(result["output_path"])
    # ceiling = 10,000 * 90% * 0.10 = 900 USD/share
    assert set(frame["symbol"]) == {"AAPL", "MSFT"}


def test_rows_are_ranked_most_liquid_first(tmp_path):
    result = _build(tmp_path)
    frame = pd.read_csv(result["output_path"])
    assert list(frame["symbol"]) == ["AAPL", "MSFT"]
    assert frame["avg_dollar_volume_usd"].is_monotonic_decreasing


def test_written_columns_and_share_counts(tmp_path):
    result = _build(tmp_path)
    frame = pd.read_csv(result["output_path"])
    assert list(frame.columns) == universe_builder.TRADABLE_COLUMNS
    row = frame.set_index("symbol").loc["AAPL"]
    assert row["max_affordable_shares"] == 4  # floor(900 / 200)
    assert row["price_ceiling_usd"] == pytest.approx(900.0)
    assert row["exchange"] == "NASDAQ"
    assert row["name"] == "Apple"


def test_share_counts_are_whole_numbers_only(tmp_path):
    frame = pd.read_csv(_build(tmp_path)["output_path"])
    for value in frame["max_affordable_shares"]:
        assert float(value).is_integer()
        assert value >= 1


def test_a_smaller_account_shrinks_the_universe(tmp_path):
    result = _build(tmp_path, budget=_budget(cash=3_000.0))  # ceiling 270
    frame = pd.read_csv(result["output_path"])
    assert set(frame["symbol"]) == {"AAPL"}  # MSFT at 400 no longer affordable


def test_an_account_that_can_afford_nothing_refuses_to_write_an_empty_file(tmp_path):
    """ORACLE-CASH-01: a build that would include NOTHING must not replace
    the entry-side pool.

    This used to write a zero-row file and log a warning. That is how an
    unusable cash figure erased the pool silently: an unknown balance read
    as $0 gives a $0 ceiling, every symbol falls to EXCLUDED_ABOVE_BUDGET,
    and a valid-looking empty file lands under the name downstream
    scanning trusts. Keeping the previous file is the safe direction --
    affordability is re-checked per candidate at entry time anyway.
    """
    paths = _paths(tmp_path)
    with pytest.raises(universe_builder.UniverseBuildError) as excinfo:
        _build(tmp_path, budget=_budget(cash=1.0))
    assert "0 of" in str(excinfo.value)
    assert not paths["output_path"].exists()


def test_a_previous_universe_survives_a_would_be_empty_build(tmp_path):
    paths = _paths(tmp_path)
    paths["output_path"].write_text("symbol\nOLD\n", encoding="utf-8")
    with pytest.raises(universe_builder.UniverseBuildError):
        _build(tmp_path, budget=_budget(cash=1.0))
    assert paths["output_path"].read_text(encoding="utf-8") == "symbol\nOLD\n"


def test_missing_budget_raises_and_writes_nothing(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(universe_builder.UniverseBuildError):
        universe_builder.build_tradable_universe(
            LISTING, StaticUniverseMetricsProvider(METRICS), None,
            thresholds=THRESHOLDS, logger=lambda *_: None, **paths)
    assert not paths["output_path"].exists()
    assert not paths["report_path"].exists()


def test_an_unusable_budget_aborts_before_writing(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(uf.UniverseFilterError):
        universe_builder.build_tradable_universe(
            LISTING, StaticUniverseMetricsProvider(METRICS),
            uf.UniverseBudget(available_cash_usd=float("nan"), as_of="x", source="kis_balance"),
            thresholds=THRESHOLDS, logger=lambda *_: None, **paths)
    assert not paths["output_path"].exists()


def test_a_previous_filtered_universe_survives_a_failed_build(tmp_path):
    paths = _paths(tmp_path)
    paths["output_path"].write_text("symbol\nOLD\n", encoding="utf-8")
    with pytest.raises(universe_builder.UniverseBuildError):
        universe_builder.build_tradable_universe(
            LISTING, StaticUniverseMetricsProvider(METRICS), None,
            thresholds=THRESHOLDS, logger=lambda *_: None, **paths)
    assert paths["output_path"].read_text(encoding="utf-8") == "symbol\nOLD\n"


# -- the decision log and report ----------------------------------------

def test_every_listed_symbol_appears_in_the_decision_log(tmp_path):
    result = _build(tmp_path)
    decisions = pd.read_csv(result["decisions_path"])
    assert list(decisions.columns) == universe_builder.DECISION_COLUMNS
    assert set(decisions["symbol"]) == {r["symbol"] for r in LISTING}
    assert len(decisions) == len(LISTING)


def test_each_exclusion_carries_its_specific_reason(tmp_path):
    decisions = pd.read_csv(_build(tmp_path)["decisions_path"]).set_index("symbol")
    assert decisions.loc["BRKA", "reason"] == uf.REASON_PRICE_ABOVE_BUDGET
    assert decisions.loc["PENNY", "reason"] == uf.REASON_PRICE_BELOW_FLOOR
    assert decisions.loc["THIN", "reason"] == uf.REASON_ILLIQUID
    assert decisions.loc["OTCX", "reason"] == uf.REASON_UNSUPPORTED_EXCHANGE
    assert decisions.loc["NODATA", "reason"] == uf.REASON_NO_PRICE_DATA
    assert decisions.loc["AAPL", "reason"] == uf.REASON_INCLUDED
    assert decisions.loc["BRKA", "detail"]  # non-empty explanation


def test_report_json_carries_the_reason_histogram_and_budget_provenance(tmp_path):
    result = _build(tmp_path)
    report = json.loads(result["report_path"].read_text(encoding="utf-8"))
    assert report["total"] == len(LISTING)
    assert report["included"] == 2
    assert report["excluded"] == len(LISTING) - 2
    assert report["reason_counts"][uf.REASON_PRICE_ABOVE_BUDGET] == 1
    assert report["reason_counts"][uf.REASON_NO_LIQUIDITY_DATA] == 0
    assert sum(report["reason_counts"].values()) == report["total"]
    assert report["budget_source"] == "kis_balance"
    assert report["price_ceiling_usd"] == pytest.approx(900.0)
    assert report["min_avg_dollar_volume_usd"] == 20_000_000.0
    assert report["budget_stale"] is False
    assert report["generated_at"] == NOW.isoformat()


def test_stale_budget_is_marked_in_the_report_and_logged(tmp_path):
    messages = []
    universe_builder.build_tradable_universe(
        LISTING, StaticUniverseMetricsProvider(METRICS), _budget(source="cached:kis_balance"),
        thresholds=THRESHOLDS, now=NOW, budget_stale=True, logger=messages.append,
        **_paths(tmp_path))
    report = json.loads(_paths(tmp_path)["report_path"].read_text(encoding="utf-8"))
    assert report["budget_stale"] is True
    assert report["budget_source"] == "cached:kis_balance"
    assert any("kept previous value" in m for m in messages)


def test_an_empty_result_aborts_instead_of_being_warned_about(tmp_path):
    """A warning printed AFTER the write does not undo the write. The
    empty universe is now refused, not announced."""
    messages = []
    with pytest.raises(universe_builder.UniverseBuildError):
        universe_builder.build_tradable_universe(
            LISTING, StaticUniverseMetricsProvider(METRICS), _budget(cash=1.0),
            thresholds=THRESHOLDS, now=NOW, logger=messages.append, **_paths(tmp_path))
    assert not any("wrote" in m for m in messages)


def test_summary_lines_are_logged(tmp_path):
    messages = []
    universe_builder.build_tradable_universe(
        LISTING, StaticUniverseMetricsProvider(METRICS), _budget(),
        thresholds=THRESHOLDS, now=NOW, logger=messages.append, **_paths(tmp_path))
    text = "\n".join(messages)
    for reason in uf.ALL_REASONS:
        assert reason in text


# -- listing reader -----------------------------------------------------

def test_load_universe_rows_keeps_symbols_as_written(tmp_path):
    path = tmp_path / "universe.csv"
    path.write_text(
        "symbol,name,exchange,tradable,shortable\n"
        "NA,Nationwide,NYSE,True,True\n"
        "INF,Infinite,NASDAQ,True,True\n"
        ",Blank,NYSE,True,True\n",
        encoding="utf-8")
    rows = universe_builder.load_universe_rows(path)
    assert [r["symbol"] for r in rows] == ["NA", "INF"]  # blank dropped, no float coercion


def test_load_universe_rows_on_a_missing_file_is_an_error_not_an_empty_universe(tmp_path):
    with pytest.raises(universe_builder.UniverseBuildError):
        universe_builder.load_universe_rows(tmp_path / "nope.csv")


def test_load_universe_rows_tolerates_a_headerless_file(tmp_path):
    path = tmp_path / "universe.csv"
    path.write_text("", encoding="utf-8")
    assert universe_builder.load_universe_rows(path) == []


def test_the_repo_universe_csv_still_parses_with_this_reader():
    rows = universe_builder.load_universe_rows(universe_builder.UNIVERSE_LISTING_PATH)
    assert len(rows) > 1000
    assert {"symbol", "exchange"} <= set(rows[0])


# -- universe.csv contract is unchanged ---------------------------------

def test_build_universe_still_writes_exactly_the_original_columns(tmp_path):
    class _Broker:
        def get_assets(self):
            return [{"symbol": "AAPL", "name": "Apple", "exchange": "NASDAQ",
                     "status": "active", "tradable": True, "shortable": True,
                     "class": "us_equity"}]

    output = tmp_path / "universe.csv"
    frame = universe_builder.build_universe(broker=_Broker(), output_path=output)
    assert list(frame.columns) == ["symbol", "name", "exchange", "tradable", "shortable"]
    assert list(pd.read_csv(output).columns) == ["symbol", "name", "exchange", "tradable", "shortable"]


def test_filtering_never_rewrites_the_full_listing(tmp_path):
    """The exchange registry (and therefore the KIS sell path) reads
    universe.csv; a build of the filtered universe must leave it alone."""
    listing = tmp_path / "universe.csv"
    # AAPL keeps the build non-empty (an all-excluded build is refused
    # outright now); BRKA is the priced-out symbol the listing must keep.
    listing.write_text(
        "symbol,name,exchange,tradable,shortable\n"
        "AAPL,Apple,NASDAQ,True,True\n"
        "BRKA,Berkshire A,NYSE,True,False\n",
        encoding="utf-8")
    before = listing.read_bytes()
    _build(tmp_path, rows=universe_builder.load_universe_rows(listing))
    assert listing.read_bytes() == before


def test_a_held_symbol_priced_out_of_the_budget_stays_resolvable(tmp_path, monkeypatch):
    """Regression guard for the reason universe.csv is not narrowed: the
    exchange registry must still resolve a symbol the filter excluded, or
    an exit for a position in it could not be routed."""
    from market_data import exchange_registry

    listing = tmp_path / "universe.csv"
    listing.write_text(
        "symbol,name,exchange,tradable,shortable\n"
        "AAPL,Apple,NASDAQ,True,True\n"
        "BRKA,Berkshire A,NYSE,True,False\n",
        encoding="utf-8")
    result = _build(tmp_path, rows=universe_builder.load_universe_rows(listing))
    # BRKA really was filtered OUT of the entry-side pool...
    assert "BRKA" not in set(pd.read_csv(result["output_path"])["symbol"])

    monkeypatch.setenv("UNIVERSE_FILE", str(listing))
    exchange_registry.reset_registry()
    try:
        record = exchange_registry.resolve_exchange("BRKA")
        assert record.exchange.value == "NYSE"
    finally:
        exchange_registry.reset_registry()


# -- daily runner wiring ------------------------------------------------

class _FakeKIS:
    def __init__(self, snapshot=None, error=None):
        self._snapshot = snapshot
        self._error = error

    def get_account_snapshot(self):
        if self._error is not None:
            raise self._error
        return self._snapshot


def _account_snapshot(orderable=10_000.0):
    from domain.account_snapshot import AccountSnapshot

    return AccountSnapshot(
        krw_cash=0.0, usd_cash=orderable, usd_orderable_cash=orderable,
        usd_reserved_in_open_orders=0.0, as_of=NOW, source="kis_balance",
        account_id="12345678")


def test_runner_refreshes_the_budget_then_rebuilds(tmp_path, monkeypatch):
    listing = tmp_path / "universe.csv"
    pd.DataFrame(LISTING).to_csv(listing, index=False)
    state_file = tmp_path / "universe_budget.json"

    state, error = runner.refresh_account_budget(
        broker=_FakeKIS(_account_snapshot()), state_path=state_file, logger=lambda *_: None)
    assert error is None and state.available_cash_usd == pytest.approx(10_000.0)

    result = runner.rebuild_tradable_universe(
        state,
        metrics_provider=StaticUniverseMetricsProvider(METRICS),
        logger=lambda *_: None,
        universe_path=listing,
        thresholds=THRESHOLDS,
        now=NOW,
        **_paths(tmp_path),
    )
    assert set(pd.read_csv(result["output_path"])["symbol"]) == {"AAPL", "MSFT"}


def test_runner_budget_step_keeps_the_previous_value_on_a_failed_read(tmp_path):
    state_file = tmp_path / "universe_budget.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=3_000.0, as_of=NOW.isoformat(), source="kis_balance"),
        state_file)

    state, error = runner.refresh_account_budget(
        broker=_FakeKIS(error=RuntimeError("kis down")), state_path=state_file,
        logger=lambda *_: None)

    assert error is not None
    assert state.stale is True
    assert state.available_cash_usd == pytest.approx(3_000.0)


def test_runner_main_aborts_the_rebuild_when_no_budget_exists(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "log_run_header", lambda *a, **k: None)
    monkeypatch.setattr(runner, "refresh_full_listing", lambda *a, **k: calls.append("listing"))
    monkeypatch.setattr(
        runner, "refresh_account_budget",
        lambda *a, **k: (None, "RuntimeError: kis down"))
    monkeypatch.setattr(
        runner, "rebuild_tradable_universe",
        lambda *a, **k: calls.append("rebuild"))

    messages = []
    assert runner.main(logger=messages.append) == 1
    assert calls == ["listing"]  # listing still refreshed, rebuild skipped
    assert any("no account budget available" in m for m in messages)


def test_runner_main_returns_zero_on_the_happy_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "log_run_header", lambda *a, **k: None)
    monkeypatch.setattr(runner, "refresh_full_listing", lambda *a, **k: calls.append("listing"))
    monkeypatch.setattr(
        runner, "refresh_account_budget",
        lambda *a, **k: (ub.BudgetState(1.0, NOW.isoformat(), "kis_balance"), None))
    monkeypatch.setattr(
        runner, "rebuild_tradable_universe", lambda *a, **k: calls.append("rebuild"))
    assert runner.main(logger=lambda *_: None) == 0
    assert calls == ["listing", "rebuild"]
