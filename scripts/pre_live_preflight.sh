#!/bin/bash
# One-command, READ-ONLY pre-live preflight for the production host.
#
# Answers "may the next session start on this host as deployed?" with
# exactly one of PASS / WARNING / BLOCKED and the names of every check
# that did not pass. It reads the shared env file, the release
# directory, the state database (read-only URI), the caches, the
# collector status, the crontab and the disk. It never writes state,
# never places an order and never refreshes external data; the only
# network activity is the documented read-only KIS account probe
# (scripts/verify_kis_account_cash.py), which can be skipped with
# --skip-kis.
#
# Usage (on the host, or piped over ssh):
#     bash scripts/pre_live_preflight.sh [--skip-kis]
#     ssh trading bash -s -- --skip-kis < scripts/pre_live_preflight.sh
#
# Exit code: 0 PASS, 1 WARNING, 2 BLOCKED.
#
# Written without `set -e`: a check that fails must be REPORTED, not
# abort the report. Every pipeline that may legitimately return non-zero
# (grep with no match, diff with differences) is guarded explicitly.

set -u

SKIP_KIS=0
for arg in "$@"; do
    case "$arg" in
        --skip-kis) SKIP_KIS=1 ;;
    esac
done

BASE="${TRADING_RELEASES_BASE:-/home/ubuntu/releases/us-stock-trading}"
ENV_FILE="${TRADING_SHARED_ENV:-$BASE/shared/env/kis-readonly.env}"
LOCK_DIR="${TRADING_CRON_LOCK_DIR:-/home/ubuntu/logs/cron}"
RUNTIME_LOCK="$LOCK_DIR/s6_exec.lock"
DISK_WARN_PCT="${PREFLIGHT_DISK_WARN_PCT:-90}"
DISK_BLOCK_PCT="${PREFLIGHT_DISK_BLOCK_PCT:-95}"
RECON_MAX_AGE_SECONDS="${PREFLIGHT_RECON_MAX_AGE_SECONDS:-900}"
TOKEN_MIN_REMAINING_SECONDS="${PREFLIGHT_TOKEN_MIN_REMAINING_SECONDS:-300}"

FAILED=()
WARNED=()
pass() { printf '[PASS] %s\n' "$1"; }
warn() { WARNED+=("$1"); printf '[WARN] %s %s\n' "$1" "${2:-}"; }
fail() { FAILED+=("$1"); printf '[FAIL] %s %s\n' "$1" "${2:-}"; }
info() { printf '[INFO] %s\n' "$1"; }

env_value() {
    # First match only; never echoes anything but the requested key.
    grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-
}

printf 'PRE-LIVE PREFLIGHT (read-only)  %s\n' "$(date -u +%FT%TZ)"

# ---------------------------------------------------------------------
# 1. release identity
# ---------------------------------------------------------------------
if [ ! -r "$ENV_FILE" ]; then
    fail ENV_FILE_UNREADABLE "$ENV_FILE"
    printf '\nRESULT: BLOCKED\nFAILED: ENV_FILE_UNREADABLE\n'
    exit 2
fi
DEPLOYED="$(env_value DEPLOYED_COMMIT)"
VALIDATED="$(env_value VALIDATED_COMMIT)"
ROOT="$(env_value TRADING_PROJECT_ROOT)"
PY="$ROOT/venv/bin/python"

if [ -z "$ROOT" ] || [ ! -d "$ROOT" ]; then
    fail RELEASE_ROOT_MISSING "TRADING_PROJECT_ROOT=${ROOT:-<unset>}"
else
    HEAD_SHA="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
    if [ "$HEAD_SHA" != "unknown" ] && [ "$HEAD_SHA" = "$DEPLOYED" ] && [ "$HEAD_SHA" = "$VALIDATED" ]; then
        pass "HEAD==VALIDATED==DEPLOYED (${HEAD_SHA:0:12})"
    else
        fail COMMIT_MISMATCH "HEAD=${HEAD_SHA:0:12} DEPLOYED=${DEPLOYED:0:12} VALIDATED=${VALIDATED:0:12}"
    fi
    DIRTY="$(git -C "$ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$DIRTY" = "0" ]; then
        pass "production worktree clean"
    else
        fail WORKTREE_DIRTY "$DIRTY path(s)"
    fi
    if [ -x "$PY" ]; then
        pass "release interpreter present"
    else
        fail RELEASE_VENV_MISSING "$PY"
    fi
