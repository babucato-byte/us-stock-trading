from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from broker import AlpacaBroker


BASE_DIR = Path(__file__).resolve().parent
ORDER_HISTORY_FILE = BASE_DIR / "order_history.csv"
PERFORMANCE_SUMMARY_FILE = BASE_DIR / "performance_summary.csv"
PERFORMANCE_TRADES_FILE = BASE_DIR / "performance_trades.csv"

SUMMARY_COLUMNS = [
    "total_orders",
    "filled_orders",
    "canceled_orders",
    "rejected_orders",
    "open_positions",
    "win_rate",
    "avg_profit_pct",
    "avg_loss_pct",
    "profit_factor",
    "total_unrealized_pl",
    "daily_return_pct",
    "best_symbol",
    "worst_symbol",
    "generated_at",
]

TRADE_COLUMNS = [
    "symbol",
    "side",
    "qty",
    "filled_avg_price",
    "current_price",
    "unrealized_pl",
    "unrealized_plpc",
    "status",
    "submitted_at",
    "filled_at",
]


def read_csv(path, columns=None):
    if not path.exists():
        return pd.DataFrame(columns=columns or [])
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame(columns=columns or [])


def safe_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def fetch_alpaca_snapshot(broker=None, order_limit=500):
    broker = broker or AlpacaBroker()
    snapshot = {"orders": [], "positions": [], "account": {}, "error": ""}
    try:
        snapshot["orders"] = broker.get_recent_orders(limit=order_limit) or []
    except Exception as exc:
        snapshot["error"] = f"orders: {exc}"
    try:
        snapshot["positions"] = broker.get_positions() or []
    except Exception as exc:
        snapshot["error"] = append_error(snapshot["error"], f"positions: {exc}")
    try:
        snapshot["account"] = broker.get_account() or {}
    except Exception as exc:
        snapshot["error"] = append_error(snapshot["error"], f"account: {exc}")
    return snapshot


def append_error(current, message):
    return f"{current}; {message}" if current else message


def normalize_orders(orders):
    if not orders:
        return pd.DataFrame(
            columns=[
                "id",
                "symbol",
                "side",
                "qty",
                "filled_avg_price",
                "status",
                "submitted_at",
                "filled_at",
            ]
        )
    df = pd.DataFrame(orders)
    for column in ["id", "symbol", "side", "qty", "filled_avg_price", "status", "submitted_at", "filled_at"]:
        if column not in df.columns:
            df[column] = None
    return df


def normalize_positions(positions):
    if not positions:
        return pd.DataFrame(
            columns=["symbol", "qty", "current_price", "unrealized_pl", "unrealized_plpc"]
        )
    df = pd.DataFrame(positions)
    for column in ["symbol", "qty", "current_price", "unrealized_pl", "unrealized_plpc"]:
        if column not in df.columns:
            df[column] = None
    return df


def build_performance_trades(orders_df, positions_df):
    position_map = {
        str(row["symbol"]): row
        for _, row in positions_df.iterrows()
        if pd.notna(row.get("symbol"))
    }
    rows = []
    for _, order in orders_df.iterrows():
        symbol = str(order.get("symbol") or "")
        position = position_map.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "side": order.get("side"),
                "qty": order.get("qty"),
                "filled_avg_price": order.get("filled_avg_price"),
                "current_price": position.get("current_price") if position is not None else None,
                "unrealized_pl": position.get("unrealized_pl") if position is not None else 0.0,
                "unrealized_plpc": normalize_plpc(position.get("unrealized_plpc")) if position is not None else 0.0,
                "status": order.get("status"),
                "submitted_at": order.get("submitted_at"),
                "filled_at": order.get("filled_at"),
            }
        )
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def normalize_plpc(value):
    number = safe_float(value)
    if abs(number) <= 1:
        return number * 100
    return number


def calculate_win_rate(trades_df):
    filled = trades_df[trades_df["status"].astype(str).str.lower() == "filled"] if not trades_df.empty else trades_df
    measured = filled[pd.to_numeric(filled.get("unrealized_pl", pd.Series(dtype=float)), errors="coerce").notna()]
    if measured.empty:
        return 0.0
    wins = (pd.to_numeric(measured["unrealized_pl"], errors="coerce") > 0).sum()
    return round((wins / len(measured)) * 100, 2)


def calculate_profit_factor(trades_df):
    if trades_df.empty or "unrealized_pl" not in trades_df.columns:
        return 0.0
    pl = pd.to_numeric(trades_df["unrealized_pl"], errors="coerce").fillna(0.0)
    gross_profit = pl[pl > 0].sum()
    gross_loss = abs(pl[pl < 0].sum())
    if gross_loss == 0:
        return round(float(gross_profit), 2) if gross_profit > 0 else 0.0
    return round(float(gross_profit / gross_loss), 2)


