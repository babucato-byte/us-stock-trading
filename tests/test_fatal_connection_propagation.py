"""CODEX-059: FatalRepositoryConnectionError survives every layer.

CODEX-058 gave the repository a fatal error and the entrypoints a
fail-stop, but the two were never connected: _cancel_inner()'s
final-state handler caught Exception broadly and converted EVERYTHING --
including the fatal -- into CancelPostTransportError. The entrypoint then
saw an ordinary execution error, returned its normal error code instead
of 4, and the process kept running while possibly still holding the
SQLite write lock.

The fatal type now propagates unchanged from the repository, through the
engine, the cancel flow, the pipelines and the `finally` blocks, to the
service entrypoint that exits non-zero.
"""
import ast
import os
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

import shadow_audit
from domain.execution_event import ExecutionRecord
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution import execution_engine, idempotency, order_gate, order_repository
from execution.order_repository import FatalRepositoryConnectionError
from state_store import db as state_db
import entry_limit_fixtures

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"
TERMINALS = ("SHADOW_COMPLETED", "SHADOW_BLOCKED", "SHADOW_ERROR")
FAKE_SECRET = "PLANTEDFATALSECRET999"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    # A poisoned connection genuinely holds the write lock, so the
    # best-effort audit write really does wait out SQLite's busy timeout
    # and the retry backoff. That is correct production behaviour (the
    # process is about to exit), but it would make these tests take
    # minutes. Shorten the waiting only -- the propagation behaviour under
    # test is untouched.
    monkeypatch.setattr(shadow_audit, "WRITE_RETRIES", 1)
    monkeypatch.setattr(shadow_audit, "WRITE_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(
        shadow_audit, "_open_conn", lambda: state_db.open_db(busy_timeout_ms=150),
    )
    state_db.open_db().close()
    yield


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    from operations import alerts as ops_alerts

    monkeypatch.setattr(ops_alerts, "send_alert", lambda message: sent.append(message) or True)
    return sent


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
            reconciliation=reconciliation, entry_limits=entry_limit_fixtures.unlimited(), now=NOW,
        )
    return _build


def _cancel_ctx_builder():
    def _build():
        return order_gate.CancelGateContext(
            execution_broker="kis", broker_order_id="kis-1", is_actually_open=True,
            kis_account_no=ACCOUNT_ID, allowed_account_no=ACCOUNT_ID, symbol="AAPL",
            has_cancel_already_in_flight=False,
        )
    return _build


class _Broker:
    def __init__(self):
        self.open_orders = []
        self.cancel_calls = 0

    def get_positions(self):
        return []

    def get_open_orders(self):
        return self.open_orders

    def get_fills(self, *, start_date, end_date):
        return []

    def submit_order(self, order_intent, instrument, *, authorization=None):
        self.open_orders.append({"ODNO": "kis-1"})
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id="kis-1", requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=NOW, updated_at=NOW,
        )

    def cancel_order(self, order_intent, instrument, broker_order_id, *, authorization=None):
        self.cancel_calls += 1
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="CANCELLED", submitted_at=NOW, updated_at=NOW,
        )


class _PoisonOnFinalWrite:
    """Everything works until the CONFIRMED cancel is persisted; then
    commit, rollback and close all fail -- the real-world shape of a
    fatal connection fault."""

    def __init__(self, conn):
        self._conn = conn
        self._tripped = False

    def execute(self, sql, *args, **kwargs):
        params = tuple(args[0]) if args else ()
        if "SET status = ?" in sql and "CANCELLED" in params:
            self._tripped = True
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        if self._tripped:
            raise sqlite3.OperationalError(f"commit failed secret={FAKE_SECRET}")
        return self._conn.commit()

    def rollback(self):
        if self._tripped:
            raise sqlite3.OperationalError("rollback failed")
        return self._conn.rollback()

    def close(self):
        if self._tripped:
            raise sqlite3.OperationalError("close failed")
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _place_order(conn, broker):
    oi = _order_intent()
    execution_engine.submit_buy_order(
        order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
        broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID,
        audit_run_id=shadow_audit.new_run_id(), now=NOW,
    )
    return oi


