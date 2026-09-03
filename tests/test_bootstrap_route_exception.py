"""One authorised bootstrap BUY may prove the daytime route. Nothing else may.

The deadlock
------------
`daytime_order_tr_id_live_buy` (TTTS6036U) can only be confirmed by a real
BUY. `order_gate._check_route_evidence` refused every real daytime BUY
because that TR was unconfirmed. The one-shot bootstrap exists to break
exactly that circle -- `final_safety_recheck`'s `armed_with_unconfirmed_route`
and `session_capability.bootstrap_permitted_on_armed()` both permit a
bootstrap BECAUSE the route is unconfirmed -- but the capability travelled
only as far as the broker guard, two steps past the gate that had already
refused. Two individually correct changes that together closed the door.

What the exception is, and is not
---------------------------------
It is a per-order capability re-validated at the gate against the order in
hand. It is NOT an `allow_unverified_route` boolean: there is nothing a
caller can set. A caller that cannot mint a capability -- and
`tests/test_broker_bootstrap_capability.py` pins that no ordinary trading
module can -- cannot reach this path at all.

The exception permits ONE order to be sent. It marks NOTHING verified: the
wire-value matrix in `brokers/kis_broker.py` is static and changes only by a
reviewed edit, so the daytime CANCEL legs stay LIVE_RESPONSE_PENDING however
this order turns out.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from brokers import kis_broker as kb  # noqa: E402
from config import session_capability as sc  # noqa: E402
from execution import bootstrap_capability as bootstrap  # noqa: E402
from execution.order_gate import (  # noqa: E402
    ROUTE_UNVERIFIED, BuyGateContext, OrderGateBlockedError, evaluate_buy_gate,
)

from tests import test_order_gate as fixtures  # noqa: E402

DAYTIME = "OVERNIGHT_DAYTIME"
GENERAL_SESSIONS = ("PREMARKET", "REGULAR", "AFTER_HOURS")
SYMBOL = "AAPL"


@pytest.fixture
def bootstrap_env(monkeypatch):
    """The environment a bootstrap genuinely requires.

    The live-entry flags are cleared so `resolve_posture` returns
    LIMITED_LIVE_BOOTSTRAP -- the posture in which the general path stays
    blocked and only the one-shot may act.
    """
    for name in ("KIS_LIVE_ORDER_ENABLED", "LIVE_ROLLOUT_ENABLED",
                 "ENTRY_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIVE_BOOTSTRAP_ENABLED", "true")
    monkeypatch.setenv("LIVE_BOOTSTRAP_ACK", "true")
    return monkeypatch


def _capability(**overrides):
    kwargs = dict(
        mode=bootstrap.MODE_LIMITED_LIVE_BOOTSTRAP,
        symbol=SYMBOL,
        side=bootstrap.BOOTSTRAP_SIDE,
        quantity=bootstrap.BOOTSTRAP_QUANTITY,
        order_type=bootstrap.BOOTSTRAP_ORDER_TYPE,
        allowed_symbols=frozenset({SYMBOL}),
        token="t" * 32,
    )
    kwargs.update(overrides)
    return bootstrap.BootstrapCapability(**kwargs)


def _ctx(session=DAYTIME, capability=None, intent_overrides=None, **overrides):
    intent = dict(session=session, symbol=SYMBOL, side="buy", quantity=1,
                  order_type="limit")
    intent.update(intent_overrides or {})
    kwargs = dict(order_intent=fixtures._order_intent(**intent),
                  allowed_symbols=frozenset({SYMBOL}))
    if capability is not None:
        kwargs["bootstrap_capability"] = capability
    kwargs.update(overrides)
    return fixtures._buy_ctx(**kwargs)


def _blocked(ctx):
    with pytest.raises(OrderGateBlockedError) as caught:
        evaluate_buy_gate(ctx)
    return caught.value


# --- the premise ----------------------------------------------------------

class TestThePremiseStillHolds:
    def test_the_daytime_buy_leg_is_still_unproven(self):
        pending = set(kb.pending_items_for(
            sc.evidence_posture_for_family(kb.FAMILY_DAYTIME)))
        assert "daytime_order_tr_id_live_buy" in pending
        assert sc.route_awaiting_live_evidence(DAYTIME) is True

    def test_the_general_family_needs_no_exception(self):
        assert sc.route_awaiting_live_evidence(kb.FAMILY_GENERAL) is False


# --- A: ordinary orders are unchanged -------------------------------------

class TestOrdinaryOrdersAreUnchanged:
    def test_an_ordinary_daytime_buy_is_still_refused(self):
        """A. The whole reason the gate exists."""
        error = _blocked(_ctx())
        assert error.code == ROUTE_UNVERIFIED
        assert "daytime_order_tr_id_live_buy" in str(error)

    @pytest.mark.parametrize("session", GENERAL_SESSIONS)
    def test_normal_sessions_still_pass_without_any_capability(self, session):
        """K. Normal S6 behaviour is untouched."""
        assert evaluate_buy_gate(_ctx(session=session)) is True

    def test_a_capability_is_not_needed_and_not_consulted_when_verified(self):
        """A verified route never reaches the exception at all."""
        assert evaluate_buy_gate(
            _ctx(session="REGULAR", capability=_capability())) is True


# --- B: the exception, at its exact shape ---------------------------------

class TestTheExceptionAtItsExactShape:
    def test_a_valid_capability_lets_the_daytime_buy_through(self, bootstrap_env):
        """B. The deadlock is broken."""
        assert evaluate_buy_gate(_ctx(capability=_capability())) is True

    def test_it_is_still_the_gate_that_decides(self, bootstrap_env, caplog):
        """The exception is announced, not silent."""
        import logging

        with caplog.at_level(logging.WARNING):
            evaluate_buy_gate(_ctx(capability=_capability()))
        assert any("ROUTE_EVIDENCE_BOOTSTRAP_EXCEPTION" in r.getMessage()
                   for r in caplog.records)


# --- C-H: everything the exception refuses --------------------------------

class TestTheExceptionRefusesEverythingElse:
    def test_wrong_symbol_is_blocked(self, bootstrap_env):
        """C. The capability names ONE symbol."""
        capability = _capability(symbol="MSFT", allowed_symbols=frozenset({"MSFT"}))
        assert _blocked(_ctx(capability=capability)).code == ROUTE_UNVERIFIED

    def test_wrong_quantity_is_blocked(self, bootstrap_env):
        """D. A bootstrap is one share."""
        assert _blocked(_ctx(
            capability=_capability(quantity=2),
            intent_overrides={"quantity": 2})).code == ROUTE_UNVERIFIED

    def test_a_quantity_the_capability_does_not_name_is_blocked(self, bootstrap_env):
        """D. The order and the capability must AGREE, not merely each be
        well-formed."""
        assert _blocked(_ctx(
            capability=_capability(),
            intent_overrides={"quantity": 5})).code == ROUTE_UNVERIFIED

    def test_a_sell_never_reaches_the_exception(self, bootstrap_env):
        """E. The route check returns early for sells; a sell must not be
        able to acquire a route exception by carrying a capability."""
        ctx = _ctx(capability=_capability(side="sell"),
                   intent_overrides={"side": "sell"})
        # Sells are not route-gated at all, so this must not raise
        # ROUTE_UNVERIFIED -- and the capability must not be what saved it.
        try:
            evaluate_buy_gate(ctx)
        except OrderGateBlockedError as exc:
            assert exc.code != ROUTE_UNVERIFIED

    def test_wrong_order_type_cannot_even_be_constructed(self, bootstrap_env):
        """F. Limit only -- enforced in the DOMAIN, before any gate. A
        non-limit bootstrap order cannot be built at all, which is a
        stronger guarantee than refusing it at the route check."""
        from domain.order_intent import OrderIntentError

        with pytest.raises(OrderIntentError):
            _ctx(capability=_capability(order_type="market"),
                 intent_overrides={"order_type": "market"})

    def test_a_limit_order_whose_capability_names_another_type_is_blocked(
            self, bootstrap_env):
        """F. The capability and the order must agree on the type too."""
        assert _blocked(_ctx(
            capability=_capability(order_type="market"))).code == ROUTE_UNVERIFIED

    @pytest.mark.parametrize("session", GENERAL_SESSIONS)
    def test_general_sessions_cannot_use_this_exception(self, bootstrap_env,
                                                        session, monkeypatch):
        """G. Scoped to OVERNIGHT_DAYTIME. Proven by making the GENERAL
        family unverified too and checking the capability does not rescue
        it."""
        monkeypatch.setattr(sc, "route_awaiting_live_evidence", lambda s: True)
        error = _blocked(_ctx(session=session, capability=_capability()))
        assert error.code == ROUTE_UNVERIFIED

    def test_a_revoked_acknowledgement_revokes_the_exception(self, bootstrap_env):
        """H. The environment is re-read at the gate, not trusted from mint."""
        bootstrap_env.setenv("LIVE_BOOTSTRAP_ACK", "false")
        assert _blocked(_ctx(capability=_capability())).code == ROUTE_UNVERIFIED

    def test_a_disabled_bootstrap_flag_revokes_the_exception(self, bootstrap_env):
        bootstrap_env.setenv("LIVE_BOOTSTRAP_ENABLED", "false")
        assert _blocked(_ctx(capability=_capability())).code == ROUTE_UNVERIFIED

    def test_an_object_that_is_not_a_capability_is_blocked(self, bootstrap_env):
        """H. A look-alike does not pass. This is why the field holds an
        object and not a boolean."""
        class _LooksRight:
            mode = bootstrap.MODE_LIMITED_LIVE_BOOTSTRAP
            symbol, side, quantity = SYMBOL, "buy", 1
            order_type = "limit"
            allowed_symbols = frozenset({SYMBOL})
            token = "t" * 32

            def describes(self, _intent):
                return True

        assert _blocked(_ctx(capability=_LooksRight())).code == ROUTE_UNVERIFIED

    def test_a_tokenless_capability_is_blocked(self, bootstrap_env):
        assert _blocked(_ctx(capability=_capability(token=""))).code == ROUTE_UNVERIFIED

    def test_a_stood_down_strategy_gets_no_exception(self, bootstrap_env):
        """The bootstrap is a smaller first order, not an exemption from the
        permission every other order answers to."""
        assert _blocked(_ctx(capability=_capability(),
                             entry_disabled=True)).code is not None

    def test_no_capability_means_no_exception(self, bootstrap_env):
        assert _blocked(_ctx()).code == ROUTE_UNVERIFIED


# --- I: every other gate still applies ------------------------------------

class TestEveryOtherGateStillApplies:
    def test_the_allow_list_still_applies(self, bootstrap_env):
        """I. The exception covers the ROUTE check and nothing else."""
        error = _blocked(_ctx(capability=_capability(),
                              allowed_symbols=frozenset({"NVDA"})))
        assert error.code != ROUTE_UNVERIFIED

    def test_the_account_check_still_applies(self, bootstrap_env):
        error = _blocked(_ctx(capability=_capability(),
                              kis_account_no="99999999"))
        assert error.code == "ACCOUNT"

    def test_reconciliation_still_applies(self, bootstrap_env):
        error = _blocked(_ctx(capability=_capability(), reconciliation=None))
        assert error.code == "RECONCILIATION"

    def test_a_non_kis_broker_still_blocks(self, bootstrap_env):
        error = _blocked(_ctx(capability=_capability(),
                              execution_broker="alpaca"))
        assert error.code != ROUTE_UNVERIFIED


# --- J: nothing becomes verified ------------------------------------------

class TestNothingBecomesVerified:
    def test_the_cancel_legs_stay_pending_after_a_permitted_buy(self, bootstrap_env):
        """J. The exception permits one order. It records no evidence."""
        before = set(kb.pending_items_for(
            sc.evidence_posture_for_family(kb.FAMILY_DAYTIME)))
        evaluate_buy_gate(_ctx(capability=_capability()))
        after = set(kb.pending_items_for(
            sc.evidence_posture_for_family(kb.FAMILY_DAYTIME)))
        assert before == after
        assert {"daytime_cancel_path", "daytime_cancel_tr_id_live"} <= after
        assert sc.route_awaiting_live_evidence(DAYTIME) is True

    def test_the_buy_leg_itself_is_not_marked_verified(self, bootstrap_env):
        evaluate_buy_gate(_ctx(capability=_capability()))
        assert "daytime_order_tr_id_live_buy" in set(kb.pending_items_for(
            sc.evidence_posture_for_family(kb.FAMILY_DAYTIME)))

    def test_a_second_ordinary_buy_is_still_refused_afterwards(self, bootstrap_env):
        """The exception is per-order, not a latch."""
        evaluate_buy_gate(_ctx(capability=_capability()))
        assert _blocked(_ctx()).code == ROUTE_UNVERIFIED


# --- the shape of the change ----------------------------------------------

class TestTheShapeOfTheChange:
    def test_there_is_no_generic_allow_unverified_route_flag(self):
        """No boolean escape hatch exists -- checked against CODE, not
        prose, since the docstrings name the thing they rule out."""
        source = (REPO_ROOT / "execution" / "order_gate.py").read_text()
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith(("#", '"""', "*")))
        for forbidden in ("allow_unverified_route", "skip_route_evidence",
                          "ignore_route_evidence", "force_route"):
            assert forbidden not in code
        fields = set(BuyGateContext.__dataclass_fields__)
        assert not [f for f in fields
                    if "allow" in f and "route" in f or "skip" in f]

    def test_only_the_bootstrap_supplies_the_field(self):
        """Every other context builder must leave it defaulted."""
        import subprocess

        hits = subprocess.run(
            ["grep", "-rn", "bootstrap_capability=", "--include=*.py", "."],
            cwd=REPO_ROOT, capture_output=True, text=True).stdout.splitlines()
        gate_ctx = [h for h in hits
                    if "BuyGateContext" in h or "live_pilot/bootstrap.py" in h]
        assert any("live_pilot/bootstrap.py" in h for h in gate_ctx)
        for hit in hits:
            assert "kis_live_trading.py" not in hit

    def test_the_field_defaults_to_absent(self):
        assert BuyGateContext.__dataclass_fields__[
            "bootstrap_capability"].default is None

    def test_the_exception_is_scoped_to_one_named_session(self):
        from execution import order_gate

        assert order_gate.BOOTSTRAP_ROUTE_SESSION == DAYTIME
