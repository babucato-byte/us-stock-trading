#!/usr/bin/env python3
"""Send one daytime route-verification result to stock-system-health, in Korean.

    scripts/notify_route_verification.py --market STALE --buy NOT_ATTEMPTED \
        --cancel NOT_ATTEMPTED --sell VERIFIED --route-state BLOCKED \
        --reason-code STALE_QUOTE --reason-detail "AAPL, NVDA에서 ..."

Exists so the server-native one-shot stops formatting its own English
message. It renders the machine status words through
`slack_presentation.status_label` and keeps the cause in both halves --
the Korean explanation an operator acts on, and the code they can search
for.

Read-only with respect to trading: it sends a message and returns. Exit 0
whether or not Slack accepted it, because a notifier must never fail the
job that called it.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.secret_redaction import install_logging_redaction  # noqa: E402

FIELDS = ("market", "buy", "cancel", "sell", "position", "open_orders")


def build(args) -> str:
    from operations import slack_presentation as sp

    result = {
        "session": args.session,
        "trading_day": args.trading_day,
        "mode": args.mode,
        "route_state": args.route_state,
        "reason_code": args.reason_code,
        "reason_detail": args.reason_detail,
    }
    for name in FIELDS:
        result[name] = getattr(args, name)
    if args.orders_submitted is not None:
        result["orders_submitted"] = args.orders_submitted
    return sp.route_verification(result)


def main(argv=None) -> int:
    install_logging_redaction()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default="OVERNIGHT_DAYTIME")
    parser.add_argument("--trading-day", default=None)
    parser.add_argument("--mode", default="서버 자동 검증")
    for name in FIELDS:
        parser.add_argument(f"--{name.replace('_', '-')}", default=None)
    parser.add_argument("--route-state", default=None)
    parser.add_argument("--reason-code", default=None)
    parser.add_argument("--reason-detail", default=None)
    parser.add_argument("--orders-submitted", type=int, default=None)
    parser.add_argument("--print-only", action="store_true",
                        help="render and print without sending")
    args = parser.parse_args(argv)

    message = build(args)
    if args.print_only:
        print(message)
        return 0
    try:
        import slack_utils

        sent = slack_utils.send_system_health_message(message)
        print(f"system-health: {'sent' if sent else 'not sent'}")
    except Exception as exc:  # noqa: BLE001 - a notifier never fails its caller
        print(f"system-health: not sent ({type(exc).__name__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
