"""scripts/verify_kis_observe_cash_path.py -- the OBSERVE cash-path
validation probe.

The probe exists because two Oracle OBSERVE sessions confirmed the
orderable-amount WIRE contract but never exercised the sizing path: the
scanner produced no candidates, so the evaluation stopped before the cash
stage. Waiting for a candidate is not a verification plan.

What these tests pin:

1. The probe reuses production code. It must not carry its own copy of
   the sizing formula, its own gate, or its own order-intent builder --
   a probe with a private formula verifies its private formula.
2. It cannot submit. The allow-list holds read methods only, submission
   methods raise, and the counters are asserted before the result prints.
3. It never writes a candidate file or a production artifact.
4. Its GATE_SEQUENCE (used only to report how far an evaluation got)
   matches the order `order_gate.evaluate_buy_gate()` actually checks in.
"""
import ast
import importlib
import pathlib
import sys

import pytest

from execution import entry_limits as entry_limits_module
from execution import order_gate as _order_gate_module
from execution import reentry_policy as _reentry_policy_module

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE_PATH = REPO_ROOT / "scripts" / "verify_kis_observe_cash_path.py"
PROBE_SOURCE = PROBE_PATH.read_text(encoding="utf-8")


def _probe():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        return importlib.import_module("verify_kis_observe_cash_path")
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))


class TestItReusesProductionCode:
    def test_it_imports_the_production_sizing_function(self):
        probe = _probe()
        from domain import cash_sizing

        assert probe.whole_shares_affordable is cash_sizing.whole_shares_affordable

    def test_it_imports_the_production_gate(self):
        probe = _probe()
        from execution import order_gate

        assert probe.order_gate is order_gate

    def test_it_imports_the_production_builders(self):
        probe = _probe()
        from domain.order_intent import OrderIntent
        from domain.signal import build_signal

        assert probe.OrderIntent is OrderIntent
        assert probe.build_signal is build_signal

    def test_it_does_not_reimplement_the_sizing_arithmetic(self):
        """The specific failure this guards: a probe that computes
        `int(cash // price)` itself would pass while production's Decimal
        floor disagreed with it at a boundary."""
        tree = ast.parse(PROBE_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.FloorDiv):
                raise AssertionError(
                    f"line {node.lineno}: the probe floor-divides; it must call "
                    "domain.cash_sizing.whole_shares_affordable")
        assert "math.floor" not in PROBE_SOURCE

    def test_it_declares_itself_a_validation_probe(self):
        probe = _probe()
        assert probe.MODE == "OBSERVE_VALIDATION"
        assert probe.TRANSPORT_ENABLED is False
        assert "mode: {MODE}" in PROBE_SOURCE or "MODE}" in PROBE_SOURCE


