import pandas as pd

from indicators import (
    calculate_technical_filter_score,
    calculate_hma200,
    calculate_hma_macd,
    calculate_sqzmom_basic,
    check_hma200_trend,
    check_hma_macd_signal,
    check_sqzmom_green,
    hma,
    technical_entry_filter,
)


def make_ohlcv(close_values):
    close = pd.Series(close_values, dtype=float)
    return pd.DataFrame(
        {
            "Open": close - 0.1,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Volume": 1_000_000,
        }
    )


def test_technical_entry_filter_returns_false_for_short_history():
    df = make_ohlcv(range(50))
    result = technical_entry_filter(df)

    assert result == {
        "pass": False,
        "score": 0,
        "checks": {
            "price_above_hma200": False,
            "hma200_rising": False,
            "hma_macd_bullish": False,
            "macd_histogram_rising": False,
            "sqzmom_green": False,
        },
    }


def test_hma200_short_history_has_no_exception_and_false_trend():
    df = make_ohlcv(range(30))

    hma200 = calculate_hma200(df)

    assert hma200.dropna().empty
    assert check_hma200_trend(df) is False


def test_technical_entry_filter_passes_with_three_or_more_checks(monkeypatch):
    monkeypatch.setenv("HMA_LONG_LENGTH", "20")
    monkeypatch.setenv("HMA_MACD_FAST", "3")
    monkeypatch.setenv("HMA_MACD_SLOW", "6")
    monkeypatch.setenv("HMA_MACD_SIGNAL", "3")
    monkeypatch.setenv("SQZMOM_LENGTH", "5")
    monkeypatch.setenv("TECHNICAL_FILTER_MIN_SCORE", "3")
    close_values = [100 + idx * 0.2 for idx in range(30)] + [108, 110, 113, 117, 122]
    df = make_ohlcv(close_values)

    result = technical_entry_filter(df)

    assert result["score"] >= 3
    assert result["pass"] is True
    assert set(result["checks"]) == {
        "price_above_hma200",
        "hma200_rising",
        "hma_macd_bullish",
        "macd_histogram_rising",
        "sqzmom_green",
    }


def test_calculate_technical_filter_score_uses_rebalanced_weights():
    checks = {
        "price_above_hma200": True,
        "hma200_rising": True,
        "hma_macd_bullish": True,
        "macd_histogram_rising": False,
        "sqzmom_green": False,
    }

    assert calculate_technical_filter_score(checks) == 4


def test_indicator_functions_return_expected_shapes(monkeypatch):
    monkeypatch.setenv("HMA_LONG_LENGTH", "20")
    close_values = [100 + idx for idx in range(40)]
    df = make_ohlcv(close_values)

    assert len(hma(df["Close"], 20)) == len(df)
    assert len(calculate_hma200(df)) == len(df)
    assert list(calculate_hma_macd(df).columns) == [
        "hma_macd_line",
        "hma_macd_signal",
        "hma_macd_histogram",
    ]
    assert len(calculate_sqzmom_basic(df)) == len(df)
    assert check_hma_macd_signal(df) in {True, False}
    assert check_sqzmom_green(df) in {True, False}
