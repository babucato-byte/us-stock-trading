"""S6_EXIT_V0's compound exit: what "price weakness" has to mean.

The defect
----------
`price_weak` answered GAVE_BACK_PEAK whenever `price < peak_price`. The
peak ratchets up on every tick, so the moment a position stopped
printing a new high -- one tick of ordinary pullback, a single bar --
it was "weak", and if the volume-expansion ratio had halved from its
own peak (which it does on most winning breakouts as the initial burst
fades) the position sold. The compound rule was meant to catch a move
that has stalled and is leaking; it caught every move that was not at
its high THIS tick.

What weakness now requires
--------------------------
Two facts, both from the position's own geometry:

    peak give-back    the fraction of the breakout's gain above the
                      range high that has been surrendered:
                      (peak - price) / (peak - range_high)
    peak staleness    minutes since the peak was set

A small pullback inside a working advance surrenders a small fraction
and is HOLD. A fresh high followed by a dip is HOLD: the move has not
stalled if it made a high minutes ago. Volume decay is still never an
exit alone, and the structural exits above this one keep their order
and meaning.

The numbers are provisional, so the rule is not enforced
--------------------------------------------------------
PEAK_GIVEBACK_FRACTION and PEAK_STALE_MINUTES are recorded as
UNMEASURED and ENFORCE_PEAK_GIVEBACK_EXIT ships False. The rule is
evaluated on every tick exactly as designed; a tick it would have sold
on is recorded as such, with the raw peak / give-back / age figures
beside it, and the decision is HOLD unless a structural exit sells on
its own. The tests below run every SELL case both ways.
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_exit_v0 as policy  # noqa: E402
from market_hours import EASTERN  # noqa: E402
from s6_live import exit_diagnostics  # noqa: E402
from s6_live import exit_policy as ex  # noqa: E402
from s6_live import position_store as ps  # noqa: E402

T0 = datetime(2026, 8, 21, 12, 0, tzinfo=EASTERN)


class Features:
    def __init__(self, price=101.0, vwap=100.0, ema9=100.5, ema21=100.0,
                 volume_expansion=2.0):
        self.price, self.vwap = price, vwap
        self.ema9, self.ema21 = ema9, ema21
        self.volume_expansion = volume_expansion


def held(**kw):
    kw.setdefault("symbol", "ABC")
    kw.setdefault("entry_price", 100.0)
    kw.setdefault("range_high", 99.5)
    kw.setdefault("range_low", 99.0)
    kw.setdefault("variant", "S6-R")
    kw.setdefault("entry_volume_expansion", 2.0)
    return ex.S6PositionState(**kw)


def stale(minutes=policy.PEAK_STALE_MINUTES + 30):
    return (T0 - timedelta(minutes=minutes)).isoformat()


def fresh(minutes=2):
    return (T0 - timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", True)


# Peak 103.0 over a range high of 99.5: the breakout gained 3.5. Half of
# that is 1.75, so 101.25 is the line; 102.5 has surrendered 0.5/3.5 and
# 101.0 has surrendered 2.0/3.5.
DECAYED = dict(peak_volume_expansion=4.0)
DECAYED_FEATURES = dict(vwap=100.0, volume_expansion=2.0)


def stalled():
    """The full compound case: decay, meaningful give-back, stale peak."""
    return (held(peak_price=103.0, peak_price_at=stale(), **DECAYED),
            Features(price=101.0, **DECAYED_FEATURES))


class TestTheDefaultIsObservationOnly:
    def test_the_switch_ships_off(self):
        assert policy.ENFORCE_PEAK_GIVEBACK_EXIT is False
        assert policy.PEAK_GIVEBACK_IS_MEASURED is False

    def test_a_would_sell_tick_is_held_and_recorded(self):
        """A: candidate + decay + stale peak, enforcement OFF."""
        state, features = stalled()
        decision = ex.decide(state, current_price=101.0, features=features,
                             now=T0)
        assert decision.action == ex.HOLD
        assert decision.reason is None
        assert decision.detail["peak_exit_candidate"] is True
        assert decision.detail["peak_exit_enforced"] is False
        assert decision.detail["would_sell_reason"] == \
            ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert decision.detail["weakness"] == ex.WEAKNESS_GAVE_BACK_PEAK
        # The raw observation travels with the would-sell record.
        assert decision.detail["giveback_fraction"] == pytest.approx(2.0 / 3.5)
        assert decision.detail["peak_age_minutes"] >= policy.PEAK_STALE_MINUTES
        assert decision.detail["current_expansion"] == 2.0

    def test_the_same_tick_sells_when_enforced(self, enforced):
        """B: identical case, enforcement ON."""
        state, features = stalled()
        decision = ex.decide(state, current_price=101.0, features=features,
                             now=T0)
        assert decision.reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert decision.detail["weakness"] == ex.WEAKNESS_GAVE_BACK_PEAK
        assert decision.detail["peak_exit_enforced"] is True
        assert decision.detail["giveback_fraction"] == pytest.approx(2.0 / 3.5)
        assert decision.detail["peak_drawdown_pct"] == pytest.approx(
            (103.0 - 101.0) / 103.0 * 100.0)
        assert "would_sell_reason" not in decision.detail

    def test_the_compound_helper_never_reports_both(self):
        state, features = stalled()
        verdict = ex.compound_decay_exit(state, features, now=T0)
        assert verdict["sell"] is None and verdict["shadow"]
        assert verdict["shadow"]["would_sell_reason"] == \
            ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS

    def test_the_compound_helper_sells_only_when_enforced(self, enforced):
        state, features = stalled()
        verdict = ex.compound_decay_exit(state, features, now=T0)
        assert verdict["shadow"] is None and verdict["sell"]

    def test_the_vwap_half_is_not_gated_by_the_switch(self):
        """VWAP_BELOW carries no new threshold. It still sells -- though
        in `decide` VWAP_FAILURE, which outranks it, says so first."""
        state = held(peak_price=103.0, peak_price_at=fresh(), **DECAYED)
        verdict = ex.compound_decay_exit(
            state, Features(price=102.9, vwap=103.5, volume_expansion=2.0),
            now=T0)
        assert verdict["sell"]["weakness"] == ex.WEAKNESS_VWAP_BELOW
        assert verdict["shadow"] is None

    def test_a_shadow_tick_still_leaves_by_the_session_exit(self):
        """The shadow does not shield a position from a later rule."""
        near_close = datetime(2026, 8, 21, 15, 50, tzinfo=EASTERN)
        state = held(peak_price=103.0,
                     peak_price_at=(near_close - timedelta(hours=2)).isoformat(),
                     **DECAYED)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, **DECAYED_FEATURES),
                             session="REGULAR", now=near_close)
        assert decision.reason == ex.REASON_SESSION_EXIT


@pytest.mark.parametrize("enforce", [False, True])
class TestASmallPullbackInsideAnAdvanceIsHeld:
    """C, either way round the switch."""

    def test_one_tick_below_the_peak_is_not_weakness(self, enforce, monkeypatch):
        """The defect: this used to sell."""
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=102.9,
                             features=Features(price=102.9, **DECAYED_FEATURES),
                             now=T0)
        assert decision.action == ex.HOLD
        assert decision.detail.get("would_sell_reason") is None

    def test_a_small_fraction_of_the_gain_given_back_is_held(self, enforce,
                                                             monkeypatch):
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=102.5,
                             features=Features(price=102.5, **DECAYED_FEATURES),
                             now=T0)
        assert decision.action == ex.HOLD
        assert decision.detail.get("would_sell_reason") is None
        assert ex.price_weak(state, Features(price=102.5, **DECAYED_FEATURES),
                             now=T0) is None

    def test_a_pullback_right_after_a_new_high_is_held(self, enforce,
                                                       monkeypatch):
        """D: meaningful give-back, but the high was minutes ago: the
        move has not stalled, it is being shaken."""
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=fresh(), **DECAYED)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, **DECAYED_FEATURES),
                             now=T0)
        assert decision.action == ex.HOLD
        assert decision.detail.get("would_sell_reason") is None


class TestTheGiveBackRuleAsDesigned:
    """What the rule ANSWERS, independent of whether it may act."""

    def test_exactly_the_fraction_counts(self):
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        at_line = 103.0 - 3.5 * policy.PEAK_GIVEBACK_FRACTION
        weak = ex.price_weak(state, Features(price=at_line, **DECAYED_FEATURES),
                             now=T0)
        assert weak and weak["weakness"] == ex.WEAKNESS_GAVE_BACK_PEAK

    def test_an_undated_peak_is_judged_on_give_back_alone(self, enforced):
        """Rows opened before the peak was dated carry no time; the
        drawdown still decides rather than the rule going silent."""
        state = held(peak_price=103.0, peak_price_at=None, **DECAYED)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, **DECAYED_FEATURES),
                             now=T0)
        assert decision.reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert decision.detail["peak_age_minutes"] is None

    def test_without_a_clock_the_peak_age_is_read_from_the_wall(self):
        """`now=None` is how the policy is called when the runtime has
        no moment to hand it; the peak's age is still knowable."""
        state = held(peak_price=103.0, peak_price_at=stale(60 * 24), **DECAYED)
        weak = ex.price_weak(state, Features(price=101.0, **DECAYED_FEATURES))
        assert weak and weak["peak_age_minutes"] > policy.PEAK_STALE_MINUTES

    def test_a_peak_at_or_below_the_range_high_cannot_give_back(self):
        """The gain above the range is zero, so no fraction of it
        exists; the range re-entry rule is the one that speaks there."""
        state = held(peak_price=99.5, peak_price_at=stale(), **DECAYED)
        assert ex.peak_given_back(
            state, Features(price=99.4, **DECAYED_FEATURES), now=T0) is None

    def test_a_missing_range_high_cannot_give_back(self):
        state = held(range_high=None, peak_price=103.0, peak_price_at=stale(),
                     **DECAYED)
        assert ex.peak_given_back(
            state, Features(price=101.0, **DECAYED_FEATURES), now=T0) is None


