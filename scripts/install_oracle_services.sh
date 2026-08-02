#!/usr/bin/env bash
#
# CODEX-049: installs the KIS systemd units on the Oracle host.
#
# Installs six services and four timers. Enables the read-only ones only.
# `us-stock-trading-live.service` is installed but NEVER enabled or
# started by this script -- and is actively disabled and stopped if a
# previous run (or a manual `systemctl enable`) left it on. Starting it
# is a separate, explicit operator action that also requires a reviewed
# change to the environment file.
#
# Idempotent: safe to re-run after a redeploy.
#
# Usage (on the Oracle host, as a user with sudo):
#   sudo TRADING_RELEASE_ROOT=/home/ubuntu/releases/us-stock-trading/current-readonly \
#        TRADING_SHARED_ROOT=/home/ubuntu/releases/us-stock-trading/shared \
#        scripts/install_oracle_services.sh
#
# Unit files ship as TEMPLATES: @TRADING_RELEASE_ROOT@, @TRADING_SHARED_ROOT@,
# @TRADING_ENV_FILE@ and @TRADING_LOG_DIR@ are substituted here, so no unit
# carries a hardcoded deployment path.
#
set -euo pipefail

# LOW: the release layout is an INPUT, not a constant baked into ten
# unit files. TRADING_RELEASE_ROOT/TRADING_SHARED_ROOT are the documented
# names; RELEASE_DIR/SHARED_DIR remain accepted so an existing runbook
# invocation keeps working.
RELEASE_DIR="${TRADING_RELEASE_ROOT:-${RELEASE_DIR:-/home/ubuntu/releases/us-stock-trading/current-readonly}}"
SHARED_DIR="${TRADING_SHARED_ROOT:-${SHARED_DIR:-/home/ubuntu/releases/us-stock-trading/shared}}"
ENV_DIR="${ENV_DIR:-/etc/us-stock-trading}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/live-readonly.env}"
LOG_DIR="${LOG_DIR:-/var/log/us-stock-trading}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SERVICE_GROUP="${SERVICE_GROUP:-trading}"
SERVICE_USER="${SERVICE_USER:-ubuntu}"
PYTHON_BIN="${PYTHON_BIN:-${RELEASE_DIR}/venv/bin/python}"
DRY_RUN="${DRY_RUN:-0}"

ENTRYPOINTS=(
    preflight_kis_live.py
    run_migrations.py
    run_reconciliation.py
    run_shadow_mode.py
    run_shadow_exit_evaluation.py
    run_health_report.py
    run_live_buy_entry.py
)

SERVICE_UNITS=(
    us-stock-trading-migrate.service
    us-stock-trading-reconcile.service
    us-stock-trading-shadow.service
    us-stock-trading-shadow-exit.service
    us-stock-trading-health.service
    us-stock-trading-live.service
)

TIMER_UNITS=(
    us-stock-trading-reconcile.timer
    us-stock-trading-shadow.timer
    us-stock-trading-shadow-exit.timer
    us-stock-trading-health.timer
)

# Every timer that may be enabled. The live service has no timer, by design.
ENABLE_TIMERS=(
    us-stock-trading-reconcile.timer
    us-stock-trading-shadow.timer
    us-stock-trading-shadow-exit.timer
    us-stock-trading-health.timer
)

run() {
    if [ "${DRY_RUN}" = "1" ]; then
        echo "DRY_RUN: $*"
    else
        "$@"
    fi
}

echo "== us-stock-trading Oracle service install =="
echo "release dir : ${RELEASE_DIR}"
echo "env file    : ${ENV_FILE}"
echo "log dir     : ${LOG_DIR}"
echo "unit dir    : ${UNIT_DIR}"
echo "shared dir  : ${SHARED_DIR}"

# ---------------------------------------------------------------------
# 1. Sanity: every file the units reference must actually exist.
# ---------------------------------------------------------------------
for entrypoint in "${ENTRYPOINTS[@]}"; do
    if [ ! -e "${RELEASE_DIR}/scripts/${entrypoint}" ]; then
        echo "ERROR: ${RELEASE_DIR}/scripts/${entrypoint} does not exist -- aborting before installing any unit." >&2
        exit 1
    fi
done
if [ ! -e "${PYTHON_BIN}" ]; then
    echo "ERROR: ${PYTHON_BIN} does not exist -- create the venv first." >&2
    exit 1
fi
for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do
    if [ ! -e "${RELEASE_DIR}/deploy/systemd/${unit}" ]; then
        echo "ERROR: ${RELEASE_DIR}/deploy/systemd/${unit} does not exist -- aborting." >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------
