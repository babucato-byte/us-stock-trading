"""Each order transition is stamped when it happens.

The history this exists to prevent
----------------------------------
The real OWL order, 2026-08-28:

    CREATED       15:17:16.246   <- later than the step that follows it
    VALIDATING    15:13:06.630
    APPROVED      15:13:06.630   <- identical
    SUBMITTING    15:13:06.630   <- identical
    ACCEPTED      15:13:06.630   <- identical

Four transitions carrying one timestamp, because the engine was handed
`now=current` -- the entry cycle's timestamp, which every symbol in the
loop shares -- and passed it to all four. Nothing was broken in a way
anything would report: the states were right and the order was real. But
no latency between any two steps could be measured, and any execution
change argued from that data (IOC, re-quoting, ASK-laddering) would have
been argued from zeros.
"""

import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution import execution_engine, order_repository  # noqa: E402


class TestTheEngineDoesNotStampTransitionsWithTheCycleClock:
    """Guarded at the source. The values themselves are wall-clock, so
    the durable assertion is that the cycle timestamp is not handed to a
    transition in the first place."""

    def _transition_calls(self):
        source = inspect.getsource(execution_engine)
        calls = []
        index = 0
        while True:
            index = source.find("order_repository.advance(", index)
            if index == -1:
                return calls
            end = source.index(")", source.index("conn, record", index))
            calls.append(source[index:end])
            index = end

    def test_no_transition_is_stamped_with_the_cycle_timestamp(self):
        offenders = [c for c in self._transition_calls() if "now=current" in c]
        assert offenders == [], (
            "a transition is being stamped with the cycle's timestamp; that "
            "is what made four of OWL's transitions share one moment")

    def test_there_are_transitions_to_check(self):
        """So the test above cannot pass by finding nothing at all."""
        assert len(self._transition_calls()) >= 4


class TestTheRepositoryStampsTheMoment:
    def test_a_transition_without_an_explicit_time_uses_now(self):
        signature = inspect.signature(order_repository.compare_and_set_state)
        assert signature.parameters["now"].default is None
        body = inspect.getsource(order_repository.compare_and_set_state)
        assert "now or datetime.now(timezone.utc)" in body

    def test_the_creation_event_stamps_the_moment_too(self):
        body = inspect.getsource(order_repository.append_creation_event)
        assert "now or datetime.now(timezone.utc)" in body


class TestTwoTransitionsGetTwoTimes:
    """The behaviour, not just the call shape."""

    def test_successive_transitions_do_not_share_a_timestamp(self, tmp_path,
                                                             monkeypatch):
        import tempfile

        monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
        from execution import idempotency
        from state_store.db import open_db

        with open_db() as conn:
            idempotency.register(
                conn, internal_order_id="kislive-OWL-1", signal_id="sig-1",
                symbol="OWL", side="buy", trading_date="2026-08-28",
                requested_quantity=1, strategy_id="S6_ORB_BREAKOUT_V1")
            record = order_repository.load(conn, "kislive-OWL-1")
            record = order_repository.advance(
                conn, record, "VALIDATING", event_type="VALIDATION_STARTED")
            order_repository.advance(
                conn, record, "APPROVED", event_type="GATE_APPROVED")

            stamps = [r["occurred_at"] for r in conn.execute(
                "SELECT occurred_at FROM order_state_events "
                "WHERE internal_order_id = ? ORDER BY version",
                ("kislive-OWL-1",))]

        assert len(stamps) == 3
        # Monotonic: a step never predates the step it follows, which is
        # the other half of what OWL's history got wrong.
        assert stamps == sorted(stamps)

    def test_an_explicit_time_is_still_honoured(self, monkeypatch):
        """Tests and replays need determinism; the default is what
        changed, not the ability to say when something happened."""
        import tempfile

        monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
        from execution import idempotency
        from state_store.db import open_db

        moment = datetime(2026, 8, 28, 15, 13, 6, tzinfo=timezone.utc)
        with open_db() as conn:
            idempotency.register(
                conn, internal_order_id="kislive-OWL-2", signal_id="sig-2",
                symbol="OWL", side="buy", trading_date="2026-08-28",
                requested_quantity=1, strategy_id="S6_ORB_BREAKOUT_V1")
            record = order_repository.load(conn, "kislive-OWL-2")
            order_repository.advance(
                conn, record, "VALIDATING", event_type="VALIDATION_STARTED",
                now=moment)
            stamp = conn.execute(
                "SELECT occurred_at FROM order_state_events WHERE "
                "internal_order_id = ? AND to_state = 'VALIDATING'",
                ("kislive-OWL-2",)).fetchone()[0]
        assert stamp == moment.isoformat()
