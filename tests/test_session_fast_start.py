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
  * the collector's symbols and the scanner's universe come from ONE
    generation but are no longer the same list -- the transport ceiling
    bounds what can be streamed and must not bound what is evaluated
  * a pre-evaluable invocation WAITS for the evaluable moment and does not
    hold the scanner lock while it waits
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


def _executable(path):
    """The file's code with every string literal blanked.

    A guard that reads the raw text cannot tell a constant from a comment
    citing the incident that produced it, and this repository has now had
    several tests fail on their own documentation. So the guards ask the
    AST.
    """
    import ast

    tree = ast.parse(Path(path).read_text()) if str(path).endswith(".py") else None
    if tree is None:
        return Path(path).read_text()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.dump(tree)


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
    """The ceiling is READ from the transport module, never restated.

    Checked against the EXECUTABLE code with string literals blanked, not
    against the file's text. The text cites the live measurements that
    produced this design -- "41 of 41 symbols rejected" -- and a guard
    that cannot tell a citation from a constant makes the documentation
    the thing that fails.
    """
    from market_data import kis_hdfscnt0 as wire

    for name in ("scanners/base/session_startup.py",
                 "scripts/strategy_startup_gate.py"):
        code = _executable(REPO_ROOT / name)
        assert "41" not in code, f"{name}: the ceiling is read, never restated"

    # And the writers that DO bound the collector read it from there.
    for name in ("scripts/prepare_session_startup.py",
                 "deploy/cron/s6_realtime_collector.sh"):
        text = (REPO_ROOT / name).read_text()
        assert "MAX_SUBSCRIPTIONS" in text, name
    assert isinstance(wire.MAX_SUBSCRIPTIONS, int)


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


# -- 17-18. the startup scan uses the MANIFEST, like every other scan -----

def test_17_and_18_the_startup_scan_uses_the_manifest():
    """The reduced startup universe is GONE.

    It handed the scanner the collector's prepared list, which made one
    appkey's subscription ceiling the strategy's universe ceiling: on
    2026-09-19 a REGULAR startup scan evaluated a transport-sized universe
    and rejected all of it for INSUFFICIENT_POST_RANGE_BARS, while the
    manifest held 593 names. The universe was never the problem.
    """
    from scanners import runner

    code = _executable(REPO_ROOT / "scanners" / "runner.py")
    assert not hasattr(runner, "UNIVERSE_PREPARED")
    assert not hasattr(runner, "_startup_cap")
    assert "UNIVERSE_PREPARED" not in code
    assert "_startup_cap" not in code
    assert runner.UNIVERSE_MANIFEST == "manifest"

    wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text()
    assert 'UNIVERSE_ARG="manifest"' in wrapper
    assert '--universe "$UNIVERSE_ARG"' in wrapper
    assert 'UNIVERSE_ARG="prepared"' not in wrapper


def test_17b_the_scanner_path_never_reads_the_transport_ceiling():
    """The strategy universe must not be bounded by the websocket cap --
    not by importing it, and not by naming it."""
    for name in ("scanners/runner.py", "scanners/base/session_startup.py",
                 "scripts/strategy_startup_gate.py"):
        code = _executable(REPO_ROOT / name)
        assert "MAX_SUBSCRIPTIONS" not in code, name
        assert "kis_hdfscnt0" not in code, name


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


def test_24b_the_defer_happens_before_the_lock_is_taken():
    """ORDER IS THE WHOLE SAFETY PROPERTY.

    A process waiting for the evaluable moment must not be holding the
    scanner lock: every other session's scan in that window would become a
    SCANNER_LOCK_SKIPPED for no reason. Asserted on the wrapper's
    ORDERING, because "we intended not to hold it" is not a property.
    """
    wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text()
    assert "strategy_startup_gate" in wrapper
    assert wrapper.index("strategy_startup_gate") < wrapper.index("flock -n"), (
        "the gate -- and therefore the wait -- must complete before flock")
    # And the gate refuses to scan on its own: it prints a verdict.
    assert 'if [ "${GATE:-}" != "PROCEED" ]' in wrapper
    gate = _executable(REPO_ROOT / "scripts" / "strategy_startup_gate.py")
    assert "flock" not in gate
    for forbidden in ("run_scanners", "runner.run", "publish"):
        assert forbidden not in gate, forbidden


# -- 25-26. the deferral is reported, and so is the completion ------------

