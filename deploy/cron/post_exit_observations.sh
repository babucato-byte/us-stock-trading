#!/bin/bash
# Post-exit price observations, on a schedule.
#
# Why this exists
# ---------------
# It did not, and the absence was invisible because everything around it
# worked. Registration was wired into all three position stores; the
# schema, the roll-up and the analytics were all in place. Only the
# thing that RUNS the observations was missing: `due_for_observation`
# and `complete_expired` were called from tests and from nowhere else.
#
# On 2026-08-29 the live database held three tracked exits --
# DT/SESSION_EXIT, OWL/RANGE_REENTRY, SBS/SESSION_EXIT -- all still
# TRACKING, with `post_exit_observations` empty and every metric NULL.
# Three real exits, none of them measured, and a set of analytics with
# nothing to analyse.
#
# Cadence
# -------
# Five minutes. The shortest horizon is +5m, so a longer gap would mean
# routinely reading the M5 mark late; the runner refuses a bar more than
# two minutes from the mark, so late means UNAVAILABLE rather than a
# wrong number, and the observation would simply be lost.
#
# It costs nothing at the broker. The prices come from the snapshot the
# realtime collector already writes, so this adds no KIS call and no
# contention on the rate limiter that starved S1's executor once
# already. It takes its OWN lock for the same reason reconciliation
# does.
#
# It is research. It places no order, touches no position row, and
# changes no strategy parameter -- "Post-Exit 결과로 threshold/Exit/stop/
# TP 자동 변경 금지". It writes observation rows for a person to read.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a

. "$SCRIPT_DIR/shared_env.sh"

resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1

cd "$SCANNER_RUNTIME_ROOT" || exit 1

LOG="${SCANNER_DATA_ROOT}/logs/cron/post_exit_observations.log"
mkdir -p "$(dirname "$LOG")" /home/ubuntu/logs/cron

# -E 99 so a skipped overlap is distinguishable in the log from a pass
# that ran and failed.
flock -n -E 99 /home/ubuntu/logs/cron/post_exit_observations.lock \
  env TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
  "$SCANNER_RUNTIME_ROOT/venv/bin/python" \
    "$SCANNER_RUNTIME_ROOT/scripts/run_post_exit_observations.py" \
    >> "$LOG" 2>&1
STATUS=$?

if [ "$STATUS" -eq 99 ]; then
    echo "$(date -u +%FT%TZ) OVERLAP_SKIPPED an observation pass is still running" >> "$LOG"
    exit 0
fi
echo "$(date -u +%FT%TZ) PASS_COMPLETE status=$STATUS sha=$SCANNER_SHA" >> "$LOG"
exit "$STATUS"
