import enum
import hmac
from dataclasses import dataclass
from typing import Optional, Union

import requests

import kill_switch
import kill_switch_state

from .broker_config import BrokerConfig, validate_order_allowed_now


class RequestPurpose(enum.Enum):
    """Explicit intent classification for every AlpacaBroker._request() call.

    CODEX-021: order_side alone was not a sufficient gate -- a caller could
    reach _request() with order_side=None (the value every non-order
    endpoint legitimately uses) while still POSTing to /v2/orders, which
    would skip the kill switch check entirely. purpose is now the primary
    signal _request() gates on; order_side may still be passed alongside it
    for a secondary sanity check, but it never stands alone.
    """

    READ_ONLY = "read_only"
    ENTRY_ORDER = "entry_order"
    EXIT_ORDER = "exit_order"
    CANCEL_ORDER = "cancel_order"
    RECONCILIATION = "reconciliation"


# Which purposes each HTTP method is allowed to carry. Checked before every
# session.request() call so a mismatched (method, purpose) pair -- e.g. a
# GET claiming ENTRY_ORDER, or a POST claiming READ_ONLY -- is rejected
# instead of silently reaching the network.
_METHOD_PURPOSES = {
    "GET": frozenset({RequestPurpose.READ_ONLY, RequestPurpose.RECONCILIATION}),
    "POST": frozenset({RequestPurpose.ENTRY_ORDER, RequestPurpose.EXIT_ORDER}),
    "DELETE": frozenset({RequestPurpose.CANCEL_ORDER}),
}

# Long-only v1.0: buy always opens a new position (entry), sell always
# closes/reduces one (exit). There is no short-selling path, so this mapping
# is exhaustive for the two sides submit_order() accepts.
_SIDE_TO_PURPOSE = {
    "buy": RequestPurpose.ENTRY_ORDER,
    "sell": RequestPurpose.EXIT_ORDER,
}

# The side each order-shaped purpose is required to carry, both as
# order_side and as the outgoing json payload's "side" field. Used
# exclusively by validate_order_intent() below.
_PURPOSE_REQUIRED_SIDE = {
    RequestPurpose.ENTRY_ORDER: "buy",
    RequestPurpose.EXIT_ORDER: "sell",
}


