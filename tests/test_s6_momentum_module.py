"""The momentum component: continuous across a boundary, gated only for entry.

The one rule that distinguishes the two callers is the point of the
module. A NEW entry waits five minutes into the session so a breakout is
judged against this session's own price discovery. An OPEN position is
not gated at all -- it is already exposed, and hiding its weakness for
the first five minutes of a new session would be a gap in monitoring,
not a safety margin.
"""

from datetime import datetime, timedelta, timezone

import pytest

from s6_live.momentum import (
    ENTRY_STABILIZATION_SECONDS, MomentumResult, PRECEDING_SESSION,
    evaluate_entry_momentum, evaluate_position_momentum,
)
from s6_live.momentum import indicators as ind
from s6_live.momentum import model, state

UTC = timezone.utc
T0 = datetime(2026, 9, 17, 14, 30, tzinfo=UTC)
SESSIONS = ("PREMARKET", "REGULAR", "AFTER_HOURS", "OVERNIGHT_DAYTIME")


class _Bar:
    def __init__(self, minute, close):
        self.minute, self.close = minute, close


def _bars(prices, start):
    return [_Bar(start + timedelta(minutes=i), p) for i, p in enumerate(prices)]


def _rising(n, base=100.0, step=0.6):
    return [base + step * i for i in range(n)]


def _context(*, current_prices, inherited_prices=None, session="REGULAR",
             start=T0):
    inherited = _bars(inherited_prices or [], start - timedelta(
        minutes=len(inherited_prices or [])))
    return state.BarContext(symbol="X", session=session,
                            current=_bars(current_prices, start),
                            inherited=inherited, session_started_at=start)


# -- 1, 4: inherited history removes the cold start -----------------------

class TestContinuity:

    def test_1_inherited_state_exists_at_the_boundary(self):
        ctx = _context(current_prices=_rising(3), inherited_prices=[100.0] * 40)
        assert ctx.inherited_context is True
        assert len(ctx.inherited) == 40 and len(ctx.current) == 3
        assert len(ctx.combined) == 43

    def test_4_indicators_are_available_from_inherited_history_alone(self):
        """Six new-session bars would never warm HMA20 (24) or the MACD
        (39) on their own."""
        ctx = _context(current_prices=_rising(6), inherited_prices=[100.0] * 40)
        result = evaluate_position_momentum("X", session="REGULAR",
                                            now=T0 + timedelta(minutes=6),
                                            context=ctx)
        assert result.hma_available is True
        assert result.macd_available is True
        assert result.inherited_bar_count == 40
        assert result.current_session_bar_count == 6

    def test_a_cold_session_without_inheritance_is_not_available(self):
        ctx = _context(current_prices=_rising(6), inherited_prices=[])
        result = evaluate_position_momentum("X", session="REGULAR",
                                            now=T0 + timedelta(minutes=6),
                                            context=ctx)
        assert result.available is False
        assert result.inherited_context is False
        assert result.reason == "INSUFFICIENT_HISTORY"

    def test_the_preceding_session_map_is_the_trading_day_order(self):
        assert PRECEDING_SESSION == {
            "PREMARKET": "OVERNIGHT_DAYTIME", "REGULAR": "PREMARKET",
            "AFTER_HOURS": "REGULAR", "OVERNIGHT_DAYTIME": "AFTER_HOURS"}
        assert set(PRECEDING_SESSION) == set(SESSIONS)


# -- 2, 3, 5: entry stabilization ----------------------------------------

