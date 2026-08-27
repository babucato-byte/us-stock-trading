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

# Defaulted ONCE, here, and every later use reads these -- never the raw
# names. `set -u` is deliberate and stays on, but `${DEPLOYED_COMMIT:0:8}`
# carries no default: substring expansion of an unset variable is an
# unbound-variable error, so the mismatch branch killed the script before
# it printed RESULT or a single reason code. That is the one branch whose
# entire job is to report the mismatch, and an operator reading a
# two-line header saw no verdict at all rather than COMMIT_MISMATCH.
# Blank stays blank in the message -- `<unset>` would be a claim the
# variable was checked and found empty, which is a different fact.
DEPLOYED_SAFE="${DEPLOYED_COMMIT:-}"
VALIDATED_SAFE="${VALIDATED_COMMIT:-}"

if [ "${HEAD_SHA}" = "unknown" ]; then
    fail COMMIT_UNREADABLE
elif [ "${HEAD_SHA}" = "${DEPLOYED_SAFE}" ] && [ "${HEAD_SHA}" = "${VALIDATED_SAFE}" ]; then
    pass "commit HEAD==DEPLOYED==VALIDATED (${HEAD_SHA:0:8})"
else
    fail COMMIT_MISMATCH \
        "HEAD=${HEAD_SHA:0:8} DEPLOYED=${DEPLOYED_SAFE:0:8} VALIDATED=${VALIDATED_SAFE:0:8}"
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
# Per-ORDER quantity is no longer pinned at 1.
#
# It was the last LIMITED_LIVE count. With it set, `min(affordable, cap)`
# made every order one share regardless of cash, so variable sizing could
# never take effect. What bounds a single mistake is unchanged and is not
# this number: whole-share-only (fractional off), no margin, no leverage,
# and orderable cash. A ceiling an operator DOES set is still honoured
# and sanity-checked below; requiring the value 1 is a test posture.

# The three COUNT caps were LIMITED_LIVE scaffolding -- 1 position per
# strategy, 1 entry per day, 1 or 2 overall -- so the first real orders
# could be counted by hand. That test is over, and requiring those
# numbers here would make its shape permanent.
#
# Unset is now the expected posture and is reported, not failed. A cap
# an operator DOES set is still checked for sanity, because a malformed
# one must never read as "no cap".
for _cap in LIVE_ROLLOUT_MAX_POSITIONS LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY \
            LIVE_ROLLOUT_MAX_DAILY_ENTRIES LIVE_ROLLOUT_MAX_QUANTITY; do
    _value="$(eval "printf '%s' \"\${${_cap}:-}\"")"
    if [ -z "${_value}" ]; then
        info "${_cap}=<unset> (capacity bounded by cash, the per-symbol lock, same-day re-entry, ownership and reconciliation)"
    elif printf '%s' "${_value}" | grep -Eq '^[1-9][0-9]*$'; then
        pass "${_cap}=${_value}"
    else
        fail "${_cap}_INVALID" "got '${_value}'"
    fi
done

GLOBAL_MAX="${LIVE_ROLLOUT_MAX_POSITIONS:-}"
PER_STRATEGY_MAX="${LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY:-}"
# Enforced here as well as in config.validate(), because this checker is
# read by an operator deciding whether to place a real order and must
# not require them to trust that a second file agrees.
# Two caps that are both absent cannot contradict each other -- the
# contradiction is only meaningful when an operator has set BOTH, which
# is exactly when config.validate() refuses it too.
if [ -z "${GLOBAL_MAX}" ] || [ -z "${PER_STRATEGY_MAX}" ]; then
    info "per-strategy/global cap comparison skipped (at least one is unset)"
elif [ "${PER_STRATEGY_MAX}" -le "${GLOBAL_MAX}" ] 2>/dev/null; then
    pass "per-strategy cap (${PER_STRATEGY_MAX}) <= global cap (${GLOBAL_MAX})"
else
    fail PER_STRATEGY_CAP_EXCEEDS_GLOBAL \
        "per_strategy=${PER_STRATEGY_MAX} global=${GLOBAL_MAX}"
fi

