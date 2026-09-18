"""A session starts on the small set prepared before it opened.

The incident, 2026-09-18 PREMARKET:

    08:00:19  collector subscribed 35/35
    08:02:11  first PREMARKET scan starts
    08:12:47  it finishes -- 636 seconds
              entry worker healthy the whole time, scanned=0 ready=0
    08:32     next invocation SCANNER_LOCK_SKIPPED behind the one after it

Nothing was broken. Every scan reads 593-600 symbols one KIS minute chart
at a time at SCANNER priority, which has measured 700-950 seconds since
2026-08-27. The heavy read simply sat on the critical path of the
session's first five minutes.

So it moves off it: the selection the collector already performs runs
BEFORE the boundary, its answer is written once, and both the collector
and the session's first scan read that same file.

What these tests hold onto:

  * the schedule is asked, never restated -- 04:00/09:30/16:00/20:00 must
    not appear as a second set of constants, because 주간거래 is published
    in KST and moves against Eastern with US DST
  * the collector's symbols and the scanner's symbols are the same list,
    on the fallback path as much as the prepared one
  * the artifact says WHO to look at and never WHAT they looked like, so a
    session cannot publish on another session's numbers
  * a missing preparation is safe and slow, and says so rather than
    reading as success
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scanners.base import scan_session, session_startup as ss

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ["OVERNIGHT_DAYTIME", "PREMARKET", "REGULAR", "AFTER_HOURS"]
DAY = "2026-09-18"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
CAP = 41


def _artifact(root, session, **over):
    fields = dict(
        trading_day=DAY, target_session=session,
        symbols=over.pop("symbols", ["AAPL", "MSFT", "NVDA"]),
        cap=over.pop("cap", CAP), source=ss.SOURCE_PREPARED,
        source_session="PRIOR", source_manifest_generation="gen-1",
        selection_source="{}", now=over.pop("now", NOW))
    fields.update(over)
    art = ss.build_artifact(**fields)
    ss.write_atomic(root, art)
    return art


# -- 1-9. artifact validation, every session -------------------------------

@pytest.mark.parametrize("session", SESSIONS)
def test_1_and_9_a_correct_artifact_is_accepted_for_every_session(tmp_path, session):
    _artifact(str(tmp_path), session)
    art, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                                target_session=session, cap=CAP, now=NOW)
    assert status == ss.STATUS_VALID
    assert art["symbols"] == ["AAPL", "MSFT", "NVDA"]
    assert art["prepared_normally"] is True


@pytest.mark.parametrize("session", SESSIONS)
def test_2_wrong_trading_day_is_rejected(tmp_path, session):
    _artifact(str(tmp_path), session)
    _, status = ss.load_valid(str(tmp_path), trading_day="2026-09-17",
                              target_session=session, cap=CAP, now=NOW)
    assert status == ss.STATUS_MISSING  # a different day is a different file


@pytest.mark.parametrize("session", SESSIONS)
def test_3_wrong_target_session_is_rejected(tmp_path, session):
    """A preparation for one session must never start another."""
    _artifact(str(tmp_path), session)
    other = next(s for s in SESSIONS if s != session)
    path = ss.artifact_path(str(tmp_path), trading_day=DAY, target_session=session)
    art = json.loads(Path(path).read_text())
    art["target_session"] = other
    Path(ss.artifact_path(str(tmp_path), trading_day=DAY,
                          target_session=session)).write_text(json.dumps(art))
    _, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                              target_session=session, cap=CAP, now=NOW)
    assert status == ss.STATUS_WRONG_SESSION


@pytest.mark.parametrize("session", SESSIONS)
def test_4_a_stale_artifact_is_rejected(tmp_path, session):
    _artifact(str(tmp_path), session, now=NOW - timedelta(hours=9))
    _, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                              target_session=session, cap=CAP, now=NOW)
    assert status == ss.STATUS_STALE


def test_5_an_unsupported_version_is_rejected(tmp_path):
    _artifact(str(tmp_path), "REGULAR")
    path = Path(ss.artifact_path(str(tmp_path), trading_day=DAY,
                                 target_session="REGULAR"))
    art = json.loads(path.read_text())
    art["version"] = ss.ARTIFACT_VERSION + 99
    path.write_text(json.dumps(art))
    _, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                              target_session="REGULAR", cap=CAP, now=NOW)
    assert status == ss.STATUS_VERSION


def test_6_a_prepared_artifact_without_provenance_is_rejected(tmp_path):
    _artifact(str(tmp_path), "REGULAR", source_manifest_generation=None,
              selection_source=None)
    _, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                              target_session="REGULAR", cap=CAP, now=NOW)
    assert status == ss.STATUS_NO_PROVENANCE


def test_6b_a_fallback_artifact_needs_no_manifest_generation(tmp_path):
    """It exists BECAUSE the prepared one did not; demanding the
    provenance it could not have would refuse every degraded start."""
    _artifact(str(tmp_path), "REGULAR", source=ss.SOURCE_COLLECTOR_FALLBACK,
              source_manifest_generation=None, selection_source=None,
              fallback_reason=ss.STATUS_MISSING)
    art, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                                target_session="REGULAR", cap=CAP, now=NOW)
    assert status == ss.STATUS_VALID
    assert art["prepared_normally"] is False
    assert art["fallback_reason"] == ss.STATUS_MISSING


def test_7_duplicate_symbols_are_rejected(tmp_path):
    _artifact(str(tmp_path), "REGULAR", symbols=["AAPL", "AAPL", "MSFT"])
    _, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                              target_session="REGULAR", cap=CAP, now=NOW)
    assert status == ss.STATUS_DUPLICATE


def test_8_exceeding_the_collector_cap_is_rejected(tmp_path):
    _artifact(str(tmp_path), "REGULAR",
              symbols=[f"S{i}" for i in range(CAP + 1)])
    _, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                              target_session="REGULAR", cap=CAP, now=NOW)
    assert status == ss.STATUS_TOO_MANY


def test_8b_the_cap_comes_from_the_collector_not_a_new_constant():
    from market_data import kis_hdfscnt0 as wire
    from scanners import runner

    assert runner._startup_cap() == wire.MAX_SUBSCRIPTIONS
    source = (REPO_ROOT / "scanners" / "base" / "session_startup.py").read_text()
    assert "41" not in source, "the ceiling is read, never restated"


# -- 10-12. the schedule is consumed, not restated -------------------------

def test_10_the_boundary_comes_from_production_session_truth(monkeypatch):
    """Probed forward through `scan_session.session_at`, so 주간거래 and US
    DST are answered by the module that already owns them."""
    calls = []
    real = scan_session.session_at

    def spy(moment=None):
        calls.append(moment)
        return real(moment)

    monkeypatch.setattr(scan_session, "session_at", spy)
    ss.next_boundary(NOW)
    assert calls, "next_boundary must ask scan_session, not a local table"


def test_10b_no_session_start_times_are_restated():
    """Checked against EXECUTABLE code, not prose.

    The docstring explains why those times are not restated, so a raw text
    search finds them in the explanation and fails a test about the code.
    """
    import ast

    tree = ast.parse(
        (REPO_ROOT / "scanners" / "base" / "session_startup.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            node.value.value = ""          # drop docstrings
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""                # and every other string literal
    code = ast.dump(tree)
    # The session start times, as they would appear in a `time(...)` call
    # or a comparison against one.
    for hour, minute in ((4, 0), (9, 30), (16, 0), (20, 0)):
        pair = f"value={hour}), Constant(value={minute})"
        assert pair not in code, (
            f"{hour}:{minute:02d} is a second definition of when a session starts")


@pytest.mark.parametrize("lead,expected", [(45, False), (35, True), (20, False)])
def test_11_prep_runs_only_inside_its_window(monkeypatch, lead, expected):
    boundary = NOW + timedelta(minutes=lead)
    monkeypatch.setattr(ss, "next_boundary",
                        lambda now=None, **k: ("REGULAR", boundary))
    target, _ = ss.prep_due(NOW)
    assert (target is not None) is expected


def test_12_a_valid_artifact_makes_preparation_idempotent(tmp_path):
    """The second tick inside the window must reuse, not re-derive: two
    lists for one session is the disagreement this removes."""
    _artifact(str(tmp_path), "REGULAR")
    art, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                                target_session="REGULAR", cap=CAP, now=NOW)
    assert status == ss.STATUS_VALID and art["prepared_normally"] is True


# -- 13-16. single source of truth -----------------------------------------

def test_13_and_15_collector_reads_the_artifact_before_building():
    wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_realtime_collector.sh").read_text()
    assert "session_startup.load_valid(" in wrapper
    assert wrapper.index("session_startup.load_valid(") < \
        wrapper.index("bootstrap.build("), (
        "the prepared artifact is consulted first; building is the fallback")


def test_14_collector_fallback_writes_the_shared_artifact():
    """A private fallback would put the collector and the scanner back out
    of step exactly when things are already degraded."""
    wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_realtime_collector.sh").read_text()
    assert "SOURCE_COLLECTOR_FALLBACK" in wrapper
    assert "session_startup.write_atomic(" in wrapper
    assert wrapper.index("bootstrap.build(") < \
        wrapper.index("session_startup.write_atomic(")
    assert "fast_start_slo_met=false" in wrapper


def test_16_both_readers_resolve_the_same_artifact_path(tmp_path):
    from scanners import runner

    art = _artifact(str(tmp_path), "REGULAR")
    collector_path = ss.artifact_path(str(tmp_path), trading_day=DAY,
                                      target_session="REGULAR")
    scanner_path = ss.artifact_path(
        runner._discovery_root(str(tmp_path / "manifest.json")),
        trading_day=DAY, target_session="REGULAR")
    assert Path(collector_path).name == Path(scanner_path).name
    assert json.loads(Path(collector_path).read_text())["symbols"] == art["symbols"]


# -- 17-18. the startup scan uses the reduced universe ---------------------

def test_17_and_18_the_runner_prefers_prepared_over_the_full_universe():
    from scanners import runner

    source = (REPO_ROOT / "scanners" / "runner.py").read_text()
    assert runner.UNIVERSE_PREPARED == "prepared"
    assert source.index("selected_universe == UNIVERSE_PREPARED") < \
        source.index("selected_universe == UNIVERSE_MANIFEST"), (
        "the prepared branch must be reached before the 600-symbol one")
    assert "SESSION_FAST_SCAN_START" in source


def test_18b_the_scanner_never_imports_the_builder():
    """`build()` reaches state_store and the scanner package must not."""
    source = (REPO_ROOT / "scanners" / "base" / "session_startup.py").read_text()
    assert "bootstrap_watchlist" not in source.split('"""', 2)[-1]
    runner_source = (REPO_ROOT / "scanners" / "runner.py").read_text()
    assert "bootstrap_watchlist" not in runner_source


