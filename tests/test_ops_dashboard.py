"""Stage 9: operations monitoring dashboard tests.

Every test isolates every underlying file (order history, reconciliation,
watchlist, position store, kill switch) to tmp_path -- never touches real
operational files. No real network calls anywhere (Slack/broker sections
are config-presence checks only, verified here to never attempt one).
"""
import json

import pandas as pd
import pytest

from ops_dashboard import cli
from ops_dashboard.snapshot import NOT_AVAILABLE, build_snapshot
from positions import states, store


@pytest.fixture(autouse=True)
def pso(tmp_path, monkeypatch):
    """Autouse (every test gets the file isolation) but also explicitly
    requestable by name (test bodies that need to call e.g.
    pso.initialize_order_history() declare `pso` as a parameter to get
    back the exact module object this fixture patched).

    Imported fresh here, at fixture-execution time, rather than at this
    file's module level: some other test in the suite
    (test_ai_analysis.py::test_ai_analysis_is_independent_from_order_modules)
    legitimately does sys.modules.pop("paper_strategy_order", ...) as part
    of what IT is testing. A module-level `import paper_strategy_order as
    pso` bound once at collection time would go stale after that pop --
    ops_dashboard/snapshot.py's own internal imports would transparently
    pick up a freshly re-imported (unpatched) module object, silently
    diverging from whatever got patched here. Importing at fixture-run
    time always binds whatever object is currently in sys.modules, which
    is what snapshot.py's own local imports will see too.
    """
    import paper_strategy_order as pso_module
    import scalping_watchlist.repository as wl_repo

    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL_SWITCH"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH_STATE.json"))
    monkeypatch.setattr(pso_module, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso_module, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso_module, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso_module, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SLACK_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(wl_repo, "WATCHLIST_FILE", tmp_path / "scalping_watchlist.csv")
    return pso_module


def test_build_snapshot_never_raises_even_with_nothing_initialized():
    snapshot = build_snapshot()
    assert snapshot.generated_at is not None
    # order_history.csv doesn't exist yet -> that section fails gracefully...
    assert snapshot.orders_today.ok is False
    # ...but sections with legitimate "missing = empty" semantics still succeed.
    assert snapshot.positions.ok is True
    assert snapshot.positions.data["open_count"] == 0
    assert snapshot.kill_switch.ok is True


def test_mode_and_broker_sections_are_config_only_no_network():
    snapshot = build_snapshot()
    assert snapshot.mode.ok is True
    assert "status_label" in snapshot.mode.data
    assert snapshot.broker.ok is True
    assert "no live connectivity check made" in snapshot.broker.data["note"]


def test_slack_section_reports_configured_state_without_network_call():
    snapshot = build_snapshot()
    assert snapshot.slack.ok is True
    assert snapshot.slack.data["webhook_configured"] is False
    assert "no live network call made" in snapshot.slack.data["note"]


def test_slack_section_reflects_env_presence(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.invalid/webhook")
    snapshot = build_snapshot()
    assert snapshot.slack.data["webhook_configured"] is True


def test_kill_switch_section_reports_both_mechanisms():
    snapshot = build_snapshot()
    assert snapshot.kill_switch.ok is True
    assert "binary_halted" in snapshot.kill_switch.data
    assert "state_machine" in snapshot.kill_switch.data


def test_kill_switch_binary_halt_reflected(tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("halted")
    snapshot = build_snapshot()
    assert snapshot.kill_switch.data["binary_halted"] is True


def test_orders_today_counts_only_todays_rows(tmp_path, pso):
    pso.initialize_order_history()
    today = pso.eastern_now().strftime("%Y-%m-%d")
    df = pd.DataFrame([
        {"symbol": "AAPL", "order_date": today, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
        {"symbol": "MSFT", "order_date": "2020-01-01", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
    ], columns=pso.REQUIRED_HISTORY_COLUMNS)
    df.to_csv(pso.ORDER_HISTORY_FILE, index=False)

    snapshot = build_snapshot()
    assert snapshot.orders_today.ok is True
    assert snapshot.orders_today.data["count"] == 1
    assert snapshot.orders_today.data["symbols"] == ["AAPL"]


def test_positions_section_lists_only_non_terminal(tmp_path):
    open_pos = store.create_position("S", "1.0", "AAPL", "coid-1", 10)
    with store.locked_position(open_pos["position_id"]) as locked:
        states.validate_transition(locked["state"], states.ARMED)
        locked["state"] = states.ARMED
        locked["state_history"].append({"state": states.ARMED, "at": "t", "reason": "test"})

    closed_pos = store.create_position("S", "1.0", "MSFT", "coid-2", 5)
    with store.locked_position(closed_pos["position_id"]) as locked:
        for target in (states.ARMED, states.REJECTED):
            states.validate_transition(locked["state"], target)
            locked["state"] = target
            locked["state_history"].append({"state": target, "at": "t", "reason": "test"})

    snapshot = build_snapshot()
    assert snapshot.positions.ok is True
    assert snapshot.positions.data["open_count"] == 1
    assert snapshot.positions.data["total_tracked"] == 2
    assert snapshot.positions.data["positions"][0]["symbol"] == "AAPL"


def test_positions_unrealized_pnl_not_available_without_current_price():
    store.create_position("S", "1.0", "AAPL", "coid-1", 10)  # SETUP_DETECTED, non-terminal
    snapshot = build_snapshot()
    entries = [p for p in snapshot.positions.data["positions"] if p["symbol"] == "AAPL"]
    assert len(entries) == 1
    assert entries[0]["unrealized_pnl"] == NOT_AVAILABLE


def test_positions_unrealized_pnl_computed_when_current_price_supplied():
    pos = store.create_position("S", "1.0", "MSFT", "coid-2", 10)
    with store.locked_position(pos["position_id"]) as locked:
        for target in (states.ARMED, states.ENTRY_RESERVED, states.ENTRY_SUBMITTED):
            states.validate_transition(locked["state"], target)
            locked["state"] = target
        locked["average_fill_price"] = 100.0
        locked["remaining_qty"] = 10
        locked["state_history"].append({"state": locked["state"], "at": "t", "reason": "test"})

    snapshot = build_snapshot(current_prices={"MSFT": 103.0})
    entries = [p for p in snapshot.positions.data["positions"] if p["symbol"] == "MSFT"]
    assert entries[0]["unrealized_pnl"] == pytest.approx(30.0)


def test_reconciliation_section_counts_pending_and_terminal(tmp_path, pso):
    df = pd.DataFrame([
        {"client_order_id": "c1", "symbol": "AAPL", "order_date": "2026-07-25", "requested_qty": 1,
         "filled_qty": None, "remaining_qty": None, "average_fill_price": None,
         "broker_status": None, "local_status": "PENDING_SUBMISSION", "last_reconciled_at": "t"},
        {"client_order_id": "c2", "symbol": "MSFT", "order_date": "2026-07-25", "requested_qty": 1,
         "filled_qty": 1, "remaining_qty": 0, "average_fill_price": 100.0,
         "broker_status": "filled", "local_status": "FILLED", "last_reconciled_at": "t"},
    ], columns=pso.RECONCILIATION_COLUMNS)
    df.to_csv(pso.ORDER_RECONCILIATION_FILE, index=False)

    snapshot = build_snapshot()
    assert snapshot.reconciliation.ok is True
    assert snapshot.reconciliation.data == {"total": 2, "pending": 1, "terminal": 1}


def test_last_successful_run_not_available_when_no_files_exist():
    snapshot = build_snapshot()
    assert snapshot.last_successful_run.data == NOT_AVAILABLE


def test_last_successful_run_reports_most_recent_file(tmp_path, pso):
    pso.initialize_order_history()
    snapshot = build_snapshot()
    assert snapshot.last_successful_run.ok is True
    assert snapshot.last_successful_run.data["source_file"] == "order_history.csv"


def test_render_text_includes_every_section():
    snapshot = build_snapshot()
    text = cli.render_text(snapshot)
    for title in ["Mode", "Active Strategy", "Market State", "Watchlist", "Orders Today",
                  "Positions", "PnL", "Kill Switch", "Slack", "Broker", "Reconciliation",
                  "Last Successful Run"]:
        assert f"[{title}]" in text


def test_render_text_shows_unavailable_for_failed_section():
    snapshot = build_snapshot()  # order_history.csv missing -> orders_today fails
    text = cli.render_text(snapshot)
    assert "[Orders Today] UNAVAILABLE" in text


def test_no_real_network_module_imported_by_snapshot():
    import inspect
    import ops_dashboard.snapshot as snap_mod
    source = inspect.getsource(snap_mod)
    assert "requests.post" not in source
    assert "requests.get" not in source
