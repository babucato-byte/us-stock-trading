# Locate the SHARED candidate store for a scanner-runtime cron entry.
# Sourced, never executed.
#
# The problem this exists for
# ---------------------------
# The scanner runtime and the trading runtime are two different
# deployments -- a git checkout at /home/ubuntu/trading and an immutable
# release under /home/ubuntu/releases/us-stock-trading/<sha>. They have
# to agree on ONE candidate directory, and only the release's env file
# says where it is.
#
# `s6_scan.sh` and `s2_regular_scan.sh` did not read it. So both scanners
# published into the checkout's own logs/scanners/candidates while the
# trading runtime read shared/state/candidates. Neither side errored: the
# producer reported success and the consumer found an empty directory.
#
# Why not just source the whole env file
# --------------------------------------
# It sets TRADING_PROJECT_ROOT to the RELEASE. A scanner cron that
# inherited that would write its analytics, logs and eligibility caches
# into the release directory -- a different bug, in the other direction,
# and one that also dirties an immutable release. Exactly one variable is
# taken, by name.
#
# Fail closed
# -----------
# If the env file is unreadable or does not name the store, the caller
# exits non-zero WITHOUT scanning. The Python side refuses too
# (scanners/publish/candidates.py::candidate_dir), so a script that
# forgot this still cannot publish into a private directory -- but
# failing here means the failure names the actual cause instead of
# surfacing as a scan that ran and handed nothing over.

#: Overridable so the resolution can be tested against a fixture rather
#: than against the production host. Production leaves both unset.
: "${SCANNER_SHARED_ENV:=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env}"
: "${SCANNER_RUNTIME_ROOT:=/home/ubuntu/trading}"

#: The one variable taken from the release's environment.
SHARED_CANDIDATE_DIR_KEY=SCANNER_CANDIDATE_DIR

scan_log() {
    echo "$(date -u +%FT%TZ) $*" >&2
}

# Export SCANNER_CANDIDATE_DIR from the shared env file, or fail.
#
# Returns 0 with the variable exported, or 1 having explained why not.
# An already-exported value wins, so an operator can redirect a manual
# run without editing anything.
resolve_shared_candidate_dir() {
    if [ -n "${SCANNER_CANDIDATE_DIR:-}" ]; then
        export SCANNER_CANDIDATE_DIR
        return 0
    fi

    if [ ! -r "$SCANNER_SHARED_ENV" ]; then
        scan_log "PRODUCER_CONFIG_ERROR: shared env $SCANNER_SHARED_ENV is not readable;" \
                 "refusing to scan rather than publish where no consumer reads"
        return 1
    fi

    # One variable, by name, from a line that is not a comment. `cut -d=
    # -f2-` keeps any '=' inside the value.
    local resolved
    resolved=$(grep -m1 "^${SHARED_CANDIDATE_DIR_KEY}=" "$SCANNER_SHARED_ENV" | cut -d= -f2-)
    if [ -z "$resolved" ]; then
        scan_log "PRODUCER_CONFIG_ERROR: $SCANNER_SHARED_ENV does not set" \
                 "${SHARED_CANDIDATE_DIR_KEY}; refusing to scan"
        return 1
    fi
    if [ ! -d "$resolved" ]; then
        scan_log "PRODUCER_CONFIG_ERROR: ${SHARED_CANDIDATE_DIR_KEY}=$resolved" \
                 "does not exist; refusing to scan"
        return 1
    fi

    export SCANNER_CANDIDATE_DIR="$resolved"
    return 0
}
