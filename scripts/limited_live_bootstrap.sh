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
# NOT IMPLEMENTED YET, ON PURPOSE. The submission step is written once
# the live-only bootstrap has an approved operating procedure and a named
# candidate symbol; wiring a real-order call ahead of that would mean an
# untested order path sitting behind one environment variable.
#
# When it is implemented it must go through execution_engine.
# submit_buy_order() -- the same gate, idempotency ledger, reconciliation
# snapshot and audit trail as every other order -- never a direct
# broker.submit_order() call, and never inside a retry loop.
printf '\nRESULT: BOOTSTRAP_NOT_IMPLEMENTED\n'
printf 'All preconditions passed and the acknowledgement is set, but the\n'
printf 'submission step is intentionally not wired yet. No order was placed.\n'
printf 'Implement it via execution_engine.submit_buy_order() only.\n'
exit 2
