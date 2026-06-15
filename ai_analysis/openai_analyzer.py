import json


def build_prompt(rows):
    payload = [candidate_payload(row) for row in rows]
    return (
        "Analyze these US stock trading candidates as a Paper Trading risk-review assistant only. "
        "Do not recommend buying, do not encourage real trading, and do not authorize or imply order execution. "
        "Every action_note must include the Korean phrase '정규장 재확인 필요'. "
        "Return a JSON object with key candidates, containing a list. Each item must include: "
        "symbol, summary, risk_level(low|medium|high), reason, action_note, gpt_score(0-100). "
        "The summary should be 2 to 3 Korean sentences and framed only as review support.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def candidate_payload(row):
    return {
        "symbol": row.get("symbol"),
        "type": row.get("type"),
        "price": row.get("price"),
        "rsi": row.get("rsi"),
        "volume_ratio": row.get("volume_ratio"),
        "score": row.get("score"),
        "technical_score": row.get("technical_score", row.get("score")),
        "smart_money_score": row.get("smart_money_score"),
        "trend": row.get("trend"),
        "trend_score": row.get("trend_score"),
        "momentum_score": row.get("momentum_score"),
        "breakout_score": row.get("breakout_score"),
        "final_score": row.get("final_score"),
        "avg_dollar_volume": row.get("avg_dollar_volume"),
        "scan_time": row.get("scan_time"),
    }


def analyze(rows, config):
    from openai import OpenAI

    client = OpenAI(api_key=config.openai_api_key)
    response = client.chat.completions.create(
        model=config.openai_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a cautious trading-analysis assistant. "
                    "You never recommend buys and never authorize order execution."
                ),
            },
            {"role": "user", "content": build_prompt(rows)},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    content = response.choices[0].message.content
    parsed = json.loads(content)
    if isinstance(parsed, dict):
        parsed = parsed.get("candidates", parsed.get("analysis", []))
    return parsed if isinstance(parsed, list) else []