def _cancel(conn, broker, run_id):
    return execution_engine.submit_cancel(
        order_intent=_order_intent(), broker_order_id="kis-1",
        cancel_gate_context_builder=_cancel_ctx_builder(), conn=conn, broker=broker,
        instrument=_instrument(), audit_run_id=run_id, now=NOW,
    )


class TestFatalSurvivesTheCancelPath:
    def _run(self):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        poison = _PoisonOnFinalWrite(conn)
        run_id = shadow_audit.new_run_id()
        with pytest.raises(FatalRepositoryConnectionError) as excinfo:
            _cancel(poison, broker, run_id)
        return conn, broker, run_id, excinfo.value

    def test_control_a_healthy_cancel_completes(self):
        """Proves the harness reaches the transport and the final write,
        so the fatal below is genuinely produced by the injected fault."""
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()
        result = _cancel(conn, broker, run_id)
        assert result.status == "CANCELLED"
        assert broker.cancel_calls == 1
        assert order_repository.load(conn, "ord-1").state == "CANCELLED"

    def test_the_exact_reproduction(self, alerts):
        conn, broker, _run_id, exc = self._run()
        assert type(exc) is FatalRepositoryConnectionError
        assert not isinstance(exc, execution_engine.CancelPostTransportError)
        assert not isinstance(exc, execution_engine.ExecutionEngineError)
        assert broker.cancel_calls == 1, "the transport DID run"
        conn.rollback()
        conn.close()

    def test_halt_is_set(self, alerts):
        from operations import kill_switch

        assert kill_switch.is_halted() is False
        conn, _broker, _run_id, _exc = self._run()
        assert kill_switch.is_halted() is True
        conn.rollback()
        conn.close()

    def test_a_critical_alert_is_raised(self, alerts):
        conn, _broker, _run_id, _exc = self._run()
        joined = "\n".join(alerts)
        assert "CRITICAL" in joined
        assert "restart" in joined.lower()
        conn.rollback()
        conn.close()

    def test_no_automatic_re_cancel(self, alerts):
        _conn, broker, _run_id, _exc = self._run()
        assert broker.cancel_calls == 1

    def test_no_secret_or_sql_in_the_alert_or_message(self, alerts):
        conn, _broker, _run_id, exc = self._run()
        joined = "\n".join(alerts) + str(exc)
        assert FAKE_SECRET not in joined
        assert "SET status" not in joined
        conn.rollback()
        conn.close()

    def test_the_finally_block_does_not_replace_the_fatal(self, alerts):
        """The safety net runs on this path (the run was not finalized by
        a normal handler), and must not swap the exception type."""
        conn, _broker, _run_id, exc = self._run()
        assert type(exc) is FatalRepositoryConnectionError
        conn.rollback()
        conn.close()

    def test_a_locked_audit_db_does_not_replace_the_fatal(self, alerts):
        """CODEX-059 requirement 4. The poisoned connection still holds
        the write transaction it could neither commit nor roll back, so
        the terminal audit write -- which uses its OWN connection -- is
        genuinely blocked by that lock. It must fail silently: the
        original fatal is what reaches the caller, not an audit error."""
        conn, _broker, run_id, exc = self._run()
        assert type(exc) is FatalRepositoryConnectionError

        # The audit really was blocked -- no terminal event became durable.
        types = [r["event_type"] for r in shadow_audit.read_events(shadow_run_id=run_id)]
        assert types, "the pre-transport audit events were written before the lock was taken"
        assert not [t for t in types if t in TERMINALS], (
            f"a terminal event landed despite the held write lock: {types}"
        )
        conn.rollback()
        conn.close()

        # And once the lock is gone, the audit DB is healthy again -- the
        # blockage was the lock, not corruption.
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type="SHADOW_ERROR", result="ERROR",
            symbol="AAPL", payload={},
        )
        after = [r["event_type"] for r in shadow_audit.read_events(shadow_run_id=run_id)]
        assert after[-1] == "SHADOW_ERROR"

    def test_an_ordinary_post_transport_failure_is_still_downgraded(self, alerts):
        """The distinction must stay: only a FATAL connection fault
        escapes as itself; an ordinary persistence failure remains a
        CancelPostTransportError ending in SHADOW_ERROR."""
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        real_advance = execution_engine.order_repository.advance

        def _ordinary(conn_, record, next_state, **kwargs):
            if next_state == "CANCELLED":
                raise order_repository.OrderRepositoryPersistenceError("ordinary write failure")
            return real_advance(conn_, record, next_state, **kwargs)

        original = execution_engine.order_repository.advance
        execution_engine.order_repository.advance = _ordinary
        try:
            with pytest.raises(execution_engine.CancelPostTransportError) as excinfo:
                _cancel(conn, broker, run_id)
        finally:
            execution_engine.order_repository.advance = original

        assert not isinstance(excinfo.value, FatalRepositoryConnectionError)
        types = [r["event_type"] for r in shadow_audit.read_events(shadow_run_id=run_id)]
        assert types[-1] == "SHADOW_ERROR"


