import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from market_guard import is_us_trading_day
from slack_utils import send_system_health_message

BASE_DIR = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")


def run_cmd(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True).strip()
    except Exception as exc:
        return f"ERROR: {exc}"


def count_rows(filename):
    path = BASE_DIR / filename
    if not path.exists():
        return 0
    try:
        return len(pd.read_csv(path))
    except Exception:
        return 0


def summarize_performance():
    path = BASE_DIR / "performance_trades.csv"
    if not path.exists():
        return "📈 페이퍼 트레이딩 성과\n- 거래 기록 없음"

    try:
        df = pd.read_csv(path)
    except Exception:
        return "📈 페이퍼 트레이딩 성과\n- 성과 파일 읽기 실패"

    if df.empty:
        return "📈 페이퍼 트레이딩 성과\n- 거래 기록 없음"

    pnl_cols = [c for c in df.columns if c.lower() in {"pnl", "profit", "profit_loss", "realized_pnl", "unrealized_pl"}]
    pnl_col = pnl_cols[0] if pnl_cols else None

    total = len(df)

    if pnl_col:
        pnl = pd.to_numeric(df[pnl_col], errors="coerce").fillna(0)
        wins = int((pnl > 0).sum())
        losses = int((pnl < 0).sum())
        win_rate = (wins / total * 100) if total else 0
        total_pnl = pnl.sum()

        return f"""📈 페이퍼 트레이딩 성과

- 총 거래: {total}건
- 승리: {wins}건
- 패배: {losses}건
- 승률: {win_rate:.1f}%
- 누적 손익: {total_pnl:.2f}
"""

    return f"""📈 페이퍼 트레이딩 성과

- 총 거래: {total}건
- 손익 컬럼 없음
"""


def main():
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")

    git_branch = run_cmd("git rev-parse --abbrev-ref HEAD")
    git_commit = run_cmd("git log --oneline -1")
    git_status = run_cmd("git status --short")

    market_day = is_us_trading_day()

    candidates = count_rows("candidates.csv")
    strong_candidates = count_rows("strong_candidates.csv")
    order_candidates = count_rows("order_candidates.csv")
    technical_logs = count_rows("technical_filter_log.csv")

    issues = []

    if git_status:
        issues.append("Git 변경 파일이 남아 있습니다.")

    status = "정상 운영 중" if not issues else "확인 필요"

    message = f"""📊 미국주식 자동매매 상태 점검

점검시각
- {now}

시장상태
- 미국 증시 개장일: {'예' if market_day else '아니오'}

Git 상태
- 브랜치: {git_branch}
- 최신 커밋: {git_commit}
- 변경 파일 존재: {'예' if git_status else '아니오'}

스캐너 현황
- 후보 종목 수: {candidates}
- 강한 후보 수: {strong_candidates}
- 주문 후보 수: {order_candidates}
- 기술필터 로그 수: {technical_logs}

시스템 상태
- {status}

점검 결과
{chr(10).join('- ' + issue for issue in issues) if issues else '- 이상 없음'}

{summarize_performance()}
"""

    print(message)
    if not send_system_health_message(message):
        print("SYSTEM_HEALTH_NOTIFICATION_UNCONFIGURED_OR_FAILED")


if __name__ == "__main__":
    main()
