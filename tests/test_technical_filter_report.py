import pandas as pd

from analyze_technical_filter_log import build_report


def test_build_report_summarizes_technical_filter_log():
    df = pd.DataFrame(
        [
            {
                "scan_id": "20260612_120000",
                "technical_filter_pass": True,
                "technical_filter_score": 3,
                "price_above_hma200": True,
                "hma200_rising": True,
                "hma_macd_bullish": False,
                "macd_histogram_rising": True,
                "sqzmom_green": False,
            },
            {
                "scan_id": "20260612_120001",
                "technical_filter_pass": False,
                "technical_filter_score": 2,
                "price_above_hma200": False,
                "hma200_rising": True,
                "hma_macd_bullish": False,
                "macd_histogram_rising": False,
                "sqzmom_green": True,
            },
        ]
    )

    report = build_report(df)

    assert "Technical Filter Log Report" in report
    assert "- score 2: 1" in report
    assert "- score 3: 1" in report
    assert "- pass: 1" in report
    assert "- fail: 1" in report
    assert "- hma200_rising: 100.0%" in report
    assert "- 20260612_120001: 1" in report


def test_build_report_includes_performance_summary():
    df = pd.DataFrame(
        [
            {
                "scan_id": "20260612_120000",
                "symbol": "AAPL",
                "technical_filter_pass": True,
                "technical_filter_score": 3,
                "price_above_hma200": True,
                "hma200_rising": True,
                "hma_macd_bullish": False,
                "macd_histogram_rising": True,
                "sqzmom_green": False,
                "return_pct": 2.0,
                "max_return_pct": 3.0,
                "min_return_pct": -1.0,
            },
            {
                "scan_id": "20260612_120001",
                "symbol": "MSFT",
                "technical_filter_pass": False,
                "technical_filter_score": 2,
                "price_above_hma200": False,
                "hma200_rising": False,
                "hma_macd_bullish": True,
                "macd_histogram_rising": False,
                "sqzmom_green": True,
                "return_pct": -1.0,
                "max_return_pct": 0.5,
                "min_return_pct": -2.5,
            },
        ]
    )

    report = build_report(df)

    assert "Performance:" in report
    assert "- score 3: 2.00%" in report
    assert "- pass: 2.00%" in report
    assert "- fail: -1.00%" in report
    assert "- price_above_hma200: hit_rate=50.0% avg_return=2.00% avg_max=3.00% avg_min=-1.00%" in report
    assert "- AAPL: max=3.00% score=3" in report
    assert "- MSFT: min=-2.50% score=2" in report
