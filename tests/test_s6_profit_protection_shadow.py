"""A profit-protection exit measured beside the live one, never in force.

The measurement that produced these two candidates, on 40 canonical ORB5
positions closed 2026-09-08..09-17:

    median peak capture                 0.179
    positive peak -> realised loss      12 of 40
    median hold past the peak           40 minutes

and, replayed causally (the gate read from the RUNNING peak at each tick,
never the final one):

    twelve faster RANGE/VWAP/EMA variants   median delta ~ 0, p2l WORSE
    same weakness test gated on peak>=+1%   median +0.704 vs realised +0.023

So what is under test is not a new indicator. It is the gate.

What these tests hold onto
-------------------------
  * the rule is one function, so the recorder and the tests cannot drift
  * an absent EMA9 or an unknown slope is NOT a trigger -- the point is
    what the rule would have done on evidence it actually had
  * the shadow cannot change, delay or block a real exit
  * the shadow keeps its state OUT of the position row: `peak_volume_
    expansion` already shows what a research quantity costs inside the
    record reconciliation compares against the broker
"""
import ast
import json
from pathlib import Path

import pytest

from s6_live import profit_protection_shadow as pps

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-09-18"


def _env(tmp_path):
    return {"PROFIT_PROTECTION_SHADOW_DIR": str(tmp_path)}


def _executable(path):
    """The file's code with string literals blanked, so a guard cannot
    fail on a comment that cites the incident behind it."""
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.dump(tree)


# -- 1-6. the rule itself ------------------------------------------------

@pytest.mark.parametrize("peak,gate,expected", [
    (0.99, pps.GATE_1P0, False),   # just under the +1.0% gate
    (1.00, pps.GATE_1P0, True),    # exactly on it
    (1.49, pps.GATE_1P5, False),   # just under the +1.5% gate
    (1.50, pps.GATE_1P5, True),    # exactly on it
    (3.00, pps.GATE_1P5, True),
])
def test_1_the_peak_gate_is_inclusive_at_its_own_threshold(peak, gate, expected):
    """Weakness confirmed throughout; only the gate varies."""
    assert pps.triggered(peak_pnl_pct=peak, price=10.0, ema9=10.5,
                         ema9_slope=-0.01, gate=gate) is expected


def test_2_price_at_or_above_ema9_is_not_a_trigger():
    for price in (10.5, 10.6):
        assert pps.triggered(peak_pnl_pct=2.0, price=price, ema9=10.5,
                             ema9_slope=-0.01, gate=pps.GATE_1P0) is False


def test_3_a_rising_ema9_is_not_a_trigger():
    """The gate says a profit exists; the slope says the move is not over."""
    assert pps.triggered(peak_pnl_pct=2.0, price=10.0, ema9=10.5,
                         ema9_slope=+0.01, gate=pps.GATE_1P0) is False
    # flat counts as non-rising, by the model definition
    assert pps.triggered(peak_pnl_pct=2.0, price=10.0, ema9=10.5,
                         ema9_slope=0.0, gate=pps.GATE_1P0) is True


@pytest.mark.parametrize("missing", ["peak_pnl_pct", "price", "ema9", "ema9_slope"])
def test_4_absent_evidence_is_never_a_trigger(missing):
    kwargs = dict(peak_pnl_pct=2.0, price=10.0, ema9=10.5, ema9_slope=-0.01,
                  gate=pps.GATE_1P0)
    kwargs[missing] = None
    assert pps.triggered(**kwargs) is False


def test_5_the_first_tick_of_a_position_cannot_trigger(tmp_path):
    """There is no previous EMA9 yet, so the slope is unknown -- and an
    unknown slope is not a trigger."""
    rec = pps.observe(position_id="p1", symbol="AAA", session="REGULAR",
                      trading_day=DAY, now="2026-09-18T14:00:00+00:00",
                      entry_price=10.0, price=10.0, peak_price=10.5,
                      ema9=10.4, decision_action="HOLD", decision_reason=None,
                      env=_env(tmp_path))
    assert rec["running_peak_pnl_pct"] == pytest.approx(5.0)
    assert rec["previous_ema9"] is None
    assert rec["ema9_slope"] is None
    assert rec["model_1p0_triggered"] is False
    assert rec["model_1p5_triggered"] is False


