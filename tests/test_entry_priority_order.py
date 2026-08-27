"""New entries are the lowest-priority use of the broker.

The priority the 2026-08-27 incident established, in order: an exit or
emergency first, then management of an existing position, then
reconciliation and fill sync, then cancels, and only then a new entry.

Missing an entry costs an opportunity. Delaying the management of a real
holding cost, that day, S1's executor two fifteen-minute ticks while it
held TX, and then its watchdog disabled entries for every strategy. The
asymmetry is the point: the two failures are not comparable, so the
entry is the one that yields.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RUNNER = (REPO_ROOT / "scripts" / "run_live_buy_entry.py").read_text(encoding="utf-8")


class TestAnExitOutranksAnEntry:
    def test_the_tick_defers_when_an_exit_is_in_flight(self):
        assert "ENTRY_DEFERRED_EXIT_PENDING" in RUNNER
        assert "_exit_in_flight()" in RUNNER

    def test_the_check_runs_before_any_candidate_work(self):
        """After the kill-switch refusal and before the cycle is
        started, so an entry never takes the shared lock ahead of an
        exit."""
        main = RUNNER[RUNNER.index("def main(argv=None):"):]
        assert main.index("if _exit_in_flight():") < main.index(
            "run_once(strategy=args.strategy)")

    def test_it_costs_no_broker_call_to_ask(self):
        """A check that spent the budget it protects would be
        self-defeating."""
        body = RUNNER[RUNNER.index("def _exit_in_flight"):
                      RUNNER.index("def run_once(")]
        assert "position_store" in body
        for forbidden in ("KISBroker", "get_positions", "get_open_orders",
                          "get_orderable_usd"):
            assert forbidden not in body, forbidden

    def test_both_pending_and_submitted_exits_count(self):
        """An exit decided but not yet sent is still an exit that must
        not queue behind an entry."""
        body = RUNNER[RUNNER.index("def _exit_in_flight"):
                      RUNNER.index("def run_once(")]
        assert "exit_submitted" in body
        assert "pending_exit_reason" in body

    def test_an_unreadable_store_does_not_block_entries(self):
        """The stronger refusals live in the gate and the runtime. Losing
        this diagnostic must not stop trading on its own."""
        body = RUNNER[RUNNER.index("def _exit_in_flight"):
                      RUNNER.index("def run_once(")]
        assert "except Exception" in body
        assert "return False" in body.split("except Exception")[1]

    def test_deferring_is_not_a_failure(self):
        block = RUNNER[RUNNER.index("if _exit_in_flight():"):]
        assert "return EXIT_OK" in block[:600]


class TestDeferringIsNeverQueueing:
    def test_neither_defer_path_waits(self):
        for marker in ("ENTRY_DEFERRED_EXIT_PENDING",
                       "ENTRY_DEFERRED_KIS_BUSY"):
            assert marker in RUNNER
        assert "dropped rather than queued" in RUNNER
        assert "dropped, not queued" in RUNNER

    def test_the_wrapper_also_drops_an_overlapping_tick(self):
        wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_buy_entry.sh").read_text(
            encoding="utf-8")
        assert "flock -n -E 99" in wrapper
        assert "OVERLAP_SKIPPED" in wrapper


class TestTheEntryStandsDownBeforeItCanStarveS1:
    """§7. The invariant that S1's tick age must not climb toward the
    watchdog threshold while the entry cron runs -- enforced, not
    observed. The lock is fair now and the entry yields on contention,
    so this should never fire; "should never" is an argument, and this
    is a measurement."""

    def test_the_stand_down_threshold_is_well_under_the_watchdog(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_entry", REPO_ROOT / "scripts" / "run_live_buy_entry.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import run_s1_position_watchdog as watchdog

        assert entry.S1_SILENCE_STAND_DOWN_MINUTES < watchdog.DEFAULT_MAX_SILENCE_MINUTES
        assert entry.S1_SILENCE_STAND_DOWN_MINUTES <= (
            watchdog.DEFAULT_MAX_SILENCE_MINUTES / 2), (
            "there must be a real recovery window between the entry getting "
            "out of the way and the account-wide stop")

    def test_it_reads_the_same_log_the_watchdog_reads(self):
        """Two definitions of "quiet" could disagree, and the one that
        matters is the one that stops trading."""
        assert "newest_tick_at" in RUNNER
        assert "ticks_expected_now" in RUNNER

    def test_silence_outside_the_session_is_not_falling_behind(self):
        """S1 is not due to tick outside REGULAR, so overnight silence
        must not stand the entry down for a whole session."""
        body = RUNNER[RUNNER.index("def _s1_is_falling_behind"):
                      RUNNER.index("def _exit_in_flight")]
        assert "ticks_expected_now()" in body
        assert "return False" in body.split("ticks_expected_now()")[1][:200]

    def test_no_tick_yet_today_is_not_falling_behind(self):
        body = RUNNER[RUNNER.index("def _s1_is_falling_behind"):
                      RUNNER.index("def _exit_in_flight")]
        assert "newest is None" in body

    def test_it_costs_no_broker_call(self):
        body = RUNNER[RUNNER.index("def _s1_is_falling_behind"):
                      RUNNER.index("def _exit_in_flight")]
        for forbidden in ("KISBroker", "get_positions", "get_orderable_usd"):
            assert forbidden not in body, forbidden

    def test_it_runs_before_the_cycle(self):
        main = RUNNER[RUNNER.index("def main(argv=None):"):]
        assert main.index("_s1_is_falling_behind()") < main.index(
            "run_once(strategy=args.strategy)")

    def test_a_missing_measurement_does_not_decide_trading_either_way(self):
        body = RUNNER[RUNNER.index("def _s1_is_falling_behind"):
                      RUNNER.index("def _exit_in_flight")]
        assert "except Exception" in body
        assert "return False" in body.split("except Exception")[1]
