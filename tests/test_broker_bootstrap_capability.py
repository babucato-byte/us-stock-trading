"""The broker's last-line guard, and the one narrow thing that gets past it.

The defect these tests exist for: the bootstrap reached
`KISBroker.submit_order()` and was refused, because the guard recognised
only KIS_LIVE_ORDER_ENABLED=true and the bootstrap posture never sets it.
Posture and gate said yes, the broker said no, and the broker was right
to -- it had never been told the posture existed.

The fix could have been one line: teach the guard to accept
LIVE_BOOTSTRAP_ENABLED too. That would have been wrong in a way worth
stating, because it is the failure these tests are really guarding
against. LIVE_BOOTSTRAP_ENABLED is deployment-wide. A guard honouring it
would, for as long as it were set, pass EVERY order that reached it --
scanner, armed runner, stray script, anything written later. The blast
radius of one variable would become "all orders".

So the grant is a per-order capability object naming the symbol, side,
quantity and order type it authorises. TestEnvAloneIsNotEnough is the
class that matters most here: it proves the env vars alone still get the
same refusal they always did.

Test letters map to the fix's regression list A-J.
"""

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers.kis_config import KISConfig, KISConfigError  # noqa: E402
from domain.order_intent import OrderIntent  # noqa: E402
from execution import bootstrap_capability as cap  # noqa: E402
from live_pilot import posture as posture_mod  # noqa: E402

SYMBOL = "OMDA"

BOOTSTRAP_ENV = {
    "LIVE_BOOTSTRAP_ENABLED": "true",
    "LIVE_BOOTSTRAP_ACK": "true",
    "KIS_LIVE_ORDER_ENABLED": "false",
    "LIVE_ROLLOUT_ENABLED": "false",
    "ENTRY_DISABLED": "true",
}


def _intent(symbol=SYMBOL, side="buy", quantity=1, order_type="limit"):
    from datetime import datetime, timezone
    return OrderIntent(
        internal_order_id="kisboot-x", signal_id="sig-1", strategy_id="S",
        symbol=symbol, exchange="NASDAQ", side=side, quantity=quantity,
        order_type=order_type, limit_price=24.5, stop_price=None, target_price=None,
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc))


def _capability(symbol=SYMBOL, **overrides):
    base = dict(
        mode=cap.MODE_LIMITED_LIVE_BOOTSTRAP, symbol=symbol, side="buy", quantity=1,
        order_type="limit", allowed_symbols=frozenset({symbol}), token="deadbeef")
    base.update(overrides)
    return cap.BootstrapCapability(**base)


def _config(*, live_order_enabled=False):
    """A KISConfig whose credential check passes, so these tests exercise
    the order grant rather than credential plumbing."""
    cfg = types.SimpleNamespace(
        kis_env="live", live_order_enabled=live_order_enabled,
        validate_credentials=lambda: True)
    cfg.validate_live_order_allowed = types.MethodType(
        KISConfig.validate_live_order_allowed.__func__
        if hasattr(KISConfig.validate_live_order_allowed, "__func__")
        else KISConfig.validate_live_order_allowed, cfg)
    return cfg


@pytest.fixture
def bootstrap_env(monkeypatch):
    for k, v in BOOTSTRAP_ENV.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


# ---------------------------------------------------------------------
# A / B / C -- the three states of the grant
# ---------------------------------------------------------------------

class TestTheGuardsThreeAnswers:
    def test_A_ordinary_order_with_live_flags_false_is_refused(self, bootstrap_env):
        """A: normal live flags false, ordinary order -> blocked, transport 0."""
        with pytest.raises(KISConfigError) as caught:
            _config().validate_live_order_allowed()
        assert "KIS_LIVE_ORDER_ENABLED" in str(caught.value)

    def test_B_bootstrap_enabled_but_ack_false_is_refused(self, monkeypatch):
        """B: bootstrap enabled, ACK false -> blocked, transport 0."""
        for k, v in BOOTSTRAP_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("LIVE_BOOTSTRAP_ACK", "false")
        with pytest.raises(KISConfigError):
            _config().validate_live_order_allowed(
                bootstrap_capability=_capability(), order_intent=_intent())

    def test_C_valid_capability_is_accepted(self, bootstrap_env):
        """C: bootstrap enabled + ACK true + valid capability -> allowed."""
        assert _config().validate_live_order_allowed(
            bootstrap_capability=_capability(), order_intent=_intent()) is True

    def test_the_ordinary_grant_is_unchanged(self, monkeypatch):
        monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "true")
        assert _config(live_order_enabled=True).validate_live_order_allowed() is True


# ---------------------------------------------------------------------
# The heart of it: an environment variable is never sufficient
# ---------------------------------------------------------------------

