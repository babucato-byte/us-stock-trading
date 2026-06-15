import argparse

from daily_candidate_scanner import scan


def parse_args():
    parser = argparse.ArgumentParser(description="Run the daily candidate scanner.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of symbols scanned.")
    parser.add_argument("--preset", default=None, help="Scanner preset name.")
    parser.add_argument("--no-slack", action="store_true", help="Run without sending Slack alerts.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    scan(preset_name=args.preset, send_slack=not args.no_slack, scan_limit=args.limit)
