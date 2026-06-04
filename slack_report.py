from pathlib import Path
import sys

import pandas as pd

from broker import BrokerConfig
from market_hours import get_us_market_session
from performance_analytics import generate_performance_report
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
        return "없음"
    return ", ".join(df["symbol"].dropna().astype(str).head(limit).tolist()) or "없음"


def _market_state_label(value):
    labels = {
        "premarket": "프리마켓",
        "regular": "정규장",
        "aftermarket": "애프터마켓",
        "afterhours": "애프터마켓",
        "closed": "장 마감",
    }
    return labels.get(str(value), str(value))


def _broker_mode_label(value):
    labels = {
        "PAPER": "모의투자(PAPER)",
        "LIVE_DRY_RUN": "실거래 예행연습(LIVE_DRY_RUN)",
        "LIVE_DISABLED": "실거래 비활성화(LIVE_DISABLED)",
        "LIVE_ENABLED": "실거래 활성화(LIVE_ENABLED)",
    }
    return labels.get(str(value), str(value))


def format_count(value):
    return f"{int(value):,}개"


def build_daily_summary():
    candidates = _read_csv("candidates.csv")
    strong = _read_csv("strong_candidates.csv")
    orders = _read_csv("order_candidates.csv")
    gpt = _read_csv("gpt_candidate_analysis.csv")
    backtest = _read_csv("backtest_results.csv")
    broker_config = BrokerConfig()
    performance_summary, _ = generate_performance_report()

    market_state = get_us_market_session()
    backtest_saved = "저장 결과: 0건"
    backtest_top = "상위 종목: 없음"
    if not backtest.empty:
        backtest_saved = f"저장 결과: {len(backtest):,}건"
        if "symbol" in backtest.columns:
            backtest_top = f"상위 종목: {_top_symbols(backtest)}"

    gpt_done = "분석 완료: 0건"
    gpt_top = "주요 종목: 없음"
    if not gpt.empty:
        gpt_done = f"분석 완료: {len(gpt):,}건"
        gpt_top = f"주요 종목: {_top_symbols(gpt)}"

    return "\n".join(
        [
            "📊 일일 운영 리포트",
            "",
            "시장 상태",
            f"- {_market_state_label(market_state)}",
            "",
            "거래 모드",
            f"- {_broker_mode_label(broker_config.status_label)}",
            "",
            "🔍 후보 종목 현황",
            f"- 전체 후보: {format_count(len(candidates))}",
            f"- 수급 강한 후보: {format_count(len(strong))}",
            f"- 주문 검토 후보: {format_count(len(orders))}",
            f"- 상위 후보: {_top_symbols(candidates)}",
            "",
            "백테스트 요약",
            f"- {backtest_saved}",
            f"- {backtest_top}",
            "",
            "🤖 AI 분석 현황",
            f"- {gpt_done}",
            f"- {gpt_top}",
            "",
            "📈 성과 요약",
            f"- 승률: {format_pct(performance_summary.get('win_rate'))}",
            f"- 손익비: {format_number(performance_summary.get('profit_factor'))}",
            f"- 일일 수익률: {format_signed_pct(performance_summary.get('daily_return_pct'))}",
            f"- 미실현 손익: {format_money(performance_summary.get('total_unrealized_pl'))}",
            f"- 보유 종목 수: {format_count(performance_summary.get('open_positions', 0))}",
            "",
            "⚠️ 리스크 상태",
            "- 실거래: 비활성화",
            "- Dry Run: 활성화",
            "- Dashboard에서 실거래 활성화 불가",
        ]
    )


def format_pct(value):
    return f"{float(value or 0):.2f}%"


def format_signed_pct(value):
    number = float(value or 0)
    return f"{number:+.2f}%"


def format_money(value):
    number = float(value or 0)
    sign = "+" if number >= 0 else "-"
    return f"{sign}${abs(number):,.2f}"


def format_number(value):
    return f"{float(value or 0):,.2f}"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    message = build_daily_summary()
    print(message)
    send_slack_message(message)


if __name__ == "__main__":
    main()
