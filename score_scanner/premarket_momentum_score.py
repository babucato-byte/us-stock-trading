import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys

import pandas as pd
import yfinance as yf
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
from market_guard import is_us_trading_day


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

LOG_DIR = BASE_DIR / "logs"
CANDIDATES_FILE = LOG_DIR / "score_scanner_candidates.csv"
PAPER_TRADES_FILE = LOG_DIR / "paper_trades_score_scanner.csv"
UNIVERSE_FILE = BASE_DIR / "universe.csv"

OUTPUT_COLUMNS = [
    "timestamp",
    "symbol",
    "price",
    "vwap",
    "ema9",
    "ema21",
    "volume",
    "avg_volume",
    "volume_multiple",
    "premarket_gain_pct",
    "prev_high",
    "adx",
    "week52_high",
    "break_prev_high",
    "adx_pass",
    "near_or_break_52w_high",
    "score",
    "reason",
]

PAPER_TRADE_COLUMNS = [
    "timestamp",
    "symbol",
    "entry_price",
    "score",
    "status",
    "notes",
]


@dataclass(frozen=True)
class ScoreScannerConfig:
    min_score: int = 60
    min_premarket_gain_pct: float = 7.0
    min_volume_multiple: float = 2.0
    adx_threshold: float = 25.0
    near_52w_ratio: float = 0.98
    avg_volume_window: int = 20


def ema(series, span):
    return pd.to_numeric(series, errors="coerce").ewm(span=span, adjust=False).mean()


def calculate_vwap(df):
    high = pd.to_numeric(df["High"], errors="coerce")
    low = pd.to_numeric(df["Low"], errors="coerce")
    close = pd.to_numeric(df["Close"], errors="coerce")
    volume = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)
    typical_price = (high + low + close) / 3
    cumulative_volume = volume.cumsum()
    return (typical_price * volume).cumsum() / cumulative_volume.replace(0, pd.NA)


def calculate_adx(daily_df, period=14):
    if daily_df is None or len(daily_df) < period + 2:
        return None
    high = pd.to_numeric(daily_df["High"], errors="coerce")
    low = pd.to_numeric(daily_df["Low"], errors="coerce")
    close = pd.to_numeric(daily_df["Close"], errors="coerce")

    plus_dm = (high.diff()).where((high.diff() > -low.diff()) & (high.diff() > 0), 0.0)
    minus_dm = (-low.diff()).where((-low.diff() > high.diff()) & (-low.diff() > 0), 0.0)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.rolling(period).mean()
    plus_di = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)) * 100
    adx = dx.rolling(period).mean().dropna()
    return float(adx.iloc[-1]) if not adx.empty else None


def latest_previous_high(daily_df):
    high = pd.to_numeric(daily_df.get("High", pd.Series(dtype=float)), errors="coerce").dropna()
    if len(high) < 2:
        return None
    return float(high.iloc[-2])


def week52_high(daily_df):
    high = pd.to_numeric(daily_df.get("High", pd.Series(dtype=float)), errors="coerce").dropna()
    if high.empty:
        return None
    return float(high.tail(252).max())


def previous_close(daily_df):
    close = pd.to_numeric(daily_df.get("Close", pd.Series(dtype=float)), errors="coerce").dropna()
    if len(close) < 2:
        return None
    return float(close.iloc[-2])


def fetch_symbol_data(symbol):
    ticker = yf.Ticker(symbol)
    intraday = ticker.history(period="5d", interval="1m", prepost=True)
    if intraday.empty:
        intraday = ticker.history(period="30d", interval="5m", prepost=True)
    daily = ticker.history(period="1y", interval="1d")
    return intraday, daily


def build_reason(required_checks, bonus_checks, score):
    required = ", ".join(name for name, passed in required_checks.items() if passed)
    bonus = ", ".join(name for name, passed in bonus_checks.items() if passed) or "none"
    return f"required={required}; bonus={bonus}; score={score}"


