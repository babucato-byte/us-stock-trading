#!/usr/bin/env python3
"""Run the independent scanners (spec sections 5 and F).

This is the operational entry point cron/systemd invokes. It is a thin
wrapper over `scanners.runner.main` -- the logic lives there so it is
importable and testable -- and exists so that scheduled invocations name
a script under `scripts/`, matching every other operational entry point
in this repository.

    scripts/run_scanners.py --profile premarket
    scripts/run_scanners.py --profile open
    scripts/run_scanners.py --profile daily
    scripts/run_scanners.py --scanners orb --limit 50

Runs nothing on a market holiday unless `--ignore-market-calendar` is
given, and never places, sizes or authorises an order.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanners.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