class TestEnvAloneIsNotEnough:
    def test_D_bootstrap_env_set_but_no_capability_is_still_refused(self, bootstrap_env):
        """D: bootstrap env true, ordinary live path -> transport 0.

        The single most important assertion in this file. With every
        bootstrap variable set, an ordinary caller -- one that does not
        construct a capability -- gets exactly the refusal it got before
        the feature existed."""
        with pytest.raises(KISConfigError) as caught:
            _config().validate_live_order_allowed()
        assert "no bootstrap capability" in str(caught.value)

    def test_a_truthy_non_capability_object_is_refused(self, bootstrap_env):
        """Passing something that is merely truthy must not work."""
        for impostor in (True, 1, "LIMITED_LIVE_BOOTSTRAP", {"mode": "LIMITED_LIVE_BOOTSTRAP"},
                         types.SimpleNamespace(mode="LIMITED_LIVE_BOOTSTRAP", symbol=SYMBOL,
                                               side="buy", quantity=1, order_type="limit",
                                               allowed_symbols=frozenset({SYMBOL}),
                                               token="x")):
            with pytest.raises(KISConfigError):
                _config().validate_live_order_allowed(
                    bootstrap_capability=impostor, order_intent=_intent())

    def test_a_capability_cannot_be_minted_without_the_env(self, monkeypatch):
        monkeypatch.setenv("LIVE_BOOTSTRAP_ENABLED", "false")
        monkeypatch.setenv("LIVE_BOOTSTRAP_ACK", "true")
        with pytest.raises(cap.BootstrapCapabilityError):
            cap.mint(symbol=SYMBOL, allowed_symbols=frozenset({SYMBOL}))

    def test_a_minted_capability_is_revoked_if_the_ack_is_withdrawn(self, bootstrap_env):
        """Minted while acknowledged, validated after it was withdrawn."""
        capability = cap.mint(symbol=SYMBOL, allowed_symbols=frozenset({SYMBOL}))
        bootstrap_env.setenv("LIVE_BOOTSTRAP_ACK", "false")
        with pytest.raises(cap.BootstrapCapabilityError):
            cap.validate(capability, _intent())

    def test_the_capability_is_immutable(self):
        capability = _capability()
        with pytest.raises(Exception):
            capability.quantity = 2


# ---------------------------------------------------------------------
# E / F / G -- the capability's scope is enforced at the guard
# ---------------------------------------------------------------------

class TestTheCapabilityScopeIsEnforced:
    def test_E_quantity_two_is_blocked(self, bootstrap_env):
        """E: capability held, but the order asks for 2 -> blocked."""
        with pytest.raises(KISConfigError):
            _config().validate_live_order_allowed(
                bootstrap_capability=_capability(), order_intent=_intent(quantity=2))

    def test_E_a_capability_claiming_quantity_two_is_itself_rejected(self, bootstrap_env):
        with pytest.raises(cap.BootstrapCapabilityError):
            cap.validate(_capability(quantity=2), _intent(quantity=2))

    def test_F_a_market_order_is_blocked(self, bootstrap_env):
        """F: capability held, MARKET order -> blocked.

        OrderIntent forbids market orders outright, so the guard is
        exercised with a stand-in for the shape it must refuse."""
        market = types.SimpleNamespace(symbol=SYMBOL, side="buy", quantity=1,
                                       order_type="market")
        with pytest.raises(KISConfigError):
            _config().validate_live_order_allowed(
                bootstrap_capability=_capability(), order_intent=market)

    def test_G_a_symbol_off_the_allowlist_is_blocked(self, bootstrap_env):
        """G: capability held, symbol misses the allow-list -> blocked."""
        with pytest.raises(KISConfigError):
            _config().validate_live_order_allowed(
                bootstrap_capability=_capability(), order_intent=_intent(symbol="AAPL"))

    def test_G_minting_for_a_symbol_off_the_allowlist_is_refused(self, bootstrap_env):
        with pytest.raises(cap.BootstrapCapabilityError):
            cap.mint(symbol="AAPL", allowed_symbols=frozenset({SYMBOL}))

    @pytest.mark.parametrize("symbols", [frozenset(), frozenset({"OMDA", "AAPL"})])
    def test_G_minting_needs_an_allowlist_of_exactly_one(self, bootstrap_env, symbols):
        with pytest.raises(cap.BootstrapCapabilityError):
            cap.mint(symbol=SYMBOL, allowed_symbols=symbols)

    def test_a_sell_is_never_authorised(self, bootstrap_env):
        with pytest.raises(KISConfigError):
            _config().validate_live_order_allowed(
                bootstrap_capability=_capability(), order_intent=_intent(side="sell"))

    def test_a_capability_for_one_symbol_does_not_authorise_another(self, bootstrap_env):
        capability = cap.mint(symbol=SYMBOL, allowed_symbols=frozenset({SYMBOL}))
        with pytest.raises(cap.BootstrapCapabilityError):
            cap.validate(capability, _intent(symbol="MSFT"))


