"""Fail-closed, read-only REST client for the Toss Open API."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE = "https://openapi.tossinvest.com"
BLOCKED_MUTATING_REQUEST = "BLOCKED_MUTATING_REQUEST"
ALLOWED_REQUESTS = frozenset(
    {
        ("POST", "/oauth2/token"),  # OAuth client-credentials issuance only.
        ("GET", "/api/v1/accounts"),
        ("GET", "/api/v1/buying-power"),
        ("GET", "/api/v1/commissions"),
        ("GET", "/api/v1/prices"),
        ("GET", "/api/v1/candles"),
    }
)


class ValidationError(RuntimeError):
    """A safe, non-sensitive validation failure."""


class MissingCredentialError(ValidationError):
    pass


@dataclass(frozen=True)
class RequestResult:
    status: int
    latency_ms: int
    data: Any


def mask_identifier(value: object) -> str:
    """Mask account identifiers while retaining a little diagnostic utility."""
    text = str(value or "")
    if not text:
        return "<missing>"
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def safe_error_message(exc: BaseException) -> str:
    """Return a fixed error summary; never serialize response/request headers."""
    if isinstance(exc, HTTPError):
        return "HTTP_ERROR status=%s" % exc.code
    if isinstance(exc, URLError):
        return "NETWORK_ERROR"
    if isinstance(exc, MissingCredentialError):
        return str(exc)
    if isinstance(exc, ValidationError):
        # These strings are generated locally and contain no transport detail.
        message = str(exc)
        if message.startswith((
            "HTTP_ERROR status=", "NETWORK_ERROR", "AUTHENTICATION_REQUIRED",
            "TOKEN_MISSING_FROM_RESPONSE", "ACCOUNT_SEQUENCE_NOT_FOUND",
            "INVALID_JSON_RESPONSE", "INVALID_SYMBOL_BATCH_SIZE",
            "WEBSOCKET_", BLOCKED_MUTATING_REQUEST,
        )):
            return message
    return "%s" % type(exc).__name__


class TossReadOnlyClient:
    """A minimal client whose request gate permits no account mutations."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.client_id = client_id if client_id is not None else os.getenv("TOSS_CLIENT_ID")
        self.client_secret = (
            client_secret if client_secret is not None else os.getenv("TOSS_CLIENT_SECRET")
        )
        self.timeout = timeout
        self.access_token: str | None = None

    def _require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise MissingCredentialError("MISSING_TOSS_CREDENTIALS")

    @staticmethod
    def _authorize(method: str, path: str) -> None:
        if (method.upper(), path) not in ALLOWED_REQUESTS:
            raise ValidationError(BLOCKED_MUTATING_REQUEST)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        form: Mapping[str, str] | None = None,
        account_seq: object | None = None,
    ) -> RequestResult:
        method = method.upper()
        self._authorize(method, path)
        url = API_BASE + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        headers = {"Accept": "application/json"}
        body = None
        if form is not None:
            body = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif not self.access_token:
            raise ValidationError("AUTHENTICATION_REQUIRED")
        else:
            headers["Authorization"] = "Bearer " + self.access_token
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)
        started = time.monotonic()
        try:
            request = Request(url, data=body, headers=headers, method=method)
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310: fixed HTTPS host
                raw = response.read()
                status = response.status
        except (HTTPError, URLError) as exc:
            # Deliberately discard HTTP body and headers: either could contain sensitive data.
            raise ValidationError(safe_error_message(exc)) from None
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("INVALID_JSON_RESPONSE") from None
        return RequestResult(status=status, latency_ms=latency_ms, data=data)

    def authenticate(self) -> RequestResult:
        self._require_credentials()
        result = self._request(
            "POST",
            "/oauth2/token",
            form={
                "grant_type": "client_credentials",
                "client_id": self.client_id or "",
                "client_secret": self.client_secret or "",
            },
        )
        token = result.data.get("access_token") if isinstance(result.data, dict) else None
        if not isinstance(token, str) or not token:
            raise ValidationError("TOKEN_MISSING_FROM_RESPONSE")
        self.access_token = token
        return result

    def accounts(self) -> RequestResult:
        return self._request("GET", "/api/v1/accounts")

    def buying_power(self, account_seq: object, currency: str = "USD") -> RequestResult:
        return self._request(
            "GET", "/api/v1/buying-power", params={"currency": currency}, account_seq=account_seq
        )

    def commissions(self, account_seq: object) -> RequestResult:
        return self._request("GET", "/api/v1/commissions", account_seq=account_seq)

    def prices(self, symbols: list[str]) -> RequestResult:
        if not symbols or len(symbols) > 200:
            raise ValidationError("INVALID_SYMBOL_BATCH_SIZE")
        return self._request("GET", "/api/v1/prices", params={"symbols": ",".join(symbols)})

    def candles_1m(self, symbol: str = "AAPL", count: int = 5) -> RequestResult:
        return self._request(
            "GET",
            "/api/v1/candles",
            params={"symbol": symbol, "interval": "1m", "count": count},
        )
