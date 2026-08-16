"""Account risk state: equity, start-of-day, peak, and restart safety (PHASE 4B).

No test here calls a real broker. Every account fact is a fixture.

The property that matters most: a restart must not reset the risk. A
process that came back at 14:00 and treated the post-loss equity as the
day's starting point would hand itself a fresh loss budget at exactly
the moment something had already gone wrong.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import risk_config  # noqa: E402
from s1_live import equity, risk_state  # noqa: E402

DAY = "2026-08-17"
NEXT_DAY = "2026-08-18"
# 08:00 ET on a weekday -> premarket, so a first capture is accepted.
PRE_OPEN = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
# 14:00 ET -> regular session.
MIDDAY = datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    from state_store import db

    connection = db.open_db()
    yield connection
    connection.close()


def snap(amount, *, now=PRE_OPEN):
    return equity.from_amount(amount, now=now, source="fixture")


class FakePosition:
    def __init__(self, quantity, average_fill_price, unrealized_pnl):
        self.quantity = quantity
        self.average_fill_price = average_fill_price
        self.unrealized_pnl = unrealized_pnl


class FakeAccount:
    def __init__(self, usd_cash=None, krw_cash=None, cash_source="TTTS3012R_DOES_NOT_PROVIDE"):
        self.usd_cash = usd_cash
        self.krw_cash = krw_cash
        self.cash_source = cash_source


# ------------------------------------------------------------------ equity

class TestEquityDefinition:
    def test_equity_is_cash_plus_position_value(self):
        result = equity.from_account(
            FakeAccount(usd_cash=200.0),
            [FakePosition(10, 30.0, 20.0)], now=PRE_OPEN)
        assert result.available is True
        assert result.position_value_usd == pytest.approx(320.0)
        assert result.require() == pytest.approx(520.0)
        assert result.currency == "USD"

    def test_no_cash_means_equity_is_unavailable_not_position_value(self):
        """The half this broker does not report. Position value is shown
        so the gap is visible, but it is never returned AS equity."""
        result = equity.from_account(
            FakeAccount(usd_cash=None), [FakePosition(10, 30.0, 20.0)], now=PRE_OPEN)
        assert result.available is False
        assert result.reason_code == equity.REASON_NO_ACCOUNT_CASH
        assert result.equity_usd is None
        assert result.position_value_usd == pytest.approx(320.0)
        assert "balance_cash_fields_absent" in result.detail
        with pytest.raises(equity.EquityUnavailable):
            result.require()

    def test_an_unusable_position_row_makes_the_total_unknown(self):
        """Treating it as zero would understate equity and therefore
        overstate the drawdown."""
        result = equity.from_account(
            FakeAccount(usd_cash=200.0),
            [FakePosition(10, 30.0, None)], now=PRE_OPEN)
        assert result.available is False
        assert result.reason_code == equity.REASON_NO_POSITION_VALUE

    def test_no_positions_is_a_real_zero(self):
        result = equity.from_account(FakeAccount(usd_cash=200.0), [], now=PRE_OPEN)
        assert result.require() == pytest.approx(200.0)
        assert result.position_value_usd == 0.0

    def test_mixed_currency_is_refused_not_converted(self):
        result = equity.from_account(
            FakeAccount(usd_cash=100.0, krw_cash=50000.0), [], now=PRE_OPEN)
        assert result.available is False
        assert result.reason_code == equity.REASON_CURRENCY_MIXED

    def test_no_fx_rate_is_read_or_assumed(self):
        source = (REPO_ROOT / "s1_live" / "equity.py").read_text(encoding="utf-8")
        for forbidden in ("1300", "1350", "FX_RATE", "fx_rate_krw"):
            assert forbidden not in source

    def test_a_failed_broker_read_is_unavailable(self):
        class Broker:
            def get_account_snapshot(self):
                raise RuntimeError("KIS down")

            def get_positions(self):
                return []

        result = equity.read(Broker(), now=PRE_OPEN)
        assert result.available is False
        assert result.reason_code == equity.REASON_READ_FAILED

    @pytest.mark.parametrize("value", [None, -1.0, float("nan"), float("inf"), True, "500"])
    def test_an_unusable_amount_is_refused(self, value):
        assert equity.from_amount(value, now=PRE_OPEN).available is False


# ------------------------------------------------------- start of day

class TestStartOfDayEquity:
    def test_a_pre_open_capture_is_accepted(self, conn):
        result = risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        assert result["captured"] is True
        assert risk_state.read_day(conn, DAY)["start_equity"] == 1000.0

    def test_it_is_never_overwritten_within_the_day(self, conn):
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        again = risk_state.capture_start_of_day(
            conn, DAY, snap(800.0, now=MIDDAY), now=MIDDAY)
        assert again["captured"] is False
        assert risk_state.read_day(conn, DAY)["start_equity"] == 1000.0

    def test_a_restart_mid_day_reuses_the_stored_start(self, conn):
        """The load-bearing restart test: a 14:00 restart must not hand
        itself a fresh loss budget."""
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        state = risk_state.refresh(conn, DAY, snap(980.0, now=MIDDAY), now=MIDDAY)
        assert state.start_equity == 1000.0
        assert state.daily_return_pct == pytest.approx(-0.02)
        assert state.daily_loss_status == risk_state.BLOCK

    def test_a_first_capture_during_the_session_is_refused(self, conn):
        """§5: no basis exists to reconstruct the real 09:30 figure."""
        result = risk_state.capture_start_of_day(
            conn, DAY, snap(800.0, now=MIDDAY), now=MIDDAY)
        assert result["captured"] is False
        assert result["reason"] == risk_state.REASON_LATE_FIRST_CAPTURE
        assert risk_state.read_day(conn, DAY)["start_equity"] is None

    def test_a_missing_start_blocks_entries(self, conn):
        state = risk_state.refresh(conn, DAY, snap(800.0, now=MIDDAY), now=MIDDAY)
        assert state.start_equity is None
        assert state.daily_loss_status == risk_state.UNKNOWN
        assert state.entries_allowed is False

    def test_an_unavailable_snapshot_captures_nothing(self, conn):
        unavailable = equity.from_account(FakeAccount(), [], now=PRE_OPEN)
        result = risk_state.capture_start_of_day(conn, DAY, unavailable, now=PRE_OPEN)
        assert result["captured"] is False
        assert risk_state.read_day(conn, DAY)["start_equity"] is None

    def test_the_next_trading_day_gets_its_own_start(self, conn):
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        next_open = PRE_OPEN + timedelta(days=1)
        risk_state.capture_start_of_day(conn, NEXT_DAY, snap(1100.0, now=next_open),
                                        now=next_open)
        assert risk_state.read_day(conn, DAY)["start_equity"] == 1000.0
        assert risk_state.read_day(conn, NEXT_DAY)["start_equity"] == 1100.0


# ------------------------------------------------------------- peak

class TestPeakEquity:
    def test_the_first_reading_sets_the_peak(self, conn):
        risk_state.record_peak(conn, 1000.0, now=PRE_OPEN)
        assert risk_state.read_peak(conn)["peak_equity"] == 1000.0

    def test_a_higher_equity_raises_it(self, conn):
        risk_state.record_peak(conn, 1000.0, now=PRE_OPEN)
        risk_state.record_peak(conn, 1100.0, now=MIDDAY)
        assert risk_state.read_peak(conn)["peak_equity"] == 1100.0

    def test_a_lower_equity_leaves_it_alone(self, conn):
        """A peak that fell with equity would make every drawdown zero."""
        risk_state.record_peak(conn, 1000.0, now=PRE_OPEN)
        risk_state.record_peak(conn, 700.0, now=MIDDAY)
        assert risk_state.read_peak(conn)["peak_equity"] == 1000.0

    def test_it_survives_across_trading_days(self, conn):
        risk_state.record_peak(conn, 1000.0, now=PRE_OPEN)
        risk_state.refresh(conn, NEXT_DAY, snap(950.0, now=PRE_OPEN + timedelta(days=1)),
                           now=PRE_OPEN + timedelta(days=1))
        assert risk_state.read_peak(conn)["peak_equity"] == 1000.0

    def test_a_large_jump_is_flagged_as_a_possible_deposit(self, conn):
        """This system cannot see external cash flow; it says so."""
        risk_state.record_peak(conn, 1000.0, now=PRE_OPEN)
        risk_state.record_peak(conn, 2000.0, now=MIDDAY)
        peak = risk_state.read_peak(conn)
        assert peak["peak_equity"] == 2000.0
        assert peak["external_flow_suspected"] == 1

    def test_an_ordinary_gain_is_not_flagged(self, conn):
        risk_state.record_peak(conn, 1000.0, now=PRE_OPEN)
        risk_state.record_peak(conn, 1050.0, now=MIDDAY)
        assert risk_state.read_peak(conn)["external_flow_suspected"] == 0

    def test_the_day_start_seeds_the_peak(self, conn):
        """Otherwise the first peak is whatever the first refresh saw --
        and a refresh landing mid-drawdown would seed the peak below the
        real high, making the drawdown read as ~0."""
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        assert risk_state.read_peak(conn)["peak_equity"] == 1000.0
        state = risk_state.refresh(conn, DAY, snap(900.0, now=MIDDAY), now=MIDDAY)
        assert state.peak_equity == 1000.0
        assert state.drawdown_pct == pytest.approx(-0.10)
        assert state.drawdown_status == risk_state.BLOCK

    def test_a_missing_peak_blocks(self, conn):
        state = risk_state.evaluate(conn, DAY, now=MIDDAY)
        assert state.peak_equity is None
        assert state.drawdown_status == risk_state.UNKNOWN
        assert state.entries_allowed is False


# ------------------------------------------------------- enforcement

def prepared(conn, start, current, peak=None, *, now=MIDDAY):
    risk_state.capture_start_of_day(conn, DAY, snap(start), now=PRE_OPEN)
    risk_state.record_peak(conn, peak if peak is not None else start, now=PRE_OPEN)
    return risk_state.refresh(conn, DAY, snap(current, now=now), now=now)


class TestDailyLossEnforcement:
    def test_the_threshold_is_the_existing_one(self):
        assert risk_config.MAX_DAILY_LOSS_RATE == -0.02

    def test_minus_one_ninety_nine_allows(self, conn):
        state = prepared(conn, 1000.0, 980.1)
        assert state.daily_return_pct == pytest.approx(-0.0199)
        assert state.daily_loss_status == risk_state.ALLOW

    def test_exactly_minus_two_blocks(self, conn):
        state = prepared(conn, 1000.0, 980.0)
        assert state.daily_loss_status == risk_state.BLOCK
        assert state.entries_allowed is False

    def test_below_minus_two_blocks(self, conn):
        assert prepared(conn, 1000.0, 900.0).daily_loss_status == risk_state.BLOCK

    def test_a_profitable_day_allows(self, conn):
        assert prepared(conn, 1000.0, 1050.0).daily_loss_status == risk_state.ALLOW


class TestDrawdownEnforcement:
    def test_the_threshold_is_the_existing_one(self):
        assert risk_config.MAX_TOTAL_DRAWDOWN == -0.10

    def test_minus_nine_ninety_nine_allows(self, conn):
        state = prepared(conn, 1000.0, 900.1, peak=1000.0)
        assert state.drawdown_pct == pytest.approx(-0.0999)
        assert state.drawdown_status == risk_state.ALLOW

    def test_exactly_minus_ten_blocks(self, conn):
        state = prepared(conn, 1000.0, 900.0, peak=1000.0)
        assert state.drawdown_pct == pytest.approx(-0.10)
        assert state.drawdown_status == risk_state.BLOCK

    def test_below_minus_ten_blocks(self, conn):
        assert prepared(conn, 1000.0, 800.0, peak=1000.0).drawdown_status == risk_state.BLOCK

    def test_drawdown_is_measured_against_the_peak_not_the_day_start(self, conn):
        """Yesterday's high still counts against today."""
        risk_state.record_peak(conn, 2000.0, now=PRE_OPEN)
        state = prepared(conn, 1000.0, 1000.0, peak=2000.0)
        assert state.drawdown_pct == pytest.approx(-0.50)
        assert state.drawdown_status == risk_state.BLOCK


