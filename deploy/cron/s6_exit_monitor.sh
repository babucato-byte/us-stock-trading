#!/bin/bash
# S6 exit monitor. Every minute, but only while something is held.
#
# The 15-minute runtime tick is the right cadence for finding entries
# and is far too slow for leaving one: an ORB breakout that fails can
# give back the day's range inside a single tick, and every exit reason
# S6 has -- VWAP failure, EMA structure failure, range re-entry, volume
# decay -- is a condition that can become true and be gone again before
# the next quarter hour.
#
# It evaluates exit conditions once a minute. It does not sell once a
# minute: the exit policy decides, unchanged, and most ticks conclude
# "hold".
#
# The position check is a local SQLite read and costs no broker call.
# That guard is the point -- S6 is flat almost all of the time, and a
# minute-by-minute KIS poll against an empty position store would spend
# the account's rate limit on the answer "nothing to do", which is
# exactly the budget the orderable-amount read needs when an entry does
# appear.
set -u
ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a
ROOT="${TRADING_PROJECT_ROOT:?}"
cd "$ROOT" || exit 1

HELD=$("$ROOT/venv/bin/python" - <<'PY' 2>/dev/null
import os, sqlite3, sys
path = os.environ.get("STATE_STORE_DB_FILE")
if not path:
    sys.exit(0)
try:
    conn = sqlite3.connect(path)
    # The store's own list, imported rather than copied. This guard was
    # hand-written as ('OPEN','SUBMITTED','EXIT_PENDING') and drifted from
    # LIVE_STATUSES when EXIT_SUBMITTED was added: the monitor then went
    # quiet at the exact moment a SELL was live at the broker and the
    # fill still had to be collected, reporting "flat" because the only
    # position it had was mid-exit.
    sys.path.insert(0, os.environ.get("TRADING_PROJECT_ROOT", ""))
    from s6_live.position_store import LIVE_STATUSES
    row = conn.execute(
        "SELECT COUNT(*) FROM s6_positions WHERE status IN (%s)"
        % ",".join("?" * len(LIVE_STATUSES)), LIVE_STATUSES
    ).fetchone()
    print(int(row[0]) if row else 0)
except Exception:
    # Unreadable store: say nothing rather than 0. The 15-minute runtime
    # tick still runs and is the safety net; claiming "flat" from a
    # failed read would be the one answer that stops us looking.
    print("UNKNOWN")
PY
)

case "${HELD:-}" in
  0)        exit 0 ;;                     # flat: no broker call at all
  UNKNOWN)  exit 0 ;;                     # the 15-minute tick covers it
  '')       exit 0 ;;
esac

LOG=/home/ubuntu/releases/us-stock-trading/shared/state/s6_exit_monitor_$(date -u +%F).log
# Two locks, and why this one is no longer the broker's.
# ------------------------------------------------------
# `s6_exit.lock` (here) stops two EXIT EVALUATIONS overlapping. That is
# all it does. It is not a broker lock, and a BUY submission never waits
# on it.
#
# `s6_exec.lock` still serialises BROKER MUTATION, and this wrapper no
# longer takes it: the runtime acquires it in Python around the SELL
# submission alone, and revalidates the position row under it
# (s6_live/exit_runtime._submit_sell_locked).
#
# It used to take `s6_exec.lock` here, for the life of the process. The
# evaluation is network-bound -- yfinance bars and KIS reads -- and
# measured at a 215.9s median, 811.6s max, so each run blocked the next
# three or four one-minute ticks. On 2026-09-15 that was 253 skips in
# 318 ticks (79.6%), a median 298s between completed evaluations and
# 2280s at worst: a one-minute exit monitor deciding roughly every five.
#
# This is the same change entry received on 2026-09-02, for the same
# reason, and its comment applies here verbatim -- what is no longer
# serialised is the ANALYSIS, which never mutated anything.
#
# Every scheduled tick leaves a line, whether or not it ran.
#
# It used to leave one only when it ran. A tick that could not take the
# lock exited silently, so a monitor blocked for eleven consecutive
# minutes and a monitor with nothing to do produced identical logs --
# both empty. On 2026-09-02 that hid the starvation completely: the
# evidence that the one-minute monitor was acquiring the lock 1 tick in
# 29 had to be reconstructed afterwards from cron firings in syslog,
# because the monitor's own log said nothing at all.
echo "$(date -u +%FT%TZ) MONITOR_TICK held=$HELD" >> "$LOG"
flock -n -E 99 /home/ubuntu/logs/cron/s6_exit.lock \
  env PYTHONPATH="$ROOT" TRADING_PROJECT_ROOT="$ROOT" \
      KIS_LOCK_OWNER=S6_EXIT \
      S6_EXECUTION_LOCK_FILE=/home/ubuntu/logs/cron/s6_exec.lock \
  "$ROOT/venv/bin/python" "$ROOT/scripts/run_s6_runtime.py" >> "$LOG" 2>&1
STATUS=$?
if [ "$STATUS" -eq 99 ]; then
    # A previous evaluation is still running. Not a failure, and NOT
    # execution-lock contention -- named differently so the two can never
    # be confused in the log again.
    echo "$(date -u +%FT%TZ) MONITOR_OVERLAP_SKIPPED held=$HELD lock=/home/ubuntu/logs/cron/s6_exit.lock" >> "$LOG"
    exit 0
fi
echo "$(date -u +%FT%TZ) MONITOR_EVALUATED held=$HELD status=$STATUS" >> "$LOG"
exit "$STATUS"