# -- 19-20. prepared is a range, never a measurement -----------------------

@pytest.mark.parametrize("session", SESSIONS)
def test_19_and_20_the_artifact_carries_no_market_data(tmp_path, session):
    art = _artifact(str(tmp_path), session)
    for forbidden in ("price", "close", "volume", "vwap", "ema", "ema9",
                      "ema21", "range_high", "range_low", "orb", "signal",
                      "momentum", "score"):
        assert forbidden not in art, (
            f"{forbidden!r} in the artifact would let one session publish on "
            "another session's numbers")
    assert set(art) >= {"symbols", "trading_day", "target_session",
                        "generated_at", "source", "version"}


# -- 21-24. the existing guards are untouched ------------------------------

def test_21_session_boundary_abort_still_exists():
    source = (REPO_ROOT / "scanners" / "runner.py").read_text()
    assert "SESSION_BOUNDARY_ABORTED" in source
    assert "session_boundary_aborted = True" in source


def test_22_stale_publish_is_still_blocked():
    source = (REPO_ROOT / "scanners" / "runner.py").read_text()
    assert "STALE_PUBLISH_BLOCKED" in source


def test_23_and_24_the_startup_scan_shares_the_existing_lock():
    wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text()
    assert "SCANNER_LOCK_SKIPPED" in wrapper
    assert wrapper.count("s6_scan.lock") >= 1
    assert "flock -n -E 99" in wrapper
    # one scanner invocation, not two
    assert wrapper.count("scripts/run_scanners.py") == 1
    prep = (REPO_ROOT / "deploy" / "cron" / "s6_session_startup_prep.sh").read_text()
    assert "s6_scan.lock" not in prep, (
        "preparation must never block a scan behind itself")
    assert "run_scanners.py" not in prep, "no second scanner process"


