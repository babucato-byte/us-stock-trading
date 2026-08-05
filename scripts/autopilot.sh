#!/bin/bash
# 자율 개발 루프 — 자리를 비우기 전에 실행하면 Claude Code가 백로그를 이어서 처리한다.
#
# 사용법:
#   ./scripts/autopilot.sh        # 5 사이클
#   ./scripts/autopilot.sh 10     # 10 사이클
#
# tmux 안에서 실행 권장:
#   tmux new -s autopilot './scripts/autopilot.sh 10'
#   (분리: Ctrl+b d / 재접속: tmux attach -t autopilot)

set -u
cd "$(dirname "$0")/.."

CYCLES="${1:-5}"
LOGDIR="logs/autopilot"
mkdir -p "$LOGDIR"

for i in $(seq 1 "$CYCLES"); do
  TS="$(date +%Y%m%d_%H%M%S)"
  LOG="$LOGDIR/cycle_${TS}.log"
  echo "=== autopilot cycle $i/$CYCLES ($TS) ==="

  claude -p "docs/autonomous/AUTOPILOT.md를 읽고 그 계약대로 docs/autonomous/BACKLOG.md의 ready 상태 최상위 항목 1개를 완전히 처리하라. 처리 후 BACKLOG와 CURRENT_STATUS를 갱신하고 커밋하라. ready 항목이 하나도 없으면 'NO_READY_TASKS'만 출력하고 종료하라." \
    --permission-mode acceptEdits \
    --allowedTools "Bash(venv/bin/python:*) Bash(git:*) Bash(python3:*) Read Write Edit Glob Grep Task" \
    2>&1 | tee "$LOG"

  if grep -q "NO_READY_TASKS" "$LOG"; then
    echo "백로그의 ready 항목이 없습니다. 종료."
    break
  fi
done

echo "=== autopilot 종료. 로그: $LOGDIR/ ==="
