import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from market_hours import get_us_market_session
from slack_utils import send_slack_alert


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
RULES_FILE = CONFIG_DIR / "scanner_rules.json"
PRESETS_FILE = CONFIG_DIR / "scanner_presets.json"

CANDIDATES_FILE = BASE_DIR / "candidates.csv"
STRONG_CANDIDATES_FILE = BASE_DIR / "strong_candidates.csv"
ORDER_CANDIDATES_FILE = BASE_DIR / "order_candidates.csv"
PREVIOUS_CANDIDATES_FILE = BASE_DIR / "previous_candidates.csv"


DEFAULT_RULES = {
    "active_preset": "paper_safe",
    "scan_limit": 800,
    "min_price": 5,
    "min_avg_dollar_volume": 20_000_000,
    "rsi_min": 40,
    "rsi_max": 65,
    "volume_ratio_min": 1.2,
    "min_score": 70,
    "smart_money_min": 50,
    "top_alert_count": 5,
    "ma200_required": True,
    "avg_volume_window": 20,
    "rsi_period": 14,
}


def ensure_config_files():
    CONFIG_DIR.mkdir(exist_ok=True)
    if not RULES_FILE.exists():
        RULES_FILE.write_text(json.dumps(DEFAULT_RULES, indent=2) + "\n", encoding="utf-8")


def load_json(path, fallback):
    if not path.exists():
        return fallback.copy()
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    merged = fallback.copy()
    merged.update(data)
    return merged


def load_scanner_rules(preset_name=None):
    ensure_config_files()
    rules = load_json(RULES_FILE, DEFAULT_RULES)
    presets = load_json(PRESETS_FILE, {})
    selected = preset_name or rules.get("active_preset")
    if selected and selected in presets:
        preset_rules = presets[selected].copy()
        preset_rules.pop("description", None)
        rules.update(preset_rules)
        rules["active_preset"] = selected
    return rules


def calculate_rsi(df, period):
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


@dataclass
class CandidateBuckets:
    candidates: pd.DataFrame
    strong_candidates: pd.DataFrame
    order_candidates: pd.DataFrame