def test_25_the_deferral_and_every_abort_are_named():
    gate = (REPO_ROOT / "scripts" / "strategy_startup_gate.py").read_text()
    for event in ("STRATEGY_STARTUP_DEFERRED", "STRATEGY_STARTUP_RESUMED",
                  "STRATEGY_STARTUP_DEFER_ABORTED", "STRATEGY_STARTUP_NO_DEFER"):
        assert event in gate, event
    for field in ("defer_seconds", "orb_minutes", "min_post_range_bars",
                  "bar_interval_minutes", "start_lag_ms"):
        assert field in gate, field
    for reason in ("SESSION_CHANGED", "TRADING_DAY_CHANGED", "RELEASE_CHANGED",
                   "SESSION_CLOSED"):
        assert reason in gate, reason


def test_26_completion_is_reported_even_with_no_candidates():
    source = (REPO_ROOT / "scanners" / "runner.py").read_text()
    assert "STRATEGY_STARTUP_SCAN_START" in source
    assert "STRATEGY_STARTUP_SCAN_COMPLETE" in source
    body = source[source.index("def _log_fast_scan_complete"):]
    body = body[:body.index("\ndef ")]
    assert "passed" in body and "evaluated" in body
    # the universe size is the claim most worth being able to disprove
    assert "report.universe_size" in body
    assert "strategy_start_lag_ms" in body
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


def test_29_fast_start_does_not_reach_the_trading_modules():
    """Asserted structurally, not by diffing against a fixed baseline.

    This compared the tree against a pinned SHA and required that no
    trading module had changed since. That is a receding baseline: it
    accumulates every later commit, so the first legitimate change to
    `execution/` or `reconciliation/` -- made by a DIFFERENT P0, for
    reasons of its own -- fails a test about FAST-START and tells the
    reader something untrue. The same defect has now bitten this
    repository three times.

    What FAST-START actually claims is that its own modules observe and
    prepare, and never reach the code that trades. That is a property of
    these files and holds no matter what else the repository does.
    """
    import ast

    FAST_START_FILES = [
        REPO_ROOT / "scanners" / "base" / "session_startup.py",
        REPO_ROOT / "scripts" / "prepare_session_startup.py",
        REPO_ROOT / "scripts" / "strategy_startup_gate.py",
    ]
    forbidden = ("execution", "brokers", "reconciliation", "s1_live",
                 "kis_live_trading", "kis_position_manager")
    for path in FAST_START_FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert name.split(".")[0] not in forbidden, (
                    f"{path.name} imports {name!r}")


# -- 30-34. one generation, two views -------------------------------------

@pytest.mark.parametrize("session", SESSIONS)
def test_30_the_scanner_view_is_uncapped_and_carries_no_symbols(tmp_path, session):
    """`cap` is present and NULL on purpose.

    An absent key reads as "nobody considered it". A null one records that
    the strategy universe is deliberately not bounded by the transport
    ceiling -- which is the entire decision this artifact version exists
    to write down.
    """
    art = _artifact(str(tmp_path), session,
                    scanner_view=ss.build_scanner_view(session))
    view = art["scanner_view"]
    assert "cap" in view, "an absent cap cannot be told from an unconsidered one"
    assert view["cap"] is None
    assert view["source"] == ss.SCANNER_VIEW_SOURCE_MANIFEST
    # no symbol list anywhere in the scanner view: a second list would be a
    # second universe, bounded by whoever wrote it
    for key, value in view.items():
        assert not isinstance(value, (list, tuple)), key


@pytest.mark.parametrize("session", SESSIONS)
def test_31_both_views_come_from_one_generation(tmp_path, session):
    art = _artifact(str(tmp_path), session,
                    scanner_view=ss.build_scanner_view(session))
    loaded, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                                   target_session=session, cap=CAP, now=NOW)
    assert status == ss.STATUS_VALID
    # the collector view and the scanner view are halves of ONE artifact:
    # same trading day, same target session, same generation stamp
    assert ss.collector_symbols(loaded) == art["symbols"]
    assert ss.scanner_view_of(loaded)["source"] == ss.SCANNER_VIEW_SOURCE_MANIFEST
    assert loaded["trading_day"] == DAY
    assert loaded["target_session"] == session
    assert loaded["generated_at"] == art["generated_at"]


