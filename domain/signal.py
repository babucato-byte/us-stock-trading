"""`Signal` -- what the Strategy Engine produces. Never a quantity, never
a broker order -- only an entry/exit opinion at a point in time, with an
explicit expiry so a stale Alpaca-derived signal can never be replayed
against a live KIS order hours later (spec §13: "다음 거래일 신호 재사용
금지").
"""

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


class SignalError(Exception):
    """Raised when a Signal cannot be safely constructed or has expired.
    Callers must treat this as a hard block on the entry it would have
    produced."""


def _is_finite_positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class Signal:
    signal_id: str
    strategy_id: str
    strategy_version: str
    config_version: str
    code_commit: str
    symbol: str
    exchange: str
    created_at: datetime
    expires_at: datetime
    signal_price: float
    score: float
    entry_reason: str
    stop_price: Optional[float] = None
    target_price: Optional[float] = None

    def __post_init__(self):
        if not isinstance(self.signal_id, str) or not self.signal_id:
            raise SignalError("signal_id must be a non-empty string")
        for field_name in ("strategy_id", "strategy_version", "config_version",
                           "code_commit", "symbol", "exchange", "entry_reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SignalError(f"{field_name} must be a non-empty string, got {value!r}")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise SignalError("created_at must be a timezone-aware datetime")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise SignalError("expires_at must be a timezone-aware datetime")
        if self.expires_at <= self.created_at:
            raise SignalError("expires_at must be after created_at")
        if not _is_finite_positive(self.signal_price):
            raise SignalError(f"signal_price must be a positive finite number, got {self.signal_price!r}")
        if not isinstance(self.score, (int, float)) or isinstance(self.score, bool) \
                or not math.isfinite(self.score):
            raise SignalError(f"score must be a finite number, got {self.score!r}")
        if self.stop_price is not None and not _is_finite_positive(self.stop_price):
            raise SignalError(f"stop_price must be a positive finite number or None, got {self.stop_price!r}")
        if self.target_price is not None and not _is_finite_positive(self.target_price):
            raise SignalError(f"target_price must be a positive finite number or None, got {self.target_price!r}")

    def is_expired(self, *, now=None):
        current = now or datetime.now(timezone.utc)
        return current >= self.expires_at


def build_signal(
    *, strategy_id, strategy_version, config_version, code_commit, symbol, exchange,
    signal_price, score, entry_reason, valid_for_seconds, stop_price=None, target_price=None,
    now=None, signal_id=None,
):
    """`valid_for_seconds` is required (no default) -- every caller must
    explicitly decide the strategy's own signal lifetime rather than
    inherit an arbitrary global default (spec §13's "전략 주기에 맞게
    명시")."""
    if not _is_finite_positive(valid_for_seconds):
        raise SignalError(f"valid_for_seconds must be a positive finite number, got {valid_for_seconds!r}")
    current = now or datetime.now(timezone.utc)
    return Signal(
        signal_id=signal_id or f"sig-{uuid.uuid4().hex[:16]}",
        strategy_id=strategy_id, strategy_version=strategy_version,
        config_version=config_version, code_commit=code_commit, symbol=symbol,
        exchange=exchange, created_at=current,
        expires_at=datetime.fromtimestamp(current.timestamp() + valid_for_seconds, tz=timezone.utc),
        signal_price=signal_price, score=score, entry_reason=entry_reason,
        stop_price=stop_price, target_price=target_price,
    )
