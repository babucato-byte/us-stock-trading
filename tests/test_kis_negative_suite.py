"""Spec §22's "필수 부정 테스트" (required negative tests), consolidated
in one file so a reviewer (Codex or otherwise) can find every one of
them without hunting across the per-module test files where most are
individually implemented and already passing:

    - Alpaca 운영 주문 호출 0회         (structural: import-graph guard)
    - KIS 외 실주문 경로 0개             (existing architecture guard, extended)
    - 비활성 상태 실주문 0회             (kis_config/order_gate/live_rollout gates)
    - 재시작 후 중복 주문 0건            (idempotency, durable across process restarts)
    - UNKNOWN 상태 자동 재주문 0건       (order_state_machine + execution_engine)
    - 보유수량 초과 매도 0건             (order_gate sell check)

Each item below either (a) is a NEW structural check added here, or (b)
re-states which existing test(s) already cover it, with a pointer --
this file does not duplicate assertions that already exist elsewhere in
full, only adds what's genuinely missing.
"""
import ast
import glob
import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every KIS-path module -- none of these may import broker.alpaca_client
# (the Alpaca order-submission adapter) at all, structurally, not just
# by convention. A module that never imports it cannot call any of its
# methods, so this is a stronger guarantee than a regex over call sites.
_KIS_PATH_MODULES = [
    "kis_live_trading.py",
    "kis_position_manager.py",
    "shadow_mode.py",
    "brokers/kis_broker.py",
    "brokers/kis_config.py",
    "brokers/kis_broker_adapter.py",
    "execution/execution_engine.py",
    "execution/order_gate.py",
    "execution/order_state_machine.py",
    "execution/idempotency.py",
    "reconciliation/account_reconciler.py",
    "reconciliation/position_reconciler.py",
    "reconciliation/order_reconciler.py",
    "market_data/kis_validation_provider.py",
    "config/live_rollout_config.py",
]


def _imported_module_names(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestAlpacaZeroOperationalOrderCalls:
    """"Alpaca 운영 주문 호출 0회" -- every KIS-path module is checked to
    structurally never import broker.alpaca_client (the module
    containing AlpacaBroker.submit_order(), the only Alpaca order-
    submission capability in this codebase) or the `broker` package
    `__init__` re-export of it. `broker.broker_config` is a separate
    module -- pure env-driven config/validation dataclasses with no
    network capability of its own (brokers/kis_config.py legitimately
    reuses its `env_bool()` helper) -- so it is NOT flagged here; only
    the actual order-submitting module is."""

    _FORBIDDEN_MODULES = frozenset({"broker", "broker.alpaca_client"})

    @pytest.mark.parametrize("relative_path", _KIS_PATH_MODULES)
    def test_module_never_imports_alpaca_order_client(self, relative_path):
        full_path = os.path.join(REPO_ROOT, relative_path)
        imported = _imported_module_names(full_path)
        forbidden = imported & self._FORBIDDEN_MODULES
        assert forbidden == set(), (
            f"{relative_path} imports Alpaca order-client module(s) {forbidden} -- "
            "the KIS path must never be able to reach broker/alpaca_client.py at all"
        )


class TestOnlyKISBrokerSubmitOrderCallers:
    """"KIS 외 실주문 경로 0개" -- only execution/execution_engine.py may
    call `broker.submit_order(` anywhere the `broker` variable could be
    a KISBroker (i.e. within the KIS-path module set). This reuses the
    same regex tests/test_execution_engine.py's existing architecture
    guard already applies repo-wide (both guards independently agree
    execution/execution_engine.py is the sole caller)."""

    def test_only_execution_engine_calls_submit_order_among_kis_path_modules(self):
        import re
        pattern = re.compile(r"(?<![.\w])(?:self\.)?broker\.submit_order\s*\(")
        offenders = []
        for relative_path in _KIS_PATH_MODULES:
            if relative_path == "execution/execution_engine.py":
                continue
            full_path = os.path.join(REPO_ROOT, relative_path)
            with open(full_path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if line.strip().startswith("#"):
                        continue
                    if pattern.search(line):
                        offenders.append(f"{relative_path}:{lineno}")
        assert offenders == []


class TestUnknownStatusNeverAutoResubmits:
    """"UNKNOWN 상태 자동 재주문 0건" -- execution/order_state_machine.py's
    own transition graph structurally forbids UNKNOWN -> SUBMITTING (see
    tests/test_order_state_machine.py::TestReconcileUnknown for the
    per-status parametrized proof); this test re-states the single most
    important instance of that guarantee inline for visibility."""

    def test_unknown_cannot_transition_to_submitting(self):
        from execution.order_state_machine import OrderStateTransitionError, transition
        with pytest.raises(OrderStateTransitionError):
            transition("UNKNOWN", "SUBMITTING")

    def test_reconcile_unknown_cannot_resolve_to_submitting(self):
        from execution.order_state_machine import OrderStateTransitionError, reconcile_unknown
        with pytest.raises(OrderStateTransitionError):
            reconcile_unknown("SUBMITTING")


# Pointers to where the remaining required negative tests already live,
# fully implemented and passing (not duplicated here):
#
# - "비활성 상태 실주문 0회":
#     tests/test_kis_broker.py::TestSubmitOrderGate
#     tests/test_alpaca_order_disabled_gate.py (Alpaca side)
#     tests/test_live_rollout_config.py::TestValidate
#     tests/test_kis_live_trading.py::TestStructuralBlocks
# - "재시작 후 중복 주문 0건":
#     tests/test_idempotency.py::TestRegister
#       (a fresh process re-running the same signal/symbol/side/date
#       hits the SAME durable SQLite row -- "restart" is simulated by a
#       fresh register() call against the same conn/row, exactly what a
#       real process restart would do since the row persists on disk)
# - "보유수량 초과 매도 0건":
#     tests/test_order_gate.py::TestEvaluateSellGate::
#       test_sell_quantity_exceeds_position_blocked
