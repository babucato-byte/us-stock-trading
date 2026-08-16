#!/usr/bin/env bash
#
# T9: start the real-time pilot -- scanner, entry conditions and exit
# conditions driven against LIVE market data for a whole session.
#
# The KIS environment is the one switch this script owns:
#
#   KIS_ENV=paper  (default)  the 모의투자 account
#   KIS_ENV=live              the real account; additionally requires
#                             LIVE_PILOT_ACK_LIVE_ENV=true, and preflight
#                             refuses while any KIS wire-format value is
#                             still LIVE_RESPONSE_PENDING
#
# What the pilot may DO is NOT this script's switch. It reads, and never
# writes, the same three flags the Order Gate and the live service unit
# read:
#
#   KIS_LIVE_ORDER_ENABLED / LIVE_ROLLOUT_ENABLED / ENTRY_DISABLED
#
# With any of them in the safe position the session runs in OBSERVE:
# every candidate is scored, priced, gated and every open position's exit
# condition is evaluated, and nothing can be submitted. Arming is one
# line in the operator's own .env -- see docs/autonomous/NEEDS_USER.md §7.
# This script sets no flag, exports no flag and has no --arm option; the
# regression suite asserts that.
#
# Usage:
#   scripts/start_live_pilot.sh                        # paper, regular session
#   scripts/start_live_pilot.sh --once                 # one tick, then report
#   KIS_ENV=live LIVE_PILOT_ACK_LIVE_ENV=true \
#     scripts/start_live_pilot.sh --sessions regular
#   scripts/start_live_pilot.sh --preflight-only       # just the checklist
#
# Every argument is passed straight through to scripts/run_live_pilot.py
# (--interval, --max-ticks, --until, --sessions, --scan-interval,
#  --scan-limit, --preset, --log-dir, --report-only, --date, --log-level).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/venv/bin/python}"
PILOT_SCRIPT="${REPO_ROOT}/scripts/run_live_pilot.py"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

[ -x "${PYTHON_BIN}" ] || PYTHON_BIN="$(command -v python3 || true)"
[ -n "${PYTHON_BIN}" ] && [ -x "${PYTHON_BIN}" ] \
    || fail "no usable python (set PYTHON_BIN=/path/to/python)."
[ -f "${PILOT_SCRIPT}" ] || fail "${PILOT_SCRIPT} does not exist."

# The KIS environment. Defaulting to paper is deliberate: a mistyped or
# forgotten value must land on the 모의투자 account, never the real one.
KIS_ENV="${KIS_ENV:-paper}"
case "${KIS_ENV}" in
    paper|live) ;;
    *) fail "KIS_ENV must be 'paper' or 'live', got '${KIS_ENV}'." ;;
esac
export KIS_ENV

if [ "${KIS_ENV}" = "live" ] && [ "${LIVE_PILOT_ACK_LIVE_ENV:-}" != "true" ]; then
    fail "KIS_ENV=live requires LIVE_PILOT_ACK_LIVE_ENV=true (the pilot will read the REAL account)."
fi

echo "== live pilot =="
echo "repo         : ${REPO_ROOT}"
echo "python       : ${PYTHON_BIN}"
echo "KIS_ENV      : ${KIS_ENV}"
# Reported so the operator sees the posture BEFORE the session starts,
# and so the terminal scrollback records which one it was. Read-only:
# the values printed are whatever the environment already held.
echo "order flags  : KIS_LIVE_ORDER_ENABLED=${KIS_LIVE_ORDER_ENABLED:-<unset>}" \
     "LIVE_ROLLOUT_ENABLED=${LIVE_ROLLOUT_ENABLED:-<unset>}" \
     "ENTRY_DISABLED=${ENTRY_DISABLED:-<unset>}"
echo

cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" "${PILOT_SCRIPT}" "$@"