def test_6_the_slope_comes_from_the_previous_tick_not_a_new_indicator(tmp_path):
    env = _env(tmp_path)
    common = dict(position_id="p1", symbol="AAA", session="REGULAR",
                  trading_day=DAY, entry_price=10.0, peak_price=10.5,
                  decision_action="HOLD", decision_reason=None, env=env)
    pps.observe(now="t1", price=10.45, ema9=10.40, **common)
    second = pps.observe(now="t2", price=10.30, ema9=10.38, **common)
    assert second["previous_ema9"] == pytest.approx(10.40)
    assert second["ema9_slope"] == pytest.approx(-0.02)
    assert second["model_1p0_triggered"] is True    # peak +5%, price<ema9, slope<0
    assert second["model_1p5_triggered"] is True


# -- 7. the first trigger is latched, and never relabelled ---------------

def test_7_the_first_trigger_is_latched_with_its_own_price(tmp_path):
    env = _env(tmp_path)
    common = dict(position_id="p1", symbol="AAA", session="REGULAR",
                  trading_day=DAY, entry_price=10.0, peak_price=10.5,
                  decision_action="HOLD", decision_reason=None, env=env)
    pps.observe(now="t1", price=10.45, ema9=10.40, **common)
    first = pps.observe(now="t2", price=10.30, ema9=10.38, **common)
    later = pps.observe(now="t3", price=10.20, ema9=10.36, **common)
    assert first["shadow_triggered_at"] == "t2"
    assert later["shadow_triggered_at"] == "t2", "a later tick must not relabel it"
    assert later["shadow_trigger_price"] == pytest.approx(10.30)
    assert later["shadow_trigger_pnl_pct"] == pytest.approx(3.0)
    # the missed-upside evidence accumulates after the trigger
    assert later["post_shadow_1p0_max_price"] == pytest.approx(10.30)
    up = pps.observe(now="t4", price=10.60, ema9=10.36, **common)
    assert up["post_shadow_1p0_max_price"] == pytest.approx(10.60)


def test_7b_the_two_models_latch_independently(tmp_path):
    """peak +1.2% clears the 1.0 gate and not the 1.5 one."""
    env = _env(tmp_path)
    common = dict(position_id="p2", symbol="BBB", session="REGULAR",
                  trading_day=DAY, entry_price=10.0, peak_price=10.12,
                  decision_action="HOLD", decision_reason=None, env=env)
    pps.observe(now="t1", price=10.10, ema9=10.09, **common)
    rec = pps.observe(now="t2", price=10.00, ema9=10.08, **common)
    assert rec["model_1p0_triggered"] is True
    assert rec["model_1p5_triggered"] is False
    assert rec["shadow_triggered_at"] == "t2"
    assert rec["shadow_1p5_triggered_at"] is None


# -- 8. the record carries what the analysis needs -----------------------

def test_8_every_required_field_is_written(tmp_path):
    env = _env(tmp_path)
    pps.observe(position_id="p1", symbol="AAA", session="REGULAR",
                trading_day=DAY, now="t1", entry_price=10.0, price=10.4,
                peak_price=10.5, ema9=10.3, decision_action="HOLD",
                decision_reason="S6_EXIT_DATA_UNAVAILABLE", env=env)
    path = pps.log_path(DAY, env=env)
    written = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(written) == 1
    row = written[0]
    for field in ("symbol", "position_id", "session", "timestamp",
                  "entry_price", "price", "running_peak_price",
                  "running_peak_pnl_pct", "ema9", "previous_ema9",
                  "ema9_slope", "model_1p0_triggered", "model_1p5_triggered",
                  "actual_exit_decision", "actual_exit_reason",
                  "shadow_triggered_at", "shadow_trigger_price",
                  "shadow_trigger_pnl_pct"):
        assert field in row, field
    assert row["actual_exit_decision"] == "HOLD"
    assert row["actual_exit_reason"] == "S6_EXIT_DATA_UNAVAILABLE"


