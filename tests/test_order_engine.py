import pandas as pd

from paper_strategy_order import is_duplicate_order


def test_duplicate_order_blocked():
    history = pd.DataFrame([{"symbol": "AAPL", "order_date": "2026-06-01"}])
    assert is_duplicate_order(history, "AAPL", "2026-06-01")
    assert not is_duplicate_order(history, "MSFT", "2026-06-01")
