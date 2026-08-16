#!/usr/bin/env bash
#
# CODEX-049: installs the KIS systemd units on the Oracle host.
#
# INSTALLS ONLY. It enables nothing and starts nothing.
#
# It used to `systemctl enable --now` all four read-only timers. Two
# problems came out of Oracle verification:
#
#   - arming the Shadow timer is a reviewed decision, not a side effect
#     of copying unit files into place;
#   - the final safety assertion ran AFTER those enables, so an installer
#     that exited 1 still left four timers armed and running. An operator
#     saw "install failed" while the system was in fact live-ish.
#
# So installation and activation are now separate programs. This one puts
# files in place and leaves every timer disabled and inactive.
# `scripts/enable_oracle_shadow_timer.sh` arms the Shadow timer, alone,
# behind an explicit operator approval.
#
# Every check that can be made before touching the host is made before
# touching the host, so a rejected install leaves nothing half-applied.
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

# Injectable so the test suite can drive the real control flow against a
# stub. Nothing else about the flow changes between real and stubbed.
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SYSTEMD_ANALYZE_BIN="${SYSTEMD_ANALYZE_BIN:-systemd-analyze}"

LIVE_UNIT="us-stock-trading-live.service"

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

echo "== us-stock-trading Oracle service install (install only -- enables nothing) =="
echo "release dir : ${RELEASE_DIR}"
echo "env file    : ${ENV_FILE}"
echo "log dir     : ${LOG_DIR}"
echo "unit dir    : ${UNIT_DIR}"
echo "shared dir  : ${SHARED_DIR}"

# =====================================================================
# 1. Paths, user, permissions -- all before anything is installed.
# =====================================================================
for entrypoint in "${ENTRYPOINTS[@]}"; do
    [ -e "${RELEASE_DIR}/scripts/${entrypoint}" ] \
        || fail "${RELEASE_DIR}/scripts/${entrypoint} does not exist -- aborting before installing any unit."
done
[ -e "${PYTHON_BIN}" ] || fail "${PYTHON_BIN} does not exist -- create the venv first."
for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do
    [ -e "${RELEASE_DIR}/deploy/systemd/${unit}" ] \
        || fail "${RELEASE_DIR}/deploy/systemd/${unit} does not exist -- aborting."
done

if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
    run groupadd --system "${SERVICE_GROUP}"
fi
run usermod -a -G "${SERVICE_GROUP}" "${SERVICE_USER}"

run install -d -m 0750 -o root -g "${SERVICE_GROUP}" "${ENV_DIR}"
run install -d -m 0770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${LOG_DIR}"
run install -d -m 0770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${SHARED_DIR}/logs"

# shared/state is the ONE directory that must not be group-writable.
# Since the limiter began failing closed on any file in its own temp
# namespace that it could not have written, a single planted file there
# stops every service on the box. Under 0770 root:trading that was one
# `trading` group member away; the services all run as ${SERVICE_USER},
# so nothing needs group access to it in the first place.
run install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${SHARED_DIR}/state"
if [ "${DRY_RUN}" != "1" ]; then
    # `install -d` also re-applies to an existing directory, but a
    # previous install may have left 0770 root:trading behind, and a
    # silent failure here is the whole exposure -- so verify.
    state_mode="$(stat -c '%a' "${SHARED_DIR}/state")"
    state_owner="$(stat -c '%U:%G' "${SHARED_DIR}/state")"
    if [ "${state_mode}" != "700" ] || [ "${state_owner}" != "${SERVICE_USER}:${SERVICE_USER}" ]; then
        fail "${SHARED_DIR}/state is ${state_mode} ${state_owner}, expected 700 ${SERVICE_USER}:${SERVICE_USER}."
    fi
    echo "shared state: ${SHARED_DIR}/state is ${state_mode} ${state_owner}"
else
    echo "DRY_RUN: would verify ${SHARED_DIR}/state is 0700 ${SERVICE_USER}:${SERVICE_USER}"
fi

[ -f "${ENV_FILE}" ] || fail "${ENV_FILE} does not exist. Create it first (see the Oracle runbook's environment section), then re-run."
run chown root:"${SERVICE_GROUP}" "${ENV_FILE}"
run chmod 0640 "${ENV_FILE}"