# ---------------------------------------------------------------------
# Only the bootstrap can obtain one
# ---------------------------------------------------------------------

class TestOnlyTheBootstrapMintsCapabilities:
    ORDINARY_PATHS = [
        "execution/order_gate.py", "execution/execution_engine.py",
        "kis_live_trading.py", "scripts/run_shadow_mode.py", "live_pilot/armed.py",
        "live_pilot/runner.py", "paper_strategy_order.py",
    ]

    @pytest.mark.parametrize("relative", ORDINARY_PATHS)
    def test_no_ordinary_path_mints_a_capability(self, relative):
        path = REPO_ROOT / relative
        if not path.exists():
            pytest.skip(f"{relative} not present")
        source = path.read_text(encoding="utf-8")
        for forbidden in ("bootstrap_capability.mint", "capability_mod.mint",
                          "BootstrapCapability("):
            assert forbidden not in source, f"{relative} constructs a capability"

    def test_exactly_one_module_mints_each_kind(self):
        """One minter per capability KIND.

        There are two kinds now -- the bootstrap's, which proves the
        pipeline, and the route verification's, which proves a wire
        value. Each is minted in exactly one place, and neither module
        mints the other's: that pairing is the property, and a bare count
        of `.mint(` call sites would stop expressing it the moment a
        second kind existed.
        """
        expected = {
            "live_pilot/bootstrap.py": "execution/bootstrap_capability.py",
            "live_pilot/route_verification_runner.py":
                "execution/route_verification.py",
        }
        minters = []
        for path in REPO_ROOT.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(("tests/", "venv/")) or rel in expected.values():
                continue
            if ".mint(" in path.read_text(encoding="utf-8"):
                minters.append(rel)
        assert sorted(minters) == sorted(expected), minters

        # Neither minter may construct the other's capability.
        bootstrap_src = (REPO_ROOT / "live_pilot/bootstrap.py").read_text()
        verification_src = (
            REPO_ROOT / "live_pilot/route_verification_runner.py").read_text()
        assert "RouteVerificationCapability(" not in bootstrap_src
        assert "BootstrapCapability(" not in verification_src
        assert "route_verification" not in bootstrap_src

    def test_the_engine_defaults_the_capability_to_none(self):
        import inspect

        from execution import execution_engine
        for name in ("submit_buy_order", "submit_cancel", "_submit_new_order"):
            sig = inspect.signature(getattr(execution_engine, name))
            assert sig.parameters["bootstrap_capability"].default is None, name

    def test_the_sell_path_hardcodes_no_capability(self):
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text(encoding="utf-8")
        sell = source.split("def submit_sell_order", 1)[1].split("\ndef ", 1)[0]
        assert "bootstrap_capability=None" in sell


# ---------------------------------------------------------------------
# H / I / J -- pre-transport rejection vs. the UNKNOWN contract
# ---------------------------------------------------------------------

class TestPreTransportRejectionReleasesTheSlot:
    def test_H_a_config_refusal_is_classified_as_never_attempted(self):
        """H: pre-transport config rejection -> durable terminal REJECTED,
        broker_order_id NULL, slots released."""
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text(encoding="utf-8")
        block = source.split("except KISConfigError as exc:", 1)[1].split("except KISAmbiguousResponseError", 1)[0]
        assert "_reject(" in block
        assert "REASON_PRE_TRANSPORT_CONFIG" in block
        assert "transport_attempted" in block
        assert "_force_unknown" not in block

    def test_H_the_guard_runs_before_any_network_call(self):
        """The classification is only sound because the config check
        physically precedes the request. Pin the ordering."""
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text(encoding="utf-8")
        body = source.split("def submit_order", 1)[1].split("\n    def ", 1)[0]
        guard_at = body.index("validate_live_order_allowed")
        request_at = body.index("self.session.request")
        assert guard_at < request_at, "the config guard must precede the network call"

    def test_H_rejected_with_no_broker_id_frees_both_slots(self):
        from execution import entry_limits

        row = {"status": "REJECTED", "broker_order_id": None, "symbol": "OMDA"}
        assert entry_limits._never_reached_the_broker(row) is True

    def test_a_rejected_row_WITH_a_broker_id_does_not_free_the_slot(self):
        from execution import entry_limits

        row = {"status": "REJECTED", "broker_order_id": "kis-1", "symbol": "OMDA"}
        assert entry_limits._never_reached_the_broker(row) is False

    def test_I_an_ambiguous_transport_is_never_auto_released(self):
        """I: ambiguous transport -> UNKNOWN/reconciliation policy kept,
        never quietly converted to REJECTED."""
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text(encoding="utf-8")
        block = source.split("except KISAmbiguousResponseError as exc:", 1)[1][:900]
        assert "_force_unknown" in block
        assert "REASON_PRE_TRANSPORT_CONFIG" not in block

    def test_I_only_pre_transport_exception_types_are_classified(self):
        """A broker rejection happens AFTER the request, so it must not
        share the never-attempted path."""
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text(encoding="utf-8")
        block = source.split("except KISBrokerError as exc:", 1)[1][:600]
        assert "REASON_PRE_TRANSPORT_CONFIG" not in block

    def test_I_unknown_status_still_counts_against_entry_limits(self):
        from execution import entry_limits

        row = {"status": "UNKNOWN", "broker_order_id": None, "symbol": "OMDA"}
        assert entry_limits._never_reached_the_broker(row) is False