fi

# ---------------------------------------------------------------------
# 2. schema, caches, universe, ranking, reconciliation, token -- via
#    the release's own python, read-only
# ---------------------------------------------------------------------
if [ -x "${PY:-/nonexistent}" ]; then
    set -a; . "$ENV_FILE"; set +a
    # The scanner crons resolve their data directories through
    # deploy/cron/shared_env.sh, not through the env file. Mirror those
    # defaults here (without calling the resolver, which creates
    # directories) so the universe and ranking checks look where the
    # scans actually read and write.
    : "${SCANNER_DATA_ROOT:=$BASE/shared/scanner}"
    : "${SCANNER_ANALYTICS_DIR:=$SCANNER_DATA_ROOT/logs/scanners}"
    : "${SCANNER_UNIVERSE_FILE:=$SCANNER_DATA_ROOT/universe.csv}"
    export SCANNER_DATA_ROOT SCANNER_ANALYTICS_DIR SCANNER_UNIVERSE_FILE
    PY_OUT="$(cd "$ROOT" && "$PY" - "$RECON_MAX_AGE_SECONDS" "$TOKEN_MIN_REMAINING_SECONDS" <<'PY' 2>&1
import json, os, sqlite3, sys, time
from datetime import datetime, timezone, date
from pathlib import Path

recon_max_age = float(sys.argv[1])
token_min_remaining = float(sys.argv[2])
now = datetime.now(timezone.utc)

def out(kind, name, detail=""):
    print(f"{kind}|{name}|{detail}")

# -- schema (read-only URI) --
try:
    from state_store.migrations import CURRENT_SCHEMA_VERSION
    db_path = os.environ.get("STATE_STORE_DB_FILE") or os.environ.get("TRADING_STATE_DB")
    if not db_path:
        out("FAIL", "STATE_DB_PATH_UNSET")
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        version = conn.execute("select max(version) from schema_migrations").fetchone()[0]
        if version == CURRENT_SCHEMA_VERSION:
            out("PASS", f"schema version {version} == code {CURRENT_SCHEMA_VERSION}")
        else:
            out("FAIL", "SCHEMA_VERSION_MISMATCH", f"db={version} code={CURRENT_SCHEMA_VERSION}")
        cols = [r[1] for r in conn.execute("pragma table_info(s6_positions)")]
        if "peak_price_at" in cols:
            out("PASS", "s6_positions.peak_price_at present")
        else:
            out("FAIL", "PEAK_PRICE_AT_MISSING")
        live = conn.execute("select count(*) from s6_positions where status != 'CLOSED'").fetchone()[0]
        closed = conn.execute("select count(*) from s6_positions where status = 'CLOSED'").fetchone()[0]
        out("INFO", f"s6_positions live={live} closed={closed}")
        conn.close()
except Exception as exc:  # noqa: BLE001
    out("FAIL", "SCHEMA_CHECK_ERROR", f"{type(exc).__name__}: {exc}")

# -- security-type cache via application logic --
try:
    from s1_live import security_type
    idx = security_type.load_index()
    idx.validate()
    out("PASS", f"security-type cache readable ({len(idx)} symbols, asof {idx.asof})")
except Exception as exc:  # noqa: BLE001
    out("FAIL", "SECURITY_TYPE_CACHE", f"{type(exc).__name__}: {str(exc)[:120]}")

# -- universe --
try:
    from scanners.universe import universe_path
    upath = universe_path()
    with open(upath, "r", encoding="utf-8") as fh:
        header = fh.readline().strip()
        count = sum(1 for line in fh if line.strip())
    if "symbol" in header.split(",") and count > 0:
        out("PASS", f"universe readable ({count} rows) {upath}")
    else:
        out("FAIL", "UNIVERSE_EMPTY_OR_MALFORMED", f"{upath} rows={count} header={header[:40]}")
except Exception as exc:  # noqa: BLE001
    out("FAIL", "UNIVERSE_UNREADABLE", f"{type(exc).__name__}: {str(exc)[:120]}")

