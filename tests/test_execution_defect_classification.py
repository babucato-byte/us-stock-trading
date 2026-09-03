"""A tick that sent nothing, and what kind of nothing it was.

2026-09-02, 16:34:52. The entry funnel reported:

    EXECUTION_DEFECT_SUSPECTED ready=12 executable=1 submitted=0
    -- the gate approved an order that was never submitted

It had not. The single approval the count found was
`s6exit-HBAN-4bd8f7bb86f3` -- an exit SELL, approved by the exit runtime
at 16:24:52, which fell inside the window because that BUY cycle had
started at 16:24:07 and was still running ten minutes later. The query
filtered on `event_type` alone: no side, no symbol, no strategy.

Entry analysis no longer holds the execution lock, so cycles legitimately
run for minutes and that window is wide. Every exit approved during one
would have raised this.

The second half is severity. An entry that yields to an exit, or drops a
candidate on current evidence, is the system working -- and it was
raising the loudest line in the entry path. A warning that cries on
ordinary Tuesdays is not read by Thursday.
"""

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
for path in (str(REPO_ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import run_live_buy_entry as entry  # noqa: E402


def _blocked(*reasons):
    return {"submitted": [], "skipped": [],
            "blocked": [(f"SYM{i}", r) for i, r in enumerate(reasons)]}


class TestExpectedContentionIsNotADefect:
    def test_yielding_to_execution_access_is_expected(self):
        label, level, _ = entry._classify_no_submission(
            _blocked("execution access is held by another cycle; this entry "
                     "was dropped before submission"), executable=0)
        assert label == entry.EXPECTED_CONTENTION
        assert level == logging.INFO

    def test_it_is_not_the_defect_label(self):
        label, _l, _d = entry._classify_no_submission(
            _blocked("execution access is held by another cycle"), executable=0)
        assert label != entry.REAL_EXECUTION_DEFECT


class TestExpectedDeferralIsNotADefect:
    @pytest.mark.parametrize("reason", [
        "an S6 exit reached the broker while this entry was being prepared",
        "ENTRY_OFF was set while this entry was being prepared",
        "operations HALT was set while this entry was being prepared",
        "HBAN became live in the canonical store (OPEN) while this entry "
        "was being prepared",
        "insufficient KIS orderable cash for even 1 share",
        "symbol not in live_rollout.allowed_symbols",
    ])
    def test_an_expected_decline_is_informational(self, reason):
        label, level, _ = entry._classify_no_submission(
            _blocked(reason), executable=0)
        assert label == entry.EXPECTED_DEFERRAL
        assert level == logging.INFO

    def test_exit_priority_defer_does_not_emit_a_defect(self, caplog):
        """The rule the system is built around: an exit outranks an entry."""
        with caplog.at_level(logging.DEBUG):
            label, level, _ = entry._classify_no_submission(
                _blocked("an S6 exit reached the broker while this entry "
                         "was being prepared"), executable=0)
        assert label != entry.REAL_EXECUTION_DEFECT
        assert level < logging.WARNING


class TestARealDefectStaysVisible:
    def test_an_unexplained_approval_is_a_defect(self):
        """Approved, never sent, and the broker never answered."""
        label, level, detail = entry._classify_no_submission(
            _blocked("something nobody recognises"), executable=1)
        assert label == entry.REAL_EXECUTION_DEFECT
        assert level == logging.ERROR
        assert "never submitted" in detail

    def test_a_broker_rejection_accounts_for_its_own_approval(self):
        """A rejection is reported by BROKER_REJECTED. It is an answer,
        not a disappearance."""
        label, level, _ = entry._classify_no_submission(
            _blocked("KIS rejected the order (code='APTR0057': bad price)"),
            executable=1)
        assert label != entry.REAL_EXECUTION_DEFECT
        assert level == logging.INFO

    def test_an_unknown_broker_answer_also_accounts_for_it(self):
        label, _l, _d = entry._classify_no_submission(
            _blocked("KIS did not confirm the order; left UNKNOWN for "
                     "reconciliation"), executable=1)
        assert label != entry.REAL_EXECUTION_DEFECT

    def test_two_approvals_and_one_rejection_still_leaves_a_defect(self):
        label, level, detail = entry._classify_no_submission(
            _blocked("KIS rejected the order (code='X')",
                     "execution access is held by another cycle"),
            executable=2)
        assert label == entry.REAL_EXECUTION_DEFECT
        assert level == logging.ERROR
        assert "1 order" in detail, "only the unaccounted one is the defect"

    def test_an_unrecognised_reason_stays_visible_without_crying_defect(self):
        label, level, _ = entry._classify_no_submission(
            _blocked("some new refusal nobody has classified yet"),
            executable=0)
        assert label == entry.EXPECTED_DEFERRAL
        assert level == logging.WARNING, (
            "unknown is not expected, and not proof of a defect either")


class TestTheCountIsScoped:
    SOURCE = (REPO_ROOT / "scripts/run_live_buy_entry.py").read_text()

    def test_only_buy_approvals_are_counted(self):
        assert "side = 'buy'" in self.SOURCE, (
            "an exit SELL approval is what produced the false positive")

    def test_only_this_funnels_own_symbols_are_counted(self):
        assert "symbol IN" in self.SOURCE, (
            "another strategy's approved buy is not this funnel's")

    def test_the_window_is_still_this_cycle(self):
        assert "created_at >= ?" in self.SOURCE


class TestReportingCannotAffectTrading:
    def test_a_classifier_failure_cannot_reach_the_cycle(self):
        """`_funnel` is called inside a try/except that swallows, after
        the orders are already placed."""
        source = (REPO_ROOT / "scripts/run_live_buy_entry.py").read_text()
        block = source[source.index("_funnel(source, results, since=now)"):]
        assert "except Exception" in block[:400]
        assert "return results" in block[:800]

    def test_classification_places_no_orders(self):
        """Checked on the CALLS it makes, not on words in its prose --
        the docstring legitimately contains "cancels" while explaining
        that it cancels nothing."""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(entry._classify_no_submission)))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    called.add(fn.attr)
                elif isinstance(fn, ast.Name):
                    called.add(fn.id)
        forbidden = {"submit_order", "submit_buy_order", "submit_sell_order",
                     "cancel_order", "execute", "executemany", "commit",
                     "close_position", "mark_exit_submitted"}
        assert not (called & forbidden), (
            f"the classifier must only classify; it calls {called & forbidden}")

    def test_classification_reads_no_state(self):
        """It is handed the results dict and a number. Nothing else."""
        import inspect

        sig = inspect.signature(entry._classify_no_submission)
        assert list(sig.parameters) == ["results", "executable"]
