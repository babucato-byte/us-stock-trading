import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "gpt_candidate_analysis.csv"


OUTPUT_COLUMNS = [
    "symbol",
    "summary",
    "risk_level",
    "reason",
    "action_note",
    "gpt_score",
    "analyzed_at",
]


def load_candidate_source():
    for name in ["strong_candidates.csv", "candidates.csv"]:
        path = BASE_DIR / name
        if path.exists():
            df = pd.read_csv(path)
            if not df.empty:
                return df, name
    return pd.DataFrame(), None


def heuristic_analysis(row):
    volume_ratio = float(row.get("volume_ratio", 0))
    rsi = float(row.get("rsi", 0))
    smart = float(row.get("smart_money_score", 0))
    score = float(row.get("score", 0))

    risk_level = "medium"
    if rsi >= 70 or volume_ratio >= 4:
        risk_level = "high"
    elif 45 <= rsi <= 62 and volume_ratio < 3:
        risk_level = "low"

    gpt_score = min(100, int(score * 0.55 + smart * 0.35 + min(volume_ratio, 5) * 2))
    return {
        "symbol": row["symbol"],
        "summary": "Rule-based fallback analysis. GPT API was not used.",
        "risk_level": risk_level,
        "reason": f"Score {score:.0f}, smart-money {smart:.0f}, RSI {rsi:.1f}, volume {volume_ratio:.2f}x.",
        "action_note": "Use only as a review aid. Re-check regular-session price, volume, spread, and news before any order.",
        "gpt_score": gpt_score,
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def call_gpt(rows):
    if not os.getenv("OPENAI_API_KEY"):
        return None

    try:
        from openai import OpenAI
    except Exception:
        return None

    client = OpenAI()
    payload = [
        {
            "symbol": row["symbol"],
            "price": row.get("price"),
            "rsi": row.get("rsi"),
            "volume_ratio": row.get("volume_ratio"),
            "score": row.get("score"),
            "smart_money_score": row.get("smart_money_score"),
        }
        for row in rows
    ]

    prompt = {
        "role": "user",
        "content": (
            "Analyze these US stock trading candidates as a risk review assistant only. "
            "Do not recommend automatic execution. Return JSON list with keys: symbol, summary, "
            "risk_level, reason, action_note, gpt_score. Cover risks, overheating, flow possibility, "
            "buying cautions, and whether regular-session re-check is needed.\n\n"
            + json.dumps(payload)
        ),
    }

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {
                "role": "system",
                "content": "You are a cautious trading-analysis assistant. You never authorize order execution.",
            },
            prompt,
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    content = response.choices[0].message.content
    parsed = json.loads(content)
    if isinstance(parsed, dict):
        parsed = parsed.get("candidates", parsed.get("analysis", []))
    return parsed if isinstance(parsed, list) else None


def analyze_candidates(limit=10):
    df, source = load_candidate_source()
    if df.empty:
        empty = pd.DataFrame(columns=OUTPUT_COLUMNS)
        empty.to_csv(OUTPUT_FILE, index=False)
        print("No candidates to analyze.")
        return empty

    sort_cols = [col for col in ["smart_money_score", "score", "volume_ratio"] if col in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols, ascending=False)
    rows = df.head(limit).to_dict("records")

    gpt_rows = call_gpt(rows)
    analyzed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results = []
    if gpt_rows:
        for item in gpt_rows:
            results.append(
                {
                    "symbol": item.get("symbol"),
                    "summary": item.get("summary", ""),
                    "risk_level": item.get("risk_level", "unknown"),
                    "reason": item.get("reason", ""),
                    "action_note": item.get("action_note", "Regular-session re-check required."),
                    "gpt_score": item.get("gpt_score", 0),
                    "analyzed_at": analyzed_at,
                }
            )
    else:
        results = [heuristic_analysis(row) for row in rows]

    output = pd.DataFrame(results, columns=OUTPUT_COLUMNS)
    output.to_csv(OUTPUT_FILE, index=False)
    print(f"Saved {len(output)} GPT analysis rows from {source} to {OUTPUT_FILE.name}")
    return output


if __name__ == "__main__":
    analyze_candidates(limit=int(os.getenv("GPT_ANALYSIS_LIMIT", "10")))
