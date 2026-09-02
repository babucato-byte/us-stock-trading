"""TCN-02A: reconciliation state must not, on its own, trap a position.

End to end through the real path: an S6 row in `s6_positions`, the
shared `_submit_sell`, the real `KISBrokerAdapter` over a fake KIS
broker, the real Execution Engine, the real snapshot and the real gate.
Only the wire is faked.

The scenarios are the ones the TCN-02A brief lists, A through J:

    A  CLEAN                    -> BUY allowed, valid EXIT allowed
    B  POSITION_MISMATCH        -> BUY blocked
    C  POSITION_MISMATCH + complete evidence -> protective EXIT proceeds
    D  POSITION_MISMATCH + broker qty 0      -> no SELL; external-close
                                                candidate
    E  BROKER_READ_FAILURE      -> BUY blocked, blind SELL forbidden
    F  SUBMISSION_UNKNOWN       -> no duplicate SELL
    G  existing pending SELL    -> no second SELL
    H  external close           -> local OPEN closed exactly once
    I  repeated ticks           -> zero duplicate broker submissions
    J  the strict policy is unchanged where no evidence is supplied
"""

from datetime import datetime, timedelta, timezone

import pytest

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from brokers.kis_broker_adapter import KISBrokerAdapter
from domain.account_snapshot import AccountSnapshot
from domain.execution_event import ExecutionRecord
from domain.position import Position
from execution import execution_engine, idempotency, order_gate
from execution.execution_engine import ExecutionEngineError
from reconciliation import external_close, external_close_service
from reconciliation import snapshot as reconciliation_snapshot
from reconciliation import state as reconciliation_state
from s6_live import exit_runtime as er
from s6_live import position_store as ps
from state_store import db as state_db
from state_store import exit_intent_ledger
import entry_limit_fixtures

# 11:00 ET on a weekday: the REGULAR session, where every KIS route has
# live evidence and `session_capability.exit_session` resolves.
NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"
SYMBOL = "AAPL"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECONCILIATION_STATE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.delenv("RECONCILIATION_MAX_AGE_SECONDS", raising=False)
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    yield


@pytest.fixture
def conn():
    connection = state_db.open_db()
    yield connection
    connection.close()


def _kis_position(symbol=SYMBOL, qty=1):
    return Position(symbol=symbol, quantity=qty, average_fill_price=95.0,
                    unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis_balance")


class FakeKIS:
    """The wire. Everything else in the test is the real code."""

    def __init__(self, positions=None, open_orders=None, fills=None,
                 read_exc=None, submit_raise=None):
        self.positions = positions if positions is not None else [_kis_position()]
        self.open_orders = open_orders or []
        self.fills = fills or []
        self.read_exc = read_exc
        self.submit_raise = submit_raise
        self.submit_calls = []
        self.price = 100.0

    def _read(self, value):
        if self.read_exc is not None:
            raise self.read_exc
        return value

    def get_current_price(self, instrument):
        return self.price

    def get_positions(self):
        return self._read(self.positions)

    def get_open_orders(self):
        return self._read(self.open_orders)

    def get_fills(self, *, start_date, end_date):
        return self._read(self.fills)

    def get_account_snapshot(self, *, source_label="kis_balance"):
        return AccountSnapshot(
            krw_cash=0.0, usd_cash=10000.0, usd_orderable_cash=10000.0,
            usd_reserved_in_open_orders=0.0, as_of=NOW, source=source_label,
            account_id=ACCOUNT_ID)

    def submit_order(self, order_intent, instrument, *, authorization=None,
                     bootstrap_capability=None):
        self.submit_calls.append(order_intent)
        if self.submit_raise is not None:
            raise self.submit_raise
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=f"kis-{len(self.submit_calls)}",
            requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=NOW,
            updated_at=NOW)


def _open_s6(conn, symbol=SYMBOL, qty=1):
    pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                               entry_session="REGULAR", range_high=99.5,
                               range_low=99.0, entry_volume_expansion=2.0, now=NOW)
    ps.open_from_fill(conn, pid, quantity=qty, average_fill_price=100.0,
                      venue="NASD", now=NOW)
    return pid


def _adapter(kis):
    return KISBrokerAdapter(kis, now_fn=lambda: NOW)