def validate_order_intent(purpose, order_side, payload):
    """The single centralized 3-way consistency gate: purpose x order_side x
    payload["side"] (CODEX-022, closing the CODEX-021 remainder).

    Every other gate in _request() validates purpose against the HTTP
    method, and order_side against {"buy", "sell", None}, each in
    isolation -- but nothing compared them against each other or against
    the outgoing JSON body itself. Before this function existed, a caller
    could pass purpose=EXIT_ORDER, order_side="sell" while the JSON payload
    still said {"side": "buy"} (or omitted "side" entirely, or spelled it
    "BUY" / " buy" / "sell " / True / 1) and every prior gate would wave it
    through to self.session.request() unexamined.

    This is the only place in the codebase that performs this comparison;
    no other call site -- including submit_order()'s own defense-in-depth
    check -- should reimplement it. _request() calls this before
    self.session.request() is ever reached, and before _check_kill_switch()
    so a mismatch never has a side effect beyond raising.
    """
    required_side = _PURPOSE_REQUIRED_SIDE.get(purpose)

    if required_side is not None:
        # ENTRY_ORDER / EXIT_ORDER: order_side and the payload's "side" must
        # both be present and must both equal the one side this purpose
        # permits.
        if order_side is None:
            raise ValueError(f"order_side must not be None for purpose {purpose.name}")
        if not isinstance(payload, dict):
            raise ValueError(
                f"purpose {purpose.name} requires a dict json payload, got {type(payload).__name__}"
            )
        if "side" not in payload:
            raise ValueError(f"purpose {purpose.name} requires json payload to contain 'side'")

        payload_side = payload["side"]
        # isinstance(..., str) also rejects bool/int (True/1 are not str
        # instances), and the exact-literal comparison rejects case or
        # whitespace variants such as "BUY", " buy", "sell ".
        if not isinstance(payload_side, str) or payload_side not in ("buy", "sell"):
            raise ValueError(
                f"json payload 'side' must be exactly 'buy' or 'sell', got {payload_side!r}"
            )

        if order_side != required_side or payload_side != required_side:
            raise ValueError(
                f"purpose/order_side/payload mismatch for {purpose.name}: "
                f"order_side={order_side!r}, payload side={payload_side!r}, required={required_side!r}"
            )
    else:
        # READ_ONLY / RECONCILIATION / CANCEL_ORDER: no order-side signal is
        # permitted anywhere -- neither as order_side nor inside a json body.
        if order_side is not None:
            raise ValueError(f"order_side must be None for purpose {purpose.name}, got {order_side!r}")
        if isinstance(payload, dict) and "side" in payload:
            raise ValueError(f"json payload must not contain 'side' for purpose {purpose.name}")


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

    def _check_kill_switch(self, purpose, order_side=None):
        """Gate order-affecting requests on both kill switch mechanisms,
        re-read fresh on every call (never cached on self).

        Only ENTRY_ORDER/EXIT_ORDER purposes are checked here -- READ_ONLY,
        RECONCILIATION, and CANCEL_ORDER keep working regardless of kill
        switch state (queries and cancellation stay unlimited by design).
        order_side, if given, is a secondary sanity check only; the binding
        decision is always purpose, never order_side alone.
        """
        if purpose not in (RequestPurpose.ENTRY_ORDER, RequestPurpose.EXIT_ORDER):
            return
        if order_side is not None and order_side not in {"buy", "sell"}:
            raise ValueError(f"order_side must be 'buy', 'sell', or None, got {order_side!r}")

        if kill_switch.is_trading_halted():
            raise RuntimeError("Kill switch engaged: trading halted, order not submitted.")

        state_allows = (
            kill_switch_state.is_entry_allowed()
            if purpose == RequestPurpose.ENTRY_ORDER
            else kill_switch_state.is_liquidation_allowed()
        )
        if not state_allows:
            raise RuntimeError(
                f"Kill switch state engaged: {purpose.name} not permitted, order not submitted."
            )

    def _request(
        self,
        method,
        path,
        *,
        purpose,
        order_side=None,
        return_response=False,
        not_found_is_none=False,
        **kwargs,
    ):
        # Safety gates must run before any network access, not just before
        # order submission: without this, a misconfigured Paper mode whose
        # ALPACA_PAPER_BASE_URL was overwritten with the Live URL could still
        # reach account/position endpoints on the Live host via this method,
        # and a kill switch engaged after construction could be bypassed by
        # calling AlpacaBroker.submit_order() directly instead of through the
        # paper_strategy_order.submit_order() wrapper.
        #
        # purpose has no default on purpose (pun intended): every call site
        # (inside this class or a direct caller bypassing it) must state its
        # intent explicitly. CODEX-021: order_side=None alone used to be
        # indistinguishable from a legitimate read-only call, which let a
        # direct POST to /v2/orders skip the kill switch entirely by simply
        # omitting order_side or passing None. purpose is checked here,
        # before self.session.request() is ever reached, and a caller that
        # omits it gets a TypeError before this line even runs.
        if purpose is None or not isinstance(purpose, RequestPurpose):
            raise ValueError(f"purpose must be a RequestPurpose member, got {purpose!r}")

        allowed_purposes = _METHOD_PURPOSES.get(method)
        if not allowed_purposes or purpose not in allowed_purposes:
            raise ValueError(
                f"HTTP method {method!r} is not permitted for purpose {purpose.name}"
            )

        if order_side is not None and order_side not in {"buy", "sell"}:
            raise ValueError(f"order_side must be 'buy', 'sell', or None, got {order_side!r}")

        # CODEX-022: the centralized 3-way check must run before any other
        # safety gate that could have a side effect (kill switch state
        # reads, credential revalidation), and always before
        # self.session.request() -- a mismatch means zero session calls,
        # regardless of what the kill switch state would otherwise allow.
        validate_order_intent(purpose, order_side, kwargs.get("json"))

        # CODEX-042: Alpaca is market-data-only in this deployment -- any
        # ORDER-shaped purpose (submit/cancel, never a read-only/
        # reconciliation call) must pass validate_alpaca_order_permitted()
        # before self.session.request() is ever reached, regardless of
        # which method/call path got here (submit_order(), cancel_order(),
        # a direct AlpacaBroker() instantiation bypassing every wrapper, a
        # dynamic import/alias -- all of them funnel through this single
        # _request() method, so this is the one place a check here closes
        # every path at once).
        if purpose in (RequestPurpose.ENTRY_ORDER, RequestPurpose.EXIT_ORDER, RequestPurpose.CANCEL_ORDER):
            self.config.validate_alpaca_order_permitted()

        self._validate_runtime_safety()
        self._check_kill_switch(purpose, order_side)
        url = f"{self.config.base_url}{path}"
        response = self.session.request(method, url, headers=self.headers, timeout=30, **kwargs)
        if not_found_is_none and response.status_code == 404:
            return None
        response.raise_for_status()
        return response if return_response else response.json()

    def get_account(self):
        return self._request("GET", "/v2/account", purpose=RequestPurpose.READ_ONLY)

    def get_positions(self):
        return self._request("GET", "/v2/positions", purpose=RequestPurpose.READ_ONLY)

    def get_recent_orders(self, limit=10):
        return self._request(
            "GET", f"/v2/orders?status=all&limit={limit}", purpose=RequestPurpose.READ_ONLY
        )

    def get_assets(self):
        """List tradable assets (used to build the trading universe).

        Goes through the same _request() safety gate as every other broker
        call — CODEX-009 closed a gap where universe_builder.py built its
        own URL from ALPACA_BASE_URL/ALPACA_PAPER_BASE_URL directly and
        called requests.get() without any endpoint validation.
        """
        return self._request("GET", "/v2/assets", purpose=RequestPurpose.READ_ONLY)

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
            purpose=RequestPurpose.RECONCILIATION,
            params={"client_order_id": client_order_id},
            not_found_is_none=True,
        )

    def submit_order(self, symbol, qty=1, *, side, order_type="market", time_in_force="day",
                      client_order_id=None, live_entry_context=None, account_cash_snapshot=None):
        # Long-only v1.0: buy is always an entry, sell is always an exit --
        # see _SIDE_TO_PURPOSE. There is no short-selling path in this
        # version, so this if/else is the complete mapping.
        if side not in {"buy", "sell"}:
            raise ValueError("side must be exactly 'buy' or 'sell'")
        purpose = _SIDE_TO_PURPOSE[side]

        # CODEX-026/CODEX-029/CODEX-031: the same live-entry gate
        # paper_strategy_order.submit_order() applies, re-run here at the
        # true final network boundary so a caller that bypasses that
        # wrapper and calls this method directly cannot escape the
        # allow-list/budget/FX/symbol-identity/authoritative-budget
        # checks. Scope is identical to the wrapper's: side="buy" AND
        # self.config.is_live_mode only -- Paper trading and every exit
        # are entirely unaffected. This is also now the SOLE reservation
        # point for real trading (paper_strategy_order.submit_order()
        # skips its own copy of the gate when `broker` is an AlpacaBroker
        # instance, to avoid double-reserving the same notional -- see
        # that module). See live_readiness/order_gateway.py's module
        # docstring for the full rationale.
        approval = None
        if side == "buy" and self.config.is_live_mode:
            from live_readiness.order_gateway import LiveOrderBlockedError, validate_and_size_live_entry
            if live_entry_context is None:
                return BrokerResponse(
                    status_code=423,
                    text="Live entry blocked: no LiveEntryContext supplied, order not submitted.",
                    data={"blocked_reason": "MISSING_LIVE_ENTRY_CONTEXT"},
                    dry_run=False,
                )
            try:
                # CODEX-034: pass the caller's own client_order_id through
                # (if it already generated one, e.g. via
                # paper_strategy_order.try_reserve_order()) so the durable
                # reservation and the actual broker order share one
                # identity -- the gateway mints one itself only if the
                # caller didn't supply one.
                #
                # CODEX-036: account_cash_snapshot, if the caller supplied
                # one (via live_readiness.account_cash.
                # fetch_account_cash_snapshot(), called earlier in the
                # caller's own flow -- see that module's docstring for why
                # this method does not fetch one itself), is passed
                # through unchanged and caps live_entry_context.
                # available_cash_krw at the real broker balance.
                approval = validate_and_size_live_entry(
                    live_entry_context, symbol, client_order_id,
                    account_cash_snapshot=account_cash_snapshot,
                )
            except LiveOrderBlockedError as exc:
                return BrokerResponse(
                    status_code=423,
                    text=f"Live entry blocked: {exc}",
                    data={"blocked_reason": str(exc)},
                    dry_run=False,
                )
            qty = approval.quantity
            client_order_id = approval.client_order_id  # always the id actually reserved

        order = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if client_order_id:
            order["client_order_id"] = client_order_id

        try:
            if self.config.is_live_mode and not self.config.can_submit_live_order:
                # LIVE_DRY_RUN: nothing was actually submitted -- release
                # the cash hold immediately rather than counting it
                # against allocatable cash until the next daily reset.
                if approval is not None:
                    _release_live_entry_reservation(approval.reservation_id)
                return BrokerResponse(
                    status_code=200,
                    text="LIVE_DRY_RUN: order was validated but not submitted.",
                    data={"dry_run": True, "order": order, "mode": self.config.status_label},
                    dry_run=True,
                )

            # CODEX-021 defense-in-depth: verify the outgoing payload's side
            # still matches the purpose derived from it before ever reaching
            # HTTP, so a future edit that mutates `order["side"]` after `purpose`
            # was computed fails loudly instead of silently submitting a
            # mismatched order under the wrong kill-switch gate.
            if _SIDE_TO_PURPOSE.get(order["side"]) is not purpose:
                raise RuntimeError(
                    "Order payload side does not match the submission purpose; refusing to submit."
                )

            response = self._request(
                "POST", "/v2/orders", purpose=purpose, order_side=side, json=order, return_response=True
            )
        except Exception as exc:
            # CODEX-034: distinguish a DEFINITIVE broker rejection (an
            # HTTP response was actually received, just a 4xx/5xx one --
            # requests' raise_for_status() raised HTTPError with
            # exc.response set) or a pre-network failure (kill switch,
            # credential revalidation, the payload/purpose consistency
            # check above -- the order definitively never reached the
            # broker) from an AMBIGUOUS failure (timeout, connection
            # reset, DNS failure -- no response was ever received, so the
            # broker may or may not have gotten the order). Only the
            # first two are safe to release; the ambiguous case must stay
            # counted against allocatable cash until reconciled (see
            # entry_reservation_ledger.py's module docstring) --
            # releasing it unconditionally here was exactly the bug
            # CODEX-034 found: a naive retry could then double-submit
            # while the authoritative snapshot under-counted real
            # exposure by the first (possibly-live) order's notional.
            if approval is not None:
                if _is_ambiguous_broker_failure(exc):
                    _mark_live_entry_submission_unknown(approval.reservation_id)
                else:
                    _release_live_entry_reservation(approval.reservation_id)
            raise

        if approval is not None:
            _commit_live_entry_reservation(approval.reservation_id)
        response_data = _safe_json(response)
        if approval is not None and isinstance(response_data, dict):
            # Surface the reservation id so positions/lifecycle.py::
            # enter_position() can link it to the position_id it's about
            # to create -- entry_reservation_ledger.build_snapshot() uses
            # that link to tell whether a COMMITTED reservation's funded
            # position has since closed (see that module's docstring).
            response_data = {**response_data, "live_entry_reservation_id": approval.reservation_id}
        return BrokerResponse(
            status_code=response.status_code,
            text=response.text,
            data=response_data,
            dry_run=False,
        )

    def cancel_order(self, order_id):
        """Cancel an order through the same runtime safety gate."""
        if not order_id or not isinstance(order_id, str):
            raise ValueError("order_id must be a non-empty string")
        return self._request(
            "DELETE", f"/v2/orders/{order_id}", purpose=RequestPurpose.CANCEL_ORDER
        )


