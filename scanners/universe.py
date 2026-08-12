"""Which symbols the scanners look at.

Section 1 forbids touching the universe build, and section 3's data flow
is `Alpaca Assets -> Universe Builder -> universe.csv -> Market Data ->
Scanners`. So this module only READS `universe.csv`, the file the
existing `universe_builder.py` / `universe_daily_runner.py` already
produce. It does not build, refresh, filter or rewrite it, and no
scanner path can cause it to be regenerated.

The `tradable` column is honoured because it is already in the file: a
non-tradable asset can still be scanned and still produce a technically
valid signal, and a month of statistics that includes symbols nothing
could ever have been bought in measures something other than what it
claims to.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

import pandas as pd

from config.paths import get_project_root

#: Points at an alternative universe file. For tests and for scanning a
#: deliberately small list without editing the real one.
UNIVERSE_FILE_ENV = "SCANNER_UNIVERSE_FILE"

logger = logging.getLogger(__name__)


class UniverseUnavailable(Exception):
    """No universe file, or it has no usable symbol column."""


def universe_path() -> Path:
    override = os.environ.get(UNIVERSE_FILE_ENV)
    if override and str(override).strip():
        return Path(override)
    return Path(get_project_root()) / "universe.csv"


def load_symbols(
    *,
    limit: Optional[int] = None,
    tradable_only: bool = True,
    path: Optional[Path] = None,
) -> List[str]:
    """Symbols from `universe.csv`, in file order, deduplicated.

    File order is preserved rather than sorted so that a `limit` selects
    the same subset run to run -- a limited scan is for smoke-testing a
    deployment, and one that scanned a different slice each time would
    not tell you whether anything was working.
    """
    target = Path(path) if path is not None else universe_path()
    if not target.exists():
        raise UniverseUnavailable(f"no universe file at {target}")
    try:
        frame = pd.read_csv(target)
    except Exception as exc:  # noqa: BLE001 - pandas raises several types here
        raise UniverseUnavailable(f"universe file unreadable at {target}: {exc}") from exc

    column = next((name for name in ("symbol", "Symbol", "ticker") if name in frame.columns), None)
    if column is None:
        raise UniverseUnavailable(
            f"universe file at {target} has no symbol column (found {list(frame.columns)})")

    if tradable_only and "tradable" in frame.columns:
        flags = frame["tradable"].astype(str).str.strip().str.lower()
        frame = frame[flags.isin({"true", "1", "yes", "y"})]

    symbols: List[str] = []
    seen = set()
    for value in frame[column].dropna().astype(str):
        symbol = value.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    if limit is not None and limit > 0:
        symbols = symbols[:limit]
    logger.info("universe: %s symbols from %s", len(symbols), target)
    return symbols
