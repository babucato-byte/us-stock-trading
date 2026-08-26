"""The bootstrap places exactly one real order, or none at all.

Every test here answers one question: under condition X, how many times
does the wire get touched? The answer is counted, not asserted about --
`_FakeBroker.submit_calls` is incremented inside the fake transport, so
a test that says "transport 0" is reading what actually happened rather
than trusting a return value.

The conditions come from the two ways this can go wrong:

  * an order goes out when it should not have (OBSERVE posture, missing
    acknowledgement, dirty reconciliation, a position already held, the
    day's entry already used, an allow-list that is not exactly one
    symbol, no cash, no KIS live Slack channel);
  * an order goes out TWICE (a retry after a timeout, a rejection, an
    ambiguous response, or a caller that loops).

The second is the dangerous one, because the first order may already be
live. `BootstrapTransportGuard` makes it structurally impossible rather
than merely unlikely, and TestTheBuyTransportIsCappedStructurally is
where that is proven -- including the case where the first attempt
raised.
"""

import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError  # noqa: E402
from config.live_rollout_config import LiveRolloutConfig  # noqa: E402
from domain.order_intent import OrderIntent  # noqa: E402
from live_pilot import bootstrap, candidate_sources  # noqa: E402
from live_pilot import posture as posture_mod  # noqa: E402

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
SYMBOL = "AAPL"


# ---------------------------------------------------------------------
# Fakes. None of them reach a network; the guard is the real thing.
# ---------------------------------------------------------------------

class _Position:
    def __init__(self, symbol, quantity):
        self.symbol = symbol
        self.quantity = quantity


class _ExecutionRecord:
    def __init__(self, status="ACCEPTED", broker_order_id="kis-boot-1"):
        self.status = status
        self.broker_order_id = broker_order_id


class _FakeBroker:
    """Counts transports. Reads are configurable; writes are counted."""

    def __init__(self, *, positions=(), open_orders=(), submit_raises=None,
                 submit_record=None):
        self._positions = list(positions)
        self._open_orders = list(open_orders)
        self._submit_raises = submit_raises
        self._submit_record = submit_record or _ExecutionRecord()
        self.submit_calls = 0
        self.cancel_calls = 0

    # -- reads
    def get_positions(self):
        return list(self._positions)

    def get_open_orders(self):
        return list(self._open_orders)

    def get_orderable_usd(self, instrument, limit_price):
        return 10_000.0

    # -- writes
    def submit_order(self, order_intent, instrument, **kwargs):
        self.submit_calls += 1
        if self._submit_raises is not None:
            raise self._submit_raises
        return self._submit_record

    def cancel_order(self, *args, **kwargs):
        self.cancel_calls += 1
        return _ExecutionRecord(status="CANCELLED")


def _rollout(**overrides):
    base = dict(
        enabled=False, allowed_symbols=frozenset({SYMBOL}), max_quantity_per_order=1,
        max_open_positions=2, max_positions_per_strategy=1, max_daily_entries=1,
    )
    base.update(overrides)
    return types.SimpleNamespace(
        max_price_deviation_percent=3.0, regular_session_only=True,
        validate=lambda: None, **base)


def _order_intent(symbol=SYMBOL, quantity=1, side="buy", order_type="limit"):
    return OrderIntent(
        internal_order_id="kisboot-test-1", signal_id="sig-1",
        strategy_id=bootstrap.BOOTSTRAP_STRATEGY_ID, symbol=symbol, exchange="NASDAQ",
        side=side, quantity=quantity, order_type=order_type, limit_price=100.0,
        stop_price=None, target_price=None, created_at=NOW)


def _limits(*, positions=0, entries=0, strategy_positions=0):
    """The account as the bootstrap re-check sees it.

    `positions` is the GLOBAL occupancy and `strategy_positions` is this
    strategy's own -- two different caps since S1 and S6 both went live,
    and the whole point of the split is that S1 holding one no longer
    blocks S6.
    """
    return types.SimpleNamespace(
        effective_position_count=positions, daily_entry_count=entries,
        max_open_positions=2, max_positions_per_strategy=1,
        max_daily_entries=1,
        strategy_effective_count=lambda slot: strategy_positions,
        strategy_symbols_for=lambda slot: frozenset(),
        unattributed_symbols=frozenset())


SAFE_ENV = {
    "LIVE_BOOTSTRAP_ENABLED": "true",
    "LIVE_BOOTSTRAP_ACK": "true",
    "KIS_ENV": "live",
    "DEPLOYED_COMMIT": "abc123",
    "VALIDATED_COMMIT": "abc123",
    "KIS_ALLOWED_ACCOUNT_NO": "12345678",
    "KIS_LIVE_ORDER_ENABLED": "false",
    "LIVE_ROLLOUT_ENABLED": "false",
    "ENTRY_DISABLED": "true",
}


@pytest.fixture
def published(tmp_path, monkeypatch):
    """A candidate set published for today, the way the scanner would.

    select_candidate() now refuses to act on candidates that are not
    today's and do not name the allow-listed symbol, so any test that
    reaches it has to publish first."""
    from market_data import candidate_store
    from market_hours import us_trading_day

    monkeypatch.setenv(candidate_store.CANDIDATE_DIR_ENV, str(tmp_path / "candidates"))

    def _publish(symbol=SYMBOL, *, trading_day=None, generated_at=None):
        csv_bytes = ("symbol,price,score\n%s,10.0,100\n" % symbol).encode()
        return candidate_store.publish(
            csv_bytes,
            trading_day=trading_day or us_trading_day(NOW),
            generated_at=generated_at or NOW)

    _publish()
    return _publish


@pytest.fixture(autouse=True)
def _no_state_outside_tmp(tmp_path, monkeypatch):
    """Nothing in this file may write to the repository.

    `run_bootstrap_buy` creates a position lifecycle row on success, and
    the real store defaults to POSITION_STORE.json at the repo root. A
    test that leaves that behind is a test that has quietly become a
    writer -- and it was caught exactly that way here."""
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TRADING_STATE.db"))


@pytest.fixture
def all_clear(monkeypatch):
    """Every fact the re-check reads, set to the state that permits an
    order. Individual tests break exactly one of them."""
    monkeypatch.setattr(bootstrap, "head_commit", lambda: "abc123")
    monkeypatch.setattr(bootstrap, "working_tree_dirty", lambda: False)
    # The re-check no longer asks `pso.get_us_market_session`; it asks
    # `_order_session()` -- "can a real order be routed right now".
    # Pinned here so the whole class stays deterministic: left to the
    # real clock, every test below would depend on the session the
    # suite happens to run in.
    monkeypatch.setattr(bootstrap, "_order_session", lambda *a, **k: "REGULAR")
    monkeypatch.setattr(bootstrap.freshness, "evaluate", lambda: types.SimpleNamespace(age_seconds=1.0))
    monkeypatch.setattr(bootstrap.ops_kill_switch, "is_halted", lambda: False)
    monkeypatch.setattr(bootstrap.ops_kill_switch, "is_entry_allowed", lambda: True)
    monkeypatch.setattr(bootstrap.entry_limits, "collect",
                        lambda **kw: _limits())
    monkeypatch.setattr(bootstrap.idempotency, "count_unknown_orders", lambda conn: 0)
    import slack_utils
    monkeypatch.setattr(slack_utils, "kis_live_notifications_configured", lambda: True)
    return monkeypatch