# -- daily liquidity ranking (canonical key preferred) --
try:
    from scanners.base import activity
    from scanners.base.trading_calendar import previous_trading_day, us_trading_day
    canonical = activity.store_path()
    if not canonical.exists():
        out("FAIL", "DAILY_LIQUIDITY_MISSING", str(canonical))
    else:
        payload = json.loads(canonical.read_text(encoding="utf-8"))
        symbols = payload.get("symbols") or {}
        days = sorted({str(r.get("trading_day")) for r in symbols.values() if r.get("trading_day")})
        newest = days[-1] if days else None
        today = us_trading_day(now)
        expected = previous_trading_day(today) if today else None
        if not symbols:
            out("FAIL", "DAILY_LIQUIDITY_EMPTY", str(canonical))
        elif expected is not None and newest is not None and newest < str(expected):
            out("WARN", "DAILY_LIQUIDITY_NOT_LATEST", f"newest={newest} expected>={expected}")
        else:
            out("PASS", f"daily_liquidity readable ({len(symbols)} symbols, newest {newest}, canonical key)")
except Exception as exc:  # noqa: BLE001
    out("FAIL", "DAILY_LIQUIDITY_CHECK_ERROR", f"{type(exc).__name__}: {str(exc)[:120]}")

# -- S6 discovery manifest (informational: refreshed by the laptop scan) --
try:
    from discovery import manifest as manifest_module
    from scanners.base.trading_calendar import us_trading_day as _utd
    mpath = os.environ.get("SCANNER_MANIFEST_PATH") or str(
        Path(os.environ.get("STATE_STORE_DB_FILE", "")).parent / "discovery" / "manifest.json")
    doc = manifest_module.read(mpath)
    if doc:
        out("INFO", f"discovery manifest trading_day={doc.get('trading_day')} session={doc.get('session')} "
                    f"symbols={len(doc.get('symbols') or [])} generated_at={doc.get('generated_at')} "
                    f"(stale manifest falls back to the active ranking)")
    else:
        out("INFO", f"discovery manifest absent at {mpath} (scan falls back to the active ranking)")
except Exception as exc:  # noqa: BLE001
    out("INFO", f"discovery manifest unreadable: {type(exc).__name__}")

# -- reconciliation state --
try:
    rpath = os.environ.get("RECONCILIATION_STATE_FILE")
    payload = json.loads(open(rpath, encoding="utf-8").read())
    checked = payload.get("checked_at")
    age = None
    if checked:
        stamp = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (now - stamp).total_seconds()
    unknown = int(payload.get("unknown_count") or 0)
    if not payload.get("clean"):
        out("FAIL", "RECONCILIATION_NOT_CLEAN", json.dumps({k: payload.get(k) for k in ("mismatch_count", "unknown_count", "halt")}))
    elif unknown != 0:
        out("FAIL", "RECONCILIATION_UNKNOWN_ORDERS", f"unknown_count={unknown}")
    elif payload.get("halt"):
        out("FAIL", "RECONCILIATION_HALT")
    elif age is None or age > recon_max_age:
        out("WARN", "RECONCILIATION_STALE", f"age={age} limit={recon_max_age}")
    else:
        out("PASS", f"reconciliation clean, unknown_count=0, {age:.0f}s old")
except Exception as exc:  # noqa: BLE001
    out("FAIL", "RECONCILIATION_STATE_UNREADABLE", f"{type(exc).__name__}: {str(exc)[:120]}")

# -- KIS token cache (validity only; nothing is issued here) --
try:
    tpath = os.environ.get("KIS_TOKEN_CACHE_FILE")
    payload = json.loads(open(tpath, encoding="utf-8").read())
    expires = float(payload.get("expires_at"))
    remaining = expires - time.time()
    if remaining >= token_min_remaining:
        out("PASS", f"KIS token cache valid ({remaining/60:.0f} min remaining)")
    else:
        out("WARN", "KIS_TOKEN_CACHE_EXPIRING", f"{remaining:.0f}s remaining; the broker re-issues on first use")
except Exception as exc:  # noqa: BLE001
    out("WARN", "KIS_TOKEN_CACHE_UNREADABLE", f"{type(exc).__name__}: {str(exc)[:80]}; the broker re-issues on first use")

