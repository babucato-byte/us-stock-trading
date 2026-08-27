"""ACCEPTED, REJECTED and UNKNOWN are all handled, on both sides.

§11. `execution_engine.submit_buy_order()` and `submit_sell_order()`
PERSIST the broker's answer and RETURN it. They raise when something
fails on the way -- a blocked gate, an ambiguous response, a state that
could not be recorded -- and NOT when the broker simply says no.

"No exception" therefore does not mean "the order is live", and the
whole 2026-08-27 incident is what that assumption costs: three refused
buys booked as approved, each leaving a position the account never held.

The SELL path already had this right, and the contrast is worth keeping
visible: `KISBrokerAdapter` compares `result.status == "ACCEPTED"` and
turns anything else into a 400, which `s1_live.exit_runtime` then treats
as a rejection -- aborting the intent, latching the trigger, and leaving
the position exactly where it was.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENGINE = (REPO_ROOT / "execution" / "execution_engine.py").read_text(encoding="utf-8")
ADAPTER = (REPO_ROOT / "brokers" / "kis_broker_adapter.py").read_text(encoding="utf-8")
S1_EXIT = (REPO_ROOT / "s1_live" / "exit_runtime.py").read_text(encoding="utf-8")
BUY = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")


class TestTheContractIsExplicitOnBothSides:
    def test_the_buy_caller_names_all_three_outcomes(self):
        block = BUY[BUY.index("result = execution_engine.submit_buy_order("):]
        head = block[:block.index('results["submitted"].append(symbol)')]
        assert '"REJECTED"' in head
        assert '"UNKNOWN"' in head

    def test_the_sell_adapter_compares_against_accepted(self):
        assert 'result.status == "ACCEPTED"' in ADAPTER

    def test_a_rejected_sell_becomes_a_non_2xx(self):
        assert '200 if result.status == "ACCEPTED" else 400' in ADAPTER


class TestARejectedSellKeepsThePosition:
    def test_the_intent_is_aborted_and_the_trigger_stays_latched(self):
        """A refused SELL leaves shares still held, so the exit must
        remain owed -- aborted as an attempt, latched as an intention."""
        block = S1_EXIT[S1_EXIT.index("if not accepted:"):]
        head = block[:block.index("exit_intent_ledger.mark_submitted(")]
        assert "mark_aborted" in head
        assert "latch_pending_exit" in head

    def test_it_is_not_marked_submitted(self):
        """`mark_submitted` on a refused sell would make the ledger claim
        an order that does not exist -- the sell-side twin of the buy
        defect."""
        block = S1_EXIT[S1_EXIT.index("if not accepted:"):
                        S1_EXIT.index("exit_intent_ledger.mark_submitted(")]
        assert "mark_submitted" not in block

    def test_it_is_not_retried_in_a_loop(self):
        block = S1_EXIT[S1_EXIT.index("if not accepted:"):
                        S1_EXIT.index("exit_intent_ledger.mark_submitted(")]
        assert "return ExitOutcome" in block

    def test_an_ambiguous_sell_goes_to_submission_unknown(self):
        """Never auto-retried: the order may be live."""
        assert "mark_submission_unknown" in S1_EXIT


class TestNoExceptionIsNotAcceptance:
    def test_the_engine_returns_the_status_rather_than_raising_it(self):
        tail = ENGINE[ENGINE.index("_notify_submitted(order_intent"):]
        assert "return ExecutionResult(" in tail
        assert "status=execution_record.status" in tail

    def test_the_buy_caller_no_longer_assumes_success(self):
        """It used to reach the success bookkeeping directly after the
        call, with nothing between."""
        block = BUY[BUY.index("result = execution_engine.submit_buy_order("):]
        head = block[:block.index('results["submitted"].append(symbol)')]
        assert "str(result.status).upper()" in head
