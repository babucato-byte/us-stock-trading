"""CODEX-022 (closing the CODEX-021 remainder): AlpacaBroker._request() must
enforce a single, centralized 3-way consistency check -- purpose x order_side
x outgoing json payload["side"] -- before self.session.request() is ever
reached.

Before validate_order_intent() existed, _request() validated purpose against
the HTTP method, and order_side against {"buy", "sell", None}, each in
isolation. Nothing ever compared purpose, order_side, and the outgoing JSON
body's "side" field against each other, so a caller could reach the network
with e.g. purpose=EXIT_ORDER, order_side="sell", json={"side": "buy"} -- or a
payload missing "side" entirely, or spelled "BUY" / " buy" / "sell " / True /
1 -- and every prior gate would wave it through unexamined.

validate_order_intent() (broker/alpaca_client.py) is the only place in the
codebase that performs this comparison. This file exercises it exhaustively
through the public _request()/submit_order() surface; it does not reimplement
the check itself.

No real network calls anywhere: every broker uses a RecordingSession double,
and every kill switch state file lives under tmp_path (tests/conftest.py's
autouse fixture already points kill_switch_state.STATE_FILE at tmp_path for
every test in this suite; only the binary halt file needs explicit isolation
here, exactly as in test_broker_request_purpose.py / test_broker_kill_switch_gate.py).
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
    # default, or every assertion in this file would fail closed on
    # credentials before ever reaching the intent gate under test.
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")


def _make_broker(session=None, api_key="key", secret_key="secret"):
    config = BrokerConfig(trading_mode="paper", api_key=api_key, secret_key=secret_key)
    return AlpacaBroker(config=config, session=session or RecordingSession())


def _isolate_binary_halt(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_HALTED", raising=False)
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")


# ---------------------------------------------------------------------------
# (1) ENTRY_DISABLED + EXIT_ORDER: 3-way mismatches must block before HTTP,
# even though the kill switch state itself would allow EXIT_ORDER here.
# ---------------------------------------------------------------------------

def test_entry_disabled_exit_order_side_sell_payload_buy_blocks(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "POST", "/v2/orders", purpose=RequestPurpose.EXIT_ORDER,
            order_side="sell", json={"side": "buy"},
        )

    assert broker.session.requests == []


def test_entry_disabled_exit_order_side_buy_payload_buy_blocks(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "POST", "/v2/orders", purpose=RequestPurpose.EXIT_ORDER,
            order_side="buy", json={"side": "buy"},
        )

    assert broker.session.requests == []


def test_entry_disabled_exit_order_side_none_payload_buy_blocks(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "POST", "/v2/orders", purpose=RequestPurpose.EXIT_ORDER,
            order_side=None, json={"side": "buy"},
        )

    assert broker.session.requests == []


def test_entry_disabled_entry_order_side_sell_payload_sell_blocks(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "POST", "/v2/orders", purpose=RequestPurpose.ENTRY_ORDER,
            order_side="sell", json={"side": "sell"},
        )

    assert broker.session.requests == []


# ---------------------------------------------------------------------------
# (2) ACTIVE state, cross-side mismatches -- the gate must catch these even
# when the kill switch state itself would allow the order.
# ---------------------------------------------------------------------------

def test_active_entry_order_side_buy_payload_sell_blocks(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "POST", "/v2/orders", purpose=RequestPurpose.ENTRY_ORDER,
            order_side="buy", json={"side": "sell"},
        )

    assert broker.session.requests == []


def test_active_exit_order_side_sell_payload_buy_blocks(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "POST", "/v2/orders", purpose=RequestPurpose.EXIT_ORDER,
            order_side="sell", json={"side": "buy"},
        )

    assert broker.session.requests == []


# ---------------------------------------------------------------------------
# (3) Malformed / non-literal payload "side" values must all be rejected,
# regardless of purpose (parametrized across ENTRY_ORDER and EXIT_ORDER).
# ---------------------------------------------------------------------------

_MALFORMED_JSON_CASES = [
    ("missing_side_key", {"symbol": "AAPL"}),
    ("none_payload", None),
    ("side_is_none", {"side": None}),
    ("side_uppercase_buy", {"side": "BUY"}),
    ("side_uppercase_sell", {"side": "SELL"}),
    ("side_leading_space", {"side": " buy"}),
    ("side_trailing_space", {"side": "sell "}),
    ("side_bool_true", {"side": True}),
    ("side_int_one", {"side": 1}),
]


@pytest.mark.parametrize("purpose, side", [
    (RequestPurpose.ENTRY_ORDER, "buy"),
    (RequestPurpose.EXIT_ORDER, "sell"),
])
@pytest.mark.parametrize("case_name, payload", _MALFORMED_JSON_CASES, ids=[c[0] for c in _MALFORMED_JSON_CASES])
def test_malformed_json_side_blocks_before_session(monkeypatch, tmp_path, purpose, side, case_name, payload):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    kwargs = {} if payload is None else {"json": payload}

    with pytest.raises(ValueError):
        broker._request("POST", "/v2/orders", purpose=purpose, order_side=side, **kwargs)

    assert broker.session.requests == []


# ---------------------------------------------------------------------------
# (4) GET/DELETE must never carry an order_side or a json["side"] -- those
# purposes are order-side-free by definition.
# ---------------------------------------------------------------------------

def test_get_with_order_side_blocks_before_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request("GET", "/v2/account", purpose=RequestPurpose.READ_ONLY, order_side="buy")

    assert broker.session.requests == []


def test_get_with_json_side_blocks_before_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "GET", "/v2/account", purpose=RequestPurpose.RECONCILIATION, json={"side": "buy"}
        )

    assert broker.session.requests == []


def test_delete_with_order_side_blocks_before_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "DELETE", "/v2/orders/abc123", purpose=RequestPurpose.CANCEL_ORDER, order_side="sell"
        )

    assert broker.session.requests == []


def test_delete_with_json_side_blocks_before_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "DELETE", "/v2/orders/abc123", purpose=RequestPurpose.CANCEL_ORDER, json={"side": "sell"}
        )

    assert broker.session.requests == []


# ---------------------------------------------------------------------------
# (5) Matching cases must pass the intent gate; whether HTTP is actually
# reached beyond that still depends on the kill switch state, exactly as
# before this gate was added.
# ---------------------------------------------------------------------------

def test_active_entry_order_side_buy_payload_buy_reaches_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    broker._request(
        "POST", "/v2/orders", purpose=RequestPurpose.ENTRY_ORDER,
        order_side="buy", json={"side": "buy"},
    )

    assert len(broker.session.posts) == 1


def test_active_exit_order_side_sell_payload_sell_reaches_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    broker._request(
        "POST", "/v2/orders", purpose=RequestPurpose.EXIT_ORDER,
        order_side="sell", json={"side": "sell"},
    )

    assert len(broker.session.posts) == 1


def test_entry_disabled_entry_order_side_buy_payload_buy_passes_gate_but_kill_switch_blocks(
    monkeypatch, tmp_path
):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    # The 3-way gate passes (order_side/payload both say "buy", matching
    # ENTRY_ORDER) -- but ENTRY_DISABLED still blocks new entries at the
    # kill-switch-state layer, which runs after the intent gate.
    with pytest.raises(RuntimeError):
        broker._request(
            "POST", "/v2/orders", purpose=RequestPurpose.ENTRY_ORDER,
            order_side="buy", json={"side": "buy"},
        )

    assert broker.session.requests == []


def test_entry_disabled_exit_order_side_sell_payload_sell_reaches_session(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    # ENTRY_DISABLED still permits exits/liquidation -- the intent gate
    # passes (sell/sell matches EXIT_ORDER) and the kill switch state allows
    # it too, so this one reaches the session.
    broker._request(
        "POST", "/v2/orders", purpose=RequestPurpose.EXIT_ORDER,
        order_side="sell", json={"side": "sell"},
    )

    assert len(broker.session.posts) == 1


# ---------------------------------------------------------------------------
# (6) Public/private path parity: submit_order()'s own payload must pass the
# same gate a direct _request() call would, under identical kill switch
# state -- same allow/block outcome either way.
# ---------------------------------------------------------------------------

def test_submit_order_buy_matches_direct_request_under_active(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)

    broker_public = _make_broker()
    broker_private = _make_broker()

    broker_public.submit_order("AAPL", qty=1, side="buy")
    broker_private._request(
        "POST", "/v2/orders", purpose=RequestPurpose.ENTRY_ORDER,
        order_side="buy", json={"side": "buy"},
    )

    assert len(broker_public.session.posts) == 1
    assert len(broker_private.session.posts) == 1


def test_submit_order_buy_matches_direct_request_under_entry_disabled(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")

    broker_public = _make_broker()
    broker_private = _make_broker()

    with pytest.raises(RuntimeError):
        broker_public.submit_order("AAPL", qty=1, side="buy")
    with pytest.raises(RuntimeError):
        broker_private._request(
            "POST", "/v2/orders", purpose=RequestPurpose.ENTRY_ORDER,
            order_side="buy", json={"side": "buy"},
        )

    assert broker_public.session.requests == []
    assert broker_private.session.requests == []
