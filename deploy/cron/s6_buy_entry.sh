#!/bin/bash
# The S6 BUY entry tick.
#
# Why this exists
# ---------------
# It did not. `s6_scan.sh` discovered candidates, `s6_exec.sh` synced
# fills and ran exits, `s6_exit_monitor.sh` watched positions -- and
# nothing ran the entry. On 2026-08-27 the REGULAR funnel reached
# READY_TO_BUY 12 with a candidate whose single share was affordable, and
# no order was ever submitted, because every BUY the system had ever made
# was invoked by hand.
#
# Cadence
# -------
# Every minute, matching the precision watch. A candidate becomes READY
# on a one-minute evaluation and the point of that cadence is to act on
# it, not to notice it and wait for a quarter-hourly tick.
#
# Overlap
# -------
# flock -n, not -w: an entry cycle still running when the next minute
# fires is SKIPPED, never queued. A queued second cycle would evaluate
# the same candidate against the same READY snapshot while the first was
# already submitting for it, and the duplicate protections downstream
# would then be the only thing between that and two orders. They would
# hold -- the idempotency ledger and the symbol lock are durable -- but
# the correct place to not send a second order is before deciding to.
#
# The lock is SHARED WITH s6_exec.sh deliberately. The runtime tick syncs
# fills and can open positions from them; an entry deciding "this symbol
# is flat" while that is landing is the race the shared lock removes.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# The credentials, the account number and the rollout flags. Without
# this the runner has no KIS_LIVE_ORDER_ENABLED and no token, so every
# tick refuses -- which looks exactly like "no candidate today" in the
# log and would have made the whole schedule a silent no-op.
#
# Loaded BEFORE the release check, never instead of it:
# `resolve_release_root` reads the deployed and validated SHAs from the
# file itself rather than from the environment, so an env file that
# names a root cannot talk the verification out of comparing it.
ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a

. "$SCRIPT_DIR/shared_env.sh"

# Code from the release, verified against the deployed and validated
# SHAs. No fallback: an entry cycle is the one place where running
# unverified code spends real money.
resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1
# The entry reads the same published candidates the scanner writes.
resolve_shared_candidate_dir || exit 1

cd "$SCANNER_RUNTIME_ROOT" || exit 1

LOG="${SCANNER_DATA_ROOT}/logs/cron/s6_buy_entry.log"
mkdir -p "$(dirname "$LOG")" /home/ubuntu/logs/cron
echo "$(date -u +%FT%TZ) tick sha=$SCANNER_SHA root=$SCANNER_RUNTIME_ROOT" >> "$LOG"

# -E 99 gives contention its own exit code, so a skipped overlap is
# distinguishable in the log from a runner that failed. Without it both
# arrive as exit 1 and a minute where the entry never ran reads the same
# as a minute where it ran and crashed.
flock -n -E 99 /home/ubuntu/logs/cron/s6_exec.lock \
  env TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      SCANNER_CANDIDATE_DIR="${SCANNER_CANDIDATE_DIR:-}" \
  "$SCANNER_RUNTIME_ROOT/venv/bin/python" \
    "$SCANNER_RUNTIME_ROOT/scripts/run_live_buy_entry.py" --strategy s6 \
    >> "$LOG" 2>&1
STATUS=$?
if [ "$STATUS" -eq 99 ]; then
    echo "$(date -u +%FT%TZ) OVERLAP_SKIPPED lock=/home/ubuntu/logs/cron/s6_exec.lock (an S6 execution cycle is already running; this tick is dropped, not queued)" >> "$LOG"
    exit 0
fi
echo "$(date -u +%FT%TZ) LOCK_ACQUIRED status=$STATUS" >> "$LOG"
exit "$STATUS"
