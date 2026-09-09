#!/bin/bash
# The S6 BUY execution worker.
#
# Why this exists
# ---------------
# `s6_buy_entry.sh` (`--strategy s6`) used to call the shared
# qualify->KIS->submit cycle inline, for every READY candidate, in the
# SAME process as fast-watch's own WATCHING/READY evaluation. Measured
# 2026-09-09: that shared cycle costs ~44-45 seconds PER READY
# CANDIDATE (three-plus sequential, rate-limited KIS network calls), so
# even one READY candidate pushed the tick past the 60-second cron
# interval and OVERLAP_SKIPPED the next one -- the fast-watch tick
# itself, not just this worker.
#
# `--strategy s6` now ONLY decides READY and writes a durable
# BUY_INTENT (s6_live/buy_intent.py); it never reaches the shared
# cycle. THIS script is the other half: it claims that queue and runs
# the exact same, unchanged shared cycle those candidates would have
# gone through inline before -- on its own cadence, its own lock, so a
# slow qualify/KIS/submit sequence here can never make the next
# fast-watch tick late.
#
# Two DIFFERENT NEW locks, and why neither is s6_entry.lock or s6_exec.lock
# ---------------------------------------------------------------------
# `s6_buy_execution.lock` (here) stops two EXECUTION WORKER ticks
# overlapping each other. It is deliberately NOT `s6_entry.lock`: that
# one still serialises fast-watch's own ticks, a completely independent
# concern now that fast-watch never blocks on this worker.
#
# `S6_EXECUTION_LOCK_FILE=s6_exec.lock` below is the OTHER lock,
# unchanged and intentionally shared: it still serialises the actual
# BROKER MUTATION (execution/execution_lock.py, acquired inside Python
# around the submit call alone) against the exit runtime's fill-sync
# and position management, exactly as it always has for the entry
# cycle. This worker submitting through the same shared cycle must
# keep coordinating with exits through the same lock a 2026-09-02
# incident already showed is load-bearing.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a

. "$SCRIPT_DIR/shared_env.sh"

resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1
resolve_shared_candidate_dir || exit 1

cd "$SCANNER_RUNTIME_ROOT" || exit 1

LOG="${SCANNER_DATA_ROOT}/logs/cron/s6_buy_execution.log"
mkdir -p "$(dirname "$LOG")" /home/ubuntu/logs/cron
echo "$(date -u +%FT%TZ) tick sha=$SCANNER_SHA root=$SCANNER_RUNTIME_ROOT" >> "$LOG"

flock -n -E 99 /home/ubuntu/logs/cron/s6_buy_execution.lock \
  env TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      SCANNER_CANDIDATE_DIR="${SCANNER_CANDIDATE_DIR:-}" \
      KIS_LOCK_OWNER=S6_BUY_EXECUTION \
      KIS_LOCK_ACQUIRE_TIMEOUT_SECONDS=1 \
      S6_EXECUTION_LOCK_FILE=/home/ubuntu/logs/cron/s6_exec.lock \
  "$SCANNER_RUNTIME_ROOT/venv/bin/python" \
    "$SCANNER_RUNTIME_ROOT/scripts/run_live_buy_entry.py" --strategy s6_buy_worker \
    >> "$LOG" 2>&1
STATUS=$?
if [ "$STATUS" -eq 99 ]; then
    echo "$(date -u +%FT%TZ) OVERLAP_SKIPPED lock=/home/ubuntu/logs/cron/s6_buy_execution.lock (the previous execution tick is still running; this tick is dropped, not queued)" >> "$LOG"
    exit 0
fi
echo "$(date -u +%FT%TZ) LOCK_ACQUIRED status=$STATUS" >> "$LOG"
exit "$STATUS"
