"""Reconciliation runs on a schedule, and a lock collision is not a fault.

Two things the first autonomous lifecycle exposed.

Nothing ran reconciliation automatically -- no cron, no timer, no
wrapper. S6's OWL trade completed correctly on 2026-08-28 and both legs
sat at ACCEPTED for two hours until a manual pass matched them against
KIS fill history. The position lifecycle was never affected
(`sync_buy_fills` moves positions); what had no scheduler was settling
the ORDER LEDGER, and an order stuck at ACCEPTED that is really filled
is the condition that once blocked every buy for a week.

And three cycles recorded SHADOW_ERROR / UNEXPECTED for what was
actually two processes reaching the order idempotency lock together --
transient, ordinary, and nothing to do with the candidate.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WRAPPER = (REPO_ROOT / "deploy" / "cron" / "reconciliation.sh").read_text(
    encoding="utf-8")
CYCLE = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")


class TestReconciliationHasAScheduler:
    def test_the_wrapper_exists_and_runs_the_entrypoint(self):
        assert "run_reconciliation.py" in WRAPPER

    def test_it_runs_verified_release_code(self):
        assert "resolve_release_root || exit 1" in WRAPPER
        code = "\n".join(l for l in WRAPPER.splitlines()
                         if not l.strip().startswith("#"))
        assert "/home/ubuntu/trading" not in code

    def test_it_uses_its_own_lock_not_the_s6_one(self):
        """Reconciliation is account-wide -- it settles S1's orders as
        much as S6's -- so sharing a per-strategy lock would let one
        strategy's busy cycle delay the safety net for every other."""
        assert "reconciliation.lock" in WRAPPER
        assert "s6_exec.lock" not in WRAPPER

    def test_overlap_is_skipped_and_distinguishable(self):
        assert "flock -n -E 99" in WRAPPER
        assert "OVERLAP_SKIPPED" in WRAPPER

    def test_it_is_labelled_for_the_lock_telemetry(self):
        assert "KIS_LOCK_OWNER=RECONCILIATION" in WRAPPER

    def test_a_kis_read_outage_is_not_escalated_as_a_crash(self):
        """Exit 2 is a defined outcome -- "KIS could not be read, nothing
        recorded" -- and a transient one. The reconciliation freshness
        check is what notices a persistent outage."""
        assert '[ "$STATUS" -eq 2 ] && exit 0' in WRAPPER

    def test_the_cadence_reasoning_is_written_down(self):
        """Five minutes rather than one, because a pass takes sixty to
        ninety seconds of shared KIS reads and a one-minute cron would be
        continuous broker traffic -- the shape that starved S1."""
        assert "Five minutes, not one" in WRAPPER


class TestALockCollisionIsNotAnUnexpectedError:
    def test_it_has_its_own_class(self):
        from execution import idempotency

        assert hasattr(idempotency, "IdempotencyLockBusy")

    def test_it_still_satisfies_existing_handlers(self):
        """A narrower class that escaped the old `except IdempotencyError`
        would be a behaviour change dressed as a classification."""
        from execution import idempotency

        assert issubclass(idempotency.IdempotencyLockBusy,
                          idempotency.IdempotencyError)

    def test_the_cycle_classifies_it_before_the_catch_all(self):
        busy = CYCLE.index("except idempotency.IdempotencyLockBusy")
        catch_all = CYCLE.index("except Exception as exc:  # noqa: BLE001 -- audited")
        assert busy < catch_all

    def test_it_is_recorded_as_blocked_not_error(self):
        block = CYCLE[CYCLE.index("except idempotency.IdempotencyLockBusy"):]
        head = block[:block.index("except Exception")]
        assert "RESULT_BLOCKED" in head
        assert "RESULT_ERROR" not in head
        assert "IDEMPOTENCY_LOCK_BUSY" in head

    def test_the_candidate_is_not_burned(self):
        """It says nothing about the candidate, so the next tick
        re-evaluates it -- no rejection is recorded against the symbol."""
        block = CYCLE[CYCLE.index("except idempotency.IdempotencyLockBusy"):]
        head = block[:block.index("except Exception")]
        assert "SYMBOL_REJECTED_TODAY" not in head
        assert "re-evaluates" in head

    def test_the_module_is_actually_imported(self):
        """It was not, so evaluating the handler raised NameError for
        every exception that reached that level -- which broke an
        unrelated broker-error path rather than the one being added."""
        assert "from execution import entry_limits, execution_engine, idempotency, order_gate" in CYCLE
