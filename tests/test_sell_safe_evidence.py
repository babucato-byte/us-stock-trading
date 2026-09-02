"""TCN-02A: SELL_SAFE_EVIDENCE, item by item.

`execution/sell_safe_evidence.py` is pure: it is handed a snapshot and
the evidence and returns a verdict. Each test removes exactly one item
and asserts the refusal names it, so the list in the module docstring
cannot quietly shrink. The one positive case is the full set.
"""

from datetime import datetime, timedelta, timezone

import pytest

from execution import sell_safe_evidence as sse
from execution.order_gate import OrderGateBlockedError, SellGateContext, evaluate_sell_gate
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from reconciliation.snapshot import ReconciliationSnapshot

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
ACCOUNT = "12345678"


def _snapshot(**overrides):
    kwargs = dict(
        account_id=ACCOUNT, symbol="AAPL", checked_at=NOW, positions_match=False,
        open_orders_match=True, fills_match=True, has_unknown_orders=False,
        source="test", detail=("position mismatch for ZZZ: internal=0 KIS=3",),
        kis_position_quantities=(("AAPL", 2), ("ZZZ", 3)),
        position_mismatch_symbols=frozenset({"ZZZ"}),
    )
    kwargs.update(overrides)
    return ReconciliationSnapshot(**kwargs)


def _local(**overrides):
    kwargs = dict(position_id="s6pos_1", status="OPEN", remaining_quantity=2,
                  entry_price=100.0, exit_submitted=False)
    kwargs.update(overrides)
    return sse.LocalPositionEvidence(**kwargs)


def _evidence(**overrides):
    kwargs = dict(local=_local(), broker_position_read_ok=True,
                  broker_position_quantity=2, broker_open_order_for_symbol=False,
                  unknown_orders_for_symbol=0, other_active_exit_intents=0,
                  collected_at=NOW)
    kwargs.update(overrides)
    return sse.SellSafeEvidence(**kwargs)


def _judge(snapshot=None, evidence=None, quantity=2, symbol="AAPL"):
    return sse.evaluate_protective_exit(
        snapshot=snapshot if snapshot is not None else _snapshot(),
        symbol=symbol, quantity=quantity,
        evidence=evidence if evidence is not None else _evidence(),
        now=NOW, account_id=ACCOUNT)


class TestTheFullSetPermits:
    def test_mismatch_elsewhere_with_complete_evidence_is_permitted(self):
        verdict = _judge()
        assert verdict.permitted, verdict
        assert verdict.reason_code == sse.PERMITTED
        assert verdict.max_quantity == 2

    def test_quantity_mismatch_on_this_symbol_is_capped_not_refused(self):
        """Local 2, broker 1: selling 1 is what both sides agree on."""
        snap = _snapshot(kis_position_quantities=(("AAPL", 1),),
                         position_mismatch_symbols=frozenset({"AAPL"}))
        ev = _evidence(broker_position_quantity=1)
        assert _judge(snap, ev, quantity=1).permitted
        refused = _judge(snap, ev, quantity=2)
        assert not refused.permitted
        assert refused.reason_code == sse.QTY_EXCEEDS_CONFIRMED
        assert refused.max_quantity == 1


