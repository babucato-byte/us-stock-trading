import json

import requests

from .openai_analyzer import build_prompt


def analyze(rows, config):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.gemini_model}:generateContent"
    )
    response = requests.post(
        url,
        params={"key": config.gemini_api_key},
        json={
            "contents": [{"parts": [{"text": build_prompt(rows)}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        parsed = parsed.get("candidates", parsed.get("analysis", []))
    return parsed if isinstance(parsed, list) else []
