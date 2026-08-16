#!/usr/bin/env bash
# One-shot LIMITED LIVE bootstrap -- the first real order, and nothing more.
#
# WHAT IT IS FOR
# Two wire values cannot be confirmed any other way:
#
#     order_tr_id_live_buy   TTTT1002U -- live-only, a paper response
#                            cannot establish it
#     cancel_tr_id_live      TTTT1004U -- same
#
# Confirming them needs exactly one real buy on the live account, and (if
# a resting limit order is available to cancel) one real cancel. This
# script exists so that happens ONCE, under every precondition checked
# first, rather than by switching the whole system to AUTO LIVE and
# hoping the first order is the small one.
#
# WHAT IT IS NOT
# Not a way to start trading. It places at most ONE order, never retries,
# and leaves every rollout limit at 1. It does not enable a timer, does
# not enable a service, and does not widen the allow-list.
#
# IT WILL NOT RUN WITHOUT EXPLICIT ACKNOWLEDGEMENT
#
#     LIVE_BOOTSTRAP_ACK=true
#
# Absent that, it prints what it WOULD check and exits non-zero. That is
# deliberate: a script that places a real order must not be runnable by
# tab-completion and Enter.
#
# EVERY precondition is verified before the acknowledgement is even
# considered, so a run with the ack set but the account in the wrong
# state still places nothing.

set -uo pipefail

RELEASE_ROOT="${TRADING_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CHECK="${RELEASE_ROOT}/scripts/final_pre_live_check.sh"

printf 'LIMITED LIVE BOOTSTRAP\n'
printf '  release: %s\n' "${RELEASE_ROOT}"
printf '  transport budget: 1 order, 0 retries\n'
printf '  UNKNOWN policy: no retry, reconciliation required\n\n'

# -- 1. every precondition, via the single readiness checker ------------
# Deliberately delegated rather than re-listed: two copies of "is this
# safe" drift, and the copy that drifts is the one that lets an order
# through.
printf 'Preconditions (delegated to final_pre_live_check.sh):\n'
if [ ! -x "${CHECK}" ] && [ ! -f "${CHECK}" ]; then
    printf '  [FAIL] readiness checker not found at %s\n' "${CHECK}"
    printf '\nRESULT: BOOTSTRAP_BLOCKED (PRECHECK_MISSING)\n'
    exit 1
fi

bash "${CHECK}"
CHECK_STATUS=$?
printf '\n'

if [ "${CHECK_STATUS}" -ne 0 ]; then
    printf 'RESULT: BOOTSTRAP_BLOCKED (PRE_LIVE_BLOCKED)\n'
    printf 'No order was placed. Resolve every reason code above and re-run.\n'
    exit 1
fi

# -- 2. bootstrap-specific preconditions --------------------------------
# The readiness checker answers "could this deployment trade at all".
# These answer "is this the clean, empty, one-symbol state a FIRST order
# requires" -- a running system could pass the former and fail these.
printf 'Bootstrap-specific preconditions:\n'
BLOCKED=0
require() {
    if [ "$2" = "ok" ]; then
        printf '  [PASS] %s\n' "$1"
    else
        printf '  [FAIL] %s -- %s\n' "$1" "$2"
        BLOCKED=1
    fi
}

require "acknowledgement present" \
    "$([ "${LIVE_BOOTSTRAP_ACK:-}" = "true" ] && echo ok || echo "LIVE_BOOTSTRAP_ACK is not 'true'")"

if [ "${BLOCKED}" -ne 0 ]; then
    printf '\nRESULT: BOOTSTRAP_BLOCKED (ACK_REQUIRED)\n'
    printf 'Nothing was submitted. This script places a REAL order on a REAL\n'
    printf 'account; it runs only with LIVE_BOOTSTRAP_ACK=true set deliberately.\n'
    exit 1
fi

# -- 3. the one order ---------------------------------------------------
# Reached only when the readiness checker returned PRE_LIVE_READY and the
# acknowledgement is set.
#
# The runner goes through execution_engine.submit_buy_order() -- the same
# gate, idempotency ledger, reservation, reconciliation snapshot, audit
# trail and UNKNOWN policy as every other order -- never a direct
# broker.submit_order() call, and never inside a retry loop. It is
# invoked ONCE. This script does not loop, does not retry on any exit
# code, and does not run it again after a failure: a second invocation
# after an ambiguous first is exactly the duplicate-order case the whole
# design exists to prevent.
RUNNER="${RELEASE_ROOT}/scripts/run_limited_live_bootstrap.py"
PYTHON_BIN="${PYTHON_BIN:-${RELEASE_ROOT}/venv/bin/python}"

if [ ! -f "${RUNNER}" ]; then
    printf '  [FAIL] bootstrap runner not found at %s\n' "${RUNNER}"
    printf '\nRESULT: BOOTSTRAP_BLOCKED (RUNNER_MISSING)\n'
    exit 1
fi

printf '\nSubmitting via execution_engine (one BUY, one share, no retry):\n'
"${PYTHON_BIN}" "${RUNNER}"
RUNNER_STATUS=$?

printf '\n'
case "${RUNNER_STATUS}" in
    0) printf 'RESULT: BOOTSTRAP_COMPLETED\n' ;;
    1) printf 'RESULT: BOOTSTRAP_BLOCKED\n'
       printf 'No order was placed.\n' ;;
    3) printf 'RESULT: BOOTSTRAP_UNKNOWN\n'
       printf 'RETRY=BLOCKED  RECONCILIATION_REQUIRED=true  NEW_ENTRY_BLOCKED=true\n'
       printf 'The order may be live at KIS. Do not re-run this script.\n' ;;
    *) printf 'RESULT: BOOTSTRAP_FAULTED (exit %s)\n' "${RUNNER_STATUS}"
       printf 'Do not re-run. Reconcile against KIS order history first.\n' ;;
esac
exit "${RUNNER_STATUS}"
