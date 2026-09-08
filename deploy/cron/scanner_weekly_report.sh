#!/bin/bash
# The weekly scanner Slack summary, read from the RELEASE analytics tree.
#
# Why this exists
# ---------------
# The crontab ran `scripts/run_scanner_report.py weekly --slack` from the
# legacy checkout with TRADING_PROJECT_ROOT=/home/ubuntu/trading, so
# `result_store.analytics_dir()` resolved to /home/ubuntu/trading/logs/
# scanners -- a tree nothing has written since 2026-08-28. The release
# scanners write to $SCANNER_DATA_ROOT/logs/scanners. For the week of
# 2026-08-31 the message said "신호 총계: 0 / 실행 0회" while the release
# tree held 19,648 signals and 286 run manifests. This wrapper resolves
# the same directories the scans use (deploy/cron/shared_env.sh) and
# runs the report from the deployed release.
#
# Crontab line (Saturday 08:05 ET, dual UTC hour with the ET guard):
#   5 12,13 * * 6 [ "$(TZ=America/New_York date +\%H)" = "08" ] && ROOT=$(grep -m1 "^TRADING_PROJECT_ROOT=" /home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env | cut -d= -f2-) && "$ROOT/deploy/cron/scanner_weekly_report.sh"
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/shared_env.sh"
resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1
LOG="${SCANNER_DATA_ROOT}/logs/cron/scanner_weekly.log"
echo "$(date -u +%FT%TZ) weekly report sha=$SCANNER_SHA root=$SCANNER_RUNTIME_ROOT analytics=$SCANNER_ANALYTICS_DIR" >> "$LOG"
cd "$SCANNER_RUNTIME_ROOT" || exit 1
flock -n "${SCANNER_DATA_ROOT}/logs/cron/scanner_weekly.lock" \
  env TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      SCANNER_ANALYTICS_DIR="$SCANNER_ANALYTICS_DIR" \
      SCANNER_LOG_DIR="$SCANNER_LOG_DIR" \
  "$SCANNER_RUNTIME_ROOT/venv/bin/python" scripts/run_scanner_report.py weekly --slack >> "$LOG" 2>&1
echo "$(date -u +%FT%TZ) WEEKLY_COMPLETE status=$? sha=$SCANNER_SHA" >> "$LOG"
