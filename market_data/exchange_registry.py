"""HIGH-1: resolves a symbol to its canonical venue, with provenance.

Callers must never pass a literal exchange to a KIS request. They ask
here, and get back both the venue and WHERE that answer came from, so an
operator can tell a verified listing apart from an operator override.

Source precedence (highest first), per the deployment directive:

  1. universe metadata  -- `universe.csv`, the scanner's own listing feed
  2. broker metadata    -- an exchange field observed on a KIS response
  3. operator overrides -- an explicitly approved static mapping

A symbol that no source resolves is UNKNOWN. It is never assumed to be
NASDAQ: the whole point of this module is that the previous hardcoded
default produced silently empty prices for every NYSE name.
"""

import csv
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from domain.exchange import UnsupportedExchangeError, normalize_exchange

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE_FILE = BASE_DIR / "universe.csv"

SOURCE_UNIVERSE = "universe"
SOURCE_BROKER = "broker"
SOURCE_OPERATOR = "operator_override"

REASON_EXCHANGE_UNKNOWN = "EXCHANGE_UNKNOWN"
REASON_UNSUPPORTED_EXCHANGE = "UNSUPPORTED_EXCHANGE"


class ExchangeResolutionError(Exception):
    """Raised when a symbol's venue cannot be established. Carries a
    reason_code so the caller can audit WHY without re-deriving it."""

    def __init__(self, message, *, reason_code, symbol):
        super().__init__(message)
        self.reason_code = reason_code
        self.symbol = symbol


class ExchangeRecord:
    """One resolved listing plus its provenance."""

    __slots__ = ("symbol", "exchange", "source", "verified_at")

    def __init__(self, symbol, exchange, source, verified_at):
        self.symbol = symbol
        self.exchange = exchange
        self.source = source
        self.verified_at = verified_at

    @property
    def kis_exchange_code(self):
        from domain.exchange import to_kis_exchange_code

        return to_kis_exchange_code(self.exchange)

    @property
    def kis_order_exchange_code(self):
        from domain.exchange import to_kis_order_exchange_code

        return to_kis_order_exchange_code(self.exchange)

    def as_dict(self):
        """Audit-safe: symbol and venue only, never a price or account."""
        return {
            "symbol": self.symbol,
            "canonical_exchange": self.exchange.value,
            "kis_exchange_code": self.kis_exchange_code,
            "source": self.source,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }

    def __repr__(self):  # pragma: no cover -- diagnostics only
        return f"ExchangeRecord({self.symbol}, {self.exchange.value}, {self.source})"


def _universe_path():
    override = os.environ.get("UNIVERSE_FILE", "").strip()
    return Path(override) if override else DEFAULT_UNIVERSE_FILE


def _operator_overrides():
    """`KIS_EXCHANGE_OVERRIDES=SYM:NYSE,OTHER:NASDAQ` -- a deliberate,
    reviewed mapping for names the universe feed does not carry. An
    unparseable or unsupported entry is dropped rather than guessed at;
    the symbol then falls through to UNKNOWN."""
    raw = os.environ.get("KIS_EXCHANGE_OVERRIDES", "").strip()
    if not raw:
        return {}
    overrides = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        symbol, _, exchange = chunk.partition(":")
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        try:
            overrides[symbol] = normalize_exchange(exchange)
        except UnsupportedExchangeError:
            continue
    return overrides


