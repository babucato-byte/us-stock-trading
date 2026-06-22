import json
import math
import os
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from breakout_engine import calculate_breakout
from indicators import TECHNICAL_CHECK_COLUMNS, technical_entry_filter
from market_hours import get_us_market_session
from momentum_engine import calculate_momentum_score
from slack_utils import send_slack_alert
from trend_engine import calculate_trend
from market_guard import is_us_trading_day

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
RULES_FILE = CONFIG_DIR / "scanner_rules.json"
PRESETS_FILE = CONFIG_DIR / "scanner_presets.json"

CANDIDATES_FILE = BASE_DIR / "candidates.csv"
STRONG_CANDIDATES_FILE = BASE_DIR / "strong_candidates.csv"
ORDER_CANDIDATES_FILE = BASE_DIR / "order_candidates.csv"
PREVIOUS_CANDIDATES_FILE = BASE_DIR / "previous_candidates.csv"
TECHNICAL_FILTER_LOG_FILE = BASE_DIR / "technical_filter_log.csv"

DEFAULT_FILTERS = [
    {"field": "price", "operator": ">=", "value": 5},
    {"field": "avg_dollar_volume", "operator": ">=", "value": 20_000_000},
    {"field": "rsi", "operator": "between", "min": 40, "max": 65},
    {"field": "volume_ratio", "operator": ">=", "value": 1.2},
    {"field": "score", "operator": ">=", "value": 70},
    {"field": "smart_money_score", "operator": ">=", "value": 50},
]

DEFAULT_RULES = {
    "active_preset": "paper_safe",
    "scan_limit": 800,
    "top_alert_count": 5,
    "avg_volume_window": 20,
    "rsi_period": 14,
    "filters": DEFAULT_FILTERS,
}

TECHNICAL_FILTER_COLUMNS = [
    "technical_filter_pass",
    "technical_filter_score",
    *TECHNICAL_CHECK_COLUMNS,
]

TECHNICAL_FILTER_LOG_COLUMNS = [
    "scan_id",
    "timestamp",
    "symbol",
    "current_price",
    "technical_filter_pass",
    "technical_filter_score",
    "price_above_hma200",
    "hma200_rising",
    "hma_macd_bullish",
    "macd_histogram_rising",
    "sqzmom_green",
    "volume_multiple",
    "rsi",
    "smart_money_score",
    "preset",
]

SUPPORTED_OPERATORS = {">=", "<=", ">", "<", "==", "!=", "between", "in", "not_in"}
SUPPORTED_FIELDS = {
    "symbol",
    "price",
    "ma200",
    "above_ma200",
    "rsi",
    "volume",
    "avg_volume",
    "volume_ratio",
    "avg_dollar_volume",
    "dollar_volume",
    "score",
    "technical_score",
    "smart_money_score",
    "trend",
    "trend_score",
    "momentum_score",
    "breakout_flag",
    "breakout_score",
    "final_score",
    "type",
    "atr",
    "gap_percent",
    "market_cap",
    "relative_strength",
    "vwap_position",
    "float_shares",
    "sector",
    "exchange",
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
    return normalize_rules(merged)


def load_scanner_rules(preset_name=None):
    ensure_config_files()
    rules = load_json(RULES_FILE, DEFAULT_RULES)
    presets = read_presets()
    selected = preset_name
    if selected and selected in presets:
        preset_rules = presets[selected].copy()
        preset_rules.pop("description", None)
        rules.update(normalize_rules(preset_rules))
        rules["active_preset"] = selected
    return normalize_rules(rules)


def read_presets():
    if not PRESETS_FILE.exists():
        return {}
    with PRESETS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {name: normalize_rules(preset) for name, preset in data.items()}


def normalize_rules(rules):
    normalized = rules.copy()
    if "filters" not in normalized:
        normalized["filters"] = legacy_rules_to_filters(normalized)
    normalized.setdefault("scan_limit", DEFAULT_RULES["scan_limit"])
    normalized.setdefault("top_alert_count", DEFAULT_RULES["top_alert_count"])
    normalized.setdefault("avg_volume_window", DEFAULT_RULES["avg_volume_window"])
    normalized.setdefault("rsi_period", DEFAULT_RULES["rsi_period"])
    normalized["filters"] = list(normalized.get("filters") or [])
    return normalized


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name, default=None):
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        warn_skip(f"Environment variable '{name}' must be an integer; ignored.")
        return default


