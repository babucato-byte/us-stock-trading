"""pytest must not be able to take a production S6 cycle lock.

2026-09-01, during a host gate run:

    [SCAN CYCLE] skipped -- orb: a orb scan started at
    2026-09-01T06:17:29 (pid 3697118) is still running

A test called `runner.main()`, `candidate_dir()` resolved the LIVE shared
store from TRADING_PROJECT_ROOT, and the test collided with the real
`orb` scan. It read the refusal as its own answer.

The failure had two directions and the quieter one is worse. A test
reading a refusal is a false red. A test WINNING the race takes the live
S6 cycle lock, and the production scan behind it stands down -- `flock -n`
does not queue. That is a test suite deciding a live strategy does not
scan this cycle.

So: every test module that reaches `runner.main` must isolate its store.
Enforced structurally rather than by habit, because the habit is exactly
what failed.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TESTS = REPO_ROOT / "tests"

#: Modules that deliberately exercise an UNSET store, because refusing to
#: guess a path is the behaviour under test. They must not be given one.
ASSERTS_MISCONFIGURATION = {
    "test_candidate_handoff.py",
    "test_s2_candidate_source.py",
    "test_no_production_path_fallback.py",
    "test_no_production_cycle_lock.py",
}

ISOLATION_MARKERS = ("SCANNER_CANDIDATE_DIR", "CANDIDATE_DIR_ENV")


def _calls_runner_main(path):
    """`runner.main(...)` where `runner` really is `scanners.runner`.

    The name alone is not enough: `test_universe_tradable_build.py` binds
    `runner` to the universe builder and calls `runner.main(logger=...)`,
    which takes no cycle lock and needs no store. Matching on the
    attribute would have flagged it and taught the next person to silence
    the guard rather than trust it.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - a broken test file is its own failure
        return False

    bound_to_scanner_runner = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "scanners.runner":
                    bound_to_scanner_runner = True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("scanners"):
                for alias in node.names:
                    if alias.name == "runner":
                        bound_to_scanner_runner = True
    if not bound_to_scanner_runner:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "main":
            if getattr(func.value, "id", None) == "runner":
                return True
    return False


def _modules_reaching_main():
    return sorted(p for p in TESTS.glob("test_*.py")
                  if _calls_runner_main(p))


def test_the_guard_can_still_find_the_call_sites():
    """If this ever finds nothing, the guard below is vacuous."""
    assert _modules_reaching_main(), "no module calls runner.main -- guard is dead"


@pytest.mark.parametrize("path", _modules_reaching_main(),
                         ids=lambda p: p.name)
def test_every_module_reaching_main_isolates_its_store(path):
    if path.name in ASSERTS_MISCONFIGURATION:
        pytest.skip("deliberately exercises an unset store")
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert any(marker in text for marker in ISOLATION_MARKERS), (
        f"{path.name} calls runner.main() without isolating the candidate "
        "store; on a release host that resolves the LIVE shared store and "
        "can take the production S6 cycle lock")


def test_the_production_store_is_never_a_test_default():
    """`candidate_dir` must refuse rather than guess -- the property that
    makes isolation possible to enforce at all."""
    from scanners.publish import candidates as publisher

    source = (REPO_ROOT / "scanners" / "publish" / "candidates.py").read_text()
    assert "raise CandidateHandoffMisconfigured" in source
    assert publisher.CANDIDATE_DIR_ENV == "SCANNER_CANDIDATE_DIR"
