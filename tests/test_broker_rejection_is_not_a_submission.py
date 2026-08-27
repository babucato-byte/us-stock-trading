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


class TestTheBTGFixture:
    """§15. The production event, fixed as a regression.

    2026-08-27 18:42 UTC. BTG, READY, sized to 3 shares at $5.805 from
    $20.96 of orderable cash, gate-approved, sent to KIS, refused. KIS's
    own order book for the day held one row -- the DT sell -- so nothing
    reached the account. The internal record disagreed: a position at
    SUBMITTED and a `submitted` count of one.
    """

    SYMBOL = "BTG"
    QTY = 3
    PRICE = 5.805

    def test_a_rejection_creates_no_position_row(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
        from state_store.db import open_db
        from s6_live import position_store

        with open_db() as conn:
            assert list(position_store.load_live(conn)) == []
            assert list(position_store.load_unconfirmed(conn)) == []

    def test_a_refused_order_is_abandoned_not_left_pending(self, tmp_path, monkeypatch):
        """The exact state the three orphans were in: SUBMITTED, quantity
        NULL, an order id that does not exist."""
        from datetime import datetime, timezone

        monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
        from state_store.db import open_db
        from s6_live import exit_runtime, position_store

        now = datetime(2026, 8, 27, 18, 43, tzinfo=timezone.utc)
        with open_db() as conn:
            client_order_id = "kislive-BTG-d04420a1209a"
            pid = position_store.record_submission(
                conn, symbol=self.SYMBOL, variant="S6-R",
                entry_session="REGULAR", client_order_id=client_order_id,
                now=now)
            conn.execute(
                "INSERT INTO kis_order_idempotency (internal_order_id, "
                "signal_id, symbol, side, trading_date, broker_order_id, "
                "status, created_at, updated_at, requested_quantity, version, "
                "strategy_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_order_id, "sig", self.SYMBOL, "buy", "2026-08-27",
                 None, "REJECTED", now.isoformat(), now.isoformat(),
                 float(self.QTY), 1, "S6_ORB_BREAKOUT_V1"))
            conn.commit()

            applied = exit_runtime.sync_buy_fills(
                conn, fills_for=lambda row: None, now=now)

            assert [a["status"] for a in applied] == ["ABANDONED"]
            assert list(position_store.load_live(conn)) == []
            row = conn.execute(
                "SELECT status, exit_reason FROM s6_positions "
                "WHERE position_id = ?", (pid,)).fetchone()
            assert row["status"] == "CLOSED"
            assert row["exit_reason"] == "BUY_NEVER_FILLED"

    def test_a_refused_symbol_is_not_re_sent_the_same_day(self, tmp_path, monkeypatch):
        """§16. The orphan row was what stopped the retry loop, and it
        was a bug. Without a deliberate brake, the fix would have the
        entry re-sending a refused order every minute."""
        from datetime import datetime, timezone

        monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
        from state_store.db import open_db
        from execution import entry_limits

        now = datetime(2026, 8, 27, 18, 43, tzinfo=timezone.utc)
        with open_db() as conn:
            conn.execute(
                "INSERT INTO kis_order_idempotency (internal_order_id, "
                "signal_id, symbol, side, trading_date, broker_order_id, "
                "status, created_at, updated_at, requested_quantity, version, "
                "strategy_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("kislive-BTG-d04420a1209a", "sig", self.SYMBOL, "buy",
                 "2026-08-27", None, "REJECTED", now.isoformat(),
                 now.isoformat(), float(self.QTY), 1, "S6_ORB_BREAKOUT_V1"))
            conn.commit()
            refused = entry_limits._broker_rejected_today(
                conn, trading_day="2026-08-27")
            assert refused == frozenset({"BTG"})

    def test_yesterdays_rejection_does_not_bar_today(self, tmp_path, monkeypatch):
        """The brake is for the day, not forever."""
        from datetime import datetime, timezone

        monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
        from state_store.db import open_db
        from execution import entry_limits

        now = datetime(2026, 8, 27, 18, 43, tzinfo=timezone.utc)
        with open_db() as conn:
            conn.execute(
                "INSERT INTO kis_order_idempotency (internal_order_id, "
                "signal_id, symbol, side, trading_date, broker_order_id, "
                "status, created_at, updated_at, requested_quantity, version, "
                "strategy_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("kislive-BTG-old", "sig", self.SYMBOL, "buy", "2026-08-26",
                 None, "REJECTED", now.isoformat(), now.isoformat(),
                 float(self.QTY), 1, "S6_ORB_BREAKOUT_V1"))
            conn.commit()
            assert entry_limits._broker_rejected_today(
                conn, trading_day="2026-08-27") == frozenset()

    def test_the_brake_is_candidate_specific_not_account_wide(self):
        """§9 and §26: a refused symbol is skipped and the next ranked
        candidate is evaluated. It is not a reason to stop trading."""
        from execution import entry_limits, order_gate

        # In the gate's own sequence, so a report names it -- and BEFORE
        # the capacity caps, so a refused symbol is skipped rather than
        # counted against a slot.
        seq = order_gate.BUY_GATE_SEQUENCE
        assert entry_limits.SYMBOL_REJECTED_TODAY in seq
        assert seq.index(entry_limits.SYMBOL_REJECTED_TODAY) < seq.index(
            entry_limits.MAX_OPEN_POSITIONS)
        # And it is not the account-wide kill switch: that is ENTRY_DISABLED.
        assert seq.index(entry_limits.SYMBOL_REJECTED_TODAY) > seq.index(
            "ENTRY_DISABLED")

    def test_an_unreadable_ledger_does_not_bar_everything(self):
        """Refusing every symbol because a diagnostic query broke would
        be a worse failure than the one it guards against."""
        from execution import entry_limits

        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db gone")

        assert entry_limits._broker_rejected_today(
            Boom(), trading_day="2026-08-27") == frozenset()