def use_technical_entry_filter():
    return env_bool("USE_TECHNICAL_ENTRY_FILTER", True)


def resolve_scan_limit(rules, override_limit=None):
    base_limit = int(rules["scan_limit"])
    requested = override_limit if override_limit is not None else env_int("SCAN_LIMIT")
    if requested is None:
        return base_limit, False
    try:
        limit = int(requested)
    except (TypeError, ValueError):
        warn_skip(f"Scan limit '{requested}' must be an integer; using configured scan_limit.")
        return base_limit, False
    if limit <= 0:
        warn_skip(f"Scan limit '{requested}' must be positive; using configured scan_limit.")
        return base_limit, False
    return min(base_limit, limit), True


def technical_filter_result_fields(result):
    checks = result.get("checks", {}) if result else {}
    fields = {
        "technical_filter_pass": bool(result.get("pass", False)) if result else False,
        "technical_filter_score": int(result.get("score", 0)) if result else 0,
    }
    for column in TECHNICAL_CHECK_COLUMNS:
        fields[column] = bool(checks.get(column, False))
    return fields


def create_scan_id(now=None):
    now = now or datetime.now()
    return now.strftime("%Y%m%d_%H%M%S")


def technical_filter_log_row(metrics, result, scan_id, rules):
    fields = technical_filter_result_fields(result)
    return {
        "scan_id": scan_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": metrics.get("symbol"),
        "current_price": round(metrics["price"], 4) if metrics.get("price") is not None else None,
        "technical_filter_pass": fields["technical_filter_pass"],
        "technical_filter_score": fields["technical_filter_score"],
        "price_above_hma200": fields["price_above_hma200"],
        "hma200_rising": fields["hma200_rising"],
        "hma_macd_bullish": fields["hma_macd_bullish"],
        "macd_histogram_rising": fields["macd_histogram_rising"],
        "sqzmom_green": fields["sqzmom_green"],
        "volume_multiple": round(metrics["volume_ratio"], 4) if metrics.get("volume_ratio") is not None else None,
        "rsi": round(metrics["rsi"], 4) if metrics.get("rsi") is not None else None,
        "smart_money_score": int(metrics.get("smart_money_score", 0)),
        "preset": rules.get("active_preset"),
    }


def save_technical_filter_log(rows):
    if not rows:
        if not TECHNICAL_FILTER_LOG_FILE.exists():
            pd.DataFrame(columns=TECHNICAL_FILTER_LOG_COLUMNS).to_csv(TECHNICAL_FILTER_LOG_FILE, index=False)
        return

    new_rows = pd.DataFrame(rows, columns=TECHNICAL_FILTER_LOG_COLUMNS)
    if TECHNICAL_FILTER_LOG_FILE.exists():
        existing = pd.read_csv(TECHNICAL_FILTER_LOG_FILE)
        missing_columns = [column for column in TECHNICAL_FILTER_LOG_COLUMNS if column not in existing.columns]
        for column in missing_columns:
            existing[column] = None
        combined = pd.concat([existing[TECHNICAL_FILTER_LOG_COLUMNS], new_rows], ignore_index=True)
    else:
        combined = new_rows
    combined = combined.drop_duplicates(subset=["scan_id", "symbol"], keep="last")
    combined.to_csv(TECHNICAL_FILTER_LOG_FILE, index=False)


def legacy_rules_to_filters(rules):
    filters = [
        {"field": "price", "operator": ">=", "value": rules.get("min_price", 5)},
        {
            "field": "avg_dollar_volume",
            "operator": ">=",
            "value": rules.get("min_avg_dollar_volume", 20_000_000),
        },
        {
            "field": "rsi",
            "operator": "between",
            "min": rules.get("rsi_min", 40),
            "max": rules.get("rsi_max", 65),
        },
        {"field": "volume_ratio", "operator": ">=", "value": rules.get("volume_ratio_min", 1.2)},
        {"field": "score", "operator": ">=", "value": rules.get("min_score", 70)},
        {
            "field": "smart_money_score",
            "operator": ">=",
            "value": rules.get("smart_money_min", 50),
        },
    ]
    if rules.get("ma200_required", True):
        filters.append({"field": "above_ma200", "operator": "==", "value": True})
    return filters