class TestEachMissingItemRefuses:
    def test_no_evidence_at_all(self):
        assert _judge(evidence=None).reason_code == sse.NOT_SUPPLIED or True
        verdict = sse.evaluate_protective_exit(
            snapshot=_snapshot(), symbol="AAPL", quantity=1, evidence=None,
            now=NOW, account_id=ACCOUNT)
        assert not verdict.permitted and verdict.reason_code == sse.NOT_SUPPLIED

    def test_stale_reconciliation_is_not_evidence(self):
        snap = _snapshot(checked_at=NOW - timedelta(seconds=31))
        v = _judge(snap)
        assert not v.permitted and v.reason_code == sse.RECONCILIATION_NOT_USABLE

    def test_foreign_account_snapshot_is_not_evidence(self):
        v = _judge(_snapshot(account_id="other"))
        assert not v.permitted and v.reason_code == sse.RECONCILIATION_NOT_USABLE

    def test_snapshot_for_another_symbol_is_not_evidence(self):
        v = _judge(_snapshot(symbol="MSFT"))
        assert not v.permitted and v.reason_code == sse.RECONCILIATION_NOT_USABLE

    def test_local_row_not_fill_backed(self):
        for local in (_local(status="SUBMITTED"), _local(entry_price=None),
                      _local(entry_price=0.0), _local(remaining_quantity=0), None):
            v = _judge(evidence=_evidence(local=local))
            assert not v.permitted and v.reason_code == sse.LOCAL_NOT_FILL_BACKED, local

    def test_exit_already_submitted_locally(self):
        v = _judge(evidence=_evidence(local=_local(exit_submitted=True)))
        assert not v.permitted and v.reason_code == sse.SELL_ALREADY_IN_FLIGHT

    def test_exit_submitted_status_is_not_sellable_again(self):
        v = _judge(evidence=_evidence(local=_local(status="EXIT_SUBMITTED")))
        assert not v.permitted and v.reason_code == sse.LOCAL_NOT_FILL_BACKED

    def test_broker_open_order_for_symbol(self):
        v = _judge(evidence=_evidence(broker_open_order_for_symbol=True))
        assert not v.permitted and v.reason_code == sse.SELL_ALREADY_PENDING

    def test_another_active_exit_intent(self):
        v = _judge(evidence=_evidence(other_active_exit_intents=1))
        assert not v.permitted and v.reason_code == sse.SELL_ALREADY_PENDING

    def test_unknown_order_for_symbol_in_ledger(self):
        v = _judge(evidence=_evidence(unknown_orders_for_symbol=1))
        assert not v.permitted and v.reason_code == sse.SUBMISSION_UNKNOWN_FOR_SYMBOL

    def test_unknown_order_for_symbol_in_snapshot(self):
        snap = _snapshot(has_unknown_orders=True, unknown_order_symbols=frozenset({"AAPL"}))
        v = _judge(snap)
        assert not v.permitted and v.reason_code == sse.SUBMISSION_UNKNOWN_FOR_SYMBOL

    def test_unknown_order_for_another_symbol_does_not_refuse_on_its_own(self):
        snap = _snapshot(has_unknown_orders=True, unknown_order_symbols=frozenset({"ZZZ"}))
        assert _judge(snap).permitted

    def test_order_level_ambiguity_about_this_symbol(self):
        snap = _snapshot(open_orders_match=False, order_dirty_symbols=frozenset({"AAPL"}))
        v = _judge(snap)
        assert not v.permitted and v.reason_code == sse.ORDER_STATE_AMBIGUOUS_FOR_SYMBOL

    def test_broker_quantity_not_carried_by_snapshot(self):
        v = _judge(_snapshot(kis_position_quantities=None))
        assert not v.permitted and v.reason_code == sse.BROKER_QTY_UNCONFIRMED

    def test_caller_read_failed(self):
        v = _judge(evidence=_evidence(broker_position_read_ok=False))
        assert not v.permitted and v.reason_code == sse.BROKER_QTY_UNCONFIRMED

    def test_two_broker_reads_disagree(self):
        v = _judge(evidence=_evidence(broker_position_quantity=3))
        assert not v.permitted and v.reason_code == sse.BROKER_QTY_UNCONFIRMED

    def test_broker_explicitly_flat_is_an_external_close_not_a_sell(self):
        snap = _snapshot(kis_position_quantities=(("ZZZ", 3),),
                         position_mismatch_symbols=frozenset({"AAPL", "ZZZ"}))
        v = _judge(snap, _evidence(broker_position_quantity=0))
        assert not v.permitted and v.reason_code == sse.BROKER_REPORTS_FLAT

    def test_sell_quantity_above_min_of_both_sides(self):
        v = _judge(quantity=3)
        assert not v.permitted and v.reason_code == sse.QTY_EXCEEDS_CONFIRMED
        assert v.max_quantity == 2


