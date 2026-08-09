#!/usr/bin/env bash
# The last gate before a first limited-live order.
#
# Answers exactly one question -- "is this deployment ready to place a
# real order right now?" -- and answers it by CHECKING, never by fixing.
# It places no order, changes no flag, writes no state, and touches no
# file. A run that finds twelve problems reports twelve reason codes and
# changes nothing.
#
#   PRE_LIVE_READY    every check passed
#   PRE_LIVE_BLOCKED  at least one did; every failing reason code listed
#
# Exit code mirrors the verdict (0 / 1) so a caller can branch on it.
#
# In the current posture this MUST report PRE_LIVE_BLOCKED: the live
# allow-list is empty and six ARMED wire values are still unconfirmed.
# A READY here today would mean a check is missing.
#
# No secret is printed. The account number appears masked to its last
# four digits and nothing else -- no app key, no token, no raw response.

set -uo pipefail

RELEASE_ROOT="${TRADING_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-${RELEASE_ROOT}/venv/bin/python}"

REASONS=()
PASSES=0

pass() { PASSES=$((PASSES + 1)); printf '  [PASS] %s\n' "$1"; }
fail() { REASONS+=("$1"); printf '  [FAIL] %s %s\n' "$1" "${2:-}"; }
info() { printf '  [INFO] %s\n' "$1"; }

printf 'FINAL PRE-LIVE CHECK\n  release: %s\n\n' "${RELEASE_ROOT}"

# -- 1. release identity ------------------------------------------------
cd "${RELEASE_ROOT}" || { echo "RELEASE_ROOT unreadable"; exit 1; }
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
if [ "${HEAD_SHA}" = "unknown" ]; then
    fail COMMIT_UNREADABLE
elif [ "${HEAD_SHA}" = "${DEPLOYED_COMMIT:-}" ] && [ "${HEAD_SHA}" = "${VALIDATED_COMMIT:-}" ]; then
    pass "commit HEAD==DEPLOYED==VALIDATED (${HEAD_SHA:0:8})"
else
    fail COMMIT_MISMATCH "HEAD=${HEAD_SHA:0:8} DEPLOYED=${DEPLOYED_COMMIT:0:8} VALIDATED=${VALIDATED_COMMIT:0:8}"
fi

if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
    pass "working tree clean"
else
    fail WORKING_TREE_DIRTY
fi

# -- 2. environment posture --------------------------------------------
if [ "${KIS_ENV:-}" = "live" ]; then
    pass "KIS_ENV=live"
else
    fail KIS_ENV_NOT_LIVE "KIS_ENV=${KIS_ENV:-<unset>}"
fi

check_limit() {
    local name="$1" expected="$2" actual="${!1:-}"
    if [ "${actual}" = "${expected}" ]; then
        pass "${name}=${expected}"
    else
        fail "${name}_NOT_${expected}" "got '${actual:-<unset>}'"
    fi
}
check_limit LIVE_ROLLOUT_MAX_POSITIONS 1
check_limit LIVE_ROLLOUT_MAX_DAILY_ENTRIES 1
check_limit LIVE_ROLLOUT_MAX_QUANTITY 1

# The one symbol a first limited-live order may touch. Empty is the
# correct read-only posture AND a hard block on going live.
ALLOWLIST="${LIVE_ROLLOUT_ALLOWED_SYMBOLS:-}"
ALLOW_COUNT=0
if [ -n "${ALLOWLIST}" ]; then
    ALLOW_COUNT="$(printf '%s' "${ALLOWLIST}" | tr ',' '\n' | grep -c '[^[:space:]]')"
fi
if [ "${ALLOW_COUNT}" -eq 1 ]; then
    pass "live allow-list holds exactly 1 symbol"
else
    fail LIVE_ALLOWLIST_NOT_EXACTLY_ONE "count=${ALLOW_COUNT}"
fi

if [ -n "${SLACK_WEBHOOK_URL:-}" ] && [ -n "${SLACK_ALERT_WEBHOOK_URL:-}" ]; then
    pass "Slack webhooks configured (general + alert)"
else
    fail SLACK_WEBHOOK_UNCONFIGURED
fi

# -- 3. everything that needs the code and the account ------------------
# One Python process for the checks that need imports or a KIS read, so
# the token is issued at most once.
PY_OUTPUT="$("${PYTHON_BIN}" - <<'PY' 2>/dev/null
import os
import sys

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


try:
    from brokers.kis_broker import (
        REQUIRED_FOR_ARMED, REQUIRED_FOR_OBSERVE, KISBroker,
        matrix_entries_for, pending_items_for,
    )
    from config.live_rollout_config import LiveRolloutConfig
    from execution import entry_limits, idempotency
    from execution.secret_redaction import mask_account_number
    from live_pilot.posture import resolve_posture
    from market_hours import get_us_market_session
    from operations import kill_switch as ops_kill_switch
    from reconciliation import freshness
    from state_store import db as state_db
    import kill_switch_state
except Exception as exc:  # noqa: BLE001
    print(f"IMPORT_FAILED::{type(exc).__name__}")
    sys.exit(3)

# -- wire verification matrix
observe_pending = list(pending_items_for(REQUIRED_FOR_OBSERVE))
armed_pending = list(pending_items_for(REQUIRED_FOR_ARMED))
check("OBSERVE_MATRIX_PENDING", not observe_pending,
      f"{len(matrix_entries_for(REQUIRED_FOR_OBSERVE)) - len(observe_pending)}"
      f"/{len(matrix_entries_for(REQUIRED_FOR_OBSERVE))} confirmed")