def calculate_rsi(df, period):
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_atr(df, period=14):
    previous_close = df["Close"].shift(1)
    ranges = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - previous_close).abs(),
            (df["Low"] - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1).rolling(window=period).mean()


@dataclass
class CandidateBuckets:
    candidates: pd.DataFrame
    strong_candidates: pd.DataFrame
    order_candidates: pd.DataFrame


def analyze(symbol, rules, scan_id=None, technical_log_rows=None):
    try:
        df = yf.Ticker(symbol).history(period="1y")
        metrics = build_symbol_metrics(symbol, df, rules)
        if not metrics:
            return None
        if not apply_filters(metrics, rules.get("filters", [])):
            return None
        if use_technical_entry_filter():
            result = technical_entry_filter(df)
            print(f"[TECH FILTER] {symbol} pass={result['pass']} score={result['score']} checks={result['checks']}")
            if technical_log_rows is not None:
                technical_log_rows.append(technical_filter_log_row(metrics, result, scan_id, rules))
            if not result["pass"]:
                return None
            metrics.update(technical_filter_result_fields(result))
        else:
            metrics.update(
                {
                    "technical_filter_pass": None,
                    "technical_filter_score": None,
                    **{column: None for column in TECHNICAL_CHECK_COLUMNS},
                }
            )
        return candidate_from_metrics(metrics, rules)
    except Exception as exc:
        print(f"{symbol} scan failed: {exc}")
        return None


def build_symbol_metrics(symbol, df, rules):
    avg_window = int(rules.get("avg_volume_window", DEFAULT_RULES["avg_volume_window"]))
    rsi_period = int(rules.get("rsi_period", DEFAULT_RULES["rsi_period"]))
    min_history = max(220, avg_window + rsi_period + 5)

    if df.empty or len(df) < min_history:
        return None

    last_bar_time = df.index[-1]
    if hasattr(last_bar_time, "to_pydatetime"):
        last_bar_time = last_bar_time.to_pydatetime()

    if last_bar_time.tzinfo is None:
        last_bar_time = last_bar_time.replace(tzinfo=timezone.utc)

    data_age_days = (datetime.now(timezone.utc) - last_bar_time.astimezone(timezone.utc)).days
    if data_age_days > 3:
        print(f"{symbol} skipped: stale data age={data_age_days} days")
        return None

    df = df.copy()
    trend_metrics = calculate_trend(df)
    breakout_metrics = calculate_breakout(df)
    df["MA200"] = df["Close"].rolling(window=200).mean()
    df["RSI"] = calculate_rsi(df, rsi_period)
    df["AVG_VOLUME"] = df["Volume"].rolling(window=avg_window).mean()
    df["ATR"] = calculate_atr(df)

    price = safe_float(df["Close"].iloc[-1])
    previous_close = safe_float(df["Close"].iloc[-2])
    ma200 = safe_float(df["MA200"].iloc[-1])
    rsi = safe_float(df["RSI"].iloc[-1])
    volume = safe_float(df["Volume"].iloc[-1])
    avg_volume = safe_float(df["AVG_VOLUME"].iloc[-1])
    atr = safe_float(df["ATR"].iloc[-1])
    if not avg_volume or avg_volume <= 0:
        return None

    volume_ratio = volume / avg_volume
    avg_dollar_volume = price * avg_volume
    dollar_volume = price * volume
    momentum_score = calculate_momentum_score(rsi, volume_ratio, dollar_volume, volume_ratio)
    above_ma200 = price > ma200 if ma200 is not None else False
    rsi_in_default_range = 40 <= rsi <= 65 if rsi is not None else False
    gap_percent = ((price - previous_close) / previous_close) * 100 if previous_close else None

    score = 0
    if above_ma200:
        score += 40
    if rsi_in_default_range:
        score += 30
    if volume_ratio >= 1.2:
        score += 30

    smart_money_score = 0
    if volume_ratio >= 2:
        smart_money_score += 30
    if volume_ratio >= 3:
        smart_money_score += 20
    if above_ma200:
        smart_money_score += 20
    if rsi_in_default_range:
        smart_money_score += 20

    technical_score = int(score)
    final_score = calculate_final_score(
        technical_score,
        smart_money_score,
        trend_metrics["trend_score"],
        momentum_score,
        breakout_metrics["breakout_score"],
    )

    return {
        "symbol": symbol,
        "price": price,
        "ma20": trend_metrics["ma20"],
        "ma50": trend_metrics["ma50"],
        "ma200": ma200,
        "above_ma200": above_ma200,
        "rsi": rsi,
        "volume": volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "avg_dollar_volume": avg_dollar_volume,
        "dollar_volume": dollar_volume,
        "score": int(score),
        "technical_score": technical_score,
        "smart_money_score": int(smart_money_score),
        "trend": trend_metrics["trend"],
        "trend_score": trend_metrics["trend_score"],
        "momentum_score": momentum_score,
        "breakout_flag": breakout_metrics["breakout_flag"],
        "breakout_score": breakout_metrics["breakout_score"],
        "final_score": final_score,
        "atr": atr,
        "gap_percent": gap_percent,
        "market_cap": None,
        "relative_strength": None,
        "vwap_position": None,
        "float_shares": None,
        "sector": None,
        "exchange": None,
    }


