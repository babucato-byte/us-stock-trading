"""Assemble a StatusSnapshot from local files and env-derived config only.

No section here ever performs a network call (no real Alpaca or Slack
API request) -- every value is either read from a local file this
project already maintains, or derived from environment variables/
BrokerConfig. Sections that would need a live value the project doesn't
otherwise persist (unrealized PnL without a current price, daily loss
without a live account snapshot) are reported as NOT_AVAILABLE rather
than fabricated or silently omitted, and accept an optional caller-
supplied value (current_prices, account) for when one is already in hand
from elsewhere in the same process.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

NOT_AVAILABLE = "NOT_AVAILABLE"


@dataclass
class SectionResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None


@dataclass
class StatusSnapshot:
    generated_at: str
    mode: SectionResult
    active_strategy: SectionResult
    market_state: SectionResult
    watchlist: SectionResult
    orders_today: SectionResult
    positions: SectionResult
    pnl: SectionResult
    kill_switch: SectionResult
    slack: SectionResult
    broker: SectionResult
    reconciliation: SectionResult
    last_successful_run: SectionResult


def _safe(fn):
    try:
        return SectionResult(ok=True, data=fn())
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: one broken section must never sink the dashboard
        return SectionResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def _mode_data():
    from broker.broker_config import BrokerConfig
    cfg = BrokerConfig()
    return {"trading_mode": cfg.trading_mode, "status_label": cfg.status_label,
            "can_submit_live_order": cfg.can_submit_live_order}


def _active_strategy_data(registry):
    from strategy.registry import default_registry
    reg = registry or default_registry
    strat = reg.get_active_strategy()
    if strat is None:
        return None
    return {"strategy_id": strat.strategy_id, "version": strat.version, "status": strat.status}


def _market_state_data(now):
    from market_hours import get_market_state_info
    info = get_market_state_info(now)
    return {"state": info.state, "label": info.label, "detail": info.detail}


def _watchlist_data():
    from scalping_watchlist.repository import load_watchlist
    df = load_watchlist()
    active_rows = df[df["status"] == "ACTIVE"] if "status" in df.columns else df
    return {"active_count": len(active_rows), "symbols": active_rows["symbol"].tolist() if "symbol" in active_rows.columns else []}


def _orders_today_data():
    import paper_strategy_order as pso
    today = pso.eastern_now().strftime("%Y-%m-%d")
    df = pso.load_order_history()
    todays = df[df["order_date"].astype(str) == today] if "order_date" in df.columns else df.iloc[0:0]
    return {"date": today, "count": len(todays), "symbols": todays["symbol"].tolist() if "symbol" in todays.columns else []}


def _positions_data(current_prices):
    from positions import states, store
    all_positions = store.load_all()
    open_positions = []
    for record in all_positions.values():
        if record["state"] in states.TERMINAL_STATES:
            continue
        entry = {
            "position_id": record["position_id"], "symbol": record["symbol"], "state": record["state"],
            "strategy_id": record["strategy_id"], "remaining_qty": record["remaining_qty"],
            "stop_price": record["stop_price"], "target_1_price": record["target_1_price"],
            "target_2_price": record["target_2_price"], "realized_pnl": record["realized_pnl"],
        }
        current_price = (current_prices or {}).get(record["symbol"])
        if current_price is not None:
            from positions.lifecycle import compute_unrealized_pnl
            entry["unrealized_pnl"] = compute_unrealized_pnl(record, current_price)
        else:
            entry["unrealized_pnl"] = NOT_AVAILABLE
        open_positions.append(entry)
    return {"open_count": len(open_positions), "positions": open_positions, "total_tracked": len(all_positions)}


def _pnl_data(positions_section):
    if not positions_section.ok:
        return {"realized_total": NOT_AVAILABLE, "unrealized_total": NOT_AVAILABLE}
    open_positions = positions_section.data["positions"]
    realized_total = sum(p["realized_pnl"] or 0.0 for p in open_positions)
    unrealized_values = [p["unrealized_pnl"] for p in open_positions if p["unrealized_pnl"] != NOT_AVAILABLE]
    unrealized_total = sum(unrealized_values) if unrealized_values else (
        0.0 if not open_positions else NOT_AVAILABLE
    )
    return {"realized_total_open_positions": realized_total, "unrealized_total": unrealized_total}


def _daily_loss_data(account):
    if account is None:
        return NOT_AVAILABLE
    equity = account.get("equity")
    last_equity = account.get("last_equity")
    if equity is None or last_equity in (None, 0, "0"):
        return NOT_AVAILABLE
    equity_f, last_equity_f = float(equity), float(last_equity)
    return {"daily_pnl": equity_f - last_equity_f, "daily_pnl_rate": (equity_f - last_equity_f) / last_equity_f}


def _kill_switch_data():
    from kill_switch import is_trading_halted
    from kill_switch_state import get_current_record
    return {"binary_halted": is_trading_halted(), "state_machine": get_current_record()}


def _slack_data():
    import os
    return {
        "webhook_configured": bool(os.environ.get("SLACK_WEBHOOK_URL")),
        "alert_webhook_configured": bool(os.environ.get("SLACK_ALERT_WEBHOOK_URL")),
        "note": "presence check only -- no live network call made, dashboard works even if Slack itself is down",
    }


def _broker_data():
    from broker.broker_config import BrokerConfig
    cfg = BrokerConfig()
    return {
        "status_label": cfg.status_label, "trading_mode": cfg.trading_mode,
        "credentials_present": bool(cfg.api_key and cfg.secret_key),
        "note": "config-derived only -- no live connectivity check made",
    }


def _reconciliation_data():
    import paper_strategy_order as pso
    df = pso.load_reconciliation()
    if "local_status" not in df.columns:
        return {"total": 0, "pending": 0, "terminal": 0}
    pending = df["local_status"].isin(pso.RECONCILIATION_NON_TERMINAL_STATUSES).sum()
    terminal = df["local_status"].isin(pso.RECONCILIATION_TERMINAL_STATUSES).sum()
    return {"total": len(df), "pending": int(pending), "terminal": int(terminal)}


def _last_successful_run_data():
    """ASSUMPTION: no dedicated "last run" marker file exists yet in this
    project, so this uses the most recent mtime among order_history.csv
    and order_reconciliation.csv as a proxy for "last time the order
    engine actually wrote something." Documented as an approximation, not
    a precise run-completion timestamp."""
    import paper_strategy_order as pso
    candidates = [pso.ORDER_HISTORY_FILE, pso.ORDER_RECONCILIATION_FILE]
    mtimes = [(p, p.stat().st_mtime) for p in candidates if p.exists()]
    if not mtimes:
        return NOT_AVAILABLE
    path, mtime = max(mtimes, key=lambda pair: pair[1])
    return {"source_file": path.name, "last_modified_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()}


def build_snapshot(*, now=None, registry=None, account=None, current_prices=None):
    """Build a full StatusSnapshot. Every section is individually
    fault-tolerant (see SectionResult) -- no single broken data source
    can prevent the rest of the snapshot from being produced."""
    generated_at = datetime.now(timezone.utc).isoformat()

    mode = _safe(_mode_data)
    active_strategy = _safe(lambda: _active_strategy_data(registry))
    market_state = _safe(lambda: _market_state_data(now))
    watchlist = _safe(_watchlist_data)
    orders_today = _safe(_orders_today_data)
    positions = _safe(lambda: _positions_data(current_prices))
    pnl = _safe(lambda: _pnl_data(positions))
    kill_switch = _safe(_kill_switch_data)
    slack = _safe(_slack_data)
    broker = _safe(_broker_data)
    reconciliation = _safe(_reconciliation_data)
    last_successful_run = _safe(_last_successful_run_data)

    return StatusSnapshot(
        generated_at=generated_at, mode=mode, active_strategy=active_strategy, market_state=market_state,
        watchlist=watchlist, orders_today=orders_today, positions=positions, pnl=pnl,
        kill_switch=kill_switch, slack=slack, broker=broker, reconciliation=reconciliation,
        last_successful_run=last_successful_run,
    )
