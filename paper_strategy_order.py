from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from account_risk import check_daily_loss_limit
from broker import AlpacaBroker
from market_hours import get_us_market_session
from order_safety import run_order_safety_check
from slack_utils import send_slack_alert


BASE_DIR = Path(__file__).resolve().parent
ORDER_CANDIDATES_FILE = BASE_DIR / "order_candidates.csv"
CANDIDATES_FILE = BASE_DIR / "candidates.csv"
ORDER_HISTORY_FILE = BASE_DIR / "order_history.csv"


def calculate_rsi(df, period=14):
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def submit_order(symbol, qty=1, broker=None):
    broker = broker or AlpacaBroker()
    response = broker.submit_order(symbol, qty=qty)
    print(f"{symbol} order result: {response.status_code} {response.text[:500]}")
    return response


def analyze_stock(symbol):
    df = yf.Ticker(symbol).history(period="1y")
    if df.empty or len(df) < 220:
        return None

    df["MA200"] = df["Close"].rolling(window=200).mean()
    df["RSI"] = calculate_rsi(df)
    df["AVG_VOLUME_20"] = df["Volume"].rolling(window=20).mean()

    price = float(df["Close"].iloc[-1])
    ma200 = float(df["MA200"].iloc[-1])
    rsi = float(df["RSI"].iloc[-1])
    avg_volume = float(df["AVG_VOLUME_20"].iloc[-1])
    if avg_volume <= 0:
        return None
    volume_ratio = float(df["Volume"].iloc[-1]) / avg_volume

    score = 0
    if price > ma200:
        score += 40
    if 40 <= rsi <= 65:
        score += 30
    if volume_ratio >= 1.2:
        score += 30

    return {
        "symbol": symbol,
        "price": price,
        "ma200": ma200,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "score": score,
    }


def _load_symbols_from_csv(path):
    try:
        df = pd.read_csv(path)
        if df.empty or "symbol" not in df.columns:
            return []
        return df["symbol"].dropna().astype(str).unique().tolist()
    except Exception as exc:
        print(f"Failed to read {path.name}: {exc}")
        return []


def load_watchlist():
    order_symbols = _load_symbols_from_csv(ORDER_CANDIDATES_FILE)
    if order_symbols:
        print(f"Loaded {len(order_symbols)} symbols from order_candidates.csv")
        return order_symbols
    fallback = _load_symbols_from_csv(CANDIDATES_FILE)
    print(f"Loaded {len(fallback)} symbols from candidates.csv fallback")
    return fallback


def load_order_history():
    try:
        return pd.read_csv(ORDER_HISTORY_FILE)
    except Exception:
        return pd.DataFrame(columns=["symbol", "order_date", "mode", "dry_run"])


def save_order_history(order_history):
    try:
        order_history.to_csv(ORDER_HISTORY_FILE, index=False)
        return True
    except Exception as exc:
        print(f"Failed to save order history to {ORDER_HISTORY_FILE}: {exc}")
        return False


def is_duplicate_order(order_history, symbol, order_date):
    if order_history.empty or "symbol" not in order_history.columns or "order_date" not in order_history.columns:
        return False
    return (
        (order_history["symbol"].astype(str) == symbol)
        & (order_history["order_date"].astype(str) == order_date)
    ).any()


def _notify_order_blocked(symbol, reason):
    send_slack_alert(f"*Order blocked*\n- Symbol: {symbol}\n- Reason: {reason}")


def _safe_send_slack_alert(message):
    try:
        return send_slack_alert(message)
    except Exception as exc:
        print(f"Slack notification failed: {exc}")
        return False


def main(broker=None):
    broker = broker or AlpacaBroker()
    market_session = get_us_market_session()
    allow_order = market_session == "regular"

    print(f"Broker mode: {broker.config.status_label}")
    print(f"Market session: {market_session}")
    if not allow_order:
        print("Orders are only reviewed during regular market hours.")

    tickers = load_watchlist()
    if not tickers:
        print("No order review candidates.")
        return

    account = broker.get_account()
    positions = broker.get_positions()
    check_daily_loss_limit(account)

    open_position_count = len(positions)
    today_trade_count = 0
    today = datetime.now().strftime("%Y-%m-%d")
    held_symbols = [p["symbol"] for p in positions]
    order_history = load_order_history()

    print(f"Open positions: {open_position_count}")
    print(f"Held symbols: {held_symbols}")

    for symbol in tickers:
        if symbol in held_symbols:
            _notify_order_blocked(symbol, "Already held")
            continue

        if is_duplicate_order(order_history, symbol, today):
            _notify_order_blocked(symbol, "Duplicate order prevented for today")
            continue

        result = analyze_stock(symbol)
        if not result:
            print(f"{symbol} analysis failed")
            continue

        if result["score"] < 70:
            print(f"{symbol} did not meet order score threshold: {result['score']}")
            continue

        if not allow_order:
            send_slack_alert(
                f"*Order review only*\n- Symbol: {symbol}\n- Score: {result['score']}\n"
                f"- Market session: {market_session}\n- Status: no order submitted"
            )
            continue

        run_order_safety_check(
            position_rate=0.01,
            today_trade_count=today_trade_count,
            open_position_count=open_position_count,
        )

        try:
            response = submit_order(symbol, qty=1, broker=broker)
        except requests.exceptions.RequestException as exc:
            print(f"{symbol} order submission failed: {exc}")
            _safe_send_slack_alert(
                f"*Order failed*\n- Symbol: {symbol}\n- Reason: {exc}"
            )
            continue

        success = response.status_code in [200, 201]
        status = "DRY RUN" if response.dry_run else ("SUBMITTED" if success else "FAILED")

        if success:
            new_row = pd.DataFrame(
                [
                    {
                        "symbol": symbol,
                        "order_date": today,
                        "mode": broker.config.status_label,
                        "dry_run": response.dry_run,
                    }
                ]
            )
            order_history = pd.concat([order_history, new_row], ignore_index=True)
            save_order_history(order_history)
            today_trade_count += 1
            if not response.dry_run:
                open_position_count += 1

        _safe_send_slack_alert(
            f"*Paper Strategy Order*\n- Symbol: {symbol}\n- Qty: 1\n"
            f"- Score: {result['score']}\n- Price: {result['price']:.2f}\n"
            f"- RSI: {result['rsi']:.2f}\n- Volume ratio: {result['volume_ratio']:.2f}x\n"
            f"- Broker mode: {broker.config.status_label}\n- Status: {status}"
        )


if __name__ == "__main__":
    main()
