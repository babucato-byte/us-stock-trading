import pandas as pd

from daily_candidate_scanner import (
    CANDIDATES_FILE,
    ORDER_CANDIDATES_FILE,
    PREVIOUS_CANDIDATES_FILE,
    STRONG_CANDIDATES_FILE,
    apply_filters,
    build_candidate_buckets,
    build_realtime_slack_message,
    load_scanner_rules,
    save_candidate_files,
)


def test_scanner_rules_loading():
    rules = load_scanner_rules("paper_safe")
    assert rules["scan_limit"] > 0
    assert "filters" in rules
    assert any(item["field"] == "smart_money_score" for item in rules["filters"])


def test_filter_greater_equal():
    metrics = {"symbol": "AAPL", "price": 10}
    assert apply_filters(metrics, [{"field": "price", "operator": ">=", "value": 5}])
    assert not apply_filters(metrics, [{"field": "price", "operator": ">=", "value": 15}])


def test_filter_between():
    metrics = {"symbol": "AAPL", "rsi": 55}
    assert apply_filters(metrics, [{"field": "rsi", "operator": "between", "min": 40, "max": 65}])
    assert not apply_filters(metrics, [{"field": "rsi", "operator": "between", "min": 60, "max": 80}])


def test_unknown_field_skips_with_warning():
    metrics = {"symbol": "AAPL", "price": 10}
    assert apply_filters(metrics, [{"field": "unknown_metric", "operator": ">=", "value": 999}])


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
    rules = {
        "filters": [
            {"field": "score", "operator": ">=", "value": 70},
            {"field": "smart_money_score", "operator": ">=", "value": 50},
            {"field": "volume_ratio", "operator": ">=", "value": 1.2},
        ]
    }
    buckets = build_candidate_buckets(df, rules)
    assert buckets.strong_candidates["symbol"].tolist() == ["AAPL"]
    assert buckets.order_candidates["symbol"].tolist() == ["AAPL"]


def test_preset_load_uses_rule_engine_filters():
    rules = load_scanner_rules("smart_money")
    assert rules["active_preset"] == "smart_money"
    assert rules["top_alert_count"] == 7
    assert any(
        item["field"] == "smart_money_score" and item["value"] == 70
        for item in rules["filters"]
    )


def test_candidate_file_generation(tmp_path, monkeypatch):
    import daily_candidate_scanner as scanner

    candidates = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "price": 200,
                "ma200": 180,
                "rsi": 55,
                "volume_ratio": 2.2,
                "avg_dollar_volume": 100000000,
                "dollar_volume": 110000000,
                "score": 100,
                "smart_money_score": 70,
                "type": "smart_money",
                "scan_time": "2026-06-01 09:30:00",
            }
        ]
    )
    buckets = build_candidate_buckets(
        candidates,
        {
            "filters": [
                {"field": "score", "operator": ">=", "value": 70},
                {"field": "smart_money_score", "operator": ">=", "value": 50},
                {"field": "volume_ratio", "operator": ">=", "value": 1.2},
            ]
        },
    )

    monkeypatch.setattr(scanner, "CANDIDATES_FILE", tmp_path / CANDIDATES_FILE.name)
    monkeypatch.setattr(scanner, "STRONG_CANDIDATES_FILE", tmp_path / STRONG_CANDIDATES_FILE.name)
    monkeypatch.setattr(scanner, "ORDER_CANDIDATES_FILE", tmp_path / ORDER_CANDIDATES_FILE.name)
    monkeypatch.setattr(scanner, "PREVIOUS_CANDIDATES_FILE", tmp_path / PREVIOUS_CANDIDATES_FILE.name)

    save_candidate_files(buckets)

    assert (tmp_path / "candidates.csv").exists()
    assert (tmp_path / "strong_candidates.csv").exists()
    assert (tmp_path / "order_candidates.csv").exists()
    assert (tmp_path / "previous_candidates.csv").exists()


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
    rules = {
        "top_alert_count": 1,
        "filters": [{"field": "smart_money_score", "operator": ">=", "value": 70}],
    }
    message = build_realtime_slack_message(df, rules, "premarket", {"S"})

    assert "전체 후보: 2개" in message
    assert "수급 강한 후보: 2개" in message
    assert "거래량 2배 이상: 2개" in message
    assert "TOP 1" in message
    assert "1. MDB - 수급/모멘텀 강한 후보" in message
    assert "2. S" not in message
    assert "신규 등장:\n\n* MDB" in message
    assert "반복 등장:\n\n* S" in message
    assert "수급 리더:\n\n* MDB, S" in message
    assert "* 프리마켓/애프터마켓에서는 주문하지 않고 후보만 기록" in message
