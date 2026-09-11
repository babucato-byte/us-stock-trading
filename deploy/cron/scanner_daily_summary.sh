#!/bin/bash
# One consolidated S1-S5 scanner summary after all four ET sessions close.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/shared_env.sh"
resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1
LOG="${SCANNER_DATA_ROOT}/logs/cron/scanner_daily_summary.log"
mkdir -p "$(dirname "$LOG")"
echo "$(date -u +%FT%TZ) daily scanner summary sha=$SCANNER_SHA" >> "$LOG"
cd "$SCANNER_RUNTIME_ROOT" || exit 1
flock -n "${SCANNER_DATA_ROOT}/logs/cron/scanner_daily_summary.lock" \
  env TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      SCANNER_ANALYTICS_DIR="$SCANNER_ANALYTICS_DIR" \
      SCANNER_LOG_DIR="$SCANNER_LOG_DIR" \
  "$SCANNER_RUNTIME_ROOT/venv/bin/python" scripts/run_daily_scanner_summary.py >> "$LOG" 2>&1
echo "$(date -u +%FT%TZ) DAILY_SCANNER_SUMMARY_COMPLETE status=$? sha=$SCANNER_SHA" >> "$LOG"
