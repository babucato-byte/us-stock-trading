import sys

import pandas as pd

import gpt_analysis
from ai_analysis.candidate_analyzer import OUTPUT_COLUMNS, analyze_candidate_rows
from ai_analysis.provider_config import ProviderConfig


def sample_rows():
    return [
        {
            "symbol": "AAPL",
            "type": "momentum",
            "price": 190.25,
            "rsi": 58,
            "volume_ratio": 2.4,
            "score": 82,
            "smart_money_score": 76,
            "avg_dollar_volume": 100000000,
            "scan_time": "2026-06-04 08:00:00",
        }
    ]


def clear_ai_env(monkeypatch):
    for key in [
        "AI_ANALYSIS_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "AI_ANALYSIS_LIMIT",
        "GPT_ANALYSIS_LIMIT",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_no_api_key_uses_fallback(monkeypatch):
    clear_ai_env(monkeypatch)

    config = ProviderConfig.from_env()
    results = analyze_candidate_rows(sample_rows(), config=config)

    assert config.selected_provider() == "fallback"
    assert results[0]["provider"] == "fallback"
    assert results[0]["model"] == "fallback"
    assert "정규장 재확인 필요" in results[0]["action_note"]


def test_provider_fallback_forces_fallback(monkeypatch):
    clear_ai_env(monkeypatch)
    monkeypatch.setenv("AI_ANALYSIS_PROVIDER", "fallback")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    config = ProviderConfig.from_env()
    results = analyze_candidate_rows(sample_rows(), config=config)

    assert config.selected_provider() == "fallback"
    assert results[0]["provider"] == "fallback"


def test_analysis_csv_generation_with_provider_columns(tmp_path, monkeypatch):
    clear_ai_env(monkeypatch)
    monkeypatch.setenv("AI_ANALYSIS_PROVIDER", "fallback")
    monkeypatch.setattr(gpt_analysis, "BASE_DIR", tmp_path)
    monkeypatch.setattr(gpt_analysis, "OUTPUT_FILE", tmp_path / "gpt_candidate_analysis.csv")

    pd.DataFrame(sample_rows()).to_csv(tmp_path / "strong_candidates.csv", index=False)
    output = gpt_analysis.analyze_candidates(limit=10)
    saved = pd.read_csv(tmp_path / "gpt_candidate_analysis.csv")

    assert output.columns.tolist() == OUTPUT_COLUMNS
    assert "provider" in saved.columns
    assert "model" in saved.columns
    assert saved.iloc[0]["provider"] == "fallback"
    assert saved.iloc[0]["model"] == "fallback"


def test_ai_analysis_is_independent_from_order_modules(monkeypatch):
    clear_ai_env(monkeypatch)
    sys.modules.pop("paper_strategy_order", None)
    sys.modules.pop("order_monitor", None)

    results = analyze_candidate_rows(sample_rows(), config=ProviderConfig.from_env())

    assert results
    assert "paper_strategy_order" not in sys.modules
    assert "order_monitor" not in sys.modules
