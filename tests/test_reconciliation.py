from datetime import datetime, timezone

import pytest

from domain.position import Position
from reconciliation.account_reconciler import reconcile_account
from reconciliation.order_reconciler import reconcile_unknown_order
from reconciliation.position_reconciler import reconcile_positions

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


def _pos(symbol, qty, source="kis"):
    return Position(symbol=symbol, quantity=qty, average_fill_price=100.0,
                     unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source=source)


class TestReconcilePositions:
    def test_matching_positions_no_mismatch(self):
        assert reconcile_positions([_pos("AAPL", 2)], [_pos("AAPL", 2)]) == []

    def test_quantity_mismatch_detected(self):
        mismatches = reconcile_positions([_pos("AAPL", 2)], [_pos("AAPL", 3)])
        assert len(mismatches) == 1
        assert mismatches[0].symbol == "AAPL"
        assert mismatches[0].reason == "quantity mismatch"

    def test_kis_only_position_detected(self):
        mismatches = reconcile_positions([], [_pos("AAPL", 1)])
        assert len(mismatches) == 1
        assert "not tracked internally" in mismatches[0].reason

    def test_internal_only_position_detected(self):
        mismatches = reconcile_positions([_pos("AAPL", 1)], [])
        assert len(mismatches) == 1
        assert "does not exist at KIS" in mismatches[0].reason

    def test_empty_both_sides_no_mismatch(self):
        assert reconcile_positions([], []) == []


class TestReconcileUnknownOrder:
    def test_no_broker_order_id_never_resolves(self):
        outcome = reconcile_unknown_order("ord-1", None, [], [])
        assert outcome.resolved is False

    def test_matches_fill_history_resolves_filled(self):
        outcome = reconcile_unknown_order(
            "ord-1", "kis-999", [], [{"ODNO": "kis-999", "ft_ccld_qty": "1"}],
            requested_quantity=1,
        )
        assert outcome.resolved is True
        assert outcome.confirmed_status == "FILLED"

    def test_partial_fill_is_not_reported_as_filled(self):
        # CODEX-044: a 2-share order with a single 1-share fill row was
        # previously resolved out of UNKNOWN as fully FILLED.
        outcome = reconcile_unknown_order(
            "ord-1", "kis-999", [], [{"ODNO": "kis-999", "ft_ccld_qty": "1"}],
            requested_quantity=2,
        )
        assert outcome.resolved is True
        assert outcome.confirmed_status == "PARTIALLY_FILLED"

    def test_two_fill_rows_summing_to_requested_resolve_filled(self):
        outcome = reconcile_unknown_order(
            "ord-1", "kis-999", [], [
                {"ODNO": "kis-999", "ft_ccld_qty": "1"},
                {"ODNO": "kis-999", "ft_ccld_qty": "1"},
            ],
            requested_quantity=2,
        )
        assert outcome.confirmed_status == "FILLED"

    def test_unknown_requested_quantity_never_guesses_filled(self):
        outcome = reconcile_unknown_order(
            "ord-1", "kis-999", [], [{"ODNO": "kis-999", "ft_ccld_qty": "1"}],
            requested_quantity=None,
        )
        assert outcome.resolved is False
        assert outcome.confirmed_status is None

    def test_cumulative_fill_exceeding_requested_is_not_resolved(self):
        outcome = reconcile_unknown_order(
            "ord-1", "kis-999", [], [{"ODNO": "kis-999", "ft_ccld_qty": "3"}],
            requested_quantity=2,
        )
        assert outcome.resolved is False
        assert "data integrity" in outcome.reason

    def test_matches_fill_history_zero_qty_stays_unresolved(self):
        # A fill row that reports zero filled quantity confirms nothing;
        # resolving it to CANCELLED was a guess that could clear the
        # UNKNOWN block on an order that had actually reached the market.
        outcome = reconcile_unknown_order(
            "ord-1", "kis-999", [], [{"ODNO": "kis-999", "ft_ccld_qty": "0"}],
            requested_quantity=1,
        )
        assert outcome.resolved is False
        assert outcome.confirmed_status is None

    def test_matches_open_order_resolves_accepted(self):
        outcome = reconcile_unknown_order("ord-1", "kis-999", [{"ODNO": "kis-999"}], [])
        assert outcome.resolved is True
        assert outcome.confirmed_status == "ACCEPTED"

    def test_no_match_anywhere_unresolved(self):
        outcome = reconcile_unknown_order("ord-1", "kis-999", [], [])
        assert outcome.resolved is False

    def test_fill_takes_priority_over_open_order(self):
        outcome = reconcile_unknown_order(
            "ord-1", "kis-999", [{"ODNO": "kis-999"}], [{"ODNO": "kis-999", "ft_ccld_qty": "1"}],
            requested_quantity=1,
        )
        assert outcome.confirmed_status == "FILLED"


class TestReconcileAccount:
    def test_within_tolerance_no_mismatch(self):
        assert reconcile_account(
            internal_reserved_usd=100.0, kis_usd_orderable_cash=900.0, kis_usd_cash=1000.0,
        ) == []

    def test_exceeds_tolerance_detected(self):
        mismatches = reconcile_account(
            internal_reserved_usd=50.0, kis_usd_orderable_cash=900.0, kis_usd_cash=1000.0,
            tolerance_usd=1.0,
        )
        assert len(mismatches) == 1
        assert mismatches[0].field == "reserved_usd"

    def test_non_finite_input_reported_as_mismatch(self):
        mismatches = reconcile_account(
            internal_reserved_usd=float("nan"), kis_usd_orderable_cash=900.0, kis_usd_cash=1000.0,
        )
        assert len(mismatches) == 1
        assert "not a finite number" in mismatches[0].reason