# ABSENT, SET-BUT-EMPTY and SET are three different instructions, read
# here exactly as config/live_rollout_config.py reads them.
#
# "Exactly one symbol" was how LIMITED_LIVE pinned trading to a
# hand-picked ticker -- and it stayed pinned to DT long after that test
# ended, filtering out every READY candidate before any gate ran. In
# NORMAL LIVE the BUY target is decided by which candidates reach
# READY_TO_BUY and clear the execution gate, so an ABSENT list is the
# expected posture: reported, not failed.
#
# SET BUT EMPTY is a block. It denies every symbol, so live trading
# would be enabled and simultaneously unable to trade -- and that is
# also what a truncated or half-written env file looks like.
ALLOWLIST="${LIVE_ROLLOUT_ALLOWED_SYMBOLS:-}"
ALLOW_COUNT=0
if [ -n "${ALLOWLIST}" ]; then
    ALLOW_COUNT="$(printf '%s' "${ALLOWLIST}" | tr ',' '\n' | grep -c '[^[:space:]]')"
fi
if [ -z "${LIVE_ROLLOUT_ALLOWED_SYMBOLS+set}" ]; then
    info "LIVE_ROLLOUT_ALLOWED_SYMBOLS=<unset> (no operator symbol restriction; candidates decide)"
elif [ "${ALLOW_COUNT}" -eq 0 ]; then
    fail LIVE_ALLOWLIST_EMPTY \
        "LIVE_ROLLOUT_ALLOWED_SYMBOLS is set but empty, which denies every symbol"
else
    pass "live allow-list restricts to ${ALLOW_COUNT} symbol(s)"
fi

if [ -n "${SLACK_WEBHOOK_URL:-}" ] && [ -n "${SLACK_ALERT_WEBHOOK_URL:-}" ]; then
    pass "Slack webhooks configured (general + alert)"
else
    fail SLACK_WEBHOOK_UNCONFIGURED
fi

# KIS live has its own two webhooks and never falls back to the Alpaca
# pair above. Both must be set: without them a real order would be
# placed with its entire lifecycle -- including an UNKNOWN -- going
# nowhere. Only presence is checked; the URLs are never printed.
if [ -n "${KIS_LIVE_SLACK_WEBHOOK_URL:-}" ] && [ -n "${KIS_LIVE_SLACK_ALERT_WEBHOOK_URL:-}" ]; then
    pass "KIS live Slack webhooks configured (general + alert)"
else
    fail KIS_LIVE_NOTIFICATION_NOT_CONFIGURED
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
        REQUIRED_FOR_ARMED, REQUIRED_FOR_DAYTIME, REQUIRED_FOR_OBSERVE, KISBroker,
        matrix_entries_for, pending_items_for,
    )
    from config import s6_sessions, session_capability, strategy_entry_policy
    from config import strategy_registry
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
check("OBSERVE_MATRIX_PENDING", not observe_pending,
      f"{len(matrix_entries_for(REQUIRED_FOR_OBSERVE)) - len(observe_pending)}"
      f"/{len(matrix_entries_for(REQUIRED_FOR_OBSERVE))} confirmed")

# The evidence set is the one belonging to the session about to be
# traded, not the regular set in every session.
#
# REGULAR and OVERNIGHT_DAYTIME are separate endpoints with separate TR
# families, so their five live-only values are separate sets and
# confirming one says nothing about the other. Judging a daytime order
# against the REGULAR set blocked daytime on evidence daytime can never
# produce: the regular five are observable only by placing a REGULAR
# order, which is not the order being placed. The strategy is one S6
# either way; the ROUTE is what has or has not been confirmed.
CAPABILITY = session_capability.current_capability(
    strategy_id=s6_sessions.STRATEGY_ID)
IS_DAYTIME = CAPABILITY.session == "OVERNIGHT_DAYTIME"
SESSION_POSTURE = REQUIRED_FOR_DAYTIME if IS_DAYTIME else REQUIRED_FOR_ARMED
LIVE_BOOTSTRAP_REQUIRED = {
    "daytime_order_path", "daytime_order_tr_id_live_buy",
    "daytime_order_tr_id_live_sell", "daytime_cancel_path",
    "daytime_cancel_tr_id_live",
} if IS_DAYTIME else {
    "order_path", "order_tr_id_live_buy", "cancel_path",
    "cancel_tr_id_live", "cancel_price_field_rule",
}
session_pending = list(pending_items_for(SESSION_POSTURE))
# While exactly those are outstanding the deployment is bootstrap-
# eligible but not generally live-ready; anything else is a plain block.
beyond_bootstrap = [n for n in session_pending if n not in LIVE_BOOTSTRAP_REQUIRED]
check("SESSION_MATRIX_PENDING", not session_pending,
      f"[{CAPABILITY.session or 'UNKNOWN'}] pending: "
      f"{', '.join(session_pending) if session_pending else 'none'}")
