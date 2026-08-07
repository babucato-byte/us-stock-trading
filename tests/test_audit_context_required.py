"""CODEX-053: the Shadow audit context is a REQUIRED argument on every
execution path, not an optional one.

Before this, `audit_run_id` defaulted to None and the approval-audit
helper returned silently when it was None. Every current caller passed
it and tests pinned that, so nothing was broken -- but the guarantee
CODEX-048 established rested on callers remembering an argument. A
future call site that omitted it would have submitted a real order with
no record of the approval that authorized it.

These tests assert the engine now refuses instead: no audit context, no
state transition, no transport call.
"""
import ast
import pathlib
from datetime import datetime, timezone

import pytest

import shadow_audit
from domain.execution_event import ExecutionRecord
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.position import Position
from domain.signal import build_signal
from execution import execution_engine, idempotency, order_gate, order_repository
from execution.execution_engine import ExecutionEngineError, validate_audit_run_id
from state_store import db as state_db

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECON.json"))
    monkeypatch.setenv("VALIDATED_COMMIT", "c1")
    monkeypatch.setenv("DEPLOYED_COMMIT", "c1")
    monkeypatch.setenv("KIS_ALLOWED_ACCOUNT_NO", ACCOUNT_ID)
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


class _Broker:
    def __init__(self):
        self.calls = 0

    def get_positions(self):
        return []

    def get_open_orders(self):
        return []

    def get_fills(self, *, start_date, end_date):
        return []

    def submit_order(self, order_intent, instrument, *, authorization=None):
        self.calls += 1
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id="kis-1", requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=NOW, updated_at=NOW,
        )

    def cancel_order(self, order_intent, instrument, broker_order_id, *, authorization=None):
        self.calls += 1
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="CANCELLED", submitted_at=NOW, updated_at=NOW,
        )


def _submit(broker, conn, audit_run_id, oi=None):
    oi = oi or _order_intent()
    return execution_engine.submit_buy_order(
        order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
        broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID,
        audit_run_id=audit_run_id, now=NOW,
    )


class TestValidateAuditRunId:
    def test_valid_id_is_returned_normalized(self):
        assert validate_audit_run_id("  run-1  ") == "run-1"

    @pytest.mark.parametrize("bad", [None, "", "   ", "\t\n", 123, 1.5, True, [], {}, object()])
    def test_every_unusable_value_is_refused(self, bad):
        with pytest.raises(ExecutionEngineError) as excinfo:
            validate_audit_run_id(bad)
        assert excinfo.value.reason_code == "AUDIT_CONTEXT_MISSING"


