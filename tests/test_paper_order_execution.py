import multiprocessing
import threading
from datetime import datetime, timezone

import pandas as pd
import pytest
import requests

import order_safety
import paper_strategy_order as pso
from broker import AlpacaBroker, BrokerConfig
from live_readiness.order_gateway import LiveEntryContext


@pytest.fixture(autouse=True)
def _isolate_live_entry_state_db(tmp_path, monkeypatch):
    # CODEX-031: the live-entry gate now reads/writes an authoritative
    # SQLite ledger (live_readiness/entry_reservation_ledger.py) -- tests
    # that exercise it via _live_entry_context() below must isolate that
    # database exactly like every other SQLite-touching test file, or
    # they silently accumulate real reservations in the repo-root
    # TRADING_STATE.db.
    from live_readiness import entry_reservation_ledger as live_ledger
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setattr(live_ledger, "_LOCK_FILE", tmp_path / "LIVE_ENTRY_RESERVATION.lock")
    yield


def _live_entry_context(symbol="AAPL"):
    """CODEX-026/029: see tests/test_broker_safety.py's identical helper --
    AlpacaBroker.submit_order() now requires a valid LiveEntryContext for
    any side="buy" call on a live-mode config, before the pre-existing
    dry-run/hard-disable checks below even run."""
    now = datetime.now(timezone.utc)
    return LiveEntryContext(
        symbol=symbol, expected_fill_price_usd=10.0, allow_list=[symbol],
        available_cash_krw=30_000, cash_usage_percent=100, cash_as_of=now.isoformat(),
        fx_rate_krw_per_usd=1_350.0, fx_rate_as_of=now.isoformat(),
        max_order_notional_krw=30_000, max_daily_loss_krw=10_000, max_position_count=1,
        max_daily_entries=2, now=now,
    )


def _mp_update_reconciliation(reconciliation_path, lock_path, client_order_id, symbol, order_date, incoming, barrier):
    """Module-level so it's picklable for multiprocessing.Process (spawn).

    Runs in a fresh child interpreter: re-imports paper_strategy_order and
    points its file/lock constants at the test's tmp_path directly (no
    monkeypatch, which doesn't cross process boundaries), then performs one
    locked reconciliation update, synchronized against the sibling process
    via a shared Barrier so both actually contend for the lock.
    """
    import paper_strategy_order as pso_child

    pso_child.ORDER_RECONCILIATION_FILE = reconciliation_path
    pso_child.ORDER_RECONCILIATION_LOCK_FILE = lock_path
    barrier.wait(timeout=10)
    pso_child._update_reconciliation_row(client_order_id, symbol, order_date, incoming)


TODAY = pso.eastern_now().strftime("%Y-%m-%d")


class DummySession:
    """Stands in for requests.Session; never performs real network I/O."""

    def __init__(self):
        self.posts = []

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        raise AssertionError("Network order should not be submitted")


class FakeBrokerResponse:
    def __init__(self, status_code=200, text="OK", dry_run=False, data=None):
        self.status_code = status_code
        self.text = text
        self.dry_run = dry_run
        self.data = data


class FakeConfig:
    status_label = "PAPER"


class FakeBroker:
    """Minimal broker double: no real Alpaca/HTTP calls, fully scripted responses."""

    def __init__(self, account=None, positions=None, submit_side_effects=None,
                 default_response=None, orders_by_client_id=None):
        self.config = FakeConfig()
        self._account = account or {"equity": "10000", "last_equity": "10000"}
        self._positions = positions or []
        self._submit_side_effects = submit_side_effects or {}
        self._default_response = default_response or FakeBrokerResponse(
            status_code=200, text="OK", dry_run=False
        )
        self._orders_by_client_id = orders_by_client_id or {}
        self.submit_calls = []
        self.client_order_ids = []

    def get_account(self):
        return self._account

    def get_positions(self):
        return self._positions

    def submit_order(self, symbol, qty=1, *, side, client_order_id=None):
        self.submit_calls.append((symbol, qty))
        self.client_order_ids.append(client_order_id)
        effect = self._submit_side_effects.get(symbol)
        if isinstance(effect, Exception):
            raise effect
        return effect or self._default_response

    def get_order_by_client_order_id(self, client_order_id):
        return self._orders_by_client_id.get(client_order_id)


def _high_score_result(symbol):
    return {
        "symbol": symbol,
        "price": 100.0,
        "ma200": 90.0,
        "rsi": 50.0,
        "volume_ratio": 1.5,
        "score": 100,
    }


def _write_history(path, rows=None):
    """Write a valid order_history.csv with the current required schema."""
    rows = rows or []
    pd.DataFrame(rows, columns=pso.REQUIRED_HISTORY_COLUMNS).to_csv(path, index=False)


