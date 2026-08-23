#!/bin/bash
# REGULAR-session S2 candidate producer.
#
# The SAME accumulation scanner the daily profile runs -- same config,
# same conditions, same score. Only the session label and the universe
# differ: the active pool, because a full-universe pass takes far longer
# than the window it describes.
#
# The ET guard is here rather than in cron because cronie has no CRON_TZ.
#
# S2 is DISCOVERY_ONLY and has no executor. It publishes anyway, into the
# SAME shared store as every other producer: the hand-off record is the
# research dataset, and a research dataset written somewhere a reader
# cannot find is not one. It had the identical directory split S6 had --
# see deploy/cron/shared_env.sh.
#
# Publishing is not permission to trade. Candidate Decision stays
# disabled, s2_live has no executor, and nothing here changes either.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/shared_env.sh"

cd "$SCANNER_RUNTIME_ROOT" || exit 1

resolve_shared_candidate_dir || exit 1

H=$(TZ=America/New_York date +%H%M)
# REGULAR is 09:30-16:00 ET. Producing outside it would publish rows
# labelled REGULAR from a session that is not REGULAR.
[ "$H" -ge 0930 ] && [ "$H" -lt 1600 ] || exit 0

LOG="$SCANNER_RUNTIME_ROOT/logs/cron/s2_regular_scan.log"
echo "$(date -u +%FT%TZ) candidates=$SCANNER_CANDIDATE_DIR" >> "$LOG"
flock -n /home/ubuntu/logs/cron/s2_scan.lock \
  env SCANNER_CANDIDATE_DIR="$SCANNER_CANDIDATE_DIR" \
      TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
  venv/bin/python scripts/run_scanners.py \
    --scanners accumulation --session REGULAR --universe active \
    >> "$LOG" 2>&1
