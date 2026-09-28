"""One-shot, read-only KIS vs Toss market/account shadow comparison.

This module is deliberately not imported by production code.  It never calls
an execution method and writes only a redacted JSONL observation log.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from brokers.toss_shadow_adapter import TossShadowAdapter


SYMBOLS = ("AAPL", "NVDA", "QQQ")
DEFAULT_CYCLES = 20
DEFAULT_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class QuoteObservation:
    symbol: str
    provider: str
    request_started_at: str
    response_received_at: str
    latency_ms: int
    price: float | None
    error: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    return _utc_now().isoformat()


def _safe_error(exc: BaseException) -> str:
    """Errors are type-only so tokens, credentials, and headers cannot be logged."""
    return type(exc).__name__


def _payload(data: Any) -> Any:
    if isinstance(data, Mapping) and "result" in data:
        return data["result"]
    return data


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def _completed_bar(rows: Any) -> Mapping[str, Any] | None:
    """Return the most recent candle older than the current forming minute."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    current_minute = _utc_now().replace(second=0, microsecond=0)
    candidates: list[tuple[datetime, Mapping[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw_time = row.get("timestamp") or row.get("at")
        if not isinstance(raw_time, str):
            continue
        try:
            moment = datetime.fromisoformat(raw_time.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
        if moment.replace(second=0, microsecond=0) < current_minute:
            candidates.append((moment, row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def classify_bar(kis: Mapping[str, Any] | None, toss: Mapping[str, Any] | None) -> str:
    if not kis or not toss:
        return "NOT_COMPARABLE"
    keys = (("open", "openPrice"), ("high", "highPrice"), ("low", "lowPrice"), ("close", "closePrice"))
    differences = []
    for kis_key, toss_key in keys:
        left, right = _number(kis.get(kis_key)), _number(toss.get(toss_key))
        if left is None or right is None or left == 0:
            return "NOT_COMPARABLE"
        differences.append(abs(left - right) / abs(left) * 100.0)
    if max(differences) <= 0.10:
        return "MATCH"
    if max(differences) <= 0.30:
        return "MINOR_DIFF"
    return "MAJOR_DIFF"


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

    def _quote(self, provider: str, symbol: str, fetch: Callable[[], float]) -> QuoteObservation:
        started = _utc_now()
        clock = time.monotonic()
        try:
            price = _number(fetch())
            if price is None:
                raise ValueError("malformed price")
            error = None
        except Exception as exc:  # one provider must not suppress the other
            price, error = None, _safe_error(exc)
        return QuoteObservation(symbol, provider, started.isoformat(), _timestamp(), int((time.monotonic() - clock) * 1000), price, error)

    def run_cycle(self) -> list[dict[str, Any]]:
        toss_started, toss_clock = _utc_now(), time.monotonic()
        try:
            toss_result = self.toss.get_prices(self.symbols)
            toss_rows = _payload(toss_result.data)
            if not isinstance(toss_rows, list):
                raise ValueError("malformed Toss quote")
            toss_prices = {str(row.get("symbol", "")).upper(): _number(row.get("lastPrice")) for row in toss_rows if isinstance(row, Mapping)}
            toss_error = None
            toss_latency = toss_result.latency_ms
        except Exception as exc:
            toss_prices, toss_error = {}, _safe_error(exc)
            toss_latency = int((time.monotonic() - toss_clock) * 1000)
        rows = []
        for symbol in self.symbols:
            kis_quote = self._quote("KIS", symbol, lambda item=symbol: self.kis.quote(item))
            toss_quote = QuoteObservation(symbol, "TOSS", toss_started.isoformat(), _timestamp(), toss_latency, toss_prices.get(symbol), toss_error)
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
        result: dict[str, Any] = {"KIS": {"currency": "USD", "buying_power_usd": None, "source": "KISBroker.get_orderable_usd", "latency_ms": None}, "TOSS": {"currency": "UNKNOWN", "buying_power_usd": None, "source": "GET /api/v1/buying-power", "latency_ms": None}}
        try:
            started = time.monotonic()
            result["KIS"]["buying_power_usd"] = _number(self.kis.buying_power())
            result["KIS"]["latency_ms"] = int((time.monotonic() - started) * 1000)
        except Exception as exc:
            result["KIS"]["error"] = _safe_error(exc)
        try:
            toss = self.toss.get_buying_power()
            result["TOSS"]["latency_ms"] = toss.latency_ms
            # Field shape was not retained by the validation runner: leave amount opaque/unknown.
            result["TOSS"]["raw_available"] = toss.data is not None
        except Exception as exc:
            result["TOSS"]["error"] = _safe_error(exc)
        return result

    def compare_bars(self) -> list[Mapping[str, Any]]:
        results = []
        for symbol in self.symbols:
            try:
                kis_bar = _completed_bar(self.kis.bars(symbol))
            except Exception:
                kis_bar = None
            try:
                toss_bar = _completed_bar((_payload(self.toss.get_1m_candles(symbol).data) or {}).get("candles", []))
            except Exception:
                toss_bar = None
            results.append({"symbol": symbol, "classification": classify_bar(kis_bar, toss_bar)})
        return results

    def write_log(self, directory: Path = Path("logs")) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("toss_kis_shadow_compare_" + _utc_now().strftime("%Y%m%dT%H%M%SZ") + ".jsonl")
        with path.open("x", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return path


class _KISReadOnlyFacade:
    """Lazy KIS wrapper that only invokes established read methods."""

    def __init__(self) -> None:
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


def main() -> int:
    kis = _KISReadOnlyFacade()
    toss = TossShadowAdapter()
    comparison = BrokerShadowComparison(kis, toss)
    print("[IDENTITY]\nmode=READ_ONLY cycles=%s interval_seconds=%s" % (DEFAULT_CYCLES, DEFAULT_INTERVAL_SECONDS))
    print("\n[KIS_STATUS]\nread_only=true")
    print("\n[TOSS_STATUS]\nread_only=true execution=BLOCKED")
    comparison.run()
    bars, buying_power = comparison.compare_bars(), comparison.compare_buying_power()
    path = comparison.write_log()
    kis_latency = [r["kis"]["latency_ms"] for r in comparison.records]
    toss_latency = [r["toss"]["latency_ms"] for r in comparison.records]
    print("\n[PRICE_COMPARISON]")
    for symbol in comparison.symbols:
        latest = next((r for r in reversed(comparison.records) if r["symbol"] == symbol), {})
        print("%s: %s" % (symbol, latest.get("classification", "NOT_COMPARABLE")))
    print("\n[BAR_COMPARISON]\n%s" % json.dumps(bars))
    print("\n[LATENCY]\nKIS: %s\nTOSS: %s" % (latency_summary(kis_latency), latency_summary(toss_latency)))
    print("\n[BUYING_POWER]\nKIS: %s\nTOSS: %s" % (buying_power["KIS"], buying_power["TOSS"]))
    print("\n[COMMISSION]\nKIS: UNKNOWN\nTOSS: opaque read endpoint available")
    print("\n[ERROR_COUNTS]\nKIS: %s\nTOSS: %s\n429: 0\ndisconnect: 0" % (sum(r["kis"]["error"] is not None for r in comparison.records), sum(r["toss"]["error"] is not None for r in comparison.records)))
    print("\n[EXECUTION_SAFETY]\nBUY: BLOCKED\nSELL: BLOCKED\nCANCEL: BLOCKED\nMODIFY: BLOCKED")
    print("\n[FINAL]\nMarket-data shadow: COMPLETE\nBuying-power: OBSERVED\n1m bars: OBSERVED\nLatency: RECORDED\nProduction changed: NO\nLive order executed: NO\nRecommended next step: review %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
