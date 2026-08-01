#!/usr/bin/env bash
#
# CODEX-049: installs the KIS systemd units on the Oracle host.
#
# Installs three services. Enables TWO of them (shadow, reconcile).
# `us-stock-trading-live.service` is installed but deliberately NEVER
# enabled by this script -- starting it is a separate, explicit operator
# action that also requires a reviewed change to the environment file.
#
# Idempotent: safe to re-run after a redeploy.
#
# Usage (on the Oracle host, as a user with sudo):
#   sudo RELEASE_DIR=/home/ubuntu/trading-release scripts/install_oracle_services.sh
#
set -euo pipefail

RELEASE_DIR="${RELEASE_DIR:-/home/ubuntu/trading-release}"
ENV_DIR="${ENV_DIR:-/etc/us-stock-trading}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/live-readonly.env}"
LOG_DIR="${LOG_DIR:-/var/log/us-stock-trading}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SERVICE_GROUP="${SERVICE_GROUP:-trading}"
SERVICE_USER="${SERVICE_USER:-ubuntu}"
DRY_RUN="${DRY_RUN:-0}"

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

# ---------------------------------------------------------------------
# 1. Sanity: every file the units reference must actually exist.
# ---------------------------------------------------------------------
for script in \
    "${RELEASE_DIR}/scripts/preflight_kis_live.py" \
    "${RELEASE_DIR}/scripts/run_shadow_mode.py" \
    "${RELEASE_DIR}/scripts/run_reconciliation.py" \
    "${RELEASE_DIR}/scripts/run_live_buy_entry.py" \
    "${RELEASE_DIR}/venv/bin/python"
do
    if [ ! -e "${script}" ]; then
        echo "ERROR: ${script} does not exist -- aborting before installing any unit." >&2
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

if [ ! -f "${ENV_FILE}" ]; then
    echo "ERROR: ${ENV_FILE} does not exist." >&2
    echo "Create it first (see the Oracle runbook's environment section), then re-run." >&2
    exit 1
fi
run chown root:"${SERVICE_GROUP}" "${ENV_FILE}"
run chmod 0640 "${ENV_FILE}"

# ---------------------------------------------------------------------
# 3. Install the units.
# ---------------------------------------------------------------------
for unit in \
    us-stock-trading-shadow.service \
    us-stock-trading-shadow.timer \
    us-stock-trading-reconcile.service \
    us-stock-trading-reconcile.timer \
    us-stock-trading-live.service
do
    run install -m 0644 -o root -g root \
        "${RELEASE_DIR}/deploy/systemd/${unit}" "${UNIT_DIR}/${unit}"
done

run systemctl daemon-reload

# ---------------------------------------------------------------------
# 4. Enable ONLY the read-only services. The live unit stays disabled.
# ---------------------------------------------------------------------
run systemctl enable --now us-stock-trading-reconcile.timer
run systemctl enable --now us-stock-trading-shadow.timer

# Explicitly assert the live unit is NOT enabled, in case a previous
# install (or a manual `systemctl enable`) left it on.
if systemctl is-enabled us-stock-trading-live.service >/dev/null 2>&1; then
    echo "WARNING: us-stock-trading-live.service was enabled -- disabling it." >&2
    run systemctl disable us-stock-trading-live.service
fi

echo
echo "Installed. Current state:"
systemctl is-enabled us-stock-trading-shadow.timer    || true
systemctl is-enabled us-stock-trading-reconcile.timer || true
echo -n "us-stock-trading-live.service: "
systemctl is-enabled us-stock-trading-live.service 2>/dev/null || echo "disabled (expected)"
