"""Nothing that decides a trade changed in this observability work.

Why assert it explicitly
------------------------
Every module added recently -- slippage, closed-bar shadow, post-exit,
warmup, tiers, rotation -- exists to MEASURE. The standing instruction
is that thresholds, exits, sizing and capital authority stay frozen
while that measurement accumulates, so that when a change is eventually
argued for, the before-and-after is a real comparison rather than two
different systems.

The risk is not a deliberate edit. It is a plausible-looking one made
while wiring an observation in: a shadow comparison that quietly becomes
the live reading, a warmup gate that changes what counts as READY, a
capital field "corrected" in passing. Those are exactly the changes that
would look like tidying in a diff.

So the frozen values are written down here. This file failing means
either the freeze was broken or the freeze was lifted deliberately -- and
in the second case, updating it is the moment to notice you are doing
it.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestCapitalAuthorityIsUnchanged:
    """The field that answers "how much can we spend". Getting this
    wrong once already meant the runtime computed 0 shares where KIS
    would have allowed 1."""

    def test_the_orderable_amount_field_is_the_authority(self):
        from brokers import kis_broker

        assert kis_broker.ORDERABLE_AMOUNT_FIELD == "ovrs_ord_psbl_amt"

    def test_the_cash_component_is_not_the_authority(self):
        """`ord_psbl_frcr_amt` is cash only. Using it as the authority
        ignored unsettled sell proceeds -- the account showed $54.44 and
        the code used $20.96."""
        from brokers import kis_broker

        assert kis_broker.ORDERABLE_CASH_COMPONENT_FIELD == "ord_psbl_frcr_amt"
        assert (kis_broker.ORDERABLE_CASH_COMPONENT_FIELD
                != kis_broker.ORDERABLE_AMOUNT_FIELD)

    def test_the_broker_quantity_field_is_unchanged(self):
        from brokers import kis_broker

        assert kis_broker.ORDERABLE_QTY_FIELD == "max_ord_psbl_qty"


class TestExitRulesAreUnchanged:
    def test_every_exit_trigger_keeps_its_setting(self):
        from config import s6_exit_v0 as exits

        assert exits.EXIT_ON_RANGE_REENTRY is True
        assert exits.EXIT_ON_VWAP_FAILURE is True
        assert exits.EXIT_ON_EMA_STRUCTURE_FAILURE is True
        assert exits.EXIT_ON_VOLUME_DECAY_WITH_WEAKNESS is True
        assert exits.EXIT_ON_SESSION_END is True

    def test_the_volume_decay_fraction_is_unchanged(self):
        from config import s6_exit_v0 as exits

        assert exits.VOLUME_DECAY_FRACTION == pytest.approx(0.5)

    def test_overnight_carry_is_still_off(self):
        from config import s6_exit_v0 as exits

        assert exits.ALLOW_OVERNIGHT_CARRY is False

    def test_the_hard_risk_level_is_unchanged(self):
        from config import s6_exit_v0 as exits

        assert exits.HARD_RISK_LEVEL == "RANGE_LOW"


class TestTheDeferredChangesAreStillDeferred:
    """The list that must not reach production while measurement runs.
    Each is checked as an absence in the modules that would host it."""

    def test_no_IOC_or_requote_in_the_entry_path(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        for forbidden in ("IOC", "immediate_or_cancel", "re_quote", "requote"):
            assert forbidden not in source, forbidden

    def test_no_spread_gate_in_the_entry_path(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        for forbidden in ("spread_gate", "max_spread", "spread_bps_limit"):
            assert forbidden not in source, forbidden

    def test_no_trailing_stop_or_partial_take_profit(self):
        source = (REPO_ROOT / "config" / "s6_exit_v0.py").read_text()
        for forbidden in ("TRAILING_STOP", "PARTIAL_TAKE_PROFIT",
                          "TIME_DECAY"):
            assert forbidden not in source, forbidden

    def test_the_observation_modules_change_no_parameter(self):
        """Each was added to measure. None may reach back into the
        rules it is measuring."""
        for module in ("s6_live/slippage_log.py",
                       "s6_live/closed_bar_shadow.py",
                       "s6_live/slot_rotation.py",
                       "s6_live/warmup.py",
                       "s6_live/discovery_tiers.py"):
            source = (REPO_ROOT / module).read_text()
            for forbidden in ("submit_buy", "submit_sell", "execution_engine",
                              "order_gate", "close_position",
                              "mark_exit_submitted"):
                assert forbidden not in source, f"{module}: {forbidden}"


class TestOwnershipAndReconciliationAreUnchanged:
    def test_a_conflict_is_still_a_conflict(self):
        """Never deduped into one holding that matches the broker --
        that arithmetic looks clean and hides the double SELL."""
        from reconciliation import ownership

        assert ownership.OWNERSHIP_CONFLICT == "OWNERSHIP_CONFLICT"

    def test_a_released_row_is_not_recorded_as_a_trade(self):
        from reconciliation import ownership

        assert ownership.RELEASED_WRONGLY_ATTRIBUTED \
            == "RELEASED_WRONGLY_ATTRIBUTED"

    def test_adoption_still_fails_closed(self):
        import inspect

        from reconciliation import ownership

        source = inspect.getsource(ownership.may_adopt)
        assert "return False" in source


class TestTheSubscriptionLimitIsStillTheMeasuredOne:
    def test_forty_one_is_not_quietly_raised(self):
        """Measured against KIS (OPSP0008 MAX SUBSCRIBE OVER). Raising
        it because more symbols are wanted would fail at the broker, not
        here."""
        from market_data import kis_hdfscnt0 as wire

        assert wire.MAX_SUBSCRIPTIONS == 41

    def test_one_connection_per_appkey_still_holds(self):
        from market_data import kis_hdfscnt0 as wire

        assert wire.ONE_CONNECTION_PER_APPKEY is True