class TestMissingAuditContextBlocksTheOrder:
    def test_omitting_the_argument_entirely_is_a_type_error(self):
        conn = state_db.open_db()
        broker = _Broker()
        oi = _order_intent()
        with pytest.raises(TypeError, match="audit_run_id"):
            execution_engine.submit_buy_order(  # deliberate-audit-omission
                order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
                broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID, now=NOW,
            )
        assert broker.calls == 0

    @pytest.mark.parametrize("bad", [None, "", "   ", 12345, object()])
    def test_unusable_audit_run_id_blocks_before_any_transport(self, bad):
        conn = state_db.open_db()
        broker = _Broker()
        with pytest.raises(ExecutionEngineError) as excinfo:
            _submit(broker, conn, bad)
        assert excinfo.value.reason_code == "AUDIT_CONTEXT_MISSING"
        assert broker.calls == 0

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_unusable_audit_run_id_leaves_no_order_state_behind(self, bad):
        """Validation runs BEFORE the idempotency row exists, so a refused
        order does not even occupy its own idempotency key -- a later,
        properly-audited attempt for the same signal is still possible."""
        conn = state_db.open_db()
        broker = _Broker()
        with pytest.raises(ExecutionEngineError):
            _submit(broker, conn, bad)
        assert order_repository.load(conn, "ord-1") is None
        assert shadow_audit.read_events() == []

        # The same order now succeeds once a real audit context is given.
        result = _submit(broker, conn, shadow_audit.new_run_id())
        assert result.status == "ACCEPTED"
        assert broker.calls == 1

    def test_sell_path_is_held_to_the_same_rule(self):
        conn = state_db.open_db()
        broker = _Broker()
        oi = _order_intent(side="sell", internal_order_id="sell-1", signal_id="sig-sell")

        def _sell_builder(reconciliation):
            return order_gate.SellGateContext(
                execution_broker="kis", live_order_enabled=True, order_intent=oi,
                instrument=_instrument(), kis_position_quantity=5, position_source="kis",
                has_existing_sell_order_for_symbol=False, reconciliation=reconciliation,
                kis_account_no=ACCOUNT_ID, now=NOW,
            )

        with pytest.raises(ExecutionEngineError) as excinfo:
            execution_engine.submit_sell_order(
                order_intent=oi, sell_gate_context_builder=_sell_builder, conn=conn,
                broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID,
                audit_run_id="", now=NOW,
            )
        assert excinfo.value.reason_code == "AUDIT_CONTEXT_MISSING"
        assert broker.calls == 0

    def test_cancel_path_is_held_to_the_same_rule(self):
        conn = state_db.open_db()
        broker = _Broker()
        _submit(broker, conn, shadow_audit.new_run_id())
        broker.calls = 0

        def _cancel_builder():
            return order_gate.CancelGateContext(
                execution_broker="kis", broker_order_id="kis-1", is_actually_open=True,
                kis_account_no=ACCOUNT_ID, allowed_account_no=ACCOUNT_ID, symbol="AAPL",
                has_cancel_already_in_flight=False,
            )

        with pytest.raises(ExecutionEngineError) as excinfo:
            execution_engine.submit_cancel(
                order_intent=_order_intent(), broker_order_id="kis-1",
                cancel_gate_context_builder=_cancel_builder, conn=conn, broker=broker,
                instrument=_instrument(), audit_run_id=None, now=NOW,
            )
        assert excinfo.value.reason_code == "AUDIT_CONTEXT_MISSING"
        assert broker.calls == 0
        # The order was not moved toward CANCEL_PENDING either.
        assert order_repository.load(conn, "ord-1").state == "ACCEPTED"

    def test_cancel_audits_its_approval_before_the_transport(self):
        conn = state_db.open_db()
        broker = _Broker()
        _submit(broker, conn, shadow_audit.new_run_id())

        cancel_run = shadow_audit.new_run_id()

        def _cancel_builder():
            return order_gate.CancelGateContext(
                execution_broker="kis", broker_order_id="kis-1", is_actually_open=True,
                kis_account_no=ACCOUNT_ID, allowed_account_no=ACCOUNT_ID, symbol="AAPL",
                has_cancel_already_in_flight=False,
            )

        execution_engine.submit_cancel(
            order_intent=_order_intent(), broker_order_id="kis-1",
            cancel_gate_context_builder=_cancel_builder, conn=conn, broker=broker,
            instrument=_instrument(), audit_run_id=cancel_run, now=NOW,
        )
        # CODEX-053: the run must also END. Pinning this at exactly the
        # two approval events is what let the missing terminal event
        # through the first time.
        types = [r["event_type"] for r in shadow_audit.read_events(shadow_run_id=cancel_run)]
        assert types == ["GATE_APPROVED", "EXECUTION_PLANNED", "SHADOW_COMPLETED"]