def _safe_json(response):
    try:
        return response.json()
    except Exception:
        return None


def _commit_live_entry_reservation(reservation_id):
    """Best-effort: mark a CODEX-031 live-entry budget reservation
    COMMITTED after the broker has actually accepted the order. A
    failure here must not fail the (already-successful) order submission
    itself -- it would only mean the reservation stays RESERVED instead
    of COMMITTED, which is still counted as active budget/count
    consumption by entry_reservation_ledger.build_snapshot() either way."""
    from live_readiness import entry_reservation_ledger as live_ledger
    from state_store import db as state_db
    try:
        conn = state_db.open_db()
        try:
            live_ledger.mark_committed(conn, reservation_id)
        finally:
            conn.close()
    except Exception:
        pass


def _release_live_entry_reservation(reservation_id):
    """Best-effort: release a CODEX-031 live-entry cash reservation that
    will never become a real order (dry-run, a DEFINITIVE rejection, or a
    pre-network failure). A failure here must not mask the original
    error/response -- the reservation simply stays in its current
    (non-terminal) state until an operator/reconciliation resolves it, a
    strictly more conservative (never fail-open) outcome than releasing
    it. CODEX-034: never call this for an AMBIGUOUS failure -- see
    _mark_live_entry_submission_unknown()."""
    from live_readiness import entry_reservation_ledger as live_ledger
    from state_store import db as state_db
    try:
        conn = state_db.open_db()
        try:
            live_ledger.mark_released(conn, reservation_id)
        finally:
            conn.close()
    except Exception:
        pass