check("ARMED_MATRIX_PENDING", not armed_pending,
      f"pending: {', '.join(armed_pending) if armed_pending else 'none'}")

# -- reconciliation
try:
    snapshot = freshness.evaluate()
    check("RECONCILIATION_NOT_USABLE", snapshot.usable,
          getattr(snapshot, "reason_code", "") or "fresh and clean")
except Exception as exc:  # noqa: BLE001
    check("RECONCILIATION_NOT_USABLE", False, type(exc).__name__)

# -- HALT / kill switch
try:
    halted = ops_kill_switch.is_halted()
    check("HALT_ACTIVE", not halted, f"halted={halted}")
except Exception as exc:  # noqa: BLE001
    check("HALT_ACTIVE", False, type(exc).__name__)

try:
    state = kill_switch_state.get_state()
    check("KILL_SWITCH_ACTIVE", state in (None, kill_switch_state.ACTIVE), f"state={state}")
except Exception as exc:  # noqa: BLE001
    check("KILL_SWITCH_ACTIVE", False, type(exc).__name__)

# -- session
try:
    session = get_us_market_session()
    check("NOT_REGULAR_SESSION", str(session).lower() == "regular", f"session={session}")
except Exception as exc:  # noqa: BLE001
    check("NOT_REGULAR_SESSION", False, type(exc).__name__)

# -- posture
decision = resolve_posture()
check("POSTURE_NOT_ARMED", decision.posture == "ARMED", f"posture={decision.posture}")

# -- single-run lock
try:
    with idempotency.single_run_lock():
        pass
    check("SINGLE_RUN_LOCK_HELD", True, "available")
except Exception as exc:  # noqa: BLE001
    check("SINGLE_RUN_LOCK_HELD", False, type(exc).__name__)

# -- account, cash, positions, open orders, daily entries
rollout = LiveRolloutConfig.from_env()
try:
    broker = KISBroker()
    snapshot_account = broker.get_account_snapshot()
    allowed = (os.environ.get("KIS_ALLOWED_ACCOUNT_NO") or "").strip()
    check("ACCOUNT_MISMATCH", str(snapshot_account.account_id) == allowed,
          f"account={mask_account_number(snapshot_account.account_id)}")

    positions = [p for p in broker.get_positions() if getattr(p, "quantity", 0) > 0]
    check("POSITIONS_NOT_ZERO", len(positions) == 0, f"open positions={len(positions)}")

    open_orders = broker.get_open_orders()
    check("OPEN_ORDERS_NOT_ZERO", len(open_orders) == 0, f"open orders={len(open_orders)}")

    symbols = [s.strip().upper() for s in
               (os.environ.get("LIVE_ROLLOUT_ALLOWED_SYMBOLS") or "").split(",") if s.strip()]
    if len(symbols) == 1:
        from market_data.exchange_registry import build_kis_instrument

        instrument, _record = build_kis_instrument(symbols[0])
        price = broker.get_current_price(instrument)
        orderable = broker.get_orderable_usd(instrument, price)
        check("INSUFFICIENT_ORDERABLE_CASH", orderable >= price,
              f"orderable={orderable} price={price}")
    else:
        check("INSUFFICIENT_ORDERABLE_CASH", False,
              "cannot price the candidate: the allow-list does not hold exactly one symbol")

    conn = state_db.open_db()
    try:
        limits = entry_limits.collect(broker=broker, conn=conn, rollout=rollout)
    finally:
        conn.close()
    check("DAILY_ENTRIES_ALREADY_USED", limits.daily_entry_count == 0,
          f"daily entries={limits.daily_entry_count}/{limits.max_daily_entries}")
    check("POSITION_SLOTS_ALREADY_USED", limits.effective_position_count == 0,
          f"effective positions={limits.effective_position_count}/{limits.max_open_positions}")
except Exception as exc:  # noqa: BLE001
    check("KIS_ACCOUNT_READ_FAILED", False, type(exc).__name__)

for name, ok, detail in results:
    print(f"{'OK' if ok else 'BAD'}::{name}::{detail}")
PY
)"

# One pass over the emitted lines. Anything that is neither OK:: nor
# BAD:: is treated as a failure rather than ignored -- an unparseable
# line means a check whose verdict is unknown, and unknown is not ready.
if printf '%s' "${PY_OUTPUT}" | grep -q '^IMPORT_FAILED::'; then
    fail CODE_IMPORT_FAILED "$(printf '%s' "${PY_OUTPUT}" | head -1)"
else
    while IFS= read -r line; do
        [ -z "${line}" ] && continue
        case "${line}" in
            OK::*)
                rest="${line#OK::}"
                pass "${rest%%::*} ${rest#*::}"
                ;;
            BAD::*)
                rest="${line#BAD::}"
                fail "${rest%%::*}" "${rest#*::}"
                ;;
            *)
                fail CHECK_OUTPUT_UNPARSEABLE "${line:0:80}"
                ;;
        esac
    done < <(printf '%s\n' "${PY_OUTPUT}")
fi

printf '\n'
if [ "${#REASONS[@]}" -eq 0 ]; then
    printf 'RESULT: PRE_LIVE_READY\n'
    exit 0
fi
printf 'RESULT: PRE_LIVE_BLOCKED\n'
printf 'BLOCKING REASON CODES (%s):\n' "${#REASONS[@]}"
printf '  - %s\n' "${REASONS[@]}"
exit 1