def _latch_and_retry(conn, kis, pid, reason="RANGE_REENTRY"):
    """The S6 runtime's own path for a latched exit: `retry_latched_exits`
    -> `_submit_sell` -> adapter -> engine -> gate -> wire."""
    ps.latch_pending_exit(conn, pid, reason, now=NOW)
    return er.retry_latched_exits(conn, broker_adapter=_adapter(kis),
                                  session="REGULAR", now=NOW, orders_allowed=True)


def _buy_intent():
    from domain.order_intent import OrderIntent

    return OrderIntent(
        internal_order_id="buy-1", signal_id="sig-1", strategy_id="S6_ORB_BREAKOUT_V1",
        symbol="MSFT", exchange="NASDAQ", side="buy", quantity=1, order_type="limit",
        limit_price=100.0, stop_price=95.0, target_price=110.0, created_at=NOW)


def _submit_buy(conn, kis):
    from domain.instrument import build_instrument
    from domain.signal import build_signal
    import shadow_audit

    intent = _buy_intent()
    signal = build_signal(
        strategy_id="S6_ORB_BREAKOUT_V1", strategy_version="v1", config_version="cfg-1",
        code_commit="abc", symbol="MSFT", exchange="NASDAQ", signal_price=100.0,
        score=90.0, entry_reason="breakout", valid_for_seconds=300, now=NOW)

    def _ctx(reconciliation):
        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=True, entry_disabled=False,
            validated_commit="c1", deployed_commit="c1", kis_account_no=ACCOUNT_ID,
            allowed_account_no=ACCOUNT_ID, order_intent=intent,
            instrument=build_instrument("MSFT", exchange="NASDAQ"), signal=signal,
            is_regular_session=True, kis_price_usd=100.0, max_price_deviation_percent=0.30,
            usd_orderable_cash=1000.0, has_open_order_for_symbol=False,
            has_order_for_signal_id=False, allowed_symbols=None,
            reconciliation=reconciliation, entry_limits=entry_limit_fixtures.unlimited(),
            now=NOW)

    return execution_engine.submit_buy_order(
        order_intent=intent, buy_gate_context_builder=_ctx, conn=conn, broker=kis,
        instrument=build_instrument("MSFT", exchange="NASDAQ"), account_id=ACCOUNT_ID,
        audit_run_id=shadow_audit.new_run_id(), now=NOW)


def _classify(conn, kis):
    try:
        snap = reconciliation_snapshot.build_snapshot(
            broker=kis, conn=conn, account_id=ACCOUNT_ID, now=NOW)
    except reconciliation_snapshot.ReconciliationUnavailableError as exc:
        return reconciliation_state.classify_failure(exc)
    return reconciliation_state.classify_snapshot(snap, account_id=ACCOUNT_ID, now=NOW)


