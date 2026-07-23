import hmac
from dataclasses import dataclass
from typing import Optional, Union

import requests

import kill_switch
import kill_switch_state

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
        """Validate both the captured config and the current environment.

        Credential revalidation runs first: it wraps its own
        BrokerConfig.from_env() call in a try/except (see
        _validate_current_credentials_match_captured), whereas
        validate_order_allowed_now() below does not -- running it first
        means an environment-read failure is always converted into a
        RuntimeError here rather than propagating as a raw OSError/etc. from
        deeper in the gate.
        """
        self.config.validate_order_allowed()
        self.config.validate_for_request()
        self._validate_current_credentials_match_captured()
        validate_order_allowed_now()

    def _validate_current_credentials_match_captured(self):
        """CODEX-018: re-read process credentials fresh on every request and
        require them to still exactly match what self.config captured at
        construction time.

        Without this, deleting or rotating ALPACA_API_KEY/ALPACA_SECRET_KEY
        after an AlpacaBroker instance already exists had no effect on that
        instance -- self.config is a frozen snapshot from construction time,
        so every subsequent call kept sending the original credentials
        regardless of what the environment now holds. Credential rotation
        must go through building a new BrokerConfig/AlpacaBroker; this gate
        never auto-recaptures the new value, it only blocks.

        Never include the actual key/secret text in any exception message --
        only whether they are present/blank/matching.
        """
        try:
            current = BrokerConfig.from_env()
        except Exception:
            raise RuntimeError(
                "Credential revalidation failed: could not read current environment credentials."
            ) from None

        current_api_key = current.api_key
        current_secret_key = current.secret_key

        if not current_api_key or not current_api_key.strip():
            raise RuntimeError("Credential revalidation failed: current API key is missing or blank.")
        if not current_secret_key or not current_secret_key.strip():
            raise RuntimeError("Credential revalidation failed: current secret key is missing or blank.")

        captured_api_key = self.config.api_key or ""
        captured_secret_key = self.config.secret_key or ""

        api_key_matches = hmac.compare_digest(current_api_key, captured_api_key)
        secret_key_matches = hmac.compare_digest(current_secret_key, captured_secret_key)

        if not api_key_matches or not secret_key_matches:
            raise RuntimeError(
                "Credential revalidation failed: current environment credentials no longer match "
                "the credentials captured when this broker instance was constructed. Build a new "
                "BrokerConfig/AlpacaBroker instead of rotating credentials under an existing instance."
            )

    def _check_kill_switch(self, order_side):
        """Gate order-affecting requests on both kill switch mechanisms,
        re-read fresh on every call (never cached on self).

        order_side is None for every non-order endpoint (get_account,
        get_positions, get_recent_orders, get_assets,
        get_order_by_client_order_id, cancel_order) and is skipped entirely
        for them -- those must keep working regardless of kill switch state.
        Only submit_order() passes "buy" or "sell", since only it initiates
        a new entry or liquidation.
        """
        if order_side is None:
            return
        if order_side not in {"buy", "sell"}:
            raise ValueError(f"order_side must be 'buy', 'sell', or None, got {order_side!r}")

        if kill_switch.is_trading_halted():
            raise RuntimeError("Kill switch engaged: trading halted, order not submitted.")

        state_allows = (
            kill_switch_state.is_entry_allowed()
            if order_side == "buy"
            else kill_switch_state.is_liquidation_allowed()
        )
        if not state_allows:
            raise RuntimeError(
                f"Kill switch state engaged: {order_side} orders not permitted, order not submitted."
            )

    def _request(self, method, path, *, order_side, return_response=False, not_found_is_none=False, **kwargs):
        # Safety gates must run before any network access, not just before
        # order submission: without this, a misconfigured Paper mode whose
        # ALPACA_PAPER_BASE_URL was overwritten with the Live URL could still
        # reach account/position endpoints on the Live host via this method,
        # and a kill switch engaged after construction could be bypassed by
        # calling AlpacaBroker.submit_order() directly instead of through the
        # paper_strategy_order.submit_order() wrapper.
        #
        # order_side has no default on purpose: every call site (inside this
        # class or a direct caller bypassing it) must state its intent
        # explicitly. A caller reaching this method without naming
        # order_side -- e.g. broker._request("POST", "/v2/orders", json=...)
        # -- gets a TypeError before the session is ever touched, instead of
        # silently skipping the kill switch check.
        self._validate_runtime_safety()
        self._check_kill_switch(order_side)
        url = f"{self.config.base_url}{path}"
        response = self.session.request(method, url, headers=self.headers, timeout=30, **kwargs)
        if not_found_is_none and response.status_code == 404:
            return None
        response.raise_for_status()
        return response if return_response else response.json()

    def get_account(self):
        return self._request("GET", "/v2/account", order_side=None)

    def get_positions(self):
        return self._request("GET", "/v2/positions", order_side=None)

    def get_recent_orders(self, limit=10):
        return self._request("GET", f"/v2/orders?status=all&limit={limit}", order_side=None)

    def get_assets(self):
        """List tradable assets (used to build the trading universe).

        Goes through the same _request() safety gate as every other broker
        call — CODEX-009 closed a gap where universe_builder.py built its
        own URL from ALPACA_BASE_URL/ALPACA_PAPER_BASE_URL directly and
        called requests.get() without any endpoint validation.
        """
        return self._request("GET", "/v2/assets", order_side=None)

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
            order_side=None,
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

        response = self._request("POST", "/v2/orders", order_side=side, json=order, return_response=True)
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
        return self._request("DELETE", f"/v2/orders/{order_id}", order_side=None)


def _safe_json(response):
    try:
        return response.json()
    except Exception:
        return None
