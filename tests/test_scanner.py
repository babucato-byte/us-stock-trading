import pandas as pd

from daily_candidate_scanner import (
    CANDIDATES_FILE,
    ORDER_CANDIDATES_FILE,
    PREVIOUS_CANDIDATES_FILE,
    STRONG_CANDIDATES_FILE,
    TECHNICAL_FILTER_LOG_FILE,
    apply_filters,
    build_candidate_buckets,
    build_realtime_slack_message,
    save_technical_filter_log,
    load_scanner_rules,
    resolve_scan_limit,
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


def test_order_candidates_preserve_existing_flow_when_technical_filter_off(monkeypatch):
    monkeypatch.setenv("USE_TECHNICAL_ENTRY_FILTER", "false")
    df = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "score": 70,
                "smart_money_score": 70,
                "volume_ratio": 2.2,
                "technical_filter_pass": False,
            }
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

    assert buckets.order_candidates["symbol"].tolist() == ["AAPL"]


def test_order_candidates_exclude_technical_filter_failures_when_on(monkeypatch):
    monkeypatch.setenv("USE_TECHNICAL_ENTRY_FILTER", "true")
    df = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "score": 70,
                "smart_money_score": 70,
                "volume_ratio": 2.2,
                "technical_filter_pass": True,
            },
            {
                "symbol": "TSLA",
                "score": 80,
                "smart_money_score": 70,
                "volume_ratio": 2.4,
                "technical_filter_pass": False,
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

    assert buckets.order_candidates["symbol"].tolist() == ["AAPL"]


def test_preset_load_uses_rule_engine_filters():
    rules = load_scanner_rules("smart_money")
    assert rules["active_preset"] == "smart_money"
    assert rules["top_alert_count"] == 7
    assert any(
        item["field"] == "smart_money_score" and item["value"] == 70
        for item in rules["filters"]
    )


def test_resolve_scan_limit_uses_existing_rule_when_unset(monkeypatch):
    monkeypatch.delenv("SCAN_LIMIT", raising=False)

    limit, enabled = resolve_scan_limit({"scan_limit": 1500})

    assert limit == 1500
    assert enabled is False


def test_resolve_scan_limit_uses_env_override(monkeypatch):
    monkeypatch.setenv("SCAN_LIMIT", "5")

    limit, enabled = resolve_scan_limit({"scan_limit": 1500})

    assert limit == 5
    assert enabled is True


def test_scan_limit_scans_no_more_than_requested(tmp_path, monkeypatch, capsys):
    import daily_candidate_scanner as scanner

    scanned = []

    monkeypatch.setenv("USE_TECHNICAL_ENTRY_FILTER", "false")
    monkeypatch.setattr(
        scanner,
        "load_scanner_rules",
        lambda preset_name=None: {
            "active_preset": "test",
            "scan_limit": 1500,
            "top_alert_count": 5,
            "filters": [],
        },
    )
    monkeypatch.setattr(
        scanner.pd,
        "read_csv",
        lambda path: pd.DataFrame({"symbol": [f"SYM{idx}" for idx in range(10)]}),
    )
    monkeypatch.setattr(scanner, "CANDIDATES_FILE", tmp_path / CANDIDATES_FILE.name)
    monkeypatch.setattr(scanner, "STRONG_CANDIDATES_FILE", tmp_path / STRONG_CANDIDATES_FILE.name)
    monkeypatch.setattr(scanner, "ORDER_CANDIDATES_FILE", tmp_path / ORDER_CANDIDATES_FILE.name)
    monkeypatch.setattr(scanner, "PREVIOUS_CANDIDATES_FILE", tmp_path / PREVIOUS_CANDIDATES_FILE.name)
    monkeypatch.setattr(scanner, "TECHNICAL_FILTER_LOG_FILE", tmp_path / TECHNICAL_FILTER_LOG_FILE.name)
    monkeypatch.setattr(scanner, "load_previous_symbols", lambda: set())
    monkeypatch.setattr(scanner, "get_us_market_session", lambda: "regular")

    def fake_analyze(symbol, rules, **kwargs):
        scanned.append(symbol)
        return None

    monkeypatch.setattr(scanner, "analyze", fake_analyze)

    scanner.scan(send_slack=False, scan_limit=5)

    assert scanned == ["SYM0", "SYM1", "SYM2", "SYM3", "SYM4"]
    assert "[SCAN LIMIT] enabled limit=5" in capsys.readouterr().out


def test_technical_filter_log_deduplicates_scan_symbol(tmp_path, monkeypatch):
    import daily_candidate_scanner as scanner

    log_file = tmp_path / "technical_filter_log.csv"
    monkeypatch.setattr(scanner, "TECHNICAL_FILTER_LOG_FILE", log_file)
    rows = [
        {
            "scan_id": "20260612_120000",
            "timestamp": "2026-06-12 12:00:00",
            "symbol": "AAPL",
            "current_price": 100,
            "technical_filter_pass": False,
            "technical_filter_score": 2,
            "price_above_hma200": False,
            "hma200_rising": False,
            "hma_macd_bullish": True,
            "macd_histogram_rising": True,
            "sqzmom_green": False,
            "volume_multiple": 1.2,
            "rsi": 55,
            "smart_money_score": 40,
            "preset": "smoke_test",
        },
        {
            "scan_id": "20260612_120000",
            "timestamp": "2026-06-12 12:00:01",
            "symbol": "AAPL",
            "current_price": 101,
            "technical_filter_pass": True,
            "technical_filter_score": 3,
            "price_above_hma200": True,
            "hma200_rising": False,
            "hma_macd_bullish": True,
            "macd_histogram_rising": True,
            "sqzmom_green": False,
            "volume_multiple": 1.3,
            "rsi": 56,
            "smart_money_score": 40,
            "preset": "smoke_test",
        },
    ]

    save_technical_filter_log(rows)

    saved = pd.read_csv(log_file)
    assert len(saved) == 1
    assert saved.loc[0, "symbol"] == "AAPL"
    assert bool(saved.loc[0, "technical_filter_pass"]) is True


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
