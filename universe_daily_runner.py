"""Daily universe refresh.

Three steps, in order (T8):

1. Refresh `universe.csv` -- the full tradable-asset listing. Still run as
   a subprocess of `universe_builder.py`, exactly as before, so a failure
   here keeps the previous listing on disk instead of truncating it.
2. Refresh the account budget from KIS (`universe_budget.refresh_budget`).
   A failed read keeps the previously persisted figure; nothing is
   fabricated.
3. Rebuild `universe_tradable.csv` -- the entry-side pool the account can
   actually afford -- plus the per-symbol decision log and JSON report.

Step 2 is the only step that touches a real account, and it is reachable
standalone via `scripts/refresh_universe_budget.py`.
"""

import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from config.paths import get_project_root

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

BASE_DIR = str(get_project_root())
PYTHON = f"{BASE_DIR}/venv/bin/python"
LOG_DIR = f"{BASE_DIR}/logs"
RUNNER_LOG = f"{LOG_DIR}/universe_daily_runner.log"


def log_run_header(now_kst=None, now_ny=None, log_path=RUNNER_LOG):
    now_kst = now_kst or datetime.now(KST)
    now_ny = now_ny or datetime.now(NY)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as handle:
        handle.write(f"{now_kst} | NY={now_ny} | universe refresh\n")


def refresh_full_listing(runner=subprocess.run):
    """Step 1 -- unchanged behaviour: shell out to universe_builder.py."""
    return runner([PYTHON, f"{BASE_DIR}/universe_builder.py"], cwd=BASE_DIR)


class LazyKISBroker:
    """Defers KISBroker construction until the balance read actually
    happens, so a missing credential / disabled-read config surfaces
    through `refresh_budget()`'s "keep the previous value" path instead of
    crashing the runner before it can log anything."""

    def __init__(self, factory=None):
        self._factory = factory

    def get_account_snapshot(self):
        if self._factory is not None:
            broker = self._factory()
        else:
            from brokers.kis_broker import KISBroker

            broker = KISBroker()
        return broker.get_account_snapshot()


def refresh_account_budget(broker=None, *, state_path=None, logger=print):
    """Step 2 -- KIS balance read with keep-previous-on-failure."""
    from universe_budget import refresh_budget

    return refresh_budget(broker or LazyKISBroker(), path=state_path, logger=logger)


def rebuild_tradable_universe(state, *, metrics_provider=None, logger=print, **kwargs):
    """Step 3 -- filter the listing down to what the account can buy."""
    import universe_builder
    from universe_metrics import YFinanceUniverseMetricsProvider

    rows = universe_builder.load_universe_rows(
        kwargs.pop("universe_path", universe_builder.UNIVERSE_LISTING_PATH)
    )
    logger(f"[UNIVERSE FILTER] listing rows={len(rows)}")
    provider = metrics_provider or YFinanceUniverseMetricsProvider(logger=logger)
    return universe_builder.build_tradable_universe(
        rows,
        provider,
        state.to_budget() if state is not None else None,
        budget_stale=bool(state is not None and state.stale),
        logger=logger,
        **kwargs,
    )


def main(argv=None, *, logger=print):
    log_run_header()
    refresh_full_listing()

    state, error = refresh_account_budget(logger=logger)
    if state is None:
        logger(
            "[UNIVERSE FILTER] aborting filtered-universe rebuild: no account budget available "
            f"({error}). universe.csv was still refreshed; universe_tradable.csv is unchanged."
        )
        return 1

    from universe_builder import UniverseBuildError

    try:
        rebuild_tradable_universe(state, logger=logger)
    except UniverseBuildError as exc:
        logger(f"[UNIVERSE FILTER] aborting: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
