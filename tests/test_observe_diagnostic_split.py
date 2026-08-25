"""OBSERVE separates "may this be ordered?" from "are the remaining
safety gates healthy?".

The problem
-----------
The live allow-list (LIVE_ROLLOUT_ALLOWED_SYMBOLS) is a hard gate, and
correctly so: nothing may be ordered for a symbol an operator has not
authorized. But it sits at SYMBOL, ahead of INSTRUMENT, RECONCILIATION,
MAX_OPEN_POSITIONS and MAX_DAILY_ENTRIES -- so while the list is empty,
which is the whole read-only posture, every OBSERVE evaluation stops
there and those four gates are never observed at all. Two Oracle sessions
showed exactly that: `furthest_gate=DUPLICATE_SIGNAL, stopped_at=SYMBOL`.

The split
---------
`order_gate.evaluate_buy_gate_diagnostic()` reports both axes. The live
axis is unchanged -- the allow-list still blocks. The diagnostic axis
re-runs the SAME gate with the allow-list substituted in a copy of the
context, so the downstream gates get evaluated without any configuration
being touched and without `evaluate_buy_gate` gaining a bypass.

What must never happen, and is tested here:
* a diagnostic pass being reported as an approval,
* SHADOW_ALLOWED_SYMBOLS widening what ARMED may order,
* the diagnostic causing any side effect.
"""
import dataclasses
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import shadow_audit
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution import entry_limits, order_gate
from execution.entry_limits import EntryLimitState
from execution.idempotency import _LOCK_FILE  # noqa: F401 -- patched per test
from market_hours import us_trading_day
from reconciliation.snapshot import ReconciliationSnapshot
from state_store import db as state_db

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

NOW = datetime(2026, 8, 7, 17, 30, tzinfo=timezone.utc)
TODAY = us_trading_day(NOW)
ACCOUNT = "12345678"


def _limits(**overrides):
    kwargs = dict(
        max_open_positions=1, max_daily_entries=1,
        open_position_symbols=frozenset(), pending_entry_symbols=frozenset(),
        daily_entry_count=0, trading_day=TODAY)
    kwargs.update(overrides)
    return EntryLimitState(**kwargs)


def _snapshot(symbol="IOVA", **overrides):
    kwargs = dict(
        account_id=ACCOUNT, symbol=symbol, checked_at=NOW, positions_match=True,
        open_orders_match=True, fills_match=True, has_unknown_orders=False,
        source="test", detail=())
    kwargs.update(overrides)
    return ReconciliationSnapshot(**kwargs)


def _ctx(*, symbol="IOVA", allowed=frozenset(), limits=None, snapshot=None,
         exchange="NASDAQ", **instrument_kwargs):
    instrument = build_instrument(symbol, exchange=exchange, **instrument_kwargs)
    signal = build_signal(
        strategy_id="S1_HMA_EARLY_TREND_V1", strategy_version="v1", config_version="c", code_commit="c1",
        symbol=symbol, exchange=exchange, signal_price=100.0, score=99,
        entry_reason="test", valid_for_seconds=300, now=NOW)
    intent = OrderIntent(
        internal_order_id="ord-1", signal_id=signal.signal_id, strategy_id="S1_HMA_EARLY_TREND_V1",
        symbol=symbol, exchange=exchange, side="buy", quantity=1, order_type="limit",
        limit_price=100.0, stop_price=None, target_price=None, created_at=NOW)
    return order_gate.BuyGateContext(
        execution_broker="kis", live_order_enabled=True, entry_disabled=False,
        validated_commit="c1", deployed_commit="c1", kis_account_no=ACCOUNT,
        allowed_account_no=ACCOUNT, order_intent=intent, instrument=instrument,
        signal=signal, is_regular_session=True, kis_price_usd=100.0,
        max_price_deviation_percent=30.0, usd_orderable_cash=10_000.0,
        has_open_order_for_symbol=False, has_order_for_signal_id=False,
        allowed_symbols=allowed,
        reconciliation=snapshot if snapshot is not None else _snapshot(symbol),
        entry_limits=limits if limits is not None else _limits(), now=NOW)