class ExchangeRegistry:
    """Thread-safe, lazily loaded. One instance is shared per process via
    the module-level accessors below."""

    def __init__(self, *, universe_file=None, now_fn=None):
        self._universe_file = universe_file
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._universe = None
        self._broker = {}

    # -- loading ---------------------------------------------------------

    def _load_universe(self):
        path = Path(self._universe_file) if self._universe_file else _universe_path()
        listings = {}
        try:
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or "symbol" not in reader.fieldnames:
                    return listings
                has_exchange = "exchange" in reader.fieldnames
                for row in reader:
                    symbol = (row.get("symbol") or "").strip().upper()
                    if not symbol or not has_exchange:
                        continue
                    try:
                        listings[symbol] = normalize_exchange(row.get("exchange"))
                    except UnsupportedExchangeError:
                        # ARCA / BATS / OTC and malformed rows are recorded
                        # as "known to the feed but not tradable here" by
                        # simply not being listed. Resolution then reports
                        # UNSUPPORTED via the raw lookup below.
                        continue
        except OSError:
            return listings
        return listings

    def _universe_map(self):
        if self._universe is None:
            with self._lock:
                if self._universe is None:
                    self._universe = self._load_universe()
        return self._universe

    def _raw_universe_exchange(self, symbol):
        """The feed's own text for a symbol, even when unsupported -- used
        only to tell UNSUPPORTED_EXCHANGE apart from EXCHANGE_UNKNOWN."""
        path = Path(self._universe_file) if self._universe_file else _universe_path()
        try:
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or "exchange" not in (reader.fieldnames or []):
                    return None
                for row in reader:
                    if (row.get("symbol") or "").strip().upper() == symbol:
                        return (row.get("exchange") or "").strip()
        except OSError:
            return None
        return None

    # -- broker-observed metadata ---------------------------------------

    def record_broker_exchange(self, symbol, exchange):
        """Records an exchange observed on a real KIS response. Ignored
        when unsupported -- an unusable observation must not displace a
        good universe listing."""
        key = (symbol or "").strip().upper()
        if not key:
            return False
        try:
            canonical = normalize_exchange(exchange)
        except UnsupportedExchangeError:
            return False
        with self._lock:
            self._broker[key] = (canonical, self._now())
        return True

    # -- resolution ------------------------------------------------------

    def resolve(self, symbol):
        """Returns an ExchangeRecord or raises ExchangeResolutionError."""
        key = (symbol or "").strip().upper()
        if not key:
            raise ExchangeResolutionError(
                "cannot resolve an empty symbol",
                reason_code=REASON_EXCHANGE_UNKNOWN, symbol=symbol,
            )

        listed = self._universe_map().get(key)
        if listed is not None:
            return ExchangeRecord(key, listed, SOURCE_UNIVERSE, self._now())

        observed = self._broker.get(key)
        if observed is not None:
            return ExchangeRecord(key, observed[0], SOURCE_BROKER, observed[1])

        override = _operator_overrides().get(key)
        if override is not None:
            return ExchangeRecord(key, override, SOURCE_OPERATOR, self._now())

        raw = self._raw_universe_exchange(key)
        if raw:
            raise ExchangeResolutionError(
                f"{key} is listed on {raw!r}, which this system does not trade",
                reason_code=REASON_UNSUPPORTED_EXCHANGE, symbol=key,
            )
        raise ExchangeResolutionError(
            f"no exchange metadata for {key}",
            reason_code=REASON_EXCHANGE_UNKNOWN, symbol=key,
        )

    def try_resolve(self, symbol):
        """Returns (record, None) or (None, ExchangeResolutionError)."""
        try:
            return self.resolve(symbol), None
        except ExchangeResolutionError as exc:
            return None, exc


_REGISTRY = None
_REGISTRY_LOCK = threading.Lock()


def get_registry():
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = ExchangeRegistry()
    return _REGISTRY


def reset_registry():
    """Test/diagnostic hook -- drops the cached universe so a changed
    UNIVERSE_FILE or override set is picked up."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def resolve_exchange(symbol):
    return get_registry().resolve(symbol)


class ExcludedSymbol:
    """A candidate the KIS pipeline must not be handed, and why.

    Kept as a record rather than dropped silently: the symbol stays in
    the analysis output, and an operator has to be able to see that it
    was analysed and then held back, not that it never appeared.
    """

    __slots__ = ("symbol", "reason_code", "detail")

    def __init__(self, symbol, reason_code, detail):
        self.symbol = symbol
        self.reason_code = reason_code
        self.detail = detail

    def as_dict(self):
        """Audit-safe: symbol, reason and venue text only."""
        return {"symbol": self.symbol, "reason_code": self.reason_code,
                "detail": self.detail}

    def __repr__(self):  # pragma: no cover -- diagnostics only
        return f"ExcludedSymbol({self.symbol}, {self.reason_code})"


def partition_kis_executable(symbols, *, registry=None):
    """Splits analysis candidates into the ones the KIS pipeline may
    evaluate and the ones it must not receive at all.

    Oracle verification produced a day whose only candidate was IXN, an
    ARCA listing. The KIS order exchange code space is NASD / NYSE /
    AMEX; ARCA has no code there, so the evaluation could only ever end
    in UNSUPPORTED_EXCHANGE -- after spending a scored analysis pass on
    it. The analysis side is deliberately broader than the executable
    side, so the split belongs here, in front of the KIS pipeline,
    rather than as an exception thrown from inside it.

    Returns (executable, excluded):
        executable -- [(symbol, ExchangeRecord)], venue already resolved
                      so the caller does not resolve it a second time
        excluded   -- [ExcludedSymbol], in the order the symbols arrived

    Nothing is removed from any analysis artefact by this function; it
    only decides what the KIS pipeline is handed.
    """
    resolver = registry or get_registry()
    executable, excluded = [], []
    for raw in symbols:
        symbol = (raw or "").strip().upper()
        if not symbol:
            continue
        record, error = resolver.try_resolve(symbol)
        if error is not None:
            excluded.append(ExcludedSymbol(symbol, error.reason_code, str(error)))
            continue
        executable.append((symbol, record))
    return executable, excluded


def supported_analysis_exchanges():
    """The venue names a candidate may carry and still be executable --
    for operator-facing messages and documentation."""
    from domain.exchange import USExchange

    return tuple(exchange.value for exchange in USExchange)


def build_kis_instrument(symbol, *, registry=None):
    """The ONLY sanctioned way to build an Instrument for a KIS call.

    Replaces every `build_instrument(symbol, exchange="NASDAQ")` -- the
    literal that made NYSE names unpriceable.
    """
    from domain.instrument import build_instrument

    record = (registry or get_registry()).resolve(symbol)
    return build_instrument(symbol, exchange=record.exchange.value), record