class TestEntryStabilization:

    @pytest.mark.parametrize("minutes", [0, 1, 2, 3, 4])
    def test_2_before_five_minutes_entry_is_not_stabilized(self, minutes):
        ctx = _context(current_prices=_rising(max(minutes, 1)),
                       inherited_prices=[100.0] * 40)
        result = evaluate_entry_momentum("X", session="REGULAR",
                                         now=T0 + timedelta(minutes=minutes),
                                         context=ctx)
        assert result.entry_stabilized is False
        assert result.entry_eligible is False

    @pytest.mark.parametrize("minutes", [5, 6, 30])
    def test_3_from_five_minutes_entry_is_stabilized(self, minutes):
        ctx = _context(current_prices=_rising(minutes),
                       inherited_prices=[100.0] * 40)
        result = evaluate_entry_momentum("X", session="REGULAR",
                                         now=T0 + timedelta(minutes=minutes),
                                         context=ctx)
        assert result.entry_stabilized is True
        assert result.available is True
        assert result.entry_eligible is True

    def test_5_measured_but_unstabilized_entry_says_so_explicitly(self):
        """Never reported as 'measured and flat' -- that would be
        indistinguishable from a real absence of momentum."""
        ctx = _context(current_prices=_rising(2), inherited_prices=[100.0] * 40)
        result = evaluate_entry_momentum("X", session="REGULAR",
                                         now=T0 + timedelta(minutes=2),
                                         context=ctx)
        assert result.hma_available is True and result.macd_available is True
        assert result.available is False
        assert result.reason == model.NOT_STABILIZED
        assert result.positive is False and result.negative is False

    def test_the_window_is_five_minutes_of_elapsed_time(self):
        assert ENTRY_STABILIZATION_SECONDS == 300.0

    def test_6_stabilization_is_time_not_bar_count(self):
        """A sparse DAYTIME session with ONE printed bar is still
        stabilized once five minutes have elapsed -- §6."""
        ctx = _context(current_prices=[101.0], inherited_prices=[100.0] * 40,
                       session="OVERNIGHT_DAYTIME")
        result = evaluate_entry_momentum("X", session="OVERNIGHT_DAYTIME",
                                         now=T0 + timedelta(minutes=7),
                                         context=ctx)
        assert result.current_session_bar_count == 1
        assert result.entry_stabilized is True

    def test_6b_no_synthetic_bars_are_created(self):
        """Seven minutes elapsed, two prints: two bars, never seven."""
        sparse = [_Bar(T0, 100.0), _Bar(T0 + timedelta(minutes=6), 101.0)]
        ctx = state.BarContext(symbol="X", session="OVERNIGHT_DAYTIME",
                               current=sparse, inherited=[],
                               session_started_at=T0)
        result = evaluate_position_momentum("X", session="OVERNIGHT_DAYTIME",
                                            now=T0 + timedelta(minutes=7),
                                            context=ctx)
        assert result.current_session_bar_count == 2


# -- 8, 9, 10: positions are never gated ---------------------------------

class TestPositionContinuity:

    @pytest.mark.parametrize("minutes", [0, 1, 4])
    def test_8_an_open_position_gets_no_stabilization_hold(self, minutes):
        ctx = _context(current_prices=_rising(max(minutes, 1)),
                       inherited_prices=[100.0] * 40)
        result = evaluate_position_momentum("X", session="REGULAR",
                                            now=T0 + timedelta(minutes=minutes),
                                            context=ctx)
        assert result.available is True, "a held position is observed at once"
        assert result.combined_direction in (model.UP, model.DOWN, model.FLAT)

    def test_8b_entry_and_position_differ_only_in_that_rule(self):
        ctx = _context(current_prices=_rising(2), inherited_prices=[100.0] * 40)
        now = T0 + timedelta(minutes=2)
        entry = evaluate_entry_momentum("X", session="REGULAR", now=now,
                                        context=ctx)
        position = evaluate_position_momentum("X", session="REGULAR", now=now,
                                              context=ctx)
        assert entry.available is False and position.available is True
        for field in ("hma_value", "histogram", "hma_direction",
                      "inherited_bar_count"):
            assert getattr(entry, field) == getattr(position, field)

    def test_9_the_first_new_session_bars_move_the_measurement(self):
        flat = [100.0] * 40
        early = evaluate_position_momentum(
            "X", session="REGULAR", now=T0 + timedelta(minutes=1),
            context=_context(current_prices=[100.0], inherited_prices=flat))
        later = evaluate_position_momentum(
            "X", session="REGULAR", now=T0 + timedelta(minutes=8),
            context=_context(current_prices=_rising(8), inherited_prices=flat))
        assert later.hma_value != early.hma_value

    def test_10_weakness_is_recorded_when_both_sides_agree(self):
        falling = [160.0] * 40
        ctx = _context(current_prices=[160 - 0.6 * i for i in range(20)],
                       inherited_prices=falling)
        result = evaluate_position_momentum("X", session="REGULAR",
                                            now=T0 + timedelta(minutes=20),
                                            context=ctx)
        assert result.hma_direction == model.DOWN
        assert result.macd_direction == model.DOWN
        assert result.combined_direction == model.DOWN
        assert result.negative is True

    def test_one_side_alone_is_never_a_direction(self):
        assert ind._combined(model.UP, model.DOWN, True, True) == model.FLAT
        assert ind._combined(model.UP, model.UP, True, False) == model.UNKNOWN
        assert ind._combined(model.DOWN, model.DOWN, True, True) == model.DOWN


