"""T8: batched price / average-dollar-volume provider (universe_metrics.py).

`download_fn` is injected in every test, so nothing here reaches yfinance
or the network.
"""

import pandas as pd
import pytest

import universe_metrics as um
from universe_filter import SymbolMetrics


def _frame(closes, volumes):
    return pd.DataFrame({
        "Open": closes, "High": closes, "Low": closes,
        "Close": closes, "Volume": volumes,
    })


def _steady_frame(days=25, close=10.0, volume=3_000_000):
    return _frame([close] * days, [volume] * days)


# -- chunking -----------------------------------------------------------

def test_chunks_are_uppercased_deduped_and_order_preserving():
    chunks = um.chunk_symbols(["aapl", "MSFT", "aapl", " nvda ", "", None], chunk_size=2)
    assert chunks == [["AAPL", "MSFT"], ["NVDA"]]


def test_chunk_size_covers_every_symbol():
    symbols = [f"S{i}" for i in range(451)]
    chunks = um.chunk_symbols(symbols, chunk_size=200)
    assert [len(c) for c in chunks] == [200, 200, 51]
    assert sum(len(c) for c in chunks) == 451


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_chunk_size_is_rejected(bad):
    with pytest.raises(ValueError):
        um.chunk_symbols(["AAPL"], chunk_size=bad)


# -- frame -> metrics ---------------------------------------------------

def test_price_is_the_most_recent_close():
    frame = _steady_frame(days=25, close=10.0)
    frame.loc[frame.index[-1], "Close"] = 12.5
    metrics = um.metrics_from_frame("AAPL", frame)
    assert metrics.price_usd == pytest.approx(12.5)


def test_average_dollar_volume_excludes_the_most_recent_partial_bar():
    # 24 complete days at 10 x 1,000,000 then a partial final day whose
    # tiny volume would drag the average down if it were counted.
    closes = [10.0] * 25
    volumes = [1_000_000] * 24 + [1]
    metrics = um.metrics_from_frame("AAPL", _frame(closes, volumes), volume_window_days=20)
    assert metrics.avg_dollar_volume_usd == pytest.approx(10_000_000.0)


def test_average_uses_at_most_the_requested_window():
    closes = [10.0] * 40
    volumes = [1_000_000] * 20 + [2_000_000] * 19 + [999]
    metrics = um.metrics_from_frame("AAPL", _frame(closes, volumes), volume_window_days=19)
    assert metrics.avg_dollar_volume_usd == pytest.approx(20_000_000.0)


def test_too_little_volume_history_reports_unknown_liquidity_not_zero():
    metrics = um.metrics_from_frame("AAPL", _steady_frame(days=3))
    assert metrics.price_usd is not None
    assert metrics.avg_dollar_volume_usd is None


def test_non_numeric_bars_are_skipped_not_treated_as_zero():
    closes = [10.0] * 25
    volumes = [None] * 10 + [1_000_000] * 15
    metrics = um.metrics_from_frame("AAPL", _frame(closes, volumes), volume_window_days=20)
    assert metrics.avg_dollar_volume_usd == pytest.approx(10_000_000.0)


@pytest.mark.parametrize("frame", [
    None,
    pd.DataFrame(),
    pd.DataFrame({"Close": [], "Volume": []}),
    pd.DataFrame({"Nope": [1, 2, 3]}),
])
def test_unusable_frames_yield_no_metrics(frame):
    assert um.metrics_from_frame("AAPL", frame) is None


@pytest.mark.parametrize("last_close", [0.0, -1.0, float("nan")])
def test_a_single_unusable_last_close_falls_back_to_the_prior_bar(last_close):
    # yfinance routinely emits a NaN row for the in-progress session.
    frame = _steady_frame(days=25)
    frame.loc[frame.index[-1], "Close"] = last_close
    assert um.metrics_from_frame("AAPL", frame).price_usd == pytest.approx(10.0)


def test_price_search_back_is_bounded_so_a_halted_symbol_is_excluded():
    frame = _steady_frame(days=25)
    for offset in range(1, um.MAX_STALE_CLOSE_BARS + 1):
        frame.loc[frame.index[-offset], "Close"] = float("nan")
    assert um.metrics_from_frame("AAPL", frame) is None


