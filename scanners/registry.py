"""The six scanners, and how to name one on a command line.

Construction is lazy and per-request. A scanner instance reads its
config at construction time, so building them all at import would freeze
month one's parameters at whenever the module happened to be imported
and would make a config edit invisible until the process restarted.

Import isolation
----------------
`build_scanners` imports each scanner inside its own try/except. A
scanner whose module fails to import -- a typo, a missing config, a bad
parameter type -- is reported and skipped, and the other five still run.
Section 5's isolation requirement starts at import, not at evaluation:
a module-level failure with no guard here would take down the whole
scan process before any scanner had evaluated anything.
"""

import logging
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: name -> (module path, class name). Strings rather than imported
#: classes so that importing this registry cannot fail because one
#: scanner module is broken.
SCANNER_SPECS: Dict[str, Tuple[str, str]] = {
    "hma_early_trend": ("scanners.hma_early_trend.scanner", "HmaEarlyTrendScanner"),
    "accumulation": ("scanners.accumulation.scanner", "AccumulationScanner"),
    "breakout_ready": ("scanners.breakout_ready.scanner", "BreakoutReadyScanner"),
    "premarket_momentum": ("scanners.premarket_momentum.scanner", "PremarketMomentumScanner"),
    "gap_pullback": ("scanners.gap_pullback.scanner", "GapPullbackScanner"),
    "orb": ("scanners.orb.scanner", "OpeningRangeBreakoutScanner"),
}

#: Scanners that only need daily bars. Useful for an end-of-day run that
#: should not pay for minute data it will not read.
DAILY_SCANNERS = ("hma_early_trend", "accumulation", "breakout_ready")

#: Scanners that read intraday bars and are therefore session-sensitive.
INTRADAY_SCANNERS = ("premarket_momentum", "gap_pullback", "orb")

ALL_SCANNERS = tuple(SCANNER_SPECS)


def scanner_names() -> List[str]:
    return list(SCANNER_SPECS)


def load_scanner_class(name: str):
    if name not in SCANNER_SPECS:
        raise KeyError(f"unknown scanner {name!r}; known: {', '.join(SCANNER_SPECS)}")
    module_path, class_name = SCANNER_SPECS[name]
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


def build_scanner(name: str):
    return load_scanner_class(name)()


def build_scanners(
    names: Optional[List[str]] = None,
    *,
    on_error: Optional[Callable[[str, Exception], None]] = None,
) -> List:
    """Instantiate the requested scanners, skipping any that will not build.

    Returns only the scanners that constructed successfully. The caller
    is told about the failures through `on_error` and reports them; it
    does not get a partially-constructed scanner that would fail later
    in a less obvious place.
    """
    built = []
    for name in (names or scanner_names()):
        try:
            built.append(build_scanner(name))
        except Exception as exc:  # noqa: BLE001 - one bad scanner must not stop the rest
            logger.exception("scanner %s could not be constructed", name)
            if on_error is not None:
                on_error(name, exc)
    return built
