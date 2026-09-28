"""Read-only Toss shadow adapter; intentionally not wired into production.

`trade:us` is lossy and is NOT SAFE AS SOLE SOURCE FOR AGGREGATED VOLUME.
This adapter consequently does not reconstruct volume, build bars, or derive
cumulative volume from WebSocket trade frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from validation.toss_openapi.client import (
    RequestResult,
    TossReadOnlyClient,
    ValidationError,
    mask_identifier,
)
from validation.toss_openapi.websocket_client import (
    ALLOWED_TOPICS,
    TossWebSocketClient,
    WebSocketResult,
)


TOSS_SHADOW_EXECUTION_BLOCKED = "TOSS_SHADOW_EXECUTION_BLOCKED"
ALLOWED_WEBSOCKET_TOPICS = ALLOWED_TOPICS


class TossShadowAdapterError(RuntimeError):
    """Safe read-only adapter failure."""


class TossShadowExecutionBlocked(RuntimeError):
    """Raised before any attempt to execute, cancel, or modify an order."""


@dataclass(frozen=True)
class TossReadResult:
    """A response summary with a recursively redacted opaque payload."""

    status: int
    latency_ms: int
    data: Any


@dataclass(frozen=True)
class TossAccountSnapshot:
    account_seq_masked: str
    account_number_masked: str
    account_type: str | None


def _payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in ("result", "data", "accounts"):
            if key in value:
                return value[key]
    return value


def _redact(value: Any, key: str = "") -> Any:
    """Do not expose account/token/credential values through opaque results."""
    sensitive = ("account", "token", "secret", "authorization", "credential")
    if any(part in key.lower() for part in sensitive):
        return "REDACTED"
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


class TossShadowAdapter:
    """Read-only Toss account/market-data observer with execution impossible by construction."""

    __slots__ = ("__client", "__websocket_factory", "__account_seq")
    _MODE = "SHADOW_READ_ONLY"

    def __init__(
        self,
        *,
        client: TossReadOnlyClient | None = None,
        websocket_factory: Callable[[str], TossWebSocketClient] = TossWebSocketClient,
    ) -> None:
        self.__client = client or TossReadOnlyClient()
        self.__websocket_factory = websocket_factory
        self.__account_seq: object | None = None

    @property
    def mode(self) -> str:
        return self._MODE

    @property
    def execution_enabled(self) -> bool:
        return False

    @staticmethod
    def _read(result: RequestResult) -> TossReadResult:
        return TossReadResult(result.status, result.latency_ms, _redact(result.data))

    def authenticate(self) -> TossReadResult:
        """Obtain a transport token without returning it."""
        return self._read(self.__client.authenticate())

    def _ensure_authenticated(self) -> None:
        if not self.__client.access_token:
            self.authenticate()

    def discover_account(self) -> TossAccountSnapshot:
        self._ensure_authenticated()
        result = self.__client.accounts()
        accounts = _payload(result.data)
        if not isinstance(accounts, list) or not accounts or not isinstance(accounts[0], Mapping):
            raise TossShadowAdapterError("TOSS_SHADOW_ACCOUNT_NOT_FOUND")
        account = accounts[0]
        account_seq = account.get("accountSeq")
        if account_seq is None:
            raise TossShadowAdapterError("TOSS_SHADOW_ACCOUNT_SEQUENCE_NOT_FOUND")
        self.__account_seq = account_seq
        return TossAccountSnapshot(
            account_seq_masked=mask_identifier(account_seq),
            account_number_masked=mask_identifier(account.get("accountNo")),
            account_type=str(account["accountType"]) if account.get("accountType") is not None else None,
        )

    def _ensure_account(self) -> object:
        if self.__account_seq is None:
            self.discover_account()
        if self.__account_seq is None:  # defensive: do not make an account call without discovery
            raise TossShadowAdapterError("TOSS_SHADOW_ACCOUNT_SEQUENCE_NOT_FOUND")
        return self.__account_seq

    def get_buying_power(self) -> TossReadResult:
        return self._read(self.__client.buying_power(self._ensure_account()))

    def get_commissions(self) -> TossReadResult:
        return self._read(self.__client.commissions(self._ensure_account()))

    def get_prices(self, symbols: Sequence[str]) -> TossReadResult:
        # The underlying validated transport fails closed for empty and >200 batches.
        return self._read(self.__client.prices(list(symbols)))

    def get_1m_candles(self, symbol: str) -> TossReadResult:
        return self._read(self.__client.candles_1m(symbol))

    def _subscribe(self, topic: str, codes: Sequence[str]) -> WebSocketResult:
        if topic not in ALLOWED_WEBSOCKET_TOPICS:
            raise TossShadowAdapterError(TOSS_SHADOW_EXECUTION_BLOCKED)
        self._ensure_authenticated()
        token = self.__client.access_token
        if not token:
            raise TossShadowAdapterError("TOSS_SHADOW_AUTHENTICATION_REQUIRED")
        return self.__websocket_factory(token).subscribe([{"type": topic, "codes": list(codes)}])

    def subscribe_trades(self, symbols: Sequence[str]) -> WebSocketResult:
        return self._subscribe("trade:us", symbols)

    def subscribe_orderbook(self, symbols: Sequence[str]) -> WebSocketResult:
        return self._subscribe("orderbook:us", symbols)

    def subscribe_personal_orders(self) -> WebSocketResult:
        return self._subscribe("personal:order", [str(self._ensure_account())])

    @staticmethod
    def _execution_blocked(*_args: Any, **_kwargs: Any) -> None:
        raise TossShadowExecutionBlocked(TOSS_SHADOW_EXECUTION_BLOCKED)

    def submit_order(self, *_args: Any, **_kwargs: Any) -> None:
        self._execution_blocked(*_args, **_kwargs)

    def cancel_order(self, *_args: Any, **_kwargs: Any) -> None:
        self._execution_blocked(*_args, **_kwargs)

    def modify_order(self, *_args: Any, **_kwargs: Any) -> None:
        self._execution_blocked(*_args, **_kwargs)