# =====================================================================
# 2. Render every unit into a scratch directory.
# =====================================================================
render_unit() {
    local src="$1" dest="$2"
    sed -e "s#@TRADING_RELEASE_ROOT@#${RELEASE_DIR}#g" \
        -e "s#@TRADING_SHARED_ROOT@#${SHARED_DIR}#g" \
        -e "s#@TRADING_ENV_FILE@#${ENV_FILE}#g" \
        -e "s#@TRADING_LOG_DIR@#${LOG_DIR}#g" \
        "${src}" > "${dest}"
}

RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "${RENDER_DIR}"' EXIT

for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do
    render_unit "${RELEASE_DIR}/deploy/systemd/${unit}" "${RENDER_DIR}/${unit}"
done

# =====================================================================
# 3. Placeholders. An unsubstituted unit must never reach the host.
# =====================================================================
for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do
    if grep -q "@TRADING_[A-Z_]*@" "${RENDER_DIR}/${unit}"; then
        echo "ERROR: ${unit} still contains an unsubstituted placeholder:" >&2
        grep -o "@TRADING_[A-Z_]*@" "${RENDER_DIR}/${unit}" | sort -u >&2
        exit 1
    fi
done
echo "placeholders : none remaining in 10 rendered unit(s)"

# =====================================================================
# 4. systemd-analyze verify on the rendered units.
# =====================================================================
if command -v "${SYSTEMD_ANALYZE_BIN}" >/dev/null 2>&1; then
    analyze_out="$("${SYSTEMD_ANALYZE_BIN}" verify "${RENDER_DIR}"/us-stock-trading-*.service \
                                                   "${RENDER_DIR}"/us-stock-trading-*.timer 2>&1 || true)"
    # Only OUR units matter here; the tool also reports on unrelated
    # system units it pulls in while resolving dependencies.
    if echo "${analyze_out}" | grep -E "^us-stock-trading-[a-z-]*\.(service|timer):" \
                             | grep -vE "is not executable: No such file"; then
        fail "systemd-analyze verify rejected a rendered unit (see above)."
    fi
    echo "unit syntax  : systemd-analyze verify clean"
else
    fail "${SYSTEMD_ANALYZE_BIN} not found -- refusing to install unverified units."
fi

# =====================================================================
# 5. The live unit must have no [Install] section.
# =====================================================================
if grep -qE "^\[Install\]" "${RENDER_DIR}/${LIVE_UNIT}"; then
    fail "${LIVE_UNIT} has an [Install] section -- it would become enableable."
fi
if grep -qE "^(WantedBy|RequiredBy|Also|Alias)=" "${RENDER_DIR}/${LIVE_UNIT}"; then
    fail "${LIVE_UNIT} declares an installation directive -- it would become enableable."
fi
echo "live unit    : no [Install] section"

# =====================================================================
# 6/7. Prove enableability in a throwaway --root sandbox, against the
#      unit file about to be installed. Nothing on this host is touched.
#
#      `static` is the GOAL state: systemd reports it for a unit with no
#      [Install] section. `disabled` would mean an [Install] section came
#      back, so it is a failure here, not a pass -- the old check had
#      this exactly inverted and rejected `static`.
# =====================================================================
SANDBOX="$(mktemp -d)"
trap 'rm -rf "${RENDER_DIR}" "${SANDBOX}"' EXIT
mkdir -p "${SANDBOX}/etc/systemd/system"
cp "${RENDER_DIR}/${LIVE_UNIT}" "${SANDBOX}/etc/systemd/system/"

sandbox_state="$("${SYSTEMCTL_BIN}" --root="${SANDBOX}" is-enabled "${LIVE_UNIT}" 2>/dev/null || true)"
if [ "${sandbox_state}" != "static" ]; then
    fail "${LIVE_UNIT} reports '${sandbox_state}' in a clean sandbox; expected 'static' (no [Install] section)."
fi

"${SYSTEMCTL_BIN}" --root="${SANDBOX}" enable "${LIVE_UNIT}" >/dev/null 2>&1 || true
sandbox_links="$(find "${SANDBOX}" -type l -name "${LIVE_UNIT}" | wc -l | tr -d ' ')"
if [ "${sandbox_links}" != "0" ]; then
    fail "${LIVE_UNIT} produced ${sandbox_links} enablement symlink(s) -- it is enableable."