# ---------------------------------------------------------------- A
class TestA_Clean:
    def test_buy_allowed_and_exit_allowed(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS()
        assert _classify(conn, kis).primary == reconciliation_state.CLEAN

        assert _submit_buy(conn, kis).status == "ACCEPTED"
        assert len(kis.submit_calls) == 1

        outcomes = _latch_and_retry(conn, kis, pid)
        assert outcomes[0]["action"] == "SOLD", outcomes
        assert len(kis.submit_calls) == 2
        assert kis.submit_calls[-1].side == "sell"
        assert ps.load(conn, pid)["status"] == ps.EXIT_SUBMITTED


# ---------------------------------------------------------------- B
class TestB_PositionMismatchBlocksBuy:
    def test_untracked_broker_holding_blocks_new_buy(self, conn):
        _open_s6(conn)
        kis = FakeKIS(positions=[_kis_position(), _kis_position("ZZZ", 3)])
        assert _classify(conn, kis).primary == reconciliation_state.POSITION_MISMATCH

        with pytest.raises(ExecutionEngineError) as exc:
            _submit_buy(conn, kis)
        assert exc.value.reason_code == execution_engine.REASON_RECONCILIATION_DIRTY
        assert kis.submit_calls == []


# ---------------------------------------------------------------- C
class TestC_ProtectiveExitUnderMismatch:
    def test_mismatch_elsewhere_does_not_trap_a_fill_backed_position(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(positions=[_kis_position(), _kis_position("ZZZ", 3)])
        assert _classify(conn, kis).primary == reconciliation_state.POSITION_MISMATCH

        outcomes = _latch_and_retry(conn, kis, pid)
        assert outcomes[0]["action"] == "SOLD", outcomes
        assert len(kis.submit_calls) == 1
        sent = kis.submit_calls[0]
        assert sent.side == "sell" and sent.symbol == SYMBOL and sent.quantity == 1
        row = ps.load(conn, pid)
        assert row["status"] == ps.EXIT_SUBMITTED and row["exit_submitted"] == 1
        intent = exit_intent_ledger.get_active_intent(conn, pid)
        assert intent["state"] == exit_intent_ledger.STATE_SUBMITTED

    def test_the_approval_is_audited_as_a_protective_exit(self, conn):
        import shadow_audit

        pid = _open_s6(conn)
        kis = FakeKIS(positions=[_kis_position(), _kis_position("ZZZ", 3)])
        _latch_and_retry(conn, kis, pid)
        approvals = [e for e in shadow_audit.read_events(conn=conn)
                     if e["event_type"] == shadow_audit.GATE_APPROVED]
        assert approvals, "the sell must be audited as approved"
        assert order_gate.PROTECTIVE_EXIT in (approvals[-1].get("payload") or "")

    def test_quantity_mismatch_on_the_symbol_itself_caps_at_the_broker(self, conn):
        """Local 2, broker 1: the sell of 2 is refused (SELL_QTY), and
        nothing is auto-resized -- resizing is TCN-02B's decision."""
        pid = _open_s6(conn, qty=2)
        kis = FakeKIS(positions=[_kis_position(qty=1)])
        outcomes = _latch_and_retry(conn, kis, pid)
        assert outcomes[0]["action"] == "SELL_BLOCKED", outcomes
        assert kis.submit_calls == []
        assert ps.load(conn, pid)["status"] == ps.EXIT_PENDING

    def test_more_at_the_broker_than_locally_sells_the_local_quantity(self, conn):
        pid = _open_s6(conn, qty=1)
        kis = FakeKIS(positions=[_kis_position(qty=2)])
        assert _classify(conn, kis).primary == reconciliation_state.POSITION_MISMATCH
        outcomes = _latch_and_retry(conn, kis, pid)
        assert outcomes[0]["action"] == "SOLD", outcomes
        assert kis.submit_calls[0].quantity == 1


# ---------------------------------------------------------------- D
class TestD_BrokerFlatIsNotASell:
    def test_no_sell_is_sent_and_the_row_is_an_external_close_candidate(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(positions=[])
        assert _classify(conn, kis).primary == reconciliation_state.POSITION_MISMATCH

        outcomes = _latch_and_retry(conn, kis, pid)
        assert outcomes[0]["action"] == "SELL_BLOCKED", outcomes
        assert kis.submit_calls == []
        assert ps.load(conn, pid)["status"] == ps.EXIT_PENDING
        # The refused attempt left nothing unresolved behind it: the
        # intent was aborted and the ledger row rejected, so the row is
        # retirable on the evidence bar external_close demands.
        assert exit_intent_ledger.get_active_intent(conn, pid) is None
        found = external_close_service.candidates(conn, kis, now=NOW)
        assert [c["position_id"] for c in found] == [pid]
        assert found[0]["book"] == ps.STRATEGY_ID
        # A dry run changed nothing.
        assert ps.load(conn, pid)["status"] == ps.EXIT_PENDING


# ---------------------------------------------------------------- E
class TestE_BrokerReadFailure:
    def test_buy_blocked_and_no_blind_sell(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(read_exc=KISBrokerError("KIS 5xx"))
        assert _classify(conn, kis).primary == reconciliation_state.BROKER_READ_FAILURE

        with pytest.raises(ExecutionEngineError) as exc:
            _submit_buy(conn, kis)
        assert exc.value.reason_code == execution_engine.REASON_RECONCILIATION_UNAVAILABLE

        outcomes = _latch_and_retry(conn, kis, pid)
        assert outcomes[0]["action"] == "SELL_BLOCKED", outcomes
        assert kis.submit_calls == []
        # Latched for the next tick, never SUBMISSION_UNKNOWN: nothing
        # was sent, so nothing is unknown.
        assert ps.load(conn, pid)["status"] == ps.EXIT_PENDING
        assert exit_intent_ledger.get_active_intent(conn, pid) is None

    def test_a_failed_local_read_is_a_named_state_not_an_unknown_submission(self, conn, monkeypatch):
        pid = _open_s6(conn)
        kis = FakeKIS()

        def _boom(*args, **kwargs):
            raise RuntimeError("ledger unreadable")

        monkeypatch.setattr(idempotency, "list_unknown_orders", _boom)
        assert _classify(conn, kis).primary == reconciliation_state.LOCAL_STATE_FAILURE
        outcomes = _latch_and_retry(conn, kis, pid)
        assert outcomes[0]["action"] == "SELL_BLOCKED", outcomes
        assert kis.submit_calls == []
        assert exit_intent_ledger.get_active_intent(conn, pid) is None


# ---------------------------------------------------------------- F
class TestF_SubmissionUnknown:
    def test_an_ambiguous_sell_is_never_sent_twice(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(submit_raise=KISAmbiguousResponseError("timeout"))
        first = _latch_and_retry(conn, kis, pid)
        assert first[0]["action"] == "SELL_BLOCKED" and "submission unknown" in first[0]["detail"]
        assert len(kis.submit_calls) == 1
        intent = exit_intent_ledger.get_active_intent(conn, pid)
        assert intent["state"] == exit_intent_ledger.STATE_SUBMISSION_UNKNOWN

        # Later ticks, the wire now healthy and reconciliation dirty on
        # exactly this symbol (an UNKNOWN order): nothing is sent.
        kis.submit_raise = None
        assert _classify(conn, kis).primary == reconciliation_state.SUBMISSION_UNKNOWN
        for _ in range(3):
            er.retry_latched_exits(conn, broker_adapter=_adapter(kis),
                                   session="REGULAR", now=NOW, orders_allowed=True)
            er.run_exits(conn, broker_adapter=_adapter(kis),
                         features_fn=lambda s: None, price_fn=lambda s: 99.0,
                         session="REGULAR", now=NOW, orders_allowed=True)
        assert len(kis.submit_calls) == 1

    def test_an_unknown_order_for_this_symbol_refuses_the_protective_exit(self, conn):
        """Even with the intent ledger clear, an UNKNOWN order on the
        symbol itself is a sell that may be working."""
        pid = _open_s6(conn)
        kis = FakeKIS(submit_raise=KISAmbiguousResponseError("timeout"))
        _latch_and_retry(conn, kis, pid)
        intent = exit_intent_ledger.get_active_intent(conn, pid)
        exit_intent_ledger.mark_aborted(conn, intent["intent_id"])
        kis.submit_raise = None
        outcomes = er.retry_latched_exits(conn, broker_adapter=_adapter(kis),
                                          session="REGULAR", now=NOW, orders_allowed=True)
        assert outcomes[0]["action"] == "SELL_BLOCKED", outcomes
        assert len(kis.submit_calls) == 1


# ---------------------------------------------------------------- G
class TestG_ExistingPendingSell:
    def test_a_broker_open_order_for_the_symbol_blocks_a_second_sell(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(open_orders=[{"pdno": SYMBOL, "odno": "manual-1", "sll_buy_dvsn_cd": "01"}])
        outcomes = _latch_and_retry(conn, kis, pid)
        assert outcomes[0]["action"] == "SELL_BLOCKED", outcomes
        assert kis.submit_calls == []

    def test_a_submitted_exit_is_not_resubmitted_under_a_dirty_reconciliation(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(positions=[_kis_position(), _kis_position("ZZZ", 3)])
        _latch_and_retry(conn, kis, pid)
        assert len(kis.submit_calls) == 1
        for _ in range(3):
            er.retry_latched_exits(conn, broker_adapter=_adapter(kis),
                                   session="REGULAR", now=NOW, orders_allowed=True)
            er.run_exits(conn, broker_adapter=_adapter(kis),
                         features_fn=lambda s: None, price_fn=lambda s: 99.0,
                         session="REGULAR", now=NOW, orders_allowed=True)
        assert len(kis.submit_calls) == 1


# ---------------------------------------------------------------- H
class TestH_ExternalClose:
    def test_local_open_broker_flat_is_closed_exactly_once(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(positions=[])
        first = external_close_service.retire_all(conn, kis, now=NOW, apply=True)
        assert [o["outcome"] for o in first[ps.STRATEGY_ID]] == [external_close.RETIRED]
        row = ps.load(conn, pid)
        assert row["status"] == ps.CLOSED
        assert row["exit_reason"] == external_close.EXTERNALLY_CLOSED
        assert row["exit_price"] is None
        assert ps.holdings(conn) == []

        second = external_close_service.retire_all(conn, kis, now=NOW, apply=True)
        assert second[ps.STRATEGY_ID] == []
        assert kis.submit_calls == []

    def test_a_pending_sell_keeps_the_row(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(positions=[_kis_position(), _kis_position("ZZZ", 3)])
        _latch_and_retry(conn, kis, pid)
        kis.positions = []
        report = external_close_service.retire_all(conn, kis, now=NOW, apply=True)
        outcomes = report[ps.STRATEGY_ID]
        assert outcomes and outcomes[0]["outcome"] != external_close.RETIRED
        assert ps.load(conn, pid)["status"] == ps.EXIT_SUBMITTED

    def test_an_unreadable_broker_retires_nothing(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(read_exc=KISBrokerError("down"))
        report = external_close_service.retire_all(conn, kis, now=NOW, apply=True)
        assert report[ps.STRATEGY_ID][0]["outcome"] == external_close.HELD_BROKER_UNREADABLE
        assert ps.load(conn, pid)["status"] == ps.OPEN

    def test_dry_run_is_the_default(self, conn):
        pid = _open_s6(conn)
        external_close_service.retire_all(conn, FakeKIS(positions=[]), now=NOW)
        assert ps.load(conn, pid)["status"] == ps.OPEN


# ---------------------------------------------------------------- I
class TestI_RepeatedTicksNeverDuplicate:
    def test_dirty_reconciliation_ticks_submit_once(self, conn):
        pid = _open_s6(conn)
        kis = FakeKIS(positions=[_kis_position(), _kis_position("ZZZ", 3)])
        ps.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=NOW)
        for tick in range(6):
            moment = NOW + timedelta(minutes=tick)
            adapter = KISBrokerAdapter(kis, now_fn=lambda m=moment: m)
            er.run_exits(conn, broker_adapter=adapter, features_fn=lambda s: None,
                         price_fn=lambda s: 99.0, session="REGULAR", now=moment,
                         orders_allowed=True)
            er.retry_latched_exits(conn, broker_adapter=adapter, session="REGULAR",
                                   now=moment, orders_allowed=True)
            er.reconcile_unconfirmed_exits(conn, broker=kis, session="REGULAR", now=moment)
        assert len(kis.submit_calls) == 1
        assert ps.load(conn, pid)["status"] == ps.EXIT_SUBMITTED
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM kis_order_idempotency WHERE side = 'sell' "
            "AND status NOT IN ('REJECTED')").fetchone()
        assert rows["n"] == 1


# ---------------------------------------------------------------- J
class TestJ_StrictPolicyUnchangedWithoutEvidence:
    def test_the_adapter_without_local_evidence_still_blocks_a_dirty_sell(self, conn):
        """The legacy lifecycle path passes no evidence; it must see
        exactly the behaviour it saw before TCN-02A."""
        _open_s6(conn)
        kis = FakeKIS(positions=[_kis_position(), _kis_position("ZZZ", 3)])
        response = _adapter(kis).submit_order(SYMBOL, qty=1, side="sell",
                                              client_order_id="legacy-1")
        assert response.status_code not in (200, 201)
        assert kis.submit_calls == []
        assert "reconciliation" in str(response.data.get("blocked_reason", "")).lower()

    def test_the_buy_gate_sequence_is_untouched(self):
        assert "RECONCILIATION" in order_gate.BUY_GATE_SEQUENCE
        assert order_gate.PROTECTIVE_EXIT not in order_gate.BUY_GATE_SEQUENCE