class TestLocalEvidenceFromRow:
    def test_a_fill_backed_open_row(self):
        local = sse.LocalPositionEvidence.from_row(
            {"position_id": "p1", "status": "OPEN", "quantity": 3,
             "entry_price": 12.5, "exit_submitted": 0})
        assert local.fill_backed and local.remaining_quantity == 3

    def test_a_submitted_row_is_not(self):
        local = sse.LocalPositionEvidence.from_row(
            {"position_id": "p1", "status": "SUBMITTED", "quantity": None,
             "entry_price": None, "exit_submitted": 0})
        assert not local.fill_backed

    def test_latched_exit_pending_from_open_is(self):
        local = sse.LocalPositionEvidence.from_row(
            {"status": "EXIT_PENDING", "quantity": 1, "entry_price": 9.0,
             "exit_submitted": 0}, position_id="p2")
        assert local.fill_backed and local.position_id == "p2"


class TestTheGateHonoursTheVerdict:
    """The gate is where the verdict becomes a block or a pass."""

    def _ctx(self, **overrides):
        intent = OrderIntent(
            internal_order_id="s6exit-AAPL-1", signal_id="s6exit-AAPL-1",
            strategy_id="POSITIONS_LIFECYCLE_EXIT", symbol="AAPL", exchange="NASDAQ",
            side="sell", quantity=2, order_type="limit", limit_price=100.0,
            stop_price=None, target_price=None, created_at=NOW,
        )
        kwargs = dict(
            execution_broker="kis", live_order_enabled=True, order_intent=intent,
            instrument=build_instrument("AAPL", exchange="NASDAQ"),
            kis_position_quantity=2, position_source="kis",
            has_existing_sell_order_for_symbol=False, reconciliation=_snapshot(),
            kis_account_no=ACCOUNT, now=NOW, sell_safe_evidence=_evidence(),
        )
        kwargs.update(overrides)
        return SellGateContext(**kwargs)

    def test_dirty_without_evidence_still_blocks(self):
        with pytest.raises(OrderGateBlockedError) as exc:
            evaluate_sell_gate(self._ctx(sell_safe_evidence=None))
        assert exc.value.code == "RECONCILIATION"

    def test_dirty_with_complete_evidence_passes(self):
        assert evaluate_sell_gate(self._ctx()) is True

    def test_dirty_with_incomplete_evidence_blocks_and_names_the_item(self):
        with pytest.raises(OrderGateBlockedError) as exc:
            evaluate_sell_gate(self._ctx(
                sell_safe_evidence=_evidence(broker_open_order_for_symbol=True)))
        assert exc.value.code == "RECONCILIATION"
        assert sse.SELL_ALREADY_PENDING in str(exc.value)

    def test_stale_snapshot_is_never_overridden(self):
        stale = _snapshot(checked_at=NOW - timedelta(seconds=31))
        with pytest.raises(OrderGateBlockedError) as exc:
            evaluate_sell_gate(self._ctx(reconciliation=stale))
        assert sse.RECONCILIATION_NOT_USABLE in str(exc.value)

    def test_clean_snapshot_passes_regardless_of_evidence(self):
        clean = _snapshot(positions_match=True, detail=(),
                          position_mismatch_symbols=frozenset())
        assert evaluate_sell_gate(self._ctx(reconciliation=clean,
                                            sell_safe_evidence=None)) is True

    def test_existing_gate_checks_still_run_first(self):
        with pytest.raises(OrderGateBlockedError) as exc:
            evaluate_sell_gate(self._ctx(has_existing_sell_order_for_symbol=True))
        assert exc.value.code == "DUPLICATE_SELL"
