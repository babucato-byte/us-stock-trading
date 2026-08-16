"""A permissive `EntryLimitState` for tests whose subject is something
other than the two rollout caps.

`BuyGateContext.entry_limits` is deliberately required, with no default:
a production caller that forgets it must fail to build a context rather
than silently skip a safety limit, which is exactly the defect that field
was added to close. That makes every existing gate test supply one, and
most of them are testing something else entirely -- price deviation,
reconciliation, audit durability -- so they use this.

Deliberately permissive, not a real measurement. The caps themselves are
covered by tests/test_entry_limits.py (how the counts are gathered) and
tests/test_entry_limit_gate.py (what the gate does with them); a test
that used this to assert a cap PASSES would be asserting this fixture.
"""

from execution.entry_limits import EntryLimitState

DEFAULT_TRADING_DAY = "2026-07-29"


def unlimited(trading_day=DEFAULT_TRADING_DAY):
    """Room in both caps, so neither can be the reason a gate blocks."""
    return EntryLimitState(
        max_open_positions=99,
        max_daily_entries=99,
        open_position_symbols=frozenset(),
        pending_entry_symbols=frozenset(),
        daily_entry_count=0,
        trading_day=trading_day,
    )
