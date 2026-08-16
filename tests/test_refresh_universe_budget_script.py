"""T8: scripts/refresh_universe_budget.py -- the one entry point in the
universe path that talks to a real account.

Its exit codes are the operator contract (0 read ok / 1 kept previous /
2 nothing usable), so they are pinned here. No test constructs a real
KISBroker or opens a socket.
"""

import ast
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import universe_budget as ub
from domain.account_snapshot import AccountSnapshot

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "refresh_universe_budget.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("refresh_universe_budget", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


class _FakeKIS:
    def __init__(self, snapshot=None, error=None):
        self._snapshot = snapshot
        self._error = error

    def get_account_snapshot(self):
        if self._error is not None:
            raise self._error
        return self._snapshot


def _snapshot(orderable=10_000.0):
    return AccountSnapshot(
        krw_cash=0.0, usd_cash=orderable, usd_orderable_cash=orderable,
        usd_reserved_in_open_orders=0.0, as_of=NOW, source="kis_balance",
        account_id="12345678")


def test_successful_read_exits_zero_and_persists(tmp_path):
    state_file = tmp_path / "universe_budget.json"
    code = script.main([], broker=_FakeKIS(_snapshot()), state_path=str(state_file),
                       logger=lambda *_: None)
    assert code == 0
    assert ub.load_budget_state(state_file).available_cash_usd == pytest.approx(10_000.0)


def test_failed_read_with_a_previous_value_exits_one(tmp_path):
    state_file = tmp_path / "universe_budget.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=500.0, as_of=NOW.isoformat(), source="kis_balance"),
        state_file)
    code = script.main([], broker=_FakeKIS(error=RuntimeError("down")),
                       state_path=str(state_file), logger=lambda *_: None)
    assert code == 1


def test_failed_read_with_no_previous_value_exits_two(tmp_path):
    code = script.main([], broker=_FakeKIS(error=RuntimeError("down")),
                       state_path=str(tmp_path / "missing.json"), logger=lambda *_: None)
    assert code == 2


def test_show_prints_the_derived_price_ceiling(tmp_path):
    messages = []
    script.main(["--show"], broker=_FakeKIS(_snapshot(orderable=10_000.0)),
                state_path=str(tmp_path / "b.json"), logger=messages.append)
    payload = json.loads(messages[-1])
    assert payload["available_cash_usd"] == pytest.approx(10_000.0)
    assert payload["price_ceiling_usd"] == pytest.approx(900.0)
    assert payload["stale"] is False


def test_script_never_touches_the_order_gate():
    """The balance read must not be able to enable or depend on order
    submission -- KIS_LIVE_ORDER_ENABLED is not referenced at all."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    # The module docstring documents what the script deliberately does NOT
    # do, so it is excluded before checking the executable text.
    docstring = ast.get_docstring(ast.parse(source))
    code = source.replace(docstring, "") if docstring else source
    assert "KIS_LIVE_ORDER_ENABLED" not in code
    for forbidden in ("submit_order", "cancel_order", "validate_live_order_allowed"):
        assert forbidden not in code