def test_32_a_v1_artifact_is_a_collector_view_and_nothing_more(tmp_path):
    """Fail-safe degradation, and the direction of it matters.

    A v1 artifact predates the split, so it says nothing about the
    scanner. It must remain a usable COLLECTOR view -- refusing it would
    leave a session with no stream -- and must NOT become a scanner
    universe again, because that is the coupling being removed.
    """
    art = _artifact(str(tmp_path), "REGULAR")
    path = Path(ss.artifact_path(str(tmp_path), trading_day=DAY,
                                 target_session="REGULAR"))
    raw = json.loads(path.read_text())
    raw["version"] = 1
    raw.pop("scanner_view", None)
    path.write_text(json.dumps(raw))

    loaded, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                                   target_session="REGULAR", cap=CAP, now=NOW)
    assert status == ss.STATUS_VALID, "a v1 artifact is still a valid stream list"
    assert ss.collector_symbols(loaded) == art["symbols"]
    assert ss.scanner_view_of(loaded) is None, (
        "a v1 artifact must not be readable as a scanner universe")


def test_33_an_unknown_version_is_still_refused(tmp_path):
    art = _artifact(str(tmp_path), "REGULAR")
    path = Path(ss.artifact_path(str(tmp_path), trading_day=DAY,
                                 target_session="REGULAR"))
    raw = json.loads(path.read_text())
    raw["version"] = max(ss.SUPPORTED_ARTIFACT_VERSIONS) + 1
    path.write_text(json.dumps(raw))
    _, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                              target_session="REGULAR", cap=CAP, now=NOW)
    assert status == ss.STATUS_VERSION
    assert art["version"] == ss.ARTIFACT_VERSION


def test_34_the_collector_view_is_still_bounded_by_the_transport_cap(tmp_path):
    """The split loosens the SCANNER. It must not loosen the collector:
    one appkey streams a fixed number of symbols and that is measured, not
    negotiable."""
    from market_data import kis_hdfscnt0 as wire

    too_many = [f"SYM{i}" for i in range(wire.MAX_SUBSCRIPTIONS + 1)]
    _artifact(str(tmp_path), "REGULAR", symbols=too_many)
    _, status = ss.load_valid(str(tmp_path), trading_day=DAY,
                              target_session="REGULAR",
                              cap=wire.MAX_SUBSCRIPTIONS, now=NOW)
    assert status == ss.STATUS_TOO_MANY


# -- 35-38. the evaluable moment is derived, never written ----------------

@pytest.mark.parametrize("session", SESSIONS)
def test_35_earliest_evaluable_is_derived_for_every_live_session(session):
    """From the ORB scanner's own configuration, including its
    per-session window override -- not from a constant here."""
    orb, post, interval = ss.strategy_inputs(session)
    assert orb > 0 and post > 0 and interval > 0

    start = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
    earliest = ss.earliest_evaluable_at(
        start, orb_minutes=orb, min_post_range_bars=post,
        bar_interval_minutes=interval)
    assert earliest == start + timedelta(minutes=orb + post * interval)
    assert earliest > start, "a session is never evaluable at its own open"


def test_35b_no_evaluable_moment_is_hard_coded():
    """The derivation must read the config. A literal offset here would
    survive a config edit and quietly describe a window nobody set."""
    for name in ("scanners/base/session_startup.py",
                 "scripts/strategy_startup_gate.py"):
        code = _executable(REPO_ROOT / name)
        for literal in ("value=8", "value=13", "value=930"):
            assert literal not in code, f"{name}: {literal}"
    source = (REPO_ROOT / "scanners" / "base" / "session_startup.py").read_text()
    assert "min_post_range_bars" in source and "orb_minutes" in source


def test_36_a_pre_evaluable_invocation_defers(monkeypatch):
    """T+2 against a T+8 evaluable moment waits the difference."""
    start = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
    invoked = start + timedelta(minutes=2)
    monkeypatch.setattr(ss, "session_started_at", lambda *a, **k: start)
    monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: "REGULAR")

    timing = ss.strategy_timing(invoked, session="REGULAR")
    orb, post, interval = ss.strategy_inputs("REGULAR")
    expected = (orb + post * interval) - 2
    assert timing["earliest_evaluable_at"] == start + timedelta(
        minutes=orb + post * interval)
    assert ss.defer_seconds(timing) == pytest.approx(expected * 60.0)


