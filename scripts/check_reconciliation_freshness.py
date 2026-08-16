#!/usr/bin/env python3
"""Exits 0 only when the reconciliation snapshot may be relied on.

The single gate used in both places that need the answer, so the TTL,
the clock-skew tolerance and the reason codes cannot drift apart:

    scripts/enable_oracle_shadow_timer.sh      once, before arming
    us-stock-trading-shadow.service            ExecStartPre, every run

The second is the one that catches a reconciler that stops AFTER the
timer was armed -- without it, Shadow would keep evaluating against a
snapshot that quietly went stale.

Prints a one-line, redacted summary. Never a path, an account number, a
token or a raw response.

Exit codes:
    0  the snapshot is fresh, clean and usable
    1  it is not, and the reason code says why
"""

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution.secret_redaction import install_logging_redaction  # noqa: E402
from reconciliation import freshness  # noqa: E402

logger = logging.getLogger("reconciliation_freshness")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify the reconciliation snapshot is fresh and clean")
    parser.add_argument("--purpose", default="shadow",
                        help="what the check is gating; used only in the log line")
    parser.add_argument("--require-unknown-zero", action="store_true",
                        help="also require zero UNKNOWN orders")
    parser.add_argument("--require-halt-clear", action="store_true",
                        help="also require HALT to be clear")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    try:
        result = freshness.evaluate(
            require_unknown_zero=args.require_unknown_zero,
            require_halt_clear=args.require_halt_clear,
        )
    except freshness.SnapshotUnusable as exc:
        fields = " ".join(f"{k}={v}" for k, v in {
            "purpose": args.purpose,
            "reason": exc.reason_code,
            "detail": exc.detail,
            "timer_enable_suppressed": "true",
            "shadow_run_suppressed": "true",
        }.items() if v is not None)
        logger.error("reconciliation snapshot refused: %s", fields)
        print(f"RECONCILIATION CHECK FAILED: {exc.reason_code}: {exc}", file=sys.stderr)
        return 1

    fields = " ".join(f"{k}={v}" for k, v in
                      {"purpose": args.purpose, **result.as_log_fields()}.items())
    logger.info("reconciliation snapshot accepted: %s", fields)
    print(f"RECONCILIATION CHECK OK: {fields}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
