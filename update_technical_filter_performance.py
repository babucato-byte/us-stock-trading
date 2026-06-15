from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf
from pandas.errors import EmptyDataError


BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "technical_filter_log.csv"
PERFORMANCE_COLUMNS = [
    "price_after",
    "return_pct",
    "max_return_pct",
    "min_return_pct",
    "checked_at",
    "holding_minutes",
    "error",
]


def calculate_return_pct(price_after, current_price):
    current = float(current_price)
    after = float(price_after)
    if current == 0:
        raise ValueError("current_price is zero")
    return ((after - current) / current) * 100


def parse_timestamp(value):
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError("invalid timestamp")
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.tz_convert(None)
    return parsed.to_pydatetime()


def normalize_history_index(history):
    if history is None or history.empty:
        return pd.DataFrame()
    df = history.copy()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert(None)
    return df


def fetch_intraday_history(symbol):
    ticker = yf.Ticker(symbol)
    for interval, period in [("1m", "7d"), ("5m", "30d")]:
        history = normalize_history_index(ticker.history(period=period, interval=interval))
        if not history.empty:
            return history
    return pd.DataFrame()


def calculate_symbol_performance(symbol, timestamp, current_price, history=None, checked_at=None):
    checked_at = checked_at or datetime.now()
    started_at = parse_timestamp(timestamp)
    history = normalize_history_index(history) if history is not None else fetch_intraday_history(symbol)
    if history.empty:
        raise RuntimeError("no price history returned")
    if "Close" not in history.columns:
        raise RuntimeError("price history has no Close column")

    after_start = history[history.index >= started_at]
    if after_start.empty:
        after_start = history

    close = pd.to_numeric(after_start["Close"], errors="coerce").dropna()
    high = pd.to_numeric(after_start.get("High", close), errors="coerce").dropna()
    low = pd.to_numeric(after_start.get("Low", close), errors="coerce").dropna()
    if close.empty:
        raise RuntimeError("price history has no usable Close values")

    price_after = float(close.iloc[-1])
    max_price = float(high.max()) if not high.empty else price_after
    min_price = float(low.min()) if not low.empty else price_after
    return {
        "price_after": round(price_after, 4),
        "return_pct": round(calculate_return_pct(price_after, current_price), 4),
        "max_return_pct": round(calculate_return_pct(max_price, current_price), 4),
        "min_return_pct": round(calculate_return_pct(min_price, current_price), 4),
        "checked_at": checked_at.strftime("%Y-%m-%d %H:%M:%S"),
        "holding_minutes": max(0, int((checked_at - started_at).total_seconds() // 60)),
        "error": "",
    }


def ensure_performance_columns(df):
    for column in PERFORMANCE_COLUMNS:
        if column not in df.columns:
            df[column] = ""
        df[column] = df[column].astype("object")
    return df


def update_performance(df):
    if df.empty:
        return ensure_performance_columns(df.copy())

    updated = ensure_performance_columns(df.copy())
    for index, row in updated.iterrows():
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            updated.at[index, "error"] = "missing symbol"
            continue
        try:
            performance = calculate_symbol_performance(
                symbol=symbol,
                timestamp=row.get("timestamp"),
                current_price=row.get("current_price"),
            )
            for column, value in performance.items():
                updated.at[index, column] = value
        except Exception as exc:
            updated.at[index, "error"] = str(exc)
    return updated


def main(path=LOG_FILE):
    if not path.exists():
        empty = pd.DataFrame(columns=PERFORMANCE_COLUMNS)
        empty.to_csv(path, index=False)
        print(f"{path.name} was missing; created empty performance log.")
        return empty

    try:
        df = pd.read_csv(path)
    except EmptyDataError:
        df = pd.DataFrame()
    updated = update_performance(df)
    updated.to_csv(path, index=False)
    success_count = int(updated["error"].fillna("").eq("").sum()) if "error" in updated.columns else 0
    print(f"Updated {path.name}: rows={len(updated)} success={success_count} errors={len(updated) - success_count}")
    return updated


if __name__ == "__main__":
    main()
