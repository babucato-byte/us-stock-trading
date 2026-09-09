#!/bin/bash
# The KIS trade-stream collector, kept alive.
#
# Not a per-minute cron. A WebSocket subscription, an application-level
# keep-alive and a session accumulator are all continuous things, and
# restarting them every sixty seconds would spend the session
# reconnecting and produce a gap per minute -- each one correctly marking
# the volume incomplete, and between them leaving nothing usable.
#
# So this is a supervised long-running process: cron starts it if it is
# not already up, and the singleton lock inside the runner is what makes
# that safe. Two collectors on one snapshot file would each write their
# own view of the session and the last writer would win, producing a
# volume belonging to no measurement anyone made.
#
# It holds a market-data socket and writes a file. It never takes the KIS
# rate-limit lock, never opens the order database and never calls a
# broker endpoint -- the starvation on 2026-08-27 came from a
# market-data-shaped workload competing for a trading resource, and this
# is the workload that shape describes.
set -u
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a

. "$SCRIPT_DIR/shared_env.sh"

# Same release check as every other cron. A collector feeding features
# to a live entry path is not a place for unverified code.
resolve_release_root || exit 1
resolve_scanner_data_dirs || exit 1

cd "$SCANNER_RUNTIME_ROOT" || exit 1

LOG="${SCANNER_DATA_ROOT}/logs/cron/s6_realtime_collector.log"
mkdir -p "$(dirname "$LOG")" /home/ubuntu/logs/cron

# Already up? Then there is nothing to do. The runner's own lock is the
# real guarantee; this just avoids the log noise of a start that will
# immediately refuse.
# Self-healing, bounded. A collector that is running but wedged --
# heartbeat stale, socket DISCONNECTED/FAILED past its grace, or
# subscriptions incomplete -- is terminated so the start below replaces
# it. market_data/collector_health.py decides, and allows at most one
# forced restart per 15 minutes (marker beside the status file), so a
# venue that is simply quiet (CONNECTED_NO_TRADES) is never restarted
# and a feed that is genuinely down is not restarted every five minutes.
STATUS_FILE="${SCANNER_DATA_ROOT}/realtime_bars/collector_status.json"
MARKER_FILE="${SCANNER_DATA_ROOT}/realtime_bars/collector_restart.marker"
if pgrep -f "run_realtime_bar_collector.py" > /dev/null 2>&1; then
    HEALTH=$("$SCANNER_RUNTIME_ROOT/venv/bin/python" -m market_data.collector_health \
        --status "$STATUS_FILE" --marker "$MARKER_FILE" --process-running yes 2>>"$LOG")
    HEALTH_RC=$?
    if [ "$HEALTH_RC" = "2" ]; then
        echo "$(date -u +%FT%TZ) COLLECTOR_RESTART reason=$HEALTH sha=$SCANNER_SHA" >> "$LOG"
        # Infrastructure event -> stock-system-health. Best-effort; a
        # Slack failure must not stop the restart.
        "$SCANNER_RUNTIME_ROOT/venv/bin/python" -m scripts.notify_system_health \
            COLLECTOR_RESTART "$HEALTH" >> "$LOG" 2>&1 || true
        pkill -TERM -f "run_realtime_bar_collector.py" 2>/dev/null
        sleep 5
        pkill -KILL -f "run_realtime_bar_collector.py" 2>/dev/null
    else
        if [ "$HEALTH_RC" != "0" ]; then
            echo "$(date -u +%FT%TZ) COLLECTOR_UNHEALTHY_NO_RESTART reason=$HEALTH" >> "$LOG"
            "$SCANNER_RUNTIME_ROOT/venv/bin/python" -m scripts.notify_system_health \
                COLLECTOR_UNHEALTHY_NO_RESTART "$HEALTH" >> "$LOG" 2>&1 || true
        fi
        exit 0
    fi
fi

echo "$(date -u +%FT%TZ) starting collector sha=$SCANNER_SHA" >> "$LOG"

# The watchlist must not depend on this session's own candidates.
#
# It did, and for premarket that is circular: discovering a premarket
# candidate needs premarket data, and premarket data is what this
# collector supplies. The result was a collector declining to start every
# five minutes while the scanner rejected 593 of 593 symbols for
# DATA_ERROR -- a session that looked like it had nothing to trade when
# nothing had been measured.
#
# So the pool is seeded from what exists BEFORE the session opens: the
# prior session's ranked candidates plus statically eligible universe
# names. At most 41, which is not a tuning choice but the measured
# ceiling on how many symbols one appkey can stream.
SYMBOLS=$("$SCANNER_RUNTIME_ROOT/venv/bin/python" - <<'PYBOOT' 2>>"$LOG"
import os, sys
sys.path.insert(0, os.environ.get("TRADING_PROJECT_ROOT", ""))
try:
    from datetime import datetime, timedelta, timezone

    from market_data import bootstrap_watchlist as bootstrap
    from market_hours import us_trading_day
    from scanners.base import scan_session

    now = datetime.now(timezone.utc)
    session = scan_session.session_at()
    day = us_trading_day(now)

    # Premarket seeds from the PREVIOUS day's after-hours; the other
    # sessions seed from the one before them on the same day.
    prior_session, prior_day = {
        "PREMARKET": ("AFTER_HOURS", us_trading_day(now - timedelta(days=1))),
        "REGULAR": ("PREMARKET", day),
        "AFTER_HOURS": ("REGULAR", day),
        "OVERNIGHT_DAYTIME": ("AFTER_HOURS", day),
    }.get(session, (None, None))

    pairs, why = bootstrap.build(session=session, trading_day=day,
                                 prior_session=prior_session,
                                 prior_trading_day=prior_day)
    sys.stderr.write("bootstrap %s\n" % why)
    print(",".join("%s:%s" % (sym, exch) for sym, exch in pairs))
except Exception as exc:
    sys.stderr.write("bootstrap failed: %r\n" % (exc,))
    print("")
PYBOOT
)

if [ -z "${SYMBOLS:-}" ]; then
    echo "$(date -u +%FT%TZ) bootstrap produced no symbols; not starting" >> "$LOG"
    exit 0
fi

setsid nohup env TRADING_PROJECT_ROOT="$SCANNER_RUNTIME_ROOT" \
      KIS_LOCK_OWNER=S6_COLLECTOR \
  "$SCANNER_RUNTIME_ROOT/venv/bin/python" \
    "$SCANNER_RUNTIME_ROOT/scripts/run_realtime_bar_collector.py" \
      --symbols "$SYMBOLS" --seconds 3600 \
  < /dev/null >> "$LOG" 2>&1 &

echo "$(date -u +%FT%TZ) collector started symbols=$(echo "$SYMBOLS" | tr ',' '\n' | wc -l)" >> "$LOG"