def _recheck(env=None, *, broker=None, rollout=None, order_intent=None):
    return bootstrap.final_safety_recheck(
        broker=broker or _FakeBroker(), conn=object(),
        rollout=rollout or _rollout(), order_intent=order_intent or _order_intent(),
        now=NOW, env={**SAFE_ENV, **(env or {})})


# ---------------------------------------------------------------------
# A / B / C -- posture, acknowledgement, and the one permitted order
# ---------------------------------------------------------------------

class TestPostureAndAcknowledgement:
    def test_A_observe_posture_blocks_with_zero_transport(self, all_clear):
        """A: OBSERVE -> transport 0."""
        env = {**SAFE_ENV, "LIVE_BOOTSTRAP_ENABLED": "false"}
        assert posture_mod.resolve_posture(env).posture == posture_mod.POSTURE_OBSERVE
        reasons = _recheck({"LIVE_BOOTSTRAP_ENABLED": "false"})
        assert bootstrap.POSTURE_NOT_BOOTSTRAP in reasons

    def test_B_enabled_but_no_acknowledgement_blocks(self, all_clear):
        """B: BOOTSTRAP enabled + ACK false -> transport 0.

        Readiness may be reached without the ack (that is the point of
        the READY -> ack -> BUY ordering); EXECUTION may not."""
        reasons = _recheck({"LIVE_BOOTSTRAP_ACK": "false"})
        assert bootstrap.BOOTSTRAP_ACK_MISSING in reasons

    def test_B_missing_acknowledgement_is_as_blocking_as_a_false_one(self, all_clear):
        env = dict(SAFE_ENV)
        env.pop("LIVE_BOOTSTRAP_ACK")
        reasons = bootstrap.final_safety_recheck(
            broker=_FakeBroker(), conn=object(), rollout=_rollout(),
            order_intent=_order_intent(), now=NOW, env=env)
        assert bootstrap.BOOTSTRAP_ACK_MISSING in reasons

    def test_C_everything_clear_permits_the_order(self, all_clear):
        """C: all preconditions pass -> the re-check raises nothing."""
        assert _recheck() == []

    def test_C_a_clear_recheck_leads_to_exactly_one_transport(self, all_clear):
        """The counted version of C: one fake BUY, no more."""
        broker = _FakeBroker()
        guard = bootstrap.BootstrapTransportGuard(broker)
        guard.submit_order(_order_intent(), object())
        assert broker.submit_calls == 1
        assert guard.submit_calls == 1


# ---------------------------------------------------------------------
# D -- the capability does not leak into the ordinary paths
# ---------------------------------------------------------------------

class TestTheBootstrapCapabilityIsIsolated:
    """D: the same environment that authorises the bootstrap must leave
    the scanner and the live runner placing nothing.

    Enforced by absence: no ordinary trading module may read the
    bootstrap flag at all. A module that merely *could* consult it is one
    edit away from treating it as permission to trade."""

    ORDINARY_PATHS = [
        "execution/order_gate.py", "execution/execution_engine.py",
        "kis_live_trading.py", "scripts/run_shadow_mode.py", "live_pilot/armed.py",
    ]

    @pytest.mark.parametrize("relative", ORDINARY_PATHS)
    def test_D_no_ordinary_trading_path_reads_the_bootstrap_flag(self, relative):
        path = REPO_ROOT / relative
        if not path.exists():
            pytest.skip(f"{relative} not present in this tree")
        source = path.read_text(encoding="utf-8")
        for flag in (posture_mod.FLAG_BOOTSTRAP_ENABLED, bootstrap.FLAG_BOOTSTRAP_ACK):
            assert flag not in source, f"{relative} reads {flag}"

    def test_D_the_bootstrap_flag_does_not_enable_the_live_rollout(self):
        rollout = LiveRolloutConfig.from_env({
            "LIVE_BOOTSTRAP_ENABLED": "true", "LIVE_BOOTSTRAP_ACK": "true",
            "LIVE_ROLLOUT_ENABLED": "false",
        })
        assert rollout.enabled is False


# ---------------------------------------------------------------------
# E / F -- one transport, whatever the outcome
# ---------------------------------------------------------------------

class TestTheBuyTransportIsCappedStructurally:
    def test_E_a_timeout_does_not_refund_the_budget(self):
        """E: timeout -> BUY 1, retry 0.

        The budget is spent BEFORE the call, so a transport that raised
        still counts. "We are not sure it went through" is the strongest
        possible reason not to send another."""
        broker = _FakeBroker(submit_raises=KISAmbiguousResponseError("timeout"))
        guard = bootstrap.BootstrapTransportGuard(broker)
        with pytest.raises(KISAmbiguousResponseError):
            guard.submit_order(_order_intent(), object())
        assert broker.submit_calls == 1
        with pytest.raises(bootstrap.BootstrapTransportBudgetExceeded):
            guard.submit_order(_order_intent(), object())
        assert broker.submit_calls == 1  # the second never reached the fake

    def test_F_a_rejection_does_not_refund_the_budget(self):
        """F: reject -> BUY 1, retry 0."""
        broker = _FakeBroker(submit_raises=KISBrokerError("rejected"))
        guard = bootstrap.BootstrapTransportGuard(broker)
        with pytest.raises(KISBrokerError):
            guard.submit_order(_order_intent(), object())
        with pytest.raises(bootstrap.BootstrapTransportBudgetExceeded):
            guard.submit_order(_order_intent(), object())
        assert broker.submit_calls == 1

    def test_a_caller_that_loops_still_sends_once(self):
        broker = _FakeBroker()
        guard = bootstrap.BootstrapTransportGuard(broker)
        sent = 0
        for _ in range(5):
            try:
                guard.submit_order(_order_intent(), object())
                sent += 1
            except bootstrap.BootstrapTransportBudgetExceeded:
                pass
        assert sent == 1
        assert broker.submit_calls == 1

    def test_the_budget_refuses_before_the_network_not_after(self):
        """The exception must be raised INSTEAD of the call, which is the
        entire difference between a cap and a log line."""
        broker = _FakeBroker()
        guard = bootstrap.BootstrapTransportGuard(broker)
        guard.submit_order(_order_intent(), object())
        try:
            guard.submit_order(_order_intent(), object())
        except bootstrap.BootstrapTransportBudgetExceeded:
            pass
        assert broker.submit_calls == 1

    def test_reads_pass_through_untouched(self):
        """Only the two wire verbs are intercepted; the engine must still
        get the real broker's facts."""
        broker = _FakeBroker(positions=[_Position(SYMBOL, 3)])
        guard = bootstrap.BootstrapTransportGuard(broker)
        assert [p.quantity for p in guard.get_positions()] == [3]
        assert guard.get_orderable_usd(object(), 100.0) == 10_000.0

    def test_the_shape_is_re_asserted_one_statement_before_the_wire(self):
        broker = _FakeBroker()
        guard = bootstrap.BootstrapTransportGuard(broker)
        # A market order cannot even be CONSTRUCTED -- OrderIntent
        # forbids it outright -- so the guard is exercised with the two
        # shapes that are constructible plus a bare stand-in for the one
        # that is not.
        market_shaped = types.SimpleNamespace(side="buy", quantity=1, order_type="market")
        for bad in (_order_intent(quantity=2), _order_intent(side="sell"), market_shaped):
            with pytest.raises(bootstrap.BootstrapBlocked):
                guard.submit_order(bad, object())
        assert broker.submit_calls == 0