class TestFatalSurvivesTheNewOrderPath:
    """The same guarantee on the buy/sell flow, whose CAS steps also
    catch OrderRepositoryError -- of which the fatal is a subclass."""

    @pytest.mark.parametrize("failing_state", ["VALIDATING", "APPROVED", "SUBMITTING"])
    def test_fatal_is_not_downgraded_at_any_cas_step(self, failing_state, alerts):
        conn = state_db.open_db()
        broker = _Broker()
        oi = _order_intent()
        real_advance = execution_engine.order_repository.advance

        def _fatal(conn_, record, next_state, **kwargs):
            if next_state == failing_state:
                raise FatalRepositoryConnectionError("rollback and close both failed")
            return real_advance(conn_, record, next_state, **kwargs)

        original = execution_engine.order_repository.advance
        execution_engine.order_repository.advance = _fatal
        try:
            with pytest.raises(FatalRepositoryConnectionError):
                execution_engine.submit_buy_order(
                    order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
                    broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID,
                    audit_run_id=shadow_audit.new_run_id(), now=NOW,
                )
        finally:
            execution_engine.order_repository.advance = original


class TestFatalAbortsTheBuyCycle:
    def test_the_pipeline_does_not_swallow_it_as_a_blocked_symbol(self, monkeypatch, alerts):
        import kis_live_trading as klt
        from config.live_rollout_config import LiveRolloutConfig
        from domain.account_snapshot import AccountSnapshot

        class _PipelineBroker(_Broker):
            def get_current_price(self, instrument):
                return 100.1

            def get_account_snapshot(self, *, source_label="kis_balance"):
                # ORACLE-CASH-01: the balance read carries no cash field.
                return AccountSnapshot(
                    krw_cash=None, usd_cash=None, usd_orderable_cash=None,
                    usd_reserved_in_open_orders=0.0, as_of=NOW, source=source_label,
                    account_id=ACCOUNT_ID, cash_source="TTTS3012R_DOES_NOT_PROVIDE",
                )

            def get_orderable_usd(self, instrument, limit_price_usd):
                return 1000.0

        monkeypatch.setenv("VALIDATED_COMMIT", "c1")
        monkeypatch.setenv("DEPLOYED_COMMIT", "c1")
        monkeypatch.setenv("KIS_ALLOWED_ACCOUNT_NO", ACCOUNT_ID)
        monkeypatch.setattr(klt.pso, "load_watchlist", lambda: ["AAPL"])
        monkeypatch.setattr(klt.pso, "get_us_market_session", lambda: "regular")
        monkeypatch.setattr(klt.pso, "analyze_stock", lambda s: {
            "symbol": s, "price": 100.0, "ma200": 90.0, "rsi": 50.0, "volume_ratio": 1.5,
            "score": 100,
        })

        def _fatal(*args, **kwargs):
            raise FatalRepositoryConnectionError("rollback and close both failed")

        monkeypatch.setattr(execution_engine, "submit_buy_order", _fatal)
        rollout = LiveRolloutConfig(
            enabled=True, allowed_symbols=frozenset({"AAPL"}), max_quantity_per_order=1,
            max_open_positions=1, max_daily_entries=1, regular_session_only=True,
            allow_fractional=False, allow_market_order=False, allow_extended_hours=False,
            allow_leverage=False, allow_inverse=False, allow_short=False, allow_margin=False,
            max_price_deviation_percent=0.30,
        )
        with pytest.raises(FatalRepositoryConnectionError):
            klt.run_live_buy_entry_cycle(
                broker=_PipelineBroker(), live_rollout=rollout, now=NOW,
            )