# 2. Group, log dir, environment file (root:trading, 0640 -- the env file
#    holds the KIS App Key/Secret and the account number).
# ---------------------------------------------------------------------
if ! getent group "${SERVICE_GROUP}" >/dev/null; then
    run groupadd --system "${SERVICE_GROUP}"
fi
run usermod -a -G "${SERVICE_GROUP}" "${SERVICE_USER}"

run install -d -m 0750 -o root -g "${SERVICE_GROUP}" "${ENV_DIR}"
run install -d -m 0770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${LOG_DIR}"
run install -d -m 0770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${SHARED_DIR}/state"
run install -d -m 0770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${SHARED_DIR}/logs"

if [ ! -f "${ENV_FILE}" ]; then
    echo "ERROR: ${ENV_FILE} does not exist." >&2
    echo "Create it first (see the Oracle runbook's environment section), then re-run." >&2
    exit 1
fi
run chown root:"${SERVICE_GROUP}" "${ENV_FILE}"
run chmod 0640 "${ENV_FILE}"

# ---------------------------------------------------------------------
# 3. Refuse to install against a live-enabled environment file. The whole
#    package is the READ-ONLY posture; installing it while live order
#    flags are on would be installing something else.
# ---------------------------------------------------------------------
for forbidden in KIS_LIVE_ORDER_ENABLED LIVE_ROLLOUT_ENABLED ALPACA_ORDER_ENABLED; do
    if grep -qiE "^${forbidden}=(1|true|yes|on)$" "${ENV_FILE}"; then
        echo "ERROR: ${ENV_FILE} has ${forbidden} enabled -- this installer only deploys the read-only posture." >&2
        exit 1
    fi
done
if ! grep -qiE "^ENTRY_DISABLED=(1|true|yes|on)$" "${ENV_FILE}"; then
    echo "ERROR: ${ENV_FILE} does not set ENTRY_DISABLED=true -- refusing to install." >&2
    exit 1
fi

# ---------------------------------------------------------------------
# 4. Install the units.
# ---------------------------------------------------------------------
render_unit() {
    # Substitutes every deployment path into a unit template. Fails loudly
    # if any placeholder survives -- an unsubstituted unit must never be
    # installed.
    local src="$1" dest="$2"
    sed -e "s#@TRADING_RELEASE_ROOT@#${RELEASE_DIR}#g" \
        -e "s#@TRADING_SHARED_ROOT@#${SHARED_DIR}#g" \
        -e "s#@TRADING_ENV_FILE@#${ENV_FILE}#g" \
        -e "s#@TRADING_LOG_DIR@#${LOG_DIR}#g" \
        "${src}" > "${dest}"
    if grep -q "@TRADING_[A-Z_]*@" "${dest}"; then
        echo "ERROR: ${dest} still contains an unsubstituted placeholder:" >&2
        grep -o "@TRADING_[A-Z_]*@" "${dest}" | sort -u >&2
        exit 1
    fi
}

RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "${RENDER_DIR}"' EXIT

for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do
    render_unit "${RELEASE_DIR}/deploy/systemd/${unit}" "${RENDER_DIR}/${unit}"
    run install -m 0644 -o root -g root "${RENDER_DIR}/${unit}" "${UNIT_DIR}/${unit}"
done

run systemctl daemon-reload

# ---------------------------------------------------------------------
# 5. Migration, then preflight. Neither is optional: a unit started
#    against a stale schema or an unsafe posture is the failure this
#    whole package exists to prevent.
# ---------------------------------------------------------------------
run "${PYTHON_BIN}" "${RELEASE_DIR}/scripts/run_migrations.py"
run "${PYTHON_BIN}" "${RELEASE_DIR}/scripts/preflight_kis_live.py"

# ---------------------------------------------------------------------
# 6. Enable ONLY the read-only services/timers. The live unit stays
#    disabled and stopped.
# ---------------------------------------------------------------------
run systemctl enable us-stock-trading-migrate.service
for timer in "${ENABLE_TIMERS[@]}"; do
    run systemctl enable --now "${timer}"
done

run systemctl disable us-stock-trading-live.service || true
run systemctl stop us-stock-trading-live.service || true

echo
echo "Installed. Current state:"
for timer in "${ENABLE_TIMERS[@]}"; do
    echo -n "${timer}: "
    systemctl is-enabled "${timer}" 2>/dev/null || echo "unknown"
done
echo -n "us-stock-trading-live.service: "
systemctl is-enabled us-stock-trading-live.service 2>/dev/null || echo "disabled (expected)"