def test_all_closes_unusable_yields_no_metrics():
    frame = _steady_frame(days=25)
    frame["Close"] = float("nan")
    assert um.metrics_from_frame("AAPL", frame) is None


# -- provider -----------------------------------------------------------

def test_provider_resolves_every_symbol_from_a_multi_ticker_frame():
    frame = pd.concat(
        {"AAPL": _steady_frame(close=10.0), "MSFT": _steady_frame(close=20.0)}, axis=1)
    calls = []

    def _download(chunk, period):
        calls.append((tuple(chunk), period))
        return frame

    provider = um.YFinanceUniverseMetricsProvider(download_fn=_download, logger=lambda *_: None)
    metrics = provider.get_metrics(["aapl", "msft"])

    assert set(metrics) == {"AAPL", "MSFT"}
    assert metrics["AAPL"].price_usd == pytest.approx(10.0)
    assert metrics["MSFT"].price_usd == pytest.approx(20.0)
    assert calls == [(("AAPL", "MSFT"), um.DEFAULT_HISTORY_PERIOD)]


def test_provider_handles_a_single_symbol_flat_frame():
    provider = um.YFinanceUniverseMetricsProvider(
        download_fn=lambda chunk, period: _steady_frame(close=7.0), logger=lambda *_: None)
    metrics = provider.get_metrics(["AAPL"])
    assert metrics["AAPL"].price_usd == pytest.approx(7.0)


def test_symbol_absent_from_the_response_is_simply_absent_never_guessed():
    frame = pd.concat({"AAPL": _steady_frame()}, axis=1)
    provider = um.YFinanceUniverseMetricsProvider(
        download_fn=lambda chunk, period: frame, logger=lambda *_: None)
    metrics = provider.get_metrics(["AAPL", "GHOST"])
    assert "GHOST" not in metrics


def test_a_failing_chunk_does_not_abort_the_remaining_chunks():
    frames = {"AAPL": _steady_frame(close=10.0), "MSFT": _steady_frame(close=20.0)}
    messages = []

    def _download(chunk, period):
        if chunk == ["AAPL"]:
            raise RuntimeError("rate limited")
        return frames[chunk[0]]

    provider = um.YFinanceUniverseMetricsProvider(
        download_fn=_download, chunk_size=1, logger=messages.append)
    metrics = provider.get_metrics(["AAPL", "MSFT"])

    assert set(metrics) == {"MSFT"}
    assert any("failed" in m for m in messages)


def test_provider_makes_one_call_per_chunk():
    calls = []

    def _download(chunk, period):
        calls.append(list(chunk))
        return _steady_frame()

    provider = um.YFinanceUniverseMetricsProvider(
        download_fn=_download, chunk_size=2, logger=lambda *_: None)
    provider.get_metrics([f"S{i}" for i in range(5)])
    assert [len(c) for c in calls] == [2, 2, 1]


def test_static_provider_returns_only_requested_symbols():
    provider = um.StaticUniverseMetricsProvider({
        "AAPL": SymbolMetrics("AAPL", 10.0, 1.0),
        "MSFT": SymbolMetrics("MSFT", 20.0, 2.0),
    })
    assert set(provider.get_metrics(["aapl"])) == {"AAPL"}


def test_provider_never_imports_yfinance_when_a_download_fn_is_supplied(monkeypatch):
    """Structural guarantee behind "tests never hit the network": with an
    injected download_fn the lazy `import yfinance` line is unreachable."""
    import builtins

    real_import = builtins.__import__

    def _guard(name, *args, **kwargs):
        if name == "yfinance":
            raise AssertionError("yfinance must not be imported when download_fn is injected")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guard)
    provider = um.YFinanceUniverseMetricsProvider(
        download_fn=lambda chunk, period: _steady_frame(), logger=lambda *_: None)
    assert provider.get_metrics(["AAPL"])["AAPL"].price_usd == pytest.approx(10.0)