class TestQuantityIsNotConfigurable:
    def test_the_quantity_is_a_module_constant_of_one(self):
        assert bootstrap.BOOTSTRAP_QUANTITY == 1
        assert bootstrap.BOOTSTRAP_SIDE == "buy"
        assert bootstrap.BOOTSTRAP_ORDER_TYPE == "limit"

    def test_no_environment_variable_can_change_it(self):
        source = (REPO_ROOT / "live_pilot" / "bootstrap.py").read_text(encoding="utf-8")
        body = source.split("BOOTSTRAP_QUANTITY = 1", 1)[1]
        assert "BOOTSTRAP_QUANTITY =" not in body, "quantity is reassigned somewhere"

    def test_cash_sizing_can_only_veto_never_enlarge(self, monkeypatch, published):
        """Affordability is a veto, not an input. Even with cash for
        hundreds of shares the order is one share."""
        seen = {}

        def _fake_analyze(symbol):
            return {"score": 99, "price": 10.0}

        monkeypatch.setattr(bootstrap.pso, "analyze_stock", _fake_analyze)
        monkeypatch.setattr(candidate_sources, "build_kis_instrument",
                            lambda s: (types.SimpleNamespace(exchange="NASDAQ", symbol=s), None))
        monkeypatch.setattr(candidate_sources, "build_signal", lambda **kw: types.SimpleNamespace(
            signal_id="sig-1", strategy_id=kw["strategy_id"], signal_price=kw["signal_price"],
            entry_reason=kw["entry_reason"]))

        class _Quote:
            price_usd = 10.0

        monkeypatch.setattr(bootstrap, "KISValidationProvider",
                            lambda broker, instrument_lookup: types.SimpleNamespace(
                                get_price_quote=lambda s: _Quote()))
        broker = _FakeBroker()
        candidate = bootstrap.select_candidate(
            broker=broker, rollout=_rollout(), deployed_commit="abc123", now=NOW)
        seen["affordable"] = candidate.affordable_shares
        assert seen["affordable"] >= 100  # cash for many
        assert candidate.as_dict()["quantity"] == 1  # order is still one


# ---------------------------------------------------------------------
# G .. L -- each blocking precondition, one at a time
# ---------------------------------------------------------------------