def analyze(symbol, rules):
    try:
        df = yf.Ticker(symbol).history(period="1y")
        avg_window = int(rules["avg_volume_window"])
        rsi_period = int(rules["rsi_period"])
        min_history = max(220, avg_window + rsi_period + 5)

        if df.empty or len(df) < min_history:
            return None

        df["MA200"] = df["Close"].rolling(window=200).mean()
        df["RSI"] = calculate_rsi(df, rsi_period)
        df["AVG_VOLUME"] = df["Volume"].rolling(window=avg_window).mean()

        price = float(df["Close"].iloc[-1])
        ma200 = float(df["MA200"].iloc[-1])
        rsi = float(df["RSI"].iloc[-1])
        volume = float(df["Volume"].iloc[-1])
        avg_volume = float(df["AVG_VOLUME"].iloc[-1])
        if avg_volume <= 0:
            return None

        volume_ratio = volume / avg_volume
        avg_dollar_volume = price * avg_volume

        if price < float(rules["min_price"]):
            return None
        if avg_dollar_volume < float(rules["min_avg_dollar_volume"]):
            return None
        if rules.get("ma200_required", True) and price <= ma200:
            return None

        score = 0
        if price > ma200:
            score += 40
        if float(rules["rsi_min"]) <= rsi <= float(rules["rsi_max"]):
            score += 30
        if volume_ratio >= float(rules["volume_ratio_min"]):
            score += 30

        smart_money_score = 0
        if volume_ratio >= 2:
            smart_money_score += 30
        if volume_ratio >= 3:
            smart_money_score += 20
        if price > ma200:
            smart_money_score += 20
        if float(rules["rsi_min"]) <= rsi <= float(rules["rsi_max"]):
            smart_money_score += 20

        if score < int(rules["min_score"]):
            return None

        stock_type = "smart_money" if smart_money_score >= int(rules["smart_money_min"]) else "technical"
        return {
            "symbol": symbol,
            "price": round(price, 2),
            "ma200": round(ma200, 2),
            "rsi": round(rsi, 2),
            "volume_ratio": round(volume_ratio, 2),
            "avg_dollar_volume": round(avg_dollar_volume, 0),
            "score": int(score),
            "smart_money_score": int(smart_money_score),
            "type": stock_type,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        print(f"{symbol} scan failed: {exc}")
        return None


def empty_candidates_frame():
    return pd.DataFrame(
        columns=[
            "symbol",
            "price",
            "ma200",
            "rsi",
            "volume_ratio",
            "avg_dollar_volume",
            "score",
            "smart_money_score",
            "type",
            "scan_time",
        ]
    )


def build_candidate_buckets(df, rules):
    if df.empty:
        empty = empty_candidates_frame()
        return CandidateBuckets(empty, empty.copy(), empty.copy())

    df = df.sort_values(
        by=["score", "smart_money_score", "volume_ratio"],
        ascending=False,
    ).reset_index(drop=True)
    strong = df[df["smart_money_score"] >= int(rules["smart_money_min"])].copy()
    order_candidates = df[
        (df["score"] >= int(rules["min_score"]))
        & (df["smart_money_score"] >= int(rules["smart_money_min"]))
        & (df["volume_ratio"] >= float(rules["volume_ratio_min"]))
    ].copy()
    return CandidateBuckets(df, strong.reset_index(drop=True), order_candidates.reset_index(drop=True))


def load_previous_symbols():
    if not PREVIOUS_CANDIDATES_FILE.exists():
        return set()
    try:
        previous = pd.read_csv(PREVIOUS_CANDIDATES_FILE)
        return set(previous.get("symbol", pd.Series(dtype=str)).dropna().astype(str))
    except Exception:
        return set()


def save_candidate_files(buckets):
    buckets.candidates.to_csv(CANDIDATES_FILE, index=False)
    buckets.strong_candidates.to_csv(STRONG_CANDIDATES_FILE, index=False)
    buckets.order_candidates.to_csv(ORDER_CANDIDATES_FILE, index=False)
    buckets.candidates[["symbol"]].to_csv(PREVIOUS_CANDIDATES_FILE, index=False)


def format_scanner_alert(buckets, rules, previous_symbols):
    df = buckets.candidates
    current_symbols = set(df["symbol"].astype(str)) if not df.empty else set()
    new_symbols = sorted(current_symbols - previous_symbols)
    repeat_symbols = sorted(current_symbols & previous_symbols)
    rising = []

    if not df.empty:
        rising = df.sort_values("smart_money_score", ascending=False).head(5)["symbol"].tolist()

    top = df.head(int(rules["top_alert_count"])) if not df.empty else df
    market_session = get_us_market_session()
    order_available = market_session == "regular" and not buckets.order_candidates.empty

    lines = [
        "*Realtime Candidate Scan*",
        f"- Total candidates: {len(buckets.candidates)}",
        f"- Strong smart-money candidates: {len(buckets.strong_candidates)}",
        f"- Volume >= 2x candidates: {int((df['volume_ratio'] >= 2).sum()) if not df.empty else 0}",
        f"- Regular-session order review: {'YES' if order_available else 'NO'} ({market_session})",
        "",
        "*Top candidates*",
    ]

    if top.empty:
        lines.append("- None")
    else:
        for _, row in top.iterrows():
            lines.append(
                f"- {row['symbol']} | score {row['score']} | smart {row['smart_money_score']} | "
                f"RSI {row['rsi']} | vol {row['volume_ratio']}x"
            )

    lines.extend(
        [
            "",
            f"*New*: {', '.join(new_symbols[:10]) if new_symbols else 'None'}",
            f"*Repeated*: {', '.join(repeat_symbols[:10]) if repeat_symbols else 'None'}",
            f"*Smart-money leaders*: {', '.join(rising) if rising else 'None'}",
        ]
    )
    return "\n".join(lines)


def scan(preset_name=None, send_slack=True):
    rules = load_scanner_rules(preset_name)
    universe = pd.read_csv(BASE_DIR / "universe.csv")
    symbols = universe["symbol"].dropna().astype(str).unique().tolist()
    scan_limit = min(len(symbols), int(rules["scan_limit"]))

    print(f"Scanning {scan_limit} of {len(symbols)} symbols with preset {rules.get('active_preset')}")
    results = []
    for idx, symbol in enumerate(symbols[:scan_limit], start=1):
        print(f"[{idx}/{scan_limit}] {symbol}")
        result = analyze(symbol, rules)
        if result:
            results.append(result)

    previous_symbols = load_previous_symbols()
    buckets = build_candidate_buckets(pd.DataFrame(results), rules)
    save_candidate_files(buckets)

    message = format_scanner_alert(buckets, rules, previous_symbols)
    print(message)
    if send_slack:
        send_slack_alert(message)
    return buckets


if __name__ == "__main__":
    scan()
