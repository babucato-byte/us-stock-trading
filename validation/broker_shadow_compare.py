"""One-shot, read-only KIS vs Toss market/account shadow comparison.

This module is deliberately not imported by production code.  It never calls
an execution method and writes only a redacted JSONL observation log.

Toss field names below were OBSERVED, key-only, from live Oracle responses on
2026-09-28 (status 200 for prices, 1m candles and buying power).  No other
field name is accepted: a response that lacks them fails closed with
TOSS_QUOTE_SCHEMA_UNVERIFIED / TOSS_CANDLE_SCHEMA_UNVERIFIED rather than being
read through a guessed alias.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from brokers.toss_shadow_adapter import TossShadowAdapter


SYMBOLS = ("AAPL", "NVDA", "QQQ")
DEFAULT_CYCLES = 20
DEFAULT_INTERVAL_SECONDS = 5.0
KIS_REQUIRED_ENV = (
    "KIS_ENV", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
    "KIS_ACCOUNT_PRODUCT_CD", "KIS_ACCOUNT_READ_ENABLED",
)

# Observed: GET /api/v1/prices -> {"result": [{symbol, lastPrice, currency, timestamp}]}
TOSS_QUOTE_FIELDS = {"symbol": "symbol", "price": "lastPrice"}
# Observed: GET /api/v1/candles -> {"result": {"candles": [{timestamp, openPrice,
# highPrice, lowPrice, closePrice, volume, currency}], "nextBefore"}}
TOSS_CANDLE_ROWS_KEY = "candles"
TOSS_CANDLE_FIELDS = {
    "timestamp": "timestamp", "open": "openPrice", "high": "highPrice",
    "low": "lowPrice", "close": "closePrice", "volume": "volume",
}
# Observed: GET /api/v1/buying-power -> {"result": {cashBuyingPower, currency}}
TOSS_BUYING_POWER_FIELDS = {"amount": "cashBuyingPower", "currency": "currency"}

BAR_MATCH_PERCENT = 0.10
BAR_MINOR_PERCENT = 0.30
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class KIS_ENVIRONMENT_NOT_LOADED(RuntimeError):
    """The existing KIS runtime environment was not sourced for this runner."""


class TOSS_QUOTE_SCHEMA_UNVERIFIED(ValueError):
    """The quote response lacks the observed keys; no alias is guessed."""


class TOSS_CANDLE_SCHEMA_UNVERIFIED(ValueError):
    """The candle response lacks the observed keys; no alias is guessed."""


class TOSS_QUOTE_SYMBOL_MISSING(LookupError):
    """The quote batch parsed, but did not include this symbol."""


@dataclass(frozen=True)
class QuoteObservation:
    symbol: str
    provider: str
    request_started_at: str
    response_received_at: str
    latency_ms: int | None  # None on failure: a failed request has no meaningful latency
    price: float | None
    error: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    return _utc_now().isoformat()


def _safe_error(exc: BaseException) -> str:
    """Errors are type-only so tokens, credentials, and headers cannot be logged."""
    return type(exc).__name__


def _is_rate_limited(exc: BaseException) -> bool:
    """Inspected locally, never printed: the client's fixed `HTTP_ERROR status=429`."""
    return any(getattr(exc, name, None) == 429 for name in ("code", "status")) or "status=429" in str(exc)


def _payload(data: Any) -> Any:
    if isinstance(data, Mapping) and "result" in data:
        return data["result"]
    return data


def _keys(mapping: Mapping[Any, Any]) -> list[str]:
    """Key names only; a non-identifier key (e.g. a numeric id) is never echoed."""
    return sorted(str(key) if _IDENTIFIER.match(str(key)) else "<NON_IDENTIFIER_KEY>" for key in mapping)


def schema_probe(data: Any, *, candle: bool = False) -> Mapping[str, Any]:
    """Return only response shape metadata; this intentionally never includes values."""
    payload = _payload(data)
    result: dict[str, Any] = {
        "top_level_keys": _keys(data) if isinstance(data, Mapping) else [],
        "payload_type": type(payload).__name__,
    }
    if candle:
        result["payload_keys"] = _keys(payload) if isinstance(payload, Mapping) else []
        rows = payload.get(TOSS_CANDLE_ROWS_KEY) if isinstance(payload, Mapping) else None
    else:
        rows = payload
    result["row_keys"] = _keys(rows[0]) if isinstance(rows, list) and rows and isinstance(rows[0], Mapping) else []
    return result