def calculate_daily_return_pct(account):
    equity = safe_float(account.get("equity"))
    last_equity = safe_float(account.get("last_equity"))
    if last_equity == 0:
        return 0.0
    return round(((equity - last_equity) / last_equity) * 100, 2)


def count_recent_orders(orders_df, local_history, days):
    cutoff = datetime.now() - timedelta(days=days)
    dates = []
    if not orders_df.empty and "submitted_at" in orders_df.columns:
        dates.extend(pd.to_datetime(orders_df["submitted_at"], errors="coerce").dropna().tolist())
    if not local_history.empty and "order_date" in local_history.columns:
        dates.extend(pd.to_datetime(local_history["order_date"], errors="coerce").dropna().tolist())
    return sum(1 for item in dates if item.to_pydatetime().replace(tzinfo=None) >= cutoff)


def build_performance_summary(orders_df, positions_df, account, local_history=None):
    local_history = local_history if local_history is not None else pd.DataFrame()
    statuses = orders_df["status"].astype(str).str.lower() if "status" in orders_df.columns else pd.Series(dtype=str)
    filled_orders = int((statuses == "filled").sum())
    canceled_orders = int(statuses.isin(["canceled", "cancelled"]).sum())
    rejected_orders = int((statuses == "rejected").sum())
    total_orders = int(len(orders_df) + len(local_history))

    trades_df = build_performance_trades(orders_df, positions_df)
    pl = pd.to_numeric(trades_df.get("unrealized_pl", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    plpc = pd.to_numeric(trades_df.get("unrealized_plpc", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    position_pl = pd.to_numeric(
        positions_df.get("unrealized_pl", pd.Series(dtype=float)),
        errors="coerce",
    ).fillna(0.0)
    gains = plpc[pl > 0]
    losses = plpc[pl < 0]

    best_symbol = ""
    worst_symbol = ""
    if not trades_df.empty and not pl.empty:
        best_symbol = str(trades_df.iloc[int(pl.idxmax())]["symbol"]) if pl.max() > 0 else ""
        worst_symbol = str(trades_df.iloc[int(pl.idxmin())]["symbol"]) if pl.min() < 0 else ""

    summary = {
        "total_orders": total_orders,
        "filled_orders": filled_orders,
        "canceled_orders": canceled_orders,
        "rejected_orders": rejected_orders,
        "open_positions": int(len(positions_df)),
        "win_rate": calculate_win_rate(trades_df),
        "avg_profit_pct": round(float(gains.mean()), 2) if not gains.empty else 0.0,
        "avg_loss_pct": round(float(losses.mean()), 2) if not losses.empty else 0.0,
        "profit_factor": calculate_profit_factor(trades_df),
        "total_unrealized_pl": round(float(position_pl.sum()), 2),
        "daily_return_pct": calculate_daily_return_pct(account or {}),
        "best_symbol": best_symbol,
        "worst_symbol": worst_symbol,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "recent_7d_orders": count_recent_orders(orders_df, local_history, 7),
        "recent_30d_orders": count_recent_orders(orders_df, local_history, 30),
    }
    return summary, trades_df


def write_performance_files(summary, trades_df):
    summary_row = {column: summary.get(column, "") for column in SUMMARY_COLUMNS}
    pd.DataFrame([summary_row], columns=SUMMARY_COLUMNS).to_csv(PERFORMANCE_SUMMARY_FILE, index=False)
    trades_df.reindex(columns=TRADE_COLUMNS).to_csv(PERFORMANCE_TRADES_FILE, index=False)


def generate_performance_report(broker=None, write_files=True):
    snapshot = fetch_alpaca_snapshot(broker=broker)
    orders_df = normalize_orders(snapshot["orders"])
    positions_df = normalize_positions(snapshot["positions"])
    local_history = read_csv(ORDER_HISTORY_FILE, columns=["symbol", "order_date"])
    summary, trades_df = build_performance_summary(
        orders_df,
        positions_df,
        snapshot["account"],
        local_history=local_history,
    )
    summary["api_error"] = snapshot.get("error", "")
    if write_files:
        write_performance_files(summary, trades_df)
    return summary, trades_df


def main():
    summary, trades_df = generate_performance_report()
    print("Performance Summary")
    for key in SUMMARY_COLUMNS:
        print(f"- {key}: {summary.get(key, '')}")
    print(f"Trades: {len(trades_df)}")
    if summary.get("api_error"):
        print(f"API fallback: {summary['api_error']}")


if __name__ == "__main__":
    main()
