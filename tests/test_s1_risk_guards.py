"""Daily loss, drawdown, re-entry, freshness and accounting (PHASE 4A §9-§17).

The property under test everywhere: a guard that cannot MEASURE its
condition must not answer ALLOW. `execution/entry_limits.py` states the
principle for counts -- "a count of zero is the single most dangerous
wrong answer a limit checker can give" -- and the same holds for a loss
limit that cannot see today's P&L.

The second property: exits are never gated. Every guard here answers one
question only, whether a NEW entry may open.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import risk_config  # noqa: E402
from s1_live import cash_pool, freshness, qualification, reentry, risk_guards, trade_store  # noqa: E402

NOW = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ daily loss

class TestDailyLoss:
    def test_the_threshold_is_the_existing_one(self):
        """Reused, not invented."""
        assert risk_config.MAX_DAILY_LOSS_RATE == -0.02

    def test_inside_the_limit_allows(self):
        result = risk_guards.check_daily_loss(pnl_today_usd=-10.0, basis_equity_usd=1000.0)
        assert result.verdict == risk_guards.ALLOW
        assert result.measured == pytest.approx(-0.01)

    def test_at_the_limit_blocks(self):
        """At-or-beyond, not merely beyond."""
        result = risk_guards.check_daily_loss(pnl_today_usd=-20.0, basis_equity_usd=1000.0)
        assert result.verdict == risk_guards.BLOCK
        assert result.reason_code == risk_guards.REASON_DAILY_LOSS_LIMIT

    def test_beyond_the_limit_blocks(self):
        result = risk_guards.check_daily_loss(pnl_today_usd=-50.0, basis_equity_usd=1000.0)
        assert result.verdict == risk_guards.BLOCK

    def test_a_profit_allows(self):
        assert risk_guards.check_daily_loss(
            pnl_today_usd=25.0, basis_equity_usd=1000.0).verdict == risk_guards.ALLOW

    @pytest.mark.parametrize("pnl,equity", [
        (None, 1000.0), (-10.0, None), (-10.0, 0.0), (-10.0, -5.0),
        (float("nan"), 1000.0), (-10.0, float("inf")), (True, 1000.0)])
    def test_an_unmeasurable_input_blocks_rather_than_allows(self, pnl, equity):
        result = risk_guards.check_daily_loss(pnl_today_usd=pnl, basis_equity_usd=equity)
        assert result.verdict == risk_guards.UNKNOWN
        assert result.allows_entry is False
        assert result.reason_code == risk_guards.REASON_DAILY_LOSS_UNKNOWN

    def test_the_basis_includes_unrealized_by_default(self):
        """Matching the existing Alpaca equity-based convention, which is
        also the more conservative of the two choices."""
        assert (risk_guards.check_daily_loss(pnl_today_usd=-1.0, basis_equity_usd=1000.0).basis
                == risk_guards.BASIS_REALIZED_AND_UNREALIZED)


class TestDrawdown:
    def test_the_threshold_is_the_existing_one(self):
        assert risk_config.MAX_TOTAL_DRAWDOWN == -0.10

    def test_inside_allows(self):
        assert risk_guards.check_drawdown(
            equity_usd=950.0, peak_equity_usd=1000.0).verdict == risk_guards.ALLOW

    def test_at_the_limit_blocks(self):
        result = risk_guards.check_drawdown(equity_usd=900.0, peak_equity_usd=1000.0)
        assert result.verdict == risk_guards.BLOCK
        assert result.reason_code == risk_guards.REASON_DRAWDOWN_LIMIT

    @pytest.mark.parametrize("equity,peak", [
        (None, 1000.0), (900.0, None), (900.0, 0.0), (float("nan"), 1000.0)])
    def test_no_peak_means_unknown_means_block(self, equity, peak):
        """This project records no live high-water mark; the guard says so
        rather than inventing one."""
        result = risk_guards.check_drawdown(equity_usd=equity, peak_equity_usd=peak)
        assert result.verdict == risk_guards.UNKNOWN
        assert result.allows_entry is False


class TestConsecutiveLosses:
    def test_unconfigured_counts_but_does_not_block(self):
        """No validated limit exists, so none is invented -- but the
        count is still recorded so one can be chosen from real data."""
        result = risk_guards.check_consecutive_losses(consecutive_losses=99)
        assert result.verdict == risk_guards.ALLOW
        assert result.reason_code == risk_guards.REASON_CONSECUTIVE_LOSS_UNCONFIGURED
        assert result.measured == 99

    def test_a_configured_limit_blocks_at_or_above(self):
        assert risk_guards.check_consecutive_losses(
            consecutive_losses=3, limit=3).verdict == risk_guards.BLOCK
        assert risk_guards.check_consecutive_losses(
            consecutive_losses=2, limit=3).verdict == risk_guards.ALLOW


class TestEvaluateAll:
    def test_all_must_allow(self):
        allowed, results = risk_guards.evaluate_all(
            pnl_today_usd=-1.0, basis_equity_usd=1000.0,
            equity_usd=999.0, peak_equity_usd=1000.0)
        assert allowed is True
        assert len(results) == 3

    def test_one_unknown_blocks_the_whole_set(self):
        allowed, _ = risk_guards.evaluate_all(
            pnl_today_usd=-1.0, basis_equity_usd=1000.0,
            equity_usd=999.0, peak_equity_usd=None)
        assert allowed is False

    def test_nothing_supplied_blocks(self):
        """The current KIS reality: no equity, no peak -> no entries."""
        allowed, results = risk_guards.evaluate_all()
        assert allowed is False
        assert [r.verdict for r in results[:2]] == [risk_guards.UNKNOWN, risk_guards.UNKNOWN]


# ------------------------------------------------------------------ re-entry

def state(**kw):
    base = dict(symbol="AAPL", known=True, used_signal_ids=frozenset())
    base.update(kw)
    return reentry.SymbolState(**base)


class TestReentry:
    def test_a_clean_symbol_allows(self):
        assert reentry.check(state=state(), source_signal_id="sig1",
                             source_signal_timestamp=NOW, now=NOW).allows_entry is True

    def test_already_held_blocks(self):
        result = reentry.check(state=state(currently_held=True), source_signal_id="sig1",
                               source_signal_timestamp=NOW, now=NOW)
        assert result.reason_code == reentry.REASON_ALREADY_HELD

    def test_open_order_blocks(self):
        result = reentry.check(state=state(has_open_order=True), source_signal_id="sig1",
                               source_signal_timestamp=NOW, now=NOW)
        assert result.reason_code == reentry.REASON_OPEN_ORDER

    def test_a_reused_signal_id_blocks(self):
        result = reentry.check(state=state(used_signal_ids=frozenset({"sig1"})),
                               source_signal_id="sig1",
                               source_signal_timestamp=NOW, now=NOW)
        assert result.reason_code == reentry.REASON_DUPLICATE_SIGNAL

    def test_a_signal_older_than_the_last_exit_blocks(self):
        """The load-bearing one: it makes a zero cooldown survivable."""
        result = reentry.check(
            state=state(last_exit_at=NOW, last_exit_reason="STOP_LOSS"),
            source_signal_id="sig1",
            source_signal_timestamp=NOW - timedelta(hours=5), now=NOW)
        assert result.reason_code == reentry.REASON_SIGNAL_PREDATES_EXIT

    def test_a_signal_equal_to_the_exit_time_blocks(self):
        result = reentry.check(state=state(last_exit_at=NOW), source_signal_id="sig1",
                               source_signal_timestamp=NOW, now=NOW)
        assert result.reason_code == reentry.REASON_SIGNAL_PREDATES_EXIT

    def test_a_newer_signal_after_an_exit_allows(self):
        assert reentry.check(
            state=state(last_exit_at=NOW - timedelta(days=1)), source_signal_id="sig2",
            source_signal_timestamp=NOW, now=NOW).allows_entry is True

    def test_unknown_state_blocks(self):
        result = reentry.check(state=state(known=False), source_signal_id="sig1",
                               source_signal_timestamp=NOW, now=NOW)
        assert result.reason_code == reentry.REASON_STATE_UNKNOWN

    def test_no_signal_id_blocks(self):
        assert reentry.check(state=state(), source_signal_id="",
                             source_signal_timestamp=NOW, now=NOW).allows_entry is False

    def test_cooldown_is_not_applied_when_unset(self):
        """No validated duration exists, so none is invented."""
        assert reentry.check(
            state=state(last_exit_at=NOW - timedelta(seconds=1)),
            source_signal_id="s", source_signal_timestamp=NOW, now=NOW,
            cooldown_seconds=None).allows_entry is True

    def test_cooldown_blocks_when_configured(self):
        result = reentry.check(
            state=state(last_exit_at=NOW - timedelta(seconds=60)),
            source_signal_id="s", source_signal_timestamp=NOW, now=NOW,
            cooldown_seconds=3600)
        assert result.reason_code == reentry.REASON_COOLDOWN

    @pytest.mark.parametrize("stamp", [
        NOW, NOW.isoformat(), NOW.isoformat().replace("+00:00", "Z"),
        "2026-08-17T14:00:00"])
    def test_an_iso_string_timestamp_is_understood(self, stamp):
        """Regression: candidate rows come off a CSV, so their timestamps
        are strings. This guard once accepted only datetime and therefore
        rejected every real candidate for having no usable timestamp."""
        assert reentry.check(state=state(), source_signal_id="sig1",
                             source_signal_timestamp=stamp, now=NOW).allows_entry is True

    def test_an_unparseable_timestamp_still_blocks(self):
        assert reentry.check(state=state(), source_signal_id="sig1",
                             source_signal_timestamp="not-a-time",
                             now=NOW).allows_entry is False

    def test_per_symbol_daily_cap_blocks_when_configured(self):
        result = reentry.check(state=state(entries_today=2), source_signal_id="s",
                               source_signal_timestamp=NOW, now=NOW,
                               max_entries_per_symbol_per_day=2)
        assert result.reason_code == reentry.REASON_DAILY_SYMBOL_LIMIT


# ------------------------------------------------------------------ freshness

class TestFreshness:
    def test_the_right_day_allows(self):
        assert freshness.check(signal_timestamp=NOW, signal_trading_day="2026-08-17",
                               expected_trading_day="2026-08-17", now=NOW).allows_entry is True

    def test_another_trading_day_is_rejected(self):
        result = freshness.check(signal_timestamp=NOW, signal_trading_day="2026-08-14",
                                 expected_trading_day="2026-08-17", now=NOW)
        assert result.reason_code == freshness.REASON_WRONG_TRADING_DAY

    def test_a_future_signal_is_rejected(self):
        result = freshness.check(signal_timestamp=NOW + timedelta(hours=1),
                                 signal_trading_day="2026-08-17",
                                 expected_trading_day="2026-08-17", now=NOW)
        assert result.reason_code == freshness.REASON_FUTURE_SIGNAL

    def test_age_is_measured_but_not_enforced_by_default(self):
        """The max age is tied to the unresolved S1 exit horizon."""
        result = freshness.check(signal_timestamp=NOW - timedelta(hours=6),
                                 signal_trading_day="2026-08-17",
                                 expected_trading_day="2026-08-17", now=NOW)
        assert result.allows_entry is True
        assert result.age_seconds == pytest.approx(21600.0)

    def test_a_configured_max_age_is_enforced(self):
        result = freshness.check(signal_timestamp=NOW - timedelta(hours=6),
                                 signal_trading_day="2026-08-17",
                                 expected_trading_day="2026-08-17", now=NOW,
                                 max_age_seconds=60)
        assert result.reason_code == freshness.REASON_TOO_OLD


class TestExtension:
    def test_extension_is_computed(self):
        assert freshness.extension_pct(100.0, 110.0) == pytest.approx(10.0)
        assert freshness.extension_pct(100.0, 95.0) == pytest.approx(-5.0)

    @pytest.mark.parametrize("signal,current", [
        (None, 100.0), (100.0, None), (0.0, 100.0), (-5.0, 100.0),
        (float("nan"), 100.0), (True, 100.0)])
    def test_unusable_input_is_none_not_zero(self, signal, current):
        assert freshness.extension_pct(signal, current) is None

    def test_it_is_recorded_not_enforced(self):
        """candidate_decision's 25% is a different measurement and is not
        borrowed; candidate_decision also stays disabled."""
        observation = freshness.measure_extension(100.0, 180.0)
        assert observation["extension_pct"] == pytest.approx(80.0)
        assert observation["extension_threshold_pct"] is None
        assert observation["extension_policy"] == freshness.EXTENSION_UNENFORCED

    def test_the_module_does_not_import_candidate_decision(self):
        import ast

        source = (REPO_ROOT / "s1_live" / "freshness.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                text = getattr(node, "module", "") or ""
                assert "candidate_decision" not in text


# ------------------------------------------------------------------ cash pool

class FakeSnapshot:
    def __init__(self, orderable=None, cash_source="TTTS3012R_DOES_NOT_PROVIDE"):
        self.usd_orderable_cash = orderable
        self.cash_source = cash_source


class TestCashPool:
    def test_a_reported_figure_is_used(self):
        pool = cash_pool.establish(account_snapshot=FakeSnapshot(orderable=500.0))
        assert pool.available is True
        assert pool.require() == 500.0
        assert pool.source == cash_pool.SOURCE_SNAPSHOT

    def test_no_figure_and_no_probe_is_unavailable_not_zero(self):
        """KIS's balance response carries no cash; that is not $0."""
        pool = cash_pool.establish(account_snapshot=FakeSnapshot())
        assert pool.status == cash_pool.UNAVAILABLE
        assert pool.amount_usd is None
        with pytest.raises(cash_pool.CashPoolUnavailable):
            pool.require()

    def test_a_probe_can_establish_it_and_is_labelled(self):
        class Broker:
            def get_orderable_usd(self, instrument, price):
                return 480.0

        pool = cash_pool.establish(account_snapshot=FakeSnapshot(), broker=Broker(),
                                   probe_instrument="AAPL", probe_price_usd=100.0)
        assert pool.available is True
        assert pool.source == "probe:AAPL", "inferred pools are labelled, not laundered"

    def test_a_failing_probe_is_unavailable_not_zero(self):
        class Broker:
            def get_orderable_usd(self, instrument, price):
                raise RuntimeError("KIS is down")

        pool = cash_pool.establish(account_snapshot=FakeSnapshot(), broker=Broker(),
                                   probe_instrument="AAPL", probe_price_usd=100.0)
        assert pool.status == cash_pool.UNAVAILABLE
        assert pool.reason_code == cash_pool.REASON_PROBE_FAILED

    def test_a_real_zero_balance_is_available_and_zero(self):
        pool = cash_pool.establish(account_snapshot=FakeSnapshot(orderable=0.0))
        assert pool.available is True and pool.require() == 0.0