def _mark_live_entry_submission_unknown(reservation_id):
    """CODEX-034: best-effort transition of a live-entry cash reservation
    to SUBMISSION_UNKNOWN after an AMBIGUOUS broker-call failure (see
    _is_ambiguous_broker_failure()) -- deliberately NOT released, so it
    keeps counting against allocatable cash until
    entry_reservation_ledger.reconcile_by_client_order_id() (or an
    operator) resolves it against the broker's own record of the order."""
    from live_readiness import entry_reservation_ledger as live_ledger
    from state_store import db as state_db
    try:
        conn = state_db.open_db()
        try:
            live_ledger.mark_submission_unknown(conn, reservation_id)
        finally:
            conn.close()
    except Exception:
        pass


# CODEX-035: an ALLOWLIST of HTTP status codes Alpaca uses for genuine,
# definitive order-request rejection (a client-side problem with THIS
# specific request -- bad symbol, bad qty, insufficient buying power,
# etc. -- that unambiguously means the order was never accepted).
# Anything not on this allowlist (408/425/429, all 5xx, and any
# unrecognized code) defaults to ambiguous -- fail-closed by construction,
# so a new/unexpected status code is never silently trusted as definitive.
# 408 Request Timeout, 425 Too Early, and 429 Too Many Requests are
# deliberately excluded even though they are formally 4xx: none of them
# says "your order was rejected", they say "try again" -- treating them
# as definitive would be exactly the CODEX-035 bug.
_DEFINITIVE_REJECTION_STATUS_CODES = frozenset({400, 401, 403, 404, 409, 410, 422})


