"""OBSERVE and ARMED do not depend on the same KIS wire values.

The pilot preflight used to refuse a live OBSERVE session while ANY of
the nine matrix values was unconfirmed -- including the order path and
the cancel TR_IDs, which OBSERVE never reaches, because it never imports
the modules that submit. So the one activity that could confirm anything
was blocked by values it does not use.

The split is derived from where each value is REFERENCED, not from what
its name suggests. `order_exchange_code_space` is the case that proves
the point: it reads like an order-only concern, but OVRS_EXCG_CD is what
`_sweep_exchanges()` puts on every balance, open-order and fill read, so
OBSERVE depends on it entirely. A name-based split would have un-gated
it; the tests below check the classification against the source.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brokers import kis_broker
from brokers.kis_broker import (
    LIVE_RESPONSE_CONFIRMED,
    LIVE_RESPONSE_PENDING,
    REQUIRED_FOR_ARMED,
    REQUIRED_FOR_OBSERVE,
    VERIFICATION_MATRIX,
    WireValueVerification,
    matrix_entries_for,
    pending_items_for,
)
from live_pilot import posture as posture_module
from live_pilot import preflight

REPO_ROOT = Path(__file__).resolve().parent.parent
BROKER_SOURCE = (REPO_ROOT / "brokers" / "kis_broker.py").read_text(encoding="utf-8")

ORDER_ONLY = ("order_path", "order_tr_id_live_buy", "cancel_path",
              "cancel_tr_id_live", "cancel_tr_id_paper", "cancel_price_field_rule")
OBSERVE_VALUES = ("price_path", "price_field_last", "order_exchange_code_space")


def _entry(name):
    return next(e for e in VERIFICATION_MATRIX if e.name == name)


# =====================================================================
# The split itself.
# =====================================================================

class TestRequirementsAreSplitByPosture:
    def test_every_entry_declares_who_needs_it(self):
        for entry in VERIFICATION_MATRIX:
            assert entry.required_for, entry.name
            assert entry.required_for <= {REQUIRED_FOR_OBSERVE, REQUIRED_FOR_ARMED}

    @pytest.mark.parametrize("name", OBSERVE_VALUES)
    def test_observe_values_are_required_by_both_postures(self, name):
        """ARMED does everything OBSERVE does, so an OBSERVE requirement
        is automatically an ARMED requirement."""
        entry = _entry(name)
        assert REQUIRED_FOR_OBSERVE in entry.required_for
        assert REQUIRED_FOR_ARMED in entry.required_for

    @pytest.mark.parametrize("name", ORDER_ONLY)
    def test_order_and_cancel_values_are_armed_only(self, name):
        entry = _entry(name)
        assert entry.required_for == frozenset({REQUIRED_FOR_ARMED}), name

    def test_observe_requires_strictly_fewer_values_than_armed(self):
        observe = {e.name for e in matrix_entries_for(REQUIRED_FOR_OBSERVE)}
        armed = {e.name for e in matrix_entries_for(REQUIRED_FOR_ARMED)}
        assert observe < armed
        assert armed == {e.name for e in VERIFICATION_MATRIX}

    def test_armed_is_not_relaxed(self):
        """ARMED must still require all nine."""
        assert len(matrix_entries_for(REQUIRED_FOR_ARMED)) == len(VERIFICATION_MATRIX)


class TestClassificationMatchesTheSource:
    """Derived from references, not from names."""

    def _assigned_constant(self, name):
        """The module-level constant each matrix value is built from."""
        return {
            "order_path": "ORDER_PATH",
            "cancel_path": "CANCEL_PATH",
            "price_path": "PRICE_PATH",
            "order_tr_id_live_buy": "TR_ID_ORDER_US",
            "cancel_tr_id_live": "TR_ID_CANCEL",
            "cancel_tr_id_paper": "TR_ID_CANCEL",
        }.get(name)

    def _methods_referencing(self, constant):
        tree = ast.parse(BROKER_SOURCE)
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and child.id == constant:
                    found.add(node.name)
        return found

    @pytest.mark.parametrize("name", ["order_path", "order_tr_id_live_buy"])
    def test_order_values_are_referenced_only_by_submission(self, name):
        methods = self._methods_referencing(self._assigned_constant(name))
        assert methods, name
        assert methods <= {"submit_order"}, f"{name} referenced by {methods}"

    @pytest.mark.parametrize("name", ["cancel_path", "cancel_tr_id_live",
                                      "cancel_tr_id_paper"])
    def test_cancel_values_are_referenced_only_by_cancellation(self, name):
        methods = self._methods_referencing(self._assigned_constant(name))
        assert methods, name
        assert methods <= {"cancel_order"}, f"{name} referenced by {methods}"

    def test_price_path_is_referenced_by_the_read_path(self):
        methods = self._methods_referencing("PRICE_PATH")
        assert "get_current_price" in methods, methods

    def test_the_order_exchange_code_space_is_used_by_account_reads(self):
        """The finding that makes name-based classification unsafe."""
        tree = ast.parse(BROKER_SOURCE)
        sweeping = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and child.id == "_sweep_exchanges":
                    sweeping.add(node.name)
                if (isinstance(child, ast.Attribute) and child.attr == "_sweep_exchanges"):
                    sweeping.add(node.name)
        assert {"get_account_snapshot", "get_positions", "get_open_orders",
                "get_fills"} <= sweeping, sweeping
        assert "OVRS_EXCG_CD" in BROKER_SOURCE

    def test_the_sweep_uses_the_order_code_space(self):
        from domain.exchange import supported_kis_order_exchange_codes

        assert set(supported_kis_order_exchange_codes()) == {"NASD", "NYSE", "AMEX"}


class TestSingleSourceOfTruth:
    def test_the_accessors_are_the_only_place_the_split_is_computed(self):
        """A second hand-written list elsewhere is how a value that
        matters gets silently un-gated."""
        offenders = []
        for path in (REPO_ROOT / "live_pilot").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for value in ORDER_ONLY + OBSERVE_VALUES:
                if f'"{value}"' in text or f"'{value}'" in text:
                    offenders.append(f"{path.name}:{value}")
        assert offenders == [], offenders

    def test_preflight_asks_the_matrix_rather_than_listing_names(self):
        text = (REPO_ROOT / "live_pilot" / "preflight.py").read_text(encoding="utf-8")
        assert "pending_items_for" in text
        assert "matrix_entries_for" in text

    def test_pending_items_for_agrees_with_the_matrix(self):
        for posture in (REQUIRED_FOR_OBSERVE, REQUIRED_FOR_ARMED):
            expected = tuple(e.name for e in VERIFICATION_MATRIX
                             if posture in e.required_for
                             and e.live_status == LIVE_RESPONSE_PENDING)
            assert pending_items_for(posture) == expected


# =====================================================================
# The preflight gate.
# =====================================================================

def _observe_env():
    return {"KIS_LIVE_ORDER_ENABLED": "false", "LIVE_ROLLOUT_ENABLED": "false",
            "ENTRY_DISABLED": "true"}


def _armed_env():
    return {"KIS_LIVE_ORDER_ENABLED": "true", "LIVE_ROLLOUT_ENABLED": "true",
            "ENTRY_DISABLED": "false"}


def _patched_matrix(monkeypatch, **statuses):
    """Rewrites live_status for named entries, leaving required_for as
    shipped."""
    rewritten = tuple(
        entry._replace(live_status=statuses.get(entry.name, entry.live_status))
        for entry in VERIFICATION_MATRIX
    )
    monkeypatch.setattr(kis_broker, "VERIFICATION_MATRIX", rewritten)
    monkeypatch.setattr(
        kis_broker, "LIVE_RESPONSE_PENDING_ITEMS",
        tuple(e.name for e in rewritten if e.live_status == LIVE_RESPONSE_PENDING))
    return rewritten


class TestObserveIsAllowedWithArmedOnlyPending:
    def test_the_shipped_matrix_lets_observe_run_on_live(self, monkeypatch):
        monkeypatch.setattr(os, "environ", {**os.environ, **_observe_env()})
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        assert report.failures == [], report.render()

    def test_the_armed_gap_is_reported_as_a_warning(self, monkeypatch):
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        warned = [r for r in report.warnings
                  if r["check"] == "armed_response_requirements"]
        assert warned, report.render()
        assert warned[0]["reason_code"] == "BLOCKED_FOR_ARMED_ONLY"
        assert "cancel_tr_id_live" in warned[0]["detail"]

    def test_the_warning_is_not_a_failure(self, monkeypatch):
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        assert report.passed is True

    def test_observe_names_how_many_values_it_needs(self, monkeypatch):
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        row = next(r for r in report.rows if r["check"] == "live_response_pending")
        assert "OBSERVE requires 3" in row["detail"]


class TestObserveValuesStillGateObserve:
    @pytest.mark.parametrize("name", OBSERVE_VALUES)
    def test_a_pending_observe_value_blocks_observe(self, monkeypatch, name):
        _patched_matrix(monkeypatch, **{name: LIVE_RESPONSE_PENDING})
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        failures = [r for r in report.failures if r["check"] == "live_response_pending"]
        assert failures, report.render()
        assert failures[0]["reason_code"] == "LIVE_RESPONSE_PENDING"
        assert name in failures[0]["detail"]

    def test_the_refusal_names_no_bypass_variable(self, monkeypatch):
        _patched_matrix(monkeypatch, price_field_last=LIVE_RESPONSE_PENDING)
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_OBSERVE)
        detail = report.failures[0]["detail"]
        assert "there is no environment variable that skips this" in detail


class TestArmedStaysBlocked:
    @pytest.mark.parametrize("name", ORDER_ONLY)
    def test_any_pending_armed_value_blocks_armed(self, monkeypatch, name):
        confirmed = {e.name: LIVE_RESPONSE_CONFIRMED for e in VERIFICATION_MATRIX}
        confirmed[name] = LIVE_RESPONSE_PENDING
        _patched_matrix(monkeypatch, **confirmed)
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_ARMED)
        failures = [r for r in report.failures if r["check"] == "live_response_pending"]
        assert failures, f"{name} did not block ARMED"
        assert name in failures[0]["detail"]

    def test_armed_passes_only_when_every_value_is_confirmed(self, monkeypatch):
        _patched_matrix(monkeypatch,
                        **{e.name: LIVE_RESPONSE_CONFIRMED for e in VERIFICATION_MATRIX})
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_ARMED)
        assert report.failures == [], report.render()

    def test_the_shipped_matrix_blocks_armed_today(self, monkeypatch):
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_ARMED)
        assert report.failures, "ARMED must still be blocked"

    def test_no_armed_warning_row_when_already_armed(self, monkeypatch):
        """The warning exists to tell an OBSERVE operator what arming
        still needs; in ARMED it is the failure itself."""
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "live", posture=posture_module.POSTURE_ARMED)
        assert not [r for r in report.rows
                    if r["check"] == "armed_response_requirements"]


class TestPaperIsUnchanged:
    def test_paper_still_treats_pending_as_information(self, monkeypatch):
        report = preflight.PreflightReport()
        preflight.check_live_response_pending(
            report, "paper", posture=posture_module.POSTURE_OBSERVE)
        row = next(r for r in report.rows if r["check"] == "live_response_pending")
        assert row["status"] in (preflight.RESULT_INFO, preflight.RESULT_PASS)
        assert report.passed


class TestNoBypassSwitch:
    @pytest.mark.parametrize("name", ["SKIP_LIVE_RESPONSE_CHECK", "IGNORE_PENDING",
                                      "FORCE_OBSERVE", "ALLOW_UNVERIFIED_KIS",
                                      "SKIP_VERIFICATION"])
    def test_no_bypass_environment_variable_exists(self, name):
        for path in (REPO_ROOT / "live_pilot" / "preflight.py",
                     REPO_ROOT / "brokers" / "kis_broker.py",
                     REPO_ROOT / "scripts" / "start_live_pilot.sh",
                     REPO_ROOT / "scripts" / "run_live_pilot.py"):
            assert name not in path.read_text(encoding="utf-8"), f"{name} in {path.name}"

    def test_the_posture_decides_and_the_environment_cannot_override_it(self, monkeypatch):
        """The requirement level follows the posture, which follows the
        three order flags -- there is no separate knob."""
        observe = posture_module.resolve_posture(_observe_env())
        armed = posture_module.resolve_posture(_armed_env())
        assert observe.posture == posture_module.POSTURE_OBSERVE
        assert armed.posture == posture_module.POSTURE_ARMED


# =====================================================================
# OBSERVE still reaches no order code.
# =====================================================================

class TestObserveTouchesNoOrderPath:
    def test_the_probe_script_cannot_order(self):
        source = (REPO_ROOT / "scripts" / "verify_kis_observe_responses.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not [m for m in imported if "execution_engine" in m or "adapter" in m]
        for forbidden in ("submit_order(", "cancel_order("):
            assert forbidden not in source.replace('"submit_order", "cancel_order"', "")

    def test_the_probe_allow_list_is_read_only(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import importlib

            module = importlib.import_module("verify_kis_observe_responses")
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))
        assert module.ReadOnlyBroker.ALLOWED == frozenset({
            "get_current_price", "get_account_snapshot", "get_positions",
            "get_open_orders", "get_fills", "config"})
        assert "submit_order" in module.FORBIDDEN_METHODS
        assert "cancel_order" in module.FORBIDDEN_METHODS

    def test_a_forbidden_method_raises_before_any_request(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import importlib

            module = importlib.import_module("verify_kis_observe_responses")
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))

        class _Broker:
            def submit_order(self, *args, **kwargs):     # pragma: no cover
                raise AssertionError("reached the real broker")

        proxy = module.ReadOnlyBroker(_Broker())
        with pytest.raises(module.ReadOnlyViolation):
            proxy.submit_order