class TestExceptionPrecedenceIsStatic:
    """Runtime tests cover today; this stops the shape from returning."""

    PATH_MODULES = [
        "execution/execution_engine.py",
        "execution/idempotency.py",
        "brokers/kis_broker_adapter.py",
        "kis_live_trading.py",
        "kis_position_manager.py",
        "reconciliation/snapshot.py",
        "scripts/run_reconciliation.py",
        "scripts/run_shadow_mode.py",
        "scripts/run_shadow_exit_evaluation.py",
        "scripts/run_live_buy_entry.py",
        "scripts/run_health_report.py",
        # T9: the pilot drives the same cycles on a loop, so a poisoned
        # connection must reach ITS exit path too -- a broad handler here
        # would keep the process (and the SQLite write lock) alive.
        "scripts/run_live_pilot.py",
        "live_pilot/runner.py",
        "live_pilot/armed.py",
    ]
    BROAD = {"Exception", "BaseException", "OrderRepositoryError"}
    REACHES_REPOSITORY = (
        "order_repository.", "idempotency.", "compare_and_set_state", "advance(",
        "submit_cancel", "submit_buy_order", "submit_sell_order", "run_once(",
        "collect(", "_cancel_inner", "register(", "load(", "load_events(",
    )

    @staticmethod
    def _handler_names(handler):
        node = handler.type
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Attribute):
            return [node.attr]
        if isinstance(node, ast.Tuple):
            return [getattr(e, "id", getattr(e, "attr", "")) for e in node.elts]
        return ["*bare*"]

    @pytest.mark.parametrize("rel", PATH_MODULES)
    def test_no_broad_catch_precedes_the_fatal_branch(self, rel):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            body = "\n".join(lines[node.body[0].lineno - 1:node.body[-1].end_lineno])
            if not any(marker in body for marker in self.REACHES_REPOSITORY):
                continue
            names = [self._handler_names(h) for h in node.handlers]
            for index, ids in enumerate(names):
                if not (self.BROAD & set(ids)) and ids != ["*bare*"]:
                    continue
                if any("FatalRepositoryConnectionError" in names[j] for j in range(index)):
                    continue
                offenders.append(f"{rel}:{node.handlers[index].lineno} catches {ids}")
        assert offenders == [], (
            "these handlers can swallow FatalRepositoryConnectionError: " + "; ".join(offenders)
        )

    def test_the_fatal_type_is_never_wrapped(self):
        """`raise SomethingElse(...) from fatal` defeats the whole point."""
        for rel in self.PATH_MODULES:
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if "FatalRepositoryConnectionError" not in self._handler_names(node):
                    continue
                for child in ast.walk(node):
                    if isinstance(child, ast.Raise) and child.exc is not None:
                        pytest.fail(
                            f"{rel}:{child.lineno} converts FatalRepositoryConnectionError "
                            "instead of re-raising it"
                        )


