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
    # S1. Moved off LIVE on 2026-08-31, after its last live position (TX)
    # was closed and the account reconciled flat.
    #
    # It is not retired: the scanner, its executor, its exits and its
    # position lifecycle all keep running, and its signals keep being
    # recorded. What it no longer has is a route to a real order --
    # `require_limited_live("hma_early_trend")` now raises, and its
    # publisher and candidate source both treat that as "not live".
    #
    # This also restores this table's own invariant. With S1 and S6 both
    # LIMITED_LIVE, `limited_live_scanner()` raised on every call --
    # "exactly one scanner may be LIMITED_LIVE; found 2" -- and every
    # caller of it was reading that raise as an empty candidate set. Two
    # live scanners is the exact condition this table exists to make
    # impossible, and it had been true in production.
    "hma_early_trend": MODE_DISCOVERY_ONLY,
    "accumulation": MODE_DISCOVERY_ONLY,
    "breakout_ready": MODE_DISCOVERY_ONLY,
    "premarket_momentum": MODE_DISCOVERY_ONLY,
    "gap_pullback": MODE_DISCOVERY_ONLY,
    # S6. Promoted from DISCOVERY_ONLY as a reviewed decision, not a
    # config drift: the ORB scanner publishes a per-session breakout row
    # carrying its own range, and `s6_live` has the qualification,
    # position store and exit policy behind it.
    #
    # This constant does NOT mean the retired LIMITED_LIVE test posture,
    # whose one-position and one-share caps are gone. It means only that
    # this scanner may reach the live entry path at all, as against
    # DISCOVERY_ONLY. What bounds an entry now is orderable cash, the
    # per-symbol lock, same-day re-entry, ownership and reconciliation.
    #
    # Ordering is still restricted to the sessions `config/s6_sessions.py`
    # marks live -- scanning happens in every session, ordering does not,
    # and this table does not widen that.
    "orb": MODE_LIMITED_LIVE,
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


#: The two strategies cleared to place real orders, by name. Kept as
#: constants so a caller asks about the strategy it means rather than
#: about "whichever one happens to be live".
S1_SCANNER_NAME = "hma_early_trend"
S2_SCANNER_NAME = "accumulation"
S6_SCANNER_NAME = "orb"


def limited_live_scanner(modes=None) -> str:
    """The single LIMITED_LIVE scanner name, or raise.

    Answers a question that only makes sense while exactly one strategy
    is live, which is no longer the posture: S1 and S2 are both
    LIMITED_LIVE. Production code asks `require_limited_live(name)`
    instead -- a caller that needs S1's scanner should say so, not
    infer it from being the only one.

    Retained because "how many strategies are live" is still worth being
    able to assert, and because a caller that genuinely requires
    single-strategy operation should fail loudly rather than pick one.
    """
    names = _limited_live_names(modes)
    if len(names) != 1:
        raise ScannerLiveModeError(
            f"exactly one scanner may be {MODE_LIMITED_LIVE}; found {len(names)}: "
            f"{names or '(none)'}")
    return names[0]


def is_limited_live(scanner_name, modes=None) -> bool:
    """Fail-closed membership test for ONE named scanner.

    Asks about the scanner it was given, and nothing else. It used to
    delegate to `limited_live_scanner()`, which raises unless exactly one
    scanner is live -- so the day a second strategy was promoted this
    would have answered False for BOTH of them, including the one that
    was already trading. A live strategy quietly reading as not-live is
    the worst direction for this to fail in: it does not stop an order,
    it stops the checks that decide whether to place one.
    """
    try:
        return scanner_name in _limited_live_names(modes)
    except ScannerLiveModeError:
        return False


def require_limited_live(scanner_name, modes=None) -> str:
    """`scanner_name` if it is LIMITED_LIVE, else raise.

    The question a live strategy's own publisher should ask. Raising
    rather than returning False because the callers are publishers and
    candidate sources: a publisher that finds its strategy not live and
    writes an empty file is indistinguishable from one that ran on a
    quiet day, and those need different operator responses.
    """
    names = _limited_live_names(modes)
    if scanner_name not in names:
        raise ScannerLiveModeError(
            f"scanner {scanner_name!r} is not {MODE_LIMITED_LIVE}; "
            f"live scanners are {names or '(none)'}")
    return scanner_name


def discovery_only_scanners(modes=None):
    table = SCANNER_LIVE_MODE if modes is None else modes
    return sorted(name for name, mode in table.items() if mode == MODE_DISCOVERY_ONLY)
