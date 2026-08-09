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
LIVE_BOOTSTRAP_REQUIRED = {
    "order_path", "order_tr_id_live_buy", "cancel_path",
    "cancel_tr_id_live", "cancel_price_field_rule",
}
# The five that ONLY a real live response can establish. While exactly
# those are outstanding the deployment is bootstrap-eligible but not
# generally live-ready; anything else outstanding is a plain block.
beyond_bootstrap = [n for n in armed_pending if n not in LIVE_BOOTSTRAP_REQUIRED]
check("ARMED_MATRIX_PENDING", not armed_pending,
      f"pending: {', '.join(armed_pending) if armed_pending else 'none'}")
check("ARMED_PENDING_BEYOND_BOOTSTRAP", not beyond_bootstrap,
      f"beyond the live-bootstrap five: "
      f"{', '.join(beyond_bootstrap) if beyond_bootstrap else 'none'}")
print(f"BOOTSTRAPABLE::{'yes' if armed_pending and not beyond_bootstrap else 'no'}")

# -- reconciliation
# A snapshot made minutes ago by an earlier step can age past its TTL
# while this checker is still running its KIS reads. Refresh once when
# that is the reason, then judge the refreshed one. A refresh that
# cannot be made, or a snapshot that is dirty / has unknowns / is
# halted, is still a hard failure -- staleness is the only thing this
# retries, and nothing here hides a mismatch.
# freshness.evaluate() signals success by RETURNING and failure by
# RAISING SnapshotUnusable -- there is no `.usable` flag to read. An
# earlier version of this check read one, so a perfectly healthy
# snapshot was reported as a blocker.
def _reconciliation_detail(snapshot):
    age = getattr(snapshot, "age_seconds", None)
    return f"fresh and clean (age {age:.1f}s)" if isinstance(age, (int, float)) \
        else "fresh and clean"


try:
    snapshot = freshness.evaluate()
    check("RECONCILIATION_NOT_USABLE", True, _reconciliation_detail(snapshot))
except Exception as exc:  # noqa: BLE001
    reason = str(getattr(exc, "reason_code", "") or exc)
    # Only staleness is retried, and only once. A dirty snapshot, one
    # carrying unknowns, a halted one, or a refresh that cannot be made
    # all stay hard failures -- nothing here hides a mismatch.
    stale = "STALE" in reason.upper() or "MISSING" in reason.upper()
    allow_refresh = os.environ.get("PRE_LIVE_ALLOW_RECONCILE_REFRESH", "").strip().lower() \
        in ("1", "true", "yes", "on")
    if stale and allow_refresh:
        # OPT-IN only. The contract of this checker is that it checks and never
        # fixes; running reconciliation writes a snapshot, so it happens
        # only when the operator asks. Default off keeps a plain run
        # side-effect free -- which is also why a test suite invoking this
        # script cannot leave state behind.
        import subprocess

        subprocess.run([sys.executable, "scripts/run_reconciliation.py"],
                       capture_output=True, timeout=300, check=False)
        try:
            snapshot = freshness.evaluate()
            check("RECONCILIATION_NOT_USABLE", True,
                  f"refreshed; {_reconciliation_detail(snapshot)}")
        except Exception as retry_exc:  # noqa: BLE001
            check("RECONCILIATION_NOT_USABLE", False,
                  f"{type(retry_exc).__name__} after refresh")
    elif stale:
        check("RECONCILIATION_NOT_USABLE", False,
              "snapshot is stale; run scripts/run_reconciliation.py first "
              "(or set PRE_LIVE_ALLOW_RECONCILE_REFRESH=true)")
    else:
        # Dirty, unknown-bearing or halted: never retried, never hidden.
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
        # Not the same as "the account is short". Nothing was priced,
        # so nothing is known about the cash -- reporting a shortfall
        # here would be an invented finding.
        check("ORDERABLE_CASH_NOT_EVALUATED", False,
              "no candidate to price: the allow-list does not hold exactly one symbol")

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
            BOOTSTRAPABLE::*)
                ;;  # control line, consumed below
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

# READY_FOR_LIVE_BOOTSTRAP is NOT a weaker PRE_LIVE_READY. It authorises
# exactly one symbol, one share, one BUY attempt and at most one CANCEL
# attempt -- the only way the five live-only wire values can be observed
# at all. Every OTHER safety check must still have passed, which is why
# it requires the reason list to contain nothing but ARMED_MATRIX_PENDING.
BOOTSTRAPABLE="$(printf '%s\n' "${PY_OUTPUT}" | sed -n 's/^BOOTSTRAPABLE:://p' | tail -1)"
if [ "${BOOTSTRAPABLE}" = "yes" ] && [ "${#REASONS[@]}" -eq 1 ] \
   && [ "${REASONS[0]}" = "ARMED_MATRIX_PENDING" ]; then
    printf 'RESULT: READY_FOR_LIVE_BOOTSTRAP\n'
    printf 'Every safety check passed. The only outstanding items are the five\n'
    printf 'wire values that a real live response is the only way to confirm:\n'
    printf '  order_path, order_tr_id_live_buy, cancel_path,\n'
    printf '  cancel_tr_id_live, cancel_price_field_rule\n'
    printf 'Authorised scope: 1 symbol, 1 share, 1 BUY, at most 1 CANCEL.\n'
    printf 'This is NOT approval for ARMED or AUTO LIVE.\n'
    exit 0
fi

printf 'RESULT: PRE_LIVE_BLOCKED\n'
printf 'BLOCKING REASON CODES (%s):\n' "${#REASONS[@]}"
printf '  - %s\n' "${REASONS[@]}"
exit 1