# -- 9: boundary gap ------------------------------------------------------

class TestBoundaryGap:

    def test_the_gap_is_recorded_when_both_sides_exist(self):
        ctx = _context(current_prices=[110.0] * 6, inherited_prices=[100.0] * 40)
        result = evaluate_position_momentum("X", session="REGULAR",
                                            now=T0 + timedelta(minutes=6),
                                            context=ctx)
        assert result.previous_session_last_price == 100.0
        assert result.new_session_first_price == 110.0
        assert result.boundary_gap_pct == pytest.approx(10.0)

    def test_no_inherited_history_means_no_gap_rather_than_zero(self):
        ctx = _context(current_prices=_rising(6), inherited_prices=[])
        result = evaluate_position_momentum("X", session="REGULAR",
                                            now=T0 + timedelta(minutes=6),
                                            context=ctx)
        assert result.boundary_gap_pct is None

    def test_no_threshold_is_applied_to_the_gap(self):
        """Diagnostic only -- §9 forbids inventing a gate from it."""
        import inspect

        for module in (state, ind):
            source = inspect.getsource(module)
            assert "gap_threshold" not in source
            assert "MAX_GAP" not in source


# -- 13-16: identical across sessions ------------------------------------

class TestAllSessions:

    @pytest.mark.parametrize("session", SESSIONS)
    def test_same_bars_same_answer_in_every_session(self, session):
        ctx = _context(current_prices=_rising(10),
                       inherited_prices=[100.0] * 40, session=session)
        now = T0 + timedelta(minutes=10)
        entry = evaluate_entry_momentum("X", session=session, now=now,
                                        context=ctx)
        position = evaluate_position_momentum("X", session=session, now=now,
                                              context=ctx)
        assert entry.session == session
        assert entry.available is True and position.available is True
        assert entry.combined_direction == position.combined_direction

    @pytest.mark.parametrize("session", SESSIONS)
    def test_the_stabilization_rule_is_the_same_everywhere(self, session):
        ctx = _context(current_prices=_rising(3),
                       inherited_prices=[100.0] * 40, session=session)
        result = evaluate_entry_momentum("X", session=session,
                                         now=T0 + timedelta(minutes=3),
                                         context=ctx)
        assert result.entry_stabilized is False

    def test_no_session_is_branched_on(self):
        """No DAYTIME-only strategy anywhere in the component."""
        import ast
        import inspect

        from s6_live.momentum import evaluator

        for module in (ind, evaluator, model):
            tree = ast.parse(inspect.getsource(module))
            literals = {n.value for n in ast.walk(tree)
                        if isinstance(n, ast.Constant)
                        and isinstance(n.value, str)}
            for name in SESSIONS:
                assert name not in literals, f"{name} in {module.__name__}"


# -- 17-20: modularity and isolation -------------------------------------

class TestModularity:

    def test_17_the_indicator_maths_lives_in_exactly_one_file(self):
        """Replacing the pair should touch `indicators.py` and nothing
        else -- so nobody else may name HMA or MACD internals."""
        import ast
        import inspect

        from s6_live.momentum import evaluator

        for module in (evaluator, model, state):
            source = inspect.getsource(module)
            for token in ("hma_series", "ewm(", "calculate_hma_macd",
                          "MACD_FAST", "HMA_LENGTH"):
                assert token not in source, f"{token} in {module.__name__}"

    def test_the_public_surface_is_generic(self):
        from s6_live import momentum

        assert set(momentum.__all__) >= {
            "evaluate_entry_momentum", "evaluate_position_momentum",
            "MomentumResult"}

    def test_20_the_result_carries_a_generic_direction(self):
        """A caller can read `combined_direction` without knowing the
        pair; the indicator-specific fields are prefixed and optional."""
        result = MomentumResult(symbol="X")
        assert result.combined_direction == model.UNKNOWN
        assert result.positive is False and result.negative is False
        record = result.as_record()
        for generic in ("available", "combined_direction", "entry_stabilized",
                        "inherited_context", "positive", "negative"):
            assert generic in record

    def test_18_19_no_broker_provider_or_limiter_import(self):
        import ast
        import inspect

        from s6_live.momentum import evaluator

        for module in (ind, model, state, evaluator):
            tree = ast.parse(inspect.getsource(module))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
            for name in imported:
                assert not name.startswith((
                    "brokers", "broker", "execution", "requests", "urllib",
                    "http", "kis_live_trading")), f"{name} in {module.__name__}"
            source = inspect.getsource(module)
            for banned in ("KISBroker", "rate_limiter", "submit_order",
                           "get_account", "provider_for_session"):
                assert banned not in source, f"{banned} in {module.__name__}"


