"""Oracle-side, read-only Phase-A OHLCV exporter for offline S6 parity.

The exporter deliberately performs no S6 calculation.  It emits only a
whitelisted market-data schema which can be copied off Oracle and evaluated
locally.  Toss is called through its fail-closed read-only client and only its
REST candle endpoint is used; no account, quote, websocket, or order API is
needed here.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from validation.broker_shadow_compare import _KISReadOnlyFacade, parse_toss_candles
from validation.s6_provider_parity import PHASE_A_SYMBOLS, normalize_bars
from validation.toss_openapi.client import TossReadOnlyClient


DEFAULT_OUTPUT_DIR = Path("/tmp/s6-provider-export")
FORBIDDEN_TEXT = ("token", "secret", "account", "authorization", "cookie", "header", "app_key", "client_secret")


def _error(exc: BaseException) -> str:
    """Never include a network response, URL, request, or exception message."""
    return type(exc).__name__


def _rate_limited(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == 429 or getattr(exc, "status", None) == 429 or "429" in str(exc)


def _bar_records(rows: Sequence[Mapping[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """The only serialized records: UTC completed OHLCV bars."""
    return [{"timestamp": bar.timestamp.isoformat(), "open": bar.open, "high": bar.high,
             "low": bar.low, "close": bar.close, "volume": bar.volume}
            for bar in normalize_bars(rows, now=now)]


def secrets_in_export(payload: Mapping[str, Any]) -> bool:
    """Structural defense-in-depth check; it does not inspect any real secret value."""
    serialized = json.dumps(payload, sort_keys=True).lower()
    return any(word in serialized for word in FORBIDDEN_TEXT)


class S6ProviderDataExporter:
    """Collects Phase-A candle data without a production write surface."""

    def __init__(self, kis: Any, toss: TossReadOnlyClient, *, candle_count: int = 120) -> None:
        self.kis = kis
        self.toss = toss
        self.candle_count = int(candle_count)
        if not 1 <= self.candle_count <= 200:
            raise ValueError("candle_count must be between 1 and 200")

    def collect(self, symbols: Sequence[str] = PHASE_A_SYMBOLS, *, now: datetime | None = None) -> Mapping[str, Any]:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        errors, rate_limited = {"KIS": Counter(), "TOSS": Counter()}, 0
        bars: dict[str, dict[str, list[dict[str, Any]]]] = {}
        try:
            self.toss.authenticate()
        except Exception as exc:  # retain KIS collection if Toss auth is unavailable
            errors["TOSS"][_error(exc)] += len(symbols)
            if _rate_limited(exc):
                rate_limited += 1
            toss_authenticated = False
        else:
            toss_authenticated = True
        for raw_symbol in symbols:
            symbol = str(raw_symbol).upper()
            item = {"KIS": [], "TOSS": []}
            try:
                item["KIS"] = _bar_records(self.kis.bars(symbol), now=moment)
                if not item["KIS"]:
                    errors["KIS"]["EMPTY_RESPONSE"] += 1
            except Exception as exc:
                errors["KIS"][_error(exc)] += 1
                if _rate_limited(exc):
                    rate_limited += 1
            if toss_authenticated:
                try:
                    response = self.toss.candles_1m(symbol, count=self.candle_count)
                    item["TOSS"] = _bar_records(parse_toss_candles(response.data), now=moment)
                    if not item["TOSS"]:
                        errors["TOSS"]["EMPTY_RESPONSE"] += 1
                except Exception as exc:
                    errors["TOSS"][_error(exc)] += 1
                    if _rate_limited(exc):
                        rate_limited += 1
            bars[symbol] = item
        payload = {
            "metadata": {"generated_at": moment.isoformat(), "symbols": list(bars), "providers": ["KIS", "TOSS"],
                         "timezone_normalized": "UTC", "interval": "1m", "forming_candle_excluded": True,
                         "toss_volume_source": "REST_CANDLE", "execution_calls": 0},
            "bars": bars,
            "errors": {provider: dict(values) for provider, values in errors.items()},
            "rate_limited_429": rate_limited,
        }
        if secrets_in_export(payload):
            raise RuntimeError("SANITIZATION_FAILED")
        return payload

    def write(self, output_dir: Path = DEFAULT_OUTPUT_DIR, **kwargs: Any) -> Path:
        payload = self.collect(**kwargs)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = output_dir / f"s6_phase_a_{stamp}.json"
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
    parser.add_argument("--candle-count", type=int, default=120)
    args = parser.parse_args(argv)
    path = S6ProviderDataExporter(_KISReadOnlyFacade(), TossReadOnlyClient(), candle_count=args.candle_count).write(
        args.output_dir, symbols=args.symbols)
    print(_result(path))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the Oracle runbook
    raise SystemExit(main())
