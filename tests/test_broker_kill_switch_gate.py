"""CODEX-020: AlpacaBroker._request() must gate on both kill switch
mechanisms itself, not only rely on paper_strategy_order.submit_order()'s
wrapper-level check.

Before this fix, calling AlpacaBroker.submit_order(side="buy") directly --
skipping the paper_strategy_order.submit_order() wrapper entirely -- reached
the network via the fake session even while ENTRY_DISABLED/ALL_TRADING_
DISABLED/MANUAL_REVIEW/binary-halt was engaged, because _request() only ran
_validate_runtime_safety() (config/env checks) and never consulted
kill_switch.is_trading_halted() / kill_switch_state.is_entry_allowed() /
is_liquidation_allowed(). test_gap_direct_broker_call_bypassed_kill_switch_*
below reproduce that exact scenario and now pass because _request() checks
order_side against both kill switches before ever calling session.request().

No real network calls anywhere: every broker uses a RecordingSession double,
and every kill switch state lives under tmp_path (conftest.py's autouse
fixture already points kill_switch_state.STATE_FILE at tmp_path for every
test in this suite; only the binary halt file needs explicit isolation here).
"""

import pytest

import kill_switch
import kill_switch_state as kss
import paper_strategy_order as pso
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


@pytest.fixture(autouse=True)
def _matching_env_credentials(monkeypatch):
    # CODEX-018: the common gate now re-reads current environment
    # credentials on every request and requires them to match self.config's
    # captured values -- every broker built by _make_broker() below captures
    # "key"/"secret", so the environment must hold the same values for the
    # kill-switch-focused assertions in this file to keep exercising their
    # intended success/failure paths instead of failing closed here first.
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")


def _make_broker(session=None):
    # KIS migration: this file tests kill-switch gate agreement between
    # the wrapper and direct broker paths specifically, not the new
    # Alpaca-order-disabled gate -- alpaca_paper_order_enabled=True keeps
    # every test here exercising exactly what it was designed to test.
    config = BrokerConfig(
        trading_mode="paper", api_key="key", secret_key="secret",
        execution_broker="alpaca", alpaca_paper_order_enabled=True,
    )
    return AlpacaBroker(config=config, session=session or RecordingSession())


def _isolate_binary_halt(monkeypatch, tmp_path):
    """Isolate kill_switch.py's binary halt file/env var.

    kill_switch_state.STATE_FILE is already pointed at tmp_path by
    conftest.py's autouse fixture (same tmp_path instance for this test
    node), so only the separate binary-halt mechanism needs isolating here.
    """
    monkeypatch.delenv("TRADING_HALTED", raising=False)
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")


# ---------------------------------------------------------------------------
# Gap reproduction: direct AlpacaBroker.submit_order() call, no wrapper.
# ---------------------------------------------------------------------------

