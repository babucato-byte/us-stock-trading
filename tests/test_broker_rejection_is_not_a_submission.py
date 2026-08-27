"""A broker rejection is not a successful buy.

2026-08-27, the first session with the entry scheduler running. BTG
reached READY, was sized to three shares from $20.96 of orderable cash,
passed every gate, and was sent to KIS. KIS refused it. The durable
record shows exactly that -- `kis_order_idempotency` REJECTED, no broker
order id, `order_state_events` SUBMITTING -> REJECTED with
`{"broker_order_id": null}`.

The cycle recorded it as an approved buy.

`submit_buy_order` PERSISTS the broker's answer and RETURNS it --
ACCEPTED, REJECTED or UNKNOWN -- and raises only when something goes
wrong on the way there. The caller read "no exception" as "the order is
live", so a rejection was counted in `submitted`, audited
SHADOW_COMPLETED/APPROVED, and given an S6 position row at SUBMITTED for
an order that existed nowhere.

That orphan row then blocked every later entry for the symbol through
SYMBOL_ALREADY_HELD, which is why the damage stopped at one order per
symbol -- and why it read as duplicate protection working rather than as
a defect being contained by it. BTG, PBR and PTEN each ended that
session holding a position the account never had, and reconciliation
reported clean throughout: no broker position disagreed, because the
rows claimed nothing the broker could contradict.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SOURCE = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")
ENGINE = (REPO_ROOT / "execution" / "execution_engine.py").read_text(encoding="utf-8")


def _submit_block():
    """The text from the transport call to the success bookkeeping."""
    start = SOURCE.index("result = execution_engine.submit_buy_order(")
    end = SOURCE.index('results["submitted"].append(symbol)', start)
    return SOURCE[start:end]


class TestTheEngineReportsRatherThanRaises:
    def test_it_returns_the_status_instead_of_raising_on_rejection(self):
        """The premise the caller got wrong. If this ever changes to a
        raise, the caller's branch becomes dead code and this test says
        so rather than leaving both in place."""
        assert "return ExecutionResult(" in ENGINE
        assert "status=execution_record.status" in ENGINE


class TestARejectionIsNotCountedAsSubmitted:
    def test_the_caller_branches_on_the_returned_status(self):
        block = _submit_block()
        assert 'str(result.status).upper() == "REJECTED"' in block

    def test_the_rejection_branch_leaves_before_the_success_bookkeeping(self):
        """`continue`, not a flag: everything below -- the submitted
        count, the APPROVED audit and the position row -- must not run."""
        block = _submit_block()
        rejected = block.index('== "REJECTED"')
        assert "continue" in block[rejected:]

    def test_no_position_row_is_created_for_a_rejected_order(self):
        """The orphan row is the part that outlived the session: it
        blocked the symbol for the rest of the day and reconciliation
        could not see it, because a row claiming nothing cannot
        disagree with the broker."""
        block = _submit_block()
        rejected = block.index('== "REJECTED"')
        tail = block[rejected:block.index("continue", rejected)]
        assert "record_entry_submission" not in tail

    def test_the_rejection_is_audited_as_blocked_not_approved(self):
        block = _submit_block()
        rejected = block.index('== "REJECTED"')
        tail = block[rejected:block.index("continue", rejected)]
        assert "BROKER_REJECTED" in tail
        assert "RESULT_BLOCKED" in tail
        assert "RESULT_APPROVED" not in tail

    def test_the_brokers_own_reason_is_kept(self):
        """The reason was computed and thrown away: msg_cd and msg1 came
        back on the execution record and were never persisted or logged,
        so every rejection was unexplainable afterwards."""
        block = _submit_block()
        assert "error_code" in block
        assert "error_message" in block


class TestUnknownIsAlsoNotASubmission:
    def test_it_has_its_own_branch(self):
        block = _submit_block()
        assert 'str(result.status).upper() == "UNKNOWN"' in block

    def test_it_keeps_the_position_row(self):
        """The opposite call from REJECTED, for the opposite reason: the
        order may be live, and a held share with no internal row is
        invisible to the exit runtime."""
        block = _submit_block()
        unknown = block.index('== "UNKNOWN"')
        tail = block[unknown:]
        assert "record_entry_submission" in tail

    def test_it_is_not_reported_as_approved(self):
        block = _submit_block()
        unknown = block.index('== "UNKNOWN"')
        tail = block[unknown:block.index("continue", unknown)]
        assert "AMBIGUOUS_RESPONSE" in tail
        assert "RESULT_APPROVED" not in tail

    def test_nothing_re_sends_the_order(self):
        block = _submit_block()
        unknown = block.index('== "UNKNOWN"')
        tail = block[unknown:block.index("continue", unknown)]
        assert "submit_buy_order" not in tail


class TestARefusedOrderCanReachATerminalState:
    """The second half. Even with the caller fixed, a row created by the
    UNKNOWN path (or by an older release) whose order turns out refused
    must be able to resolve -- and it could not."""

    RUNTIME = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text(encoding="utf-8")

    def test_the_ledger_is_consulted_before_the_broker_lookup(self):
        """A refused order has no broker order id, so the fill lookup has
        nothing to ask about and answers "no fills yet" forever. Asking
        after would never change the outcome."""
        block = self.RUNTIME[self.RUNTIME.index("    applied = []"):]
        assert block.index("_order_will_never_fill") < block.index("fill = fills_for(row)")

    def test_only_a_positive_refusal_counts(self):
        """UNKNOWN must never qualify: it is the state that exists
        because nobody knows, and abandoning on it would discard a row
        whose order may be live at the broker."""
        assert '_TERMINAL_LEDGER_STATUSES = ("REJECTED", "CANCELLED")' in self.RUNTIME
        assert "UNKNOWN" not in self.RUNTIME[
            self.RUNTIME.index("_TERMINAL_LEDGER_STATUSES = ("):
            self.RUNTIME.index("def _order_will_never_fill")]

    def test_only_a_submitted_row_may_be_abandoned(self):
        """An OPEN position holds shares; "the order was refused" can
        never be a reason to drop one."""
        assert 'row.get("status") == position_store.SUBMITTED' in self.RUNTIME

    def test_an_unreadable_ledger_abandons_nothing(self):
        """Losing the evidence is not evidence. It must fail towards
        leaving the row alone."""
        body = self.RUNTIME[self.RUNTIME.index("def _order_will_never_fill"):
                            self.RUNTIME.index("def sync_buy_fills")]
        assert "except Exception" in body
        assert body.rstrip().endswith("_TERMINAL_LEDGER_STATUSES") or "return False" in body

    def test_it_is_not_recorded_as_a_trade(self):
        """BUY_NEVER_FILLED, the existing non-trade reason -- so it does
        not enter the realized record and does not trigger the same-day
        re-entry block for a position that never existed."""
        assert 'reason="BUY_NEVER_FILLED"' in self.RUNTIME
        from execution import reentry_policy

        assert "BUY_NEVER_FILLED" in reentry_policy.NON_TRADE_EXIT_REASONS
