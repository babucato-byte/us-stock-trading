from datetime import datetime

import pandas as pd

from update_technical_filter_performance import (
    calculate_return_pct,
    calculate_symbol_performance,
    update_performance,
)


def test_calculate_return_pct():
    assert calculate_return_pct(110, 100) == 10
    assert calculate_return_pct(95, 100) == -5


def test_calculate_symbol_performance_uses_after_timestamp_history():
    history = pd.DataFrame(
        {
            "High": [101, 106, 104],
            "Low": [99, 102, 98],
            "Close": [100, 105, 103],
        },
        index=pd.to_datetime(
            [
                "2026-06-12 09:30:00",
                "2026-06-12 09:31:00",
                "2026-06-12 09:32:00",
            ]
        ),
    )

    result = calculate_symbol_performance(
        "AAPL",
        "2026-06-12 09:31:00",
        100,
        history=history,
        checked_at=datetime(2026, 6, 12, 9, 36),
    )

    assert result["price_after"] == 103
    assert result["return_pct"] == 3
    assert result["max_return_pct"] == 6
    assert result["min_return_pct"] == -2
    assert result["holding_minutes"] == 5
    assert result["error"] == ""


def test_update_performance_keeps_existing_row_values_on_fetch_failure(monkeypatch):
    import update_technical_filter_performance as performance

    df = pd.DataFrame(
        [
            {
                "timestamp": "2026-06-12 09:30:00",
                "symbol": "AAPL",
                "current_price": 100,
                "price_after": 101,
                "return_pct": 1,
            }
        ]
    )

    def fail(*args, **kwargs):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(performance, "calculate_symbol_performance", fail)

    updated = update_performance(df)

    assert updated.loc[0, "price_after"] == 101
    assert updated.loc[0, "return_pct"] == 1
    assert updated.loc[0, "error"] == "lookup failed"


def test_update_performance_handles_empty_log():
    updated = update_performance(pd.DataFrame())

    assert updated.empty
    assert "price_after" in updated.columns
    assert "holding_minutes" in updated.columns
