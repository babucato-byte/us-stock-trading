"""EXIT V2 PHASE 1: durable exit-evaluation snapshots. Instrumentation
only -- every test in `TestDecisionUnchanged` proves the persisted
record never altered what `exit_policy.decide()`/`exit_runtime` already
decided.
"""

import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from s6_live import exit_diagnostics, exit_policy, exit_snapshot
from s6_live import realtime_features as rf
from s6_live.entry_quality import EntryQuality

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _row(**overrides):
    kwargs = dict(
        position_id="s6pos_test1", symbol="RIG", entry_price=5.79,
        quantity=28, entry_time=(NOW - timedelta(minutes=20)).isoformat(),
        range_high=5.76, range_low=5.71, exit_submitted=0,
    )
    kwargs.update(overrides)
    return kwargs


def _quality(**overrides):
    base = dict(
        symbol="RIG", session="PREMARKET", scanner_variant="S6_ORB5",
        orb_minutes=5, bar_count=15, recent_volume_5m=3.0,
        recent_volume_10m=800.0, recent_volume_15m=1200.0,
        dollar_volume_5m=17.37, spread_bps=None,
    )
    base.update(overrides)
    return EntryQuality(**base)


def _feats(entry_quality=None, **overrides):
    kwargs = dict(
        symbol="RIG", session="PREMARKET", market_data_asof=NOW, price=5.75,
        vwap=5.80, ema9=5.70, ema21=5.72, volume=100, volume_status=rf.VOLUME_OK,
        volume_expansion=1.1, range_high=5.76, range_low=5.71,
        entry_quality=entry_quality,
    )
    kwargs.update(overrides)
    return rf.SessionFeatures(**kwargs)


def _state(**overrides):
    kwargs = dict(
        symbol="RIG", entry_price=5.79, range_high=5.76, range_low=5.71,
        peak_price=5.85, peak_price_at=NOW.isoformat(), exit_submitted=False,
    )
    kwargs.update(overrides)
    return exit_policy.S6PositionState(**kwargs)


def _build(conn, *, state=None, feats=None, row=None, now=NOW, current_price=5.75,
          session="PREMARKET"):
    state = state or _state()
    feats = feats if feats is not None else _feats(entry_quality=_quality())
    row = row or _row()
    session = session if session is not None else feats.session
    decision = exit_policy.decide(state, current_price=current_price,
                                  features=feats, session=session, now=now)
    diagnostics = exit_diagnostics.evaluate(
        state, features=feats, price=exit_policy._price_of(feats, current_price),
        session=session, now=now, decision=decision)
    record = exit_snapshot.build(conn=conn, position_id=row["position_id"], row=row,
                                 features=feats, diagnostics=diagnostics,
                                 decision=decision, now=now)
    return record, decision, diagnostics


class TestSnapshotCapturesRequiredFields:
    def test_the_full_field_set_is_present(self, conn):
        record, decision, _diag = _build(conn)
        for field in (
            "position_id", "symbol", "session", "evaluated_at", "entry_price",
            "current_price", "position_qty", "time_in_trade_seconds",
            "range_high", "range_low", "vwap", "price_minus_vwap",
            "price_vs_vwap_pct", "ema9", "ema21", "current_exit_reason",
            "current_exit_priority", "range_reentry", "hard_risk_cap",
            "vwap_breach", "ema_structure_failure", "recent_volume_5m",
            "recent_volume_10m", "recent_volume_15m", "dollar_volume_5m",
            "nonzero_bar_count", "data_age_seconds", "peak_price",
            "peak_gain_pct", "drawdown_from_peak_pct", "exit_submitted",
            "active_exit_intent_id", "active_broker_order_id", "vwap_state",
            "liquidity_state", "momentum_state",
        ):
            assert field in record, field

    def test_time_in_trade_progresses(self, conn):
        row = _row(entry_time=(NOW - timedelta(minutes=37)).isoformat())
        record, _d, _diag = _build(conn, row=row)
        assert record["time_in_trade_seconds"] == pytest.approx(37 * 60, abs=1)

    def test_vwap_relationship_is_recorded(self, conn):
        record, _d, _diag = _build(conn)
        assert record["vwap"] == 5.80
        assert record["price_minus_vwap"] == pytest.approx(5.75 - 5.80)
        assert record["price_vs_vwap_pct"] == pytest.approx((5.75 / 5.80 - 1) * 100)

    def test_priority_matches_the_documented_order(self, conn):
        state = _state(range_high=100.0)  # forces RANGE_REENTRY (priority 3)
        record, decision, _diag = _build(conn, state=state, current_price=5.75)
        assert decision.reason == exit_policy.REASON_RANGE_REENTRY
        assert record["current_exit_priority"] == 3

    def test_hold_tick_has_no_priority(self, conn, monkeypatch):
        from config import s6_exit_v0 as policy

        monkeypatch.setattr(policy, "EXIT_ON_SESSION_END", False)
        # A healthy tick with no rule firing: HOLD, reason None.
        healthy = _feats(entry_quality=_quality(), price=6.0, vwap=5.5,
                         ema9=5.9, ema21=5.8)
        state = _state(range_high=5.0, range_low=4.5, peak_price=6.0)
        record, decision, _diag = _build(conn, state=state, feats=healthy,
                                        current_price=6.0)
        assert decision.action == exit_policy.HOLD
        assert record["current_exit_reason"] is None
        assert record["current_exit_priority"] is None


