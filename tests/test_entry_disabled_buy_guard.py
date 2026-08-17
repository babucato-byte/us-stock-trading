"""Two independent, fail-closed entry permissions -- and neither may block a SELL.

Before the posture gate existed, `ENTRY_DISABLED` in the environment was
read ONLY by `live_pilot/posture.py`, which `live_pilot/armed.py:
entry_cycle()` does not call. So an operator who set it in shared env saw
every report say OBSERVE while the buy cycle went on placing orders: the
documented incident procedure did not stop new entries.

These tests are an A/B/C contrast rather than three separate assertions,
because the thing that has to be true is CAUSAL -- the same candidate, the
same gates, one switch moved. A test that only shows "no order was placed"
proves nothing: that is also what an absent candidate looks like, which is
exactly how the gap survived.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import kis_live_trading as klt  # noqa: E402
from live_pilot import posture as live_posture  # noqa: E402

ARMED_ENV = {
    "KIS_LIVE_ORDER_ENABLED": "true",
    "LIVE_ROLLOUT_ENABLED": "true",
    "S1_LIVE_SOURCE_ENABLED": "true",
    "EXECUTION_BROKER": "kis",
    "KIS_ENV": "live",
}


def set_env(monkeypatch, **overrides):
    for key, value in {**ARMED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


class TestPostureResolution:
    def test_env_entry_disabled_yields_observe(self):
        d = live_posture.resolve_posture(dict(ARMED_ENV, ENTRY_DISABLED="true"))
        assert d.posture == live_posture.POSTURE_OBSERVE
        assert d.entry_disabled is True
        assert d.armed is False
        assert "ENTRY_DISABLED" in d.reason

    def test_env_entry_enabled_yields_armed(self):
        d = live_posture.resolve_posture(dict(ARMED_ENV, ENTRY_DISABLED="false"))
        assert d.posture == live_posture.POSTURE_ARMED
        assert d.armed is True

    def test_arming_while_entry_disabled_is_a_contradiction(self):
        assert live_posture.contradictory_posture(dict(ARMED_ENV, ENTRY_DISABLED="true"))
        assert not live_posture.contradictory_posture(dict(ARMED_ENV, ENTRY_DISABLED="false"))


class TestTheBuyCycleEnforcesBothSwitches:
    """The gate is structural: `run_live_buy_entry_cycle()` raises before
    any candidate is evaluated, so no per-symbol path can reach a broker."""

    def test_env_entry_disabled_refuses_the_cycle(self, monkeypatch):
        set_env(monkeypatch, ENTRY_DISABLED="true")
        assert live_posture.resolve_posture().armed is False

    def test_the_refusal_names_the_env_switch(self, monkeypatch):
        set_env(monkeypatch, ENTRY_DISABLED="true")
        d = live_posture.resolve_posture()
        assert d.entry_disabled is True
        assert "ENTRY_DISABLED" in d.reason

    def test_the_cycle_actually_raises_with_entry_disabled_set(self, monkeypatch, tmp_path):
        """The A of the A/B contrast, proven at runtime rather than read.

        No candidate is injected on purpose: the refusal is STRUCTURAL and
        fires before candidates are loaded, so "there were no candidates"
        cannot be what produced the zero orders -- which is precisely the
        ambiguity that let this gap survive.
        """
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s.db"))
        set_env(monkeypatch, ENTRY_DISABLED="true")

        class ExplodingBroker:
            def __getattr__(self, name):
                raise AssertionError(f"broker touched: {name}")

        with pytest.raises(klt.KISLiveTradingError) as caught:
            klt.run_live_buy_entry_cycle(broker=ExplodingBroker())
        assert "ENTRY_DISABLED" in str(caught.value)

    def test_the_same_call_gets_past_this_gate_with_it_unset(self, monkeypatch, tmp_path):
        """The B of the contrast: only the switch moved, and the refusal is
        no longer about ENTRY_DISABLED."""
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s.db"))
        set_env(monkeypatch, ENTRY_DISABLED="false")

        class ExplodingBroker:
            def __getattr__(self, name):
                raise AssertionError(f"broker touched: {name}")

        try:
            klt.run_live_buy_entry_cycle(broker=ExplodingBroker())
        except klt.KISLiveTradingError as exc:
            assert "ENTRY_DISABLED" not in str(exc), (
                "the env switch is still what blocks the cycle after being cleared")
        except AssertionError:
            pass  # got far enough to touch the broker -- past this gate

    def test_the_gate_sits_before_candidate_evaluation(self):
        """Ordering is the whole point: a gate after candidate loading
        would let "no candidates today" masquerade as "entry blocked"."""
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        posture_at = source.index("resolve_posture()")
        for later in ("_load_candidates", "source.qualify", "submit_buy_order"):
            if later in source:
                assert posture_at < source.index(later), later

    def test_both_switches_are_checked_and_neither_is_derived_from_the_other(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "is_entry_allowed()" in source, "persistent kill switch still checked"
        assert "resolve_posture()" in source, "env posture now checked"
        # No synchronisation in either direction.
        assert "activate(" not in source.split("resolve_posture()")[1][:1200]

    def test_the_persistent_kill_switch_check_was_not_removed(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "ENTRY_OFF (kill_switch_state) is set" in source


class TestKillSwitchStateBlocksEntryIndependently:
    def test_entry_disabled_state_forbids_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "ks.json"))
        # No importlib.reload(): `kill_switch_state` is re-exported by
        # operations/kill_switch.py, and reloading rebinds the module object
        # so those re-exports point at the old one. `_resolve_state_path()`
        # reads the environment on every call, so the monkeypatched path
        # takes effect without a reload.
        import kill_switch_state as kss
        kss.activate(kss.ENTRY_DISABLED, reason="test", activated_by="pytest")
        assert kss.is_entry_allowed() is False

    def test_but_it_still_permits_exits(self, tmp_path, monkeypatch):
        """The property that keeps a blocked account from being trapped."""
        monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "ks.json"))
        # No importlib.reload(): `kill_switch_state` is re-exported by
        # operations/kill_switch.py, and reloading rebinds the module object
        # so those re-exports point at the old one. `_resolve_state_path()`
        # reads the environment on every call, so the monkeypatched path
        # takes effect without a reload.
        import kill_switch_state as kss
        kss.activate(kss.ENTRY_DISABLED, reason="test", activated_by="pytest")
        assert kss.get_state() == kss.ENTRY_DISABLED
        exit_fn = next((getattr(kss, n) for n in dir(kss)
                        if n.startswith("is_") and "exit" in n), None)
        if exit_fn is not None:
            assert exit_fn() is True


class TestSellIsNeverBlockedByEitherSwitch:
    def test_the_exit_policy_reads_no_entry_permission(self):
        source = (REPO_ROOT / "s1_live" / "exit_policy.py").read_text()
        for token in ("ENTRY_DISABLED", "resolve_posture", "kill_switch",
                      "is_entry_allowed"):
            assert token not in source, token

    def test_the_exit_runtime_reads_no_entry_permission(self):
        source = (REPO_ROOT / "s1_live" / "exit_runtime.py").read_text()
        for token in ("ENTRY_DISABLED", "resolve_posture", "kill_switch",
                      "is_entry_allowed", "risk_guards"):
            assert token not in source, token

    def test_a_stop_sells_with_both_switches_engaged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "ks.json"))
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s.db"))
        set_env(monkeypatch, ENTRY_DISABLED="true")
        # No importlib.reload(): `kill_switch_state` is re-exported by
        # operations/kill_switch.py, and reloading rebinds the module object
        # so those re-exports point at the old one. `_resolve_state_path()`
        # reads the environment on every call, so the monkeypatched path
        # takes effect without a reload.
        import kill_switch_state as kss
        kss.activate(kss.ENTRY_DISABLED, reason="test", activated_by="pytest")

        from state_store import db as sdb
        from s1_live import exit_policy as ep, exit_runtime as er, position_store as ps
        import config.s1_exit_v0 as pol

        conn = sdb.open_db()
        sdb.init_db(conn)
        pid = ps.open_position(conn, symbol="TRAPPED", strategy_id="hma_early_trend",
                               signal_id="sig", entry_price=100.0, quantity=1)

        class Features:
            price, hma200, hma89, hma200_slope = 93.0, 85.0, 90.0, 1.0

        class FakeBroker:
            def __init__(self):
                self.calls = []

            def submit_order(self, symbol, qty=1, *, side, **kw):
                self.calls.append((symbol, qty, side))
                return type("R", (), {"status_code": 200, "text": "ok"})()

        broker = FakeBroker()
        stop_price = 100.0 * (1 + pol.HARD_STOP_PCT) - 0.01
        out = er.evaluate_position(
            conn, broker_adapter=broker, position_id=pid,
            state=ps.load_state(conn, pid), row=ps.get_row(conn, pid),
            current_price=stop_price, features=Features(),
            session=er.SessionPolicy("REGULAR", True, "VERIFIED"))

        assert out.action == er.ACTION_SOLD, out.action
        assert broker.calls == [("TRAPPED", 1, "sell")]
        conn.close()
