from datetime import datetime, timezone

import pytest

from execution import authorization as auth
from execution.order_gate import OrderGateBlockedError

NOW = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)


class _FakeOrderIntent:
    def __init__(self, internal_order_id="ord-1", side="buy", symbol="AAPL"):
        self.internal_order_id = internal_order_id
        self.side = side
        self.symbol = symbol


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    yield


def _passing_gate(ctx):
    return True


def _failing_gate(ctx):
    raise OrderGateBlockedError("blocked for test")


class TestAuthorizeNewOrder:
    def test_success_returns_authorized_execution(self):
        oi = _FakeOrderIntent()
        result = auth.authorize_new_order(oi, lambda: object(), _passing_gate, now=NOW)
        assert isinstance(result, auth.AuthorizedExecution)
        assert result.internal_order_id == "ord-1"
        assert result.action == "order"

    def test_halt_blocks_before_gate_even_runs(self):
        from operations import kill_switch
        kill_switch.set_halt(True, reason="test", actor="tester")
        oi = _FakeOrderIntent()
        gate_calls = []

        def _gate_that_should_not_run(ctx):
            gate_calls.append(ctx)
            return True

        with pytest.raises(auth.UnauthorizedExecutionError, match="HALT"):
            auth.authorize_new_order(oi, lambda: object(), _gate_that_should_not_run, now=NOW)
        assert gate_calls == []

    def test_gate_failure_propagates(self):
        oi = _FakeOrderIntent()
        with pytest.raises(OrderGateBlockedError):
            auth.authorize_new_order(oi, lambda: object(), _failing_gate, now=NOW)

    def test_each_authorization_has_a_unique_single_use_token(self):
        oi1 = _FakeOrderIntent(internal_order_id="ord-1")
        oi2 = _FakeOrderIntent(internal_order_id="ord-2")
        a1 = auth.authorize_new_order(oi1, lambda: object(), _passing_gate, now=NOW)
        a2 = auth.authorize_new_order(oi2, lambda: object(), _passing_gate, now=NOW)
        assert a1.token != a2.token


class TestAuthorizeCancel:
    def test_success_ignores_halt(self):
        from operations import kill_switch
        kill_switch.set_halt(True, reason="risk event", actor="tester")
        oi = _FakeOrderIntent(side="sell")
        result = auth.authorize_cancel(oi, lambda: object(), _passing_gate, now=NOW)
        assert result.action == "cancel"

    def test_gate_failure_still_propagates(self):
        oi = _FakeOrderIntent()
        with pytest.raises(OrderGateBlockedError):
            auth.authorize_cancel(oi, lambda: object(), _failing_gate, now=NOW)


class TestConsume:
    def test_valid_authorization_consumed_once(self):
        oi = _FakeOrderIntent()
        authorization = auth.authorize_new_order(oi, lambda: object(), _passing_gate, now=NOW)
        auth.consume(authorization, oi, expected_action="order")  # should not raise

    def test_reused_token_rejected(self):
        oi = _FakeOrderIntent()
        authorization = auth.authorize_new_order(oi, lambda: object(), _passing_gate, now=NOW)
        auth.consume(authorization, oi, expected_action="order")
        with pytest.raises(auth.UnauthorizedExecutionError, match="already used"):
            auth.consume(authorization, oi, expected_action="order")

    def test_none_rejected(self):
        oi = _FakeOrderIntent()
        with pytest.raises(auth.UnauthorizedExecutionError):
            auth.consume(None, oi, expected_action="order")

    def test_hand_built_fake_authorization_rejected(self):
        # Proves this is not "protection by underscore" -- a caller
        # constructing an identical-looking AuthorizedExecution by hand
        # (not via authorize_new_order/authorize_cancel) still fails,
        # since its token was never registered.
        oi = _FakeOrderIntent()
        fake = auth.AuthorizedExecution(
            internal_order_id=oi.internal_order_id, side=oi.side, action="order",
            token="totally-made-up-token", authorized_at=NOW,
        )
        with pytest.raises(auth.UnauthorizedExecutionError, match="invalid, expired, or"):
            auth.consume(fake, oi, expected_action="order")

    def test_mismatched_order_id_rejected(self):
        oi = _FakeOrderIntent(internal_order_id="ord-1")
        authorization = auth.authorize_new_order(oi, lambda: object(), _passing_gate, now=NOW)
        other = _FakeOrderIntent(internal_order_id="ord-2")
        with pytest.raises(auth.UnauthorizedExecutionError, match="does not match"):
            auth.consume(authorization, other, expected_action="order")

    def test_mismatched_side_rejected(self):
        oi = _FakeOrderIntent(side="buy")
        authorization = auth.authorize_new_order(oi, lambda: object(), _passing_gate, now=NOW)
        oi.side = "sell"  # mutate after the fact
        with pytest.raises(auth.UnauthorizedExecutionError, match="side"):
            auth.consume(authorization, oi, expected_action="order")

    def test_mismatched_action_rejected(self):
        oi = _FakeOrderIntent()
        authorization = auth.authorize_new_order(oi, lambda: object(), _passing_gate, now=NOW)
        with pytest.raises(auth.UnauthorizedExecutionError, match="action"):
            auth.consume(authorization, oi, expected_action="cancel")