class TestEachPreconditionBlocksOnItsOwn:
    def test_G_stale_or_dirty_reconciliation_blocks(self, all_clear):
        def _boom():
            raise RuntimeError("SnapshotUnusable: stale")

        all_clear.setattr(bootstrap.freshness, "evaluate", _boom)
        assert bootstrap.RECONCILIATION_NOT_USABLE in _recheck()

    def test_H_an_entry_already_used_today_blocks(self, all_clear):
        all_clear.setattr(bootstrap.entry_limits, "collect",
                          lambda **kw: _limits(entries=1))
        assert bootstrap.DAILY_ENTRIES_NOT_ZERO in _recheck()

    def test_I_a_position_already_held_blocks(self, all_clear):
        """The ACCOUNT being full blocks -- global occupancy at the cap."""
        all_clear.setattr(bootstrap.entry_limits, "collect",
                          lambda **kw: _limits(positions=2))
        assert bootstrap.POSITIONS_NOT_ZERO in _recheck()

    def test_I2_this_strategy_already_trading_blocks(self, all_clear):
        """Distinct from the account being full: the account has room,
        but this strategy already holds its one position, so the
        bootstrap would be its SECOND entry rather than its first."""
        all_clear.setattr(bootstrap.entry_limits, "collect",
                          lambda **kw: _limits(positions=1, strategy_positions=1))
        assert bootstrap.STRATEGY_POSITIONS_NOT_ZERO in _recheck()

    def test_I3_another_strategy_holding_one_does_not_block(self, all_clear):
        """The regression this split exists to prevent: S1 holding TX
        must not make the S6 bootstrap unreachable."""
        all_clear.setattr(bootstrap.entry_limits, "collect",
                          lambda **kw: _limits(positions=1, strategy_positions=0))
        reasons = _recheck()
        assert bootstrap.POSITIONS_NOT_ZERO not in reasons
        assert bootstrap.STRATEGY_POSITIONS_NOT_ZERO not in reasons

    @pytest.mark.parametrize("symbols", [frozenset(), frozenset({"AAPL", "MSFT"})])
    def test_J_an_allowlist_that_is_not_exactly_one_blocks(self, all_clear, symbols):
        reasons = _recheck(rollout=_rollout(allowed_symbols=symbols))
        assert bootstrap.LIVE_ALLOWLIST_NOT_EXACTLY_ONE in reasons

    def test_J_a_symbol_off_the_allowlist_blocks(self, all_clear):
        reasons = _recheck(rollout=_rollout(allowed_symbols=frozenset({"MSFT"})))
        assert bootstrap.SYMBOL_NOT_ALLOWLISTED in reasons

    def test_J_selection_refuses_an_allowlist_that_is_not_exactly_one(self):
        for symbols in (frozenset(), frozenset({"AAPL", "MSFT"})):
            with pytest.raises(bootstrap.BootstrapBlocked) as caught:
                bootstrap.select_candidate(
                    broker=_FakeBroker(), rollout=_rollout(allowed_symbols=symbols),
                    deployed_commit="abc123", now=NOW)
            assert bootstrap.LIVE_ALLOWLIST_NOT_EXACTLY_ONE in caught.value.reason_codes

    def test_K_insufficient_cash_blocks_with_zero_transport(self, monkeypatch, published):
        monkeypatch.setattr(bootstrap.pso, "analyze_stock",
                            lambda s: {"score": 99, "price": 500.0})
        monkeypatch.setattr(candidate_sources, "build_kis_instrument",
                            lambda s: (types.SimpleNamespace(exchange="NASDAQ", symbol=s), None))
        monkeypatch.setattr(candidate_sources, "build_signal", lambda **kw: types.SimpleNamespace(
            signal_id="sig-1", strategy_id=kw["strategy_id"], signal_price=kw["signal_price"],
            entry_reason=kw["entry_reason"]))

        class _Quote:
            price_usd = 500.0

        monkeypatch.setattr(bootstrap, "KISValidationProvider",
                            lambda broker, instrument_lookup: types.SimpleNamespace(
                                get_price_quote=lambda s: _Quote()))
        broker = _FakeBroker()
        broker.get_orderable_usd = lambda instrument, price: 12.5  # under one share
        with pytest.raises(bootstrap.BootstrapBlocked) as caught:
            bootstrap.select_candidate(broker=broker, rollout=_rollout(),
                                       deployed_commit="abc123", now=NOW)
        assert bootstrap.INSUFFICIENT_CASH in caught.value.reason_codes
        assert broker.submit_calls == 0

    def test_L_missing_kis_live_slack_blocks(self, all_clear):
        """L: an order whose entire lifecycle -- including an UNKNOWN --
        would go nowhere must not be placed."""
        import slack_utils
        all_clear.setattr(slack_utils, "kis_live_notifications_configured", lambda: False)
        assert bootstrap.KIS_LIVE_NOTIFICATION_NOT_CONFIGURED in _recheck()

    def test_a_halted_system_blocks(self, all_clear):
        all_clear.setattr(bootstrap.ops_kill_switch, "is_halted", lambda: True)
        assert bootstrap.HALT_ACTIVE in _recheck()

    def test_entry_off_blocks(self, all_clear):
        all_clear.setattr(bootstrap.ops_kill_switch, "is_entry_allowed", lambda: False)
        assert bootstrap.ENTRY_NOT_ALLOWED in _recheck()

    def test_an_unresolved_unknown_order_blocks(self, all_clear):
        all_clear.setattr(bootstrap.idempotency, "count_unknown_orders", lambda conn: 1)
        assert bootstrap.UNRESOLVED_UNKNOWN_ORDERS in _recheck()

    def test_a_resting_open_order_blocks(self, all_clear):
        broker = _FakeBroker(open_orders=[{"pdno": SYMBOL}])
        assert bootstrap.OPEN_ORDERS_NOT_ZERO in _recheck(broker=broker)

    def test_a_dirty_working_tree_blocks(self, all_clear):
        all_clear.setattr(bootstrap, "working_tree_dirty", lambda: True)
        assert bootstrap.WORKING_TREE_DIRTY in _recheck()

    def test_a_commit_mismatch_blocks(self, all_clear):
        assert bootstrap.COMMIT_MISMATCH in _recheck({"VALIDATED_COMMIT": "deadbeef"})

    def test_a_session_with_no_order_route_blocks(self, all_clear):
        """Not "is it REGULAR" any more, but "can an order be routed
        right now". A session S6 may not order in, or one KIS defines
        no endpoint for, blocks -- rather than being served a guessed
        route to an endpoint that is not open at that hour."""
        all_clear.setattr(bootstrap, "_order_session", lambda *a, **k: None)
        assert bootstrap.NOT_ORDERABLE_SESSION in _recheck()

    def test_a_paper_environment_blocks(self, all_clear):
        assert bootstrap.KIS_ENV_NOT_LIVE in _recheck({"KIS_ENV": "paper"})

    def test_a_widened_rollout_limit_blocks(self, all_clear):
        """The limits this one-shot pins: one share, one entry today,
        one position for the strategy placing it.

        The GLOBAL cap is deliberately not among them -- it is the sum
        across live strategies, so pinning it would pin the number of
        live strategies to one.
        """
        for widened in ({"max_positions_per_strategy": 2},
                        {"max_daily_entries": 2},
                        {"max_quantity_per_order": 2}):
            assert bootstrap.ROLLOUT_LIMIT_NOT_ONE in _recheck(
                rollout=_rollout(**widened)), widened

    def test_a_wider_global_cap_alone_does_not_block(self, all_clear):
        """Two strategies live at one position each is the authorised
        posture, not a widened limit."""
        assert bootstrap.ROLLOUT_LIMIT_NOT_ONE not in _recheck(
            rollout=_rollout(max_open_positions=2))

    def test_every_unreadable_fact_is_a_block_not_a_pass(self, all_clear):
        """Fail-closed: an exception while reading a safety fact must
        produce a reason code, never silence."""
        def _boom(**kwargs):
            raise RuntimeError("db down")

        all_clear.setattr(bootstrap.entry_limits, "collect", _boom)
        assert bootstrap.SAFETY_STATE_UNREADABLE in _recheck()


class TestTheScannerThresholdIsNotLowered:
    def test_M_a_below_threshold_candidate_is_refused(self, monkeypatch, published):
        monkeypatch.setattr(bootstrap.pso, "analyze_stock",
                            lambda s: {"score": bootstrap.SCORE_THRESHOLD - 1, "price": 10.0})
        with pytest.raises(bootstrap.BootstrapBlocked) as caught:
            bootstrap.select_candidate(broker=_FakeBroker(), rollout=_rollout(),
                                       deployed_commit="abc", now=NOW)
        assert bootstrap.NO_QUALIFYING_CANDIDATE in caught.value.reason_codes

    def test_no_symbol_can_be_supplied_from_outside(self):
        """The symbol comes from the allow-list, never from a caller --
        a bootstrap that ordered whatever it was told would be testing
        the operator, not the pipeline."""
        import inspect
        params = inspect.signature(bootstrap.select_candidate).parameters
        assert "symbol" not in params
        assert "symbol" not in inspect.signature(bootstrap.run_bootstrap_buy).parameters

    def test_the_threshold_matches_the_production_cycle(self):
        import kis_live_trading
        assert bootstrap.SCORE_THRESHOLD == kis_live_trading.SCORE_THRESHOLD


# ---------------------------------------------------------------------
# M / N -- what the pending matrix entries do and do not permit
# ---------------------------------------------------------------------

class TestPendingWireValuesDoNotBlockTheBootstrap:
    def test_M_paper_only_evidence_pending_does_not_block_the_bootstrap(self):
        """M: `cancel_tr_id_paper` is PAPER_EVIDENCE_ONLY. `_env_key()`
        selects the LIVE cancel TR whenever KIS_ENV=live, so no live
        order can read it -- blocking a live bootstrap on evidence about
        a code path a live order cannot take buys nothing."""
        from brokers.kis_broker import REQUIRED_FOR_ARMED, pending_items_for
        armed_pending = set(pending_items_for(REQUIRED_FOR_ARMED))
        assert "cancel_tr_id_paper" not in armed_pending

    def test_N_the_general_route_has_nothing_outstanding(self):
        """N, resolved. The five live-only values WERE what stood
        between this deployment and ARMED, and the 2026-08-26 premarket
        bootstrap confirmed them from a real response.

        The property this protects is unchanged: nothing may be pending
        BEYOND those five. It is asserted the same way -- if some other
        general-route value were outstanding it would show up here.
        """
        from brokers.kis_broker import REQUIRED_FOR_ARMED, pending_items_for

        live_only = {"order_path", "order_tr_id_live_buy", "cancel_path",
                     "cancel_tr_id_live", "cancel_price_field_rule"}
        pending = set(pending_items_for(REQUIRED_FOR_ARMED))
        assert pending <= live_only, f"pending beyond the live-only five: {pending - live_only}"
        assert pending == set(), f"unexpectedly still pending: {pending}"

    def test_N_the_daytime_route_still_has_its_own_five(self):
        """The bootstrap still has a purpose -- for the OTHER route. One
        family's live response confirms nothing about the other's."""
        from brokers.kis_broker import REQUIRED_FOR_DAYTIME, pending_items_for

        assert list(pending_items_for(REQUIRED_FOR_DAYTIME))