fi
echo "live unit    : is-enabled=static, enable produces 0 symlinks (not enableable)"

# =====================================================================
# 8. Refuse to install against a live-enabled environment file.
# =====================================================================
for forbidden in KIS_LIVE_ORDER_ENABLED LIVE_ROLLOUT_ENABLED ALPACA_ORDER_ENABLED; do
    if grep -qiE "^${forbidden}=(1|true|yes|on)$" "${ENV_FILE}"; then
        fail "${ENV_FILE} has ${forbidden} enabled -- this installer only deploys the read-only posture."
    fi
done
grep -qiE "^ENTRY_DISABLED=(1|true|yes|on)$" "${ENV_FILE}" \
    || fail "${ENV_FILE} does not set ENTRY_DISABLED=true -- refusing to install."
echo "env file     : read-only posture confirmed"

# =====================================================================
# 9. Migration, then preflight. A unit installed against a stale schema
#    or an unsafe posture is the failure this package exists to prevent.
# =====================================================================
run "${PYTHON_BIN}" "${RELEASE_DIR}/scripts/run_migrations.py"
run "${PYTHON_BIN}" "${RELEASE_DIR}/scripts/preflight_kis_live.py"

# =====================================================================
# 10/11. Only now does anything reach the host.
# =====================================================================
for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do
    run install -m 0644 -o root -g root "${RENDER_DIR}/${unit}" "${UNIT_DIR}/${unit}"
done
run "${SYSTEMCTL_BIN}" daemon-reload

# =====================================================================
# 12/13. Everything ends disabled and stopped. The live unit is not
#        enableable, but a PREVIOUS install's symlink keeps working even
#        after the [Install] section is removed, so disable it anyway.
# =====================================================================
run "${SYSTEMCTL_BIN}" disable "${LIVE_UNIT}" || true
run "${SYSTEMCTL_BIN}" stop "${LIVE_UNIT}" || true

for timer in "${TIMER_UNITS[@]}"; do
    run "${SYSTEMCTL_BIN}" disable "${timer}" || true
    run "${SYSTEMCTL_BIN}" stop "${timer}" || true
done

# =====================================================================
# 14. Final state. Nothing was ever enabled, so a failure here still
#     leaves a safe host.
# =====================================================================
if [ "${DRY_RUN}" = "1" ]; then
    echo "DRY_RUN: would verify ${LIVE_UNIT} is static + inactive"
    echo "DRY_RUN: would verify every trading timer is disabled + inactive"
    echo
    echo "DRY_RUN complete -- nothing was enabled, started or installed."
    exit 0
fi

failures=0
report_state() {
    local unit="$1" want_enabled="$2"
    local enabled active
    enabled="$("${SYSTEMCTL_BIN}" is-enabled "${unit}" 2>/dev/null || true)"
    active="$("${SYSTEMCTL_BIN}" is-active "${unit}" 2>/dev/null || true)"
    echo "  ${unit}: is-enabled=${enabled:-unknown} is-active=${active:-unknown}"
    if [ "${enabled}" != "${want_enabled}" ]; then
        echo "ERROR: ${unit} is-enabled='${enabled}', expected '${want_enabled}'." >&2
        failures=$((failures + 1))
    fi
    if [ "${active}" = "active" ] || [ "${active}" = "activating" ]; then
        echo "ERROR: ${unit} is ${active}; this installer must leave everything stopped." >&2
        failures=$((failures + 1))
    fi
}

echo
echo "Installed. Final state:"
report_state "${LIVE_UNIT}" "static"
for timer in "${TIMER_UNITS[@]}"; do
    report_state "${timer}" "disabled"
done

if [ "${failures}" -ne 0 ]; then
    fail "${failures} unit(s) are not in the expected installed-but-inactive state."
fi

echo
echo "Units are installed and every timer is disabled and inactive."
echo "To arm the Shadow timer (a separate, reviewed decision):"
echo "  sudo ALLOW_SHADOW_TIMER_ENABLE=true ${RELEASE_DIR}/scripts/enable_oracle_shadow_timer.sh"
