import fcntl
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from account_risk import check_daily_loss_limit
from broker import AlpacaBroker
from market_hours import eastern_now, get_us_market_session
from order_safety import check_daily_trade_count, run_order_safety_check
from slack_utils import send_slack_alert


BASE_DIR = Path(__file__).resolve().parent
ORDER_CANDIDATES_FILE = BASE_DIR / "order_candidates.csv"
CANDIDATES_FILE = BASE_DIR / "candidates.csv"
ORDER_HISTORY_FILE = BASE_DIR / "order_history.csv"
ORDER_HISTORY_LOCK_FILE = BASE_DIR / "order_history.lock"
ORDER_HISTORY_LOCK_TIMEOUT_SECONDS = 5.0
REQUIRED_HISTORY_COLUMNS = ["symbol", "order_date", "mode", "dry_run", "status"]


class OrderHistoryUnavailable(Exception):
    """Order history could not be safely read; new orders are blocked (fail-closed)."""


class DuplicateOrderError(Exception):
    """A reservation for this symbol/date already exists."""


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
    """Read order_history.csv, fail-closed on anything but a valid file.

    A MISSING file (never initialized) or a CORRUPTED file (parse failure,
    missing required columns, or an unparseable order_date column) both
    raise OrderHistoryUnavailable instead of silently returning an empty
    DataFrame. A run that cannot prove today's order count is zero must not
    treat it as zero. Only a file that was explicitly created via
    initialize_order_history() (or already has valid rows) counts as a
    legitimate empty history.
    """
    if not ORDER_HISTORY_FILE.exists():
        raise OrderHistoryUnavailable(
            f"MISSING_HISTORY: {ORDER_HISTORY_FILE} does not exist. "
            "Run initialize_order_history() once during initial setup if this is a fresh deployment."
        )
    try:
        df = pd.read_csv(ORDER_HISTORY_FILE)
    except Exception as exc:
        raise OrderHistoryUnavailable(
            f"CORRUPTED_HISTORY: failed to parse {ORDER_HISTORY_FILE}: {exc}"
        )
    missing_columns = [c for c in REQUIRED_HISTORY_COLUMNS if c not in df.columns]
    if missing_columns:
        raise OrderHistoryUnavailable(
            f"CORRUPTED_HISTORY: {ORDER_HISTORY_FILE} is missing required columns {missing_columns}"
        )
    if not df.empty:
        try:
            pd.to_datetime(df["order_date"], errors="raise")
        except Exception as exc:
            raise OrderHistoryUnavailable(
                f"CORRUPTED_HISTORY: order_date column in {ORDER_HISTORY_FILE} failed to parse: {exc}"
            )
    return df


def initialize_order_history():
    """Explicit one-time setup: create a valid, empty order history file.

    Only this function (or an already-valid file) may produce the
    EMPTY_VALID_HISTORY state load_order_history() accepts. A file that
    goes missing after this point is treated as an operational anomaly,
    not a fresh install.
    """
    df = pd.DataFrame(columns=REQUIRED_HISTORY_COLUMNS)
    if not save_order_history(df):
        raise OrderHistoryUnavailable(f"Failed to initialize {ORDER_HISTORY_FILE}")
    return df


def save_order_history(order_history):
    """Write order_history.csv atomically: temp file + fsync + os.replace().

    A crash or exception mid-write leaves the previous file untouched
    (os.replace is atomic on the same filesystem on macOS/Ubuntu), instead
    of a partially-written CSV from an in-place to_csv().
    """
    try:
        ORDER_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(ORDER_HISTORY_FILE.parent), prefix=".order_history_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", newline="") as tmp_file:
                order_history.to_csv(tmp_file, index=False)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_name, ORDER_HISTORY_FILE)
        except Exception:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise
        return True
    except Exception as exc:
        print(f"Failed to save order history to {ORDER_HISTORY_FILE}: {exc}")
        return False


