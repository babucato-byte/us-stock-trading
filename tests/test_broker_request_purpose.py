"""CODEX-021/CODEX-020 remainder: AlpacaBroker._request() must gate on an
explicit RequestPurpose, not merely on order_side.

Before this fix, order_side=None was indistinguishable from a legitimate
read-only call: a direct broker._request("POST", "/v2/orders", order_side=
None, ...) call reached self.session.request() without ever consulting the
kill switch, because _check_kill_switch() returned immediately whenever
order_side was None. purpose is now a mandatory, no-default, isinstance-
checked argument that _request() validates -- together with a strict
(method, purpose) matrix -- before touching self.session at all.

No real network calls anywhere: every broker uses a RecordingSession double,
and every kill switch state lives under tmp_path (tests/conftest.py's
autouse fixture already points kill_switch_state.STATE_FILE at tmp_path for
every test in this suite; only the binary halt file needs explicit
isolation here, exactly as in test_broker_kill_switch_gate.py).
"""

import pytest

import kill_switch
import kill_switch_state as kss
from broker import AlpacaBroker, BrokerConfig
from broker.alpaca_client import RequestPurpose


class RecordingSession:
    """Session double whose .request() is spied on: records every call and
    returns a canned 200 response, so tests can assert call counts without
    touching the network."""

    class _Response:
        status_code = 200
        text = "OK"

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "accepted"}

    def __init__(self):
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self._Response()

    @property
    def posts(self):
        return [r for r in self.requests if r[0][0] == "POST"]

    @property
    def gets(self):
        return [r for r in self.requests if r[0][0] == "GET"]

    @property
    def deletes(self):
        return [r for r in self.requests if r[0][0] == "DELETE"]


@pytest.fixture(autouse=True)
def _matching_env_credentials(monkeypatch):
    # CODEX-018: the common gate re-reads current environment credentials on
    # every request and requires them to match self.config's captured
    # values -- every broker built by _make_broker() below captures
    # "key"/"secret", so the environment must hold the same values by
    # default for the purpose/kill-switch assertions in this file to
    # exercise their intended paths instead of failing closed on
    # credentials first.
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")


def _make_broker(session=None, api_key="key", secret_key="secret"):
    # KIS migration (CODEX-042): this file tests the RequestPurpose gate
    # specifically, not the Alpaca-order-disabled gate -- explicitly
    # authorize Alpaca paper orders so these tests keep exercising exactly
    # what they were designed to test.
    config = BrokerConfig(
        trading_mode="paper", api_key=api_key, secret_key=secret_key,
        execution_broker="alpaca", alpaca_paper_order_enabled=True,
    )
    return AlpacaBroker(config=config, session=session or RecordingSession())


def _isolate_binary_halt(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_HALTED", raising=False)
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")


# ---------------------------------------------------------------------------
# (a) purpose=None on a direct order-shaped POST must never reach the session.
# ---------------------------------------------------------------------------

def test_purpose_none_on_post_orders_blocks_before_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request("POST", "/v2/orders", purpose=None, order_side="buy", json={"side": "buy"})

    assert broker.session.requests == []


# ---------------------------------------------------------------------------
# (b) Old call shape (order_side only, no purpose) must not silently pass.
# ---------------------------------------------------------------------------

def test_legacy_order_side_only_call_without_purpose_blocks_before_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(TypeError):
        broker._request("POST", "/v2/orders", order_side=None, json={"side": "buy"})

    assert broker.session.requests == []


# ---------------------------------------------------------------------------
# (c) / (d) Kill switch engaged, purpose=None or a wrong (method, purpose)
# combination on a direct POST: HTTP must never be reached, in every state.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "purpose",
    [None, RequestPurpose.READ_ONLY, RequestPurpose.RECONCILIATION, RequestPurpose.CANCEL_ORDER],
)
def test_entry_disabled_direct_post_with_invalid_purpose_never_reaches_http(monkeypatch, tmp_path, purpose):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    with pytest.raises((ValueError, RuntimeError)):
        broker._request("POST", "/v2/orders", purpose=purpose, order_side="buy", json={"side": "buy"})

    assert broker.session.requests == []


@pytest.mark.parametrize(
    "purpose",
    [None, RequestPurpose.READ_ONLY, RequestPurpose.RECONCILIATION, RequestPurpose.CANCEL_ORDER],
)
def test_binary_halt_direct_post_with_invalid_purpose_never_reaches_http(monkeypatch, tmp_path, purpose):
    _isolate_binary_halt(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING_HALTED", "true")
    broker = _make_broker()

    with pytest.raises((ValueError, RuntimeError)):
        broker._request("POST", "/v2/orders", purpose=purpose, order_side="buy", json={"side": "buy"})

    assert broker.session.requests == []


# ---------------------------------------------------------------------------
# (e) Method x purpose matrix.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "purpose, side",
    [(RequestPurpose.ENTRY_ORDER, "buy"), (RequestPurpose.EXIT_ORDER, "sell")],
)
def test_post_allows_entry_and_exit_purpose(monkeypatch, tmp_path, purpose, side):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    # CODEX-022: order_side and json["side"] must both agree with purpose --
    # see test_broker_order_intent_gate.py for the exhaustive 3-way matrix.
    broker._request(
        "POST", "/v2/orders", purpose=purpose, order_side=side, json={"side": side}
    )

    assert len(broker.session.posts) == 1


