"""CODEX-053 (remainder): a cancel is ONE audit run, and it always ends.

The blocking defect: `submit_cancel()` recorded GATE_APPROVED and
EXECUTION_PLANNED before the transport call and then stopped. Success,
gate block, CAS conflict and ambiguous response all left the run open --
no SHADOW_COMPLETED, no SHADOW_BLOCKED, no SHADOW_ERROR -- so every
cancel produced a run that `runs_without_terminal_event()` would flag
forever. The test written alongside that code pinned the expected events
at exactly two, so it asserted the gap instead of catching it.

These tests assert the whole lifecycle, on every path, under one
audit_run_id, exactly once.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

import shadow_audit
from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from domain.execution_event import ExecutionRecord
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution import execution_engine, idempotency, order_gate, order_repository
from execution.execution_engine import ExecutionEngineError
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"
TERMINALS = ("SHADOW_COMPLETED", "SHADOW_BLOCKED", "SHADOW_ERROR")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    state_db.open_db().close()
    yield


def _instrument():
    return build_instrument("AAPL", exchange="NASDAQ")


def _order_intent(**overrides):
    kwargs = dict(
        internal_order_id="ord-1", signal_id="sig-1", strategy_id="strat-1", symbol="AAPL",
        exchange="NASDAQ", side="buy", quantity=1, order_type="limit", limit_price=100.0,
        stop_price=95.0, target_price=110.0, created_at=NOW,
    )
    kwargs.update(overrides)
    return OrderIntent(**kwargs)


def _buy_ctx_builder(order_intent):
    def _build(reconciliation):
        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=True, entry_disabled=False,
            validated_commit="c1", deployed_commit="c1", kis_account_no=ACCOUNT_ID,
            allowed_account_no=ACCOUNT_ID, order_intent=order_intent, instrument=_instrument(),
            signal=build_signal(
                strategy_id="strat-1", strategy_version="v1", config_version="cfg-1",
                code_commit="abc", symbol="AAPL", exchange="NASDAQ", signal_price=100.0,
                score=90.0, entry_reason="breakout", valid_for_seconds=300, now=NOW,
            ),
            is_regular_session=True, kis_price_usd=100.1, max_price_deviation_percent=0.30,
            usd_orderable_cash=1000.0, has_open_order_for_symbol=False,
            has_order_for_signal_id=False, allowed_symbols=frozenset({"AAPL"}),
            reconciliation=reconciliation, now=NOW,
        )
    return _build


def _cancel_ctx_builder(**overrides):
    def _build():
        kwargs = dict(
            execution_broker="kis", broker_order_id="kis-1", is_actually_open=True,
            kis_account_no=ACCOUNT_ID, allowed_account_no=ACCOUNT_ID, symbol="AAPL",
            has_cancel_already_in_flight=False,
        )
        kwargs.update(overrides)
        return order_gate.CancelGateContext(**kwargs)
    return _build


class _Broker:
    """Records what the audit trail looked like, read through a SEPARATE
    connection, at the instant cancel_order() was invoked."""

    def __init__(self, cancel_status="CANCELLED", cancel_exc=None, open_orders=None):
        self.cancel_status = cancel_status
        self.cancel_exc = cancel_exc
        self.cancel_calls = 0
        self.events_at_transport = None
        self.state_at_transport = None
        # Orders KIS reports as still working. Needed once more than one
        # order exists, or reconciliation blocks the next placement on
        # "internally live but KIS has never heard of it".
        self.open_orders = open_orders if open_orders is not None else []

    def get_positions(self):
        return []

    def get_open_orders(self):
        return self.open_orders

    def get_fills(self, *, start_date, end_date):
        return []

    def submit_order(self, order_intent, instrument, *, authorization=None):
        # Keep the fake self-consistent: once an order is accepted, KIS
        # reports it as working, so the NEXT reconciliation does not see
        # an internally-live order KIS has never heard of.
        if not any(o.get("ODNO") == "kis-1" for o in self.open_orders):
            self.open_orders.append({"ODNO": "kis-1"})
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id="kis-1", requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=NOW, updated_at=NOW,
        )

    def cancel_order(self, order_intent, instrument, broker_order_id, *, authorization=None):
        self.cancel_calls += 1
        self.cancel_run_id = _current_cancel_run_id[0]
        conn = state_db.open_db()
        try:
            self.events_at_transport = [
                row["event_type"] for row in
                shadow_audit.read_events(shadow_run_id=self.cancel_run_id, conn=conn)
            ]
            self.state_at_transport = order_repository.load(
                conn, order_intent.internal_order_id,
            ).state
        finally:
            conn.close()
        if self.cancel_exc is not None:
            raise self.cancel_exc
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status=self.cancel_status, submitted_at=NOW, updated_at=NOW,
        )


def _place_order(conn, broker, order_intent=None):
    """Places a real order so there is something to cancel.

    Ownership of the terminal event differs by path, deliberately: a
    BUY/SELL run is finalized by its pipeline (kis_live_trading.py /
    kis_broker_adapter.py), which also records SIGNAL_RECEIVED and the
    other pipeline-level events, so the engine must not finalize it and
    conflict. A CANCEL has no such pipeline -- submit_cancel() is the
    whole run -- so the engine finalizes that one itself. This helper
    plays the pipeline's part for the setup orders."""
    oi = order_intent or _order_intent()
    run_id = shadow_audit.new_run_id()
    execution_engine.submit_buy_order(
        order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
        broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID,
        audit_run_id=run_id, now=NOW,
    )
    shadow_audit.finalize_audit_run(
        audit_run_id=run_id, terminal_event=shadow_audit.SHADOW_COMPLETED,
        internal_order_id=oi.internal_order_id, action="buy", symbol=oi.symbol, side="buy",
        reason_code="ACCEPTED", now=NOW,
    )
    return oi