# -- account identity --
acct = os.environ.get("KIS_ACCOUNT_NO", "")
allowed = os.environ.get("KIS_ALLOWED_ACCOUNT_NO", "")
if acct and acct == allowed:
    out("PASS", "KIS_ACCOUNT_NO == KIS_ALLOWED_ACCOUNT_NO")
else:
    out("FAIL", "ACCOUNT_MISMATCH")

# -- collector --
try:
    from pathlib import Path
    data_root = os.environ.get("SCANNER_DATA_ROOT") or "/home/ubuntu/releases/us-stock-trading/shared/scanner"
    spath = Path(data_root) / "realtime_bars" / "collector_status.json"
    status = json.loads(spath.read_text(encoding="utf-8"))
    if status.get("connection_state") == "CONNECTED":
        out("PASS", f"collector connected ({status.get('state')})")
    else:
        out("FAIL", "COLLECTOR_NOT_CONNECTED", str(status.get("connection_state")))
    req, got = status.get("subscription_requested"), status.get("subscription_count")
    if req and got == req:
        out("PASS", f"collector subscriptions restored ({got}/{req})")
    else:
        out("FAIL", "COLLECTOR_SUBSCRIPTIONS_INCOMPLETE", f"{got}/{req}")
except Exception as exc:  # noqa: BLE001
    out("FAIL", "COLLECTOR_STATUS_UNREADABLE", f"{type(exc).__name__}: {str(exc)[:80]}")
PY
)"
    PY_RC=$?
    if [ "$PY_RC" -ne 0 ] && [ -z "$PY_OUT" ]; then
        fail RELEASE_PYTHON_CHECKS_CRASHED "exit $PY_RC"
    fi
    while IFS='|' read -r kind name detail; do
        [ -z "$kind" ] && continue
        case "$kind" in
            PASS) pass "$name" ;;
            WARN) warn "$name" "$detail" ;;
            FAIL) fail "$name" "$detail" ;;
            INFO) info "$name" ;;
            *)    fail RELEASE_PYTHON_CHECKS_UNPARSEABLE "$kind $name $detail" ;;
        esac
    done <<< "$PY_OUT"
fi

# ---------------------------------------------------------------------
# 3. collector process runs the deployed release
# ---------------------------------------------------------------------
COLLECTOR_ARGS="$(ps -eo args 2>/dev/null | grep run_realtime_bar_collector | grep -v grep || true)"
COLLECTOR_COUNT="$(printf '%s' "$COLLECTOR_ARGS" | grep -c run_realtime_bar_collector || true)"
if [ "$COLLECTOR_COUNT" = "1" ]; then
    pass "collector running (one process)"
    if printf '%s' "$COLLECTOR_ARGS" | grep -q "$DEPLOYED"; then
        pass "collector SHA == deployed SHA"
    else
        fail COLLECTOR_ON_OLD_RELEASE "$(printf '%s' "$COLLECTOR_ARGS" | grep -o 'us-stock-trading/[0-9a-f]\{12\}' | head -1)"
    fi
elif [ "$COLLECTOR_COUNT" = "0" ]; then
    fail COLLECTOR_NOT_RUNNING
else
    fail COLLECTOR_DUPLICATE_PROCESSES "$COLLECTOR_COUNT"
fi

# ---------------------------------------------------------------------
# 4. schedulers
# ---------------------------------------------------------------------
CRON="$(crontab -l 2>/dev/null | grep -v '^\s*#' || true)"
cron_count() { printf '%s\n' "$CRON" | grep -c "$1" || true; }
for spec in "s6_scan.sh:1:SCAN_CRON" "s6_buy_entry.sh:1:ENTRY_CRON" "s6_exit_monitor.sh:1:EXIT_CRON"; do
    job="${spec%%:*}"; rest="${spec#*:}"; want="${rest%%:*}"; label="${rest#*:}"
    n="$(cron_count "$job")"
    if [ "$n" = "$want" ]; then
        pass "$label present exactly once ($job)"
    else
        fail "${label}_COUNT" "$job appears $n time(s), expected $want"
    fi