class TestItCannotSubmit:
    def test_the_allow_list_is_read_only(self):
        probe = _probe()
        assert probe.ReadOnlyBroker.ALLOWED == frozenset({
            "get_current_price", "get_account_snapshot", "get_positions",
            "get_open_orders", "get_fills", "get_orderable_usd", "config"})
        assert not (probe.ReadOnlyBroker.ALLOWED & probe.FORBIDDEN_METHODS)

    @pytest.mark.parametrize("method", sorted({
        "submit_order", "cancel_order", "submit_buy_order", "submit_sell_order",
        "amend_order", "replace_order", "place_order"}))
    def test_a_submission_method_raises_before_any_request(self, method):
        probe = _probe()

        class _Exploding:
            def __getattr__(self, name):  # pragma: no cover -- must never run
                raise AssertionError(f"the probe reached the real broker's {name}")

        wrapped = probe.ReadOnlyBroker(_Exploding())
        with pytest.raises(probe.ReadOnlyViolation):
            getattr(wrapped, method)
        assert wrapped.order_calls + wrapped.cancel_calls == 1, (
            "the attempt must be counted so the report states a measured zero")

    def test_an_unlisted_method_also_raises(self):
        probe = _probe()
        wrapped = probe.ReadOnlyBroker(object())
        with pytest.raises(probe.ReadOnlyViolation):
            wrapped.get_assets  # noqa: B018

    def test_it_never_imports_the_execution_engine(self):
        tree = ast.parse(PROBE_SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not [m for m in imported if "execution_engine" in m or "adapter" in m]

    def test_it_asserts_zero_transport_before_reporting(self):
        assert "assert broker.order_calls == 0 and broker.cancel_calls == 0" in PROBE_SOURCE


class TestItTouchesNoProductionArtifact:
    @pytest.mark.parametrize("artifact", [
        "candidates.csv", "order_candidates.csv", "strong_candidates.csv",
        "previous_candidates.csv", "universe.csv", "universe_tradable.csv",
        "order_history.csv", "strategy_performance.csv",
    ])
    def test_the_probe_never_names_a_production_artifact_as_a_write(self, artifact):
        """The names may appear in the docstring (saying it does NOT touch
        them); they must not appear in code."""
        tree = ast.parse(PROBE_SOURCE)
        # Collect the docstring NODES, not `ast.get_docstring()`'s cleaned
        # text -- cleandoc() strips indentation, so the cleaned string
        # never equals the raw Constant it came from.
        docstring_nodes = set()
        for node in ast.walk(tree):
            # Only the four node types whose `body` is a statement LIST;
            # IfExp/Lambda also have a `body`, but it is a single node.
            if not isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            body = node.body
            if not body or not isinstance(body[0], ast.Expr):
                continue
            first = body[0].value
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                docstring_nodes.add(id(first))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstring_nodes:
                    continue
                assert artifact not in node.value, (
                    f"line {node.lineno}: {artifact} appears outside a docstring")

    def test_it_opens_no_file_for_writing(self):
        tree = ast.parse(PROBE_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                assert name not in ("write_text", "write_bytes"), (
                    f"line {node.lineno}: the probe writes a file")
                if name == "open":
                    raise AssertionError(f"line {node.lineno}: the probe calls open()")

    def test_it_checks_every_order_table_for_side_effects(self):
        probe = _probe()
        assert set(probe.ORDER_TABLES) == {
            "orders", "fills", "positions", "kis_order_idempotency",
            "live_entry_reservations", "exit_intents", "order_state_events"}


#: Modules whose attributes may appear as an OrderGateBlockedError
#: `code=`. Looked up by the name used in the gate's own source.
_GATE_CODE_MODULES = {
    "entry_limits": entry_limits_module,
    "reentry_policy": _reentry_policy_module,
}


class TestGateSequenceMatchesTheGate:
    def _gate_codes_in_source_order(self):
        """The `code=` literals raised by evaluate_buy_gate, in order."""
        source = (REPO_ROOT / "execution" / "order_gate.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "evaluate_buy_gate")
        codes = []
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "OrderGateBlockedError":
                continue
            for keyword in node.keywords:
                if keyword.arg == "code" and isinstance(keyword.value, ast.Constant):
                    codes.append((keyword.value.lineno, keyword.value.value))
        # ast.walk() is BREADTH-first, so it yields a raise nested inside an
        # if/else AFTER shallower ones regardless of where it appears in the
        # file. Sorting by line number is what "in source order" actually
        # means, and it keeps this check independent of nesting depth.
        return [code for _lineno, code in sorted(codes)]

    def _helper_codes_in_source_order(self, function_name):
        """The codes raised by a helper the gate calls.

        These are module-attribute references rather than string
        literals -- `entry_limits.X`, and since the same-day re-entry
        block was added, `reentry_policy.X` too. The module is resolved
        from the reference itself, so a code introduced from a THIRD
        module is picked up here instead of silently going unreported.
        """
        source = (REPO_ROOT / "execution" / "order_gate.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function_name)
        codes = []
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "OrderGateBlockedError":
                continue
            for keyword in node.keywords:
                if keyword.arg != "code":
                    continue
                # A code defined in order_gate itself is a bare Name, not
                # a module attribute. Resolving only Attributes silently
                # dropped ROUTE_UNVERIFIED, which is exactly the drift
                # this test exists to catch.
                if isinstance(keyword.value, ast.Name):
                    codes.append((keyword.value.lineno,
                                  getattr(_order_gate_module, keyword.value.id)))
                    continue
                if not isinstance(keyword.value, ast.Attribute):
                    continue
                module_name = getattr(keyword.value.value, "id", None)
                module = _GATE_CODE_MODULES.get(module_name)
                assert module is not None, (
                    f"{module_name!r} is not a known source of gate codes; "
                    "add it to _GATE_CODE_MODULES so its codes stay covered")
                codes.append((keyword.value.lineno,
                              getattr(module, keyword.value.attr)))
        return [code for _lineno, code in sorted(codes)]

    def test_the_probe_sequence_matches_the_gate_source(self):
        probe = _probe()
        codes = self._gate_codes_in_source_order()
        # One code can cover several consecutive checks -- QUANTITY is
        # raised twice (not-an-int, then < 1). GATE_SEQUENCE lists each
        # gate once, in order, because it reports HOW FAR an evaluation
        # got, not how many raise sites there are.
        deduped = []
        for code in codes:
            if not deduped or deduped[-1] != code:
                deduped.append(code)
        # RECONCILIATION and the capacity caps are raised by the two
        # helpers the gate calls last, so they are not among
        # evaluate_buy_gate's own literals. The caps are read from the
        # helper's source rather than hard-coded, so adding another cap
        # fails here instead of silently going unreported.
        #
        # The *_UNKNOWN codes are excluded: they mean "this limit could
        # not be established", which is a state fault rather than a
        # position in the sequence. GATE_SEQUENCE reports how far an
        # evaluation got, and "the state was unreadable" is not a
        # further stage of getting there.
        state_faults = {entry_limits_module.POSITION_LIMIT_STATE_UNKNOWN,
                        entry_limits_module.STRATEGY_ATTRIBUTION_UNKNOWN}
        # Route evidence runs before the capacity checks and raises from
        # its own helper, so its code is collected the same way rather
        # than being hand-added here -- a second helper that the gate
        # calls must show up in this sequence or the probe would report
        # "how far did this get" against a stage it cannot see.
        route = self._helper_codes_in_source_order("_check_route_evidence")
        caps = route + self._helper_codes_in_source_order("_check_entry_limits")
        deduped_caps = []
        for code in caps:
            if code in state_faults:
                continue
            if not deduped_caps or deduped_caps[-1] != code:
                deduped_caps.append(code)
        caps = deduped_caps
        expected = tuple(deduped) + ("RECONCILIATION",) + tuple(caps)
        assert probe.GATE_SEQUENCE == expected, (
            "GATE_SEQUENCE drifted from order_gate.evaluate_buy_gate")

    def test_cash_is_in_the_sequence_and_has_gates_after_it(self):
        probe = _probe()
        assert "CASH" in probe.GATE_SEQUENCE
        after = probe.GATE_SEQUENCE[probe.GATE_SEQUENCE.index("CASH") + 1:]
        assert after, "nothing follows CASH; the probe could not show progress past it"

    def test_furthest_gate_reporting(self):
        probe = _probe()
        # Blocked at SYMBOL -> reached the check just before it.
        reached, stopped = probe._furthest_gate("SYMBOL")
        assert stopped == "SYMBOL"
        assert reached == probe.GATE_SEQUENCE[probe.GATE_SEQUENCE.index("SYMBOL") - 1]
        # Nothing blocked -> everything passed.
        reached, stopped = probe._furthest_gate(None)
        assert stopped == "ALL_PASSED"
        assert reached == probe.GATE_SEQUENCE[-1]

    def test_a_block_at_cash_is_not_reported_as_progress_past_cash(self):
        probe = _probe()
        reached, stopped = probe._furthest_gate("CASH")
        assert stopped == "CASH"
        assert reached != "CASH"


class TestTheSizingCasesTheProbeReliesOn:
    """The arithmetic the probe asserts on Oracle, checked here so a live
    run is confirming behaviour this suite already fixed."""

    def test_the_observed_live_numbers(self):
        from domain.cash_sizing import whole_shares_affordable

        assert whole_shares_affordable(30.99, 5.82) == 5

    def test_the_rollout_cap_wins(self):
        from domain.cash_sizing import whole_shares_affordable

        assert min(whole_shares_affordable(30.99, 5.82), 1) == 1

    def test_just_under_one_share(self):
        from domain.cash_sizing import whole_shares_affordable

        assert whole_shares_affordable(5.819999, 5.82) == 0

    def test_exactly_one_share(self):
        from domain.cash_sizing import whole_shares_affordable

        assert whole_shares_affordable(5.82, 5.82) == 1

    def test_the_probes_insufficient_cash_control_is_sound(self):
        """The control the probe computes with no API call: one cent short
        of a share is zero shares, at any price."""
        from domain.cash_sizing import whole_shares_affordable

        for price in (5.82, 30.99, 100.0, 0.5):
            assert whole_shares_affordable(max(0.0, price - 0.01), price) == 0

    def test_unavailable_is_a_different_outcome_from_insufficient(self):
        from domain import cash_sizing

        assert cash_sizing.ORDERABLE_CASH_UNAVAILABLE != cash_sizing.INSUFFICIENT_CASH


class TestTheProbeDoesNotChangeProductionBehaviour:
    def test_no_production_module_imports_the_probe(self):
        """A probe that production depends on is not a probe."""
        hits = []
        for path in REPO_ROOT.rglob("*.py"):
            parts = path.parts
            if "venv" in parts or "tests" in parts:
                continue
            if path == PROBE_PATH:
                continue
            if "verify_kis_observe_cash_path" in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(REPO_ROOT)))
        assert hits == [], f"production code references the probe: {hits}"

    def test_it_writes_no_environment_variable(self):
        """The hypothetical evaluation flips flags in the CONTEXT OBJECT,
        never in the environment -- so the probe cannot leave a relaxed
        flag behind if it dies mid-run."""
        tree = ast.parse(PROBE_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None)
                if name in ("setenv", "putenv"):
                    raise AssertionError(f"line {node.lineno}: the probe sets an env var")
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                value = getattr(node.value, "attr", None) or getattr(node.value, "id", None)
                assert value != "environ", (
                    f"line {node.lineno}: the probe assigns into os.environ")