# The run id the cancel currently under test is using, so the broker
# double can scope its read to THAT run rather than the whole table.
_current_cancel_run_id = [None]


def _cancel(conn, broker, run_id, *, ctx=None, order_intent=None):
    _current_cancel_run_id[0] = run_id
    return execution_engine.submit_cancel(
        order_intent=order_intent or _order_intent(), broker_order_id="kis-1",
        cancel_gate_context_builder=ctx or _cancel_ctx_builder(), conn=conn, broker=broker,
        instrument=_instrument(), audit_run_id=run_id, now=NOW,
    )


def _events(run_id):
    return [row["event_type"] for row in shadow_audit.read_events(shadow_run_id=run_id)]


def _terminal_count(run_id):
    return sum(1 for event in _events(run_id) if event in TERMINALS)


class TestCancelSuccess:
    def test_full_lifecycle_under_one_run_id(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        result = _cancel(conn, broker, run_id)

        assert result.status == "CANCELLED"
        assert _events(run_id) == ["GATE_APPROVED", "EXECUTION_PLANNED", "SHADOW_COMPLETED"]
        assert _terminal_count(run_id) == 1
        rows = shadow_audit.read_events(shadow_run_id=run_id)
        assert all(row["shadow_run_id"] == run_id for row in rows)

    def test_pre_transport_events_are_durable_at_the_transport_call(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        _cancel(conn, broker, shadow_audit.new_run_id())

        # Read through a separate connection INSIDE cancel_order(),
        # scoped to this cancel's own run.
        assert broker.events_at_transport == ["GATE_APPROVED", "EXECUTION_PLANNED"]
        assert broker.state_at_transport == "CANCEL_PENDING"
        # ...and the terminal event was NOT yet written at that point.
        assert not set(broker.events_at_transport) & set(TERMINALS)

    def test_terminal_event_is_visible_from_a_fresh_connection(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()
        _cancel(conn, broker, run_id)
        conn.close()

        fresh = state_db.open_db()
        try:
            types = [r["event_type"] for r in shadow_audit.read_events(shadow_run_id=run_id,
                                                                       conn=fresh)]
        finally:
            fresh.close()
        assert types[-1] == "SHADOW_COMPLETED"

    def test_terminal_payload_carries_the_required_context(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()
        _cancel(conn, broker, run_id)

        terminal = [r for r in shadow_audit.read_events(shadow_run_id=run_id)
                    if r["event_type"] == "SHADOW_COMPLETED"][0]
        assert terminal["internal_order_id"] == "ord-1"
        assert terminal["reason_code"] == "CANCEL_CONFIRMED"
        assert terminal["symbol"] == "AAPL"
        payload = terminal["payload"] or ""
        assert '"action": "cancel"' in payload
        assert '"final_order_state": "CANCELLED"' in payload
        # CODEX-050: no full broker order id in the durable payload.
        assert '"broker_order_id_last4"' in payload
        assert '"broker_order_id":' not in payload


class TestCancelBlockedBeforeTransport:
    def _assert_blocked(self, run_id, broker, reason_fragment=None):
        assert broker.cancel_calls == 0
        types = _events(run_id)
        assert types[-1] == "SHADOW_BLOCKED"
        assert _terminal_count(run_id) == 1
        if reason_fragment:
            terminal = [r for r in shadow_audit.read_events(shadow_run_id=run_id)
                        if r["event_type"] == "SHADOW_BLOCKED"][0]
            assert reason_fragment in (terminal["reason_code"] or "")

    def test_gate_rejection_ends_the_run_as_blocked(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        with pytest.raises(ExecutionEngineError, match="order gate"):
            _cancel(conn, broker, run_id, ctx=_cancel_ctx_builder(is_actually_open=False))
        self._assert_blocked(run_id, broker, reason_fragment="GATE")

    def test_missing_order_record_ends_the_run_as_blocked(self):
        conn = state_db.open_db()
        broker = _Broker()
        run_id = shadow_audit.new_run_id()

        with pytest.raises(ExecutionEngineError, match="no durable order record"):
            _cancel(conn, broker, run_id)
        self._assert_blocked(run_id, broker, reason_fragment="CANCEL_NO_ORDER_RECORD")

    def test_duplicate_cancel_in_flight_ends_the_run_as_blocked(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        with pytest.raises(ExecutionEngineError):
            _cancel(conn, broker, run_id,
                    ctx=_cancel_ctx_builder(has_cancel_already_in_flight=True))
        self._assert_blocked(run_id, broker)

    def test_account_mismatch_ends_the_run_as_blocked(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        with pytest.raises(ExecutionEngineError):
            _cancel(conn, broker, run_id, ctx=_cancel_ctx_builder(kis_account_no="99999999"))
        self._assert_blocked(run_id, broker)

    def test_cas_conflict_ends_the_run_as_blocked(self):
        """The order is already CANCELLED, so CANCEL_PENDING is not a legal
        transition from its current state -- the cancel must not reach the
        transport, and the run must still end."""
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        _cancel(conn, broker, shadow_audit.new_run_id())  # first cancel succeeds
        assert order_repository.load(conn, "ord-1").state == "CANCELLED"

        broker.cancel_calls = 0
        run_id = shadow_audit.new_run_id()
        with pytest.raises(ExecutionEngineError, match="CANCEL_PENDING"):
            _cancel(conn, broker, run_id)
        self._assert_blocked(run_id, broker, reason_fragment="STATE_PERSISTENCE")


class TestCancelErrorPaths:
    def test_ambiguous_response_ends_the_run_as_error_and_leaves_unknown(self):
        conn = state_db.open_db()
        broker = _Broker(cancel_exc=KISAmbiguousResponseError("cancel timed out"))
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        with pytest.raises(KISAmbiguousResponseError):
            _cancel(conn, broker, run_id)

        assert broker.cancel_calls == 1
        assert order_repository.load(conn, "ord-1").state == "UNKNOWN"
        types = _events(run_id)
        assert types[-1] == "SHADOW_ERROR"
        assert _terminal_count(run_id) == 1
        terminal = [r for r in shadow_audit.read_events(shadow_run_id=run_id)
                    if r["event_type"] == "SHADOW_ERROR"][0]
        assert terminal["reason_code"] == "CANCEL_OUTCOME_UNKNOWN"

    def test_ambiguous_cancel_is_never_retried_automatically(self):
        conn = state_db.open_db()
        broker = _Broker(cancel_exc=KISAmbiguousResponseError("cancel timed out"))
        _place_order(conn, broker)
        with pytest.raises(KISAmbiguousResponseError):
            _cancel(conn, broker, shadow_audit.new_run_id())
        assert broker.cancel_calls == 1

    def test_definite_broker_error_ends_the_run_as_error(self):
        conn = state_db.open_db()
        broker = _Broker(cancel_exc=KISBrokerError("cancel rejected outright"))
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        with pytest.raises(KISBrokerError):
            _cancel(conn, broker, run_id)

        assert order_repository.load(conn, "ord-1").state == "UNKNOWN"
        assert _events(run_id)[-1] == "SHADOW_ERROR"
        assert _terminal_count(run_id) == 1

    def test_unconfirmed_cancel_status_ends_the_run_as_error(self):
        """KIS returned something other than CANCELLED: the underlying
        order's real state is not known, so the run is an ERROR, not a
        completion."""
        conn = state_db.open_db()
        broker = _Broker(cancel_status="REJECTED")
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        result = _cancel(conn, broker, run_id)

        assert result.status == "UNKNOWN"
        assert order_repository.load(conn, "ord-1").state == "UNKNOWN"
        assert _events(run_id)[-1] == "SHADOW_ERROR"
        assert _terminal_count(run_id) == 1


class _FailAuditAt:
    """Fails the shadow-audit INSERT for one specific event type."""

    def __init__(self, conn, event_type):
        self._conn = conn
        self._event_type = event_type

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO shadow_audit_events" in sql:
            params = args[0] if args else ()
            if self._event_type in tuple(params):
                raise sqlite3.OperationalError("simulated audit store failure")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestCancelAuditPersistenceFailures:
    def _patch(self, monkeypatch, event_type):
        real_open = shadow_audit._open_conn
        monkeypatch.setattr(
            shadow_audit, "_open_conn", lambda: _FailAuditAt(real_open(), event_type),
        )

    def test_gate_approved_audit_failure_blocks_before_transport(self, monkeypatch):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        self._patch(monkeypatch, "GATE_APPROVED")

        with pytest.raises(ExecutionEngineError) as excinfo:
            _cancel(conn, broker, shadow_audit.new_run_id())
        assert excinfo.value.reason_code == "AUDIT_PERSISTENCE"
        assert broker.cancel_calls == 0

    def test_execution_planned_audit_failure_blocks_before_transport(self, monkeypatch):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        self._patch(monkeypatch, "EXECUTION_PLANNED")

        with pytest.raises(ExecutionEngineError) as excinfo:
            _cancel(conn, broker, shadow_audit.new_run_id())
        assert excinfo.value.reason_code == "AUDIT_PERSISTENCE"
        assert broker.cancel_calls == 0

    def test_terminal_audit_failure_is_not_reported_as_a_successful_cancel(self, monkeypatch):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()
        self._patch(monkeypatch, "SHADOW_COMPLETED")

        with pytest.raises(ExecutionEngineError) as excinfo:
            _cancel(conn, broker, run_id)
        assert excinfo.value.reason_code == "AUDIT_PERSISTENCE"
        # The cancel DID reach KIS and was confirmed -- that outcome is
        # preserved, not rolled back to hide it.
        assert broker.cancel_calls == 1
        assert order_repository.load(conn, "ord-1").state == "CANCELLED"

    def test_blocked_terminal_audit_failure_alerts_and_keeps_the_original_error(
        self, monkeypatch,
    ):
        alerts_sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda m: alerts_sent.append(m) or True)
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        self._patch(monkeypatch, "SHADOW_BLOCKED")

        # The ORIGINAL gate block must reach the caller, not an audit error.
        with pytest.raises(ExecutionEngineError, match="order gate"):
            _cancel(conn, broker, shadow_audit.new_run_id(),
                    ctx=_cancel_ctx_builder(is_actually_open=False))
        assert alerts_sent, "no operator alert for a failed terminal audit"
        assert broker.cancel_calls == 0

    def test_error_terminal_audit_failure_keeps_the_original_error(self, monkeypatch):
        alerts_sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda m: alerts_sent.append(m) or True)
        conn = state_db.open_db()
        broker = _Broker(cancel_exc=KISAmbiguousResponseError("timeout"))
        _place_order(conn, broker)
        self._patch(monkeypatch, "SHADOW_ERROR")

        with pytest.raises(KISAmbiguousResponseError):
            _cancel(conn, broker, shadow_audit.new_run_id())
        assert alerts_sent
        # The order's real state is still preserved as UNKNOWN.
        assert order_repository.load(conn, "ord-1").state == "UNKNOWN"


class TestTerminalInvariantAcrossEveryCancelPath:
    def test_no_cancel_path_leaves_an_open_or_doubled_run(self):
        """Drives every outcome in one database and then asserts the
        global invariant, which is what an operator actually checks.

        All four orders are placed FIRST: once one of them goes UNKNOWN
        (the ambiguous cancel), reconciliation correctly blocks every new
        order account-wide, so placements have to precede the cancels."""
        conn = state_db.open_db()
        working = []
        placer = _Broker(open_orders=working)
        intents = {
            key: _order_intent(internal_order_id=key, signal_id=f"sig-{key}")
            for key in ("a", "b", "c", "d")
        }
        for intent in intents.values():
            _place_order(conn, placer, intent)

        # success
        _cancel(conn, _Broker(open_orders=working), shadow_audit.new_run_id(),
                order_intent=intents["a"])

        # gate block
        blocked_broker = _Broker(open_orders=working)
        with pytest.raises(ExecutionEngineError):
            _cancel(conn, blocked_broker, shadow_audit.new_run_id(),
                    ctx=_cancel_ctx_builder(is_actually_open=False),
                    order_intent=intents["b"])
        assert blocked_broker.cancel_calls == 0

        # ambiguous transport
        with pytest.raises(KISAmbiguousResponseError):
            _cancel(conn, _Broker(cancel_exc=KISAmbiguousResponseError("timeout"),
                                  open_orders=working),
                    shadow_audit.new_run_id(), order_intent=intents["c"])

        # transport returned something other than a confirmed cancel
        _cancel(conn, _Broker(cancel_status="REJECTED", open_orders=working),
                shadow_audit.new_run_id(), order_intent=intents["d"])

        report = shadow_audit.audit_integrity_report()
        assert report["runs_without_terminal_event"] == []
        assert report["runs_with_multiple_terminal_events"] == []

        cancel_runs = {}
        for row in shadow_audit.read_events():
            if row["side"] == "cancel":
                cancel_runs.setdefault(row["shadow_run_id"], []).append(row["event_type"])
        assert len(cancel_runs) == 4
        outcomes = []
        for run_id, types in cancel_runs.items():
            terminals = [t for t in types if t in TERMINALS]
            assert len(terminals) == 1, f"{run_id}: {types}"
            outcomes.append(terminals[0])
        assert sorted(outcomes) == [
            "SHADOW_BLOCKED", "SHADOW_COMPLETED", "SHADOW_ERROR", "SHADOW_ERROR",
        ]


class _FailFinalStateConn:
    """Lets everything through except the durable write that records the
    CONFIRMED cancel -- the exact window CODEX-054 is about."""

    def __init__(self, conn, mode="update"):
        self._conn = conn
        self._mode = mode
        self.armed = False
        self._final_write_seen = False

    def arm(self):
        self.armed = True

    def execute(self, sql, *args, **kwargs):
        params = tuple(args[0]) if args else ()
        if self.armed and self._mode == "update" and "SET status = ?" in sql:
            if "CANCELLED" in params:
                raise sqlite3.OperationalError("simulated final-state UPDATE failure")
        if self.armed and self._mode == "event" and "INSERT INTO order_state_events" in sql:
            if "CANCEL_CONFIRMED" in params:
                raise sqlite3.OperationalError("simulated final-state event failure")
        if self.armed and self._mode == "commit" and "SET status = ?" in sql:
            # Only the COMMIT that would persist the confirmed cancel
            # fails; every earlier transition must still work, or this
            # would be a PRE-transport failure instead.
            if "CANCELLED" in params:
                self._final_write_seen = True
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        if self.armed and self._mode == "commit" and self._final_write_seen:
            self._final_write_seen = False
            raise sqlite3.OperationalError("simulated commit failure")
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestPostTransportPersistenceFailureIsAnError:
    """CODEX-054: SHADOW_BLOCKED means the execution never reached the
    broker. Once broker.cancel_order() has run, every failure is
    SHADOW_ERROR -- otherwise the audit trail says an order that may be
    cancelled at KIS was never even attempted."""

    def _run(self, mode):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        failing = _FailFinalStateConn(conn, mode=mode)
        run_id = shadow_audit.new_run_id()
        failing.arm()
        with pytest.raises(ExecutionEngineError) as excinfo:
            execution_engine.submit_cancel(
                order_intent=_order_intent(), broker_order_id="kis-1",
                cancel_gate_context_builder=_cancel_ctx_builder(), conn=failing, broker=broker,
                instrument=_instrument(), audit_run_id=run_id, now=NOW,
            )
        return conn, broker, run_id, excinfo.value

    def test_reproduction_final_state_update_failure(self):
        """The exact path Codex reported: transport ran, KIS confirmed the
        cancel, persisting the final state failed."""
        conn, broker, run_id, exc = self._run("update")

        assert broker.cancel_calls == 1, "the transport DID run"
        types = _events(run_id)
        assert "SHADOW_ERROR" in types
        assert "SHADOW_BLOCKED" not in types
        assert "SHADOW_COMPLETED" not in types
        assert _terminal_count(run_id) == 1
        assert order_repository.load(conn, "ord-1").state == "UNKNOWN"
        assert exc.reason_code == "CANCEL_FINAL_STATE_PERSISTENCE"

    def test_final_state_event_insert_failure(self):
        conn, broker, run_id, exc = self._run("event")
        assert broker.cancel_calls == 1
        assert _events(run_id)[-1] == "SHADOW_ERROR"
        assert _terminal_count(run_id) == 1
        assert order_repository.load(conn, "ord-1").state == "UNKNOWN"

    def test_commit_failure_after_confirmation(self):
        conn, broker, run_id, exc = self._run("commit")
        assert broker.cancel_calls == 1
        assert _events(run_id)[-1] == "SHADOW_ERROR"
        assert _terminal_count(run_id) == 1

    def test_cas_conflict_on_the_final_transition(self, monkeypatch):
        """A concurrent writer moved the order between CANCEL_PENDING and
        the final write. Still post-transport, still an ERROR."""
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        real_advance = execution_engine.order_repository.advance

        def _conflict(conn_, record, next_state, **kwargs):
            if next_state == "CANCELLED":
                raise execution_engine.order_repository.OrderStateConflictError("lost the race")
            return real_advance(conn_, record, next_state, **kwargs)

        # NOTE: no monkeypatch.undo() here -- it would undo the autouse
        # _isolate fixture's env vars too and point the assertions below
        # at a different database. pytest tears the patch down anyway.
        monkeypatch.setattr(execution_engine.order_repository, "advance", _conflict)
        with pytest.raises(ExecutionEngineError):
            _cancel(conn, broker, run_id)

        assert broker.cancel_calls == 1
        assert _events(run_id)[-1] == "SHADOW_ERROR"
        assert _terminal_count(run_id) == 1

    def test_no_automatic_re_cancel_after_a_persistence_failure(self):
        _conn, broker, _run_id, _exc = self._run("update")
        assert broker.cancel_calls == 1

    def test_alert_is_raised_for_the_divergence(self, monkeypatch):
        sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda m: sent.append(m) or True)
        self._run("update")
        assert sent, "no operator alert for a confirmed-but-unrecorded cancel"
        alert = sent[0].lower()
        assert "reconciliation" in alert
        assert "no automatic re-cancel" in alert

    def test_unknown_persistence_failing_too_is_still_an_error(self, monkeypatch):
        """Neither the confirmed state NOR the UNKNOWN fallback could be
        written. Still post-transport, so still SHADOW_ERROR -- and the
        reason code says the durable state does not reflect reality."""
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        real_advance = execution_engine.order_repository.advance

        def _fail_both(conn_, record, next_state, **kwargs):
            if next_state in ("CANCELLED", "UNKNOWN"):
                raise execution_engine.order_repository.OrderRepositoryError("no writes")
            return real_advance(conn_, record, next_state, **kwargs)

        monkeypatch.setattr(execution_engine.order_repository, "advance", _fail_both)
        with pytest.raises(ExecutionEngineError) as excinfo:
            _cancel(conn, broker, run_id)

        assert broker.cancel_calls == 1
        assert _events(run_id)[-1] == "SHADOW_ERROR"
        assert _terminal_count(run_id) == 1
        assert excinfo.value.reason_code == "STATE_PERSISTENCE"

    def test_terminal_payload_records_that_the_transport_ran(self):
        _conn, _broker, run_id, _exc = self._run("update")
        terminal = [r for r in shadow_audit.read_events(shadow_run_id=run_id)
                    if r["event_type"] == "SHADOW_ERROR"][0]
        payload = terminal["payload"] or ""
        assert '"transport_attempted": true' in payload
        assert '"action": "cancel"' in payload


class TestPreTransportBlocksStayBlocked:
    """The other half of CODEX-054: nothing that never reached the broker
    may be reclassified as an execution error."""

    def _assert_blocked_only(self, run_id, broker):
        types = _events(run_id)
        assert broker.cancel_calls == 0
        assert "SHADOW_BLOCKED" in types
        assert "SHADOW_ERROR" not in types
        assert "SHADOW_COMPLETED" not in types
        assert _terminal_count(run_id) == 1

    def test_gate_rejection(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()
        with pytest.raises(ExecutionEngineError):
            _cancel(conn, broker, run_id, ctx=_cancel_ctx_builder(is_actually_open=False))
        self._assert_blocked_only(run_id, broker)

    def test_missing_order_record(self):
        conn = state_db.open_db()
        broker = _Broker()
        run_id = shadow_audit.new_run_id()
        with pytest.raises(ExecutionEngineError):
            _cancel(conn, broker, run_id)
        self._assert_blocked_only(run_id, broker)

    def test_cancel_pending_cas_conflict(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        _cancel(conn, broker, shadow_audit.new_run_id())
        broker.cancel_calls = 0
        run_id = shadow_audit.new_run_id()
        with pytest.raises(ExecutionEngineError):
            _cancel(conn, broker, run_id)
        self._assert_blocked_only(run_id, broker)

    def test_execution_planned_audit_failure(self, monkeypatch):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        real_open = shadow_audit._open_conn
        monkeypatch.setattr(
            shadow_audit, "_open_conn", lambda: _FailAuditAt(real_open(), "EXECUTION_PLANNED"),
        )
        run_id = shadow_audit.new_run_id()
        with pytest.raises(ExecutionEngineError):
            _cancel(conn, broker, run_id)
        # The audit store itself is broken here, so the terminal event
        # cannot be written either (handle_audit_failure() alerts). What
        # must hold is that nothing reached the broker and nothing was
        # misclassified as an execution error.
        assert broker.cancel_calls == 0
        assert "SHADOW_ERROR" not in _events(run_id)


class TestFinalizeAuditRun:
    def test_same_terminal_event_twice_is_an_idempotent_no_op(self):
        state_db.open_db().close()
        shadow_audit.finalize_audit_run(
            audit_run_id="r", terminal_event=shadow_audit.SHADOW_COMPLETED, action="cancel",
        )
        shadow_audit.finalize_audit_run(
            audit_run_id="r", terminal_event=shadow_audit.SHADOW_COMPLETED, action="cancel",
        )
        assert _terminal_count("r") == 1

    def test_conflicting_terminal_event_raises_and_alerts(self, monkeypatch):
        alerts_sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda m: alerts_sent.append(m) or True)
        state_db.open_db().close()
        shadow_audit.finalize_audit_run(
            audit_run_id="r", terminal_event=shadow_audit.SHADOW_COMPLETED, action="cancel",
        )
        with pytest.raises(shadow_audit.AuditInvariantError):
            shadow_audit.finalize_audit_run(
                audit_run_id="r", terminal_event=shadow_audit.SHADOW_ERROR, action="cancel",
            )
        assert alerts_sent
        assert _terminal_count("r") == 1

    def test_a_non_terminal_event_is_refused(self):
        state_db.open_db().close()
        with pytest.raises(shadow_audit.ShadowAuditError, match="not a terminal event"):
            shadow_audit.finalize_audit_run(
                audit_run_id="r", terminal_event=shadow_audit.GATE_APPROVED, action="cancel",
            )
