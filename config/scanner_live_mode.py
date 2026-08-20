"""Which scanner, if any, may act as a live candidate source.

There is exactly one live-eligible scanner and it is named here, not
inferred. Every other scanner is DISCOVERY_ONLY: its signals are recorded
for the month-1 analysis and are structurally unable to reach an order.

Why a table rather than a flag on each scanner
----------------------------------------------
A per-scanner flag makes "how many scanners are live right now?" a
question you answer by reading six files. Here it is one dict, and
`limited_live_scanner()` refuses unless the answer is exactly one.

That refusal is the important part. The dangerous failure is not "no
scanner is live" -- that is a quiet, safe no-op. It is "two scanners are
live and nobody noticed", because the second one arrived through a
config edit that looked local. A count of anything other than 1 raises,
and every caller treats the raise as an empty candidate set.

Changing which scanner is LIMITED_LIVE is a reviewed decision that also
requires month-1 evidence for the new one. It is deliberately not an
environment variable: an env var is edited on a server at 2am, and this
is not a setting that should ever be changed that way.
"""

MODE_LIMITED_LIVE = "LIMITED_LIVE"
MODE_DISCOVERY_ONLY = "DISCOVERY_ONLY"

VALID_MODES = frozenset({MODE_LIMITED_LIVE, MODE_DISCOVERY_ONLY})

#: Every scanner in `scanners/registry.py` must appear here.
SCANNER_LIVE_MODE = {
    "hma_early_trend": MODE_LIMITED_LIVE,
    "accumulation": MODE_DISCOVERY_ONLY,
    "breakout_ready": MODE_DISCOVERY_ONLY,
    "premarket_momentum": MODE_DISCOVERY_ONLY,
    "gap_pullback": MODE_DISCOVERY_ONLY,
    "orb": MODE_DISCOVERY_ONLY,
}


class ScannerLiveModeError(Exception):
    """The live-mode configuration could not be validated.

    Callers must treat this as "no scanner is live", never as "use the
    first one you found".
    """


def _limited_live_names(modes=None):
    table = SCANNER_LIVE_MODE if modes is None else modes
    if not isinstance(table, dict) or not table:
        raise ScannerLiveModeError(f"scanner live-mode table is not a non-empty dict: {table!r}")
    for name, mode in table.items():
        if mode not in VALID_MODES:
            raise ScannerLiveModeError(
                f"scanner {name!r} has an unknown live mode {mode!r}; "
                f"valid modes are {sorted(VALID_MODES)}")
    return sorted(name for name, mode in table.items() if mode == MODE_LIMITED_LIVE)


def limited_live_scanner(modes=None) -> str:
    """The single LIMITED_LIVE scanner name, or raise.

    Raises on zero as well as on two. Zero is safe but it is also not a
    state the publisher should silently run in -- a publisher that finds
    no live scanner and writes an empty candidate file is
    indistinguishable from one that ran on a quiet day, and those two
    need different operator responses.
    """
    names = _limited_live_names(modes)
    if len(names) != 1:
        raise ScannerLiveModeError(
            f"exactly one scanner may be {MODE_LIMITED_LIVE}; found {len(names)}: "
            f"{names or '(none)'}")
    return names[0]


def is_limited_live(scanner_name, modes=None) -> bool:
    """Fail-closed membership test. Any configuration problem is False."""
    try:
        return scanner_name == limited_live_scanner(modes)
    except ScannerLiveModeError:
        return False


def discovery_only_scanners(modes=None):
    table = SCANNER_LIVE_MODE if modes is None else modes
    return sorted(name for name, mode in table.items() if mode == MODE_DISCOVERY_ONLY)