def test_37_an_evaluable_invocation_does_not_defer(monkeypatch):
    start = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(ss, "session_started_at", lambda *a, **k: start)
    monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: "REGULAR")
    timing = ss.strategy_timing(start + timedelta(minutes=24), session="REGULAR")
    assert ss.defer_seconds(timing) == 0.0


def test_38_an_underivable_moment_never_defers(monkeypatch):
    """An unanswerable clock must not become a skipped scan.

    Every failure to derive the moment resolves to "scan now", which is
    what this wrapper did before the deferral existed. Waiting on a moment
    nobody can compute would be strictly worse than scanning early.
    """
    monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: "REGULAR")
    monkeypatch.setattr(ss, "session_started_at", lambda *a, **k: None)
    timing = ss.strategy_timing(NOW, session="REGULAR")
    assert timing["earliest_evaluable_at"] is None
    assert timing["unavailable_reason"] == "SESSION_START_NOT_LOCATABLE"
    assert ss.defer_seconds(timing) == 0.0

    monkeypatch.setattr(scan_session, "session_at",
                        lambda *a, **k: scan_session.CLOSED)
    closed = ss.strategy_timing(NOW, session=None)
    assert closed["unavailable_reason"] == "SESSION_CLOSED"
    assert ss.defer_seconds(closed) == 0.0


def test_39_the_wait_is_bounded_by_one_scanner_cadence(monkeypatch):
    """A wait longer than the cron cadence is not taken at all: the next
    tick is already closer to the answer than this process can get by
    holding itself open across it."""
    start = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(ss, "session_started_at", lambda *a, **k: start)
    monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: "REGULAR")
    timing = ss.strategy_timing(start, session="REGULAR")
    assert ss.defer_seconds(timing, max_defer_seconds=60) == 0.0
    assert ss.defer_seconds(timing, max_defer_seconds=ss.MAX_DEFER_SECONDS) > 0


# -- 40-42. nothing learned before the wait is trusted after it -----------

def _deferring_gate(monkeypatch, *, session_answers, release_answers=None):
    """The gate, forced into a real deferral, with the sleep removed.

    The session is claimed to have opened NOW, so the evaluable moment is
    a few minutes out and `defer_seconds` is positive. `session_at` then
    answers from a list: the first call is the pre-wait session, later
    calls are what the clock says after waking.
    """
    import scripts.strategy_startup_gate as gate

    monkeypatch.setattr(ss, "session_started_at",
                        lambda *a, **k: datetime.now(timezone.utc))

    asked = {"n": 0}

    def answer(*_a, **_k):
        index = min(asked["n"], len(session_answers) - 1)
        asked["n"] += 1
        return session_answers[index]

    monkeypatch.setattr(scan_session, "session_at", answer)

    releases = list(release_answers or [("root", "sha")])
    handed = {"n": 0}

    def release():
        index = min(handed["n"], len(releases) - 1)
        handed["n"] += 1
        return releases[index]

    monkeypatch.setattr(gate, "_release_identity", release)

    slept = []
    monkeypatch.setattr(gate.time, "sleep", slept.append)
    return gate, slept, asked


def test_40_the_gate_revalidates_the_session_after_waking(monkeypatch, capsys):
    """A session can end during the wait. Scanning after that would
    publish candidates describing a session that is already over."""
    gate, slept, asked = _deferring_gate(
        monkeypatch, session_answers=["REGULAR", "AFTER_HOURS"])

    code = gate.run(["--session", "REGULAR"])
    out = capsys.readouterr()

    assert code == 0
    assert slept and slept[0] > 0, "this test is only meaningful if it deferred"
    assert asked["n"] >= 2, "the session must be asked again AFTER the wait"
    assert out.out.strip() == "ABORT"
    assert "STRATEGY_STARTUP_DEFER_ABORTED" in out.err
    assert "SESSION_CHANGED" in out.err
    assert "STRATEGY_STARTUP_RESUMED" not in out.err


def test_40b_an_unchanged_session_resumes_and_proceeds(monkeypatch, capsys):
    """The other half of the same claim: revalidation must not be a
    blanket refusal to scan after waiting."""
    gate, slept, _ = _deferring_gate(monkeypatch, session_answers=["REGULAR"])

    code = gate.run(["--session", "REGULAR"])
    out = capsys.readouterr()

    assert code == 0
    assert slept and slept[0] > 0
    assert out.out.strip() == "PROCEED"
    assert "STRATEGY_STARTUP_DEFERRED" in out.err
    assert "STRATEGY_STARTUP_RESUMED" in out.err
    assert "start_lag_ms=" in out.err


