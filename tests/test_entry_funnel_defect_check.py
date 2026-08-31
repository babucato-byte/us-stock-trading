"""The execution-defect check has to survive an empty tick.

READY candidates that reached the gate, were approved, and produced no
order is the one combination that is a defect rather than a market
condition -- so it is worth an ERROR line.

The check had drifted out of the function that computes the counts and
into `_record_shadow_signals`, where `ready` is a per-symbol boolean
from a loop rather than a count. Two things followed. It read the LAST
symbol's readiness instead of how many were ready. And on any tick with
no candidates the loop never ran, so the name was unbound and the check
raised:

    UnboundLocalError: local variable 'ready' referenced before assignment

Which is every tick while discovery is empty. The guard against a silent
execution defect was itself failing silently, once a minute, in
production.
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "run_live_buy_entry", REPO_ROOT / "scripts" / "run_live_buy_entry.py")
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)

SINCE = datetime(2026, 8, 31, 3, 10, tzinfo=timezone.utc)


class _Source:
    def __init__(self, evaluations=None):
        self.evaluations = evaluations or {}


class _Eval:
    def __init__(self, ready):
        self.ready = ready
        self.state = "READY_TO_BUY" if ready else "WATCHING"
        self.blocking = () if ready else ("volume_expansion",)


class TestAnEmptyTickDoesNotRaise:
    def test_no_candidates_at_all(self, caplog):
        """The production failure, exactly: discovery empty, no
        evaluations, and the check must simply not fire."""
        with caplog.at_level("INFO"):
            entry._funnel(_Source(), {"submitted": []}, since=SINCE)
        assert "UnboundLocalError" not in caplog.text
        assert "EXECUTION_DEFECT_SUSPECTED" not in caplog.text

    def test_no_candidates_still_reports_the_funnel(self, caplog):
        with caplog.at_level("INFO"):
            entry._funnel(_Source(), {"submitted": []}, since=SINCE)
        assert "FUNNEL scanned=0" in caplog.text

    def test_the_shadow_recorder_no_longer_carries_the_check(self):
        """It belongs with the counts, not with a per-symbol loop."""
        import inspect

        source = inspect.getsource(entry._record_shadow_signals)
        assert "EXECUTION_DEFECT_SUSPECTED" not in source

    def test_the_funnel_carries_it(self):
        import inspect

        assert "EXECUTION_DEFECT_SUSPECTED" in inspect.getsource(entry._funnel)


class TestItCountsRatherThanReadingTheLastSymbol:
    def test_watching_candidates_alone_are_not_a_defect(self, caplog):
        source = _Source({"AAA": _Eval(False), "BBB": _Eval(False)})
        with caplog.at_level("ERROR"):
            entry._funnel(source, {"submitted": []}, since=SINCE)
        assert "EXECUTION_DEFECT_SUSPECTED" not in caplog.text

    def test_a_ready_candidate_that_produced_an_order_is_not_a_defect(
            self, caplog, monkeypatch):
        monkeypatch.setattr(entry, "_funnel_executable", None, raising=False)
        source = _Source({"AAA": _Eval(True)})
        with caplog.at_level("ERROR"):
            entry._funnel(source, {"submitted": ["AAA"]}, since=SINCE)
        assert "EXECUTION_DEFECT_SUSPECTED" not in caplog.text

    def test_the_count_is_the_number_ready_not_the_last_one(self, caplog):
        """With the old loop-variable, a set ending in a NOT-ready symbol
        reported ready=False however many were ready."""
        source = _Source({"AAA": _Eval(True), "ZZZ": _Eval(False)})
        with caplog.at_level("INFO"):
            entry._funnel(source, {"submitted": []}, since=SINCE)
        assert "ready=1" in caplog.text