def calculate_final_score(technical_score, smart_money_score, trend_score, momentum_score, breakout_score):
    weighted = (
        safe_score(technical_score) * 0.20
        + safe_score(smart_money_score) * 0.20
        + safe_score(trend_score) * 0.25
        + safe_score(momentum_score) * 0.25
        + safe_score(breakout_score) * 0.10
    )
    return int(max(0, min(100, round(weighted))))


def safe_score(value):
    try:
        return max(0, min(100, float(value)))
    except Exception:
        return 0


def safe_float(value):
    try:
        number = float(value)
    except Exception:
        return None
    return None if math.isnan(number) else number


def candidate_from_metrics(metrics, rules):
    smart_money_score = int(metrics["smart_money_score"])
    smart_money_min = int(get_filter_threshold(rules, "smart_money_score", 50))
    stock_type = "smart_money" if smart_money_score >= smart_money_min else "technical"
    candidate = {
        "symbol": metrics["symbol"],
        "price": round(metrics["price"], 2),
        "ma20": round(metrics["ma20"], 2) if metrics.get("ma20") is not None else None,
        "ma50": round(metrics["ma50"], 2) if metrics.get("ma50") is not None else None,
        "ma200": round(metrics["ma200"], 2),
        "rsi": round(metrics["rsi"], 2),
        "volume_ratio": round(metrics["volume_ratio"], 2),
        "avg_dollar_volume": round(metrics["avg_dollar_volume"], 0),
        "dollar_volume": round(metrics["dollar_volume"], 0),
        "score": int(metrics["score"]),
        "technical_score": int(metrics["technical_score"]),
        "smart_money_score": smart_money_score,
        "trend": metrics["trend"],
        "trend_score": int(metrics["trend_score"]),
        "momentum_score": int(metrics["momentum_score"]),
        "breakout_flag": bool(metrics["breakout_flag"]),
        "breakout_score": int(metrics["breakout_score"]),
        "final_score": int(metrics["final_score"]),
        "type": stock_type,
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    for column in TECHNICAL_FILTER_COLUMNS:
        candidate[column] = metrics.get(column)
    return candidate


def apply_filters(metrics, filters):
    for rule_filter in filters:
        if not evaluate_filter(metrics, rule_filter):
            return False
    return True


def evaluate_filter(metrics, rule_filter):
    field = rule_filter.get("field")
    operator = rule_filter.get("operator")
    if field not in SUPPORTED_FIELDS:
        warn_skip(f"Unsupported scanner field '{field}' skipped.")
        return True
    if operator not in SUPPORTED_OPERATORS:
        warn_skip(f"Unsupported scanner operator '{operator}' skipped.")
        return True
    actual = metrics.get(field)
    if actual is None:
        warn_skip(f"Scanner field '{field}' is not available for {metrics.get('symbol', 'symbol')}; skipped.")
        return True

    try:
        if operator == "between":
            return compare_between(actual, rule_filter.get("min"), rule_filter.get("max"))
        if operator in {"in", "not_in"}:
            expected = normalize_collection(rule_filter.get("value", []))
            contains = actual in expected
            return contains if operator == "in" else not contains

        expected = normalize_expected(actual, rule_filter.get("value"))
        if operator == ">=":
            return actual >= expected
        if operator == "<=":
            return actual <= expected
        if operator == ">":
            return actual > expected
        if operator == "<":
            return actual < expected
        if operator == "==":
            return actual == expected
        if operator == "!=":
            return actual != expected
    except (TypeError, ValueError) as exc:
        warn_skip(f"Scanner filter for field '{field}' is invalid: {exc}; skipped.")
        return True
    return True


def compare_between(actual, min_value, max_value):
    min_value = normalize_expected(actual, min_value)
    max_value = normalize_expected(actual, max_value)
    if min_value not in (None, "") and actual < min_value:
        return False
    if max_value not in (None, "") and actual > max_value:
        return False
    return True


def normalize_expected(actual, expected):
    if isinstance(actual, (int, float)) and isinstance(expected, str):
        try:
            number = float(expected)
            return int(number) if number.is_integer() else number
        except ValueError:
            return expected
    return expected


def normalize_collection(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value]


def warn_skip(message):
    warnings.warn(message, RuntimeWarning, stacklevel=2)
    print(f"WARNING: {message}")


def get_filter_threshold(rules, field, fallback):
    for rule_filter in rules.get("filters", []):
        if rule_filter.get("field") == field and rule_filter.get("operator") in {">=", ">"}:
            return rule_filter.get("value", fallback)
        if rule_filter.get("field") == field and rule_filter.get("operator") == "between":
            return rule_filter.get("min", fallback)
    return fallback


def get_default_threshold(field, fallback):
    return get_filter_threshold(DEFAULT_RULES, field, fallback)


def empty_candidates_frame():
    return pd.DataFrame(
        columns=[
            "symbol",
            "price",
            "ma20",
            "ma50",
            "ma200",
            "rsi",
            "volume_ratio",
            "avg_dollar_volume",
            "dollar_volume",
            "score",
            "technical_score",
            "smart_money_score",
            "trend",
            "trend_score",
            "momentum_score",
            "breakout_flag",
            "breakout_score",
            "final_score",
            "type",
            "scan_time",
            *TECHNICAL_FILTER_COLUMNS,
        ]
    )


def build_candidate_buckets(df, rules):
    rules = normalize_rules(rules)
    if df.empty:
        empty = empty_candidates_frame()
        return CandidateBuckets(empty, empty.copy(), empty.copy())

    sort_columns = [col for col in ["final_score", "score", "smart_money_score", "volume_ratio"] if col in df.columns]
    df = df.sort_values(by=sort_columns, ascending=False).reset_index(drop=True)
    smart_money_min = int(get_filter_threshold(rules, "smart_money_score", 50))
    score_min = int(get_filter_threshold(rules, "score", 70))
    volume_ratio_min = float(get_filter_threshold(rules, "volume_ratio", 1.2))
    strong = df[df["smart_money_score"] >= smart_money_min].copy()
    order_candidates = df[
        (df["score"] >= score_min)
        & (df["smart_money_score"] >= smart_money_min)
        & (df["volume_ratio"] >= volume_ratio_min)
    ].copy()
    if use_technical_entry_filter() and "technical_filter_pass" in order_candidates.columns:
        order_candidates = order_candidates[order_candidates["technical_filter_pass"] == True].copy()
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


def classify_candidate(row, smart_money_min):
    if int(row.get("smart_money_score", 0)) >= int(smart_money_min):
        return "수급/모멘텀 강한 후보"
    return "기술 조건 후보"

def build_realtime_slack_message(df, rules, market_session, previous_symbols=None):
    rules = normalize_rules(rules)
    previous_symbols = previous_symbols or set()
    top_count = int(rules.get("top_alert_count", 5) or 5)
    smart_money_min = int(get_filter_threshold(rules, "smart_money_score", 50))
    total_count = len(df)
    strong_count = int((df["smart_money_score"] >= smart_money_min).sum()) if not df.empty else 0
    volume_2x_count = int((df["volume_ratio"] >= 2).sum()) if not df.empty else 0
    top = df.head(top_count) if not df.empty else df
    current_symbols = set(df["symbol"].dropna().astype(str)) if not df.empty else set()
    new_symbols = sorted(current_symbols - previous_symbols)
    repeated_symbols = sorted(current_symbols & previous_symbols)
    smart_money_leaders = (
        df.sort_values("smart_money_score", ascending=False).head(5)["symbol"].dropna().astype(str).tolist()
        if not df.empty
        else []
    )

    lines = [
        f"전체 후보: {total_count}개",
        f"수급 강한 후보: {strong_count}개",
        f"거래량 2배 이상: {volume_2x_count}개",
        "",
        f"TOP {top_count}",
        "",
    ]

    if top.empty:
        lines.append("조건을 만족한 후보가 없습니다.")
    else:
        for idx, (_, row) in enumerate(top.iterrows(), start=1):
            lines.append(f"{idx}. {row['symbol']} - {classify_candidate(row, smart_money_min)}")
            lines.append(
                f"   가격: {float(row['price']):.2f} | RSI: {float(row['rsi']):.2f} | "
                f"거래량: {float(row['volume_ratio']):.2f}배"
            )
            lines.append(
                f"   기술점수: {int(row['score'])} | 수급점수: {int(row['smart_money_score'])}"
            )
            lines.append("")
            lines.append(f"   Trend: {row.get('trend', 'Unknown')}")
            lines.append(f"   Trend Score: {format_int(row.get('trend_score'))}")
            lines.append(f"   Momentum: {format_int(row.get('momentum_score'))}")
            lines.append(f"   Breakout: {format_yes_no(row.get('breakout_flag'))}")
            lines.append(f"   Breakout Score: {format_int(row.get('breakout_score'))}")
            lines.append(f"   Final Score: {format_int(row.get('final_score', row.get('score')))}")
            lines.append("")

    lines.extend(
        [
            "신규 등장:",
            "",
            f"* {', '.join(new_symbols) if new_symbols else '없음'}",
            "",
            "반복 등장:",
            "",
            f"* {', '.join(repeated_symbols) if repeated_symbols else '없음'}",
            "",
            "수급 리더:",
            "",
            f"* {', '.join(smart_money_leaders) if smart_money_leaders else '없음'}",
            "",
            "해석:",
            "",
            "* 반복 등장 종목은 수급 지속 가능성 우선 관찰",
            f"* 수급점수 {smart_money_min} 이상은 우선 관찰",
            "* 프리마켓/애프터마켓에서는 주문하지 않고 후보만 기록",
        ]
    )
    if market_session == "regular":
        lines[-1] = "* 정규장에서는 Paper Trading 안전조건 기준으로만 주문 검토"
    return "\n".join(lines)


def format_int(value):
    try:
        return int(float(value))
    except Exception:
        return 0


def format_yes_no(value):
    return "YES" if value is True or str(value).lower() in {"true", "1", "yes"} else "NO"


def scan(preset_name=None, send_slack=True, scan_limit=None):
    rules = load_scanner_rules(preset_name)
    scan_id = create_scan_id()
    universe = pd.read_csv(BASE_DIR / "universe.csv")

    if "exchange" in universe.columns:
        universe = universe[universe["exchange"].astype(str).str.upper() != "OTC"]

    symbols = universe["symbol"].dropna().astype(str).unique().tolist()
    configured_limit, scan_limit_enabled = resolve_scan_limit(rules, scan_limit)
    scan_limit = min(len(symbols), configured_limit)

    if scan_limit_enabled:
        print(f"[SCAN LIMIT] enabled limit={scan_limit}")
    print(f"Scanning {scan_limit} of {len(symbols)} symbols with preset {rules.get('active_preset')}")
    print(f"[SCAN ID] {scan_id}")
    results = []
    technical_log_rows = []
    for idx, symbol in enumerate(symbols[:scan_limit], start=1):
        print(f"[{idx}/{scan_limit}] {symbol}")
        result = analyze(symbol, rules, scan_id=scan_id, technical_log_rows=technical_log_rows)
        if result:
            results.append(result)

    previous_symbols = load_previous_symbols()
    buckets = build_candidate_buckets(pd.DataFrame(results), rules)
    save_candidate_files(buckets)
    save_technical_filter_log(technical_log_rows)

    message = build_realtime_slack_message(
        buckets.candidates,
        rules,
        get_us_market_session(),
        previous_symbols,
    )
    print(message)
    if send_slack:
        send_slack_alert(message)
    return buckets


if __name__ == "__main__":

    if not is_us_trading_day():
        print("NYSE closed. Scanner skipped.")
        raise SystemExit(0)

    scan()
