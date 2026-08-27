# Locate the RELEASE a scanner cron must run, and where it may write.
# Sourced, never executed.
#
# The split-brain this removes
# ----------------------------
# The scanner ran from a mutable checkout at /home/ubuntu/trading while
# the trading runtime ran an immutable release. On 2026-08-27 that
# checkout was still pinned at 5326eac, roughly twenty commits behind,
# and every scanner change deployed to the release had been inert in
# production the whole time -- deployed, tested, and never executed.
# Nothing errored: both halves reported success about different code.
#
# Code is the release. Data is not.
# ---------------------------------
# The earlier version of this file took exactly ONE variable from the
# release env and deliberately refused TRADING_PROJECT_ROOT, because a
# scanner inheriting it would write analytics, logs and its universe
# file INTO the immutable release. That concern was right; the
# conclusion was too narrow. The answer is not to leave the scanner on
# stale code, it is to separate the two things being conflated:
#
#     CODE  the release, verified against the deployed SHA
#     DATA  a shared mutable directory outside every release
#
# Every scanner write path already has an environment override, so the
# separation costs three variables and no code change:
#
#     SCANNER_UNIVERSE_FILE   else <root>/universe.csv
#     SCANNER_ANALYTICS_DIR   else <root>/logs/scanners
#     SCANNER_LOG_DIR         else <root>/logs/scanners
#
# Fail closed, never fall back
# ----------------------------
# A missing env file, an absent release, or a scanner SHA that does not
# match the deployed one stops the scan. There is no fallback to the
# checkout: falling back is what produced twenty commits of silent drift,
# and a scan that does not run is visible in a way that one running old
# code is not.

: "${SCANNER_SHARED_ENV:=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env}"
#: Mutable operational data, deliberately a sibling of the releases
#: rather than inside one.
: "${SCANNER_DATA_ROOT:=/home/ubuntu/releases/us-stock-trading/shared/scanner}"

SHARED_CANDIDATE_DIR_KEY=SCANNER_CANDIDATE_DIR

scan_log() {
    echo "$(date -u +%FT%TZ) $*" >&2
}

_shared_env_value() {
    grep -m1 "^${1}=" "$SCANNER_SHARED_ENV" 2>/dev/null | cut -d= -f2-
}

# Resolve the release the scanner must run, verify it, and export
# SCANNER_RUNTIME_ROOT / TRADING_PROJECT_ROOT.
#
# Returns 0 with both exported, or 1 having named the reason.
resolve_release_root() {
    if [ ! -r "$SCANNER_SHARED_ENV" ]; then
        scan_log "SCANNER_RUNTIME_ROOT_INVALID: shared env $SCANNER_SHARED_ENV" \
                 "is not readable; refusing to scan"
        return 1
    fi

    local root deployed validated scanner_sha
    root=$(_shared_env_value TRADING_PROJECT_ROOT)
    if [ -z "$root" ] || [ ! -d "$root" ]; then
        scan_log "SCANNER_RUNTIME_ROOT_INVALID: TRADING_PROJECT_ROOT='${root:-<unset>}'" \
                 "is not a directory; refusing to scan"
        return 1
    fi
    if [ ! -x "$root/venv/bin/python" ]; then
        # §7: one venv, the release's. A scanner quietly using a
        # different interpreter is the same class of split as the code.
        scan_log "SCANNER_RUNTIME_ROOT_INVALID: no venv at $root/venv;" \
                 "refusing to scan"
        return 1
    fi

    deployed=$(_shared_env_value DEPLOYED_COMMIT)
    validated=$(_shared_env_value VALIDATED_COMMIT)
    scanner_sha=$(cd "$root" && git rev-parse HEAD 2>/dev/null)
    if [ -z "$scanner_sha" ] || [ "$scanner_sha" != "$deployed" ] \
       || [ "$scanner_sha" != "$validated" ]; then
        scan_log "SCANNER_RELEASE_DRIFT: scanner=${scanner_sha:-<unknown>}" \
                 "deployed=${deployed:-<unset>} validated=${validated:-<unset>};" \
                 "refusing to publish live candidates"
        return 1
    fi

    export SCANNER_RUNTIME_ROOT="$root"
    export TRADING_PROJECT_ROOT="$root"
    export SCANNER_SHA="$scanner_sha"
    return 0
}

# Point every scanner write at the shared mutable data root, so running
# release code cannot dirty the release it is running.
resolve_scanner_data_dirs() {
    mkdir -p "$SCANNER_DATA_ROOT/logs/scanners" "$SCANNER_DATA_ROOT/logs/cron" \
        2>/dev/null || {
        scan_log "SCANNER_RUNTIME_ROOT_INVALID: cannot create" \
                 "$SCANNER_DATA_ROOT; refusing to scan"
        return 1
    }
    : "${SCANNER_ANALYTICS_DIR:=$SCANNER_DATA_ROOT/logs/scanners}"
    : "${SCANNER_LOG_DIR:=$SCANNER_DATA_ROOT/logs/scanners}"
    : "${SCANNER_UNIVERSE_FILE:=$SCANNER_DATA_ROOT/universe.csv}"
    export SCANNER_ANALYTICS_DIR SCANNER_LOG_DIR SCANNER_UNIVERSE_FILE
    if [ ! -r "$SCANNER_UNIVERSE_FILE" ]; then
        scan_log "SCANNER_UNIVERSE_MISSING: $SCANNER_UNIVERSE_FILE is not" \
                 "readable; refusing to scan rather than scanning an empty universe"
        return 1
    fi
    return 0
}

# Export SCANNER_CANDIDATE_DIR from the shared env file, or fail.
#
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
    local resolved
    resolved=$(_shared_env_value "$SHARED_CANDIDATE_DIR_KEY")
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