def quote_schema_verified(probe: Mapping[str, Any]) -> bool:
    return probe.get("payload_type") == "list" and set(TOSS_QUOTE_FIELDS.values()).issubset(probe.get("row_keys", ()))


def candle_schema_verified(probe: Mapping[str, Any]) -> bool:
    return (
        TOSS_CANDLE_ROWS_KEY in probe.get("payload_keys", ())
        and set(TOSS_CANDLE_FIELDS.values()).issubset(probe.get("row_keys", ()))
    )


def format_schema_probe(title: str, probe: Mapping[str, Any], *, verified: bool | None = None) -> str:
    """YAML-like, key-only diagnostic output suitable for Oracle logs."""
    lines = ["[%s]" % title]
    for label in ("error", "top_level_keys", "payload_type", "payload_keys", "row_keys"):
        if label not in probe:
            continue
        value = probe[label]
        lines.append("%s:" % label)
        if isinstance(value, list):
            lines.extend("- %s" % item for item in value)
        else:
            lines.append(str(value))
    if verified is not None:
        lines.append("verified: %s" % ("YES" if verified else "NO"))
    return "\n".join(lines)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_toss_quote_rows(data: Any) -> Mapping[str, float]:
    """Parse only the observed schema; no alternate field names are guessed."""
    rows = _payload(data)
    if not isinstance(rows, list) or not rows:
        raise TOSS_QUOTE_SCHEMA_UNVERIFIED()
    symbol_key, price_key = TOSS_QUOTE_FIELDS["symbol"], TOSS_QUOTE_FIELDS["price"]
    prices: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping) or symbol_key not in row or price_key not in row:
            raise TOSS_QUOTE_SCHEMA_UNVERIFIED()
        price = _number(row[price_key])
        if price is None:
            raise TOSS_QUOTE_SCHEMA_UNVERIFIED()
        prices[str(row[symbol_key]).upper()] = price
    return prices


def parse_toss_candles(data: Any) -> list[Mapping[str, Any]]:
    """Observed candle rows normalized to {at, open, high, low, close, volume}."""
    payload = _payload(data)
    rows = payload.get(TOSS_CANDLE_ROWS_KEY) if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise TOSS_CANDLE_SCHEMA_UNVERIFIED()
    normalized = []
    for row in rows:
        if not isinstance(row, Mapping) or not set(TOSS_CANDLE_FIELDS.values()).issubset(row):
            raise TOSS_CANDLE_SCHEMA_UNVERIFIED()
        bar = {name: row[key] for name, key in TOSS_CANDLE_FIELDS.items()}
        bar["at"] = bar.pop("timestamp")
        normalized.append(bar)
    return normalized


def normalize_toss_buying_power(data: Any) -> Mapping[str, Any]:
    """Only the observed keys; USD is reported only when Toss itself says USD."""
    payload = _payload(data)
    if not isinstance(payload, Mapping) or not set(TOSS_BUYING_POWER_FIELDS.values()).issubset(payload):
        return {"currency": "UNKNOWN", "buying_power_usd": "UNKNOWN", "raw_available": data is not None}
    currency = str(payload[TOSS_BUYING_POWER_FIELDS["currency"]]).upper()
    amount = _number(payload[TOSS_BUYING_POWER_FIELDS["amount"]])
    return {
        "currency": currency,
        "buying_power_usd": amount if currency == "USD" and amount is not None else "UNKNOWN",
        "raw_available": True,
    }


