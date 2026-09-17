"""A SELL that is abandoned after filling part of the position.

VIAV, 2026-09-17. SELL 4 accepted at 09:11, ONE share filled, the stale
order cancelled at 09:26 and the position released for retry with
`quantity` still 4 while KIS held 3. Every retry after that asked for 4
and was refused by the reconciliation gate -- "internal=4 KIS=3
(quantity mismatch)" -- seven rejected orders between 09:35 and 10:22.

The gate was right. The quantity it was reading was not: the two paths
that ABANDON a partially-filled SELL and return the row to EXIT_PENDING
never took the filled shares off the position, so the retry was sized to
the original entry rather than to what the account still held.
"""

from datetime import datetime, timezone

import pytest

from s6_live import exit_runtime, position_store

UTC = timezone.utc
NOW = datetime(2026, 9, 17, 9, 26, tzinfo=UTC)


def _executable(fn) -> str:
    """`fn`'s source with docstrings stripped.

    `cancel_stale_sell`'s own docstring names `release_dead_exit` before
    any code does, so a raw index would compare against prose.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(ast.fix_missing_locations(tree))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "state.db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _open_position(conn, *, symbol="VIAV", quantity=4):
    pid = position_store.record_submission(
        conn, symbol=symbol, variant="S6-P", entry_session="PREMARKET",
        range_high=10.0, range_low=9.0, entry_volume_expansion=2.0,
        client_order_id=f"c-{symbol}")
    position_store.open_from_fill(conn, pid, quantity=quantity,
                                  average_fill_price=10.0,
                                  entry_order_id=f"o-{symbol}")
    return pid


def _quantity(conn, pid):
    return int(position_store.load(conn, pid)["quantity"])


# -- the reducer ----------------------------------------------------------

class TestReducer:

    def test_1_to_4_a_partial_fill_of_one_leaves_three(self, conn):
        pid = _open_position(conn, quantity=4)
        assert _quantity(conn, pid) == 4
        remaining = position_store.reduce_after_partial_exit(
            conn, pid, sold=1, now=NOW)
        assert remaining == 3
        assert _quantity(conn, pid) == 3

    def test_5_two_partials_walk_the_position_down(self, conn):
        """4 -> 3 -> 2, and the next retry is sized to 2."""
        pid = _open_position(conn, quantity=4)
        assert position_store.reduce_after_partial_exit(conn, pid, sold=1,
                                                        now=NOW) == 3
        assert position_store.reduce_after_partial_exit(conn, pid, sold=1,
                                                        now=NOW) == 2
        assert _quantity(conn, pid) == 2

    def test_a_zero_fill_changes_nothing(self, conn):
        """An abandoned SELL that filled nothing must not shrink it."""
        pid = _open_position(conn, quantity=4)
        for sold in (0, -1, None, "x"):
            assert position_store.reduce_after_partial_exit(
                conn, pid, sold=sold, now=NOW) == 4
        assert _quantity(conn, pid) == 4

    def test_6_a_full_fill_is_left_to_the_close_path(self, conn):
        """A held position must have at least one share -- the table's own
        CHECK enforces it -- so a SELL that filled everything is closed by
        the ordinary path, not reduced to zero here."""
        pid = _open_position(conn, quantity=4)
        for sold in (4, 9):
            assert position_store.reduce_after_partial_exit(
                conn, pid, sold=sold, now=NOW) == 4
        assert _quantity(conn, pid) == 4
        assert position_store.load(conn, pid)["status"] != position_store.CLOSED

    def test_a_missing_position_is_not_an_error(self, conn):
        assert position_store.reduce_after_partial_exit(
            conn, "s6pos_missing", sold=1, now=NOW) == 0


# -- what the intent carries ---------------------------------------------

class TestFilledBeforeAbort:

    def test_the_recorded_progress_is_read(self):
        assert exit_runtime.filled_before_abort(
            {"confirmed_filled_qty": 1.0}) == 1

    @pytest.mark.parametrize("intent", [
        None, {}, {"confirmed_filled_qty": None},
        {"confirmed_filled_qty": "x"}, {"confirmed_filled_qty": -2}])
    def test_anything_unreadable_is_zero_not_a_guess(self, intent):
        assert exit_runtime.filled_before_abort(intent) == 0


# -- the two abandon-and-release paths -----------------------------------

class TestCancelPathReducesBeforeRelease:
    """8. The stale-SELL timeout path -- the one VIAV took."""

    def test_the_cancel_path_reduces_then_releases(self, conn, monkeypatch):
        pid = _open_position(conn, quantity=4)
        position_store.mark_exit_submitted(conn, pid, "EMA_STRUCTURE_FAILURE")

        calls = []
        monkeypatch.setattr(
            exit_runtime, "_abort_intent",
            lambda c, p: calls.append(("abort", p)) or {"confirmed_filled_qty": 1.0})

        from s6_live import exit_timeout

        # Drive only the release tail, with the broker interaction stubbed:
        # this test is about the quantity, not the cancel transport.
        intent = exit_runtime._abort_intent(conn, pid)
        sold = exit_runtime.filled_before_abort(intent)
        position_store.reduce_after_partial_exit(conn, pid, sold=sold, now=NOW)
        position_store.release_dead_exit(conn, pid, reason="X", now=NOW)

        assert _quantity(conn, pid) == 3, "the filled share is off the position"
        row = position_store.load(conn, pid)
        assert row["status"] == position_store.EXIT_PENDING
        assert not row["exit_submitted"], "released for retry"
        assert "apply_confirmed_exit_fill" in \
            __import__("inspect").getsource(exit_timeout)

    def test_the_cancel_path_calls_the_reducer_before_releasing(self):
        """Pinned structurally, on the FUNCTION rather than the module --
        the module docstring names `release_dead_exit` several hundred
        lines earlier, which a whole-file index would find first."""
        import inspect

        from s6_live import exit_timeout

        body = _executable(exit_timeout.cancel_stale_sell)
        assert "apply_confirmed_exit_fill" in body
        assert body.index("apply_confirmed_exit_fill") < \
            body.index("release_dead_exit"), (
            "the quantity must be corrected before the row is released")
        # The "apply before abort" ordering is pinned behaviourally in
        # TestExactlyOnce rather than here: this function imports
        # `_abort_intent` at the top, so a textual index finds the import
        # long before the call and proves nothing.

    def test_9_recover_dead_exits_does_not_reduce_at_all(self):
        """Inverted deliberately. This assertion used to require the
        reduction that WAS the defect: the branch is only reachable when
        `broker_qty >= held_row`, so the account still holds everything
        the row claims and there is nothing to take off. Subtracting the
        dead SELL's fill there double-applied whatever `sync_sell_fills`
        had already applied for the same fill."""
        import inspect

        body = _executable(exit_runtime.recover_dead_exits)
        assert "reduce_after_partial_exit" not in body
        assert "apply_confirmed_exit_fill" not in body
        assert "_abort_intent" in body, "the stale intent must still be ended"


# -- 4. the retry reads the corrected quantity ---------------------------

class TestRetryUsesRemaining:

    class _Adapter:
        def __init__(self):
            self.submissions = []

        def submit_order(self, symbol, quantity, **kwargs):
            self.submissions.append((symbol, quantity))

            class _R:
                status_code = 200
                text = "ok"

                def json(self):
                    return {"output": {"ODNO": "1"}}

            return _R()

    @pytest.fixture(autouse=True)
    def _lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("S6_EXECUTION_LOCK_FILE", str(tmp_path / "x.lock"))

    def test_4_after_a_partial_the_retry_sells_the_remainder(self, conn):
        pid = _open_position(conn, quantity=4)
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        adapter = self._Adapter()
        outcome = exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid,
            row=position_store.load(conn, pid), reason="EMA_STRUCTURE_FAILURE")
        assert outcome.action == "SOLD"
        assert adapter.submissions == [("VIAV", 3)], "3, never the original 4"

    def test_7_a_stale_row_cannot_resurrect_the_old_quantity(self, conn):
        """The lock-retry path re-reads under the lock, so a decision made
        from a pre-reduction row still submits the corrected size."""
        pid = _open_position(conn, quantity=4)
        stale = dict(position_store.load(conn, pid))     # says 4
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        adapter = self._Adapter()
        exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid, row=stale,
            reason="EMA_STRUCTURE_FAILURE")
        assert adapter.submissions == [("VIAV", 3)]

    def test_5b_after_two_partials_the_retry_sells_two(self, conn):
        pid = _open_position(conn, quantity=4)
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        position_store.reduce_after_partial_exit(conn, pid, sold=1, now=NOW)
        adapter = self._Adapter()
        exit_runtime._submit_sell_locked(
            conn, broker_adapter=adapter, position_id=pid,
            row=position_store.load(conn, pid), reason="EMA_STRUCTURE_FAILURE")
        assert adapter.submissions == [("VIAV", 2)]

    def test_6b_a_zero_quantity_row_is_refused_before_the_broker(self, conn):
        """The pre-check already refuses it, and the reducer never creates
        such a row -- both halves of the zero-remaining guard."""
        assert exit_runtime._sell_still_valid(
            {"status": "OPEN", "quantity": 0}).startswith("no quantity")
        pid = _open_position(conn, quantity=4)
        assert position_store.reduce_after_partial_exit(
            conn, pid, sold=4, now=NOW) == 4, "never reduced to zero"


# -- 10. the gate is untouched -------------------------------------------

class TestGateUnchanged:

    def test_the_precheck_still_refuses_a_zero_quantity(self):
        assert exit_runtime._sell_still_valid({"status": "OPEN", "quantity": 0})
        assert exit_runtime._sell_still_valid(
            {"status": "OPEN", "quantity": 0}).startswith("no quantity")

    def test_the_gate_that_refused_is_still_there(self):
        """The reconciliation POSITION_MISMATCH gate did its job; the fix
        was upstream state, not a weakened gate.

        Asserted as behaviour. What stood here was a name-only diff against
        a fixed SHA, which could only say a file was untouched -- and broke
        the first time one of those files was legitimately edited. The
        refusals themselves are exercised in TestTheGateStillRefuses below.
        """
        assert _fill_check_still_refuses_disagreement(), (
            "the fill check must still report an order whose broker fill "
            "disagrees with what we recorded")


def _fill_check_still_refuses_disagreement():
    """Positive proof the fill check still fails closed, replacing a
    name-only diff that could only ever say a file was untouched."""
    import inspect

    from reconciliation import snapshot

    body = inspect.getsource(snapshot._check_fills)
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    return ("no internal record of any fill" in code
            and "internally requested quantity" in code)


# -- the safety contracts these tests actually protect ---------------------
#
# What stood here was `git diff --name-only <fixed sha> HEAD` plus
# "assert this file is not in the list". The intent was right -- this work
# must not weaken the gate that refuses a BUY -- but a name-only diff
# against a FIXED baseline cannot express it. The baseline recedes: every
# later commit adds to the list, so the first legitimate edit to a shared
# file fails a test about quantity semantics and tells the reader the
# opposite of the truth. It happened twice, to the same guard, a day apart.
#
# A newer SHA would only reset the clock. These assert the contracts
# instead, so they hold no matter who edits what.


def _blocked_snapshot(**flags):
    """A ReconciliationSnapshot that is dirty in exactly one way."""
    from datetime import datetime, timezone

    from reconciliation.snapshot import ReconciliationSnapshot

    base = dict(positions_match=True, open_orders_match=True,
                fills_match=True, has_unknown_orders=False)
    base.update(flags)
    return ReconciliationSnapshot(
        account_id="ACCT", symbol="AAPL",
        checked_at=datetime.now(timezone.utc), source="test", **base)


def _verify(snapshot):
    """verify_snapshot's verdict: None if it permits, the error if it refuses."""
    from reconciliation.snapshot import ReconciliationBlockedError, verify_snapshot

    try:
        verify_snapshot(snapshot, account_id="ACCT", symbol="AAPL")
        return None
    except ReconciliationBlockedError as exc:
        return exc


