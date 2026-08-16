"""Shadow evaluation is gated separately from live trading, and the
read-only verifier cannot reach an order path.

Oracle verification found Shadow silently evaluating nothing: the loop
reused LIVE_ROLLOUT_ALLOWED_SYMBOLS, which is empty in the read-only
posture (correctly -- nothing may be traded yet). Gating the one tool
meant to OBSERVE candidates on a live-TRADING control made it a no-op.
"""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _shadow_module():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        module = importlib.import_module("run_shadow_mode")
        return importlib.reload(module)
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


class _Rollout:
    def __init__(self, allowed=frozenset()):
        self.allowed_symbols = allowed


class TestShadowAllowlistIsSeparate:
    def test_unset_evaluates_everything(self, monkeypatch):
        """The regression: an empty live allow-list must not silence
        Shadow. With nothing configured, every candidate is evaluated."""
        monkeypatch.delenv("SHADOW_ALLOWED_SYMBOLS", raising=False)
        module = _shadow_module()
        assert module.shadow_allowed_symbols(_Rollout(frozenset())) is None

    def test_an_empty_live_rollout_does_not_restrict_shadow(self, monkeypatch):
        monkeypatch.delenv("SHADOW_ALLOWED_SYMBOLS", raising=False)
        module = _shadow_module()
        # Live rollout allows nothing -- Shadow still evaluates.
        assert module.shadow_allowed_symbols(_Rollout(frozenset())) is None

    def test_shadow_allowlist_restricts_only_shadow(self, monkeypatch):
        monkeypatch.setenv("SHADOW_ALLOWED_SYMBOLS", "BBVA,GFL")
        module = _shadow_module()
        allowed = module.shadow_allowed_symbols(_Rollout(frozenset({"AAPL"})))
        assert allowed == frozenset({"BBVA", "GFL"})

    @pytest.mark.parametrize("raw", ["", "   ", ",", " , "])
    def test_a_blank_shadow_allowlist_means_everything(self, monkeypatch, raw):
        monkeypatch.setenv("SHADOW_ALLOWED_SYMBOLS", raw)
        module = _shadow_module()
        assert module.shadow_allowed_symbols(_Rollout(frozenset())) is None

    def test_it_is_case_and_space_insensitive(self, monkeypatch):
        monkeypatch.setenv("SHADOW_ALLOWED_SYMBOLS", " bbva , gfl ")
        module = _shadow_module()
        assert module.shadow_allowed_symbols(_Rollout()) == frozenset({"BBVA", "GFL"})

    def test_the_live_rollout_allowlist_is_still_enforced_in_the_gate(self):
        """Separation must not weaken the live control: the Order Gate
        still receives rollout.allowed_symbols for every evaluation."""
        source = (SCRIPTS_DIR / "run_shadow_mode.py").read_text(encoding="utf-8")
        assert "allowed_symbols=rollout.allowed_symbols" in source

    def test_the_evaluation_loop_no_longer_uses_the_live_allowlist(self):
        source = (SCRIPTS_DIR / "run_shadow_mode.py").read_text(encoding="utf-8")
        assert "if symbol not in rollout.allowed_symbols:" not in source, (
            "Shadow evaluation is gated on the live-trading allow-list again"
        )


class TestVerifierCannotOrder:
    def test_the_script_never_imports_the_execution_engine(self):
        """Checked on the parsed IMPORTS, not the text -- the docstring
        legitimately names the module it refuses to import."""
        import ast

        tree = ast.parse((SCRIPTS_DIR / "verify_kis_live_responses.py")
                         .read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
                    imported.update(f"{node.module}.{a.name}" for a in node.names)
        offenders = [name for name in imported if "execution_engine" in name]
        assert offenders == [], offenders

    def test_the_readonly_proxy_refuses_state_mutating_methods(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            module = importlib.import_module("verify_kis_live_responses")
            importlib.reload(module)
        finally:
            sys.path.remove(str(SCRIPTS_DIR))

        class _Broker:
            config = object()

            def submit_order(self, *a, **k):    # pragma: no cover -- must be unreachable
                raise AssertionError("an order was submitted by the verifier")

            def cancel_order(self, *a, **k):    # pragma: no cover
                raise AssertionError("a cancel was submitted by the verifier")

            def get_positions(self):
                return []

        proxy = module.ReadOnlyBroker(_Broker())
        for forbidden in module.FORBIDDEN_METHODS:
            with pytest.raises(module.ReadOnlyViolation):
                getattr(proxy, forbidden)
        # ...while the read side still works.
        assert proxy.get_positions() == []

    def test_an_unlisted_method_is_also_refused(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            module = importlib.reload(importlib.import_module("verify_kis_live_responses"))
        finally:
            sys.path.remove(str(SCRIPTS_DIR))

        class _Broker:
            def something_else(self):   # pragma: no cover
                return "nope"

        with pytest.raises(module.ReadOnlyViolation):
            module.ReadOnlyBroker(_Broker()).something_else

    def test_help_runs_without_touching_kis(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "verify_kis_live_responses.py"), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
        )
        assert result.returncode == 0, result.stderr[-400:]
        assert "places no orders" in result.stdout

    def test_it_contains_no_order_submission_call(self):
        """No CALL to a state-mutating broker method anywhere in the AST.
        The method names appear only as strings in the refusal list."""
        import ast

        tree = ast.parse((SCRIPTS_DIR / "verify_kis_live_responses.py")
                         .read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
                elif isinstance(func, ast.Name):
                    called.add(func.id)
        for forbidden in ("submit_order", "cancel_order", "amend_order", "revise_order"):
            assert forbidden not in called, f"{forbidden}() is called by the verifier"