@pytest.mark.parametrize("enforce", [False, True])
class TestVolumeDecayStillNeverExitsAlone:
    """E."""

    def test_decay_with_price_at_its_high_is_held(self, enforce, monkeypatch):
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=103.0,
                             features=Features(price=103.0, **DECAYED_FEATURES),
                             now=T0)
        assert decision.action == ex.HOLD
        assert decision.detail.get("would_sell_reason") is None

    def test_decay_with_a_working_advance_is_held(self, enforce, monkeypatch):
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=101.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=102.0,
                             features=Features(price=102.0, **DECAYED_FEATURES),
                             now=T0)
        assert decision.action == ex.HOLD
        assert decision.detail.get("would_sell_reason") is None

    def test_give_back_without_decay_is_held(self, enforce, monkeypatch):
        """The mirror: weakness never exits alone either."""
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=stale(),
                     peak_volume_expansion=4.0)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, vwap=100.0,
                                               volume_expansion=3.5),
                             now=T0)
        assert decision.action == ex.HOLD
        assert decision.detail.get("would_sell_reason") is None

    def test_the_flag_is_still_the_compound_one(self, enforce):
        assert policy.EXIT_ON_VOLUME_DECAY_WITH_WEAKNESS is True
        assert not hasattr(policy, "EXIT_ON_VOLUME_DECAY")