class TestValidAuditContextFlowsThroughTheLifecycle:
    def test_engine_events_all_carry_the_supplied_run_id(self):
        conn = state_db.open_db()
        broker = _Broker()
        run_id = shadow_audit.new_run_id()
        _submit(broker, conn, run_id)
        rows = shadow_audit.read_events(shadow_run_id=run_id)
        assert [r["event_type"] for r in rows] == ["GATE_APPROVED", "EXECUTION_PLANNED"]
        assert all(r["shadow_run_id"] == run_id for r in rows)

    def test_buy_lifecycle_shares_one_run_id_end_to_end(self, monkeypatch):
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

        monkeypatch.setattr(klt.pso, "load_watchlist", lambda: ["AAPL"])
        monkeypatch.setattr(klt.pso, "get_us_market_session", lambda: "regular")
        monkeypatch.setattr(klt.pso, "analyze_stock", lambda s: {
            "symbol": s, "price": 100.0, "ma200": 90.0, "rsi": 50.0, "volume_ratio": 1.5,
            "score": 100,
        })
        rollout = LiveRolloutConfig(
            enabled=True, allowed_symbols=frozenset({"AAPL"}), max_quantity_per_order=1,
            max_open_positions=1, max_daily_entries=1, regular_session_only=True,
            allow_fractional=False, allow_market_order=False, allow_extended_hours=False,
            allow_leverage=False, allow_inverse=False, allow_short=False, allow_margin=False,
            max_price_deviation_percent=0.30,
        )
        klt.run_live_buy_entry_cycle(broker=_PipelineBroker(), live_rollout=rollout, now=NOW)

        per_run = {}
        for row in shadow_audit.read_events():
            per_run.setdefault(row["shadow_run_id"], []).append(row["event_type"])
        approved = [types for types in per_run.values() if "GATE_APPROVED" in types]
        assert len(approved) == 1, per_run
        types = approved[0]
        # One run id spans signal -> engine approval -> engine plan -> terminal.
        assert types[0] == "SIGNAL_RECEIVED"
        assert "GATE_APPROVED" in types
        assert "EXECUTION_PLANNED" in types
        assert types[-1] == "SHADOW_COMPLETED"

    def test_sell_lifecycle_shares_one_run_id_end_to_end(self):
        import kis_position_manager as kpm
        from brokers.kis_broker_adapter import KISBrokerAdapter
        from domain.account_snapshot import AccountSnapshot
        from positions import lifecycle

        record = kpm.create_kis_position_after_buy(
            strategy_id="T", strategy_version="v1", symbol="AAPL", quantity=5,
            client_order_id="seed", broker_order_id="kis-seed", now=NOW,
        )
        lifecycle.record_fill(record["position_id"], 5, 100.0)
        kpm.finalize_stop_and_targets_from_fill(record["position_id"], 100.0)

        class _SellBroker(_Broker):
            def get_current_price(self, instrument):
                return 100.0

            def get_positions(self):
                return [Position(symbol="AAPL", quantity=5, average_fill_price=100.0,
                                 unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW,
                                 source="kis_balance")]

            def get_account_snapshot(self, *, source_label="kis_balance"):
                # ORACLE-CASH-01: the balance read carries no cash field.
                return AccountSnapshot(
                    krw_cash=None, usd_cash=None, usd_orderable_cash=None,
                    usd_reserved_in_open_orders=0.0, as_of=NOW, source=source_label,
                    account_id=ACCOUNT_ID, cash_source="TTTS3012R_DOES_NOT_PROVIDE",
                )

            def get_orderable_usd(self, instrument, limit_price_usd):
                return 1000.0

        adapter = KISBrokerAdapter(_SellBroker(), now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-1")
        assert response.status_code in (200, 201)

        per_run = {}
        for row in shadow_audit.read_events():
            per_run.setdefault(row["shadow_run_id"], []).append(row["event_type"])
        assert len(per_run) == 1, per_run
        types = next(iter(per_run.values()))
        assert types[0] == "SIGNAL_RECEIVED"
        assert "GATE_APPROVED" in types
        assert "EXECUTION_PLANNED" in types
        assert types[-1] == "SHADOW_COMPLETED"


class TestStaticGuarantees:
    """The runtime tests above prove today's behaviour; these prevent the
    fail-open shape from being reintroduced."""

    OPERATIONAL_FILES = (
        "execution/execution_engine.py",
        "kis_live_trading.py",
        "brokers/kis_broker_adapter.py",
        "shadow_audit.py",
    )

    def test_no_operational_signature_defaults_audit_run_id_to_none(self):
        for rel in self.OPERATIONAL_FILES:
            tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                args = node.args
                pairs = list(zip(args.kwonlyargs, args.kw_defaults))
                positional = args.args[len(args.args) - len(args.defaults):]
                pairs += list(zip(positional, args.defaults))
                for arg, default in pairs:
                    if arg.arg != "audit_run_id" or default is None:
                        continue
                    assert not (isinstance(default, ast.Constant) and default.value is None), (
                        f"{rel}:{node.lineno} {node.name}() defaults audit_run_id to None"
                    )

    def test_execution_engine_has_no_conditional_audit_skip(self):
        source = (REPO_ROOT / "execution/execution_engine.py").read_text(encoding="utf-8")
        for banned in ("if audit_run_id is None:\n        return",
                       "if not audit_run_id:",
                       "audit_run_id or "):
            assert banned not in source, f"execution_engine.py still contains {banned!r}"

    def test_every_real_caller_passes_audit_run_id(self):
        """Every call to an engine entry point -- in operational code AND
        in tests -- must name audit_run_id explicitly."""
        entry_points = {"submit_buy_order", "submit_sell_order", "submit_cancel",
                        "_submit_new_order"}
        offenders = []
        for path in sorted(REPO_ROOT.glob("**/*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith("venv/") or rel == "execution/execution_engine.py":
                continue
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except SyntaxError:  # pragma: no cover -- not our files
                continue
            lines = source.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name not in entry_points:
                    continue
                keywords = {kw.arg for kw in node.keywords}
                if None in keywords:  # **kwargs forwarding
                    continue
                if "audit_run_id" in keywords:
                    continue
                # The one sanctioned exception: the test that proves
                # omitting the argument raises TypeError must omit it.
                if "deliberate-audit-omission" in lines[node.lineno - 1]:
                    continue
                offenders.append(f"{rel}:{node.lineno}")
        assert offenders == [], f"engine calls without audit_run_id: {offenders}"