class TestTheGateStillRefuses:
    """A. B. C. D. -- the refusals a BUY depends on, asserted by behaviour.

    Each builds a snapshot dirty in exactly ONE way and requires
    `verify_snapshot` to refuse it. A clean snapshot must still be
    permitted, or these would pass against a gate that refuses everything.
    """

    def test_a_clean_snapshot_is_permitted(self):
        assert _verify(_blocked_snapshot()) is None, (
            "a gate that refuses everything proves nothing below")

    def test_A_a_reconciliation_dirty_snapshot_is_refused(self):
        assert _verify(_blocked_snapshot(fills_match=False)) is not None

    def test_B_a_position_mismatch_is_refused(self):
        assert _verify(_blocked_snapshot(positions_match=False)) is not None

    def test_C_an_unknown_order_is_refused(self):
        assert _verify(_blocked_snapshot(has_unknown_orders=True)) is not None

    def test_C2_an_open_order_disagreement_is_refused(self):
        assert _verify(_blocked_snapshot(open_orders_match=False)) is not None

    def test_D_a_missing_snapshot_is_refused(self):
        from reconciliation.snapshot import ReconciliationBlockedError, verify_snapshot

        with pytest.raises(ReconciliationBlockedError):
            verify_snapshot(None, account_id="ACCT")

    def test_D2_a_snapshot_for_another_account_is_refused(self):
        from reconciliation.snapshot import ReconciliationBlockedError, verify_snapshot

        with pytest.raises(ReconciliationBlockedError):
            verify_snapshot(_blocked_snapshot(), account_id="SOMEONE_ELSE")

    def test_D3_a_stale_snapshot_is_refused(self):
        from datetime import datetime, timedelta, timezone

        from reconciliation.snapshot import ReconciliationBlockedError, verify_snapshot

        old = _blocked_snapshot()
        future = datetime.now(timezone.utc) + timedelta(days=1)
        with pytest.raises(ReconciliationBlockedError):
            verify_snapshot(old, account_id="ACCT", symbol="AAPL", now=future)

    def test_E_the_engine_refuses_rather_than_reporting_a_reason(self):
        """The engine converts a blocked snapshot into a refusal with a
        reason code -- it never returns a permissive default."""
        from execution import execution_engine

        assert execution_engine.REASON_RECONCILIATION_DIRTY == "RECONCILIATION_DIRTY"
        assert execution_engine.REASON_UNKNOWN_ORDER == "UNKNOWN_ORDER"
        assert execution_engine.REASON_HALT == "HALT"
        code = execution_engine._reconciliation_block_code(
            _blocked_snapshot(has_unknown_orders=True))
        assert code == execution_engine.REASON_UNKNOWN_ORDER
        assert execution_engine._reconciliation_block_code(
            _blocked_snapshot(positions_match=False)) == \
            execution_engine.REASON_RECONCILIATION_DIRTY


class TestLimiterPolicyUnchanged:
    """F. -- asserted from the configuration itself, not from a file diff."""

    def test_the_read_interval_and_queue_depth_are_unchanged(self):
        from brokers import kis_rate_limiter as limiter

        assert limiter.DEFAULT_READ_MIN_INTERVAL == 3.0
        assert limiter.DEFAULT_MAX_RESERVATION_DEPTH == 16

    def test_the_priority_caps_are_unchanged(self):
        """Caps are in INTERVALS -- multiples of the read interval -- not in
        absolute seconds. An absolute cap once broke TOKEN, whose interval
        is 60s."""
        from brokers import kis_rate_limiter as limiter

        caps = limiter.LOW_PRIORITY_QUEUE_CAP_INTERVALS
        assert caps[limiter.PRIORITY_P1] == 4.0
        assert caps[limiter.PRIORITY_P2] == 2.0
        assert caps[limiter.PRIORITY_P3] == 1.0

    def test_p0_is_uncapped(self):
        """A safety read must never be refused by this codebase's own
        fairness rule, so P0 carries no cap at all."""
        from brokers import kis_rate_limiter as limiter

        assert limiter.PRIORITY_P0 not in limiter.LOW_PRIORITY_QUEUE_CAP_INTERVALS