# ---------------------------------------------------------------------
# The evaluator itself
# ---------------------------------------------------------------------
class TestTheTwoAxes:
    def test_an_empty_allowlist_blocks_live_and_still_diagnoses(self):
        result = order_gate.evaluate_buy_gate_diagnostic(_ctx(allowed=frozenset()))
        assert result.live_allowlist_allowed is False
        assert result.live_authorization_result == "LIVE_BLOCKED:SYMBOL"
        assert result.diagnostic_result == order_gate.DIAGNOSTIC_PASS
        assert result.diagnostic_furthest_gate == entry_limits.MAX_DAILY_ENTRIES

    def test_an_allow_listed_symbol_reports_both_as_clean(self):
        result = order_gate.evaluate_buy_gate_diagnostic(_ctx(allowed=frozenset({"IOVA"})))
        assert result.live_allowlist_allowed is True
        assert result.live_authorization_result == "WOULD_APPROVE"
        assert result.diagnostic_result == order_gate.DIAGNOSTIC_PASS

    def test_a_diagnostic_pass_is_never_called_approved(self):
        """§6: a symbol the live list does not hold must not produce a
        string an operator could read as authorization."""
        result = order_gate.evaluate_buy_gate_diagnostic(_ctx(allowed=frozenset()))
        assert "APPROVED" not in result.diagnostic_result
        assert result.diagnostic_result == "DIAGNOSTIC_PASS"
        assert "BLOCKED" in result.live_authorization_result

    def test_a_block_before_the_allowlist_is_reported_on_both_axes(self):
        """Nothing to look past: the diagnostic must not silently improve
        on a verdict the allow-list had no part in."""
        ctx = dataclasses.replace(_ctx(allowed=frozenset({"IOVA"})),
                                  is_regular_session=False)
        result = order_gate.evaluate_buy_gate_diagnostic(ctx)
        assert result.live_authorization_result == "LIVE_BLOCKED:SESSION"
        assert result.diagnostic_result == "DIAGNOSTIC_BLOCKED:SESSION"

    def test_both_verdicts_survive_when_they_differ(self):
        """§18: an allow-list miss must not cost the reconciliation
        verdict, and the reconciliation problem must not hide the miss."""
        result = order_gate.evaluate_buy_gate_diagnostic(_ctx(
            allowed=frozenset(), snapshot=_snapshot(positions_match=False)))
        assert result.live_authorization_result == "LIVE_BLOCKED:SYMBOL"
        assert result.diagnostic_result == "DIAGNOSTIC_BLOCKED:RECONCILIATION"

    def test_the_instrument_gate_still_stops_the_diagnostic(self):
        """§9: instrument eligibility is production policy, unchanged."""
        result = order_gate.evaluate_buy_gate_diagnostic(
            _ctx(allowed=frozenset(), leveraged=True))
        assert result.live_authorization_result == "LIVE_BLOCKED:SYMBOL"
        assert result.diagnostic_result == "DIAGNOSTIC_BLOCKED:INSTRUMENT"

    @pytest.mark.parametrize("limits,expected", [
        (dict(open_position_symbols=frozenset({"MSFT"})), entry_limits.MAX_OPEN_POSITIONS),
        (dict(daily_entry_count=1), entry_limits.MAX_DAILY_ENTRIES),
    ])
    def test_the_capacity_caps_are_reachable_with_an_empty_allowlist(self, limits, expected):
        """§11, the core purpose: these two are behind SYMBOL and were
        therefore unobservable while the allow-list was empty."""
        result = order_gate.evaluate_buy_gate_diagnostic(
            _ctx(allowed=frozenset(), limits=_limits(**limits)))
        assert result.live_authorization_result == "LIVE_BLOCKED:SYMBOL"
        assert result.diagnostic_result == f"DIAGNOSTIC_BLOCKED:{expected}"

    def test_it_substitutes_the_allowlist_in_a_copy_only(self):
        ctx = _ctx(allowed=frozenset())
        order_gate.evaluate_buy_gate_diagnostic(ctx)
        assert ctx.allowed_symbols == frozenset(), "the caller's context was mutated"

    def test_the_audit_payload_carries_both_axes(self):
        payload = order_gate.evaluate_buy_gate_diagnostic(
            _ctx(allowed=frozenset())).as_audit_payload()
        assert set(payload) == {
            "live_allowlist_allowed", "live_authorization_result",
            "diagnostic_result", "diagnostic_furthest_gate"}