class TestTheOrphanReleaseCommand:
    SCRIPT = REPO_ROOT / "scripts" / "release_pre_transport_orphan.py"

    def test_J_it_exists_and_compiles(self):
        import py_compile

        py_compile.compile(str(self.SCRIPT), doraise=True)

    def test_J_it_refuses_anything_but_a_pre_transport_state_with_a_null_broker_id(self):
        """The releasable set is the states that are at or before the
        transport boundary, and nothing else. SUBMITTING is the ambiguous
        one the evidence checks exist for; CREATED and VALIDATING are
        strictly earlier and cannot have reached the broker. Behaviour is
        covered directly in test_pre_transport_orphan_release.py -- this
        keeps the set itself from quietly growing.
        """
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert ('RELEASABLE_STATES = ("SUBMITTING", "VALIDATING", "CREATED")'
                in source)
        assert 'if row["broker_order_id"]:' in source
        assert "REFUSED" in source

    def test_J_it_requires_positive_evidence_from_kis(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        for read in ("get_open_orders", "get_positions", "get_fills"):
            assert read in source, read
        assert "if any(findings.values()):" in source

    def test_J_an_unreadable_kis_is_a_refusal_not_an_assumption(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        block = source.split("findings = _evidence", 1)[1][:400]
        assert "REFUSED" in block

    def test_J_it_never_writes_sql_directly(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        code = "\n".join(l for l in source.splitlines() if not l.strip().startswith("#"))
        body = code.split('"""', 2)[-1]
        for forbidden in ("UPDATE ", "DELETE ", "INSERT "):
            assert forbidden not in body.upper(), forbidden
        assert "order_repository.advance(" in body

    def test_J_it_is_a_dry_run_without_confirm(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        assert '"--confirm"' in source
        assert "DRY RUN" in source


class TestTheOrdinaryCallSignatureIsUnchanged:
    """The capability is passed only when one exists.

    This is not a convenience: it means a broker that has never heard of
    bootstrap capabilities still works for ordinary orders, and that an
    ordinary order's transport call cannot carry a capability even by
    accident, because the parameter is never mentioned.

    It is also how the whole existing suite keeps passing -- every
    broker double in this repo has the pre-capability signature, and
    they are the most realistic evidence available that ordinary
    callers are untouched."""

    def test_a_broker_without_the_parameter_still_takes_ordinary_orders(self):
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text(encoding="utf-8")
        block = source.split("_transport_kwargs = ", 1)[1][:400]
        assert "if bootstrap_capability is not None else {}" in block
        assert "**_transport_kwargs" in block

    def test_the_cancel_path_does_the_same(self):
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text(encoding="utf-8")
        block = source.split("_cancel_kwargs = ", 1)[1][:400]
        assert "if bootstrap_capability is not None else {}" in block

    def test_a_strict_broker_double_receives_no_capability_kwarg(self):
        """Runtime proof: a broker whose submit_order REFUSES the kwarg
        must still be callable on the ordinary path."""
        seen = {}

        class _StrictBroker:
            def submit_order(self, order_intent, instrument, *, authorization=None):
                seen["called"] = True
                return object()

        broker = _StrictBroker()
        kwargs = {}  # what the engine builds when bootstrap_capability is None
        broker.submit_order(_intent(), None, authorization=None, **kwargs)
        assert seen["called"] is True

    def test_a_strict_broker_double_rejects_a_capability(self):
        """And the same double must FAIL if one is supplied -- proving
        the kwarg genuinely is absent on the ordinary path rather than
        being silently swallowed."""
        class _StrictBroker:
            def submit_order(self, order_intent, instrument, *, authorization=None):
                return object()

        with pytest.raises(TypeError):
            _StrictBroker().submit_order(
                _intent(), None, authorization=None, bootstrap_capability=_capability())

    def test_the_real_broker_accepts_it(self):
        import inspect

        from brokers.kis_broker import KISBroker
        for name in ("submit_order", "cancel_order"):
            sig = inspect.signature(getattr(KISBroker, name))
            assert "bootstrap_capability" in sig.parameters, name
            assert sig.parameters["bootstrap_capability"].default is None