# ------------------------------------------------------------------ accounting

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    from state_store import db as state_db

    connection = state_db.open_db()
    yield connection
    connection.close()


def open_one(connection, **kw):
    base = dict(source_signal_id="sig-1", scanner_run_id="run-1",
                trading_day="2026-08-17", allocation_version="s1_alloc_v1",
                scanner_score=88.0, candidate_rank=1, allocated_cash=350.0,
                account_cash_before=1000.0)
    base.update(kw)
    return trade_store.open_trade(connection, **base)


class TestTradeAccounting:
    def test_the_table_exists_at_the_current_schema_version(self, conn):
        from state_store.migrations import CURRENT_SCHEMA_VERSION
        from state_store.schema import ALL_TABLES

        assert CURRENT_SCHEMA_VERSION == 11
        assert "s1_live_trades" in ALL_TABLES
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "s1_live_trades" in names

    def test_a_trade_records_its_scanner_provenance(self, conn):
        trade_id = open_one(conn)
        row = trade_store.read_trade(conn, trade_id)
        assert row["source_signal_id"] == "sig-1"
        assert row["scanner_run_id"] == "run-1"
        assert row["allocation_version"] == "s1_alloc_v1"
        assert row["fees_status"] == trade_store.FEES_UNKNOWN

    def test_one_signal_may_produce_at_most_one_trade(self, conn):
        open_one(conn)
        with pytest.raises(trade_store.TradeStoreError):
            open_one(conn, trade_id="different")

    def test_broker_values_overwrite_local_ones(self, conn):
        trade_id = open_one(conn)
        trade_store.apply_broker_fill(conn, trade_id, side="entry",
                                      broker_order_id="KIS-99", price=101.5, quantity=3,
                                      filled_at="2026-08-17T13:31:00+00:00")
        row = trade_store.read_trade(conn, trade_id)
        assert row["broker_order_id"] == "KIS-99"
        assert row["entry_price"] == 101.5
        assert row["qty"] == 3

    def test_net_pnl_stays_null_while_any_fee_is_unknown(self, conn):
        """fee=0 would turn a gross figure into something labelled net."""
        trade_id = open_one(conn)
        trade_store.apply_broker_fill(conn, trade_id, side="entry", price=100.0, quantity=10)
        trade_store.apply_broker_fill(conn, trade_id, side="exit", price=110.0)
        state = trade_store.record_fees(conn, trade_id, commission=1.0)
        assert state["fees_status"] == trade_store.FEES_PARTIAL
        assert state["gross_pnl"] == pytest.approx(100.0)
        assert state["net_pnl"] is None
        assert set(state["unknown_components"]) == {"regulatory_fees", "fx_cost"}
        assert trade_store.read_trade(conn, trade_id)["net_pnl"] is None

    def test_net_pnl_appears_only_when_every_component_is_reported(self, conn):
        trade_id = open_one(conn)
        trade_store.apply_broker_fill(conn, trade_id, side="entry", price=100.0, quantity=10)
        trade_store.apply_broker_fill(conn, trade_id, side="exit", price=110.0)
        state = trade_store.record_fees(conn, trade_id, commission=1.0,
                                        regulatory_fees=0.25, fx_cost=2.0,
                                        source="kis_execution_report")
        assert state["fees_status"] == trade_store.FEES_REPORTED
        assert state["fees_total"] == pytest.approx(3.25)
        assert state["net_pnl"] == pytest.approx(96.75)

    def test_a_reported_zero_fee_is_accepted(self, conn):
        """Zero is a legitimate REPORTED value; only absence is unknown."""
        trade_id = open_one(conn)
        trade_store.apply_broker_fill(conn, trade_id, side="entry", price=100.0, quantity=1)
        trade_store.apply_broker_fill(conn, trade_id, side="exit", price=105.0)
        state = trade_store.record_fees(conn, trade_id, commission=0.0,
                                        regulatory_fees=0.0, fx_cost=0.0)
        assert state["fees_status"] == trade_store.FEES_REPORTED
        assert state["net_pnl"] == pytest.approx(5.0)

    def test_the_day_summary_withholds_net_while_fees_are_unknown(self, conn):
        trade_id = open_one(conn)
        trade_store.apply_broker_fill(conn, trade_id, side="entry", price=100.0, quantity=1)
        trade_store.apply_broker_fill(conn, trade_id, side="exit", price=110.0)
        trade_store.record_fees(conn, trade_id, commission=1.0)
        summary = trade_store.day_summary(conn, "2026-08-17")
        assert summary["gross_pnl_usd"] == pytest.approx(10.0)
        assert summary["net_pnl_usd"] is None
        assert summary["fees_unknown_trades"] == 1
        assert "unverified fees" in summary["net_pnl_blocked_reason"]

    def test_no_fee_constant_is_hardcoded_in_the_store(self):
        """§16: no assumed commission rate anywhere in this module."""
        source = (REPO_ROOT / "s1_live" / "trade_store.py").read_text(encoding="utf-8")
        for forbidden in ("0.0025", "0.25%", "COMMISSION_RATE", "DEFAULT_COMMISSION"):
            assert forbidden not in source