def test_8b_no_root_means_no_write_and_no_raise(tmp_path):
    """The default-root defect that silenced shadow_signal_log for weeks
    is refused here: no root, no observation, and no exception."""
    assert pps.log_path(DAY, env={}) is None
    assert pps.state_path(env={}) is None
    rec = pps.observe(position_id="p1", symbol="AAA", session="REGULAR",
                      trading_day=DAY, now="t1", entry_price=10.0, price=10.4,
                      peak_price=10.5, ema9=10.3, decision_action="HOLD",
                      decision_reason=None, env={})
    assert rec is not None and rec["symbol"] == "AAA"


# -- 9-12. it cannot touch anything that trades --------------------------

def test_9_the_shadow_reaches_no_broker_order_or_position_store():
    code = _executable(REPO_ROOT / "s6_live" / "profit_protection_shadow.py")
    for forbidden in ("brokers", "execution", "position_store", "state_store",
                      "exit_intent", "submit_order", "submit_sell_order",
                      "reconciliation", "latch_pending_exit", "exit_submitted",
                      "kis_live_trading"):
        assert forbidden not in code, forbidden


def test_10_the_call_site_runs_after_the_decision_and_fails_open():
    source = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
    assert "profit_protection_shadow" in source
    # after the decision exists
    assert source.index("decision = exit_policy.decide(") < \
        source.index("profit_protection_shadow"), (
        "the shadow must observe a decision, never contribute to one")
    # and before the SELL branch is taken, inside a guard
    block = source[source.index("profit_protection_shadow") - 400:]
    block = block[:block.index("if not decision.sells:")]
    assert "try:" in block and "except Exception" in block, (
        "a research record must never be able to stop a position leaving")


def test_11_a_broken_shadow_does_not_change_or_block_the_real_exit(
        monkeypatch, tmp_path):
    """The strongest form of the claim: run the REAL exit path twice, once
    with the shadow working and once with it raising, and require the same
    decision, the same reason and the same position state both times."""
    import tempfile
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("PROFIT_PROTECTION_SHADOW_DIR", str(tmp_path))
    from state_store.db import open_db
    from s6_live import exit_runtime, position_store as ps

    T0 = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)

    class Features:
        def __init__(self, price, vwap=None, ema9=None, ema21=None):
            self.price, self.vwap = price, vwap
            self.ema9, self.ema21 = ema9, ema21
            self.volume_expansion = None

    class Broker:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def refuse(*a, **k):
                self.calls.append(name)
                raise AssertionError(f"the shadow path called the broker: {name}")
            return refuse

    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))

    def run_once(break_shadow):
        # A distinct symbol per run: one open position per symbol.
        symbol = "BROKEN" if break_shadow else "WORKING"
        with open_db() as conn:
            pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                       entry_session="REGULAR", range_high=99.5,
                                       range_low=99.0,
                                       entry_volume_expansion=2.0, now=T0)
            ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                              venue="NASD", now=T0)
            later = T0 + timedelta(minutes=10)
            ps.observe(conn, pid, price=102.0, now=later)
            if break_shadow:
                monkeypatch.setattr(pps, "observe",
                                    lambda **k: (_ for _ in ()).throw(
                                        RuntimeError("shadow is broken")))
            out = exit_runtime.evaluate_position(
                conn, broker_adapter=Broker(), position_id=pid,
                row=ps.load(conn, pid),
                features=Features(price=101.0, vwap=100.0, ema9=101.5,
                                  ema21=101.0),
                current_price=101.0, session="REGULAR", now=later,
                orders_allowed=True)
            row = ps.load(conn, pid)
            return (out.action, out.reason, row["status"],
                    row["exit_submitted"], row["pending_exit_reason"])


    working = run_once(False)
    broken = run_once(True)
    assert working == broken, (
        f"the shadow changed the real exit: {working} vs {broken}")
    # and the working run is a HOLD, so this test is about a live decision
    assert working[0] == exit_runtime.ACTION_HELD
    assert working[3] in (0, False)
    assert working[4] is None