@pytest.mark.parametrize(
    "purpose",
    [None, RequestPurpose.READ_ONLY, RequestPurpose.RECONCILIATION, RequestPurpose.CANCEL_ORDER],
)
def test_post_blocks_non_order_purposes(monkeypatch, tmp_path, purpose):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request("POST", "/v2/orders", purpose=purpose, json={"side": "buy"})

    assert broker.session.posts == []


@pytest.mark.parametrize("purpose", [RequestPurpose.READ_ONLY, RequestPurpose.RECONCILIATION])
def test_get_allows_read_only_and_reconciliation(monkeypatch, tmp_path, purpose):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    broker._request("GET", "/v2/account", purpose=purpose)

    assert len(broker.session.gets) == 1


def test_get_blocks_entry_order_purpose(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request("GET", "/v2/account", purpose=RequestPurpose.ENTRY_ORDER)

    assert broker.session.gets == []


def test_delete_allows_cancel_order_purpose(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    broker._request("DELETE", "/v2/orders/order-1", purpose=RequestPurpose.CANCEL_ORDER)

    assert len(broker.session.deletes) == 1


@pytest.mark.parametrize(
    "purpose",
    [None, RequestPurpose.READ_ONLY, RequestPurpose.RECONCILIATION, RequestPurpose.ENTRY_ORDER, RequestPurpose.EXIT_ORDER],
)
def test_delete_blocks_non_cancel_purposes(monkeypatch, tmp_path, purpose):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request("DELETE", "/v2/orders/order-1", purpose=purpose)

    assert broker.session.deletes == []


# ---------------------------------------------------------------------------
# (f) Public API regression: submit_order() behavior must be unchanged.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side", ["buy", "sell"])
def test_active_allows_both_sides(monkeypatch, tmp_path, side):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    response = broker.submit_order("AAPL", qty=1, side=side)

    assert response.status_code == 200
    assert len(broker.session.posts) == 1


def test_entry_disabled_blocks_buy_allows_sell(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side="buy")
    assert broker.session.posts == []

    response = broker.submit_order("AAPL", qty=1, side="sell")
    assert response.status_code == 200
    assert len(broker.session.posts) == 1


def test_binary_halt_blocks_buy(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING_HALTED", "true")
    broker = _make_broker()

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side="buy")

    assert broker.session.posts == []


@pytest.mark.parametrize("bad_side", [None, "", "hold", "BUY", "buy "])
def test_missing_or_misspelled_side_is_blocked(monkeypatch, tmp_path, bad_side):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker.submit_order("AAPL", qty=1, side=bad_side)

    assert broker.session.posts == []


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_payload_side_is_preserved_not_swapped(monkeypatch, tmp_path, side):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    broker.submit_order("AAPL", qty=1, side=side)

    assert len(broker.session.posts) == 1
    _, kwargs = broker.session.posts[0]
    assert kwargs["json"]["side"] == side


# ---------------------------------------------------------------------------
# (g) Credentials regression (CODEX-018): GET/POST/DELETE all fail closed
# when key/secret are missing, swapped, or blank, and succeed unchanged
# otherwise.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "env_key, env_secret",
    [
        (None, "secret"),
        ("key", None),
        ("", "secret"),
        ("key", ""),
        ("   ", "secret"),
        ("key", "   "),
        ("rotated-key", "secret"),
        ("key", "rotated-secret"),
    ],
)
def test_credential_mismatch_blocks_get_post_delete(monkeypatch, tmp_path, env_key, env_secret):
    _isolate_binary_halt(monkeypatch, tmp_path)
    if env_key is None:
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ALPACA_API_KEY", env_key)
    if env_secret is None:
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("ALPACA_SECRET_KEY", env_secret)

    broker = _make_broker()

    with pytest.raises(RuntimeError):
        broker.get_account()
    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side="buy")
    with pytest.raises(RuntimeError):
        broker.cancel_order("order-1")

    assert broker.session.requests == []


def test_matching_credentials_allow_get_post_delete(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    broker = _make_broker()

    assert broker.get_account() == {"status": "accepted"}
    assert broker.submit_order("AAPL", qty=1, side="buy").status_code == 200
    assert broker.cancel_order("order-1") == {"status": "accepted"}

    assert len(broker.session.requests) == 3