def test_gap_direct_broker_call_allowed_when_active(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    response = broker.submit_order("AAPL", qty=1, side="buy")

    assert response.status_code == 200
    assert len(broker.session.posts) == 1


def test_gap_direct_broker_call_blocked_when_entry_disabled(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side="buy")

    assert broker.session.posts == []


def test_entry_disabled_still_allows_liquidation_sell(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = _make_broker()

    response = broker.submit_order("AAPL", qty=1, side="sell")

    assert response.status_code == 200
    assert len(broker.session.posts) == 1


@pytest.mark.parametrize("state", [kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_all_trading_disabled_and_manual_review_block_both_sides_direct(monkeypatch, tmp_path, state, side):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(state, reason="incident", activated_by="ops1")
    broker = _make_broker()

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side=side)

    assert broker.session.posts == []


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_binary_halt_blocks_every_side_even_when_state_active(monkeypatch, tmp_path, side):
    _isolate_binary_halt(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING_HALTED", "true")
    broker = _make_broker()

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side=side)

    assert broker.session.posts == []


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_corrupted_state_file_blocks_direct_call_fail_closed(monkeypatch, tmp_path, side):
    _isolate_binary_halt(monkeypatch, tmp_path)
    # conftest.py's autouse fixture already points kss.STATE_FILE at this
    # exact path for this test node (same tmp_path instance).
    (tmp_path / "KILL_SWITCH_STATE.json").write_text("{ not valid json ]")
    broker = _make_broker()

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side=side)

    assert broker.session.posts == []


def test_missing_state_file_preserves_existing_active_default(monkeypatch, tmp_path):
    """A state file that was never created means ACTIVE (kill_switch_state's
    own documented default-allow behavior, unchanged by this fix) -- unlike a
    corrupted file, which fails closed via _fail_closed_snapshot above."""
    _isolate_binary_halt(monkeypatch, tmp_path)
    assert not (tmp_path / "KILL_SWITCH_STATE.json").exists()
    broker = _make_broker()

    response = broker.submit_order("AAPL", qty=1, side="buy")

    assert response.status_code == 200
    assert len(broker.session.posts) == 1


# ---------------------------------------------------------------------------
# Non-order endpoints must keep working in every kill switch state.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "state", [kss.ACTIVE, kss.ENTRY_DISABLED, kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW]
)
def test_get_account_and_positions_and_cancel_unaffected_by_kill_switch_state(monkeypatch, tmp_path, state):
    _isolate_binary_halt(monkeypatch, tmp_path)
    if state != kss.ACTIVE:
        kss.activate(state, reason="incident", activated_by="ops1")
    broker = _make_broker()

    assert broker.get_account() == {"status": "accepted"}
    assert broker.get_positions() == {"status": "accepted"}
    assert broker.cancel_order("order-1") == {"status": "accepted"}
    assert broker.get_recent_orders() == {"status": "accepted"}
    assert broker.get_assets() == {"status": "accepted"}
    assert broker.get_order_by_client_order_id("cid-1") == {"status": "accepted"}


def test_get_account_and_positions_unaffected_by_binary_halt(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING_HALTED", "true")
    broker = _make_broker()

    assert broker.get_account() == {"status": "accepted"}
    assert broker.get_positions() == {"status": "accepted"}
    assert broker.cancel_order("order-1") == {"status": "accepted"}


# ---------------------------------------------------------------------------
# Wrapper path (paper_strategy_order.submit_order) and direct broker path
# must agree on the same permit/deny decision for the same kill switch state.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side", ["buy", "sell"])
def test_wrapper_and_direct_paths_agree_when_entry_disabled(monkeypatch, tmp_path, side):
    _isolate_binary_halt(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")

    direct_broker = _make_broker()
    direct_allowed = True
    try:
        direct_broker.submit_order("AAPL", qty=1, side=side)
    except RuntimeError:
        direct_allowed = False

    wrapper_broker = _make_broker()
    wrapper_response = pso.submit_order("AAPL", qty=1, broker=wrapper_broker, side=side)
    wrapper_allowed = wrapper_response.status_code != 423

    assert direct_allowed == wrapper_allowed
    if side == "buy":
        assert direct_allowed is False
        assert direct_broker.session.posts == []
        assert wrapper_broker.session.posts == []
    else:
        assert direct_allowed is True
        assert len(direct_broker.session.posts) == 1
        assert len(wrapper_broker.session.posts) == 1


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_wrapper_and_direct_paths_agree_when_active(monkeypatch, tmp_path, side):
    _isolate_binary_halt(monkeypatch, tmp_path)

    direct_broker = _make_broker()
    direct_broker.submit_order("AAPL", qty=1, side=side)

    wrapper_broker = _make_broker()
    wrapper_response = pso.submit_order("AAPL", qty=1, broker=wrapper_broker, side=side)

    assert wrapper_response.status_code == 200
    assert len(direct_broker.session.posts) == 1
    assert len(wrapper_broker.session.posts) == 1


# ---------------------------------------------------------------------------
# State changes must be observed immediately by an already-constructed
# broker instance -- no caching of kill switch state on self.
# ---------------------------------------------------------------------------

def test_state_change_after_construction_is_reflected_immediately(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    response = broker.submit_order("AAPL", qty=1, side="buy")
    assert response.status_code == 200
    assert len(broker.session.posts) == 1

    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side="buy")
    assert len(broker.session.posts) == 1  # unchanged: second attempt never reached the session

    kss.release(released_by="ops1", reason="resolved")

    response = broker.submit_order("AAPL", qty=1, side="buy")
    assert response.status_code == 200
    assert len(broker.session.posts) == 2


def test_binary_halt_engaged_after_construction_is_reflected_immediately(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    response = broker.submit_order("AAPL", qty=1, side="buy")
    assert response.status_code == 200
    assert len(broker.session.posts) == 1

    monkeypatch.setenv("TRADING_HALTED", "true")

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side="buy")
    assert len(broker.session.posts) == 1


# ---------------------------------------------------------------------------
# order_side itself: only "buy"/"sell"/None are meaningful; the gate must
# never infer intent from HTTP method or the request body.
# ---------------------------------------------------------------------------

def test_request_rejects_unknown_order_side_value(monkeypatch, tmp_path):
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request(
            "POST", "/v2/orders", purpose=RequestPurpose.ENTRY_ORDER, order_side="hold", json={}
        )

    assert broker.session.posts == []


def test_request_requires_purpose_even_when_bypassing_submit_order(monkeypatch, tmp_path):
    """Reproduces the independent-review gap: calling the common _request()
    path directly for an order-shaped POST, without naming purpose at all,
    must not silently default to "no gate" -- it must fail before the
    session is ever touched, in every kill switch state (including ACTIVE,
    where submit_order() itself would have been allowed through). purpose
    has no default on purpose (CODEX-021): order_side alone, which does have
    a default of None, is no longer sufficient to reach the session."""
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(TypeError):
        broker._request("POST", "/v2/orders", json={"symbol": "AAPL", "side": "buy"})

    assert broker.session.posts == []


def test_request_requires_purpose_even_when_order_side_given(monkeypatch, tmp_path):
    """CODEX-021: order_side alone (even a valid one) must never be enough to
    reach the session -- purpose is the mandatory, no-default gate. A caller
    that names order_side but omits purpose entirely must fail with a
    TypeError before self.session.request() is ever touched."""
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(TypeError):
        broker._request("POST", "/v2/orders", order_side="buy", json={"symbol": "AAPL", "side": "buy"})

    assert broker.session.posts == []


def test_request_rejects_none_purpose_explicitly(monkeypatch, tmp_path):
    """purpose=None must be rejected explicitly (not just "missing"), since a
    caller could construct one dynamically and pass None instead of omitting
    the keyword -- e.g. order_side=None was the exact shape of the original
    CODEX-021 bypass, and purpose must not have an equivalent hole."""
    _isolate_binary_halt(monkeypatch, tmp_path)
    broker = _make_broker()

    with pytest.raises(ValueError):
        broker._request("POST", "/v2/orders", purpose=None, order_side="buy", json={"symbol": "AAPL", "side": "buy"})

    assert broker.session.posts == []
