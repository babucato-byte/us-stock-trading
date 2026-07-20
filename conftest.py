import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Ensure the repository root is importable regardless of invocation cwd or
# whether pytest.ini's `pythonpath = .` was applied for this run (CODEX-004).
# A conftest.py at the collected directory's own path is loaded by pytest
# very early in the collection walk, before any test module import, so this
# runs even when the ini file itself wasn't picked up.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# These are root-level ad-hoc scratch scripts, not part of the official
# tests/ suite pytest.ini's testpaths points at. They happen to match
# pytest's default python_files pattern (test_*.py / *_test.py), and
# several of them run real network calls (yfinance, Alpaca, Slack) at
# import time. `testpaths` is only honored when pytest is invoked with no
# path arguments — an invocation that names a directory explicitly (e.g.
# `pytest us-stock-trading` run from its parent directory) ignores
# testpaths and would otherwise collect these too (CODEX-005). This guard
# is enforced here because collect_ignore is consulted per-directory during
# the actual file walk, independent of how/whether the ini file resolved
# for that invocation.
collect_ignore = [
    "test.py",
    "ma_test.py",
    "indicator_test.py",
    "slack_test.py",
    "test_alpaca_account.py",
    "test_paper_order.py",
    "test_stock.py",
    "test_order_safety.py",
]
