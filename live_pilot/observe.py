"""OBSERVE-posture dispatch: evaluate everything, submit nothing.

Both halves delegate to the two service entrypoints that already exist
and are already covered by
`tests/test_oracle_deploy_package.py::TestShadowServicesCannotOrder`,
which proves at the AST level that neither imports the execution engine,
the KIS adapter or kis_position_manager, and that neither contains a
call to submit_order/cancel_order/submit_buy_order/submit_sell_order/
check_and_manage. Re-implementing either evaluation here would create a
second copy of the entry gate and the exit rules that could drift from
the live path -- the exact failure the Shadow services were built to
avoid.

They live in `scripts/`, which is not an importable package, so they are
loaded from their file paths. That is a load, not a run: both files
guard their `main()` behind `if __name__ == "__main__"`, and they are
given a module name that is not `__main__`.
"""

import importlib.util
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

ENTRY_ENTRYPOINT = "run_shadow_mode"
EXIT_ENTRYPOINT = "run_shadow_exit_evaluation"

_MODULE_CACHE = {}
_CACHE_LOCK = threading.Lock()


class EntrypointUnavailable(Exception):
    """A service entrypoint the pilot delegates to is missing or does not
    import. Fail closed: the pilot does not fall back to a private copy
    of the evaluation."""


def load_entrypoint(name, *, scripts_dir=None):
    """Loads `scripts/<name>.py` as a module, once per process."""
    directory = Path(scripts_dir) if scripts_dir is not None else SCRIPTS_DIR
    path = directory / f"{name}.py"
    key = str(path)
    with _CACHE_LOCK:
        cached = _MODULE_CACHE.get(key)
        if cached is not None:
            return cached
        if not path.is_file():
            raise EntrypointUnavailable(f"{path} does not exist")
        spec = importlib.util.spec_from_file_location(f"live_pilot._entry_{name}", path)
        if spec is None or spec.loader is None:
            raise EntrypointUnavailable(f"{path} could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a pilot failure
            sys.modules.pop(spec.name, None)
            raise EntrypointUnavailable(f"{path} failed to import: {exc}") from exc
        _MODULE_CACHE[key] = module
        return module


def evaluate_entries(*, broker, watchlist, now):
    """One entry-evaluation pass. Returns the tick's `entry` section."""
    module = load_entrypoint(ENTRY_ENTRYPOINT)
    outcomes = module.run_once(broker=broker, watchlist=watchlist, now=now)
    return {
        "mode": "OBSERVE",
        "entrypoint": f"{ENTRY_ENTRYPOINT}.run_once",
        "evaluations": len(outcomes),
        "outcomes": [
            {
                "symbol": outcome.get("symbol"),
                "result": outcome.get("result"),
                "reason_code": outcome.get("reason_code"),
                "hypothetical": outcome.get("hypothetical"),
                "run_id": outcome.get("run_id"),
            }
            for outcome in outcomes
        ],
        "submitted": [],
        "error": None,
    }


def evaluate_exits(*, broker, now):
    """One exit-evaluation pass. Returns the tick's `exit` section.

    A position count of zero is a legitimate result, not a failure: in
    OBSERVE nothing was ever bought, so there is nothing to exit. That is
    the same structural gap SHADOW_MODE_EXIT_CRITERIA G5 records, and it
    is why an ARMED paper pilot is the thing that actually exercises the
    sell path.
    """
    module = load_entrypoint(EXIT_ENTRYPOINT)
    result = module.run_once(broker=broker, now=now)
    evaluated = result.get("evaluated") or []
    return {
        "mode": "OBSERVE",
        "entrypoint": f"{EXIT_ENTRYPOINT}.run_once",
        "status": result.get("status"),
        "halt": result.get("halt"),
        "evaluations": len(evaluated),
        "outcomes": [
            {
                "symbol": outcome.get("symbol"),
                "position_id": outcome.get("position_id"),
                "decision": outcome.get("decision"),
                "result": outcome.get("result"),
                "reason_code": outcome.get("reason_code"),
                "exit_classification": outcome.get("exit_classification"),
            }
            for outcome in evaluated
        ],
        "error": None,
    }
