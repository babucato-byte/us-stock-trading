"""CODEX-049: the Shadow EXIT evaluation service.

Excluding the sell/exit tick from the deployment made Oracle Shadow
verification of the exit path impossible. This service restores it while
keeping the read-only posture: it decides, records, and submits nothing.

The decisive property under test is that a Shadow exit pass reaches ZERO
transport calls while still producing the same verdict the live path
would -- because both call `positions.lifecycle.decide_exit()`.
"""
import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import kis_position_manager as kpm
import shadow_audit
from clock import FrozenClock
from domain.account_snapshot import AccountSnapshot
from domain.position import Position
from execution import idempotency
from market_hours import combine_eastern
from positions import lifecycle, states, store
from state_store import db as state_db

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
EASTERN_MIDSESSION = combine_eastern(datetime(2026, 7, 29).date(), __import__(
    "datetime").time(11, 0))
ACCOUNT_ID = "12345678"


def _load(module_name):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        module = importlib.import_module(module_name)
        return importlib.reload(module)
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECON.json"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    for flag in ("LIVE_ENABLE_PARTIAL_PROFIT", "LIVE_ENABLE_TRAILING_STOP",
                 "LIVE_ENABLE_TIME_STOP", "LIVE_ENABLE_EOD_EXIT"):
        monkeypatch.delenv(flag, raising=False)
    state_db.open_db().close()
    yield


class _ReadOnlyBroker:
    """Records EVERY method call, so a test can assert that no
    order-submitting method was ever reached."""

    def __init__(self, price=100.0, positions=None, open_orders=None, fills=None):
        self.price = price
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders if open_orders is not None else []
        self.fills = fills if fills is not None else []
        self.calls = []

    def get_current_price(self, instrument):
        self.calls.append("get_current_price")
        return self.price

    def get_positions(self):
        self.calls.append("get_positions")
        return self.positions

    def get_open_orders(self):
        self.calls.append("get_open_orders")
        return self.open_orders

    def get_fills(self, *, start_date, end_date):
        self.calls.append("get_fills")
        return self.fills

    def get_account_snapshot(self, *, source_label="kis_balance"):
        self.calls.append("get_account_snapshot")
        return AccountSnapshot(
            krw_cash=0.0, usd_cash=10000.0, usd_orderable_cash=10000.0,
            usd_reserved_in_open_orders=0.0, as_of=NOW, source=source_label,
            account_id=ACCOUNT_ID,
        )

    def submit_order(self, *args, **kwargs):  # pragma: no cover -- must never run
        self.calls.append("submit_order")
        raise AssertionError("the Shadow exit service submitted an order")

    def cancel_order(self, *args, **kwargs):  # pragma: no cover -- must never run
        self.calls.append("cancel_order")
        raise AssertionError("the Shadow exit service cancelled an order")


def _seed_position(symbol="AAPL", qty=10, avg_price=100.0):
    record = kpm.create_kis_position_after_buy(
        strategy_id="TEST", strategy_version="v1", symbol=symbol, quantity=qty,
        client_order_id=f"seed-{symbol}", broker_order_id=f"kis-seed-{symbol}", now=NOW,
    )
    lifecycle.record_fill(record["position_id"], qty, avg_price)
    return kpm.finalize_stop_and_targets_from_fill(record["position_id"], avg_price)