@pytest.mark.parametrize("enforce", [False, True])
class TestTheStructuralExitsKeepTheirOrder:
    """F, either way round the switch."""

    def test_back_inside_the_range_is_still_a_reentry(self, enforce, monkeypatch):
        """Give-back past the whole gain IS a re-entry, and is named as
        one, not as price weakness."""
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=99.4,
                             features=Features(price=99.4, **DECAYED_FEATURES),
                             now=T0)
        assert decision.reason == ex.REASON_RANGE_REENTRY

    def test_vwap_failure_still_outranks_the_compound_exit(self, enforce,
                                                           monkeypatch):
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, vwap=101.5,
                                               volume_expansion=2.0),
                             now=T0)
        assert decision.reason == ex.REASON_VWAP_FAILURE

    def test_ema_failure_still_outranks_the_compound_exit(self, enforce,
                                                          monkeypatch):
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, vwap=100.0,
                                               ema9=100.0, ema21=100.5,
                                               volume_expansion=2.0),
                             now=T0)
        assert decision.reason == ex.REASON_EMA_STRUCTURE_FAILURE

    def test_the_hard_risk_cap_is_untouched(self, enforce, monkeypatch):
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=98.9,
                             features=Features(price=98.9, **DECAYED_FEATURES),
                             now=T0)
        assert decision.reason == ex.REASON_HARD_RISK_CAP

    def test_the_session_exit_is_still_last(self, enforce, monkeypatch):
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        near_close = datetime(2026, 8, 21, 15, 50, tzinfo=EASTERN)
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        decision = ex.decide(state, current_price=102.9,
                             features=Features(price=102.9, **DECAYED_FEATURES),
                             session="REGULAR", now=near_close)
        assert decision.reason == ex.REASON_SESSION_EXIT

    def test_emergency_and_no_structure_are_untouched(self, enforce, monkeypatch):
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state, features = stalled()
        assert ex.decide(state, features=features, now=T0,
                         emergency=True).reason == ex.REASON_EMERGENCY
        assert ex.decide(held(range_low=None), current_price=100.0,
                         features=Features(100.0)).reason == \
            ex.REASON_NO_STRUCTURE

    def test_the_vwap_weakness_branch_is_unchanged(self, enforce, monkeypatch):
        """`price_weak` still reports VWAP_BELOW first; the give-back
        test is the second branch only."""
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", enforce)
        state = held(peak_price=103.0, peak_price_at=fresh(), **DECAYED)
        weak = ex.price_weak(state, Features(price=102.9, vwap=103.5), now=T0)
        assert weak["weakness"] == ex.WEAKNESS_VWAP_BELOW


