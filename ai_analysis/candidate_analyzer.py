from datetime import datetime

from . import fallback_analyzer
from .provider_config import ProviderConfig


OUTPUT_COLUMNS = [
    "symbol",
    "summary",
    "risk_level",
    "reason",
    "action_note",
    "gpt_score",
    "analyzed_at",
    "provider",
    "model",
]


def analyze_candidate_rows(rows, config=None):
    config = config or ProviderConfig.from_env()
    provider = config.selected_provider()
    if provider == "openai":
        try:
            from .openai_analyzer import analyze

            return normalize_results(analyze(rows, config), rows, provider, config.openai_model)
        except Exception as exc:
            return fallback_with_reason(rows, f"OpenAI API 분석 실패: {exc}")
    if provider == "gemini":
        try:
            from .gemini_analyzer import analyze

            return normalize_results(analyze(rows, config), rows, provider, config.gemini_model)
        except Exception as exc:
            return fallback_with_reason(rows, f"Gemini API 분석 실패: {exc}")
    return fallback_analyzer.analyze(rows)


def normalize_results(items, rows, provider, model):
    analyzed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fallback_by_symbol = {str(item.get("symbol", "")): item for item in fallback_analyzer.analyze(rows)}
    normalized = []
    for row in rows:
        symbol = str(row.get("symbol", ""))
        item = next((candidate for candidate in items if str(candidate.get("symbol", "")) == symbol), None)
        base = fallback_by_symbol.get(symbol, fallback_analyzer.analyze_row(row, analyzed_at=analyzed_at))
        if not item:
            normalized.append(base)
            continue
        action_note = str(item.get("action_note") or base["action_note"])
        if "정규장 재확인 필요" not in action_note:
            action_note = f"{action_note} 정규장 재확인 필요."
        normalized.append(
            {
                "symbol": symbol,
                "summary": item.get("summary") or base["summary"],
                "risk_level": normalize_risk_level(item.get("risk_level")),
                "reason": item.get("reason") or base["reason"],
                "action_note": action_note,
                "gpt_score": normalize_score(item.get("gpt_score", base["gpt_score"])),
                "analyzed_at": analyzed_at,
                "provider": provider,
                "model": model,
            }
        )
    return normalized


def fallback_with_reason(rows, reason):
    results = fallback_analyzer.analyze(rows)
    for item in results:
        item["reason"] = f"{item['reason']} {reason}"
    return results


def normalize_risk_level(value):
    value = str(value or "medium").strip().lower()
    return value if value in {"low", "medium", "high"} else "medium"


def normalize_score(value):
    try:
        return max(0, min(100, int(float(value))))
    except Exception:
        return 0