class TestItAuthorizesNothing:
    def test_the_production_gate_still_stops_at_the_allowlist(self):
        """§21: evaluate_buy_gate keeps its stop-at-first-violation
        semantics; the diagnostic is a separate function."""
        with pytest.raises(order_gate.OrderGateBlockedError) as excinfo:
            order_gate.evaluate_buy_gate(_ctx(allowed=frozenset()))
        assert excinfo.value.code == "SYMBOL"

    def test_the_diagnostic_returns_a_report_not_a_permission(self):
        result = order_gate.evaluate_buy_gate_diagnostic(_ctx(allowed=frozenset()))
        assert not isinstance(result, bool)
        assert isinstance(result, order_gate.DiagnosticGateResult)

    def test_no_authorization_or_execution_module_can_reach_it(self):
        """The guarantee that keeps this a report: nothing on the order
        path calls it."""
        import ast

        offenders = []
        for rel in ("execution/authorization.py", "execution/execution_engine.py",
                    "kis_live_trading.py", "live_pilot/armed.py",
                    "brokers/kis_broker_adapter.py"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            if "evaluate_buy_gate_diagnostic" in source:
                offenders.append(rel)
        assert offenders == [], f"the order path reaches the diagnostic: {offenders}"

    def test_the_gate_has_no_bypass_flag(self):
        """A gate that can be told to continue past a violation is a gate
        with a bypass. The diagnostic passes a different allow-list
        instead, so evaluate_buy_gate takes no such parameter."""
        import inspect

        params = set(inspect.signature(order_gate.evaluate_buy_gate).parameters)
        assert params == {"ctx"}


# ---------------------------------------------------------------------
# ARMED isolation (§4, §16)
# ---------------------------------------------------------------------
class TestArmedIsolation:
    def test_the_shadow_variable_is_absent_from_every_live_path(self):
        for rel in ("kis_live_trading.py", "execution/order_gate.py",
                    "execution/execution_engine.py", "live_pilot/armed.py",
                    "config/live_rollout_config.py", "brokers/kis_broker_adapter.py"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "SHADOW_ALLOWED_SYMBOLS" not in source, rel

    def test_live_authorization_reads_only_the_live_variable(self):
        source = (REPO_ROOT / "config" / "live_rollout_config.py").read_text(encoding="utf-8")
        assert "LIVE_ROLLOUT_ALLOWED_SYMBOLS" in source

    def test_an_armed_buy_for_a_shadow_only_symbol_is_hard_blocked(self, monkeypatch):
        """§16: SHADOW_ALLOWED_SYMBOLS=BBVA with an empty live list must
        not authorize a BBVA order."""
        monkeypatch.setenv("SHADOW_ALLOWED_SYMBOLS", "BBVA")
        monkeypatch.setenv("LIVE_ROLLOUT_ALLOWED_SYMBOLS", "")
        from config.live_rollout_config import LiveRolloutConfig

        rollout = LiveRolloutConfig.from_env()
        assert rollout.allowed_symbols == frozenset()
        with pytest.raises(order_gate.OrderGateBlockedError) as excinfo:
            order_gate.evaluate_buy_gate(
                _ctx(symbol="BBVA", exchange="NYSE", allowed=rollout.allowed_symbols))
        assert excinfo.value.code == "SYMBOL"

    def test_an_armed_buy_for_a_live_listed_symbol_passes_symbol(self, monkeypatch):
        monkeypatch.setenv("LIVE_ROLLOUT_ALLOWED_SYMBOLS", "BBVA")
        from config.live_rollout_config import LiveRolloutConfig

        rollout = LiveRolloutConfig.from_env()
        assert rollout.allowed_symbols == frozenset({"BBVA"})
        order_gate.evaluate_buy_gate(
            _ctx(symbol="BBVA", exchange="NYSE", allowed=rollout.allowed_symbols))

    def test_an_armed_buy_for_a_symbol_outside_the_live_list_is_blocked(self, monkeypatch):
        monkeypatch.setenv("LIVE_ROLLOUT_ALLOWED_SYMBOLS", "BBVA")
        from config.live_rollout_config import LiveRolloutConfig

        rollout = LiveRolloutConfig.from_env()
        with pytest.raises(order_gate.OrderGateBlockedError) as excinfo:
            order_gate.evaluate_buy_gate(_ctx(symbol="IOVA", allowed=rollout.allowed_symbols))
        assert excinfo.value.code == "SYMBOL"


# ---------------------------------------------------------------------
# The OBSERVE pipeline, end to end
# ---------------------------------------------------------------------
UNIVERSE = "symbol,exchange\nIOVA,NASDAQ\nBBVA,NYSE\nGFL,NYSE\n"


def _shadow_module():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        return importlib.import_module("run_shadow_mode")
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


class _Broker:
    def __init__(self, price=5.97, orderable=30.99):
        self.calls = []
        self._price = price
        self._orderable = orderable

    def get_account_snapshot(self):
        class Snapshot:
            account_id = ACCOUNT
            usd_cash = None
            usd_orderable_cash = None
            usd_available_for_new_order = None
            cash_status = "UNAVAILABLE"
            cash_source = "TTTS3012R_DOES_NOT_PROVIDE"
        return Snapshot()

    def get_orderable_usd(self, instrument, limit_price_usd):
        return self._orderable

    def get_current_price(self, instrument):
        return self._price

    def get_open_orders(self):
        return []

    def get_positions(self):
        return []

    def get_fills(self, **kwargs):
        return []

    def submit_order(self, *a, **k):    # pragma: no cover
        raise AssertionError("OBSERVE reached an order transport")

    def cancel_order(self, *a, **k):    # pragma: no cover
        raise AssertionError("OBSERVE reached a cancel transport")


class _Rollout:
    """The read-only posture: nothing is authorized for live trading."""
    allowed_symbols = frozenset()
    max_quantity_per_order = 1
    max_price_deviation_percent = 30.0
    regular_session_only = False
    max_open_positions = 1
    max_daily_entries = 1


@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POS.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECON.json"))
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")
    monkeypatch.setenv("ENTRY_DISABLED", "true")
    monkeypatch.delenv("SHADOW_ALLOWED_SYMBOLS", raising=False)
    universe = tmp_path / "universe.csv"
    universe.write_text(UNIVERSE, encoding="utf-8")
    monkeypatch.setenv("UNIVERSE_FILE", str(universe))
    from execution import idempotency

    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEM.lock")
    from market_data import exchange_registry

    exchange_registry.reset_registry()
    state_db.open_db().close()
    yield tmp_path
    exchange_registry.reset_registry()


ORDER_TABLES = ("orders", "fills", "positions", "kis_order_idempotency",
                "live_entry_reservations", "exit_intents", "order_state_events")


def _counts():
    conn = state_db.open_db()
    try:
        return {t: conn.execute(f"select count(*) from {t}").fetchone()[0]
                for t in ORDER_TABLES}
    finally:
        conn.close()


def _events(symbol=None):
    conn = state_db.open_db()
    try:
        rows = conn.execute(
            "select symbol, event_type, result, reason_code, payload "
            "from shadow_audit_events order by rowid").fetchall()
    finally:
        conn.close()
    return [dict(symbol=r[0], event_type=r[1], result=r[2], reason_code=r[3],
                 payload=json.loads(r[4]) if r[4] else None)
            for r in rows if symbol is None or r[0] == symbol]


def _evaluate(module, monkeypatch, *, symbol="IOVA", price=5.97, broker=None):
    monkeypatch.setattr(module.pso, "analyze_stock",
                        lambda s: {"score": 999, "price": price})

    class _Quote:
        price_usd = price

    conn = state_db.open_db()
    try:
        return module._evaluate_symbol(
            symbol=symbol, broker=broker or _Broker(price=price), rollout=_Rollout(),
            conn=conn,
            kis_validation=type("V", (), {"get_price_quote": staticmethod(
                lambda s: _Quote())})(),
            deployed_commit="abc", validated_commit="abc",
            allowed_account_no=ACCOUNT, is_regular_session=True, now=NOW,
        )
    finally:
        conn.close()


class TestObserveWithAnEmptyLiveAllowlist:
    """§14: the posture the server actually runs in."""

    def test_the_candidate_is_still_evaluated(self, shadow_env, monkeypatch):
        module = _shadow_module()
        outcome = _evaluate(module, monkeypatch)
        assert outcome["symbol"] == "IOVA"
        assert outcome["hypothetical"] is not None

    def test_live_authorization_is_blocked_at_symbol(self, shadow_env, monkeypatch):
        module = _shadow_module()
        outcome = _evaluate(module, monkeypatch)
        assert outcome["live_allowlist_allowed"] is False
        assert outcome["hypothetical"] == "BLOCKED:SYMBOL"

    def test_the_diagnostic_reaches_the_last_gate(self, shadow_env, monkeypatch):
        module = _shadow_module()
        outcome = _evaluate(module, monkeypatch)
        assert outcome["diagnostic"] == order_gate.DIAGNOSTIC_PASS
        assert outcome["diagnostic_furthest_gate"] == entry_limits.MAX_DAILY_ENTRIES

    def test_the_diagnostic_audit_event_records_both_axes(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, monkeypatch)
        rows = [e for e in _events("IOVA")
                if e["event_type"] == shadow_audit.DIAGNOSTIC_COMPLETED]
        assert len(rows) == 1
        payload = rows[0]["payload"] or {}
        assert payload["live_allowlist_allowed"] is False
        assert payload["live_authorization_result"] == "LIVE_BLOCKED:SYMBOL"
        assert payload["diagnostic_result"] == order_gate.DIAGNOSTIC_PASS
        assert payload["diagnostic_furthest_gate"] == entry_limits.MAX_DAILY_ENTRIES
        # The capacity numbers ride along on the same event.
        assert payload["max_open_positions"] == 1
        assert payload["trading_day"] == TODAY

    def test_exactly_one_terminal_event(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, monkeypatch)
        terminal = [e for e in _events("IOVA")
                    if e["event_type"] in shadow_audit.TERMINAL_EVENT_TYPES]
        assert len(terminal) == 1

    def test_no_transport_and_no_side_effect(self, shadow_env, monkeypatch):
        module = _shadow_module()
        before = _counts()
        broker = _Broker()
        _evaluate(module, monkeypatch, broker=broker)
        assert _counts() == before

    def test_the_diagnostic_is_not_reported_as_an_approval(self, shadow_env, monkeypatch):
        module = _shadow_module()
        outcome = _evaluate(module, monkeypatch)
        assert "APPROVED" not in outcome["diagnostic"]
        gate_events = [e for e in _events("IOVA")
                       if e["event_type"] == shadow_audit.GATE_APPROVED]
        assert gate_events == [], "a non-allow-listed symbol was recorded as gate-approved"


class TestAnAllowlistMissIsNotAlwaysTheReason:
    """An allow-list MISS and a block AT the allow-list are different
    facts, and conflating them produced a false failure on the server: a
    Saturday run stopped at SESSION (which precedes SYMBOL), and a probe
    assertion that assumed "not allow-listed => blocked at SYMBOL"
    reported a defect where the behaviour was correct.

    The invariant runs one way only: a block AT the allow-list means the
    symbol really is not on it. The converse does not hold.
    """

    def test_an_earlier_gate_reports_itself_not_the_allowlist(self):
        ctx = dataclasses.replace(_ctx(allowed=frozenset()), is_regular_session=False)
        result = order_gate.evaluate_buy_gate_diagnostic(ctx)
        assert result.live_allowlist_allowed is False
        assert result.live_blocked_code == "SESSION"
        assert result.diagnostic_blocked_code == "SESSION"

    def test_the_one_way_invariant_holds(self):
        """Blocked at the allow-list => the symbol is not on it."""
        for allowed in (frozenset(), frozenset({"IOVA"}), frozenset({"MSFT"})):
            for session in (True, False):
                ctx = dataclasses.replace(_ctx(allowed=allowed),
                                          is_regular_session=session)
                result = order_gate.evaluate_buy_gate_diagnostic(ctx)
                if result.live_blocked_code == order_gate.LIVE_ALLOWLIST_GATE:
                    assert result.live_allowlist_allowed is False, (allowed, session)

    def test_the_split_only_engages_at_the_allowlist_gate(self):
        """When an earlier gate stops it, the diagnostic must mirror that
        verdict rather than inventing progress past it."""
        ctx = dataclasses.replace(_ctx(allowed=frozenset()), is_regular_session=False)
        result = order_gate.evaluate_buy_gate_diagnostic(ctx)
        assert result.diagnostic_blocked_code == result.live_blocked_code


class TestTheAuditFieldSurvivesRedaction:
    """`live_authorization_result` contains the word "authorization", so
    the key-based redactor masked it into uselessness. The exemption
    added for it must be exact -- a key that really is a credential must
    still be masked."""

    def test_the_verdict_field_is_readable(self):
        from execution.secret_redaction import redact_value

        payload = {"live_authorization_result": "LIVE_BLOCKED:SYMBOL"}
        assert redact_value(payload)["live_authorization_result"] == "LIVE_BLOCKED:SYMBOL"

    @pytest.mark.parametrize("key", [
        "authorization", "Authorization", "AUTHORIZATION",
        "live_authorization_token", "authorization_result",
        "app_key", "app_secret", "access_token", "account_number",
    ])
    def test_the_exemption_did_not_widen(self, key):
        from execution.secret_redaction import REDACTED, redact_value

        assert redact_value({key: "sensitive"})[key] == REDACTED


class TestShadowScopeIsEvaluationOnly:
    """§13/§15: SHADOW_ALLOWED_SYMBOLS selects what gets evaluated and
    nothing else."""

    def test_unset_evaluates_every_candidate(self, shadow_env, monkeypatch):
        module = _shadow_module()
        monkeypatch.delenv("SHADOW_ALLOWED_SYMBOLS", raising=False)
        assert module.shadow_allowed_symbols(_Rollout()) is None

    def test_a_scope_excludes_other_candidates(self, shadow_env, monkeypatch):
        module = _shadow_module()
        monkeypatch.setenv("SHADOW_ALLOWED_SYMBOLS", "BBVA,GFL")
        scope = module.shadow_allowed_symbols(_Rollout())
        assert scope == frozenset({"BBVA", "GFL"})
        assert "IOVA" not in scope

    def test_a_scoped_symbol_is_still_live_blocked(self, shadow_env, monkeypatch):
        """In scope for evaluation, still unauthorized for ordering."""
        module = _shadow_module()
        monkeypatch.setenv("SHADOW_ALLOWED_SYMBOLS", "BBVA")
        outcome = _evaluate(module, monkeypatch, symbol="BBVA")
        assert outcome["live_allowlist_allowed"] is False
        assert outcome["hypothetical"] == "BLOCKED:SYMBOL"
        assert outcome["diagnostic"] == order_gate.DIAGNOSTIC_PASS

    def test_the_scope_never_reaches_the_gate_context(self):
        """The variable must not leak into authorization."""
        source = (SCRIPTS_DIR / "run_shadow_mode.py").read_text(encoding="utf-8")
        gate_block = source[source.index("def _ctx("):source.index("real_blocked = None")]
        assert "shadow_allowed_symbols" not in gate_block
        assert "allowed_symbols=rollout.allowed_symbols" in gate_block