#: What a replay of alternative give-back / staleness thresholds needs
#: on EVERY tick, as raw numbers rather than as the verdict.
RAW_PEAK_FIELDS = (
    "peak_price", "peak_price_at", "peak_age_minutes", "current_price",
    "range_high", "breakout_gain_at_peak", "giveback_amount",
    "giveback_fraction", "peak_volume_expansion", "current_volume_expansion",
    "volume_decay_triggered", "peak_weakness_triggered",
    "peak_exit_candidate", "peak_exit_enforced", "would_sell_reason",
)


class TestTheDiagnosticsCarryTheRawObservation:
    """H."""

    def test_they_are_recorded_as_unmeasured(self):
        assert policy.PEAK_GIVEBACK_IS_MEASURED is False
        assert 0.0 < policy.PEAK_GIVEBACK_FRACTION < 1.0
        assert policy.PEAK_STALE_MINUTES > 0
        assert any("give" in q.lower() for q in policy.REEVALUATION_QUESTIONS)

    def test_a_hold_tick_carries_every_raw_field(self):
        state = held(peak_price=103.0, peak_price_at=stale(), **DECAYED)
        features = Features(price=102.5, **DECAYED_FEATURES)
        decision = ex.decide(state, current_price=102.5, features=features,
                             now=T0)
        assert decision.action == ex.HOLD
        record = exit_diagnostics.evaluate(state, features=features,
                                           price=102.5, now=T0,
                                           decision=decision)
        peak = record["peak"]
        for name in RAW_PEAK_FIELDS:
            assert name in peak, name
        assert peak["peak_price"] == 103.0
        assert peak["peak_price_at"] == stale()
        assert peak["peak_age_minutes"] == pytest.approx(
            policy.PEAK_STALE_MINUTES + 30)
        assert peak["current_price"] == 102.5
        assert peak["range_high"] == 99.5
        assert peak["breakout_gain_at_peak"] == pytest.approx(3.5)
        assert peak["giveback_amount"] == pytest.approx(0.5)
        assert peak["giveback_fraction"] == pytest.approx(0.5 / 3.5)
        assert peak["peak_volume_expansion"] == 4.0
        assert peak["current_volume_expansion"] == 2.0
        assert peak["volume_decay_triggered"] is True
        assert peak["peak_weakness_triggered"] is False
        assert peak["peak_exit_candidate"] is False
        assert peak["peak_exit_enforced"] is False
        assert peak["would_sell_reason"] is None
        assert peak["giveback_fraction_threshold"] == policy.PEAK_GIVEBACK_FRACTION
        assert peak["stale_minutes_threshold"] == policy.PEAK_STALE_MINUTES
        assert record["would_sell_reason"] is None

    def test_a_would_sell_tick_is_named_without_losing_the_numbers(self):
        state, features = stalled()
        decision = ex.decide(state, current_price=101.0, features=features,
                             now=T0)
        record = exit_diagnostics.evaluate(state, features=features,
                                           price=101.0, now=T0,
                                           decision=decision)
        assert record["action"] == ex.HOLD
        assert record["would_sell_reason"] == \
            ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        peak = record["peak"]
        assert peak["peak_exit_candidate"] is True
        assert peak["peak_exit_enforced"] is False
        assert peak["giveback_fraction"] == pytest.approx(2.0 / 3.5)
        assert peak["giveback_amount"] == pytest.approx(2.0)
        # A condition marked TRUE is one that sells; this one did not.
        assert record["conditions"][
            ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS] == exit_diagnostics.FALSE

    def test_an_enforced_sell_is_a_true_condition_and_no_shadow(self, enforced):
        state, features = stalled()
        decision = ex.decide(state, current_price=101.0, features=features,
                             now=T0)
        record = exit_diagnostics.evaluate(state, features=features,
                                           price=101.0, now=T0,
                                           decision=decision)
        assert record["action"] == ex.SELL
        assert record["would_sell_reason"] is None
        assert record["peak"]["peak_exit_enforced"] is True
        assert record["conditions"][
            ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS] == exit_diagnostics.TRUE

    def test_a_fresh_peak_dip_is_recorded_as_not_a_candidate(self):
        state = held(peak_price=103.0, peak_price_at=fresh(), **DECAYED)
        features = Features(price=101.0, **DECAYED_FEATURES)
        record = exit_diagnostics.evaluate(state, features=features,
                                           price=101.0, now=T0)
        peak = record["peak"]
        assert peak["volume_decay_triggered"] is True
        assert peak["peak_weakness_triggered"] is False
        assert peak["peak_exit_candidate"] is False
        # The numbers a different staleness threshold would need are
        # still there: 2 minutes old, 57% given back.
        assert peak["peak_age_minutes"] == pytest.approx(2.0)
        assert peak["giveback_fraction"] == pytest.approx(2.0 / 3.5)

    def test_an_alternative_threshold_can_be_replayed_from_the_record(self):
        """The point of the raw fields: re-decide with other numbers."""
        state = held(peak_price=103.0, peak_price_at=fresh(10), **DECAYED)
        record = exit_diagnostics.evaluate(
            state, features=Features(price=102.0, **DECAYED_FEATURES),
            price=102.0, now=T0)["peak"]

        def replay(fraction, stale_minutes):
            return (record["volume_decay_triggered"]
                    and record["giveback_fraction"] >= fraction
                    and (record["peak_age_minutes"] is None
                         or record["peak_age_minutes"] >= stale_minutes))

        assert record["peak_exit_candidate"] is False        # 0.5 / 30
        assert replay(0.25, 5) is True                       # looser
        assert replay(0.25, 15) is False                     # staler
        assert replay(0.5, 5) is False                       # deeper


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def opened(conn, symbol="ABC", now=T0):
    pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                               entry_session="REGULAR", range_high=99.5,
                               range_low=99.0, entry_volume_expansion=2.0,
                               now=now)
    ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                      venue="NASD", now=now)
    return pid