check("SESSION_PENDING_BEYOND_BOOTSTRAP", not beyond_bootstrap,
      f"beyond the live-bootstrap five: "
      f"{', '.join(beyond_bootstrap) if beyond_bootstrap else 'none'}")
# The OTHER route's evidence: reported, never blocking. An operator
# placing a daytime order still wants to see that regular is unconfirmed;
# it is just not a reason to refuse THIS order.
other_posture = REQUIRED_FOR_ARMED if IS_DAYTIME else REQUIRED_FOR_DAYTIME
other_pending = list(pending_items_for(other_posture))
print(f"INFO::OTHER_ROUTE_PENDING::{other_posture}: {len(other_pending)} pending")
print(f"BOOTSTRAPABLE::{'yes' if session_pending and not beyond_bootstrap else 'no'}")

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
    # Not "is it REGULAR" -- "can THIS order be routed right now".
    #
    # The old question was a stand-in that stopped agreeing with the real
    # one once S6 went live in OVERNIGHT_DAYTIME. Worse, it consulted
    # `get_us_market_session()`, which reports the US venue's own state
    # and is "closed" for the whole daytime window by construction -- so
    # the readiness checker refused the daytime route in every session
    # that could reach it. Both entry AND exit capability are required: a
    # session an order can be placed into but not left is not one to
    # trade in.
    session = CAPABILITY.session or get_us_market_session()
    check("NO_ENTRY_ROUTE_FOR_SESSION", CAPABILITY.entry_supported,
          f"session={session} reason={CAPABILITY.entry_reason}")
    check("NO_EXIT_ROUTE_FOR_SESSION", CAPABILITY.exit_supported,
          f"session={session} reason={CAPABILITY.exit_reason}")
except Exception as exc:  # noqa: BLE001
    check("NO_ENTRY_ROUTE_FOR_SESSION", False, type(exc).__name__)

