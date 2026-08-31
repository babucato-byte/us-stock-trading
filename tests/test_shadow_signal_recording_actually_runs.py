"""The shadow signal log has to actually record something.

It never did. `_record_shadow_signals` imported `s6_sessions` from
`s6_live`, where it does not live -- it is in `config`. Every call
raised ImportError into the surrounding `except Exception`, which logged
"could not record shadow signals" at WARNING and moved on. The cycle
reported success, the log line was never read, and the
`shadow_signals/` directory on the host stayed empty from the day the
feature shipped.

The closed-bar comparison was then wired into the same try block, after
that import, so it never ran either: one wrong import silently cost two
independent observations. They are in separate blocks now.

These tests import the module for real, because that is the only thing
that would have caught it -- every unit test of the log itself passed,
since the log was fine. What was broken was the one line that calls it.
"""

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "run_live_buy_entry", REPO_ROOT / "scripts" / "run_live_buy_entry.py")
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)


class TestTheImportsResolve:
    """Asserted by importing, not by reading the source. A string check
    would have been satisfied by the broken line too."""

    def test_s6_sessions_is_importable_from_where_the_code_asks(self):
        from config import s6_sessions

        assert s6_sessions.STRATEGY_ID == "S6_ORB_BREAKOUT_V1"

    def test_it_is_not_in_s6_live(self):
        """The module the broken import named."""
        with pytest.raises(ImportError):
            from s6_live import s6_sessions  # noqa: F401

    def test_the_recorder_asks_for_the_right_one(self):
        source = inspect.getsource(entry._record_shadow_signals)
        assert "from config import s6_sessions" in source
        assert "from s6_live import s6_sessions" not in source


class TestOneObservationCannotKillTheOther:
    def test_the_closed_bar_comparison_has_its_own_block(self):
        """It used to sit after the failing import, so a single wrong
        import cost two unrelated observations."""
        source = inspect.getsource(entry._record_shadow_signals)
        assert source.count("except Exception") >= 2

    def test_a_broken_shadow_log_does_not_stop_the_comparison(self, monkeypatch):
        from s6_live import shadow_signal_log

        monkeypatch.setattr(
            shadow_signal_log, "append",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        called = {}
        monkeypatch.setattr(entry, "_record_closed_bar_shadow",
                            lambda *a, **k: called.setdefault("yes", True))

        class _Src:
            evaluations = {}

        from datetime import datetime, timezone

        entry._record_shadow_signals(
            _Src(), {"submitted": [], "blocked": [], "skipped": []},
            since=datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc))
        assert called.get("yes") is True

    def test_neither_failure_raises_out_of_the_recorder(self, monkeypatch):
        """A finished trading cycle must not be undone by bookkeeping."""
        from datetime import datetime, timezone

        monkeypatch.setattr(
            entry, "_record_closed_bar_shadow",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        class _Src:
            evaluations = {}

        entry._record_shadow_signals(
            _Src(), {"submitted": [], "blocked": [], "skipped": []},
            since=datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc))


class TestItWritesWhenGivenSomewhereToWrite:
    def test_a_candidate_produces_a_shadow_row(self, tmp_path, monkeypatch):
        """End to end through the real recorder -- the check that would
        have caught an empty directory."""
        from datetime import datetime, timezone

        monkeypatch.setenv("SHADOW_SIGNAL_DIR", str(tmp_path))

        class _Eval:
            ready = False
            state = "WATCHING"
            blocking = ("volume_expansion",)
            features = None

        class _Src:
            evaluations = {"OWL": _Eval()}

        since = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)
        entry._record_shadow_signals(
            _Src(), {"submitted": [], "blocked": [], "skipped": []},
            since=since)

        from market_hours import us_trading_day
        from s6_live import shadow_signal_log

        rows = shadow_signal_log.read(us_trading_day(since),
                                      env={"SHADOW_SIGNAL_DIR": str(tmp_path)})
        assert [r["symbol"] for r in rows] == ["OWL"]
        assert rows[0]["outcome"] == shadow_signal_log.OUTCOME_NOT_READY