done
for spec in "reconciliation.sh:RECONCILIATION_CRON" "s6_realtime_collector.sh:COLLECTOR_CRON" "post_exit_observations.sh:POST_EXIT_CRON"; do
    job="${spec%%:*}"; label="${spec#*:}"
    n="$(cron_count "$job")"
    if [ "$n" -ge 1 ] 2>/dev/null; then
        pass "$label present ($job x$n)"
    else
        fail "${label}_MISSING" "$job"
    fi
done

# ---------------------------------------------------------------------
# 5. runtime lock
# ---------------------------------------------------------------------
if [ -d "$LOCK_DIR" ] && [ -w "$LOCK_DIR" ]; then
    EXIT_MON="$ROOT/deploy/cron/s6_exit_monitor.sh"
    EXEC_SH="/home/ubuntu/s6_exec.sh"
    ok=1
    for f in "$EXIT_MON" "$EXEC_SH"; do
        if [ -r "$f" ] && ! grep -q "$RUNTIME_LOCK" "$f"; then ok=0; fi
    done
    if [ "$ok" = "1" ]; then
        pass "runtime lock path valid and shared ($RUNTIME_LOCK)"
    else
        fail RUNTIME_LOCK_NOT_SHARED "$RUNTIME_LOCK not referenced by both runtime wrappers"
    fi
else
    fail RUNTIME_LOCK_DIR_INVALID "$LOCK_DIR"
fi

# ---------------------------------------------------------------------
# 6. disk
# ---------------------------------------------------------------------
USE_PCT="$(df -P "$BASE" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
FREE_GB="$(df -BG -P "$BASE" 2>/dev/null | awk 'NR==2 {gsub("G","",$4); print $4}')"
if [ -z "$USE_PCT" ]; then
    fail DISK_UNREADABLE
elif [ "$USE_PCT" -ge "$DISK_BLOCK_PCT" ]; then
    fail DISK_FULL "${USE_PCT}% used, ${FREE_GB}G free"
elif [ "$USE_PCT" -ge "$DISK_WARN_PCT" ]; then
    warn DISK_HIGH "${USE_PCT}% used, ${FREE_GB}G free"
else
    pass "disk ${USE_PCT}% used, ${FREE_GB}G free (warn at ${DISK_WARN_PCT}%)"
fi

# ---------------------------------------------------------------------
# 7. KIS read-only probe (auth, account, cash) -- the documented one
# ---------------------------------------------------------------------
if [ "$SKIP_KIS" = "1" ]; then
    info "KIS read-only probe skipped (--skip-kis)"
elif [ -x "${PY:-/nonexistent}" ] && [ -f "$ROOT/scripts/verify_kis_account_cash.py" ]; then
    PROBE="$(cd "$ROOT" && timeout 180 "$PY" scripts/verify_kis_account_cash.py 2>&1)"
    PROBE_RC=$?
    ORDERS="$(printf '%s' "$PROBE" | grep -o 'orders submitted: [0-9]*' | tail -1 | grep -o '[0-9]*$' || true)"
    if [ "$PROBE_RC" = "0" ] && [ "${ORDERS:-x}" = "0" ]; then
        pass "KIS auth + account + cash probe OK (orders submitted 0)"
    elif [ "${ORDERS:-x}" != "0" ] && [ -n "$ORDERS" ]; then
        fail KIS_PROBE_SUBMITTED_ORDERS "$ORDERS"
    else
        fail KIS_PROBE_FAILED "exit $PROBE_RC: $(printf '%s' "$PROBE" | grep -i -E 'error|fail|refused' | grep -v -i secret | tail -1 | cut -c1-120)"
    fi
else
    fail KIS_PROBE_UNAVAILABLE
fi

# ---------------------------------------------------------------------
# result
# ---------------------------------------------------------------------
printf '\n'
if [ "${#FAILED[@]}" -gt 0 ]; then
    printf 'RESULT: BLOCKED\nFAILED: %s\n' "$(IFS=,; echo "${FAILED[*]}")"
    [ "${#WARNED[@]}" -gt 0 ] && printf 'WARNINGS: %s\n' "$(IFS=,; echo "${WARNED[*]}")"
    exit 2
elif [ "${#WARNED[@]}" -gt 0 ]; then
    printf 'RESULT: WARNING\nWARNINGS: %s\n' "$(IFS=,; echo "${WARNED[*]}")"
    exit 1
else
    printf 'RESULT: PASS\n'
    exit 0
fi