def _kis_position(symbol="AAPL", qty=10, avg_price=100.0):
    return Position(symbol=symbol, quantity=qty, average_fill_price=avg_price,
                    unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis_balance")


def _events():
    return shadow_audit.read_events()


def _event_types():
    return [row["event_type"] for row in _events()]


class TestShadowExitEvaluation:
    def test_stop_loss_condition_is_evaluated_and_recorded_without_ordering(self):
        module = _load("run_shadow_exit_evaluation")
        record = _seed_position(qty=10, avg_price=100.0)
        broker = _ReadOnlyBroker(price=record["stop_price"] - 1.0,
                                 positions=[_kis_position(qty=10)])
        result = module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)

        assert "submit_order" not in broker.calls
        assert "cancel_order" not in broker.calls
        outcome = result["evaluated"][0]
        assert outcome["decision"] == lifecycle.ACTION_FULL_EXIT
        assert outcome["reason_code"] == "STOP_LOSS"
        types = _event_types()
        assert "GATE_APPROVED" in types
        assert "EXECUTION_PLANNED" in types
        assert shadow_audit.audit_integrity_report()["runs_without_terminal_event"] == []

    def test_no_exit_condition_is_recorded_as_info(self):
        module = _load("run_shadow_exit_evaluation")
        record = _seed_position(qty=10, avg_price=100.0)
        midway = (record["stop_price"] + record["target_1_price"]) / 2
        broker = _ReadOnlyBroker(price=midway, positions=[_kis_position(qty=10)])
        result = module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)

        outcome = result["evaluated"][0]
        assert outcome["decision"] == lifecycle.ACTION_NONE
        assert outcome["reason_code"] == "NO_EXIT_CONDITION"
        assert "submit_order" not in broker.calls

    def test_target_2_full_exit_is_evaluated(self):
        module = _load("run_shadow_exit_evaluation")
        record = _seed_position(qty=10, avg_price=100.0)
        broker = _ReadOnlyBroker(price=record["target_2_price"] + 1.0,
                                 positions=[_kis_position(qty=10)])
        result = module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)
        outcome = result["evaluated"][0]
        # With partial profit off (the default posture) a STOP_ACTIVE
        # position past target_2 takes the full exit -- the CODEX-046
        # behaviour, evaluated here without submitting it.
        assert outcome["decision"] == lifecycle.ACTION_FULL_EXIT
        assert outcome["reason_code"] == "TARGET_2"
        assert "submit_order" not in broker.calls

    def test_partial_profit_flag_changes_the_shadow_verdict(self, monkeypatch):
        monkeypatch.setenv("LIVE_ENABLE_PARTIAL_PROFIT", "true")
        module = _load("run_shadow_exit_evaluation")
        record = _seed_position(qty=10, avg_price=100.0)
        broker = _ReadOnlyBroker(price=record["target_1_price"] + 0.5,
                                 positions=[_kis_position(qty=10)])
        result = module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)
        assert result["evaluated"][0]["decision"] == lifecycle.ACTION_PARTIAL_EXIT
        assert "submit_order" not in broker.calls

    def test_reconciliation_mismatch_is_recorded_as_blocked(self):
        module = _load("run_shadow_exit_evaluation")
        record = _seed_position(qty=10, avg_price=100.0)
        # KIS reports 9 shares, the internal store says 10.
        broker = _ReadOnlyBroker(price=record["stop_price"] - 1.0,
                                 positions=[_kis_position(qty=9)])
        result = module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)
        outcome = result["evaluated"][0]
        assert outcome["reason_code"] == "RECONCILIATION_DIRTY"
        assert "RECONCILIATION_BLOCKED" in _event_types()
        assert "submit_order" not in broker.calls

    def test_price_read_failure_is_recorded_as_blocked(self):
        module = _load("run_shadow_exit_evaluation")
        _seed_position(qty=10, avg_price=100.0)

        class _NoPriceBroker(_ReadOnlyBroker):
            def get_current_price(self, instrument):
                raise RuntimeError("KIS price unavailable")

        broker = _NoPriceBroker(positions=[_kis_position(qty=10)])
        result = module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)
        assert result["evaluated"][0]["reason_code"] == "PRICE_UNAVAILABLE"
        assert "PRICE_DEVIATION_BLOCKED" in _event_types()

    def test_unknown_order_blocks_the_shadow_exit_verdict(self):
        from helpers_order_state import register_and_drive

        module = _load("run_shadow_exit_evaluation")
        conn = state_db.open_db()
        register_and_drive(
            conn, internal_order_id="prior-1", signal_id="prior-1", symbol="MSFT", side="buy",
            trading_date="2026-07-29", target="UNKNOWN", broker_order_id="kis-prior",
        )
        conn.close()
        record = _seed_position(qty=10, avg_price=100.0)
        broker = _ReadOnlyBroker(price=record["stop_price"] - 1.0,
                                 positions=[_kis_position(qty=10)],
                                 open_orders=[{"ODNO": "kis-prior"}])
        result = module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)
        assert result["evaluated"][0]["reason_code"] in ("UNKNOWN_ORDER", "RECONCILIATION_DIRTY")
        assert "submit_order" not in broker.calls

    def test_every_run_has_exactly_one_terminal_event(self):
        module = _load("run_shadow_exit_evaluation")
        record = _seed_position(qty=10, avg_price=100.0)
        broker = _ReadOnlyBroker(price=record["stop_price"] - 1.0,
                                 positions=[_kis_position(qty=10)])
        module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)
        report = shadow_audit.audit_integrity_report()
        assert report["runs_without_terminal_event"] == []
        assert report["runs_with_multiple_terminal_events"] == []
        assert report["total_runs"] >= 1

    def test_position_state_is_not_mutated_by_a_shadow_pass(self):
        module = _load("run_shadow_exit_evaluation")
        record = _seed_position(qty=10, avg_price=100.0)
        position_id = record["position_id"]
        before = store.load_position(position_id)
        broker = _ReadOnlyBroker(price=record["stop_price"] - 1.0,
                                 positions=[_kis_position(qty=10)])
        module.run_once(broker=broker, now=NOW, eastern_now=EASTERN_MIDSESSION)
        after = store.load_position(position_id)
        assert after["state"] == before["state"]
        assert after["remaining_qty"] == before["remaining_qty"]