class TestFailClosed:
    def test_unavailable_equity_blocks_both(self, conn):
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        unavailable = equity.from_account(FakeAccount(), [], now=MIDDAY)
        state = risk_state.refresh(conn, DAY, unavailable, now=MIDDAY)
        assert state.daily_loss_status == risk_state.UNKNOWN
        assert state.drawdown_status == risk_state.UNKNOWN
        assert state.entries_allowed is False

    def test_a_stale_snapshot_blocks(self, conn):
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        old = snap(990.0, now=MIDDAY - timedelta(hours=2))
        state = risk_state.refresh(conn, DAY, old, now=MIDDAY, max_age_seconds=300)
        assert state.entries_allowed is False
        assert "stale" in state.status_detail or "old" in state.status_detail

    def test_a_fresh_snapshot_within_the_window_is_accepted(self, conn):
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        recent = snap(999.0, now=MIDDAY - timedelta(seconds=30))
        state = risk_state.refresh(conn, DAY, recent, now=MIDDAY, max_age_seconds=300)
        assert state.entries_allowed is True

    def test_a_malformed_balance_blocks(self, conn):
        class Broker:
            def get_account_snapshot(self):
                return FakeAccount(usd_cash="not-a-number")

            def get_positions(self):
                return []

        state = risk_state.refresh(conn, DAY, equity.read(Broker(), now=MIDDAY), now=MIDDAY)
        assert state.entries_allowed is False

    def test_nothing_recorded_at_all_blocks(self, conn):
        assert risk_state.current_state(conn, DAY).entries_allowed is False