def _is_ambiguous_broker_failure(exc):
    """CODEX-034/CODEX-035: True for a broker-call failure that does NOT
    prove the order was never received. False only for a DEFINITIVE
    outcome: a response whose HTTP status is on
    _DEFINITIVE_REJECTION_STATUS_CODES AND whose body parses as a JSON
    object (Alpaca's actual error-response shape), or any non-network
    exception (e.g. a pre-network safety-gate RuntimeError) -- both mean
    the order's fate IS known (rejected, or never sent at all).

    Ambiguous (SUBMISSION_UNKNOWN, never released) cases, per CODEX-035's
    direct reproduction: a plain requests.exceptions.RequestException
    with no response at all (timeout, connection reset, DNS failure);
    an HTTPError whose response IS present but whose status is 408, 425,
    429, or any 5xx (500/502/503/504/...) -- an upstream/gateway/rate-limit
    failure proves nothing about whether Alpaca's own order-matching
    engine ever received the request; an HTTPError whose status happens to
    be on the definitive allowlist but whose body doesn't parse as JSON
    (can't confirm it's actually Alpaca's rejection contract and not some
    proxy's generic error page); and any status code not on the allowlist
    at all (fail-closed default for codes this classifier doesn't
    recognize)."""
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        if response is None:
            return True
        if getattr(response, "status_code", None) not in _DEFINITIVE_REJECTION_STATUS_CODES:
            return True
        try:
            body = response.json()
        except Exception:
            return True
        return not isinstance(body, dict)
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    return False
