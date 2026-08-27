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
# The script's own location decides where shared_env.sh is, and NOTHING
# else. It used to decide the project root too, which is how the scanner
# spent twenty commits running a mutable checkout while the trading
# runtime ran a validated release -- deployed scanner changes were inert
# in production and nothing errored, because both halves reported success
# about different code.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/shared_env.sh"

# Code from the release, verified against the deployed and validated
# SHAs. No fallback to a checkout: falling back is what produced the
# drift, and a scan that does not run is visible in a way that one
# running old code is not.
resolve_release_root || exit 1

# Writes go to shared mutable data, so running release code cannot
# dirty the release it is running.
resolve_scanner_data_dirs || exit 1

cd "$SCANNER_RUNTIME_ROOT" || exit 1

# Before the session probe: an unresolvable store is a producer fault,
# and reporting it is worth more than reporting which session it was.
resolve_shared_candidate_dir || exit 1

# Whether a scan may run is a CALENDAR question, not a regular-market-
# hours one. This used to ask `get_market_state() == "CLOSED"`, which is
# true for the whole of OVERNIGHT_DAYTIME, PREMARKET and AFTER_HOURS --
# so S6's all-session family could only ever scan in REGULAR, and in
# practice never scanned at all. `scan_window.probe` prints the session
# when a scan is allowed and the REFUSAL REASON otherwise, so a skipped
# window leaves a line instead of a silent gap.
SESSION=$(venv/bin/python -c "
from scanners.base import scan_window
print(scan_window.probe())
" 2>/dev/null)
[ -n "$SESSION" ] || exit 0

LOG="${SCANNER_DATA_ROOT}/logs/cron/s6_scan.log"
case "$SESSION" in
  PREMARKET|REGULAR|AFTER_HOURS|OVERNIGHT_DAYTIME) ;;
  *)
    # WEEKEND / US_MARKET_HOLIDAY / CALENDAR_UNAVAILABLE. A correct
    # no-op, recorded so "no scan" is never mistaken for "scan found
    # nothing".
    echo "$(date -u +%FT%TZ) skipped=$SESSION" >> "$LOG"
    exit 0;;
esac
# Derived from the candidate dir the shared env already resolved and
# validated, rather than from a second variable that could point
# somewhere else. `shared/state/candidates` and `shared/state/discovery`
# are siblings by construction.
MANIFEST_PATH="$(dirname "$SCANNER_CANDIDATE_DIR")/discovery/manifest.json"

echo "$(date -u +%FT%TZ) session=$SESSION scanner_sha=$SCANNER_SHA root=$SCANNER_RUNTIME_ROOT candidates=$SCANNER_CANDIDATE_DIR manifest=$MANIFEST_PATH" >> "$LOG"
flock -n /home/ubuntu/logs/cron/s6_scan.lock \
  env SCANNER_CANDIDATE_DIR="$SCANNER_CANDIDATE_DIR" \
      TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      SCANNER_ANALYTICS_DIR="$SCANNER_ANALYTICS_DIR" \
      SCANNER_LOG_DIR="$SCANNER_LOG_DIR" \
      SCANNER_UNIVERSE_FILE="$SCANNER_UNIVERSE_FILE" \
  venv/bin/python scripts/run_scanners.py --scanners orb \
    --session "$SESSION" --universe manifest \
    --manifest-path "$MANIFEST_PATH" \
    --supplement-size 50 >> "$LOG" 2>&1
# --universe manifest takes the SCANNER NODE's list -- the whole market
# ranked on today's data -- and falls back to the server's own active
# ranking whenever that manifest is missing, stale, malformed or from
# the wrong trading day. The trading node must degrade to what it would
# have done anyway, never stop, and never trade a symbol nobody
# re-derived today. See discovery/manifest.py.
#
# --supplement-size is S6-only on purpose. The active universe is ranked
# by the PREVIOUS day's dollar volume, so on a Monday it is Friday's and
# cannot see a name that woke up this morning -- MARA sat at Friday's
# rank 306 while trading 21.8M shares on the Monday. 50 names from the
# window just below the cut, computed once per session and cached, is
# the bounded version of closing that gap. S1 and S2 keep the default of
# 0 until there is evidence for them too.