class TestDecideExitMatchesLiveBehaviour:
    """The Shadow verdict is only meaningful if it is the SAME decision
    the live path takes. Both call decide_exit(), and these assert the
    live dispatcher honours it."""

    def test_stop_loss_decision_matches_what_check_and_manage_executes(self):
        record = _seed_position(qty=10, avg_price=100.0)
        decision = lifecycle.decide_exit(
            store.load_position(record["position_id"]),
            current_price=record["stop_price"] - 1.0, now=EASTERN_MIDSESSION,
            enable_partial_profit=False, enable_trailing_stop=False,
            enable_time_stop=False, enable_eod_exit=False,
        )
        assert decision.action == lifecycle.ACTION_FULL_EXIT
        assert decision.reason == "STOP_LOSS"

        class _ExitBroker:
            def __init__(self):
                self.submitted = []

            @property
            def config(self):
                class _C:
                    is_live_mode = False
                return _C()

            def submit_order(self, symbol, qty=1, *, side, **kwargs):
                self.submitted.append((symbol, qty, side))

                class _R:
                    status_code = 200
                    data = {"id": "x", "status": "accepted", "filled_qty": None,
                            "filled_avg_price": None}
                return _R()

        broker = _ExitBroker()
        lifecycle.check_and_manage(
            record["position_id"], current_price=record["stop_price"] - 1.0,
            now=EASTERN_MIDSESSION, broker=broker, order_date="2026-07-29",
            enable_partial_profit=False, enable_trailing_stop=False,
            enable_time_stop=False, enable_eod_exit=False,
        )
        assert broker.submitted, "the live path did not act on the same decision"

    def test_no_exit_decision_means_the_live_path_submits_nothing(self):
        record = _seed_position(qty=10, avg_price=100.0)
        midway = (record["stop_price"] + record["target_1_price"]) / 2
        decision = lifecycle.decide_exit(
            store.load_position(record["position_id"]), current_price=midway,
            now=EASTERN_MIDSESSION,
        )
        assert decision.action == lifecycle.ACTION_NONE

        class _NoOrderBroker:
            @property
            def config(self):
                class _C:
                    is_live_mode = False
                return _C()

            def submit_order(self, *args, **kwargs):
                raise AssertionError("no exit was due, but the live path ordered")

        lifecycle.check_and_manage(
            record["position_id"], current_price=midway, now=EASTERN_MIDSESSION,
            broker=_NoOrderBroker(), order_date="2026-07-29",
        )

    def test_time_stop_decision(self):
        record = _seed_position(qty=10, avg_price=100.0)
        loaded = store.load_position(record["position_id"])
        loaded = dict(loaded)
        loaded["entry_time"] = (EASTERN_MIDSESSION - timedelta(days=1)).isoformat()
        decision = lifecycle.decide_exit(
            loaded, current_price=loaded["target_1_price"] - 0.5, now=EASTERN_MIDSESSION,
            enable_time_stop=True,
        )
        assert decision.action == lifecycle.ACTION_FULL_EXIT
        assert decision.reason == "TIME_STOP"

    def test_disabled_flags_suppress_their_branches(self):
        record = _seed_position(qty=10, avg_price=100.0)
        loaded = dict(store.load_position(record["position_id"]))
        loaded["entry_time"] = (EASTERN_MIDSESSION - timedelta(days=1)).isoformat()
        decision = lifecycle.decide_exit(
            loaded, current_price=loaded["target_1_price"] - 0.5, now=EASTERN_MIDSESSION,
            enable_time_stop=False, enable_eod_exit=False,
        )
        assert decision.action == lifecycle.ACTION_NONE


class TestHealthAndMigrationEntrypoints:
    def test_health_report_is_clean_on_a_fresh_healthy_deployment(self):
        module = _load("run_health_report")
        from reconciliation import reconciliation_state

        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW, unknown_count=0, halt=False)
        report = module.collect(now=NOW, check_live_unit=False)
        assert report["healthy"] is True, report["problems"]
        assert report["schema_version"] >= 9

    def test_health_report_flags_unknown_orders(self):
        from helpers_order_state import register_and_drive

        module = _load("run_health_report")
        from reconciliation import reconciliation_state

        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW, unknown_count=0, halt=False)
        conn = state_db.open_db()
        register_and_drive(
            conn, internal_order_id="u-1", signal_id="u-1", symbol="AAPL", side="buy",
            trading_date="2026-07-29", target="UNKNOWN",
        )
        conn.close()
        report = module.collect(now=NOW, check_live_unit=False)
        assert report["healthy"] is False
        assert any("UNKNOWN" in problem for problem in report["problems"])

    def test_health_report_flags_a_run_without_a_terminal_event(self):
        module = _load("run_health_report")
        from reconciliation import reconciliation_state

        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW, unknown_count=0, halt=False)
        shadow_audit.record_event(
            shadow_run_id="dangling", event_type=shadow_audit.SIGNAL_RECEIVED,
            result=shadow_audit.RESULT_INFO, now=NOW,
        )
        report = module.collect(now=NOW, check_live_unit=False)
        assert report["healthy"] is False
        assert any("terminal event" in problem for problem in report["problems"])

    def test_health_report_flags_a_missing_reconciliation_record(self):
        module = _load("run_health_report")
        report = module.collect(now=NOW, check_live_unit=False)
        assert report["healthy"] is False
        assert any("reconciliation" in problem for problem in report["problems"])

    def test_migration_entrypoint_brings_the_schema_current(self, tmp_path):
        module = _load("run_migrations")
        from state_store.migrations import CURRENT_SCHEMA_VERSION

        version = module.run_once(str(tmp_path / "FRESH.db"))
        assert version == CURRENT_SCHEMA_VERSION
