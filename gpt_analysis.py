import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from ai_analysis.candidate_analyzer import OUTPUT_COLUMNS, analyze_candidate_rows
from ai_analysis.provider_config import ProviderConfig

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "gpt_candidate_analysis.csv"


def load_candidate_source():
    for name in ["strong_candidates.csv", "candidates.csv"]:
        path = BASE_DIR / name
        if path.exists():
            df = pd.read_csv(path)
            if not df.empty:
                return df, name
    return pd.DataFrame(), None


def analyze_candidates(limit=10):
    df, source = load_candidate_source()
    if df.empty:
        empty = pd.DataFrame(columns=OUTPUT_COLUMNS)
        empty.to_csv(OUTPUT_FILE, index=False)
        print("분석할 후보 종목이 없습니다.")
        return empty

    sort_cols = [col for col in ["smart_money_score", "score", "volume_ratio"] if col in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols, ascending=False)
    rows = df.head(limit).to_dict("records")

    config = ProviderConfig.from_env()
    results = analyze_candidate_rows(rows, config=config)
    output = pd.DataFrame(results, columns=OUTPUT_COLUMNS)
    output.to_csv(OUTPUT_FILE, index=False)
    print(
        f"{source} 상위 {len(output):,}건 AI 분석 저장 완료: "
        f"{config.provider_label()} -> {OUTPUT_FILE.name}"
    )
    return output


def analysis_limit_from_env():
    return int(os.getenv("AI_ANALYSIS_LIMIT", os.getenv("GPT_ANALYSIS_LIMIT", "10")))


if __name__ == "__main__":
    analyze_candidates(limit=analysis_limit_from_env())
