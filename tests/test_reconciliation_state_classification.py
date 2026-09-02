"""TCN-02A: the reconciliation state vocabulary and what each state permits.

`reconciliation/state.py` names what a snapshot means. These pin two
things: that the classifier agrees with `verify_snapshot` about every
axis, and that the BUY policy is fail-closed in every state but CLEAN --
the property TCN-02A must not weaken while it changes the SELL policy.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reconciliation import state as rs
from reconciliation.snapshot import (
    ReconciliationLocalStateError,
    ReconciliationSnapshot,
    ReconciliationUnavailableError,
    verify_snapshot,
    ReconciliationBlockedError,
)

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
ACCOUNT = "12345678"


def _snapshot(**overrides):
    kwargs = dict(
        account_id=ACCOUNT, symbol="AAPL", checked_at=NOW, positions_match=True,
        open_orders_match=True, fills_match=True, has_unknown_orders=False,
        source="test",
    )
    kwargs.update(overrides)
    return ReconciliationSnapshot(**kwargs)


class TestEveryStateIsReachable:
    def test_clean(self):
        c = rs.classify_snapshot(_snapshot(), account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.CLEAN and c.is_clean()

    def test_position_mismatch(self):
        c = rs.classify_snapshot(_snapshot(positions_match=False), account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.POSITION_MISMATCH

    def test_order_mismatch_from_open_orders(self):
        c = rs.classify_snapshot(_snapshot(open_orders_match=False), account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.ORDER_MISMATCH

    def test_order_mismatch_from_fills(self):
        c = rs.classify_snapshot(_snapshot(fills_match=False), account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.ORDER_MISMATCH

    def test_submission_unknown(self):
        c = rs.classify_snapshot(_snapshot(has_unknown_orders=True), account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.SUBMISSION_UNKNOWN

    def test_stale(self):
        old = _snapshot(checked_at=NOW - timedelta(seconds=31))
        c = rs.classify_snapshot(old, account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.STALE_RECONCILIATION

    def test_future_is_stale(self):
        c = rs.classify_snapshot(_snapshot(checked_at=NOW + timedelta(seconds=5)),
                                 account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.STALE_RECONCILIATION

    def test_missing_snapshot_is_local_state_failure(self):
        c = rs.classify_snapshot(None, account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.LOCAL_STATE_FAILURE

    def test_foreign_account_is_local_state_failure(self):
        c = rs.classify_snapshot(_snapshot(account_id="other"), account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.LOCAL_STATE_FAILURE

    def test_broker_read_failure(self):
        c = rs.classify_failure(ReconciliationUnavailableError("KIS position read failed"))
        assert c.primary == rs.BROKER_READ_FAILURE

    def test_local_read_failure(self):
        c = rs.classify_failure(ReconciliationLocalStateError("ledger unreadable"))
        assert c.primary == rs.LOCAL_STATE_FAILURE


class TestSeverityAndPolicy:
    def test_the_most_severe_state_is_primary_and_all_are_kept(self):
        snap = _snapshot(positions_match=False, has_unknown_orders=True,
                         checked_at=NOW - timedelta(seconds=40))
        c = rs.classify_snapshot(snap, account_id=ACCOUNT, now=NOW)
        assert c.primary == rs.STALE_RECONCILIATION
        assert {rs.STALE_RECONCILIATION, rs.SUBMISSION_UNKNOWN,
                rs.POSITION_MISMATCH} <= c.states

    @pytest.mark.parametrize("state", sorted(rs.ALL_STATES - {rs.CLEAN}))
    def test_every_dirty_state_blocks_buy(self, state):
        assert state in rs.BUY_BLOCKING_STATES

    def test_clean_does_not_block_buy(self):
        assert rs.CLEAN not in rs.BUY_BLOCKING_STATES

    @pytest.mark.parametrize("state", [rs.BROKER_READ_FAILURE, rs.LOCAL_STATE_FAILURE,
                                       rs.STALE_RECONCILIATION])
    def test_unobserved_states_hard_block_sell(self, state):
        assert state in rs.SELL_HARD_BLOCK_STATES
        assert state not in rs.SELL_EVIDENCE_STATES

    @pytest.mark.parametrize("state", [rs.POSITION_MISMATCH, rs.ORDER_MISMATCH,
                                       rs.SUBMISSION_UNKNOWN])
    def test_observed_disagreements_need_evidence_for_sell(self, state):
        assert state in rs.SELL_EVIDENCE_STATES
        assert state not in rs.SELL_HARD_BLOCK_STATES

    def test_hard_block_outranks_evidence(self):
        snap = _snapshot(positions_match=False, checked_at=NOW - timedelta(seconds=40))
        c = rs.classify_snapshot(snap, account_id=ACCOUNT, now=NOW)
        assert c.hard_blocks_sell()
        assert not c.sell_needs_evidence()


class TestClassifierAgreesWithVerifySnapshot:
    """The two must never disagree: one is the decision, the other the
    name of the decision."""

    @pytest.mark.parametrize("overrides", [
        {}, {"positions_match": False}, {"open_orders_match": False},
        {"fills_match": False}, {"has_unknown_orders": True},
        {"checked_at": NOW - timedelta(seconds=31)},
        {"checked_at": NOW + timedelta(seconds=1)},
    ])
    def test_clean_iff_verify_passes(self, overrides):
        snap = _snapshot(**overrides)
        try:
            verify_snapshot(snap, account_id=ACCOUNT, symbol="AAPL", now=NOW)
            passes = True
        except ReconciliationBlockedError:
            passes = False
        c = rs.classify_snapshot(snap, account_id=ACCOUNT, now=NOW)
        assert c.is_clean() == passes
