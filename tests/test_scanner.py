import pandas as pd

from daily_candidate_scanner import (
    build_candidate_buckets,
    build_realtime_slack_message,
    load_scanner_rules,
)


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


def test_realtime_slack_message_uses_korean_summary_format():
    df = pd.DataFrame(
        [
            {
                "symbol": "MDB",
                "price": 335.55,
                "rsi": 61.65,
                "volume_ratio": 3.94,
                "score": 100,
                "smart_money_score": 90,
            },
            {
                "symbol": "S",
                "price": 16.55,
                "rsi": 49.67,
                "volume_ratio": 3.15,
                "score": 100,
                "smart_money_score": 90,
            },
        ]
    )
    rules = {"top_alert_count": 1, "smart_money_min": 70}
    message = build_realtime_slack_message(df, rules, "premarket", {"S"})

    assert "전체 후보: 2개" in message
    assert "수급 강한 후보: 2개" in message
    assert "거래량 2배 이상: 2개" in message
    assert "TOP 1 후보" in message
    assert "1. MDB — 수급/세력 가능 후보" in message
    assert "2. S" not in message
    assert "신규 등장:\n\n* MDB" in message
    assert "반복 등장:\n\n* S" in message
    assert "수급 리더:\n\n* MDB, S" in message
    assert "* 프리마켓 탐지 단계이므로 주문은 정규장 기준" in message