class TestThePeakIsDated:
    def test_the_fill_dates_the_first_peak(self, conn):
        pid = opened(conn)
        row = ps.load(conn, pid)
        assert row["peak_price"] == 100.0
        assert row["peak_price_at"] == T0.isoformat()

    def test_a_new_peak_moves_the_date(self, conn):
        pid = opened(conn)
        later = T0 + timedelta(minutes=10)
        ps.observe(conn, pid, price=101.0, now=later)
        row = ps.load(conn, pid)
        assert row["peak_price"] == 101.0
        assert row["peak_price_at"] == later.isoformat()

    def test_a_lower_reading_leaves_the_date_alone(self, conn):
        pid = opened(conn)
        ps.observe(conn, pid, price=101.0, now=T0 + timedelta(minutes=10))
        ps.observe(conn, pid, price=100.5, now=T0 + timedelta(minutes=20))
        row = ps.load(conn, pid)
        assert row["peak_price"] == 101.0
        assert row["peak_price_at"] == (T0 + timedelta(minutes=10)).isoformat()

    def test_the_state_carries_the_date(self, conn):
        pid = opened(conn)
        state = ps.to_state(ps.load(conn, pid))
        assert state.peak_price_at == T0.isoformat()


class Broker:
    def __init__(self):
        self.orders = []

    def submit_order(self, symbol, quantity, *, side, client_order_id=None):
        self.orders.append((symbol, quantity, side))
        return {"status": 200, "order_id": "X1"}


