#!/usr/bin/env python3
"""Send one infrastructure event to stock-system-health.

    python -m scripts.notify_system_health COLLECTOR_RESTART "HEARTBEAT_STALE last heartbeat age 240s"

For shell wrappers (the collector supervisor, the preflight) that have
something to say about the machinery rather than the market. The code is
one of `operations.slack_presentation.HEALTH_TITLES`; an unknown code is
printed as itself. Deduplicated per (code, detail, hour) through the
notification ledger when the state store is reachable, so a wrapper that
fires every five minutes cannot repeat the same fact twelve times.

Exit 0 whether or not Slack accepted the message: a notifier must never
fail the job that called it.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.secret_redaction import install_logging_redaction  # noqa: E402


def _dedupe(code, detail) -> bool:
    try:
        from operations import notification_ledger as ledger
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
            key = ledger.key_for("SYSTEM_HEALTH", subject_id=code,
                                 state_version=f"{hour}:{str(detail)[:80]}")
            return bool(ledger.claim(conn, key, event_type="SYSTEM_HEALTH",
                                     subject_id=code, state_version=hour,
                                     channel="SYSTEM_HEALTH"))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a missing ledger must not silence
        return True


def main(argv=None) -> int:
    install_logging_redaction()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("code")
    parser.add_argument("detail", nargs="?", default="")
    parser.add_argument("--no-dedupe", action="store_true")
    args = parser.parse_args(argv)
    from operations import slack_presentation as sp
    import slack_utils

    if not args.no_dedupe and not _dedupe(args.code, args.detail):
        print(f"SYSTEM_HEALTH {args.code}: already announced this hour")
        return 0
    message = sp.system_health(
        args.code, args.detail,
        release=(os.environ.get("DEPLOYED_COMMIT") or "")[:12] or None)
    print(message)
    slack_utils.send_system_health_message(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