# ---------------------------------------------------------------------
# Cancel A .. D
# ---------------------------------------------------------------------

class TestCancelOnlyTouchesAnOrderKISCallsOpen:
    def _result(self, guard, *, broker_order_id="kis-boot-1"):
        return bootstrap.BootstrapResult(
            candidate=types.SimpleNamespace(
                symbol=SYMBOL, instrument=types.SimpleNamespace(exchange="NASDAQ"),
                signal=types.SimpleNamespace(signal_id="sig-1")),
            order_intent=_order_intent(),
            execution_result=types.SimpleNamespace(
                internal_order_id="kisboot-test-1", status="ACCEPTED",
                execution_record=_ExecutionRecord(broker_order_id=broker_order_id)),
            guard=guard)

    def test_cancel_A_a_filled_buy_is_never_cancelled(self):
        """A: BUY filled -> CANCEL 0."""
        broker = _FakeBroker()
        guard = bootstrap.BootstrapTransportGuard(broker)
        outcome = bootstrap.cancel_if_open(
            conn=object(), result=self._result(guard),
            verification={"conclusion": "FILLED"},
            order_intent=_order_intent(), account_id="12345678")
        assert outcome["cancelled"] is False
        assert outcome["reason_code"] == bootstrap.CANCEL_NOT_OPEN_AT_BROKER
        assert broker.cancel_calls == 0

    @pytest.mark.parametrize("conclusion", ["FILLED", "PARTIALLY_FILLED", "INDETERMINATE"])
    def test_cancel_A_only_open_unfilled_authorises_the_call(self, conclusion):
        broker = _FakeBroker()
        guard = bootstrap.BootstrapTransportGuard(broker)
        outcome = bootstrap.cancel_if_open(
            conn=object(), result=self._result(guard),
            verification={"conclusion": conclusion},
            order_intent=_order_intent(), account_id="12345678")
        assert outcome["cancelled"] is False
        assert broker.cancel_calls == 0

    def test_cancel_A_no_broker_order_id_means_no_cancel(self):
        broker = _FakeBroker()
        guard = bootstrap.BootstrapTransportGuard(broker)
        outcome = bootstrap.cancel_if_open(
            conn=object(), result=self._result(guard, broker_order_id=None),
            verification={"conclusion": "OPEN_UNFILLED"},
            order_intent=_order_intent(), account_id="12345678")
        assert outcome["cancelled"] is False
        assert broker.cancel_calls == 0

    def test_cancel_B_an_open_order_is_cancelled_at_most_once(self, monkeypatch):
        """B: BUY open at KIS -> CANCEL <= 1."""
        broker = _FakeBroker(open_orders=[{"pdno": SYMBOL}])
        guard = bootstrap.BootstrapTransportGuard(broker)
        calls = []

        def _fake_submit_cancel(**kwargs):
            calls.append(kwargs)
            kwargs["broker"].cancel_order()
            return types.SimpleNamespace(status="CANCELLED")

        monkeypatch.setattr(bootstrap.execution_engine, "submit_cancel", _fake_submit_cancel)
        succeeded = 0
        for _ in range(3):
            try:
                bootstrap.cancel_if_open(
                    conn=object(), result=self._result(guard),
                    verification={"conclusion": "OPEN_UNFILLED"},
                    order_intent=_order_intent(), account_id="12345678")
                succeeded += 1
            except bootstrap.BootstrapTransportBudgetExceeded:
                # Deliberately propagated rather than swallowed: a second
                # cancel attempt is a bug the operator must see.
                pass
        assert succeeded == 1
        assert broker.cancel_calls == 1, "the cancel budget was exceeded"

    def test_cancel_B_goes_through_the_engine_not_the_broker(self, monkeypatch):
        broker = _FakeBroker(open_orders=[{"pdno": SYMBOL}])
        guard = bootstrap.BootstrapTransportGuard(broker)
        seen = {}

        def _fake_submit_cancel(**kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(status="CANCELLED")

        monkeypatch.setattr(bootstrap.execution_engine, "submit_cancel", _fake_submit_cancel)
        bootstrap.cancel_if_open(
            conn=object(), result=self._result(guard),
            verification={"conclusion": "OPEN_UNFILLED"},
            order_intent=_order_intent(), account_id="12345678")
        assert seen["broker"] is guard, "the engine must hold the budgeted guard"
        assert seen["audit_run_id"]
        # The gate context is built fresh, from a live read.
        ctx = seen["cancel_gate_context_builder"]()
        assert ctx.is_actually_open is True
        assert ctx.broker_order_id == "kis-boot-1"

    def test_cancel_B_a_fill_between_verify_and_cancel_still_stops_it(self, monkeypatch):
        """The gate re-reads rather than trusting the earlier
        verification, so a fill that lands in between is caught."""
        broker = _FakeBroker(open_orders=[])  # filled since verification
        guard = bootstrap.BootstrapTransportGuard(broker)
        seen = {}
        monkeypatch.setattr(bootstrap.execution_engine, "submit_cancel",
                            lambda **kw: seen.update(kw))
        bootstrap.cancel_if_open(
            conn=object(), result=self._result(guard),
            verification={"conclusion": "OPEN_UNFILLED"},
            order_intent=_order_intent(), account_id="12345678")
        assert seen["cancel_gate_context_builder"]().is_actually_open is False

    def test_cancel_C_a_timeout_is_not_retried(self, monkeypatch):
        """C: cancel timeout -> retry 0."""
        broker = _FakeBroker(open_orders=[{"pdno": SYMBOL}])
        guard = bootstrap.BootstrapTransportGuard(broker)

        def _timeout(**kwargs):
            kwargs["broker"].cancel_order()
            raise KISAmbiguousResponseError("cancel timed out")

        monkeypatch.setattr(bootstrap.execution_engine, "submit_cancel", _timeout)
        outcome = bootstrap.cancel_if_open(
            conn=object(), result=self._result(guard),
            verification={"conclusion": "OPEN_UNFILLED"},
            order_intent=_order_intent(), account_id="12345678")
        assert outcome["cancelled"] is False
        assert broker.cancel_calls == 1

    def test_cancel_D_an_ambiguous_cancel_keeps_the_engines_policy(self, monkeypatch):
        """D: this module adds no cancel-specific UNKNOWN handling. It
        reports the failure and stops; the durable state the engine wrote
        stands."""
        broker = _FakeBroker(open_orders=[{"pdno": SYMBOL}])
        guard = bootstrap.BootstrapTransportGuard(broker)
        monkeypatch.setattr(
            bootstrap.execution_engine, "submit_cancel",
            lambda **kw: (_ for _ in ()).throw(KISAmbiguousResponseError("ambiguous")))
        outcome = bootstrap.cancel_if_open(
            conn=object(), result=self._result(guard),
            verification={"conclusion": "OPEN_UNFILLED"},
            order_intent=_order_intent(), account_id="12345678")
        assert outcome["reason_code"] == "CANCEL_FAILED"
        assert "KISAmbiguousResponseError" in outcome["detail"]

    def test_no_order_is_ever_created_in_order_to_cancel_one(self):
        source = (REPO_ROOT / "live_pilot" / "bootstrap.py").read_text(encoding="utf-8")
        cancel_section = source.split("def cancel_if_open", 1)[1]
        for forbidden in ("submit_buy_order", "OrderIntent(", "run_bootstrap_buy"):
            assert forbidden not in cancel_section, forbidden


# ---------------------------------------------------------------------
# Verification -- ACCEPTED is not a fill
# ---------------------------------------------------------------------

class TestVerificationAsksKISRatherThanTheSubmitResponse:
    def _result(self, broker):
        guard = bootstrap.BootstrapTransportGuard(broker)
        return bootstrap.BootstrapResult(
            candidate=types.SimpleNamespace(
                symbol=SYMBOL, instrument=types.SimpleNamespace(exchange="NASDAQ"),
                signal=types.SimpleNamespace(signal_id="sig-1")),
            order_intent=_order_intent(),
            execution_result=types.SimpleNamespace(
                internal_order_id="kisboot-test-1", status="ACCEPTED",
                execution_record=_ExecutionRecord()),
            guard=guard)

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn

    def test_accepted_with_a_resting_order_is_not_a_fill(self, monkeypatch):
        broker = _FakeBroker(open_orders=[{"pdno": SYMBOL}], positions=[])
        monkeypatch.setattr(bootstrap.idempotency, "find_existing", lambda *a, **k: None)
        observed = bootstrap.verify_buy(broker=broker, conn=self._conn(),
                                        result=self._result(broker))
        assert observed["submit_status"] == "ACCEPTED"
        assert observed["conclusion"] == "OPEN_UNFILLED"

    def test_a_held_position_with_no_resting_order_is_a_fill(self, monkeypatch):
        broker = _FakeBroker(open_orders=[], positions=[_Position(SYMBOL, 1)])
        monkeypatch.setattr(bootstrap.idempotency, "find_existing", lambda *a, **k: None)
        observed = bootstrap.verify_buy(broker=broker, conn=self._conn(),
                                        result=self._result(broker))
        assert observed["conclusion"] == "FILLED"

    def test_both_present_reads_as_partially_filled(self, monkeypatch):
        broker = _FakeBroker(open_orders=[{"pdno": SYMBOL}], positions=[_Position(SYMBOL, 1)])
        monkeypatch.setattr(bootstrap.idempotency, "find_existing", lambda *a, **k: None)
        observed = bootstrap.verify_buy(broker=broker, conn=self._conn(),
                                        result=self._result(broker))
        assert observed["conclusion"] == "PARTIALLY_FILLED"

    def test_neither_present_is_indeterminate_not_success(self, monkeypatch):
        broker = _FakeBroker(open_orders=[], positions=[])
        monkeypatch.setattr(bootstrap.idempotency, "find_existing", lambda *a, **k: None)
        observed = bootstrap.verify_buy(broker=broker, conn=self._conn(),
                                        result=self._result(broker))
        assert observed["conclusion"] == "INDETERMINATE"

    def test_an_unreadable_step_is_reported_not_raised(self, monkeypatch):
        broker = _FakeBroker()
        broker.get_positions = lambda: (_ for _ in ()).throw(KISBrokerError("read failed"))
        monkeypatch.setattr(bootstrap.idempotency, "find_existing", lambda *a, **k: None)
        observed = bootstrap.verify_buy(broker=broker, conn=self._conn(),
                                        result=self._result(broker))
        assert "unavailable" in str(observed["kis_positions"])
        assert observed["conclusion"] == "INDETERMINATE"

    def test_verification_touches_no_wire_verb(self):
        broker = _FakeBroker()
        result = self._result(broker)
        bootstrap.verify_buy(broker=broker, conn=self._conn(), result=result)
        assert broker.submit_calls == 0
        assert broker.cancel_calls == 0


# ---------------------------------------------------------------------
# The UNKNOWN contract
# ---------------------------------------------------------------------

class TestUnknownIsTerminal:
    def test_an_ambiguous_buy_raises_a_terminal_error_and_is_not_retried(self, monkeypatch):
        seen = {"submits": 0}

        def _ambiguous(**kwargs):
            seen["submits"] += 1
            raise KISAmbiguousResponseError("response lost")

        monkeypatch.setattr(bootstrap.execution_engine, "submit_buy_order", _ambiguous)
        monkeypatch.setattr(bootstrap, "select_candidate", lambda **kw: types.SimpleNamespace(
            symbol=SYMBOL, instrument=types.SimpleNamespace(exchange="NASDAQ"),
            signal=types.SimpleNamespace(signal_id="sig-1",
                                         strategy_id=bootstrap.BOOTSTRAP_STRATEGY_ID),
            limit_price=100.0, kis_price_usd=100.0, orderable_usd=1000.0,
            affordable_shares=10, analysis={"score": 99}))
        with pytest.raises(bootstrap.BootstrapUnknownOrder):
            bootstrap.run_bootstrap_buy(
                broker=_FakeBroker(), conn=object(), rollout=_rollout(),
                now=NOW, env=SAFE_ENV)
        assert seen["submits"] == 1

    def test_the_unknown_error_carries_no_retry_affordance(self):
        import inspect
        source = inspect.getsource(bootstrap.BootstrapUnknownOrder)
        for forbidden in ("retry", "resubmit", "again"):
            assert f"def {forbidden}" not in source

    def test_the_runner_maps_unknown_to_its_own_exit_code(self):
        source = (REPO_ROOT / "scripts" / "run_limited_live_bootstrap.py").read_text(
            encoding="utf-8")
        assert "BootstrapUnknownOrder" in source
        assert "RETRY" in source and "BLOCKED" in source
        assert "RECONCILIATION_REQUIRED" in source
        assert "NEW_ENTRY_BLOCKED" in source
        assert "return 3" in source


# ---------------------------------------------------------------------
# No bypass of anything the ordinary path does
# ---------------------------------------------------------------------

class TestNothingIsBypassed:
    SOURCE = (REPO_ROOT / "live_pilot" / "bootstrap.py").read_text(encoding="utf-8")

    def test_the_only_transport_is_the_execution_engine(self):
        """The module may hold a guard that DELEGATES submit_order, but it
        must never originate one against a raw broker."""
        assert "execution_engine.submit_buy_order(" in self.SOURCE
        assert "execution_engine.submit_cancel(" in self.SOURCE
        assert "KISBroker(" not in self.SOURCE
        for forbidden in ("requests.post", "requests.get", "urlopen", "http"):
            assert forbidden not in self.SOURCE.lower().replace("https://", ""), forbidden

    def test_the_engine_receives_the_guard_not_the_naked_broker(self):
        # rsplit: the module docstring names the function too, and the
        # last occurrence is the call site.
        submit_call = self.SOURCE.rsplit("execution_engine.submit_buy_order(", 1)[1][:400]
        assert "broker=guard" in submit_call

    def test_the_gate_context_is_built_not_faked(self):
        assert "order_gate.BuyGateContext(" in self.SOURCE
        assert "entry_limits.collect(" in self.SOURCE
        # The reconciliation snapshot comes FROM the engine, never from here.
        assert "reconciliation=reconciliation" in self.SOURCE

    def test_the_recheck_runs_inside_the_gate_context_builder(self):
        """So it sees the same instant the gate judges, while the engine
        holds the single-run lock -- and so a failure blocks through the
        ordinary gate path with the transport count still at zero."""
        builder = self.SOURCE.split("def _buy_ctx_builder", 1)[1].split(
            "def ", 1)[0]
        assert "final_safety_recheck(" in builder
        assert "raise BootstrapBlocked" in builder

    def test_no_safety_flag_is_written_anywhere(self):
        for forbidden in ("os.environ[", "putenv", "ENTRY_DISABLED =",
                          "KIS_LIVE_ORDER_ENABLED ="):
            assert forbidden not in self.SOURCE, forbidden


class TestARecheckFailureUsesTheEnginesOwnRejectionPath:
    """A bespoke exception from the gate context builder would leave the
    durable row in VALIDATING -- indistinguishable, to reconciliation,
    from a process that died mid-order. Raising the engine's own
    OrderGateBlockedError instead means the row is CAS'd to REJECTED and
    a GATE_REJECTED audit event is written, with the transport count
    still at zero."""

    def _run_with(self, monkeypatch, recheck_reasons):
        import kis_position_manager
        from execution import order_gate

        # A successful fake order still reaches the position-tracking
        # call; stub it so this class tests only the gate path.
        monkeypatch.setattr(kis_position_manager, "create_kis_position_after_buy",
                            lambda **kw: None)

        monkeypatch.setattr(bootstrap, "select_candidate", lambda **kw: types.SimpleNamespace(
            symbol=SYMBOL, instrument=types.SimpleNamespace(exchange="NASDAQ"),
            signal=types.SimpleNamespace(signal_id="sig-1",
                                         strategy_id=bootstrap.BOOTSTRAP_STRATEGY_ID),
            limit_price=100.0, kis_price_usd=100.0, orderable_usd=1000.0,
            affordable_shares=10, analysis={"score": 99}))
        monkeypatch.setattr(bootstrap, "final_safety_recheck",
                            lambda **kw: list(recheck_reasons))
        monkeypatch.setattr(bootstrap.entry_limits, "collect", lambda **kw: _limits())

        captured = {}

        def _engine(**kwargs):
            captured["broker"] = kwargs["broker"]
            try:
                kwargs["buy_gate_context_builder"](object())
            except order_gate.OrderGateBlockedError as exc:
                captured["gate_error"] = exc
                # Exactly what the engine does with it.
                from execution.execution_engine import ExecutionEngineError
                raise ExecutionEngineError(
                    f"buy order blocked by order gate: {exc}",
                    reason_code=f"GATE:{exc.code}") from exc
            return types.SimpleNamespace(status="ACCEPTED", internal_order_id="x",
                                         execution_record=_ExecutionRecord())

        monkeypatch.setattr(bootstrap.execution_engine, "submit_buy_order", _engine)
        return captured

    def test_the_builder_raises_the_engines_gate_error(self, monkeypatch):
        captured = self._run_with(monkeypatch, [bootstrap.HALT_ACTIVE])
        broker = _FakeBroker()
        with pytest.raises(bootstrap.BootstrapBlocked) as caught:
            bootstrap.run_bootstrap_buy(broker=broker, conn=object(),
                                        rollout=_rollout(), now=NOW, env=SAFE_ENV)
        assert captured["gate_error"].code == "BOOTSTRAP_RECHECK"
        assert bootstrap.HALT_ACTIVE in caught.value.reason_codes
        assert broker.submit_calls == 0

    def test_every_failing_reason_reaches_the_caller(self, monkeypatch):
        reasons = [bootstrap.HALT_ACTIVE, bootstrap.POSITIONS_NOT_ZERO,
                   bootstrap.KIS_LIVE_NOTIFICATION_NOT_CONFIGURED]
        self._run_with(monkeypatch, reasons)
        with pytest.raises(bootstrap.BootstrapBlocked) as caught:
            bootstrap.run_bootstrap_buy(broker=_FakeBroker(), conn=object(),
                                        rollout=_rollout(), now=NOW, env=SAFE_ENV)
        assert set(caught.value.reason_codes) == set(reasons)

    def test_a_clear_recheck_lets_the_order_through(self, monkeypatch):
        captured = self._run_with(monkeypatch, [])
        broker = _FakeBroker()
        result = bootstrap.run_bootstrap_buy(broker=broker, conn=object(),
                                             rollout=_rollout(), now=NOW, env=SAFE_ENV)
        assert result.status == "ACCEPTED"
        assert "gate_error" not in captured
        assert isinstance(captured["broker"], bootstrap.BootstrapTransportGuard)

    def test_the_order_intent_the_engine_receives_is_one_share(self, monkeypatch):
        import kis_position_manager

        seen = {}
        monkeypatch.setattr(kis_position_manager, "create_kis_position_after_buy",
                            lambda **kw: None)

        monkeypatch.setattr(bootstrap, "select_candidate", lambda **kw: types.SimpleNamespace(
            symbol=SYMBOL, instrument=types.SimpleNamespace(exchange="NASDAQ"),
            signal=types.SimpleNamespace(signal_id="sig-1",
                                         strategy_id=bootstrap.BOOTSTRAP_STRATEGY_ID),
            limit_price=100.0, kis_price_usd=100.0, orderable_usd=1_000_000.0,
            affordable_shares=10_000, analysis={"score": 99}))
        monkeypatch.setattr(bootstrap.execution_engine, "submit_buy_order",
                            lambda **kw: seen.update(kw) or types.SimpleNamespace(
                                status="ACCEPTED", internal_order_id="x",
                                execution_record=_ExecutionRecord()))
        bootstrap.run_bootstrap_buy(broker=_FakeBroker(), conn=object(),
                                    rollout=_rollout(), now=NOW, env=SAFE_ENV)
        intent = seen["order_intent"]
        assert (intent.side, intent.quantity, intent.order_type) == ("buy", 1, "limit")
        assert intent.symbol == SYMBOL


class TestTheBootstrapPositionIsManagedLikeAnyOther:
    """PHASE 9: the bootstrap share becomes the first LIMITED LIVE
    position, so exit / SELL / SELL-fill / reconciliation can run against
    it. Without the lifecycle row it would sit unmanaged, and the SELL
    half of the wire matrix would stay unobservable."""

    def _candidate(self):
        return types.SimpleNamespace(
            symbol=SYMBOL, instrument=types.SimpleNamespace(exchange="NASDAQ"),
            signal=types.SimpleNamespace(signal_id="sig-1",
                                         strategy_id=bootstrap.BOOTSTRAP_STRATEGY_ID),
            limit_price=100.0, kis_price_usd=100.0, orderable_usd=1000.0,
            affordable_shares=10, analysis={"score": 99})

    def _patch_engine(self, monkeypatch):
        monkeypatch.setattr(bootstrap, "select_candidate", lambda **kw: self._candidate())
        monkeypatch.setattr(bootstrap.execution_engine, "submit_buy_order",
                            lambda **kw: types.SimpleNamespace(
                                status="ACCEPTED", internal_order_id="x",
                                execution_record=_ExecutionRecord()))

    def test_a_lifecycle_row_is_created_after_a_successful_buy(self, monkeypatch):
        import kis_position_manager

        seen = {}
        self._patch_engine(monkeypatch)
        monkeypatch.setattr(kis_position_manager, "create_kis_position_after_buy",
                            lambda **kw: seen.update(kw))
        bootstrap.run_bootstrap_buy(broker=_FakeBroker(), conn=object(),
                                    rollout=_rollout(), now=NOW, env=SAFE_ENV)
        assert seen["symbol"] == SYMBOL
        assert seen["quantity"] == 1
        assert seen["broker_order_id"] == "kis-boot-1"

    def test_a_tracking_failure_does_not_turn_a_placed_order_into_an_error(
            self, monkeypatch):
        """The order already reached KIS. Raising here would report a
        successful order as failed -- and invite a re-run."""
        import kis_position_manager

        self._patch_engine(monkeypatch)
        monkeypatch.setattr(
            kis_position_manager, "create_kis_position_after_buy",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
        result = bootstrap.run_bootstrap_buy(broker=_FakeBroker(), conn=object(),
                                             rollout=_rollout(), now=NOW, env=SAFE_ENV)
        assert result.status == "ACCEPTED"

    def test_no_row_is_created_when_the_order_was_blocked(self, monkeypatch):
        import kis_position_manager

        calls = []
        monkeypatch.setattr(kis_position_manager, "create_kis_position_after_buy",
                            lambda **kw: calls.append(kw))
        monkeypatch.setattr(bootstrap, "select_candidate", lambda **kw: (
            _ for _ in ()).throw(bootstrap.BootstrapBlocked(
                "no candidate", reason_codes=(bootstrap.NO_QUALIFYING_CANDIDATE,))))
        with pytest.raises(bootstrap.BootstrapBlocked):
            bootstrap.run_bootstrap_buy(broker=_FakeBroker(), conn=object(),
                                        rollout=_rollout(), now=NOW, env=SAFE_ENV)
        assert calls == []

    def test_the_daily_entry_cap_counts_the_bootstrap_attempt(self):
        """The engine registers the idempotency row before transport, and
        entry_limits counts registered attempts -- so a bootstrap BUY
        that reached the broker consumes the day's single entry and a
        second one is refused by the ordinary limit, not by a
        bootstrap-specific rule."""
        import inspect

        from execution import entry_limits as el
        source = inspect.getsource(el)
        assert "_never_reached_the_broker" in source
        assert "daily_entry_count" in source


class TestWireEvidenceIsNeverManufactured:
    """PHASE 7: the five live-only values become confirmed when a real
    response shows them, and by no other route.

    The temptation this closes is specific: the TR IDs are in KIS's
    documentation and in this repo's own constants, so "confirming" them
    from a constant is one edit away and looks like progress. It would
    mean the ARMED matrix asserts evidence that does not exist."""

    LIVE_ONLY = ("order_path", "order_tr_id_live_buy", "cancel_path",
                 "cancel_tr_id_live", "cancel_price_field_rule")

    def test_they_are_confirmed_only_against_an_observed_response(self):
        """A real response has now shown them, so "still pending" is no
        longer the property to assert -- PROVENANCE is.

        The temptation this closes is unchanged: the TR IDs sit in KIS's
        documentation and in this repo's own constants, so confirming
        them from a constant is one edit away and looks like progress.
        So each of the five must be confirmed AND must cite what was
        actually seen on the wire -- an order id -- rather than a path
        into the reference repo.
        """
        from brokers.kis_broker import (
            LIVE_RESPONSE_CONFIRMED, REQUIRED_FOR_ARMED, matrix_entries_for,
        )

        entries = {e.name: e for e in matrix_entries_for(REQUIRED_FOR_ARMED)}
        for name in self.LIVE_ONLY:
            entry = entries[name]
            assert entry.live_status == LIVE_RESPONSE_CONFIRMED, name
            # Observed, not documented.
            assert "odno=" in entry.source, f"{name} cites no observed order"
            assert "examples_" not in entry.source, \
                f"{name} is confirmed from documentation, not a response"

    def test_the_daytime_five_are_not_confirmed_by_the_general_response(self):
        """One route's evidence is not another's. The daytime family is a
        different endpoint and a different TR family, and confirming it
        from a general-route response is the same error as confirming
        from documentation -- evidence that does not exist."""
        from brokers.kis_broker import REQUIRED_FOR_DAYTIME, pending_items_for

        pending = set(pending_items_for(REQUIRED_FOR_DAYTIME))
        assert pending == {
            "daytime_order_path", "daytime_order_tr_id_live_buy",
            "daytime_order_tr_id_live_sell", "daytime_cancel_path",
            "daytime_cancel_tr_id_live"}

    def test_the_bootstrap_does_not_write_to_the_verification_matrix(self):
        source = (REPO_ROOT / "live_pilot" / "bootstrap.py").read_text(encoding="utf-8")
        for forbidden in ("LIVE_RESPONSE_CONFIRMED", "VERIFICATION_MATRIX",
                          "matrix_entries_for", "mark_confirmed"):
            assert forbidden not in source, forbidden

    def test_the_runner_only_reminds_and_never_confirms(self):
        source = (REPO_ROOT / "scripts" / "run_limited_live_bootstrap.py").read_text(
            encoding="utf-8")
        assert "LIVE_RESPONSE_CONFIRMED" not in source
        assert "OBSERVED" in source.upper()

    def test_the_bootstrap_never_asserts_a_tr_id_of_its_own(self):
        """It must not carry its own copy of a TR ID to compare against --
        a value asserted from inside the codebase is not evidence."""
        source = (REPO_ROOT / "live_pilot" / "bootstrap.py").read_text(encoding="utf-8")
        for tr_id in ("TTTT1002U", "TTTT1004U", "TTTS3012R", "TTTS3007R"):
            assert tr_id not in source, tr_id