# -- 7, 11, 12: the live paths are untouched -----------------------------

def _runtime_fn(name):
    from scripts import run_s6_runtime

    return getattr(run_s6_runtime, name)


def _executable(fn) -> str:
    """`fn`'s source with docstrings stripped, lower-cased.

    A check that reads prose fails on a docstring explaining that the
    thing does not happen -- the opposite of useful.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(ast.fix_missing_locations(tree)).lower()


class TestLivePathsUnchanged:

    def test_7_the_entry_gate_never_consults_the_momentum_module(self):
        """`precision_watch` already uses the WORD momentum in comments
        about entry-quality freshness, which predates this work. What
        must be absent is any use of this component."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "s6_live" / "precision_watch.py").read_text()
        assert "s6_live.momentum" not in source
        assert "momentum_shadow" not in source
        assert "evaluate_entry_momentum" not in source

    def test_12_the_exit_runtime_never_consults_momentum(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "s6_live" / "exit_runtime.py").read_text()
        assert "momentum" not in source.lower()
        assert "MOMENTUM_STRUCTURE_FAILURE" not in source

    def test_11_the_observation_runs_after_every_exit_stage(self):
        """And inside the loop that already isolates each stage, which is
        what makes an observation failure unable to stop exit
        monitoring."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "run_s6_runtime.py").read_text()
        assert '("momentum_observation"' in source
        assert source.index('("exits"') < source.index('("momentum_observation"')
        assert source.index('("momentum_observation"') < source.index(
            'except Exception as exc:  # noqa: BLE001 - one stage failing')

    def test_11b_a_failing_observation_returns_rather_than_raises(self):
        """Per symbol, so one unreadable position cannot end the sweep."""
        import inspect

        from scripts import run_s6_runtime

        body = inspect.getsource(run_s6_runtime._observe_momentum)
        assert "except Exception" in body

    def test_the_observation_makes_no_broker_call(self):
        """Checked against EXECUTABLE code: the docstring legitimately
        says "Never sells" and "No broker call" while explaining that
        neither happens."""
        code = _executable(_runtime_fn("_observe_momentum"))
        for banned in ("broker", "get_open_orders", "get_positions",
                       "submit_order", "submit_sell", "_submit"):
            assert banned not in code, banned


# -- 13: performance ------------------------------------------------------

class TestCost:

    def test_timings_are_reported(self):
        ctx = _context(current_prices=_rising(10),
                       inherited_prices=[100.0] * 40)
        result = evaluate_position_momentum("X", session="REGULAR",
                                            now=T0 + timedelta(minutes=10),
                                            context=ctx)
        for key in ("HMA_COMPUTE_MS", "MACD_COMPUTE_MS",
                    "MOMENTUM_EVALUATION_MS"):
            assert key in result.timings_ms
        assert result.timings_ms["MOMENTUM_EVALUATION_MS"] < 60.0

    def test_the_macd_still_matches_the_reference(self):
        """The optimisation proven in 854d19c, re-pinned here."""
        from indicators import calculate_hma_macd

        frame = ind.frame_from(_bars(
            [100.0] * 40 + [100 + 0.6 * i for i in range(40)], T0))
        reference = calculate_hma_macd(frame, fast=ind.MACD_FAST,
                                       slow=ind.MACD_SLOW,
                                       signal=ind.MACD_SIGNAL)
        mine = ind.hma_macd(frame)
        for column in ("hma_macd_line", "hma_macd_histogram"):
            a = reference[column].dropna().tolist()
            b = mine[column].dropna().tolist()
            assert a and len(a) == len(b)
            for x, y in zip(a, b):
                assert x == pytest.approx(y, abs=1e-9)
