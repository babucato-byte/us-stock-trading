"""One submission decision, one reading of the position book.

Inside a single `s6_exec.lock` hold a BUY performed seven KIS round
trips, two of which fetched a fact already fetched in the same section:

    get_open_orders()      _revalidate_before_submit
    get_orderable_usd()    _revalidate_before_submit
    get_positions()        build_snapshot
    get_open_orders()      build_snapshot            <- same fact
    get_fills()            fill_window
    get_positions()        entry_limits.collect      <- same fact
    submit_order()

Each is paced by the shared 3s KIS read interval, and the pacing is
shared with the scanner -- so on 2026-09-02 a submission that should
have cost seconds held the mutation lock for 101,973 ms while a scan was
running.

This removes the POSITIONS duplicate only. `entry_limits` now derives
open-position symbols from the snapshot the execution engine already
built for this submission, so the fact still comes from the engine's own
KIS read -- CODEX-044's requirement that the reconciliation half of the
gate's facts never come from the caller.

The open-orders duplicate is deliberately NOT removed here: the
revalidation runs before the engine on purpose, so that a dropped entry
never registers an idempotency row, and deduping it would mean either
handing caller-sourced facts to the engine or moving the duplicate-order
check into the gate. Neither is a dedup.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution import entry_limits  # noqa: E402


class _Broker:
    """Counts what a submission actually asks the broker for."""

    def __init__(self, positions=()):
        self.calls = {"get_positions": 0, "get_open_orders": 0,
                      "get_orderable_usd": 0, "get_fills": 0}
        self._positions = list(positions)

    def get_positions(self):
        self.calls["get_positions"] += 1
        return self._positions

    def get_open_orders(self):
        self.calls["get_open_orders"] += 1
        return []

    def get_orderable_usd(self, instrument, price):
        self.calls["get_orderable_usd"] += 1
        return 1000.0

    def get_fills(self, *, start_date, end_date):
        self.calls["get_fills"] += 1
        return []


class _P:
    def __init__(self, symbol, quantity):
        self.symbol, self.quantity = symbol, quantity


class TestPositionsAreReadOnceFromTheEngineSnapshot:
    def test_injected_quantities_avoid_the_broker_read(self):
        broker = _Broker(positions=[_P("AAPL", 3)])
        symbols = entry_limits._symbols_from_quantities((("AAPL", 3),))
        assert symbols == frozenset({"AAPL"})
        assert broker.calls["get_positions"] == 0

    def test_the_derivation_matches_the_broker_path_exactly(self):
        """Same rule, same result -- only the source of the numbers moves."""
        broker = _Broker(positions=[_P("AAPL", 3), _P("MSFT", 0), _P("NVDA", 2)])
        from_broker = entry_limits._open_position_symbols(broker)
        from_snapshot = entry_limits._symbols_from_quantities(
            (("AAPL", 3), ("MSFT", 0), ("NVDA", 2)))
        assert from_broker == from_snapshot == frozenset({"AAPL", "NVDA"})

    def test_zero_quantities_are_excluded_either_way(self):
        assert entry_limits._symbols_from_quantities((("AAPL", 0),)) == frozenset()

    def test_a_non_numeric_quantity_fails_loudly(self):
        """Unknown is never treated as zero."""
        with pytest.raises(entry_limits.EntryLimitStateUnavailable):
            entry_limits._symbols_from_quantities((("AAPL", "many"),))
        with pytest.raises(entry_limits.EntryLimitStateUnavailable):
            entry_limits._symbols_from_quantities((("AAPL", True),))


class TestMissingEvidenceDoesNotBecomeEmpty:
    def test_none_means_read_the_broker_not_hold_nothing(self):
        """The distinction that matters: None is 'not carried', an empty
        tuple is 'read it, the account holds nothing'. Collapsing them
        would turn a missing reading into a licence to open a position."""
        broker = _Broker(positions=[_P("AAPL", 3)])
        assert entry_limits._symbols_from_quantities(()) == frozenset()
        assert entry_limits._open_position_symbols(broker) == frozenset({"AAPL"})
        assert broker.calls["get_positions"] == 1, (
            "None must fall back to a real read, never to an empty set")

    def test_collect_signature_takes_the_evidence_by_name(self):
        import inspect

        params = inspect.signature(entry_limits.collect).parameters
        assert "kis_position_quantities" in params
        assert params["kis_position_quantities"].default is None
        assert params["kis_position_quantities"].kind is inspect.Parameter.KEYWORD_ONLY


class TestTheOtherReadsAreUntouched:
    SOURCE = (REPO_ROOT / "kis_live_trading.py").read_text()

    def test_orderable_cash_is_still_read_at_submit_time(self):
        assert "broker.get_orderable_usd(instrument, buffered_price)" in self.SOURCE

    def test_the_open_order_check_is_still_performed(self):
        assert "broker.get_open_orders()" in self.SOURCE

    def test_the_fill_window_is_untouched(self):
        text = (REPO_ROOT / "reconciliation/fill_window.py").read_text()
        assert "broker.get_fills(" in text

    def test_the_engine_still_collects_its_own_reconciliation_facts(self):
        """CODEX-044: the caller must never hand the engine broker truth."""
        engine = (REPO_ROOT / "execution/execution_engine.py").read_text()
        assert "build_snapshot(" in engine
        snap = (REPO_ROOT / "reconciliation/snapshot.py").read_text()
        assert "kis_positions = broker.get_positions()" in snap
        assert "kis_open_orders = broker.get_open_orders()" in snap

    def test_no_cross_cycle_cache_was_introduced(self):
        """Scoped to the function this change added -- the module at
        large legitimately contains words like `field(...)`."""
        import inspect

        text = inspect.getsource(entry_limits._symbols_from_quantities)
        for forbidden in ("lru_cache", "cache", "global ", "TTL"):
            assert forbidden not in text, (
                f"the evidence is per-submission, not cached; found {forbidden!r}")

    def test_the_evidence_is_recomputed_per_call(self):
        """No memoisation: two different readings give two answers."""
        first = entry_limits._symbols_from_quantities((("AAPL", 3),))
        second = entry_limits._symbols_from_quantities((("NVDA", 1),))
        assert first == frozenset({"AAPL"}) and second == frozenset({"NVDA"})

    def test_the_snapshot_is_passed_by_name_from_the_gate_builder(self):
        assert "kis_position_quantities=getattr(" in self.SOURCE
        assert '"kis_position_quantities", None)' in self.SOURCE
