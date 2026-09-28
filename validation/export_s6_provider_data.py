"""Self-contained Oracle-side, read-only Phase-A OHLCV exporter.

The exporter deliberately performs no S6 calculation.  It emits only a
whitelisted market-data schema which can be copied off Oracle and evaluated
locally.  Toss is called through its fail-closed read-only client and only its
REST candle endpoint is used; no account, quote, websocket, or order API is
needed here.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from brokers.kis_broker import KISBroker
from market_data.kis_minute_chart import fetch
from validation.toss_openapi.client import TossReadOnlyClient


DEFAULT_OUTPUT_DIR = Path("/tmp/s6-provider-export")
FORBIDDEN_TEXT = ("token", "secret", "account", "authorization", "cookie", "header", "app_key", "client_secret")
PHASE_A_SYMBOLS = ("AAPL", "NVDA", "QQQ", "AMD", "PLTR", "TSLA", "MSFT", "AMZN", "META", "AVGO")
PHASE_B_MIN_SYMBOLS = 50
PHASE_B_MAX_SYMBOLS = 100


def _error(exc: BaseException) -> str:
    """Never include a network response, URL, request, or exception message."""
    return type(exc).__name__


def _rate_limited(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == 429 or getattr(exc, "status", None) == 429 or "429" in str(exc)


def load_symbols(path: Path, *, minimum: int = 1, maximum: int = 100) -> list[str]:
    """Read a ``symbol`` CSV column without importing a candidate pipeline.

    The input file is read once, never written.  Source order is retained so
    a production Discovery artifact remains a Discovery-derived universe.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        if not rows.fieldnames or "symbol" not in {name.lower() for name in rows.fieldnames}:
            raise ValueError("SYMBOL_FILE_REQUIRES_SYMBOL_COLUMN")
        column = next(name for name in rows.fieldnames if name.lower() == "symbol")
        symbols: list[str] = []
        seen: set[str] = set()
        for row in rows:
            symbol = str(row.get(column) or "").strip().upper()
            if symbol and symbol not in seen:
                symbols.append(symbol)
                seen.add(symbol)
    if not minimum <= len(symbols) <= maximum:
        raise ValueError("SYMBOL_FILE_COUNT_OUT_OF_RANGE")
    return symbols


