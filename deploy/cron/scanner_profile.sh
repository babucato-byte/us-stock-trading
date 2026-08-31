#!/bin/bash
# A scanner profile, run from the immutable release.
#
# Why this exists
# ---------------
# The premarket / open / daily profiles ran like this:
#
#   cd /home/ubuntu/trading && env TRADING_PROJECT_ROOT=/home/ubuntu/trading \
#     venv/bin/python scripts/run_scanners.py --profile daily
#
# That tree was at ecb906b14 with four uncommitted modifications while
# the deployed release was ad7019c7d. So the activity ranking every S6
# session depends on was produced by code nobody validated, from a
# mutable checkout, at a commit no release invariant covers.
#
# It also wrote its output where the release could not read it. The
# ranking lives at <root>/logs/scanners/activity unless
# SCANNER_ANALYTICS_DIR says otherwise; the legacy tree set no override
# and the release sets one, so on 2026-08-31 the S6 scanner reported "no
# active universe available" while 2MB of ranking covering 10,564
# symbols sat two paths away. The message was true of the directory and
# false of the system.
#
# Running from the release fixes both at once: one validated commit
# produces the ranking, and it lands in shared state where every reader
# already looks.
#
# Outputs stay in shared state, never inside the release, so running
# code cannot dirty the release it runs from.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

PROFILE="${1:?usage: scanner_profile.sh <premarket|open|daily>}"
case "$PROFILE" in
    premarket|open|daily) ;;
    *) echo "unknown profile: $PROFILE" >&2; exit 2 ;;
esac

ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a

. "$SCRIPT_DIR/shared_env.sh"

resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1

cd "$SCANNER_RUNTIME_ROOT" || exit 1

LOG="${SCANNER_DATA_ROOT}/logs/cron/scanner_${PROFILE}.log"
mkdir -p "$(dirname "$LOG")" /home/ubuntu/logs/cron

echo "$(date -u +%FT%TZ) profile=$PROFILE sha=$SCANNER_SHA root=$SCANNER_RUNTIME_ROOT analytics=$SCANNER_ANALYTICS_DIR" >> "$LOG"

# Its own lock per profile: the daily walk is long and must not be
# started twice, but it must also not block the premarket refresh.
#
# -E 99 so a skipped overlap is distinguishable in the log from a pass
# that ran and failed.
flock -n -E 99 "/home/ubuntu/logs/cron/scanner_${PROFILE}.lock" \
  env TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      SCANNER_ANALYTICS_DIR="$SCANNER_ANALYTICS_DIR" \
  "$SCANNER_RUNTIME_ROOT/venv/bin/python" \
    "$SCANNER_RUNTIME_ROOT/scripts/run_scanners.py" --profile "$PROFILE" \
    >> "$LOG" 2>&1
STATUS=$?

if [ "$STATUS" -eq 99 ]; then
    echo "$(date -u +%FT%TZ) OVERLAP_SKIPPED profile=$PROFILE is still running" >> "$LOG"
    exit 0
fi
echo "$(date -u +%FT%TZ) PROFILE_COMPLETE profile=$PROFILE status=$STATUS sha=$SCANNER_SHA" >> "$LOG"
exit "$STATUS"