# A real child process: holds a genuine write transaction it cannot roll
# back or close, drives a REAL cancel through the engine, and exits via
# the entrypoint contract.
_CHILD = textwrap.dedent(
    """
    import os, sqlite3, sys
    sys.path.insert(0, sys.argv[2])
    os.environ["STATE_STORE_DB_FILE"] = sys.argv[1]
    os.environ["OPERATIONS_HALT_STATE_FILE"] = sys.argv[3]
    os.environ["KILL_SWITCH_STATE_FILE"] = sys.argv[4]
    os.environ["POSITION_STORE_FILE"] = sys.argv[5]
    os.environ["SHADOW_MODE_LOG_FILE"] = sys.argv[6]
    import pathlib
    from datetime import datetime, timezone
    from execution import idempotency
    idempotency._LOCK_FILE = pathlib.Path(sys.argv[3]).parent / "lock"
    import shadow_audit
    from domain.execution_event import ExecutionRecord
    from domain.instrument import build_instrument
    from domain.order_intent import OrderIntent
    from domain.signal import build_signal
    from execution import execution_engine, order_gate, order_repository
    from state_store import db as state_db

    # The poisoned connection really does hold the write lock for the rest
    # of this process, so the best-effort audit write genuinely waits out
    # SQLite's busy timeout and the retry backoff -- correct behaviour, but
    # it would make this child take minutes. Shorten only the waiting; what
    # is asserted (exit code, cycle count, HALT, lock release) is untouched.
    shadow_audit.WRITE_RETRIES = 1
    shadow_audit.WRITE_RETRY_BASE_SECONDS = 0.001
    shadow_audit._open_conn = lambda: state_db.open_db(sys.argv[1], busy_timeout_ms=150)

    NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    ACC = "12345678"
    oi = OrderIntent(internal_order_id="p", signal_id="sp", strategy_id="s", symbol="AAPL",
                     exchange="NASDAQ", side="buy", quantity=1, order_type="limit",
                     limit_price=100.0, stop_price=95.0, target_price=110.0, created_at=NOW)
    sig = build_signal(strategy_id="s", strategy_version="v1", config_version="c",
                       code_commit="a", symbol="AAPL", exchange="NASDAQ", signal_price=100.0,
                       score=90.0, entry_reason="b", valid_for_seconds=300, now=NOW)

    class B:
        def __init__(self):
            self.oo = []
        def get_positions(self): return []
        def get_open_orders(self): return self.oo
        def get_fills(self, *, start_date, end_date): return []
        def submit_order(self, o, i, *, authorization=None):
            self.oo.append({"ODNO": "kis-1"})
            return ExecutionRecord(internal_order_id=o.internal_order_id, broker="kis",
                broker_order_id="kis-1", requested_quantity=o.quantity,
                requested_price=o.limit_price, filled_quantity=0.0, average_fill_price=None,
                status="ACCEPTED", submitted_at=NOW, updated_at=NOW)
        def cancel_order(self, o, i, boid, *, authorization=None):
            return ExecutionRecord(internal_order_id=o.internal_order_id, broker="kis",
                broker_order_id=boid, requested_quantity=o.quantity,
                requested_price=o.limit_price, filled_quantity=0.0, average_fill_price=None,
                status="CANCELLED", submitted_at=NOW, updated_at=NOW)

    class Poison:
        def __init__(self, c):
            self._c = c; self._tripped = False
        def execute(self, sql, *a, **k):
            params = tuple(a[0]) if a else ()
            if "SET status = ?" in sql and "CANCELLED" in params:
                self._tripped = True
            return self._c.execute(sql, *a, **k)
        def commit(self):
            if self._tripped: raise sqlite3.OperationalError("commit failed")
            return self._c.commit()
        def rollback(self):
            if self._tripped: raise sqlite3.OperationalError("rollback failed")
            return self._c.rollback()
        def close(self):
            if self._tripped: raise sqlite3.OperationalError("close failed")
            return self._c.close()
        def __getattr__(self, n): return getattr(self._c, n)

    conn = state_db.open_db(sys.argv[1])
    b = B()
    from execution.entry_limits import EntryLimitState
    LIMITS = EntryLimitState(max_open_positions=99, max_daily_entries=99,
        open_position_symbols=frozenset(), pending_entry_symbols=frozenset(),
        daily_entry_count=0, trading_day='2026-07-29')
    execution_engine.submit_buy_order(order_intent=oi,
        buy_gate_context_builder=lambda rec: order_gate.BuyGateContext(execution_broker="kis",
            live_order_enabled=True, entry_disabled=False, validated_commit="c1",
            deployed_commit="c1", kis_account_no=ACC, allowed_account_no=ACC, order_intent=oi,
            instrument=build_instrument("AAPL", exchange="NASDAQ"), signal=sig,
            is_regular_session=True, kis_price_usd=100.1, max_price_deviation_percent=0.30,
            usd_orderable_cash=1000.0, has_open_order_for_symbol=False,
            has_order_for_signal_id=False, allowed_symbols=frozenset({"AAPL"}),
            reconciliation=rec, entry_limits=LIMITS, now=NOW),
        conn=conn, broker=b, instrument=build_instrument("AAPL", exchange="NASDAQ"),
        account_id=ACC, audit_run_id=shadow_audit.new_run_id(), now=NOW)

    cycles = {"count": 0}
    def one_cycle():
        cycles["count"] += 1
        execution_engine.submit_cancel(order_intent=oi, broker_order_id="kis-1",
            cancel_gate_context_builder=lambda: order_gate.CancelGateContext(
                execution_broker="kis", broker_order_id="kis-1", is_actually_open=True,
                kis_account_no=ACC, allowed_account_no=ACC, symbol="AAPL",
                has_cancel_already_in_flight=False),
            conn=Poison(conn), broker=b,
            instrument=build_instrument("AAPL", exchange="NASDAQ"),
            audit_run_id=shadow_audit.new_run_id(), now=NOW)

    # This is the entrypoint contract, exercised through a REAL cancel.
    try:
        one_cycle()
        one_cycle()          # must never be reached
        print("NO_FATAL")
        os._exit(0)
    except order_repository.FatalRepositoryConnectionError:
        from operations import kill_switch
        print("FATAL cycles=%d halt=%s" % (cycles["count"], kill_switch.is_halted()))
        sys.stdout.flush()
        os._exit(4)
    except BaseException as exc:
        print("WRONG_TYPE %s" % type(exc).__name__)
        sys.stdout.flush()
        os._exit(1)
    """
)