def _summary(records: Sequence[Mapping[str, Any]], cycle_durations: Sequence[float]) -> Mapping[str, Any]:
    latencies = [record["latency_ms"] for record in records if record.get("success") and record.get("latency_ms") is not None]
    ordered = sorted(latencies)
    p95 = ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)] if ordered else None
    duration_ms = [round(value * 1000, 3) for value in cycle_durations]
    median_total = statistics.median(duration_ms) if duration_ms else None
    symbols_per_second = (len(records) / sum(cycle_durations)) if cycle_durations and sum(cycle_durations) else None
    return {"requests": len(records), "success": sum(bool(record.get("success")) for record in records),
            "failed": sum(not bool(record.get("success")) for record in records),
            "min_latency_ms": min(latencies) if latencies else None,
            "median_latency_ms": statistics.median(latencies) if latencies else None,
            "p95_latency_ms": p95, "max_latency_ms": max(latencies) if latencies else None,
            "total_duration_ms": round(sum(duration_ms), 3), "median_cycle_duration_ms": median_total,
            "symbols_per_second": symbols_per_second, "request_records": list(records)}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _normalize_timestamp(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _normalize_completed_bars(rows: Sequence[Mapping[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """The only serialized records: UTC completed OHLCV bars."""
    cutoff = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    output: dict[datetime, dict[str, Any]] = {}
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        timestamp = _normalize_timestamp(row.get("at", row.get("timestamp")))
        values = [_number(row.get(name)) for name in ("open", "high", "low", "close", "volume")]
        if timestamp is None or timestamp >= cutoff or any(value is None for value in values):
            continue
        open_, high, low, close, volume = values
        if min(open_, high, low, close) <= 0 or volume < 0 or low > high:
            continue
        output[timestamp] = {"timestamp": timestamp.isoformat(), "open": open_, "high": high,
                             "low": low, "close": close, "volume": volume}
    return [output[key] for key in sorted(output)]


def _parse_toss_candles(payload: Any) -> list[dict[str, Any]]:
    """Parse only the independently verified Toss REST-candle schema."""
    result = payload.get("result") if isinstance(payload, Mapping) else None
    candles = result.get("candles") if isinstance(result, Mapping) else None
    if not isinstance(candles, list):
        raise ValueError("TOSS_CANDLE_SCHEMA_UNVERIFIED")
    rows = []
    required = ("timestamp", "openPrice", "highPrice", "lowPrice", "closePrice", "volume", "currency")
    for candle in candles:
        if not isinstance(candle, Mapping) or not all(key in candle for key in required):
            raise ValueError("TOSS_CANDLE_SCHEMA_UNVERIFIED")
        rows.append({"timestamp": candle["timestamp"], "open": candle["openPrice"], "high": candle["highPrice"],
                     "low": candle["lowPrice"], "close": candle["closePrice"], "volume": candle["volume"]})
    return rows


class _KISReadOnlyFacade:
    """Minute-chart reads only; no account, quote, or execution surface."""

    def __init__(self) -> None:
        self._broker = KISBroker()

    def bars(self, symbol: str):
        return fetch(self._broker, symbol=symbol, exchange="NASDAQ")


def secrets_in_export(payload: Mapping[str, Any]) -> bool:
    """Structural defense-in-depth check; it does not inspect any real secret value."""
    serialized = json.dumps(payload, sort_keys=True).lower()
    return any(word in serialized for word in FORBIDDEN_TEXT)


class S6ProviderDataExporter:
    """Collects Phase-A candle data without a production write surface."""

    def __init__(self, kis: Any, toss: TossReadOnlyClient, *, candle_count: int = 120,
                 toss_min_interval_seconds: float = 0.05) -> None:
        self.kis = kis
        self.toss = toss
        self.candle_count = int(candle_count)
        if not 1 <= self.candle_count <= 200:
            raise ValueError("candle_count must be between 1 and 200")
        self.toss_min_interval_seconds = max(0.05, float(toss_min_interval_seconds))

    def collect(self, symbols: Sequence[str] = PHASE_A_SYMBOLS, *, now: datetime | None = None,
                cycles: int = 1, universe_source: str = "PHASE_A_FIXED") -> Mapping[str, Any]:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if cycles < 1:
            raise ValueError("cycles must be positive")
        errors, rate_limited = {"KIS": Counter(), "TOSS": Counter()}, 0
        normalized_symbols = [str(symbol).upper() for symbol in symbols]
        bars: dict[str, dict[str, list[dict[str, Any]]]] = {symbol: {"KIS": [], "TOSS": []} for symbol in normalized_symbols}
        records: dict[str, list[dict[str, Any]]] = {"KIS": [], "TOSS": []}
        cycle_durations: dict[str, list[float]] = {"KIS": [], "TOSS": []}
        try:
            self.toss.authenticate()
        except Exception as exc:  # retain KIS collection if Toss auth is unavailable
            errors["TOSS"][_error(exc)] += len(normalized_symbols) * cycles
            if _rate_limited(exc):
                rate_limited += 1
            toss_authenticated = False
        else:
            toss_authenticated = True
        for cycle in range(cycles):
            # Alternate provider order to avoid systematically giving one
            # provider the earlier/later position in every observation.
            order = ("KIS", "TOSS") if cycle % 2 == 0 else ("TOSS", "KIS")
            for provider in order:
                if provider == "TOSS" and not toss_authenticated:
                    # Authentication was one failed request and is already
                    # counted above.  Do not manufacture candle attempts or
                    # EMPTY_RESPONSE errors that never occurred.
                    continue
                started_cycle = time.monotonic()
                for symbol in normalized_symbols:
                    started_at = datetime.now(timezone.utc).isoformat()
                    began = time.monotonic()
                    success, count = False, 0
                    try:
                        if provider == "KIS":
                            value = _normalize_completed_bars(self.kis.bars(symbol), now=moment)
                        elif toss_authenticated:
                            response = self.toss.candles_1m(symbol, count=self.candle_count)
                            value = _normalize_completed_bars(_parse_toss_candles(response.data), now=moment)
                        else:
                            value = []
                        count, success = len(value), bool(value)
                        bars[symbol][provider] = value
                        if not success:
                            errors[provider]["EMPTY_RESPONSE"] += 1
                    except Exception as exc:
                        errors[provider][_error(exc)] += 1
                        if _rate_limited(exc):
                            rate_limited += 1
                    latency_ms = round((time.monotonic() - began) * 1000, 3)
                    records[provider].append({"symbol": symbol, "request_started_at": started_at,
                                              "latency_ms": latency_ms, "success": success, "bar_count": count,
                                              "cycle": cycle + 1})
                    if provider == "TOSS":
                        elapsed = time.monotonic() - began
                        if elapsed < self.toss_min_interval_seconds:
                            time.sleep(self.toss_min_interval_seconds - elapsed)
                cycle_durations[provider].append(time.monotonic() - started_cycle)
        performance = {provider: _summary(records[provider], cycle_durations[provider]) for provider in ("KIS", "TOSS")}
        left, right = performance["KIS"]["median_cycle_duration_ms"], performance["TOSS"]["median_cycle_duration_ms"]
        performance["relative"] = {"speed_ratio_kis_over_toss": (left / right) if left is not None and right else None}
        payload = {
            "metadata": {"generated_at": moment.isoformat(), "symbols": list(bars), "providers": ["KIS", "TOSS"],
                         "timezone_normalized": "UTC", "interval": "1m", "forming_candle_excluded": True,
                         "toss_volume_source": "REST_CANDLE", "execution_calls": 0, "cycles": cycles,
                         "universe_source": universe_source, "provider_order": "ALTERNATING"},
            "performance": performance,
            "bars": bars,
            "errors": {provider: dict(values) for provider, values in errors.items()},
            "rate_limited_429": rate_limited,
        }
        if secrets_in_export(payload):
            raise RuntimeError("SANITIZATION_FAILED")
        return payload

    def write(self, output_dir: Path = DEFAULT_OUTPUT_DIR, *, phase: str = "a", **kwargs: Any) -> Path:
        payload = self.collect(**kwargs)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = output_dir / f"s6_phase_{str(phase).lower()}_{stamp}.json"
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return target


def _result(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    counts = {provider: sum(len(item.get(provider, [])) for item in payload["bars"].values()) for provider in ("KIS", "TOSS")}
    return "\n".join(("[EXPORT_RESULT]", f"File: {path}", f"Symbols: {len(payload['bars'])}",
                      f"KIS bars: {counts['KIS']}", f"Toss bars: {counts['TOSS']}",
                      f"KIS errors: {sum(payload['errors']['KIS'].values())}", f"Toss errors: {sum(payload['errors']['TOSS'].values())}",
                      f"429: {payload['rate_limited_429']}", "Execution calls: 0",
                      f"Secrets in export: {'YES' if secrets_in_export(payload) else 'NO'}", "Production changed: NO"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Oracle S6 candle exporter")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--symbols", nargs="*", default=list(PHASE_A_SYMBOLS))
    parser.add_argument("--symbols-file", type=Path)
    parser.add_argument("--candle-count", type=int, default=120)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--phase", choices=("a", "b"), default="a")
    args = parser.parse_args(argv)
    if args.symbols_file:
        minimum = PHASE_B_MIN_SYMBOLS if args.phase == "b" else 1
        symbols = load_symbols(args.symbols_file, minimum=minimum, maximum=PHASE_B_MAX_SYMBOLS)
        source = "READ_ONLY_SYMBOL_FILE"
    else:
        symbols, source = args.symbols, "PHASE_A_FIXED"
    if args.phase == "b" and args.cycles < 3:
        parser.error("Phase B requires --cycles >= 3")
    path = S6ProviderDataExporter(_KISReadOnlyFacade(), TossReadOnlyClient(), candle_count=args.candle_count).write(
        args.output_dir, phase=args.phase, symbols=symbols, cycles=args.cycles, universe_source=source)
    print(_result(path))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the Oracle runbook
    raise SystemExit(main())