def _patch_common(monkeypatch, tmp_path, tickers, broker, market_session="regular", init_history=True):
    monkeypatch.setattr(pso, "load_watchlist", lambda: tickers)
    monkeypatch.setattr(pso, "analyze_stock", _high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: market_session)
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    if init_history:
        pso.initialize_order_history()
    slack_calls = []
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: slack_calls.append(msg) or True)
    return slack_calls


# ---------------------------------------------------------------------------
# Happy path (scenarios 1-3)
# ---------------------------------------------------------------------------

def test_valid_candidate_submits_order_once(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert broker.submit_calls == [("AAPL", 1)]


def test_successful_order_is_persisted_to_history(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert (history["symbol"] == "AAPL").any()
    assert (history["order_date"] == TODAY).any()
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMITTED"


def test_successful_order_triggers_slack_notification(monkeypatch, tmp_path):
    broker = FakeBroker()
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert any("Paper Strategy Order" in msg and "SUBMITTED" in msg for msg in slack_calls)


# ---------------------------------------------------------------------------
# Safety blocks (scenarios 4-11)
# ---------------------------------------------------------------------------

def test_live_url_order_blocked():
    config = BrokerConfig(
        trading_mode="live",
        enable_real_trading=True,
        live_dry_run=False,
        api_key="key",
        secret_key="secret",
    )
    assert config.base_url == "https://api.alpaca.markets"
    broker = AlpacaBroker(config=config, session=DummySession())

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1, side="buy", live_entry_context=_live_entry_context())


def test_non_paper_mode_blocked_by_trading_mode_check(monkeypatch):
    class FakeLiveConfig:
        is_live_mode = True
        can_submit_live_order = True

    monkeypatch.setattr(order_safety, "BrokerConfig", lambda: FakeLiveConfig())

    with pytest.raises(Exception):
        order_safety.check_trading_mode()


def test_paper_mode_passes_trading_mode_check(monkeypatch):
    class FakePaperConfig:
        is_live_mode = False
        is_paper_mode = True
        can_submit_live_order = False

        def validate_order_allowed(self):
            return True

    monkeypatch.setattr(order_safety, "BrokerConfig", lambda: FakePaperConfig())

    assert order_safety.check_trading_mode() is True


def test_unknown_trading_mode_is_blocked():
    config = BrokerConfig(
        trading_mode="papre",
        api_key="key",
        secret_key="secret",
    )
    broker = AlpacaBroker(config=config, session=DummySession())

    with pytest.raises(RuntimeError, match="must be exactly 'paper'"):
        broker.submit_order("AAPL", qty=1, side="buy")


def test_paper_mode_with_live_endpoint_is_blocked():
    config = BrokerConfig(
        trading_mode="paper",
        paper_base_url="https://api.alpaca.markets",
        api_key="key",
        secret_key="secret",
    )
    broker = AlpacaBroker(config=config, session=DummySession())

    with pytest.raises(RuntimeError, match="not the official Paper endpoint"):
        broker.submit_order("AAPL", qty=1, side="buy")


def test_duplicate_order_blocks_resubmission(monkeypatch, tmp_path):
    history_file = tmp_path / "order_history.csv"
    _write_history(
        history_file,
        [{"symbol": "AAPL", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"}],
    )

    broker = FakeBroker()
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)

    pso.main(broker=broker)

    assert broker.submit_calls == []
    assert any("Duplicate order prevented" in msg for msg in slack_calls)


def test_held_position_blocks_rebuy(monkeypatch, tmp_path):
    broker = FakeBroker(positions=[{"symbol": "AAPL"}])
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert broker.submit_calls == []
    assert any("Already held" in msg for msg in slack_calls)


def test_daily_trade_count_limit_blocks_order(monkeypatch, tmp_path):
    monkeypatch.setattr(order_safety, "MAX_TRADES_PER_DAY", 0)
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    with pytest.raises(Exception, match="Daily trade count exceeded"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_daily_trade_count_is_restored_from_history(monkeypatch, tmp_path):
    _write_history(
        tmp_path / "order_history.csv",
        [
            {"symbol": f"OLD{i}", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"}
            for i in range(order_safety.MAX_TRADES_PER_DAY)
        ],
    )
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)

    with pytest.raises(Exception, match="Daily trade count exceeded"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_daily_loss_limit_blocks_all_orders(monkeypatch, tmp_path):
    broker = FakeBroker(account={"equity": "9700", "last_equity": "10000"})
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    with pytest.raises(Exception, match="Daily loss limit exceeded"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_outside_regular_session_orders_not_submitted(monkeypatch, tmp_path):
    broker = FakeBroker()
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, market_session="premarket")

    pso.main(broker=broker)

    assert broker.submit_calls == []
    assert any("Order review only" in msg for msg in slack_calls)


def test_position_size_over_limit_is_blocked():
    with pytest.raises(Exception):
        order_safety.check_position_size(order_safety.MAX_POSITION_RATE + 0.5)


def test_abnormal_order_value_relative_to_equity_blocks_order(monkeypatch, tmp_path):
    # Small account, high-priced candidate: qty(1) * price(200) / equity(1000) = 0.20,
    # which exceeds the existing risk_config.MAX_POSITION_RATE (0.10). No new
    # threshold is introduced here; this exercises the real position-value
    # calculation instead of the previous hardcoded 0.01 placeholder.
    broker = FakeBroker(account={"equity": "1000", "last_equity": "1000"})
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)
    monkeypatch.setattr(
        pso,
        "analyze_stock",
        lambda symbol: {
            "symbol": symbol,
            "price": 200.0,
            "ma200": 150.0,
            "rsi": 50.0,
            "volume_ratio": 1.5,
            "score": 100,
        },
    )

    with pytest.raises(Exception, match="Position size exceeded"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_normal_order_value_within_limit_is_not_blocked_by_position_size(monkeypatch, tmp_path):
    # Same account/price ratio as the default happy-path fixtures (0.01),
    # well under MAX_POSITION_RATE — confirms the real calculation still
    # allows ordinary small orders through.
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert broker.submit_calls == [("AAPL", 1)]


# ---------------------------------------------------------------------------
# External failure handling (scenarios 12-15)
# ---------------------------------------------------------------------------

def test_broker_timeout_is_handled_safely_and_next_symbol_continues(monkeypatch, tmp_path):
    broker = FakeBroker(
        submit_side_effects={"AAPL": requests.exceptions.Timeout("timed out")}
    )
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL", "MSFT"], broker)

    pso.main(broker=broker)  # must not raise

    assert broker.submit_calls == [("AAPL", 1), ("MSFT", 1)]
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMISSION_FAILED"
    assert history.loc[history["symbol"] == "MSFT", "status"].iloc[0] == "SUBMITTED"
    assert any("Order failed" in msg and "AAPL" in msg for msg in slack_calls)


def test_rejected_response_is_recorded_as_rejected(monkeypatch, tmp_path):
    broker = FakeBroker(
        submit_side_effects={"AAPL": FakeBrokerResponse(status_code=422, text="rejected", dry_run=False)}
    )
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "REJECTED"
    assert any("FAILED" in msg for msg in slack_calls)


def test_order_history_save_failure_is_logged_not_raised(monkeypatch, tmp_path, capsys):
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", readonly_dir / "order_history.csv")
    readonly_dir.chmod(0o500)
    try:
        result = pso.save_order_history(pd.DataFrame([{"symbol": "AAPL", "order_date": TODAY}]))
    finally:
        readonly_dir.chmod(0o700)

    assert result is False
    assert "Failed to save" in capsys.readouterr().out


def test_failed_save_preserves_existing_file(monkeypatch, tmp_path):
    history_file = tmp_path / "order_history.csv"
    _write_history(
        history_file,
        [{"symbol": "AAPL", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"}],
    )
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", history_file)
    original_bytes = history_file.read_bytes()

    history_file.parent.chmod(0o500)
    try:
        result = pso.save_order_history(
            pd.DataFrame([{"symbol": "MSFT", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"}])
        )
    finally:
        history_file.parent.chmod(0o700)

    assert result is False
    assert history_file.read_bytes() == original_bytes


def test_order_is_not_submitted_when_history_reservation_fails(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)
    monkeypatch.setattr(pso, "save_order_history", lambda history: False)

    with pytest.raises(RuntimeError, match="reservation failed"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_pending_reservation_blocks_duplicate_after_restart(monkeypatch, tmp_path):
    _write_history(
        tmp_path / "order_history.csv",
        [{"symbol": "AAPL", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "PENDING_SUBMISSION"}],
    )
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)

    pso.main(broker=broker)

    assert broker.submit_calls == []


def test_slack_failure_does_not_prevent_history_save(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    def _raise_slack(message):
        raise requests.exceptions.ConnectionError("slack unreachable")

    monkeypatch.setattr(pso, "send_slack_alert", _raise_slack)

    pso.main(broker=broker)  # must not raise despite Slack failing

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert (history["symbol"] == "AAPL").any()


# ---------------------------------------------------------------------------
# CODEX-002: order history integrity (fail-closed reads)
# ---------------------------------------------------------------------------

def test_missing_history_blocks_new_orders(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)
    # ORDER_HISTORY_FILE deliberately left absent — simulates the file
    # disappearing mid-operation, not a fresh install.

    with pytest.raises(pso.OrderHistoryUnavailable, match="MISSING_HISTORY"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_corrupted_history_missing_columns_blocks_new_orders(monkeypatch, tmp_path):
    history_file = tmp_path / "order_history.csv"
    pd.DataFrame([{"symbol": "AAPL"}]).to_csv(history_file, index=False)  # missing order_date/mode/dry_run/status
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)

    with pytest.raises(pso.OrderHistoryUnavailable, match="CORRUPTED_HISTORY"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_corrupted_history_bad_date_blocks_new_orders(monkeypatch, tmp_path):
    history_file = tmp_path / "order_history.csv"
    _write_history(
        history_file,
        [{"symbol": "AAPL", "order_date": "not-a-date", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"}],
    )
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)

    with pytest.raises(pso.OrderHistoryUnavailable, match="CORRUPTED_HISTORY"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_unreadable_history_blocks_new_orders(monkeypatch, tmp_path):
    history_file = tmp_path / "order_history.csv"
    history_file.write_text("not,a,valid\ncsv\x00structure,,,")
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)

    with pytest.raises(pso.OrderHistoryUnavailable):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_initialize_order_history_creates_valid_empty_file(monkeypatch, tmp_path):
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")

    created = pso.initialize_order_history()

    assert created.empty
    reloaded = pso.load_order_history()  # must not raise
    assert reloaded.empty
    assert list(reloaded.columns) == pso.REQUIRED_HISTORY_COLUMNS


def test_daily_trade_date_uses_eastern_time_not_local(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 23:30 in Seoul on 2026-06-15 is still 2026-06-15 10:30 in New York —
    # this must resolve to the New York calendar date, not the Seoul one.
    seoul_evening = datetime(2026, 6, 15, 23, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    ny_date = pso.eastern_now(seoul_evening).strftime("%Y-%m-%d")
    assert ny_date == "2026-06-15"

    # Conversely, 00:30 in New York on 2026-06-16 is already 13:30 in Seoul
    # on the same NY calendar day per this call's own reference point.
    ny_midnight = datetime(2026, 6, 16, 0, 30, tzinfo=ZoneInfo("America/New_York"))
    assert pso.eastern_now(ny_midnight).strftime("%Y-%m-%d") == "2026-06-16"


# ---------------------------------------------------------------------------
# CODEX-007: strict canonical order_date validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["2026-07-21", "2024-02-29"])
def test_validate_order_date_str_accepts_canonical_dates(value):
    assert pso.validate_order_date_str(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "2026/07/21",
        "07-21-2026",
        "2026-7-21",
        "2026-07-1",
        "2026-07-21T00:00:00",
        "2026-07-21 00:00:00",
        "2026-07-21Z",
        "2026-07-21+09:00",
        " 2026-07-21",
        "2026-07-21 ",
        "2026-02-30",
        "not-a-date",
        "",
        None,
        float("nan"),
    ],
)
def test_validate_order_date_str_rejects_non_canonical_values(value):
    with pytest.raises(ValueError):
        pso.validate_order_date_str(value)


def test_non_canonical_order_date_blocks_all_new_orders(monkeypatch, tmp_path):
    _write_history(
        tmp_path / "order_history.csv",
        [
            {"symbol": "OLD", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
            {"symbol": "MSFT", "order_date": "2026-07-20 10:30:00", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
        ],
    )
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)

    with pytest.raises(pso.OrderHistoryUnavailable, match="CORRUPTED_HISTORY"):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_non_canonical_date_is_not_silently_undercounted(monkeypatch, tmp_path):
    # Directly reproduces the CODEX-007 evidence: a parseable-but-non-canonical
    # order_date must not let count_orders_for_date() silently return 0 for
    # today's canonical date.
    history_file = tmp_path / "order_history.csv"
    pd.DataFrame(
        [{"symbol": "AAPL", "order_date": "2026-07-20 10:30:00", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"}],
        columns=pso.REQUIRED_HISTORY_COLUMNS,
    ).to_csv(history_file, index=False)
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", history_file)

    with pytest.raises(pso.OrderHistoryUnavailable):
        pso.load_order_history()  # must fail closed, never silently return count 0


def test_valid_and_non_canonical_dates_together_still_block(monkeypatch, tmp_path):
    # A normal past date and today's canonical date coexist with one bad row
    # — the single bad row must still corrupt the whole file (fail-closed).
    _write_history(
        tmp_path / "order_history.csv",
        [
            {"symbol": "OLD1", "order_date": "2026-01-05", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
            {"symbol": "OLD2", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
            {"symbol": "BAD", "order_date": "2026-1-5", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
        ],
    )
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")

    with pytest.raises(pso.OrderHistoryUnavailable, match="CORRUPTED_HISTORY"):
        pso.load_order_history()


def test_et_midnight_boundary_daily_count_is_exact(monkeypatch, tmp_path):
    # Two canonical-date rows for "today" and one for "yesterday" must count
    # exactly 2 for today, regardless of ET midnight boundary phrasing.
    from datetime import datetime as dt_class
    from datetime import timedelta

    yesterday = (dt_class.strptime(TODAY, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    history_file = tmp_path / "order_history.csv"
    _write_history(
        history_file,
        [
            {"symbol": "A", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
            {"symbol": "B", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
            {"symbol": "C", "order_date": yesterday, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
        ],
    )
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", history_file)

    history = pso.load_order_history()
    assert pso.count_orders_for_date(history, TODAY) == 2
    assert pso.count_orders_for_date(history, yesterday) == 1


def test_date_validation_runs_before_duplicate_check(monkeypatch, tmp_path):
    # Even when a symbol/date pair would also be flagged as a duplicate,
    # the corrupted-date failure must surface first (load_order_history()
    # raises before is_duplicate_order() is ever reached).
    _write_history(
        tmp_path / "order_history.csv",
        [
            {"symbol": "AAPL", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
            {"symbol": "AAPL", "order_date": "2026/07/20", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
        ],
    )
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, init_history=False)

    with pytest.raises(pso.OrderHistoryUnavailable):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_diagnose_order_history_dates_reports_problems_without_mutating_file(monkeypatch, tmp_path):
    history_file = tmp_path / "order_history.csv"
    _write_history(
        history_file,
        [
            {"symbol": "AAPL", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
            {"symbol": "MSFT", "order_date": "2026-7-20", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
        ],
    )
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", history_file)
    original_bytes = history_file.read_bytes()

    problems = pso.diagnose_order_history_dates()

    assert len(problems) == 1
    assert problems[0]["raw_value"] == "2026-7-20"
    assert history_file.read_bytes() == original_bytes  # diagnostic only, never rewrites


# ---------------------------------------------------------------------------
# CODEX-003: atomic, concurrency-safe order history writes
# ---------------------------------------------------------------------------

def test_concurrent_reservations_same_symbol_only_one_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()

    results = []
    barrier = threading.Barrier(2)

    def _attempt():
        barrier.wait(timeout=5)
        try:
            pso.try_reserve_order("AAPL", TODAY, "PAPER", False)
            results.append("ok")
        except pso.DuplicateOrderError:
            results.append("duplicate")

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert sorted(results) == ["duplicate", "ok"]
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert len(history[history["symbol"] == "AAPL"]) == 1


def test_concurrent_reservations_different_symbols_both_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()

    errors = []
    barrier = threading.Barrier(2)

    def _attempt(symbol):
        barrier.wait(timeout=5)
        try:
            pso.try_reserve_order(symbol, TODAY, "PAPER", False)
        except Exception as exc:  # pragma: no cover - failure path asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_attempt, args=(sym,)) for sym in ("AAPL", "MSFT")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert set(history["symbol"]) == {"AAPL", "MSFT"}
    assert len(history) == 2  # no lost update


def test_concurrent_reservations_respect_daily_trade_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    monkeypatch.setattr(order_safety, "MAX_TRADES_PER_DAY", 1)
    pso.initialize_order_history()

    outcomes = []
    barrier = threading.Barrier(2)

    def _attempt(symbol):
        barrier.wait(timeout=5)
        try:
            pso.try_reserve_order(symbol, TODAY, "PAPER", False)
            outcomes.append("reserved")
        except Exception:
            outcomes.append("blocked")

    threads = [threading.Thread(target=_attempt, args=(sym,)) for sym in ("AAPL", "MSFT")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert sorted(outcomes) == ["blocked", "reserved"]
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert len(history) == 1  # the daily limit was not raced past


def test_lock_acquisition_timeout_blocks_order(tmp_path, monkeypatch):
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()

    import fcntl

    held_lock_file = open(tmp_path / "order_history.lock", "a+")
    fcntl.flock(held_lock_file, fcntl.LOCK_EX)
    try:
        with pytest.raises(RuntimeError, match="Could not acquire order history lock"):
            pso.try_reserve_order("AAPL", TODAY, "PAPER", False, lock_timeout=0.2)
    finally:
        fcntl.flock(held_lock_file, fcntl.LOCK_UN)
        held_lock_file.close()

    # The file must be untouched — no partial reservation was written.
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.empty


# ---------------------------------------------------------------------------
# CODEX-006: reconciliation and partial-fill tracking
# ---------------------------------------------------------------------------

def _reserve_pending(monkeypatch, tmp_path, symbol="AAPL", qty=1):
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()
    _, client_order_id = pso.try_reserve_order(symbol, TODAY, "PAPER", False, qty=qty)
    return client_order_id


def test_client_order_id_is_generated_and_sent_to_broker(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert len(broker.client_order_ids) == 1
    assert broker.client_order_ids[0].startswith("scalp-AAPL-")


def test_reconcile_marks_filled_order_and_updates_history(monkeypatch, tmp_path):
    client_order_id = _reserve_pending(monkeypatch, tmp_path)
    broker = FakeBroker(
        orders_by_client_id={
            client_order_id: {"status": "filled", "filled_qty": "1", "filled_avg_price": "101.50"}
        }
    )

    pso.reconcile_pending_orders(broker)

    reconciliation = pd.read_csv(tmp_path / "order_reconciliation.csv")
    row = reconciliation[reconciliation["client_order_id"] == client_order_id].iloc[0]
    assert row["local_status"] == "FILLED"
    assert float(row["filled_qty"]) == 1.0
    assert float(row["average_fill_price"]) == 101.50
    assert pd.notna(row["last_reconciled_at"])

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "FILLED"


def test_reconcile_partial_fill_is_not_treated_as_filled(monkeypatch, tmp_path):
    client_order_id = _reserve_pending(monkeypatch, tmp_path)
    broker = FakeBroker(
        orders_by_client_id={
            client_order_id: {"status": "partially_filled", "filled_qty": "0.5", "filled_avg_price": "101.50"}
        }
    )

    pso.reconcile_pending_orders(broker)

    reconciliation = pd.read_csv(tmp_path / "order_reconciliation.csv")
    row = reconciliation[reconciliation["client_order_id"] == client_order_id].iloc[0]
    assert row["local_status"] == "PARTIALLY_FILLED"
    assert row["local_status"] != "FILLED"

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "PARTIALLY_FILLED"


def test_reconcile_unknown_broker_order_marks_manual_review_without_resubmit(monkeypatch, tmp_path):
    client_order_id = _reserve_pending(monkeypatch, tmp_path)
    broker = FakeBroker()  # orders_by_client_id empty -> lookup returns None

    pso.reconcile_pending_orders(broker)

    reconciliation = pd.read_csv(tmp_path / "order_reconciliation.csv")
    row = reconciliation[reconciliation["client_order_id"] == client_order_id].iloc[0]
    assert row["local_status"] == "MANUAL_REVIEW"

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "MANUAL_REVIEW"
    assert broker.submit_calls == []  # never auto-resubmitted


@pytest.mark.parametrize(
    "broker_status,expected_local_status",
    [
        ("rejected", "REJECTED"),
        ("canceled", "CANCELLED"),
        ("expired", "EXPIRED"),
        ("something_alpaca_added_later", "UNKNOWN"),
    ],
)
def test_reconcile_maps_terminal_broker_statuses(monkeypatch, tmp_path, broker_status, expected_local_status):
    client_order_id = _reserve_pending(monkeypatch, tmp_path)
    broker = FakeBroker(orders_by_client_id={client_order_id: {"status": broker_status, "filled_qty": "0"}})

    pso.reconcile_pending_orders(broker)

    reconciliation = pd.read_csv(tmp_path / "order_reconciliation.csv")
    row = reconciliation[reconciliation["client_order_id"] == client_order_id].iloc[0]
    assert row["local_status"] == expected_local_status


def test_reconcile_lookup_failure_leaves_state_unchanged(monkeypatch, tmp_path):
    client_order_id = _reserve_pending(monkeypatch, tmp_path)

    class FailingLookupBroker(FakeBroker):
        def get_order_by_client_order_id(self, client_order_id):
            raise requests.exceptions.ConnectionError("network down")

    broker = FailingLookupBroker()

    pso.reconcile_pending_orders(broker)  # must not raise

    reconciliation = pd.read_csv(tmp_path / "order_reconciliation.csv")
    row = reconciliation[reconciliation["client_order_id"] == client_order_id].iloc[0]
    assert row["local_status"] == "PENDING_SUBMISSION"  # unresolved, retryable on a future run


def test_reconcile_is_idempotent_no_duplicate_rows(monkeypatch, tmp_path):
    client_order_id = _reserve_pending(monkeypatch, tmp_path)
    broker = FakeBroker(
        orders_by_client_id={
            client_order_id: {"status": "filled", "filled_qty": "1", "filled_avg_price": "101.50"}
        }
    )

    pso.reconcile_pending_orders(broker)
    pso.reconcile_pending_orders(broker)  # re-run with no new broker state

    reconciliation = pd.read_csv(tmp_path / "order_reconciliation.csv")
    assert len(reconciliation) == 1
    assert reconciliation.iloc[0]["local_status"] == "FILLED"


# ---------------------------------------------------------------------------
# CODEX-008: reconciliation locking and monotonic state merge
# ---------------------------------------------------------------------------

def test_merge_never_regresses_status():
    existing = {"local_status": "FILLED", "filled_qty": 1, "requested_qty": 1, "average_fill_price": 101.5}
    incoming = {"local_status": "SUBMITTED", "filled_qty": 0}

    merged = pso.merge_reconciliation_state(existing, incoming)

    assert merged["local_status"] == "FILLED"


def test_merge_unknown_never_overwrites_filled():
    existing = {"local_status": "FILLED", "filled_qty": 1, "requested_qty": 1, "average_fill_price": 101.5}
    incoming = {"local_status": "UNKNOWN"}

    merged = pso.merge_reconciliation_state(existing, incoming)

    assert merged["local_status"] == "FILLED"


def test_merge_unknown_is_accepted_from_pending_submission():
    existing = {"local_status": "PENDING_SUBMISSION", "filled_qty": 0, "requested_qty": 1}
    incoming = {"local_status": "UNKNOWN"}

    merged = pso.merge_reconciliation_state(existing, incoming)

    assert merged["local_status"] == "UNKNOWN"


def test_merge_filled_qty_never_decreases():
    existing = {"local_status": "PARTIALLY_FILLED", "filled_qty": 70, "requested_qty": 100}
    incoming = {"local_status": "PARTIALLY_FILLED", "filled_qty": 30}

    merged = pso.merge_reconciliation_state(existing, incoming)

    assert merged["filled_qty"] == 70


def test_merge_average_fill_price_never_cleared():
    existing = {"local_status": "FILLED", "filled_qty": 1, "requested_qty": 1, "average_fill_price": 101.5}
    incoming = {"local_status": "FILLED", "filled_qty": 1, "average_fill_price": None}

    merged = pso.merge_reconciliation_state(existing, incoming)

    assert merged["average_fill_price"] == 101.5


def test_merge_progression_is_allowed_forward():
    existing = {"local_status": "SUBMITTED", "filled_qty": 0, "requested_qty": 1}
    incoming = {"local_status": "PARTIALLY_FILLED", "filled_qty": 0.5}

    merged = pso.merge_reconciliation_state(existing, incoming)

    assert merged["local_status"] == "PARTIALLY_FILLED"
    assert merged["filled_qty"] == 0.5


def test_corrupted_reconciliation_file_fails_closed_not_reinitialized(monkeypatch, tmp_path):
    reconciliation_file = tmp_path / "order_reconciliation.csv"
    reconciliation_file.write_text("not,the,right,columns\n1,2,3,4\n")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", reconciliation_file)
    original_bytes = reconciliation_file.read_bytes()

    with pytest.raises(pso.ReconciliationUnavailable):
        pso.load_reconciliation()

    assert reconciliation_file.read_bytes() == original_bytes  # never silently reinitialized


def test_reconciliation_write_failure_blocks_order_reservation(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)
    monkeypatch.setattr(pso, "save_reconciliation", lambda df: False)

    with pytest.raises(RuntimeError, match="Reconciliation record failed"):
        pso.main(broker=broker)

    assert broker.submit_calls == []  # never reached broker without a tracked reservation


def test_reconciliation_lock_timeout_leaves_file_unchanged(tmp_path, monkeypatch):
    reconciliation_file = tmp_path / "order_reconciliation.csv"
    lock_file = tmp_path / "order_reconciliation.lock"
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", reconciliation_file)
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", lock_file)
    pso.save_reconciliation(pd.DataFrame(columns=pso.RECONCILIATION_COLUMNS))
    original_bytes = reconciliation_file.read_bytes()

    import fcntl

    held = open(lock_file, "a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        result = pso._update_reconciliation_row(
            "some-id", "AAPL", TODAY, {"local_status": "FILLED", "filled_qty": 1}, lock_timeout=0.2
        )
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert result is False
    assert reconciliation_file.read_bytes() == original_bytes


def test_reconciliation_save_failure_preserves_existing_file(monkeypatch, tmp_path):
    reconciliation_file = tmp_path / "order_reconciliation.csv"
    lock_file = tmp_path / "order_reconciliation.lock"
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", reconciliation_file)
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", lock_file)
    pd.DataFrame(
        [{"client_order_id": "id-1", "symbol": "AAPL", "order_date": TODAY, "requested_qty": 1,
          "filled_qty": 0, "remaining_qty": 1, "average_fill_price": None, "broker_status": None,
          "local_status": "PENDING_SUBMISSION", "last_reconciled_at": None}],
        columns=pso.RECONCILIATION_COLUMNS,
    ).to_csv(reconciliation_file, index=False)
    lock_file.touch()  # pre-create so the read-only dir below blocks the *write*, not lock-file creation
    original_bytes = reconciliation_file.read_bytes()

    reconciliation_file.parent.chmod(0o500)
    try:
        result = pso._update_reconciliation_row(
            "id-1", "AAPL", TODAY, {"local_status": "FILLED", "filled_qty": 1}
        )
    finally:
        reconciliation_file.parent.chmod(0o700)

    assert result is False
    assert reconciliation_file.read_bytes() == original_bytes


def test_immediate_response_and_history_agree_on_partial_fill(monkeypatch, tmp_path):
    # Reproduces the CODEX-008 evidence directly: an immediate partially_filled
    # broker response must leave order_history.csv and order_reconciliation.csv
    # in agreement, not history=SUBMITTED / reconciliation=PARTIALLY_FILLED.
    broker = FakeBroker(
        default_response=FakeBrokerResponse(
            status_code=200,
            text="OK",
            dry_run=False,
            data={"status": "partially_filled", "filled_qty": "0.5", "filled_avg_price": "101.50"},
        )
    )
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    history = pd.read_csv(tmp_path / "order_history.csv")
    reconciliation = pd.read_csv(tmp_path / "order_reconciliation.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "PARTIALLY_FILLED"
    assert reconciliation.iloc[0]["local_status"] == "PARTIALLY_FILLED"


def test_repeated_reconcile_pending_orders_is_idempotent_with_locking(monkeypatch, tmp_path):
    client_order_id = _reserve_pending(monkeypatch, tmp_path)
    broker = FakeBroker(
        orders_by_client_id={
            client_order_id: {"status": "filled", "filled_qty": "1", "filled_avg_price": "101.50"}
        }
    )

    for _ in range(3):
        pso.reconcile_pending_orders(broker)

    reconciliation = pd.read_csv(tmp_path / "order_reconciliation.csv")
    assert len(reconciliation) == 1
    assert reconciliation.iloc[0]["local_status"] == "FILLED"


def test_multiprocessing_concurrent_updates_resolve_to_filled_with_max_qty(tmp_path):
    reconciliation_file = tmp_path / "order_reconciliation.csv"
    lock_file = tmp_path / "order_reconciliation.lock"
    seed = pd.DataFrame(
        [{"client_order_id": "order-x", "symbol": "AAPL", "order_date": TODAY, "requested_qty": 100,
          "filled_qty": 0, "remaining_qty": 100, "average_fill_price": None, "broker_status": None,
          "local_status": "PENDING_SUBMISSION", "last_reconciled_at": None}],
        columns=pso.RECONCILIATION_COLUMNS,
    )
    seed.to_csv(reconciliation_file, index=False)

    barrier = multiprocessing.Barrier(2)
    proc_a = multiprocessing.Process(
        target=_mp_update_reconciliation,
        args=(reconciliation_file, lock_file, "order-x", "AAPL", TODAY,
              {"local_status": "PARTIALLY_FILLED", "filled_qty": 30}, barrier),
    )
    proc_b = multiprocessing.Process(
        target=_mp_update_reconciliation,
        args=(reconciliation_file, lock_file, "order-x", "AAPL", TODAY,
              {"local_status": "FILLED", "filled_qty": 70}, barrier),
    )
    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=15)
    proc_b.join(timeout=15)
    assert proc_a.exitcode == 0
    assert proc_b.exitcode == 0

    result = pd.read_csv(reconciliation_file)
    assert len(result) == 1  # no lost update, no duplicate row
    assert result.iloc[0]["local_status"] == "FILLED"
    assert result.iloc[0]["filled_qty"] >= 70


def test_multiprocessing_concurrent_different_orders_both_preserved(tmp_path):
    reconciliation_file = tmp_path / "order_reconciliation.csv"
    lock_file = tmp_path / "order_reconciliation.lock"
    pd.DataFrame(columns=pso.RECONCILIATION_COLUMNS).to_csv(reconciliation_file, index=False)

    barrier = multiprocessing.Barrier(2)
    proc_a = multiprocessing.Process(
        target=_mp_update_reconciliation,
        args=(reconciliation_file, lock_file, "order-a", "AAPL", TODAY,
              {"local_status": "SUBMITTED", "filled_qty": 0, "requested_qty": 1}, barrier),
    )
    proc_b = multiprocessing.Process(
        target=_mp_update_reconciliation,
        args=(reconciliation_file, lock_file, "order-m", "MSFT", TODAY,
              {"local_status": "SUBMITTED", "filled_qty": 0, "requested_qty": 1}, barrier),
    )
    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=15)
    proc_b.join(timeout=15)
    assert proc_a.exitcode == 0
    assert proc_b.exitcode == 0

    result = pd.read_csv(reconciliation_file)
    assert set(result["client_order_id"]) == {"order-a", "order-m"}
    assert len(result) == 2  # no lost update
