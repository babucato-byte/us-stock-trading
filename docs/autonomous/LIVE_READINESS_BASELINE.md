# Live Readiness Baseline

Baseline snapshot of the full regression test suite, captured before any
live-readiness remediation work begins. No source files were modified to
produce this snapshot.

## Environment

- Python: 3.9.6 (`venv/bin/python`)
- Commit hash: `158671ede0320c4c22179b75cb76c4e9eb8ae1fa` (branch `orchestrator/20260722-021713-us-stock-trading`)
- Working tree: clean at time of capture
- Captured (UTC): 2026-07-21T17:18:18Z

## Command

```
venv/bin/python -m pytest -q
```

## Result

- Exit code: `0`
- Collected/passed: `267 passed` (0 failed, 0 errors, 2 warnings)
- Duration: 22.32s

## Raw output (tail)

```
........................................................................ [ 26%]
........................................................................ [ 53%]
........................................................................ [ 80%]
...................................................                      [100%]
=============================== warnings summary ===============================
venv/lib/python3.9/site-packages/urllib3/__init__.py:35
  /Users/jihoonhan/Projects/us-stock-trading/venv/lib/python3.9/site-packages/urllib3/__init__.py:35: NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+, currently the 'ssl' module is compiled with 'LibreSSL 2.8.3'. See: https://github.com/urllib3/urllib3/issues/3020
    warnings.warn(

tests/test_scanner.py::test_unknown_field_skips_with_warning
  /Users/jihoonhan/Projects/us-stock-trading/daily_candidate_scanner.py:513: RuntimeWarning: Unsupported scanner field 'unknown_metric' skipped.
    warn_skip(f"Unsupported scanner field 'unknown_metric' skipped.")

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
267 passed, 2 warnings in 22.32s
```
