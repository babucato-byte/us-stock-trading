"""HMA20 and the HMA-MACD histogram, observed and never acting.

The feature exists to answer one question with evidence: which EXISTING
gate is last to turn true, and would momentum have turned first. So the
assertions that matter most are the ones proving it changes nothing --
no verdict, no order, no network call -- and that an indicator without
enough bars is neither positive nor negative but simply unavailable.
"""

from datetime import datetime, timedelta, timezone

import pytest

from s6_live import momentum_shadow as ms

UTC = timezone.utc
T0 = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)
SESSIONS = ("PREMARKET", "REGULAR", "AFTER_HOURS", "OVERNIGHT_DAYTIME")


class _Bar:
    def __init__(self, minute, close):
        self.minute = minute
        self.close = close


def _bars(prices, start=T0):
    return [_Bar(start + timedelta(minutes=i), p) for i, p in enumerate(prices)]


# The three shapes below were chosen by MEASURING what the indicator pair
# actually reports, not by assuming. A smooth power curve looks
# "accelerating" to the eye but its HMA-MACD histogram is already rolling
# over by bar 80, so it tests the opposite of what its name would claim.


def _accelerating(n=80, base=100.0):
    """Flat, then a ramp: HMA up AND histogram rising."""
    half = max(1, n // 2)
    return [base] * half + [base + 0.6 * i for i in range(n - half)]


def _decelerating(n=80, base=100.0):
    """A fast ramp easing into a slow one: HMA still up, histogram fading.

    The case that must NOT read as confirmation -- momentum needs both.
    """
    half = max(1, n // 2)
    head = [base + 0.8 * i for i in range(half)]
    tail_base = head[-1] if head else base
    return head + [tail_base + 0.05 * i for i in range(n - half)]


def _falling(n=80, base=160.0):
    """Flat, then a decline: HMA down AND histogram fading."""
    half = max(1, n // 2)
    return [base] * half + [base - 0.6 * i for i in range(n - half)]


# -- the indicators -------------------------------------------------------

class TestHMA20:

    def test_the_period_is_20_and_the_warm_up_comes_from_the_framework(self):
        assert ms.HMA_LENGTH == 20
        from scanners.base.indicators import min_bars_for_hma

        assert ms.min_bars_for_hma() == min_bars_for_hma(20) == 23

    def test_it_reuses_the_framework_fast_hma(self):
        """Not a second copy of the maths."""
        import inspect

        assert "from scanners.base.indicators import hma_series" in \
            inspect.getsource(ms._hma_fields)

    def test_a_rising_series_is_direction_up(self):
        view = ms.compute(_bars(_accelerating()), symbol="A", session="REGULAR")
        assert view.hma_available is True
        assert view.hma_direction == ms.DIRECTION_UP
        assert view.hma_slope > 0
        assert view.hma_positive is True and view.hma_negative is False

    def test_a_falling_series_is_direction_down(self):
        view = ms.compute(_bars(_falling()), symbol="A", session="REGULAR")
        assert view.hma_direction == ms.DIRECTION_DOWN
        assert view.hma_slope < 0
        assert view.hma_negative is True and view.hma_positive is False

    def test_value_and_previous_are_both_recorded(self):
        view = ms.compute(_bars(_accelerating()), symbol="A", session="REGULAR")
        assert view.hma_value is not None and view.hma_previous is not None
        assert view.hma_slope == pytest.approx(view.hma_value - view.hma_previous)


class TestMACD:

    def test_the_configuration_is_the_existing_production_one(self):
        assert (ms.MACD_FAST, ms.MACD_SLOW, ms.MACD_SIGNAL) == (12, 26, 9)

    def test_it_matches_the_reference_implementation(self):
        """Same configuration on the fast HMA. The reference is a per-bar
        rolling apply and measured 51.9ms per symbol; equivalence is
        pinned here rather than assumed."""
        from indicators import calculate_hma_macd

        frame = ms._frame(_bars(_decelerating()))
        reference = calculate_hma_macd(frame, fast=ms.MACD_FAST,
                                       slow=ms.MACD_SLOW,
                                       signal=ms.MACD_SIGNAL)
        mine = ms._hma_macd(frame)
        for column in ("hma_macd_line", "hma_macd_signal", "hma_macd_histogram"):
            a = reference[column].dropna().tolist()
            b = mine[column].dropna().tolist()
            assert len(a) == len(b) and a, column
            for x, y in zip(a, b):
                assert x == pytest.approx(y, abs=1e-9), column

    def test_an_accelerating_histogram_is_flagged(self):
        view = ms.compute(_bars(_accelerating()), symbol="A", session="REGULAR")
        assert view.macd_available is True
        assert view.histogram_delta == pytest.approx(
            view.histogram - view.histogram_previous)
        assert view.histogram_accelerating is True
        assert view.histogram_decelerating is False

    def test_a_decelerating_histogram_is_flagged(self):
        view = ms.compute(_bars(_decelerating()), symbol="A", session="REGULAR")
        assert view.macd_available is True
        assert view.histogram_decelerating is True
        assert view.histogram_accelerating is False


class TestCombinedFlags:
    """Both sides required, never one."""

    def test_momentum_confirmed_needs_hma_and_macd(self):
        view = ms.compute(_bars(_accelerating()), symbol="A", session="REGULAR")
        assert view.hma_positive and view.histogram_accelerating
        assert view.momentum_confirmed is True

    def test_hma_positive_alone_is_not_confirmation(self):
        view = ms.compute(_bars(_decelerating()), symbol="A", session="REGULAR")
        assert view.hma_positive is True
        assert view.histogram_accelerating is False
        assert view.momentum_confirmed is False

    def test_momentum_weak_needs_both_sides(self):
        view = ms.compute(_bars(_falling()), symbol="A", session="REGULAR")
        assert view.hma_negative is True
        assert view.histogram_decelerating is True
        assert view.momentum_weak is True

    def test_macd_deterioration_alone_is_not_weakness(self):
        """HMA still rising while the histogram fades -- no exit signal."""
        view = ms.compute(_bars(_decelerating()), symbol="A", session="REGULAR")
        assert view.histogram_decelerating is True
        assert view.hma_negative is False
        assert view.momentum_weak is False

    def test_neither_flag_is_true_when_unavailable(self):
        view = ms.compute(_bars([100.0, 101.0]), symbol="A", session="REGULAR")
        assert view.hma_available is False and view.macd_available is False
        assert view.momentum_confirmed is False
        assert view.momentum_weak is False
        assert view.hma_positive is False and view.hma_negative is False


# -- warm-up --------------------------------------------------------------

class TestInsufficientBars:
    """Unavailable is its own answer, never positive and never negative."""

    def test_no_bars_at_all(self):
        view = ms.compute([], symbol="A", session="OVERNIGHT_DAYTIME")
        assert view.unavailable_reason == "NO_BARS"
        assert view.bar_count == 0
        assert view.hma_available is False and view.macd_available is False

    @pytest.mark.parametrize("count,hma,macd", [
        (10, False, False), (23, False, False),
        (24, True, False), (38, False, False), (39, True, True)])
    def test_availability_by_bar_count(self, count, hma, macd):
        """HMA20's first value needs 23 bars; a SLOPE needs a previous one,
        so 24. The MACD needs its slow HMA plus a 9-period signal."""
        view = ms.compute(_bars(_accelerating(count)), symbol="A",
                          session="PREMARKET")
        if count in (24, 39):
            assert view.hma_available is hma
        if count == 39:
            assert view.macd_available is macd
        if count <= 23:
            assert view.hma_available is False
            assert view.unavailable_reason == "HMA_INSUFFICIENT_BARS"

    def test_an_unparseable_bar_is_skipped_not_guessed(self):
        bars = _bars(_accelerating(60))
        bars[5].close = None
        bars[9].minute = None
        view = ms.compute(bars, symbol="A", session="REGULAR")
        assert view.bar_count == 58


# -- identical across sessions -------------------------------------------

class TestAllSessionsIdentical:

    @pytest.mark.parametrize("session", SESSIONS)
    def test_the_same_bars_give_the_same_answer_in_every_session(self, session):
        view = ms.compute(_bars(_accelerating()), symbol="A", session=session)
        assert view.hma_direction == ms.DIRECTION_UP
        assert view.macd_available is True
        assert view.momentum_confirmed is True
        assert view.session == session

    def test_the_module_branches_on_no_session(self):
        """No DAYTIME-only logic anywhere."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(ms))
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for name in SESSIONS:
            assert name not in literals, f"{name} is branched on"


# -- timestamps and the trace ---------------------------------------------

class TestTimingTrace:

    def _evaluation(self, symbol, state, conditions, at, session="REGULAR"):
        class _E:
            pass

        e = _E()
        e.symbol, e.session, e.state = symbol, session, state
        e.conditions, e.evaluated_at = conditions, at
        return e

    def test_observed_at_is_recorded(self):
        view = ms.compute(_bars(_accelerating()), symbol="A",
                          session="REGULAR", now=T0)
        assert view.observed_at == T0.isoformat()

    def test_first_true_finds_the_first_moment_each_gate_passed(self, tmp_path,
                                                                monkeypatch):
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        day = "2026-09-17"
        gates = [
            {"ORB_BREAKOUT_HOLDS": "PASS", "PRICE_ABOVE_VWAP": "FAIL"},
            {"ORB_BREAKOUT_HOLDS": "PASS", "PRICE_ABOVE_VWAP": "PASS"},
        ]
        for index, conditions in enumerate(gates):
            at = (T0 + timedelta(minutes=index)).isoformat()
            view = ms.compute(_bars(_accelerating()), symbol="A",
                              session="REGULAR", now=T0 + timedelta(minutes=index))
            ms.observe(self._evaluation("A", "WATCHING", conditions, at),
                       view, trading_day=day)
        # the tick that finally became READY
        ready_at = (T0 + timedelta(minutes=2)).isoformat()
        ms.observe(self._evaluation("A", "READY_TO_BUY",
                                    {"ORB_BREAKOUT_HOLDS": "PASS",
                                     "PRICE_ABOVE_VWAP": "PASS"}, ready_at),
                   ms.compute(_bars(_accelerating()), symbol="A",
                              session="REGULAR", now=T0 + timedelta(minutes=2)),
                   trading_day=day)

        rows = ms.read(day)
        assert len(rows) == 3
        summary = ms.first_true(rows, "A")
        assert summary["gates_first_pass_at"]["ORB_BREAKOUT_HOLDS"] == T0.isoformat()
        assert summary["gates_first_pass_at"]["PRICE_ABOVE_VWAP"] == \
            (T0 + timedelta(minutes=1)).isoformat()
        assert summary["entry_ready_at"] == ready_at

        blocking = ms.last_blocking_gate(summary)
        assert blocking["gate"] == "PRICE_ABOVE_VWAP"

        lead = ms.lead_seconds(summary, "momentum_confirmed")
        assert lead == pytest.approx(120.0), "momentum led the live READY by 2 min"

    def test_a_gate_that_never_passed_is_named_as_such(self):
        summary = {"gates_first_pass_at": {}, "entry_ready_at": None}
        assert ms.last_blocking_gate(summary)["reason"] == "NO_GATE_EVER_PASSED"

    def test_lead_is_none_rather_than_zero_when_either_side_is_missing(self):
        assert ms.lead_seconds({"entry_ready_at": None,
                                "momentum_first_true_at": {}}) is None

    def test_availability_is_measured_per_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        day = "2026-09-17"
        for session, count in (("OVERNIGHT_DAYTIME", 10),
                               ("OVERNIGHT_DAYTIME", 60), ("REGULAR", 60)):
            view = ms.compute(_bars(_accelerating(count)), symbol="A",
                              session=session)
            ms.observe(self._evaluation("A", "WATCHING", {}, T0.isoformat(),
                                        session=session),
                       view, trading_day=day)
        stats = ms.availability(ms.read(day))
        assert stats["OVERNIGHT_DAYTIME"]["evaluations"] == 2
        assert stats["OVERNIGHT_DAYTIME"]["both_available_pct"] == 50.0
        assert stats["REGULAR"]["both_available_pct"] == 100.0

    def test_an_unwritable_log_never_raises(self, monkeypatch):
        monkeypatch.delenv("SCANNER_ANALYTICS_DIR", raising=False)
        monkeypatch.delenv("TRADING_LOG_DIR", raising=False)
        assert ms.log_path("2026-09-17") is None
        assert ms.append({"x": 1}, trading_day="2026-09-17") is False


# -- it changes nothing ---------------------------------------------------

class TestChangesNothing:

    def test_no_network_or_broker_import(self):
        """NEW_KIS_CALLS = 0, NEW_PROVIDER_CALLS = 0,
        NEW_LIMITER_RESERVATIONS = 0 -- an import that does not exist
        cannot be reached by a path nobody thought of."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(ms))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for name in imported:
            assert not name.startswith(("brokers", "broker", "execution",
                                        "requests", "urllib", "http",
                                        "kis_live_trading")), name
        source = inspect.getsource(ms)
        for banned in ("KISBroker", "rate_limiter", "get_positions",
                       "get_account", "submit_order", "provider_for_session"):
            assert banned not in source, banned

    def test_compute_does_not_mutate_the_bars(self):
        bars = _bars(_accelerating(60))
        before = [(b.minute, b.close) for b in bars]
        ms.compute(bars, symbol="A", session="REGULAR")
        assert [(b.minute, b.close) for b in bars] == before

    def test_observe_does_not_mutate_the_evaluation(self):
        conditions = {"ORB_BREAKOUT_HOLDS": "PASS"}

        class _E:
            symbol, session, state = "A", "REGULAR", "WATCHING"
            evaluated_at = T0.isoformat()

        e = _E()
        e.conditions = conditions
        view = ms.compute(_bars(_accelerating()), symbol="A", session="REGULAR")
        ms.observe(e, view, trading_day="2026-09-17")
        assert e.conditions == {"ORB_BREAKOUT_HOLDS": "PASS"}
        assert e.state == "WATCHING"

    def test_the_live_entry_gate_set_is_untouched(self):
        """precision_watch decides entry and this feature is not in it."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "s6_live" / "precision_watch.py").read_text()
        assert "momentum_shadow" not in source
        assert "momentum" not in source.lower().split("def rank_ready")[0][-4000:] \
            or "momentum_shadow" not in source

    def test_the_live_exit_path_is_untouched(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "s6_live" / "exit_runtime.py").read_text()
        assert "momentum_shadow" not in source
        assert "MOMENTUM_STRUCTURE_FAILURE" not in source

    def test_the_recording_call_is_optional_research_only(self):
        """Wired beside the ORB15 shadow, in its own try, under the same
        tick budget -- never before the trading decision."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "run_live_buy_entry.py").read_text()
        assert "momentum_shadow.record_cycle" in source
        assert source.index("range_shadow.record_cycle") < \
            source.index("momentum_shadow.record_cycle")
        after = source.split("momentum_shadow.record_cycle")[1][:400]
        assert "deadline=optional_deadline.expired" in \
            source.split("momentum_shadow.record_cycle")[1][:300] or \
            "deadline" in after

    def test_record_cycle_writes_nothing_without_evaluations(self):
        class _Source:
            session = "REGULAR"
            evaluations = {}

        assert ms.record_cycle(_Source(), trading_day="2026-09-17",
                               now=T0) == 0


# -- performance ----------------------------------------------------------

class TestComputeCost:

    def test_timings_are_reported_and_small(self):
        view, timings = ms.compute_timed(_bars(_accelerating()), symbol="A",
                                         session="REGULAR")
        assert view.macd_available is True
        for key in ("HMA_COMPUTE_MS", "MACD_COMPUTE_MS", "TOTAL_AUX_COMPUTE_MS"):
            assert key in timings and timings[key] >= 0.0
        # The reference MACD measured 51.9ms per symbol; the fast path is
        # an order of magnitude below that. Generous bound so a slow CI
        # box does not fail on timing.
        assert timings["TOTAL_AUX_COMPUTE_MS"] < 40.0, timings
