"""Only the Common Execution Engine may reach a real KIS order.

The architecture
----------------
    Strategy / Scanner
            |
            v
      BUY / SELL intent
            |
            v
     Execution mode router
        /              \\
     LIVE              PAPER
       |                  |
       v                  v
  Common Execution   Virtual Execution
  Engine                  |
       |                  v
       v            (no broker at all)
      KIS

The engine owns authentication, orderable cash, capability validation,
submission, acknowledgement, fills, cancels, rejections and duplicate
prevention. It owns none of the strategy's reasoning: what to buy, when,
how many, and when to leave are the strategy's, and it never asks.

These are structural tests against the source tree rather than against
behaviour. A behaviour test proves the paths someone thought to exercise
are safe; an import that does not exist cannot be reached by a path
nobody thought of.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The real network call. Only these files may name it.
#:
#: `execution_engine` is the engine itself. `kis_broker` defines the
#: method. `kis_broker_adapter` and `live_pilot/bootstrap` are transport
#: wrappers the engine calls THROUGH -- they add budgets and logging and
#: cannot originate an order, because the intent reaches them already
#: gated.
SUBMIT_PERMITTED = {
    # --- the KIS path -------------------------------------------------
    # The Common Execution Engine itself, and the broker that defines
    # the method.
    "execution/execution_engine.py",
    "brokers/kis_broker.py",
    # Transport wrappers the engine calls THROUGH. They add budgets,
    # counting and logging; they cannot originate an order, because an
    # intent reaches them already gated.
    "brokers/kis_broker_adapter.py",
    "live_pilot/bootstrap.py",
    # The route-verification one-shot's transport budget, the same shape
    # as the bootstrap's above: it proxies the real broker so the engine
    # can count one BUY, one cancel and one flatten, and it re-asserts
    # the order shape immediately before the call. It cannot originate an
    # order either -- `execution_engine.submit_buy_order` runs the full
    # gate before the transport it holds is ever reached.
    "live_pilot/route_verification_runner.py",
    # The strategy handing a sell intent to the ROUTER, not to KIS:
    # `broker_adapter.submit_order(...)` lands in kis_broker_adapter,
    # which calls `execution_engine.submit_sell_order()`. Naming it here
    # records that it was checked, rather than leaving the guard to
    # flag it forever.
    "s1_live/exit_runtime.py",

    # --- the Alpaca path ----------------------------------------------
    # A SECOND, older execution engine for a different broker, with the
    # same "only this module submits" rule and its own long-standing
    # guard in tests/test_execution_engine.py. It is not a KIS bypass;
    # it never reaches KIS at all.
    "live_readiness/execution_engine.py",
    # The grandfathered Alpaca paper wrapper, and the lifecycle that
    # calls it. Different broker; kept out of the KIS path.
    "paper_strategy_order.py",
    "positions/lifecycle.py",
}

#: Packages that must never contain a broker reference at all.
ISOLATED_PACKAGES = ("scanners",)


def _python_files(root):
    for path in Path(root).rglob("*.py"):
        if "venv" in path.parts or "tests" in path.parts:
            continue
        yield path


def _calls_named(path, names):
    """Attribute calls like `x.submit_order(...)` appearing as CODE.

    Parsed rather than grepped, so a mention in a docstring or comment
    -- of which this repo has many, deliberately -- is not a finding.
    """
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in names:
                found.add(node.func.attr)
    return found


class TestOnlyTheEngineSubmits:
    def test_no_unexpected_module_calls_submit_or_cancel(self):
        offenders = []
        for path in _python_files(REPO_ROOT):
            rel = str(path.relative_to(REPO_ROOT))
            if rel in SUBMIT_PERMITTED:
                continue
            if _calls_named(path, {"submit_order", "cancel_order"}):
                offenders.append(rel)
        assert offenders == [], (
            "these modules reach a broker order directly, bypassing the "
            f"Common Execution Engine: {offenders}")

    def test_the_engine_is_the_one_that_calls_the_broker(self):
        engine = REPO_ROOT / "execution" / "execution_engine.py"
        assert _calls_named(engine, {"submit_order"}), (
            "the Common Execution Engine must be what actually submits")

    def test_the_live_entry_path_goes_through_the_engine(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "execution_engine.submit_buy_order(" in source

    def test_the_live_exit_path_goes_through_the_engine(self):
        source = (REPO_ROOT / "brokers" / "kis_broker_adapter.py").read_text()
        assert "execution_engine.submit_sell_order(" in source


class TestScannersCannotTrade:
    @pytest.mark.parametrize("package", ISOLATED_PACKAGES)
    def test_no_broker_import_anywhere_in_the_package(self, package):
        offenders = []
        for path in _python_files(REPO_ROOT / package):
            text = path.read_text()
            if "from brokers" in text or "import brokers" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == [], offenders

    @pytest.mark.parametrize("package", ISOLATED_PACKAGES)
    def test_no_submission_call_anywhere_in_the_package(self, package):
        offenders = [str(p.relative_to(REPO_ROOT))
                     for p in _python_files(REPO_ROOT / package)
                     if _calls_named(p, {"submit_order", "cancel_order",
                                         "submit_buy_order",
                                         "submit_sell_order"})]
        assert offenders == [], offenders


class TestTheEngineDoesNotDecideStrategy:
    """It decides CAN EXECUTE / EXECUTE / RESULT. What to buy, when, how
    many and when to leave belong to the strategy."""

    def test_it_holds_no_entry_or_exit_thresholds(self):
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text()
        for forbidden in ("orb_minutes", "volume_expansion", "ema9", "ema21",
                          "range_high", "retest_tolerance", "take_profit",
                          "trailing_stop"):
            assert forbidden not in source, (
                f"the execution engine references {forbidden}, which is a "
                "strategy decision it must not make")

    def test_it_does_not_choose_symbols(self):
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text()
        for forbidden in ("candidate_source", "scanner_live_mode",
                          "precision_watch"):
            assert forbidden not in source, forbidden

    def test_the_quantity_arrives_already_decided(self):
        """The engine executes a quantity; it does not size a position."""
        import inspect

        from execution import execution_engine

        source = inspect.getsource(execution_engine.submit_buy_order)
        assert "order_intent" in source
