#!/bin/bash
# S6 all-session scan. The scheduler picks the variant from the CLOCK,
# so one entry serves S6-R/O/P/A and two variants can never run at once.
#
# flock is -n: a scan still running when the next fires is SKIPPED, not
# queued. A queued second scan would publish a range built from bars the
# first was already using, and the executor cannot tell which produced
# the row it reads.
#
# The candidate directory comes from the RELEASE's shared env, not from
# this checkout -- see deploy/cron/shared_env.sh. Without it this scan
# published into /home/ubuntu/trading/logs/scanners/candidates while the
# trading runtime read shared/state/candidates, and both reported
# success.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/shared_env.sh"

cd "$SCANNER_RUNTIME_ROOT" || exit 1

# Before the session probe: an unresolvable store is a producer fault,
# and reporting it is worth more than reporting which session it was.
resolve_shared_candidate_dir || exit 1

SESSION=$(venv/bin/python -c "
from scanners.base import scan_session
from market_hours import get_market_state
print('CLOSED' if get_market_state() == 'CLOSED' else scan_session.session_at())
" 2>/dev/null)
# CLOSED: no scan, no entry. A holiday is a correct no-op.
[ "$SESSION" = "CLOSED" ] && exit 0
[ -n "$SESSION" ] || exit 0

LOG="$SCANNER_RUNTIME_ROOT/logs/cron/s6_scan.log"
echo "$(date -u +%FT%TZ) session=$SESSION candidates=$SCANNER_CANDIDATE_DIR" >> "$LOG"
flock -n /home/ubuntu/logs/cron/s6_scan.lock \
  env SCANNER_CANDIDATE_DIR="$SCANNER_CANDIDATE_DIR" \
      TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
  venv/bin/python scripts/run_scanners.py --scanners orb \
    --session "$SESSION" --universe active >> "$LOG" 2>&1