class TestMissingOptionalDataStaysNullNotFalse:
    def test_no_entry_quality_leaves_liquidity_fields_null(self, conn):
        record, _d, _diag = _build(conn, feats=_feats(entry_quality=None))
        assert record["recent_volume_5m"] is None
        assert record["dollar_volume_5m"] is None
        assert record["nonzero_bar_count"] is None
        assert record["liquidity_state"] == exit_snapshot.LIQUIDITY_UNKNOWN

    def test_regular_session_does_not_fabricate_a_nonzero_bar_count(self, conn):
        # yfinance-backed REGULAR bars are zero-padded per minute; bar_count
        # there is NOT an exact nonzero count and must not be reported as one.
        quality = _quality(session="REGULAR", bar_count=40)
        record, _d, _diag = _build(
            conn, feats=_feats(entry_quality=quality, session="REGULAR"),
            session="REGULAR")
        assert record["nonzero_bar_count"] is None

    def test_premarket_session_reports_the_exact_nonzero_count(self, conn):
        quality = _quality(session="PREMARKET", bar_count=9)
        record, _d, _diag = _build(conn, feats=_feats(entry_quality=quality))
        assert record["nonzero_bar_count"] == 9

    def test_no_entry_time_leaves_time_in_trade_null(self, conn):
        record, _d, _diag = _build(conn, row=_row(entry_time=None))
        assert record["time_in_trade_seconds"] is None

    def test_no_peak_price_leaves_peak_fields_null(self, conn):
        record, _d, _diag = _build(conn, state=_state(peak_price=None,
                                                       peak_price_at=None))
        assert record["peak_price"] is None
        assert record["peak_gain_pct"] is None

    def test_no_active_intent_is_null_not_a_fabricated_id(self, conn):
        record, _d, _diag = _build(conn)
        assert record["active_exit_intent_id"] is None
        assert record["active_broker_order_id"] is None


class TestActiveExitIntentIsRead:
    def test_an_active_intent_is_attached(self, conn):
        from s6_live import position_store
        from state_store import exit_intent_ledger

        pid = position_store.record_submission(
            conn, symbol="RIG", variant="S6-P", entry_session="PREMARKET",
            client_order_id="s6buy-RIG-test", now=NOW)
        position_store.open_from_fill(conn, pid, quantity=28,
                                      average_fill_price=5.79, now=NOW)
        position_store.latch_pending_exit(conn, pid, "VWAP_FAILURE", now=NOW)
        position_store.mark_exit_submitted(conn, pid, "VWAP_FAILURE", now=NOW)
        intent_id = exit_intent_ledger.reserve(conn, pid, "VWAP_FAILURE", 28,
                                               "s6exit-RIG-test")
        exit_intent_ledger.mark_submitted(conn, intent_id, broker_order_id="0001")

        row = position_store.load(conn, pid)
        record, _d, _diag = _build(conn, row=dict(row))
        assert record["active_exit_intent_id"] == intent_id
        assert record["active_broker_order_id"] == "0001"
        assert record["exit_submitted"] is True


