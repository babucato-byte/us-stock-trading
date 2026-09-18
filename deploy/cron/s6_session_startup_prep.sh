#!/bin/bash
# Prepare the NEXT session's startup universe, before it opens.
#
# Runs on a plain tick and decides nothing about the clock itself: the
# Python asks the production session truth whether this moment is inside
# the preparation window and NO-OPs otherwise. No session start time is
# written here -- 04:00/09:30/16:00/20:00 do not appear in this file, and
# 주간거래 is published in KST and moves against Eastern with US DST, so a
# time asserted in a wrapper would be wrong for half the year.
#
# There is deliberately NO fast-scan wrapper beside this one. The startup
# scan is the existing s6_scan.sh invocation that follows the boundary, so
# there is one scanner process, one lock and one publication owner.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Same environment every other production wrapper loads, and for the same
# reason: without it the KIS-backed reads inside the selection report
# success while authenticating against nothing.
ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a

. "$SCRIPT_DIR/shared_env.sh"

resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1
cd "$SCANNER_RUNTIME_ROOT" || exit 1
resolve_shared_candidate_dir || exit 1

LOG="${SCANNER_DATA_ROOT}/logs/cron/s6_session_startup_prep.log"

# Serialised against itself only. This never touches the broker-mutation
# lock and never takes the scanner lock: it reads a ranking and writes one
# file, and blocking a scan behind that would be the very stall this work
# exists to remove.
flock -n -E 99 /home/ubuntu/logs/cron/s6_session_startup_prep.lock \
  env SCANNER_CANDIDATE_DIR="$SCANNER_CANDIDATE_DIR" \
      TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      SCANNER_ANALYTICS_DIR="$SCANNER_ANALYTICS_DIR" \
      SCANNER_LOG_DIR="$SCANNER_LOG_DIR" \
      SCANNER_UNIVERSE_FILE="$SCANNER_UNIVERSE_FILE" \
  venv/bin/python -m scripts.prepare_session_startup >> "$LOG" 2>&1
STATUS=$?
if [ "$STATUS" -eq 99 ]; then
    echo "$(date -u +%FT%TZ) SESSION_STARTUP_PREP_SKIPPED -- a preparation is already running" >> "$LOG"
    exit 0
fi
exit 0