def test_11b_the_shadow_wrote_its_observation_on_that_same_tick(monkeypatch, tmp_path):
    """The other half: it really did run, so test_11 is not passing because
    nothing happened."""
    import tempfile
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    monkeypatch.setenv("PROFIT_PROTECTION_SHADOW_DIR", str(tmp_path))
    from state_store.db import open_db
    from s6_live import exit_runtime, position_store as ps

    T0 = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)

    class Features:
        def __init__(self):
            self.price, self.vwap = 101.0, 100.0
            self.ema9, self.ema21 = 101.5, 101.0
            self.volume_expansion = None

    with open_db() as conn:
        pid = ps.record_submission(conn, symbol="ABC", variant="S6-R",
                                   entry_session="REGULAR", range_high=99.5,
                                   range_low=99.0, entry_volume_expansion=2.0,
                                   now=T0)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          venue="NASD", now=T0)
        later = T0 + timedelta(minutes=10)
        exit_runtime.evaluate_position(
            conn, broker_adapter=object(), position_id=pid,
            row=ps.load(conn, pid), features=Features(), current_price=101.0,
            session="REGULAR", now=later, orders_allowed=True)

    rows = list((tmp_path / pps.SUBDIR).glob("*.jsonl"))
    assert rows, "the shadow did not record anything"
    written = [json.loads(l) for l in rows[0].read_text().splitlines() if l.strip()]
    assert written and written[-1]["symbol"] == "ABC"
    assert written[-1]["actual_exit_decision"] in ("HOLD", "SELL")


def test_12_the_seven_rules_and_the_enforce_flag_are_untouched():
    from config import s6_exit_v0 as policy
    from s6_live import exit_policy

    assert policy.ENFORCE_PEAK_GIVEBACK_EXIT is False, (
        "this work observes a candidate; it does not promote rule 6")
    assert policy.PEAK_GIVEBACK_FRACTION == 0.5
    assert policy.PEAK_STALE_MINUTES == 30
    assert policy.VOLUME_DECAY_FRACTION == 0.5
    assert policy.EXIT_ON_RANGE_REENTRY is True
    assert policy.EXIT_ON_VWAP_FAILURE is True
    assert policy.EXIT_ON_EMA_STRUCTURE_FAILURE is True
    assert policy.SESSION_EXIT_LEAD_MINUTES == 15
    assert exit_policy.EXIT_REASONS == (
        exit_policy.REASON_EMERGENCY, exit_policy.REASON_HARD_RISK_CAP,
        exit_policy.REASON_RANGE_REENTRY, exit_policy.REASON_VWAP_FAILURE,
        exit_policy.REASON_EMA_STRUCTURE_FAILURE,
        exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS,
        exit_policy.REASON_SESSION_EXIT, exit_policy.REASON_NO_STRUCTURE)
    # the shadow names appear nowhere in the real decision path
    code = _executable(REPO_ROOT / "s6_live" / "exit_policy.py")
    for name in ("PROFIT_PROTECTION", "profit_protection", "ema9_slope"):
        assert name not in code, name


def test_13_the_shadow_state_is_not_on_the_position_row():
    """`peak_volume_expansion` is what a research quantity costs inside
    the record reconciliation compares against the broker."""
    store = (REPO_ROOT / "s6_live" / "position_store.py").read_text()
    for name in ("previous_ema9", "ema9_slope", "shadow_triggered_at",
                 "profit_protection"):
        assert name not in store, name