def evaluate_symbol(symbol, intraday_df, daily_df, config=None, timestamp=None):
    config = config or ScoreScannerConfig()
    timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if intraday_df is None or intraday_df.empty or daily_df is None or daily_df.empty:
        return None

    intraday = intraday_df.copy()
    intraday["VWAP"] = calculate_vwap(intraday)
    intraday["EMA9"] = ema(intraday["Close"], 9)
    intraday["EMA21"] = ema(intraday["Close"], 21)
    intraday["AVG_VOLUME"] = pd.to_numeric(intraday["Volume"], errors="coerce").rolling(
        config.avg_volume_window
    ).mean()

    latest = intraday.dropna(subset=["Close", "VWAP", "EMA9", "EMA21", "Volume", "AVG_VOLUME"]).tail(1)
    if latest.empty:
        return None
    row = latest.iloc[0]

    price = float(row["Close"])
    vwap = float(row["VWAP"])
    ema9_value = float(row["EMA9"])
    ema21_value = float(row["EMA21"])
    volume = float(row["Volume"])
    avg_volume = float(row["AVG_VOLUME"])
    if avg_volume <= 0:
        return None

    prev_close = previous_close(daily_df)
    prev_high = latest_previous_high(daily_df)
    high52 = week52_high(daily_df)
    adx = calculate_adx(daily_df)
    if prev_close is None or prev_high is None or high52 is None:
        return None

    volume_multiple = volume / avg_volume
    premarket_gain_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
    required_checks = {
        "price_above_vwap": price > vwap,
        "ema9_above_ema21": ema9_value > ema21_value,
        "volume_3x": volume_multiple > config.min_volume_multiple,
        "premarket_gain_10pct": premarket_gain_pct >= config.min_premarket_gain_pct,
    }
    if not all(required_checks.values()):
        return None

    break_prev_high = price > prev_high
    adx_pass = adx is not None and adx > config.adx_threshold
    near_or_break_52w_high = price >= high52 * config.near_52w_ratio
    bonus_checks = {
        "break_prev_high": break_prev_high,
        "adx_pass": adx_pass,
        "near_or_break_52w_high": near_or_break_52w_high,
    }

    score = 50
    if break_prev_high:
        score += 20
    if adx_pass:
        score += 10
    if near_or_break_52w_high:
        score += 10
    if score < config.min_score:
        return None

    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "price": round(price, 4),
        "vwap": round(vwap, 4),
        "ema9": round(ema9_value, 4),
        "ema21": round(ema21_value, 4),
        "volume": round(volume, 0),
        "avg_volume": round(avg_volume, 2),
        "volume_multiple": round(volume_multiple, 4),
        "premarket_gain_pct": round(premarket_gain_pct, 4),
        "prev_high": round(prev_high, 4),
        "adx": round(adx, 4) if adx is not None else None,
        "week52_high": round(high52, 4),
        "break_prev_high": bool(break_prev_high),
        "adx_pass": bool(adx_pass),
        "near_or_break_52w_high": bool(near_or_break_52w_high),
        "score": int(score),
        "reason": build_reason(required_checks, bonus_checks, score),
    }


def ensure_output_files():
    LOG_DIR.mkdir(exist_ok=True)
    if not PAPER_TRADES_FILE.exists():
        pd.DataFrame(columns=PAPER_TRADE_COLUMNS).to_csv(PAPER_TRADES_FILE, index=False)


def save_candidates(rows, path=None):
    ensure_output_files()
    path = path or CANDIDATES_FILE
    path.parent.mkdir(exist_ok=True)
    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    df.to_csv(path, index=False)
    return df


def load_symbols(limit=None):
    if not UNIVERSE_FILE.exists():
        return []
    df = pd.read_csv(UNIVERSE_FILE)
    symbols = df["symbol"].dropna().astype(str).drop_duplicates().tolist()
    return symbols[:limit] if limit else symbols


def sample_data():
    closes = [10 + i * 0.25 for i in range(24)] + [19.5]
    volumes = [1000] * 24 + [6000]
    intraday = pd.DataFrame(
        {
            "Open": [value - 0.1 for value in closes],
            "High": [value + 0.3 for value in closes],
            "Low": [value - 0.3 for value in closes],
            "Close": closes,
            "Volume": volumes,
        }
    )
    daily_close = [10 + i * 0.03 for i in range(258)] + [14.0, 14.5]
    daily = pd.DataFrame(
        {
            "High": [min(value + 0.5, 19.0) for value in daily_close[:-2]] + [18.0, 19.0],
            "Low": [value - 0.5 for value in daily_close],
            "Close": daily_close,
        }
    )
    return intraday, daily


def run_scan(symbols=None, limit=None, dry_run=True, sample=False):
    ensure_output_files()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    config = ScoreScannerConfig()
    rows = []

    if sample:
        intraday, daily = sample_data()
        candidate = evaluate_symbol("SAMPLE", intraday, daily, config=config, timestamp=timestamp)
        if candidate:
            rows.append(candidate)
    else:
        for symbol in symbols or load_symbols(limit):
            try:
                intraday, daily = fetch_symbol_data(symbol)
                candidate = evaluate_symbol(symbol, intraday, daily, config=config, timestamp=timestamp)
                if candidate:
                    rows.append(candidate)
            except Exception as exc:
                print(f"[SCORE SCANNER] {symbol} skipped: {exc}")

    df = save_candidates(rows)
    mode = "dry-run" if dry_run else "record-only"
    print(f"[SCORE SCANNER] mode={mode} candidates={len(df)} output={CANDIDATES_FILE}")
    print(f"[SCORE SCANNER] paper trade log prepared={PAPER_TRADES_FILE}")
    return df


def parse_args():
    parser = argparse.ArgumentParser(description="Premarket momentum score scanner. Records candidates only.")
    parser.add_argument("--limit", type=int, default=30, help="Maximum symbols to scan.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to scan.")
    parser.add_argument("--sample", action="store_true", help="Use built-in sample data for a deterministic dry run.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Record candidates only; no orders.")
    return parser.parse_args()


def main():
    args = parse_args()
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()] or None
    run_scan(symbols=symbols, limit=args.limit, dry_run=args.dry_run, sample=args.sample)


if __name__ == "__main__":
    if not is_us_trading_day():
        print("[MARKET GUARD] NYSE closed. Score scanner skipped.")
        raise SystemExit(0)

    main()
