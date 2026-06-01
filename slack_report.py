from pathlib import Path

import pandas as pd

from broker import BrokerConfig
from market_hours import get_us_market_session
from slack_utils import send_slack_message


BASE_DIR = Path(__file__).resolve().parent


def _read_csv(name):
    path = BASE_DIR / name
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _top_symbols(df, limit=5):
    if df.empty or "symbol" not in df.columns:
        return "None"
    return ", ".join(df["symbol"].dropna().astype(str).head(limit).tolist()) or "None"


def build_daily_summary():
    candidates = _read_csv("candidates.csv")
    strong = _read_csv("strong_candidates.csv")
    orders = _read_csv("order_candidates.csv")
    gpt = _read_csv("gpt_candidate_analysis.csv")
    backtest = _read_csv("backtest_results.csv")
    broker_config = BrokerConfig()

    market_state = get_us_market_session()
    backtest_summary = "No backtest file"
    if not backtest.empty:
        backtest_summary = f"{len(backtest)} rows available"
        if "symbol" in backtest.columns:
            backtest_summary += f"; top: {_top_symbols(backtest)}"

    gpt_summary = "No GPT analysis"
    if not gpt.empty:
        gpt_summary = f"{len(gpt)} analyzed; top: {_top_symbols(gpt)}"

    return "\n".join(
        [
            "*Daily Value Report*",
            f"- Market state: {market_state}",
            f"- Broker guardrail: {broker_config.status_label}",
            "",
            "*Candidate summary*",
            f"- Total candidates: {len(candidates)}",
            f"- Strong candidates: {len(strong)}",
            f"- Order review candidates: {len(orders)}",
            f"- Top candidates: {_top_symbols(candidates)}",
            "",
            "*Backtest summary*",
            f"- {backtest_summary}",
            "",
            "*GPT summary*",
            f"- {gpt_summary}",
            "",
            "*Risk status*",
            "- ENABLE_REAL_TRADING default is False",
            "- LIVE_DRY_RUN default is True",
            "- Dashboard cannot enable live trading",
        ]
    )


def main():
    message = build_daily_summary()
    print(message)
    send_slack_message(message)


if __name__ == "__main__":
    main()