def _stalled_position(conn):
    """A position that made its high, then gave most of it back."""
    from s6_live import exit_runtime as er

    pid = opened(conn)
    ps.observe(conn, pid, price=103.0, volume_expansion=4.0,
               now=T0 + timedelta(minutes=5))
    broker = Broker()
    # Two minutes after the high, 0.5 of a 3.5 gain given back.
    outcome = er.evaluate_position(
        conn, broker_adapter=broker, position_id=pid, row=ps.load(conn, pid),
        features=Features(price=102.5, **DECAYED_FEATURES),
        current_price=102.5, session="REGULAR",
        now=T0 + timedelta(minutes=7))
    assert outcome.action == er.ACTION_HELD
    assert broker.orders == []
    return pid, broker


class TestTheRuntimeEndToEnd:
    def test_unenforced_the_stalled_position_is_held_with_a_would_sell(self, conn):
        from s6_live import exit_runtime as er

        pid, broker = _stalled_position(conn)
        outcome = er.evaluate_position(
            conn, broker_adapter=broker, position_id=pid, row=ps.load(conn, pid),
            features=Features(price=101.0, **DECAYED_FEATURES),
            current_price=101.0, session="REGULAR",
            now=T0 + timedelta(minutes=5 + policy.PEAK_STALE_MINUTES + 30))
        assert outcome.action == er.ACTION_HELD
        assert broker.orders == []
        assert outcome.detail["would_sell_reason"] == \
            ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert outcome.detail["peak"]["peak_exit_candidate"] is True
        assert outcome.detail["peak"]["peak_price_at"] == (
            T0 + timedelta(minutes=5)).isoformat()

    def test_enforced_the_stalled_position_sells(self, conn, enforced):
        from s6_live import exit_runtime as er

        pid, broker = _stalled_position(conn)
        outcome = er.evaluate_position(
            conn, broker_adapter=broker, position_id=pid, row=ps.load(conn, pid),
            features=Features(price=101.0, **DECAYED_FEATURES),
            current_price=101.0, session="REGULAR",
            now=T0 + timedelta(minutes=5 + policy.PEAK_STALE_MINUTES + 30))
        assert outcome.reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert len(broker.orders) == 1
