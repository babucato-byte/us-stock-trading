#!/bin/bash
# SCANNER NODE. Runs on the laptop, holds no broker credentials.
#
# Hourly, not per-tick: the full-market first stage costs about twelve
# minutes of provider time (measured 800 symbols in 46.8s), which is
# affordable once an hour and is not affordable every fifteen minutes.
# The trading node's own precision scan stays at fifteen minutes -- the
# two cadences answer different questions.
#
# The manifest is written locally first and only then copied, so a
# failed transfer leaves the previous manifest on the trading node
# rather than a truncated one. scp to a temporary name plus a remote
# mv keeps the same atomicity across the network that manifest.write
# gives locally.
set -uo pipefail

ROOT="${TRADING_SCANNER_ROOT:-$HOME/Projects/us-stock-trading}"
REMOTE="${TRADING_REMOTE:-trading}"
REMOTE_DIR="${TRADING_REMOTE_DIR:-/home/ubuntu/releases/us-stock-trading/shared/state/discovery}"
LOG="$ROOT/logs/discovery/market_discovery.log"
LOCAL="$ROOT/logs/discovery/manifest.json"

cd "$ROOT" || exit 1
mkdir -p "$(dirname "$LOG")"

# A scan is only meaningful once the opening range exists. Before that
# there is nothing for the trading node's ORB15 to evaluate, so an
# earlier manifest would cost twelve minutes of fetches to produce rows
# that cannot yet qualify.
SESSION=$(venv/bin/python -c "
from scanners.base import scan_window
print(scan_window.probe())
" 2>/dev/null)
case "${SESSION:-}" in
  PREMARKET|REGULAR|AFTER_HOURS|OVERNIGHT_DAYTIME) ;;
  *)
    echo "$(date -u +%FT%TZ) skipped=${SESSION:-unknown}" >> "$LOG"
    exit 0;;
esac

echo "$(date -u +%FT%TZ) session=$SESSION starting market-wide discovery" >> "$LOG"
if ! venv/bin/python scripts/run_market_discovery.py --out "$LOCAL" >> "$LOG" 2>&1; then
    echo "$(date -u +%FT%TZ) scan FAILED; previous manifest left in place" >> "$LOG"
    exit 1
fi

# Transfer. A failure here is not a scan failure: the trading node keeps
# the manifest it already has and falls back to its own ranking once
# that one goes stale.
if scp -q "$LOCAL" "$REMOTE:$REMOTE_DIR/.manifest.incoming" \
   && ssh "$REMOTE" "mv -f '$REMOTE_DIR/.manifest.incoming' '$REMOTE_DIR/manifest.json'"; then
    echo "$(date -u +%FT%TZ) manifest delivered" >> "$LOG"
else
    echo "$(date -u +%FT%TZ) TRANSFER FAILED; trading node keeps its previous manifest" >> "$LOG"
    exit 1
fi
