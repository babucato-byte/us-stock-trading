import pandas as pd

from daily_candidate_scanner import build_candidate_buckets, load_scanner_rules


def test_scanner_rules_loading():
    rules = load_scanner_rules("paper_safe")
    assert rules["scan_limit"] > 0
    assert "smart_money_min" in rules


def test_order_candidates_created_from_strong_candidates():
    df = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "score": 70,
                "smart_money_score": 70,
                "volume_ratio": 2.2,
            },
            {
                "symbol": "MSFT",
                "score": 70,
                "smart_money_score": 20,
                "volume_ratio": 1.3,
            },
        ]
    )
    rules = {"min_score": 70, "smart_money_min": 50, "volume_ratio_min": 1.2}
    buckets = build_candidate_buckets(df, rules)
    assert buckets.strong_candidates["symbol"].tolist() == ["AAPL"]
    assert buckets.order_candidates["symbol"].tolist() == ["AAPL"]