class TestVwapStateTransitions:
    def test_first_tick_below_vwap_is_a_breach(self, conn):
        record, _d, _diag = _build(conn)  # price 5.75 < vwap 5.80
        assert record["vwap_state"] == exit_snapshot.VWAP_BREACH
        exit_snapshot.persist(conn, record, now=NOW)

    def test_continuing_below_vwap_is_below_not_a_repeat_breach(self, conn):
        first, _d, _diag = _build(conn)
        exit_snapshot.persist(conn, first, now=NOW)
        second, _d, _diag = _build(conn, now=NOW + timedelta(minutes=1))
        assert second["vwap_state"] == exit_snapshot.VWAP_BELOW

    def test_a_move_back_above_vwap_is_recovered(self, conn):
        first, _d, _diag = _build(conn)  # BREACH
        exit_snapshot.persist(conn, first, now=NOW)
        healthy = _feats(entry_quality=_quality(), price=5.85)
        second, _d, _diag = _build(conn, feats=healthy, current_price=5.85,
                                   now=NOW + timedelta(minutes=1))
        assert second["vwap_state"] == exit_snapshot.VWAP_RECOVERED

    def test_staying_above_vwap_is_ordinary_above(self, conn):
        healthy = _feats(entry_quality=_quality(), price=5.85)
        first, _d, _diag = _build(conn, feats=healthy, current_price=5.85)
        exit_snapshot.persist(conn, first, now=NOW)
        second, _d, _diag = _build(conn, feats=healthy, current_price=5.86,
                                   now=NOW + timedelta(minutes=1))
        assert second["vwap_state"] == exit_snapshot.VWAP_ABOVE

    def test_missing_price_or_vwap_is_unknown(self, conn):
        record, _d, _diag = _build(conn, feats=_feats(entry_quality=_quality(),
                                                       vwap=None))
        assert record["vwap_state"] == exit_snapshot.VWAP_UNKNOWN


class TestLiquidityStateBuckets:
    def test_below_critical_floor(self, conn):
        record, _d, _diag = _build(
            conn, feats=_feats(entry_quality=_quality(dollar_volume_5m=17.37)))
        assert record["liquidity_state"] == exit_snapshot.LIQUIDITY_CRITICAL

    def test_between_critical_and_warning(self, conn):
        record, _d, _diag = _build(
            conn, feats=_feats(entry_quality=_quality(dollar_volume_5m=250.0)))
        assert record["liquidity_state"] == exit_snapshot.LIQUIDITY_WARNING

    def test_above_warning_floor_is_normal(self, conn):
        record, _d, _diag = _build(
            conn, feats=_feats(entry_quality=_quality(dollar_volume_5m=2794.1)))
        assert record["liquidity_state"] == exit_snapshot.LIQUIDITY_NORMAL


class TestMomentumStateMapping:
    def test_vwap_or_ema_failed_is_failed(self, conn):
        record, _d, _diag = _build(conn)  # vwap AND ema both fail here
        assert record["momentum_state"] == exit_snapshot.MOMENTUM_FAILED

    def test_healthy_reading_is_healthy(self, conn):
        healthy = _feats(entry_quality=_quality(), price=6.0, vwap=5.5,
                         ema9=5.9, ema21=5.8, volume_expansion=1.1)
        state = _state(range_high=5.0, range_low=4.5, peak_price=6.0,
                       entry_volume_expansion=1.1, peak_volume_expansion=1.1)
        record, _d, _diag = _build(conn, state=state, feats=healthy,
                                   current_price=6.0)
        assert record["momentum_state"] == exit_snapshot.MOMENTUM_HEALTHY


class TestPersistenceIsAppendOnlyAndSurvivesRestart:
    def test_multiple_ticks_for_one_position_are_never_overwritten(self, conn):
        first, _d, _diag = _build(conn, now=NOW)
        exit_snapshot.persist(conn, first, now=NOW)
        second, _d, _diag = _build(conn, now=NOW + timedelta(minutes=1))
        exit_snapshot.persist(conn, second, now=NOW + timedelta(minutes=1))
        rows = conn.execute(
            "SELECT evaluated_at FROM s6_exit_snapshots WHERE position_id = ? "
            "ORDER BY evaluated_at", (first["position_id"],)).fetchall()
        assert len(rows) == 2
        assert rows[0]["evaluated_at"] != rows[1]["evaluated_at"]

    def test_restart_persistence(self, monkeypatch):
        db_file = tempfile.mktemp(suffix=".db")
        monkeypatch.setenv("STATE_STORE_DB_FILE", db_file)
        from state_store.db import open_db

        with open_db() as c1:
            record, _d, _diag = _build(c1)
            exit_snapshot.persist(c1, record, now=NOW)

        with open_db() as c2:  # a fresh connection, as after a restart
            count = c2.execute(
                "SELECT COUNT(*) c FROM s6_exit_snapshots").fetchone()["c"]
            assert count == 1

    def test_the_full_diagnostics_record_is_preserved_verbatim(self, conn):
        record, _d, diagnostics = _build(conn)
        exit_snapshot.persist(conn, record, now=NOW)
        row = conn.execute(
            "SELECT diagnostics_json FROM s6_exit_snapshots").fetchone()
        import json
        stored = json.loads(row["diagnostics_json"])
        assert stored["conditions"] == diagnostics["conditions"]

    def test_a_persist_failure_never_raises(self, conn):
        class _BrokenConn:
            def execute(self, *a, **k):
                raise RuntimeError("disk full")

        record, _d, _diag = _build(conn)
        exit_snapshot.persist(_BrokenConn(), record, now=NOW)  # must not raise


