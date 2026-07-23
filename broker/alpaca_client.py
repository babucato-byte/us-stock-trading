from dataclasses import dataclass
from typing import Optional, Union

import requests

from .broker_config import BrokerConfig, validate_order_allowed_now


@dataclass
class BrokerResponse:
    status_code: int
    text: str
    data: Optional[Union[dict, list]] = None
    dry_run: bool = False

    def json(self):
        return self.data


class AlpacaBroker:
    def __init__(self, config=None, session=None):
        self.config = config or BrokerConfig()
        self.session = session or requests.Session()

    @property
    def headers(self):
        return {
            "APCA-API-KEY-ID": self.config.api_key or "",
            "APCA-API-SECRET-KEY": self.config.secret_key or "",
            "Content-Type": "application/json",
        }

    def _validate_runtime_safety(self):
        """Validate both the captured config and the current environment."""
        self.config.validate_order_allowed()
        self.config.validate_for_request()
        validate_order_allowed_now()

    def _request(self, method, path, *, return_response=False, not_found_is_none=False, **kwargs):
        # Safety gate must run before any network access, not just before
        # order submission: without this, a misconfigured Paper mode whose
        # ALPACA_PAPER_BASE_URL was overwritten with the Live URL could still
        # reach account/position endpoints on the Live host via this method.
        self._validate_runtime_safety()
        url = f"{self.config.base_url}{path}"
        response = self.session.request(method, url, headers=self.headers, timeout=30, **kwargs)
        if not_found_is_none and response.status_code == 404:
            return None
        response.raise_for_status()
        return response if return_response else response.json()

    def get_account(self):
        return self._request("GET", "/v2/account")

    def get_positions(self):
        return self._request("GET", "/v2/positions")

    def get_recent_orders(self, limit=10):
        return self._request("GET", f"/v2/orders?status=all&limit={limit}")

    def get_assets(self):
        """List tradable assets (used to build the trading universe).

        Goes through the same _request() safety gate as every other broker
        call — CODEX-009 closed a gap where universe_builder.py built its
        own URL from ALPACA_BASE_URL/ALPACA_PAPER_BASE_URL directly and
        called requests.get() without any endpoint validation.
        """
        return self._request("GET", "/v2/assets")

    def get_order_by_client_order_id(self, client_order_id):
        """Look up a submitted order by the id we generated at reservation time.

        Returns None on a 404 (order unknown to the broker) instead of
        raising, so reconciliation can distinguish "not found" from a
        transport/auth failure, which should be retried rather than treated
        as a definitive answer.
        """
        return self._request(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
            not_found_is_none=True,
        )

    def submit_order(self, symbol, qty=1, *, side, order_type="market", time_in_force="day", client_order_id=None):
        if side not in {"buy", "sell"}:
            raise ValueError("side must be exactly 'buy' or 'sell'")

        order = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if client_order_id:
            order["client_order_id"] = client_order_id

        if self.config.is_live_mode and not self.config.can_submit_live_order:
            return BrokerResponse(
                status_code=200,
                text="LIVE_DRY_RUN: order was validated but not submitted.",
                data={"dry_run": True, "order": order, "mode": self.config.status_label},
                dry_run=True,
            )

        response = self._request("POST", "/v2/orders", json=order, return_response=True)
        return BrokerResponse(
            status_code=response.status_code,
            text=response.text,
            data=_safe_json(response),
            dry_run=False,
        )

    def cancel_order(self, order_id):
        """Cancel an order through the same runtime safety gate."""
        if not order_id or not isinstance(order_id, str):
            raise ValueError("order_id must be a non-empty string")
        return self._request("DELETE", f"/v2/orders/{order_id}")


def _safe_json(response):
    try:
        return response.json()
    except Exception:
        return None
