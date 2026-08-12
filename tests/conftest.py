"""Test setup for the scanner framework.

Scoped deliberately narrowly: this file exists to keep scanner tests
from touching real state, and to make the repository root importable.
It carries no fixtures for the trading modules, so it stays valid on a
branch where those modules are not present.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# `pytest.ini` here sets `testpaths` and `python_files` but not
# `pythonpath`, so nothing puts the repository root on `sys.path` for a
# test run. Without it, `from tests import scanner_fixtures` and
# `import scanners...` resolve only because each test module happens to
# insert the root itself before its first import -- which works until a
# module is added that does not, and then fails as an import error with
# no obvious cause. conftest.py is imported before any test module, so
# doing it once here makes it deterministic instead of incidental.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_scanner_analytics_store(monkeypatch, tmp_path):
    """Keep scanner tests out of the real Month 1 analytics and log stores.

    `scanners/base/result_store.py` and `scanners/base/scanner_logging.py`
    resolve their paths at CALL time from the project root, so a test
    that runs a scanner would otherwise append signal rows into the real
    `logs/scanners/` tree and contaminate the dataset the whole exercise
    depends on being clean. Redirecting the environment variables covers
    every entry point, including those that resolve the directory deep
    inside a call.
    """
    monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "scanner_analytics"))
    monkeypatch.setenv("SCANNER_LOG_DIR", str(tmp_path / "scanner_logs"))