class TestDecisionUnchanged:
    """The behavior-preservation proof: exit_policy.decide() and its
    detail are byte-for-byte identical with and without the snapshot
    call in the loop -- instrumentation reads decisions, it never makes
    or alters one."""

    @pytest.mark.parametrize("current_price,range_high,range_low,extra_feats,expect_reason", [
        (4.50, 5.76, 4.60, {}, exit_policy.REASON_HARD_RISK_CAP),
        (5.75, 5.76, 5.71, {}, exit_policy.REASON_RANGE_REENTRY),
        (5.90, 5.60, 5.50, {"vwap": 6.0}, exit_policy.REASON_VWAP_FAILURE),
        (5.90, 5.60, 5.50, {"vwap": 5.5, "ema9": 5.0, "ema21": 5.5},
         exit_policy.REASON_EMA_STRUCTURE_FAILURE),
    ])
    def test_each_hard_exit_rule_is_unchanged_by_instrumentation(
            self, conn, current_price, range_high, range_low, extra_feats, expect_reason):
        state = _state(range_high=range_high, range_low=range_low,
                       peak_price=current_price)
        base_feats = {"vwap": 5.5, "ema9": 5.9, "ema21": 5.8}
        base_feats.update(extra_feats)
        feats = _feats(entry_quality=_quality(), price=current_price, **base_feats)
        decision_before = exit_policy.decide(
            state, current_price=current_price, features=feats,
            session="PREMARKET", now=NOW)
        # Build + persist the snapshot -- exactly what the runtime now does.
        diagnostics = exit_diagnostics.evaluate(
            state, features=feats,
            price=exit_policy._price_of(feats, current_price),
            session="PREMARKET", now=NOW, decision=decision_before)
        record = exit_snapshot.build(
            conn=conn, position_id="s6pos_x", row=_row(),
            features=feats, diagnostics=diagnostics, decision=decision_before, now=NOW)
        exit_snapshot.persist(conn, record, now=NOW)
        # Re-decide from scratch: the snapshot call above must have
        # changed nothing decide() reads.
        decision_after = exit_policy.decide(
            state, current_price=current_price, features=feats,
            session="PREMARKET", now=NOW)
        assert decision_after.as_dict() == decision_before.as_dict()
        assert decision_before.reason == expect_reason

    def test_session_exit_unchanged(self, conn, monkeypatch):
        from config import s6_exit_v0 as policy

        monkeypatch.setattr(policy, "EXIT_ON_SESSION_END", True)
        monkeypatch.setattr(policy, "ALLOW_OVERNIGHT_CARRY", False)
        state = _state(range_high=1.0, range_low=0.5, peak_price=6.0)
        feats = _feats(entry_quality=_quality(), price=6.0, vwap=5.5,
                       ema9=5.9, ema21=5.8)
        session_end_now = NOW.replace(hour=15, minute=58)  # near PREMARKET close (ET-aware inside decide)
        decision = exit_policy.decide(state, current_price=6.0, features=feats,
                                      session="PREMARKET", now=session_end_now)
        diagnostics = exit_diagnostics.evaluate(
            state, features=feats, price=6.0, session="PREMARKET",
            now=session_end_now, decision=decision)
        record = exit_snapshot.build(conn=conn, position_id="s6pos_y", row=_row(),
                                     features=feats, diagnostics=diagnostics,
                                     decision=decision, now=session_end_now)
        exit_snapshot.persist(conn, record, now=session_end_now)
        decision_after = exit_policy.decide(state, current_price=6.0, features=feats,
                                            session="PREMARKET", now=session_end_now)
        assert decision_after.reason == decision.reason

    def test_no_extra_broker_call_and_no_duplicate_sell(self, conn):
        """`build`/`persist` touch only entry_quality already on `features`
        and one local SQLite read/write -- neither can submit an order."""
        import inspect

        source = inspect.getsource(exit_snapshot)
        for forbidden in ("submit_order", "submit_buy_order", "submit_sell",
                          "broker.", "_submit_sell"):
            assert forbidden not in source, forbidden