# ------------------------------------------------------------------ qualification

class TestQualificationIsSourceSpecific:
    def test_legacy_still_applies_the_score_threshold(self):
        low = qualification.qualify_legacy(
            "AAPL", analyze=lambda s: {"price": 100.0, "score": 50}, score_threshold=70)
        assert low.qualified is False
        assert low.reason_code == qualification.REASON_BELOW_SCORE_THRESHOLD

        high = qualification.qualify_legacy(
            "AAPL", analyze=lambda s: {"price": 100.0, "score": 90}, score_threshold=70)
        assert high.qualified is True
        assert high.strategy_id == qualification.LEGACY_STRATEGY_ID

    def test_s1_does_not_consult_the_legacy_scorer_at_all(self):
        """A candidate whose S1 score is below the legacy threshold still
        qualifies -- the legacy model is not S1's strategy."""
        row = {"symbol": "NVDA", "signal_price": 100.0, "scanner_score": 41.0,
               "signal_id": "sig-1"}
        result = qualification.qualify_s1("NVDA", candidate_row=row)
        assert result.qualified is True
        assert result.score == 41.0
        assert result.strategy_id == qualification.S1_STRATEGY_ID
        assert result.strategy_id != qualification.LEGACY_STRATEGY_ID

    def test_a_symbol_not_in_the_candidate_set_does_not_qualify(self):
        result = qualification.qualify_s1("GHOST", candidate_row=None)
        assert result.qualified is False
        assert result.reason_code == qualification.REASON_NOT_AN_S1_CANDIDATE

    def test_an_unusable_row_does_not_qualify(self):
        for row in ({"symbol": "X", "signal_price": 0.0, "signal_id": "s"},
                    {"symbol": "X", "signal_price": 10.0, "signal_id": ""}):
            assert qualification.qualify_s1("X", candidate_row=row).qualified is False

    def test_the_module_never_imports_paper_strategy_order(self):
        """PHASE 3's module-identity hazard: `analyze` is injected."""
        import ast

        source = (REPO_ROOT / "s1_live" / "qualification.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "paper_strategy_order" not in alias.name
            elif isinstance(node, ast.ImportFrom):
                assert "paper_strategy_order" not in (node.module or "")