class TestRealProcessFailStopThroughARealCancel:
    """CODEX-059 §8: not an injected fatal at the entrypoint -- a real
    cancel whose final-state write poisons the connection."""

    def test_child_exits_four_and_releases_the_lock(self, tmp_path):
        db_path = str(tmp_path / "PROC.db")
        state_db.open_db(db_path).close()

        result = subprocess.run(
            [sys.executable, "-c", _CHILD, db_path, str(REPO_ROOT),
             str(tmp_path / "HALT.json"), str(tmp_path / "KS.json"),
             str(tmp_path / "POS.json"), str(tmp_path / "SH.jsonl")],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=90,
        )
        assert "FATAL" in result.stdout, f"stdout={result.stdout} stderr={result.stderr[-2000:]}"
        assert result.returncode == 4, f"exit={result.returncode}"
        assert "cycles=1" in result.stdout, "a second cycle ran after the fatal fault"
        assert "halt=True" in result.stdout, "HALT was not set"

        # The child is gone; the OS released the descriptor.
        writer = state_db.connect(db_path, busy_timeout_ms=2000)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE kis_order_idempotency SET updated_at = ? WHERE internal_order_id = ?",
                ("after-restart", "p"),
            )
            writer.commit()
        finally:
            writer.close()

        verify = state_db.connect(db_path)
        try:
            row = verify.execute(
                "SELECT status, updated_at FROM kis_order_idempotency "
                "WHERE internal_order_id = 'p'"
            ).fetchone()
        finally:
            verify.close()
        assert row["updated_at"] == "after-restart"
        # The uncommitted CANCELLED never became durable.
        assert row["status"] == "CANCEL_PENDING"