# -- posture
# Two different questions. PRE_LIVE_READY means the general live path may
# run, which requires ARMED. READY_FOR_LIVE_BOOTSTRAP means the one-shot
# may run, which requires LIMITED_LIVE_BOOTSTRAP. Reporting the second as
# "not ARMED" made the bootstrap state unreachable by construction, since
# reaching ARMED is exactly what the bootstrap exists to make possible.
decision = resolve_posture()
check("POSTURE_NOT_ARMED", decision.posture == "ARMED", f"posture={decision.posture}")
check("INVALID_BOOTSTRAP_POSTURE",
      decision.posture in ("ARMED", "LIMITED_LIVE_BOOTSTRAP"),
      f"posture={decision.posture}")

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
    # NOT "zero positions anywhere" any more. That was the right question
    # while one strategy was live under one global slot; with S1 and S6
    # both live it made the S6 bootstrap unreachable for as long as S1
    # held anything, which is a scheduling accident rather than a safety
    # property. What matters is that the account has ROOM -- the
    # per-strategy check below is what stops a strategy doubling up.
    if rollout.max_open_positions is None:
        # No count cap: how many names the account holds is a fact, not
        # a refusal. Cash and the per-symbol lock decide capacity now.
        print(f"INFO::ACCOUNT_POSITIONS::{len(positions)} held (uncapped)")
    else:
        check("ACCOUNT_AT_POSITION_CAP",
              len(positions) < rollout.max_open_positions,
              f"open positions={len(positions)}/{rollout.max_open_positions}")

    open_orders = broker.get_open_orders()
    check("OPEN_ORDERS_NOT_ZERO", len(open_orders) == 0, f"open orders={len(open_orders)}")

    raw_allowlist = os.environ.get("LIVE_ROLLOUT_ALLOWED_SYMBOLS")
    symbols = [s.strip().upper() for s in (raw_allowlist or "").split(",")
               if s.strip()]
    if symbols:
        # An operator-set list is small by nature, so every symbol on it
        # is priced rather than only the first -- a list whose second
        # name is unaffordable is worth knowing before going live.
        from market_data.exchange_registry import build_kis_instrument

        affordable = []
        for symbol in symbols:
            instrument, _record = build_kis_instrument(symbol)
            price = broker.get_current_price(instrument)
            orderable = broker.get_orderable_usd(instrument, price)
            affordable.append((symbol, orderable, price, orderable >= price))
        detail = " ".join(f"{sym}:orderable={cash}/price={px}"
                          for sym, cash, px, _ok in affordable)
        # ANY affordable name is enough: the entry evaluates candidates
        # in rank order and skips the ones it cannot afford, so a single
        # expensive name on the list is not a blocker.
        check("INSUFFICIENT_ORDERABLE_CASH",
              any(ok for _s, _c, _p, ok in affordable), detail)
    elif raw_allowlist is None:
        # NORMAL LIVE. There is no pre-approved symbol to price, and
        # there is nothing missing either: orderable cash is read per
        # candidate at the moment of entry, against the exact limit
        # price the order will carry. A single account-level figure was
        # never the question -- the same $20.96 buys one share of an
        # $11 name and none of a $40 one.
        print("INFO::ORDERABLE_CASH_PER_CANDIDATE::"
              "no operator allow-list; orderable cash is read per candidate at entry")
    else:
        # Set but empty: the allow-list denies every symbol, so there is
        # genuinely nothing this account could buy. That is reported by
        # the shell check above; nothing was priced, and claiming a
        # shortfall here would be an invented finding on top of it.
        check("ORDERABLE_CASH_NOT_EVALUATED", False,
              "the allow-list is set but empty, so no candidate could be priced")

    conn = state_db.open_db()
    try:
        limits = entry_limits.collect(broker=broker, conn=conn, rollout=rollout)
    finally:
        conn.close()
    # Both of these were LIMITED_LIVE counters. With the caps unset they
    # are reported and never block: an entry is refused now because the
    # strategy already holds THAT symbol or sold it today, not because
    # some number of unrelated positions is open.
    if limits.max_daily_entries is None:
        print(f"INFO::DAILY_ENTRIES::{limits.daily_entry_count} today (uncapped)")
    else:
        check("DAILY_ENTRIES_ALREADY_USED",
              limits.daily_entry_count < limits.max_daily_entries,
              f"daily entries={limits.daily_entry_count}/{limits.max_daily_entries}")
    if limits.max_open_positions is None:
        print(f"INFO::POSITION_SLOTS::{limits.effective_position_count} "
              f"effective (uncapped)")
    else:
        check("POSITION_SLOTS_ALREADY_USED",
              limits.effective_position_count < limits.max_open_positions,
              f"effective positions={limits.effective_position_count}/"
              f"{limits.max_open_positions}")
    # What DOES bar a candidate now, named so an operator can see it.
    _blocked_today = sorted(limits.same_day_exits.get(
        strategy_registry.slot_for(s6_sessions.STRATEGY_ID)) or {})
    print(f"INFO::SAME_DAY_REENTRY_BLOCKED::{_blocked_today or 'none'}")

    # Per-strategy occupancy, reported for EVERY live strategy rather
    # than only the one about to trade. An operator reading this is
    # deciding whether to place a real order, and "S1 1/1, S6 0/1" is
    # the fact that decision turns on.
    # Only the slot of the strategy ABOUT TO TRADE can block. The others
    # are still reported, because "S1 1/1, S6 0/1" is the fact an
    # operator's decision turns on -- but a full S1 is not a reason to
    # refuse an S6 entry. Per-strategy caps exist precisely so one
    # strategy holding a position does not consume another's capacity;
    # blocking on every slot re-created the single global cap that the
    # per-strategy table was introduced to replace.
    trading_slot = strategy_registry.slot_for(s6_sessions.STRATEGY_ID)
    _cap = limits.max_positions_per_strategy
    for slot in strategy_registry.LIVE_SLOTS:
        used = limits.strategy_effective_count(slot)
        detail = (f"{slot}={used}/{_cap if _cap is not None else 'uncapped'} "
                  f"{sorted(limits.strategy_symbols_for(slot))}")
        if slot == trading_slot and _cap is not None:
            check(f"STRATEGY_SLOT_FULL_{slot}", used < _cap, detail)
        else:
            # With no count cap, "how many does this slot hold" is a
            # fact worth printing and not a reason to refuse: what bars
            # a candidate now is holding THAT symbol, or having sold it
            # today -- both of which the gate checks per candidate and
            # neither of which this slot count can express.
            print(f"INFO::STRATEGY_SLOT_{slot}::{detail}")
    # Capacity and permission are different questions: a strategy can
    # have a free slot and still be stood down for new entries.
    _entry_ok = strategy_entry_policy.entry_enabled(s6_sessions.STRATEGY_ID)
    check("STRATEGY_ENTRY_DISABLED", _entry_ok,
          f"{s6_sessions.STRATEGY_ID} entry="
          f"{'ENABLED' if _entry_ok else 'DISABLED'} "
          f"disabled_slots={list(strategy_entry_policy.entry_disabled_slots())}")
    # A held or in-flight symbol nobody can attribute counts against
    # EVERY strategy (execution/entry_limits.py), so it silently makes
    # all of the above look full. Naming it separately is the difference
    # between "wait for an exit" and "run reconciliation".
    check("UNATTRIBUTED_POSITIONS", not limits.unattributed_symbols,
          f"unattributed={sorted(limits.unattributed_symbols)}")
