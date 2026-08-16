#!/usr/bin/env python3
"""Print the S1 live-readiness matrix and the highest permitted stage.

    scripts/run_s1_readiness.py
    scripts/run_s1_readiness.py --json

Read-only and offline by default: it reports what the durable state and
the configuration say, and does not contact the broker. `--live` adds a
read-only account read so the cash/equity rows reflect the real account.

It cannot enable anything. The rollout flags are printed, never written.

Exit codes
    0  the matrix rendered
    1  it could not be built
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import s1_rollout_stages as stages  # noqa: E402
from s1_live import readiness  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trading-day", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    from config.live_rollout_config import LiveRolloutConfig
    from scanners import candidate_decision
    from scanners.base.trading_calendar import us_trading_day
    from s1_live import risk_state as risk_state_module
    from state_store import db

    day = args.trading_day or us_trading_day()
    conn = db.open_db()
    try:
        state = risk_state_module.current_state(conn, day)
        matrix = readiness.build_matrix(
            conn=conn, risk_state=state, equity_snapshot=None,
            candidate_decision_enabled=candidate_decision.is_enabled(),
            candidate_source_ok=False, kill_switch_healthy=None,
            reconciliation_healthy=None, minimum_order_verified=False,
            exit_policy_defined=False, fees_reported=False)
    finally:
        conn.close()

    rollout = LiveRolloutConfig.from_env()
    if args.json:
        payload = matrix.as_dict()
        payload["actual_rollout"] = {
            "enabled": rollout.enabled,
            "max_quantity_per_order": rollout.max_quantity_per_order,
            "max_open_positions": rollout.max_open_positions,
            "max_daily_entries": rollout.max_daily_entries,
        }
        payload["planned_profiles"] = {
            stage: stages.profile_for(stage) for stage in stages.STAGE_ORDER}
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0

    print(readiness.format_matrix(matrix))
    print("")
    print(f"  ACTUAL rollout: enabled={rollout.enabled} "
          f"qty={rollout.max_quantity_per_order} "
          f"positions={rollout.max_open_positions} "
          f"daily={rollout.max_daily_entries}")
    print("  (planned stage profiles are simulation only; nothing here writes a flag)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
