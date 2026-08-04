#!/usr/bin/env bash
#
# Arms `us-stock-trading-shadow.timer`. Nothing else.
#
# Installation and activation used to be one program, so copying unit
# files into place also started four timers -- including this one, whose
# activation is a reviewed decision. `install_oracle_services.sh` now
# installs and leaves everything stopped; this script is the only way the
# Shadow timer starts, and it refuses unless the deployment is provably
# in the read-only posture.
#
# It CANNOT arm live order placement:
#   - `us-stock-trading-live.service` has no [Install] section, so it is
#     not enableable at all;
#   - this script names exactly one unit, and refuses to run if the
#     environment file has any order flag on.
#
# Approval is explicit. ALLOW_SHADOW_TIMER_ENABLE must be exactly "true".
#
# Usage (on the Oracle host, as a user with sudo):
#   sudo ALLOW_SHADOW_TIMER_ENABLE=true \
#        TRADING_RELEASE_ROOT=/home/ubuntu/releases/us-stock-trading/<commit> \
#        TRADING_SHARED_ROOT=/home/ubuntu/releases/us-stock-trading/shared \
#        scripts/enable_oracle_shadow_timer.sh
#
set -euo pipefail

RELEASE_DIR="${TRADING_RELEASE_ROOT:-${RELEASE_DIR:-/home/ubuntu/releases/us-stock-trading/current-readonly}}"
SHARED_DIR="${TRADING_SHARED_ROOT:-${SHARED_DIR:-/home/ubuntu/releases/us-stock-trading/shared}}"
ENV_DIR="${ENV_DIR:-/etc/us-stock-trading}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/live-readonly.env}"
SERVICE_USER="${SERVICE_USER:-ubuntu}"
PYTHON_BIN="${PYTHON_BIN:-${RELEASE_DIR}/venv/bin/python}"
# Which preflight program to run. Named, not skippable: there is no
# value that turns the check off, and the default is the release's own.
PREFLIGHT_SCRIPT="${PREFLIGHT_SCRIPT:-${RELEASE_DIR}/scripts/preflight_kis_live.py}"
DRY_RUN="${DRY_RUN:-0}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"

TARGET_TIMER="us-stock-trading-shadow.timer"
TARGET_SERVICE="us-stock-trading-shadow.service"
LIVE_UNIT="us-stock-trading-live.service"