@contextmanager
def _order_history_lock(timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    """Process-level exclusive lock guarding read-check-write of order history.

    Uses fcntl.flock (macOS/Ubuntu); Windows compatibility is out of scope
    for this project. Failure to acquire the lock within `timeout` blocks
    the order rather than proceeding without mutual exclusion.
    """
    ORDER_HISTORY_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(ORDER_HISTORY_LOCK_FILE, "a+")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Could not acquire order history lock "
                        f"({ORDER_HISTORY_LOCK_FILE}) within {timeout}s; order blocked"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


def is_duplicate_order(order_history, symbol, order_date):
    if order_history.empty or "symbol" not in order_history.columns or "order_date" not in order_history.columns:
        return False
    return (
        (order_history["symbol"].astype(str) == symbol)
        & (order_history["order_date"].astype(str) == order_date)
    ).any()


def count_orders_for_date(order_history, order_date):
    """Count order attempts that consume the daily trade limit for order_date.

    Counting policy: every persisted row for the date counts, regardless of
    its status column (PENDING_SUBMISSION, SUBMITTED, DRY_RUN, REJECTED,
    SUBMISSION_FAILED all count). This is deliberately conservative — safety
    margin is prioritized over squeezing out the maximum allowed trade count
    — since the current schema has no broker order id to dedupe by identity.
    """
    if order_history.empty or "order_date" not in order_history.columns:
        return 0
    return int((order_history["order_date"].astype(str) == order_date).sum())


def try_reserve_order(symbol, order_date, mode, dry_run, lock_timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    """Atomically reserve an order slot: lock -> reread -> check -> write -> unlock.

    Re-reads order history from disk under an exclusive lock (not the
    caller's possibly-stale in-memory copy) so two concurrent runs can't
    both observe the same duplicate/daily-count state and both proceed.
    Raises DuplicateOrderError (soft block, caller may continue to the next
    symbol) or propagates OrderHistoryUnavailable / the daily-trade-count
    Exception / a reservation-save RuntimeError (hard block, caller should
    stop). Returns the updated history including the new PENDING_SUBMISSION
    row on success.
    """
    with _order_history_lock(timeout=lock_timeout):
        order_history = load_order_history()
        if is_duplicate_order(order_history, symbol, order_date):
            raise DuplicateOrderError(symbol)
        today_trade_count = count_orders_for_date(order_history, order_date)
        check_daily_trade_count(today_trade_count)
        new_row = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "order_date": order_date,
                    "mode": mode,
                    "dry_run": dry_run,
                    "status": "PENDING_SUBMISSION",
                }
            ]
        )
        reserved_history = pd.concat([order_history, new_row], ignore_index=True)
        if not save_order_history(reserved_history):
            raise RuntimeError("Order history reservation failed; order submission blocked")
        return reserved_history


def update_order_status(symbol, order_date, status, lock_timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    """Update a reserved order's status, rereading fresh under lock first.

    Never overwrites the file with a stale in-memory snapshot: another
    process may have reserved a different symbol in the meantime, and a
    blind overwrite would silently drop that row (lost update).
    """
    with _order_history_lock(timeout=lock_timeout):
        try:
            order_history = load_order_history()
        except OrderHistoryUnavailable as exc:
            print(f"Could not update order status for {symbol}: {exc}")
            return False
        mask = (
            (order_history["symbol"].astype(str) == symbol)
            & (order_history["order_date"].astype(str) == order_date)
        )
        order_history.loc[mask, "status"] = status
        return save_order_history(order_history)


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
    equity = float(account["equity"])

    open_position_count = len(positions)
    # Trading-day boundary is always America/New_York, never the host's
    # local time — a server running in Asia/Seoul would otherwise cross
    # into "tomorrow" (and reset the daily counters) hours before the US
    # market day actually ends.
    today = eastern_now().strftime("%Y-%m-%d")
    held_symbols = [p["symbol"] for p in positions]
    order_history = load_order_history()
    today_trade_count = count_orders_for_date(order_history, today)

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

        order_qty = 1
        position_value = order_qty * result["price"]
        position_rate = (position_value / equity) if equity > 0 else float("inf")

        run_order_safety_check(
            position_rate=position_rate,
            today_trade_count=today_trade_count,
            open_position_count=open_position_count,
        )

        try:
            order_history = try_reserve_order(
                symbol,
                today,
                broker.config.status_label,
                False,
            )
        except DuplicateOrderError:
            _notify_order_blocked(symbol, "Duplicate order prevented for today")
            continue
        # Any other exception here (OrderHistoryUnavailable, the daily trade
        # count check re-verified fresh under lock, or a reservation-save
        # RuntimeError) propagates and stops the run for the remaining
        # symbols too — same conservative behavior as run_order_safety_check
        # above, now re-checked against the authoritative on-disk state.

        today_trade_count = count_orders_for_date(order_history, today)

        try:
            response = submit_order(symbol, qty=order_qty, broker=broker)
        except requests.exceptions.RequestException as exc:
            update_order_status(symbol, today, "SUBMISSION_FAILED")
            print(f"{symbol} order submission failed: {exc}")
            _safe_send_slack_alert(
                f"*Order failed*\n- Symbol: {symbol}\n- Reason: {exc}"
            )
            continue

        success = response.status_code in [200, 201]
        status = "DRY RUN" if response.dry_run else ("SUBMITTED" if success else "FAILED")

        if success:
            update_order_status(
                symbol,
                today,
                "DRY_RUN" if response.dry_run else "SUBMITTED",
            )
            if not response.dry_run:
                open_position_count += 1
        else:
            update_order_status(symbol, today, "REJECTED")

        _safe_send_slack_alert(
            f"*Paper Strategy Order*\n- Symbol: {symbol}\n- Qty: {order_qty}\n"
            f"- Score: {result['score']}\n- Price: {result['price']:.2f}\n"
            f"- RSI: {result['rsi']:.2f}\n- Volume ratio: {result['volume_ratio']:.2f}x\n"
            f"- Broker mode: {broker.config.status_label}\n- Status: {status}"
        )


if __name__ == "__main__":
    main()