except Exception as exc:  # noqa: BLE001
    # The message, not just the class. An operator reading "TypeError"
    # learns nothing about which read failed or why.
    import traceback
    _where = traceback.extract_tb(exc.__traceback__)[-1]
    check("KIS_ACCOUNT_READ_FAILED", False,
          f"{type(exc).__name__}: {exc} (at line {_where.lineno}: {_where.line})")

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
            INFO::*)
                # Reported, never judged. These carry facts an operator's
                # decision turns on -- the other route's evidence, the
                # occupancy of slots that are not the one about to trade
                # -- which are deliberately NOT verdicts. They are listed
                # explicitly rather than by falling through, so the
                # catch-all below keeps meaning "a check whose verdict is
                # unknown", which is still not ready.
                printf '  [INFO] %s\n' "${line#INFO::}"
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

# READY_FOR_LIVE_BOOTSTRAP is NOT a weaker PRE_LIVE_READY. It authorises
# exactly one symbol, one share, one BUY attempt and at most one CANCEL
# attempt -- the only way the five live-only wire values can be observed
# at all. Every OTHER safety check must still have passed, which is why
# it requires the reason list to contain nothing but SESSION_MATRIX_PENDING.
BOOTSTRAPABLE="$(printf '%s\n' "${PY_OUTPUT}" | sed -n 's/^BOOTSTRAPABLE:://p' | tail -1)"
BOOTSTRAP_TOLERATED=0
for reason in "${REASONS[@]-}"; do
    case "${reason}" in
        SESSION_MATRIX_PENDING|POSTURE_NOT_ARMED) BOOTSTRAP_TOLERATED=$((BOOTSTRAP_TOLERATED+1)) ;;
    esac
done
if [ "${BOOTSTRAPABLE}" = "yes" ] \
   && [ "${BOOTSTRAP_TOLERATED}" -eq "${#REASONS[@]}" ]; then
    printf 'RESULT: READY_FOR_LIVE_BOOTSTRAP\n'
    printf 'Every safety check passed. The only outstanding items are the five\n'
    printf 'wire values that a real live response is the only way to confirm\n'
    printf 'for the session being traded -- see SESSION_MATRIX_PENDING above.\n'
    printf 'Authorised scope: 1 symbol, 1 share, 1 BUY, at most 1 CANCEL.\n'
    printf 'This is NOT approval for ARMED or AUTO LIVE.\n'
    exit 0
fi

printf 'RESULT: PRE_LIVE_BLOCKED\n'
printf 'BLOCKING REASON CODES (%s):\n' "${#REASONS[@]}"
printf '  - %s\n' "${REASONS[@]}"
exit 1
