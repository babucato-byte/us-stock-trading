"""Shared test setup: keep every test out of real, shared state.

This file is the union of two conftests that were written independently
on two branches -- the scanner framework's and the KIS live path's --
and merged when the two were integrated. They had no overlap: three
autouse fixtures, three disjoint sets of state, no shared names. The
merge is therefore a concatenation, and each fixture keeps the reasoning
of whoever wrote it.

The pattern they share is worth stating once. Every one of these modules
resolves its path at CALL time rather than at import, so redirecting a
module-level constant in a single test does not cover the call made
three frames deeper. Each fixture below redirects the ENVIRONMENT (or
patches the module attribute the code actually reads), which is the only
place that covers every entry point.

What they protect, concretely:

    scanner analytics   month 1's signal dataset, which the entire
                        discovery exercise depends on being clean
    notification health a test that merely stubs a Slack call could
                        otherwise escalate the REAL kill switch
    KIS rate limiter    unisolated tests would genuinely sleep three
                        seconds per read and leave a token cache and a
                        candidate CSV in the repository root
"""

import sys
from pathlib import Path

import pytest

import kill_switch_state as kss
import notification_health as nh

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


@pytest.fixture(autouse=True)
def _isolate_health_and_kill_switch_state_files(monkeypatch, tmp_path):
    """CODEX-017: paper_strategy_order._safe_send_slack_alert() now always
    routes through notification_health.send_with_health_tracking(), which can
    in turn escalate kill_switch_state. Without this, any test that merely
    monkeypatches pso.send_slack_alert (there are several, none of which are
    testing notification_health/kill_switch_state themselves) would silently
    read/write the real NOTIFICATION_HEALTH_STATE.json / notification_health.log
    / KILL_SWITCH_STATE.json at the repo root as a side effect, and could even
    escalate the real kill switch if consecutive failures crossed the
    threshold across tests. Point both at tmp_path by default; a test that
    wants to exercise these modules directly (e.g. test_notification_health.py,
    test_paper_strategy_order_notification_health.py) still monkeypatches its
    own path afterwards, which simply overrides this default.
    """
    monkeypatch.delenv("NOTIFICATION_HEALTH_STATE_FILE", raising=False)
    monkeypatch.delenv("NOTIFICATION_HEALTH_LOG_FILE", raising=False)
    monkeypatch.setattr(nh, "STATE_FILE", tmp_path / "NOTIFICATION_HEALTH_STATE.json")
    monkeypatch.setattr(nh, "LOG_FILE", tmp_path / "notification_health.log")

    monkeypatch.delenv("KILL_SWITCH_STATE_FILE", raising=False)
    monkeypatch.setattr(kss, "STATE_FILE", tmp_path / "KILL_SWITCH_STATE.json")


@pytest.fixture(autouse=True)
def _isolate_kis_rate_limiter(monkeypatch, tmp_path):
    """HIGH-2: the KIS rate limiter paces every read by a real 3 seconds
    and records the budget in a shared file at the repo root. Without
    this, every unrelated KIS test would genuinely sleep and would drop a
    rate-limit state file into the repository.

    Only the WAITING is neutralized, and only for tests that are not about
    pacing: tests/test_kis_rate_limiting.py injects its own limiter with a
    virtual clock and asserts the real intervals there.
    """
    from brokers import kis_rate_limiter, kis_token_cache

    monkeypatch.setenv("KIS_RATE_LIMIT_STATE_FILE", str(tmp_path / "KIS_RATE_LIMIT.json"))
    # MEDIUM: same reasoning for the shared token cache -- an unisolated
    # test would read/write a real token file at the repo root.
    monkeypatch.setenv("KIS_TOKEN_CACHE_FILE", str(tmp_path / "KIS_TOKEN_CACHE.json"))
    # Same reasoning again for the shared candidate store. The scanner
    # publishes to it from save_candidate_files(), and that path is
    # resolved at call time -- so redirecting the scanner's module-level
    # file constants, as several tests do, does NOT cover it. Without
    # this, any test that runs the scanner publishes a candidate CSV and
    # manifest into the repository root.
    monkeypatch.setenv("KIS_CANDIDATE_DIR", str(tmp_path / "candidates"))
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("KIS_TOKEN_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("KIS_ORDER_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("KIS_RATE_LIMIT_BASE_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("KIS_RATE_LIMIT_MAX_BACKOFF_SECONDS", "0")
    kis_rate_limiter.reset_limiter()
    kis_token_cache.reset_cache()
    yield
    kis_rate_limiter.reset_limiter()
    kis_token_cache.reset_cache()
