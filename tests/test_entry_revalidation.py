"""What is re-asked once the execution lock is finally held.

Entry analysis now runs WITHOUT the execution lock -- that is the whole
point of the 2026-09-02 fix. The cost of that is a window: everything
decided during candidate loading, pre-trade validation, quoting and
sizing was decided against a world that was free to move before the
order was sent.

So each of these is a state that was true when the entry was prepared
and false by the time it could be submitted. Every one of them must drop
the prepared order rather than send it.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import kis_live_trading as klt  # noqa: E402
from s6_live import position_store  # noqa: E402

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)


class _Intent:
    internal_order_id = "iid-1"
    quantity = 7
    limit_price = 17.01


class _Signal:
    signal_id = "sig-1"

    def __init__(self, expired=False):
        self._expired = expired

    def is_expired(self, now=None):
        return self._expired


class _Instrument:
    symbol = "HBAN"
    exchange = "NASDAQ"


class _Broker:
    def __init__(self, open_orders=(), orderable=1000.0, fail=None):
        self._orders = list(open_orders)
        self._orderable = orderable
        self._fail = fail

    def get_open_orders(self):
        if self._fail == "orders":
            raise RuntimeError("KIS unreachable")
        return self._orders

    def get_orderable_usd(self, instrument, price):
        if self._fail == "cash":
            raise RuntimeError("KIS unreachable")
        return self._orderable


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "pos.json"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


@pytest.fixture(autouse=True)
def permissive_switches(monkeypatch):
    monkeypatch.setattr(klt.ops_kill_switch, "is_halted", lambda: False)
    monkeypatch.setattr(klt.ops_kill_switch, "is_entry_allowed", lambda: True)
    monkeypatch.delenv("ENTRY_DISABLED", raising=False)


def _revalidate(conn, broker, **kw):
    params = dict(symbol="HBAN", broker=broker, conn=conn,
                  instrument=_Instrument(), order_intent=_Intent(),
                  buffered_price=17.01, live_state={},
                  signal=_Signal(), now=NOW)
    params.update(kw)
    return klt._revalidate_before_submit(**params)


class TestNothingChanged:
    def test_a_clean_revalidation_permits_the_order(self, conn):
        assert _revalidate(conn, _Broker()) is None

    def test_it_refreshes_the_values_the_gate_will_use(self, conn):
        state = {"available_usd": 1.0, "has_open_order_for_symbol": True}
        assert _revalidate(conn, _Broker(orderable=930.0),
                           live_state=state) is None
        assert state["available_usd"] == 930.0, (
            "the gate must size against cash read under the lock, not "
            "against the number the sizing step saw minutes ago")
        assert state["has_open_order_for_symbol"] is False


class TestTheKillSwitchesAreReAsked:
    def test_a_halt_set_during_analysis_drops_the_order(self, conn, monkeypatch):
        monkeypatch.setattr(klt.ops_kill_switch, "is_halted", lambda: True)
        code, _ = _revalidate(conn, _Broker())
        assert code == klt.REVALIDATION_HALT

    def test_entry_off_set_during_analysis_drops_the_order(self, conn, monkeypatch):
        monkeypatch.setattr(klt.ops_kill_switch, "is_entry_allowed", lambda: False)
        code, _ = _revalidate(conn, _Broker())
        assert code == klt.REVALIDATION_ENTRY_OFF

    def test_operator_posture_set_during_analysis_drops_the_order(self, conn, monkeypatch):
        monkeypatch.setenv("ENTRY_DISABLED", "true")
        code, _ = _revalidate(conn, _Broker())
        assert code == klt.REVALIDATION_ENTRY_DISABLED_ENV

    def test_an_unreadable_switch_drops_the_order(self, conn, monkeypatch):
        def boom():
            raise RuntimeError("state file gone")
        monkeypatch.setattr(klt.ops_kill_switch, "is_halted", boom)
        code, _ = _revalidate(conn, _Broker())
        assert code == klt.REVALIDATION_STATE_UNREADABLE


class TestAnExitOutranksTheEntry:
    def test_a_latched_pending_exit_alone_does_not_drop_the_order(self, conn):
        """RIG, 2026: a latched EXIT_PENDING whose route was unavailable
        returned "exit in flight" for three days and deferred every
        entry, protecting a cycle that could never start. Only an order
        actually at the broker stands a new entry down here."""
        pid = position_store.record_submission(
            conn, symbol="MTCH", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid-mtch", now=NOW)
        position_store.open_from_fill(conn, pid, quantity=2,
                                      average_fill_price=41.36, now=NOW)
        position_store.latch_pending_exit(conn, pid, "VWAP_FAILURE", now=NOW)
        assert _revalidate(conn, _Broker()) is None

    def test_an_exit_that_reached_the_broker_drops_the_order(self, conn):
        pid = position_store.record_submission(
            conn, symbol="MTCH", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid-mtch", now=NOW)
        position_store.open_from_fill(conn, pid, quantity=2,
                                      average_fill_price=41.36, now=NOW)
        position_store.mark_exit_submitted(conn, pid, "VWAP_FAILURE", now=NOW)
        code, _ = _revalidate(conn, _Broker())
        assert code == klt.REVALIDATION_EXIT_IN_FLIGHT


class TestTheSymbolBecameHeld:
    def test_a_position_opened_during_analysis_drops_the_order(self, conn):
        pid = position_store.record_submission(
            conn, symbol="HBAN", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid-hban", now=NOW)
        position_store.open_from_fill(conn, pid, quantity=7,
                                      average_fill_price=17.01, now=NOW)
        code, _ = _revalidate(conn, _Broker())
        assert code == klt.REVALIDATION_SYMBOL_HELD

    def test_a_submitted_row_also_drops_the_order(self, conn):
        """SUBMITTED is live too -- an order already on its way."""
        position_store.record_submission(
            conn, symbol="HBAN", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid-hban", now=NOW)
        code, _ = _revalidate(conn, _Broker())
        assert code == klt.REVALIDATION_SYMBOL_HELD


class TestDuplicateOrderPrevention:
    """The decision stays with the gate; revalidation only makes the
    reading current.

    The gate already refuses a symbol with a resting order (code
    OPEN_ORDER) and the engine's ledger refuses a repeated signal
    (DUPLICATE_BLOCKED). Both were correct and both were reading an
    open-order book from before the analysis. Deciding it here as well
    would shadow the more specific refusal and drop DUPLICATE_BLOCKED
    from the audit trail.
    """

    def test_a_resting_order_is_reported_to_the_gate(self, conn):
        state = {"has_open_order_for_symbol": False, "available_usd": 0.0}
        broker = _Broker(open_orders=[{"pdno": "HBAN"}])
        assert _revalidate(conn, broker, live_state=state) is None
        assert state["has_open_order_for_symbol"] is True, (
            "the gate must see the order that appeared during analysis")

    def test_another_symbols_resting_order_is_irrelevant(self, conn):
        state = {}
        broker = _Broker(open_orders=[{"pdno": "NVDA"}])
        assert _revalidate(conn, broker, live_state=state) is None
        assert state["has_open_order_for_symbol"] is False

    def test_an_unreadable_order_book_drops_the_order(self, conn):
        code, _ = _revalidate(conn, _Broker(fail="orders"))
        assert code == klt.REVALIDATION_STATE_UNREADABLE


class TestCashIsReReadUnderTheLock:
    """Again: refreshed here, refused by the gate."""

    def test_cash_spent_during_analysis_reaches_the_gate(self, conn):
        state = {"available_usd": 5000.0}
        assert _revalidate(conn, _Broker(orderable=100.0),
                           live_state=state) is None
        assert state["available_usd"] == 100.0, (
            "the gate must size against the balance as it is now, not as "
            "it was before another entry in this cycle spent it")

    def test_an_unreadable_balance_drops_the_order(self, conn):
        code, _ = _revalidate(conn, _Broker(fail="cash"))
        assert code == klt.REVALIDATION_STATE_UNREADABLE


class TestSignalFreshnessRemainsCycleScoped:
    def test_analysis_over_120_seconds_does_not_add_a_submit_time_refusal(
            self, conn):
        """The historical gate owns freshness against the cycle clock.

        A lock refactor must not silently ask the same signal again
        against a wall clock five minutes later and change trade selection.
        """
        calls = []

        class _ExpiredAtSubmit(_Signal):
            def is_expired(self, now=None):
                calls.append(now)
                return True

        assert klt.SIGNAL_VALID_SECONDS == 120
        assert _revalidate(
            conn, _Broker(), signal=_ExpiredAtSubmit(),
            now=NOW + timedelta(minutes=5)) is None
        assert calls == [], "submit-time revalidation must not re-ask freshness"
