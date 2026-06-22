import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from market_guard import is_us_trading_day
from slack_utils import send_slack_alert

BASE_DIR = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")


def run_cmd(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True).strip()
    except Exception as exc:
        return f"ERROR: {exc}"


def count_rows(path):
    file_path = BASE_DIR / path
    if not file_path.exists():
        return 0
    try:
        df = pd.read_csv(file_path)
        return len(df)
    except Exception:
        return 0


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

    status = "HEALTHY"
    issues = []

    if git_status:
        status = "CHECK"
        issues.append("Git working tree has changes")

    message = f"""=== Trading Health Check ===

Time: {now}
Status: {status}

Market Day: {'YES' if market_day else 'NO'}

Git:
- Branch: {git_branch}
- Commit: {git_commit}
- Dirty: {'YES' if git_status else 'NO'}

Scanner Files:
- Candidates: {candidates}
- Strong Candidates: {strong_candidates}
- Order Candidates: {order_candidates}
- Technical Log Rows: {technical_logs}

Issues:
{chr(10).join('- ' + issue for issue in issues) if issues else '- None'}
"""

    print(message)
    send_slack_alert(message)


if __name__ == "__main__":
    main()