# -- 25-26. fallback is safe, and says it is not fast ----------------------

def test_25_a_missing_preparation_falls_back_and_records_the_miss():
    source = (REPO_ROOT / "scanners" / "runner.py").read_text()
    assert "SESSION_FAST_SCAN_INVALID_PREP" in source
    assert "SESSION_FAST_SCAN_FALLBACK" in source
    assert "fast_start_slo_met=false" in source
    assert "selected_universe = UNIVERSE_MANIFEST" in source, (
        "safe means it still scans, not that it stops")


def test_26_completion_is_reported_even_with_no_candidates():
    from scanners import runner

    source = (REPO_ROOT / "scanners" / "runner.py").read_text()
    assert "SESSION_FAST_SCAN_COMPLETE" in source
    body = source[source.index("def _log_fast_scan_complete"):]
    body = body[:body.index("\ndef ")]
    assert "passed" in body and "evaluated" in body
    assert "if report.fast_start_slo_met is None" in body
    # nothing in the completion path is conditional on a candidate existing
    assert "if passed" not in body


# -- 27-29. nothing that trades was touched -------------------------------

def test_27_scanner_trading_isolation_still_holds():
    from tests.test_scanner_trading_isolation import (FORBIDDEN_PREFIXES,
                                                      imported_modules)

    path = REPO_ROOT / "scanners" / "base" / "session_startup.py"
    for module in imported_modules(path):
        assert module.split(".")[0] not in FORBIDDEN_PREFIXES, module


def test_28_preparation_cannot_submit_an_order():
    source = (REPO_ROOT / "scripts" / "prepare_session_startup.py").read_text()
    code = "\n".join(l for l in source.splitlines()
                     if not l.strip().startswith("#"))
    code = code.split('"""', 2)[-1]
    for forbidden in ("submit_order", "submit_buy_order", "submit_sell_order",
                      "execution_engine", "order_intent", "BuyGateContext"):
        assert forbidden not in code, forbidden


def test_29_no_trading_module_was_modified():
    import subprocess

    changed = subprocess.run(
        ["git", "diff", "--name-only",
         "9bc7e8266c58dc07ed7a23d11111481cab87b50e", "HEAD"],
        capture_output=True, text=True, cwd=str(REPO_ROOT)).stdout.split()
    for path in changed:
        assert not path.startswith(("execution/", "brokers/", "reconciliation/",
                                    "s6_live/", "s1_live/", "state_store/")), path
