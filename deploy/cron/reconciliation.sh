#!/bin/bash
# The reconciliation pass, on a schedule.
#
# Why this exists
# ---------------
# It did not. Nothing ran reconciliation automatically -- no cron, no
# timer, no wrapper -- so the order ledger was only ever settled when
# someone ran it by hand. On 2026-08-28 S6's first autonomous lifecycle
# completed correctly and BOTH legs sat at ACCEPTED for two hours, until
# a manual pass matched them against KIS fill history and moved them to
# FILLED.
#
# The position lifecycle was fine throughout: `sync_buy_fills` in the S6
# runtime moves POSITIONS from SUBMITTED to OPEN. What had no scheduler
# was settling the ORDER LEDGER, and an order stuck at ACCEPTED that is
# really filled is the exact condition that once blocked every buy for a
# week.
#
# Cadence
# -------
# Five minutes, not one. A pass takes sixty to ninety seconds, almost
# all of it the shared KIS read pacing, so a one-minute cron would mean
# continuous broker reads -- the precise shape of workload that starved
# S1's executor on 2026-08-27 and ended with a watchdog disabling
# entries account-wide. Five minutes gives three to five times headroom,
# and reconciliation is a safety net rather than a latency-critical
# path: the cost of settling a ledger row a few minutes later is
# nothing, and the cost of crowding out position management is a real
# holding going unmanaged.
#
# It is READ-ONLY against KIS by construction -- get_positions,
# get_open_orders and get_fills only, each of which validates
# read-allowed. It cannot submit or cancel an order.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a

. "$SCRIPT_DIR/shared_env.sh"

resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1

cd "$SCANNER_RUNTIME_ROOT" || exit 1

LOG="${SCANNER_DATA_ROOT}/logs/cron/reconciliation.log"
mkdir -p "$(dirname "$LOG")" /home/ubuntu/logs/cron

# Its OWN lock, not the S6 execution lock. Reconciliation is
# account-wide -- it settles S1's orders as much as S6's -- and sharing
# a per-strategy lock would make one strategy's busy cycle delay the
# safety net for every other.
#
# -E 99 so a skipped overlap is distinguishable in the log from a pass
# that ran and failed.
flock -n -E 99 /home/ubuntu/logs/cron/reconciliation.lock \
  env TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      KIS_LOCK_OWNER=RECONCILIATION \
  "$SCANNER_RUNTIME_ROOT/venv/bin/python" \
    "$SCANNER_RUNTIME_ROOT/scripts/run_reconciliation.py" \
    >> "$LOG" 2>&1
STATUS=$?

if [ "$STATUS" -eq 99 ]; then
    echo "$(date -u +%FT%TZ) OVERLAP_SKIPPED a reconciliation pass is still running" >> "$LOG"
    exit 0
fi
echo "$(date -u +%FT%TZ) PASS_COMPLETE status=$STATUS sha=$SCANNER_SHA" >> "$LOG"
# Exit 2 is "KIS could not be read, nothing recorded" -- a real outcome
# the entrypoint defines, and a transient one. It is logged and not
# escalated to cron as a failure; a persistent read outage surfaces
# through the reconciliation freshness check, which is what exists to
# notice it.
[ "$STATUS" -eq 2 ] && exit 0
exit "$STATUS"