class TestRestartDoesNotResetRisk:
    def test_state_survives_a_new_connection(self, conn, tmp_path, monkeypatch):
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        risk_state.refresh(conn, DAY, snap(900.0, now=MIDDAY), now=MIDDAY)
        assert risk_state.current_state(conn, DAY).daily_loss_status == risk_state.BLOCK
        conn.commit()
        conn.close()

        from state_store import db

        reopened = db.open_db()
        try:
            after = risk_state.current_state(reopened, DAY)
            assert after.start_equity == 1000.0
            assert after.daily_loss_status == risk_state.BLOCK
            assert after.entries_allowed is False, "a restart must not clear the block"
            assert after.peak_equity == 1000.0
        finally:
            reopened.close()

    def test_a_restart_cannot_raise_the_loss_budget(self, conn):
        """Re-refreshing after a restart re-derives the SAME verdict from
        the stored start, not from the current equity."""
        risk_state.capture_start_of_day(conn, DAY, snap(1000.0), now=PRE_OPEN)
        risk_state.refresh(conn, DAY, snap(950.0, now=MIDDAY), now=MIDDAY)
        later = MIDDAY + timedelta(hours=1)
        state = risk_state.refresh(conn, DAY, snap(950.0, now=later), now=later)
        assert state.start_equity == 1000.0
        assert state.daily_return_pct == pytest.approx(-0.05)
        assert state.daily_loss_status == risk_state.BLOCK


class TestSchema:
    def test_the_tables_exist_at_version_twelve(self, conn):
        from state_store.migrations import CURRENT_SCHEMA_VERSION
        from state_store.schema import ALL_TABLES

        assert CURRENT_SCHEMA_VERSION == 12
        assert "s1_risk_state" in ALL_TABLES and "s1_risk_peak" in ALL_TABLES
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"s1_risk_state", "s1_risk_peak"} <= names

    def test_the_peak_table_holds_one_row(self, conn):
        risk_state.record_peak(conn, 1000.0, now=PRE_OPEN)
        risk_state.record_peak(conn, 1100.0, now=MIDDAY)
        assert conn.execute("SELECT COUNT(*) FROM s1_risk_peak").fetchone()[0] == 1


class TestNoBrokerCalls:
    def test_the_modules_do_not_import_an_order_path(self):
        import ast

        forbidden = {"execution", "kis_live_trading", "live_pilot", "brokers"}
        for name in ("equity.py", "risk_state.py"):
            path = REPO_ROOT / "s1_live" / name
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name.split(".")[0] not in forbidden, name
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] not in forbidden, name