def test_41_the_gate_aborts_when_the_release_moves_under_it(monkeypatch, capsys):
    """A deploy switches the release pointers atomically. A process that
    resolved one release before the wait and scanned after it would run
    half of one release and half of another."""
    gate, slept, _ = _deferring_gate(
        monkeypatch, session_answers=["REGULAR"],
        release_answers=[("root", "sha-old"), ("root", "sha-new")])

    code = gate.run(["--session", "REGULAR"])
    out = capsys.readouterr()

    assert code == 0
    assert slept and slept[0] > 0
    assert out.out.strip() == "ABORT"
    assert "RELEASE_CHANGED" in out.err


def test_41b_the_wait_happens_before_any_verdict_is_printed(monkeypatch, capsys):
    """The wrapper takes the lock only after this process exits, so the
    sleep must be inside the gate's own lifetime -- not deferred to the
    caller in any form."""
    gate, slept, _ = _deferring_gate(monkeypatch, session_answers=["REGULAR"])
    gate.run(["--session", "REGULAR"])
    out = capsys.readouterr()
    assert slept, "the gate itself must do the waiting"
    assert out.err.index("STRATEGY_STARTUP_DEFERRED") < \
        out.err.index("STRATEGY_STARTUP_RESUMED")


def test_42_the_gate_prints_exactly_one_verdict(monkeypatch, capsys):
    """The wrapper branches on this single word. Anything else on stdout
    would make it read as a refusal."""
    import scripts.strategy_startup_gate as gate

    monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: "REGULAR")
    monkeypatch.setattr(
        ss, "session_started_at",
        lambda *a, **k: datetime.now(timezone.utc) - timedelta(hours=2))
    code = gate.run(["--session", "REGULAR", "--no-wait"])
    out = capsys.readouterr()
    assert code == 0
    assert out.out.strip() in {"PROCEED", "ABORT"}
    assert len(out.out.strip().splitlines()) == 1, (
        "telemetry goes to stderr; stdout carries the verdict alone")


# -- 43-46. the invariants this change must not touch ---------------------

def test_43_the_shared_lock_and_its_skip_are_unchanged():
    """An overlapping invocation is still a plain flock skip. No new
    overlap mechanism: the first scan running past the next tick is
    NORMAL now, because it starts later and reads 593 symbols."""
    wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text()
    assert wrapper.count("s6_scan.lock") >= 1
    assert "flock -n -E 99" in wrapper
    assert "SCANNER_LOCK_SKIPPED" in wrapper
    assert "SCANNER_LOCK_ACQUIRED" in wrapper


def test_44_the_logical_and_physical_watch_limits_are_untouched():
    from market_data import kis_hdfscnt0 as wire
    from s6_live import active_watch

    assert wire.MAX_SUBSCRIPTIONS == 41
    assert active_watch.MAX_LOGICAL_WATCH_SYMBOLS == 120
    assert active_watch.MAX_LOGICAL_WATCH_SYMBOLS > wire.MAX_SUBSCRIPTIONS, (
        "the logical watch is deliberately larger than what can be streamed")


def test_45_the_legacy_rotation_modules_still_have_no_production_caller():
    """discovery_tiers is dead code and slot_rotation is observation-only.
    Neither may be wired by this change -- they encode the very coupling
    being removed, a world where only a streamed symbol can be READY."""
    import subprocess

    for module in ("discovery_tiers", "slot_rotation"):
        found = subprocess.run(
            ["grep", "-rln", module, "--include=*.py", "--include=*.sh",
             "scanners", "scripts", "deploy", "s6_live", "execution"],
            cwd=str(REPO_ROOT), capture_output=True, text=True).stdout.split()
        callers = [f for f in found if not f.endswith(f"{module}.py")]
        assert not callers, f"{module} acquired a caller: {callers}"


def test_46_the_transport_only_membership_question_is_unchanged():
    """A REST-backed candidate may still become READY. The physical cap
    bounds the stream, not eligibility."""
    from s6_live import active_watch

    assert hasattr(active_watch, "is_transport_only")
    code = _executable(REPO_ROOT / "scanners" / "base" / "session_startup.py")
    for forbidden in ("is_transport_only", "record_provisional_pass",
                      "READY_TO_BUY", "CLAIM"):
        assert forbidden not in code, forbidden