def missing_kis_environment(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    mapping = os.environ if environ is None else environ
    return tuple(name for name in KIS_REQUIRED_ENV if not str(mapping.get(name, "")).strip())


def format_kis_environment(environ: Mapping[str, str] | None = None) -> str:
    """Presence only; a value, its length, or any part of it is never rendered."""
    missing = set(missing_kis_environment(environ))
    lines = ["[KIS_ENVIRONMENT]"]
    lines.extend("%s: %s" % (name, "MISSING" if name in missing else "PRESENT") for name in KIS_REQUIRED_ENV)
    if missing:
        lines.append("KIS_ENVIRONMENT_NOT_LOADED")
    return "\n".join(lines)


def provider_error_summary(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Mapping[str, int]]:
    summary: dict[str, Counter[str]] = {"KIS": Counter(), "TOSS": Counter()}
    for record in records:
        for provider, key in (("KIS", "kis"), ("TOSS", "toss")):
            nested = record.get(key)
            if isinstance(nested, Mapping) and isinstance(nested.get("error"), str):
                summary[provider][nested["error"]] += 1
    return {provider: dict(counts) for provider, counts in summary.items()}


def price_difference(kis: float | None, toss: float | None) -> tuple[float | None, float | None]:
    if kis is None or toss is None or kis == 0:
        return None, None
    absolute = abs(kis - toss)
    return absolute, (absolute / abs(kis)) * 100.0


def classify_difference(diff_percent: float | None) -> str:
    if diff_percent is None:
        return "NOT_COMPARABLE"
    if diff_percent <= 0.10:
        return "NORMAL"
    if diff_percent <= 0.30:
        return "REVIEW"
    return "LARGE_DIFFERENCE"


def _bar_minute(raw: Any) -> datetime | None:
    """UTC minute of a bar label: KIS gives an aware datetime, Toss an offset ISO string."""
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(raw, datetime) or raw.tzinfo is None:
        return None
    return raw.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _completed_bars(rows: Any, now: datetime | None = None) -> dict[datetime, Mapping[str, Any]]:
    """Bars keyed by UTC minute, excluding the current (forming) minute and anything later."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    current_minute = (now or _utc_now()).astimezone(timezone.utc).replace(second=0, microsecond=0)
    completed = {}
    for row in rows:
        minute = _bar_minute(row.get("at")) if isinstance(row, Mapping) else None
        if minute is not None and minute < current_minute:
            completed[minute] = row
    return completed


def classify_bar(kis: Mapping[str, Any] | None, toss: Mapping[str, Any] | None) -> tuple[str, float | None]:
    """Both bars use normalized keys {open, high, low, close}."""
    if not kis or not toss:
        return "NOT_COMPARABLE", None
    differences = []
    for key in ("open", "high", "low", "close"):
        left, right = _number(kis.get(key)), _number(toss.get(key))
        if left is None or right is None or left == 0:
            return "NOT_COMPARABLE", None
        differences.append(abs(left - right) / abs(left) * 100.0)
    worst = max(differences)
    if worst <= BAR_MATCH_PERCENT:
        return "MATCH", worst
    if worst <= BAR_MINOR_PERCENT:
        return "MINOR_DIFF", worst
    return "MAJOR_DIFF", worst


def compare_completed_bars(kis_rows: Any, toss_rows: Any, now: datetime | None = None) -> Mapping[str, Any]:
    """Compare the most recent completed minute BOTH providers have, never two different minutes."""
    kis, toss = _completed_bars(kis_rows, now), _completed_bars(toss_rows, now)
    common = sorted(set(kis) & set(toss))
    if not common:
        reason = "KIS_NO_COMPLETED_BAR" if not kis else "TOSS_NO_COMPLETED_BAR" if not toss else "NO_COMMON_MINUTE"
        return {"classification": "NOT_COMPARABLE", "minute_utc": None, "max_ohlc_diff_percent": None, "reason": reason}
    minute = common[-1]
    classification, worst = classify_bar(kis[minute], toss[minute])
    return {"classification": classification, "minute_utc": minute.isoformat(), "max_ohlc_diff_percent": worst, "reason": None}


def latency_summary(samples: Sequence[int]) -> Mapping[str, float | int | None]:
    if not samples:
        return {"min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(samples)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {"min": ordered[0], "median": statistics.median(ordered), "p95": ordered[index], "max": ordered[-1]}


class BrokerShadowComparison:
    """Coordinates independent read-only providers; one failure never stops the other."""

    def __init__(self, kis: Any, toss: TossShadowAdapter, *, symbols: Sequence[str] = SYMBOLS) -> None:
        self.kis, self.toss = kis, toss
        self.symbols = tuple(symbols)
        self.records: list[dict[str, Any]] = []
        # Request-level: one KIS request per symbol, one Toss request per batch.
        self.latency: dict[str, dict[str, Any]] = {p: {"success": [], "failed": 0} for p in ("KIS", "TOSS")}
        self.rate_limited: Counter[str] = Counter()
        self.authentication_error: str | None = None

    def _failed(self, provider: str, exc: BaseException) -> str:
        self.latency[provider]["failed"] += 1
        if _is_rate_limited(exc):
            self.rate_limited[provider] += 1
        return _safe_error(exc)

    def authenticate(self) -> None:
        """Toss price/candle reads do not authenticate themselves; only the account path does.

        Without this, every quote and candle is sent tokenless and fails
        AUTHENTICATION_REQUIRED -- the cause of the 60/60 Toss errors in the
        first Oracle run.
        """
        try:
            self.toss.authenticate()
        except Exception as exc:
            self.authentication_error = self._failed("TOSS", exc)

    def _quote(self, provider: str, symbol: str, fetch: Callable[[], float]) -> QuoteObservation:
        started = _utc_now()
        clock = time.monotonic()
        try:
            price = _number(fetch())
            if price is None:
                raise ValueError("malformed price")
        except Exception as exc:  # one provider must not suppress the other
            return QuoteObservation(symbol, provider, started.isoformat(), _timestamp(), None, None, self._failed(provider, exc))
        latency = int((time.monotonic() - clock) * 1000)
        self.latency[provider]["success"].append(latency)
        return QuoteObservation(symbol, provider, started.isoformat(), _timestamp(), latency, price, None)

    def run_cycle(self) -> list[dict[str, Any]]:
        toss_started = _utc_now()
        try:
            toss_result = self.toss.get_prices(self.symbols)
            toss_prices = parse_toss_quote_rows(toss_result.data)
        except Exception as exc:
            toss_prices, toss_error, toss_latency = {}, self._failed("TOSS", exc), None
        else:
            toss_error, toss_latency = None, toss_result.latency_ms
            self.latency["TOSS"]["success"].append(toss_latency)
        rows = []
        for symbol in self.symbols:
            kis_quote = self._quote("KIS", symbol, lambda item=symbol: self.kis.quote(item))
            error = toss_error or (None if symbol in toss_prices else TOSS_QUOTE_SYMBOL_MISSING.__name__)
            toss_quote = QuoteObservation(symbol, "TOSS", toss_started.isoformat(), _timestamp(), toss_latency, toss_prices.get(symbol), error)
            absolute, percent = price_difference(kis_quote.price, toss_quote.price)
            row = {"timestamp": _timestamp(), "symbol": symbol, "kis": asdict(kis_quote), "toss": asdict(toss_quote), "absolute_diff": absolute, "diff_percent": percent, "classification": classify_difference(percent)}
            rows.append(row)
            self.records.append(row)
        return rows

    def run(self, *, cycles: int = DEFAULT_CYCLES, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> list[dict[str, Any]]:
        if cycles < 1 or interval_seconds < 0:
            raise ValueError("invalid comparison schedule")
        for index in range(cycles):
            self.run_cycle()
            if index + 1 < cycles:
                time.sleep(interval_seconds)
        return self.records

    def compare_buying_power(self) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "KIS": {"currency": "USD", "buying_power_usd": None, "source": "KISBroker.get_orderable_usd", "latency_ms": None},
            "TOSS": {"currency": "UNKNOWN", "buying_power_usd": "UNKNOWN", "source": "GET /api/v1/buying-power", "latency_ms": None},
        }
        try:
            started = time.monotonic()
            result["KIS"]["buying_power_usd"] = _number(self.kis.buying_power())
            result["KIS"]["latency_ms"] = int((time.monotonic() - started) * 1000)
        except Exception as exc:
            result["KIS"]["error"] = _safe_error(exc)
        try:
            toss = self.toss.get_buying_power()
            result["TOSS"]["latency_ms"] = toss.latency_ms
            result["TOSS"].update(normalize_toss_buying_power(toss.data))
        except Exception as exc:
            result["TOSS"]["error"] = _safe_error(exc)
        return result

    def compare_bars(self, now: datetime | None = None) -> list[Mapping[str, Any]]:
        results = []
        for symbol in self.symbols:
            kis_rows: Any = []
            toss_rows: Any = []
            errors = {}
            try:
                kis_rows = self.kis.bars(symbol)  # market_data.kis_minute_chart: {at, open, high, low, close, ...}
            except Exception as exc:
                errors["kis_error"] = _safe_error(exc)
            try:
                toss_rows = parse_toss_candles(self.toss.get_1m_candles(symbol).data)
            except Exception as exc:
                errors["toss_error"] = _safe_error(exc)
            results.append({"symbol": symbol, **compare_completed_bars(kis_rows, toss_rows, now), **errors})
        return results

    def write_log(self, directory: Path = Path("logs")) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("toss_kis_shadow_compare_" + _utc_now().strftime("%Y%m%dT%H%M%SZ") + ".jsonl")
        with path.open("x", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return path


def latest_classifications(records: Sequence[Mapping[str, Any]], symbols: Sequence[str]) -> Mapping[str, str]:
    latest = {}
    for symbol in symbols:
        row = next((r for r in reversed(records) if r.get("symbol") == symbol), {})
        latest[symbol] = row.get("classification", "NOT_COMPARABLE")
    return latest


def final_verdict(
    *, kis_env_loaded: bool, schema_verified: bool, comparison: BrokerShadowComparison,
    bars: Sequence[Mapping[str, Any]],
) -> tuple[str, list[str]]:
    """PASS only when every success criterion holds; otherwise name each one that failed."""
    reasons = []
    if not kis_env_loaded:
        reasons.append("KIS_ENVIRONMENT_NOT_LOADED")
    if not schema_verified:
        reasons.append("TOSS_SCHEMA_UNVERIFIED")
    for provider in ("KIS", "TOSS"):
        if not comparison.latency[provider]["success"]:
            reasons.append("%s_NO_SUCCESSFUL_QUOTE" % provider)
    for symbol, label in latest_classifications(comparison.records, comparison.symbols).items():
        if label == "NOT_COMPARABLE":
            reasons.append("%s_PRICE_NOT_COMPARABLE" % symbol)
    for bar in bars:
        if bar["classification"] == "NOT_COMPARABLE":
            reasons.append("%s_BAR_NOT_COMPARABLE" % bar["symbol"])
    if sum(comparison.rate_limited.values()):
        reasons.append("RATE_LIMITED_429")
    return ("TOSS_SHADOW_COMPARISON_PASS" if not reasons else "TOSS_SHADOW_COMPARISON_INCOMPLETE"), reasons


class _KISReadOnlyFacade:
    """Lazy KIS wrapper that only invokes established read methods."""

    def __init__(self) -> None:
        if missing_kis_environment():
            raise KIS_ENVIRONMENT_NOT_LOADED()
        from brokers.kis_broker import KISBroker
        from domain.instrument import build_instrument
        self._broker = KISBroker()
        self._instrument = build_instrument

    def _item(self, symbol: str):
        return self._instrument(symbol, exchange="NASDAQ")

    def quote(self, symbol: str) -> float:
        return self._broker.get_current_price(self._item(symbol))

    def buying_power(self) -> float:
        item = self._item("AAPL")
        return self._broker.get_orderable_usd(item, self._broker.get_current_price(item))

    def bars(self, symbol: str) -> list[Mapping[str, Any]]:
        from market_data.kis_minute_chart import fetch
        return fetch(self._broker, symbol=symbol, exchange="NASDAQ")


class _UnavailableKIS:
    """Preserves Toss observation when KIS credentials were not sourced."""

    @staticmethod
    def quote(_symbol: str) -> float:
        raise KIS_ENVIRONMENT_NOT_LOADED()

    @staticmethod
    def buying_power() -> float:
        raise KIS_ENVIRONMENT_NOT_LOADED()

    @staticmethod
    def bars(_symbol: str) -> list[Mapping[str, Any]]:
        raise KIS_ENVIRONMENT_NOT_LOADED()


def _probe(title: str, fetch: Callable[[], Any], *, candle: bool) -> tuple[Mapping[str, Any], bool]:
    try:
        probe = schema_probe(fetch().data, candle=candle)
    except Exception as exc:
        probe = {"error": _safe_error(exc)}
    verified = candle_schema_verified(probe) if candle else quote_schema_verified(probe)
    print("\n" + format_schema_probe(title, probe, verified=verified))
    return probe, verified


def _print_latency(comparison: BrokerShadowComparison) -> None:
    print("\n[LATENCY]")
    for provider in ("KIS", "TOSS"):
        summary = latency_summary(comparison.latency[provider]["success"])
        print("\n%s success (n=%s):" % (provider, len(comparison.latency[provider]["success"])))
        for label in ("min", "median", "p95", "max"):
            print("%s: %s" % (label, summary[label]))
        print("\n%s failed:\ncount: %s" % (provider, comparison.latency[provider]["failed"]))


def main() -> int:
    missing_env = missing_kis_environment()
    kis = _UnavailableKIS() if missing_env else _KISReadOnlyFacade()
    toss = TossShadowAdapter()
    comparison = BrokerShadowComparison(kis, toss)
    print("[IDENTITY]\nmode=READ_ONLY cycles=%s interval_seconds=%s symbols=%s" % (DEFAULT_CYCLES, DEFAULT_INTERVAL_SECONDS, ",".join(SYMBOLS)))
    print("\n" + format_kis_environment())
    comparison.authenticate()
    _, quote_verified = _probe("TOSS_QUOTE_SCHEMA", lambda: toss.get_prices(["AAPL"]), candle=False)
    _, candle_verified = _probe("TOSS_CANDLE_SCHEMA", lambda: toss.get_1m_candles("AAPL"), candle=True)
    print("\n[KIS_STATUS]\n%s" % ("KIS_ENVIRONMENT_NOT_LOADED" if missing_env else "read_only=true"))
    print("\n[TOSS_STATUS]\nread_only=true execution=BLOCKED authentication=%s" % (comparison.authentication_error or "OK"))
    comparison.run()
    bars, buying_power = comparison.compare_bars(), comparison.compare_buying_power()
    path = comparison.write_log()
    print("\n[PRICE_COMPARISON]")
    for symbol, label in latest_classifications(comparison.records, comparison.symbols).items():
        counts = Counter(r["classification"] for r in comparison.records if r["symbol"] == symbol)
        print("%s: %s (all cycles: %s)" % (symbol, label, dict(sorted(counts.items()))))
    print("\n[BAR_COMPARISON]")
    for bar in bars:
        print(json.dumps(bar, sort_keys=True))
    _print_latency(comparison)
    print("\n[BUYING_POWER]\nKIS: %s\nTOSS: %s" % (buying_power["KIS"], buying_power["TOSS"]))
    print("\n[COMMISSION]\nKIS: UNKNOWN\nTOSS: opaque read endpoint available")
    print("\n[PROVIDER_ERRORS]")
    for provider, counts in provider_error_summary(comparison.records).items():
        print("\n%s:" % provider)
        for name, count in sorted(counts.items()):
            print("%s: %s" % (name, count))
        if not counts:
            print("none")
    print("\n429:\nKIS: %s\nTOSS: %s" % (comparison.rate_limited["KIS"], comparison.rate_limited["TOSS"]))
    print("\n[EXECUTION_SAFETY]\nBUY: BLOCKED\nSELL: BLOCKED\nCANCEL: BLOCKED\nMODIFY: BLOCKED\nOCO: BLOCKED\nOTO: BLOCKED")
    verdict, reasons = final_verdict(
        kis_env_loaded=not missing_env, schema_verified=quote_verified and candle_verified,
        comparison=comparison, bars=bars,
    )
    print("\n[FINAL]\n%s" % verdict)
    for reason in reasons:
        print("- %s" % reason)
    print("Production changed: NO\nLive order executed: NO\nLog: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