class TestWiredIntoTheRealExitRuntime:
    """Same harness as tests/test_s6_exit_runtime.py -- proves the
    snapshot call actually fires from `run_exits`/`evaluate_position`,
    for both HOLD and SELL, without changing what those tests already
    assert about broker calls and outcomes."""

    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch):
        monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
        monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))

    def _opened(self, conn, symbol="ABC", **kw):
        from s6_live import position_store as ps
        from market_hours import EASTERN

        t0 = datetime(2026, 8, 21, 12, 0, tzinfo=EASTERN)
        pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                   entry_session="REGULAR", range_high=99.5,
                                   range_low=99.0, entry_volume_expansion=2.0,
                                   now=t0, **kw)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          venue="NASD", now=t0)
        return pid, t0

    class _Features:
        def __init__(self, price=101.0, vwap=100.0, ema9=100.5, ema21=100.0,
                     volume_expansion=2.0):
            self.price, self.vwap = price, vwap
            self.ema9, self.ema21 = ema9, ema21
            self.volume_expansion = volume_expansion

    class _Adapter:
        def __init__(self):
            self.calls = []

        def submit_order(self, symbol, quantity, *, side, client_order_id=None):
            self.calls.append({"symbol": symbol, "quantity": quantity,
                               "side": side, "client_order_id": client_order_id})
            return type("R", (), {"status_code": 200, "text": "ok"})()

    def test_a_sell_tick_persists_a_snapshot_and_still_sells_exactly_once(self, conn):
        pid, t0 = self._opened(conn)
        adapter = self._Adapter()
        from s6_live import exit_runtime as er

        outcomes = er.run_exits(
            conn, broker_adapter=adapter,
            features_fn=lambda s: self._Features(price=99.4),
            price_fn=lambda s: 99.4, session="REGULAR", now=t0)
        assert len(adapter.calls) == 1  # unchanged from the pre-instrumentation test
        assert outcomes[0]["reason"] == "RANGE_REENTRY"
        rows = conn.execute(
            "SELECT * FROM s6_exit_snapshots WHERE position_id = ?", (pid,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["current_exit_reason"] == "RANGE_REENTRY"
        assert rows[0]["symbol"] == "ABC"

    def test_a_hold_tick_persists_a_snapshot_and_does_not_call_the_broker(self, conn):
        pid, t0 = self._opened(conn)
        adapter = self._Adapter()
        from s6_live import exit_runtime as er

        outcomes = er.run_exits(
            conn, broker_adapter=adapter, features_fn=lambda s: self._Features(),
            price_fn=lambda s: 101.0, session="REGULAR", now=t0)
        assert adapter.calls == []  # unchanged
        rows = conn.execute(
            "SELECT * FROM s6_exit_snapshots WHERE position_id = ?", (pid,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["current_exit_reason"] is None

    def test_consecutive_ticks_accumulate_snapshots_for_the_same_position(self, conn):
        from s6_live import exit_runtime as er
        from datetime import timedelta

        pid, t0 = self._opened(conn)
        adapter = self._Adapter()
        for i in range(3):
            er.run_exits(conn, broker_adapter=adapter,
                        features_fn=lambda s: self._Features(),
                        price_fn=lambda s: 101.0, session="REGULAR",
                        now=t0 + timedelta(minutes=i))
        rows = conn.execute(
            "SELECT evaluated_at FROM s6_exit_snapshots WHERE position_id = ? "
            "ORDER BY evaluated_at", (pid,)).fetchall()
        assert len(rows) == 3
        assert len({r["evaluated_at"] for r in rows}) == 3  # each tick distinct


class TestS1S2Unaffected:
    def test_s1_exit_runtime_does_not_import_exit_snapshot(self):
        from pathlib import Path

        text = Path("s1_live/exit_runtime.py").read_text(encoding="utf-8")
        assert "exit_snapshot" not in text

    def test_s2_exit_runtime_does_not_import_exit_snapshot(self):
        from pathlib import Path

        text = Path("s2_live/exit_runtime.py").read_text(encoding="utf-8")
        assert "exit_snapshot" not in text