run() {
    if [ "${DRY_RUN}" = "1" ]; then
        echo "DRY_RUN: $*"
    else
        "$@"
    fi
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

echo "== arm ${TARGET_TIMER} =="

# ---------------------------------------------------------------------
# 1. Explicit operator approval.
# ---------------------------------------------------------------------
if [ "${ALLOW_SHADOW_TIMER_ENABLE:-}" != "true" ]; then
    fail "ALLOW_SHADOW_TIMER_ENABLE must be exactly 'true' to arm ${TARGET_TIMER} (got '${ALLOW_SHADOW_TIMER_ENABLE:-<unset>}')."
fi

# ---------------------------------------------------------------------
# 2. The deployment this is arming.
# ---------------------------------------------------------------------
[ -f "${ENV_FILE}" ] || fail "${ENV_FILE} does not exist."
[ -e "${PYTHON_BIN}" ] || fail "${PYTHON_BIN} does not exist."

env_value() {
    sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1
}

DEPLOYED_COMMIT="$(env_value DEPLOYED_COMMIT)"
VALIDATED_COMMIT="$(env_value VALIDATED_COMMIT)"
[ -n "${DEPLOYED_COMMIT}" ] || fail "${ENV_FILE} does not set DEPLOYED_COMMIT."
[ "${DEPLOYED_COMMIT}" = "${VALIDATED_COMMIT}" ] \
    || fail "DEPLOYED_COMMIT (${DEPLOYED_COMMIT}) != VALIDATED_COMMIT (${VALIDATED_COMMIT})."

if command -v git >/dev/null 2>&1 && [ -d "${RELEASE_DIR}/.git" ]; then
    head_commit="$(git -C "${RELEASE_DIR}" rev-parse HEAD 2>/dev/null || true)"
    [ "${head_commit}" = "${DEPLOYED_COMMIT}" ] \
        || fail "${RELEASE_DIR} is at ${head_commit:-unknown}, but DEPLOYED_COMMIT is ${DEPLOYED_COMMIT}."
fi
echo "commit       : ${DEPLOYED_COMMIT}"

# ---------------------------------------------------------------------
# 3. Safety flags. Shadow places no orders, but arming a recurring job
#    against a posture that CAN is not something to do by accident.
# ---------------------------------------------------------------------
for forbidden in KIS_LIVE_ORDER_ENABLED LIVE_ROLLOUT_ENABLED ALPACA_ORDER_ENABLED; do
    if grep -qiE "^${forbidden}=(1|true|yes|on)$" "${ENV_FILE}"; then
        fail "${ENV_FILE} has ${forbidden} enabled -- refusing to arm anything."
    fi
done
grep -qiE "^ENTRY_DISABLED=(1|true|yes|on)$" "${ENV_FILE}" \
    || fail "${ENV_FILE} does not set ENTRY_DISABLED=true."
echo "safety flags : KIS_LIVE_ORDER_ENABLED=false LIVE_ROLLOUT_ENABLED=false ENTRY_DISABLED=true"

# ---------------------------------------------------------------------
# 4. The live unit must be exactly where the installer left it.
# ---------------------------------------------------------------------
live_enabled="$("${SYSTEMCTL_BIN}" is-enabled "${LIVE_UNIT}" 2>/dev/null || true)"
live_active="$("${SYSTEMCTL_BIN}" is-active "${LIVE_UNIT}" 2>/dev/null || true)"
[ "${live_enabled}" = "static" ] \
    || fail "${LIVE_UNIT} is-enabled='${live_enabled}', expected 'static'."
[ "${live_active}" != "active" ] && [ "${live_active}" != "activating" ] \
    || fail "${LIVE_UNIT} is ${live_active}."
echo "live unit    : static + ${live_active:-inactive}"

# ---------------------------------------------------------------------
# 5. shared/state ownership and mode.
# ---------------------------------------------------------------------
state_mode="$(stat -c '%a' "${SHARED_DIR}/state")"
state_owner="$(stat -c '%U:%G' "${SHARED_DIR}/state")"
if [ "${state_mode}" != "700" ] || [ "${state_owner}" != "${SERVICE_USER}:${SERVICE_USER}" ]; then
    fail "${SHARED_DIR}/state is ${state_mode} ${state_owner}, expected 700 ${SERVICE_USER}:${SERVICE_USER}."
fi
echo "shared state : ${state_mode} ${state_owner}"

# ---------------------------------------------------------------------
# 6. preflight, then the operational preconditions the Shadow pass will
#    itself depend on: a fresh reconciliation snapshot, no UNKNOWN
#    orders, no HALT.
# ---------------------------------------------------------------------
run "${PYTHON_BIN}" "${PREFLIGHT_SCRIPT}"

# The snapshot must be FRESH, not merely present. Codex armed the timer
# with a 30-day-old clean snapshot because this step only checked that
# `checked_at` existed. It is now the same program the Shadow service
# runs before every single evaluation, so the TTL, the clock-skew
# tolerance and the reason codes cannot drift between the two.
#
# Deliberately not an inline heredoc any more: a shell-embedded check is
# invisible to the test suite (a stubbed PYTHON_BIN skips it entirely,
# which is exactly how this defect survived), while a script is run and
# tested like any other program.
if ! "${PYTHON_BIN}" "${RELEASE_DIR}/scripts/check_reconciliation_freshness.py" \
        --purpose shadow-timer-enable --require-unknown-zero --require-halt-clear; then
    fail "reconciliation snapshot is not usable -- refusing to arm ${TARGET_TIMER}."
fi

# ---------------------------------------------------------------------
# 7. Verify the unit, then enable + start ATOMICALLY: any failure rolls
#    back to disabled + inactive. "enabled but failed" is not a state an
#    operator should have to discover later.
# ---------------------------------------------------------------------
"${SYSTEMCTL_BIN}" cat "${TARGET_TIMER}" >/dev/null 2>&1 \
    || fail "${TARGET_TIMER} is not installed -- run install_oracle_services.sh first."
"${SYSTEMCTL_BIN}" cat "${TARGET_SERVICE}" >/dev/null 2>&1 \
    || fail "${TARGET_SERVICE} is not installed."

rollback() {
    echo "rolling back ${TARGET_TIMER} to disabled + inactive" >&2
    "${SYSTEMCTL_BIN}" stop "${TARGET_TIMER}" >/dev/null 2>&1 || true
    "${SYSTEMCTL_BIN}" disable "${TARGET_TIMER}" >/dev/null 2>&1 || true
}

if [ "${DRY_RUN}" = "1" ]; then
    echo "DRY_RUN: ${SYSTEMCTL_BIN} enable ${TARGET_TIMER}"
    echo "DRY_RUN: ${SYSTEMCTL_BIN} start ${TARGET_TIMER}"
    echo "DRY_RUN: would verify is-enabled=enabled and is-active=active, else roll back"
    echo
    echo "DRY_RUN complete -- nothing was enabled or started."
    exit 0
fi

if ! "${SYSTEMCTL_BIN}" enable "${TARGET_TIMER}"; then
    rollback
    fail "could not enable ${TARGET_TIMER}."
fi
if ! "${SYSTEMCTL_BIN}" start "${TARGET_TIMER}"; then
    rollback
    fail "could not start ${TARGET_TIMER}."
fi

final_enabled="$("${SYSTEMCTL_BIN}" is-enabled "${TARGET_TIMER}" 2>/dev/null || true)"
final_active="$("${SYSTEMCTL_BIN}" is-active "${TARGET_TIMER}" 2>/dev/null || true)"
if [ "${final_enabled}" != "enabled" ] || [ "${final_active}" != "active" ]; then
    echo "ERROR: ${TARGET_TIMER} ended at is-enabled='${final_enabled}' is-active='${final_active}'." >&2
    rollback
    exit 1
fi

# The one thing this script must never have done.
live_after="$("${SYSTEMCTL_BIN}" is-enabled "${LIVE_UNIT}" 2>/dev/null || true)"
if [ "${live_after}" != "static" ]; then
    echo "ERROR: ${LIVE_UNIT} is now '${live_after}' -- rolling back." >&2
    rollback
    exit 1
fi

echo
echo "${TARGET_TIMER}: is-enabled=${final_enabled} is-active=${final_active}"
echo "${LIVE_UNIT}: is-enabled=${live_after} (unchanged)"
echo "Shadow timer armed. No other timer and no order path was touched."
