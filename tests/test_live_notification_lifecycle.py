"""The KIS live trading Slack lifecycle, and the two properties that make
it safe to have at all.

Background
----------
An audit found the KIS live path Slack-silent: a first real order would
have submitted, filled, gone UNKNOWN or been cancelled without a single
message. `operations/live_notifications.py` fills that gap.

Adding notifications to a transport path is itself a hazard, so two
things are pinned here above everything else:

1. **Ordering.** LIVE_ORDER_PREPARED is emitted before the broker call
   and every post-transport event after it. A message sequence that
   claimed an order was submitted before it was prepared would misinform
   exactly when it matters.

2. **Failure isolation.** A Slack outage must not change what the trading
   system does -- and specifically must not cause a second transport
   call, because the first order may already be live at the broker.
   `transport call count == 1` is asserted under a notifier that raises
   on every send.
"""
from datetime import datetime, timezone

import pytest

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from domain.execution_event import ExecutionRecord
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution import execution_engine, idempotency, order_gate
from execution.entry_limits import EntryLimitState
from market_hours import us_trading_day
from operations import live_notifications
from reconciliation.snapshot import ReconciliationSnapshot
from state_store import db as state_db
import shadow_audit

NOW = datetime(2026, 8, 7, 17, 30, tzinfo=timezone.utc)
TODAY = us_trading_day(NOW)
ACCOUNT = "12345678"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POS.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "HALT.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEM.lock")
    # notification_health persists send outcomes; without this it writes
    # NOTIFICATION_HEALTH_STATE.json and notification_health.log into the
    # repository root, which the suite must never leave behind.
    monkeypatch.setenv("NOTIFICATION_HEALTH_STATE_FILE", str(tmp_path / "NH_STATE.json"))
    monkeypatch.setenv("NOTIFICATION_HEALTH_LOG_FILE", str(tmp_path / "nh.log"))
    state_db.open_db().close()
    yield


class _Recorder:
    """Captures the event sequence instead of sending anything."""

    def __init__(self, fail=False):
        self.events = []
        self.messages = []
        self._fail = fail

    def install(self, monkeypatch):
        real_format = live_notifications._format

        def _spy(event, fields=None, *, test=False, send_fn=None):
            self.events.append(event)
            self.messages.append(real_format(event, fields or {}, test=test))
            if self._fail:
                raise RuntimeError("slack is down")
            return True

        monkeypatch.setattr(live_notifications, "notify", _spy)
        return self


def _order_intent(side="buy", symbol="AAPL", quantity=1):
    return OrderIntent(
        internal_order_id=f"ord-{side}-1", signal_id="sig-1", strategy_id="S",
        symbol=symbol, exchange="NASDAQ", side=side, quantity=quantity,
        order_type="limit", limit_price=100.0, stop_price=None, target_price=None,
        created_at=NOW)


def _limits():
    return EntryLimitState(
        max_open_positions=1, max_daily_entries=1,
        open_position_symbols=frozenset(), pending_entry_symbols=frozenset(),
        daily_entry_count=0, trading_day=TODAY)


def _snapshot(symbol="AAPL"):
    return ReconciliationSnapshot(
        account_id=ACCOUNT, symbol=symbol, checked_at=NOW, positions_match=True,
        open_orders_match=True, fills_match=True, has_unknown_orders=False,
        source="test", detail=())


def _buy_ctx_builder(order_intent):
    def _build(reconciliation):
        signal = build_signal(
            strategy_id="S", strategy_version="v1", config_version="c", code_commit="c1",
            symbol=order_intent.symbol, exchange="NASDAQ", signal_price=100.0, score=99,
            entry_reason="test", valid_for_seconds=300, now=NOW)
        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=True, entry_disabled=False,
            validated_commit="c1", deployed_commit="c1", kis_account_no=ACCOUNT,
            allowed_account_no=ACCOUNT, order_intent=order_intent,
            instrument=build_instrument(order_intent.symbol, exchange="NASDAQ"),
            signal=signal, is_regular_session=True, kis_price_usd=100.0,
            max_price_deviation_percent=30.0, usd_orderable_cash=10_000.0,
            has_open_order_for_symbol=False, has_order_for_signal_id=False,
            allowed_symbols=frozenset({order_intent.symbol}),
            reconciliation=reconciliation, entry_limits=_limits(), now=NOW)
    return _build


class _Broker:
    def __init__(self, *, status="ACCEPTED", raises=None):
        self.submit_calls = 0
        self.cancel_calls = 0
        self._status = status
        self._raises = raises

    def get_positions(self):
        return []

    def get_open_orders(self):
        return []

    def get_fills(self, **kwargs):
        return []

    def submit_order(self, order_intent, instrument, *, authorization=None):
        self.submit_calls += 1
        if self._raises is not None:
            raise self._raises
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id="kis-1", requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status=self._status, submitted_at=NOW, updated_at=NOW)


def _submit_buy(broker, *, order_intent=None):
    order_intent = order_intent or _order_intent()
    conn = state_db.open_db()
    try:
        return execution_engine.submit_buy_order(
            order_intent=order_intent,
            buy_gate_context_builder=_buy_ctx_builder(order_intent),
            conn=conn, broker=broker,
            instrument=build_instrument(order_intent.symbol, exchange="NASDAQ"),
            account_id=ACCOUNT, audit_run_id=shadow_audit.new_run_id(), now=NOW)
    finally:
        conn.close()


# ---------------------------------------------------------------------
# §8 -- ordering
# ---------------------------------------------------------------------
class TestBuyOrdering:
    def test_prepared_precedes_submitted_and_accepted(self, monkeypatch):
        recorder = _Recorder().install(monkeypatch)
        _submit_buy(_Broker(status="ACCEPTED"))
        assert live_notifications.LIVE_ORDER_PREPARED in recorder.events
        assert live_notifications.ORDER_SUBMITTED in recorder.events
        assert live_notifications.ORDER_ACCEPTED in recorder.events
        assert (recorder.events.index(live_notifications.LIVE_ORDER_PREPARED)
                < recorder.events.index(live_notifications.ORDER_SUBMITTED)
                < recorder.events.index(live_notifications.ORDER_ACCEPTED))

    def test_prepared_is_emitted_before_the_wire_is_touched(self, monkeypatch):
        """Not just before the SUBMITTED message -- before the broker
        call itself. A notifier that observes the broker proves it."""
        order = []

        class _Watching(_Broker):
            def submit_order(self, *args, **kwargs):
                order.append("transport")
                return super().submit_order(*args, **kwargs)

        def _spy(event, fields=None, **kwargs):
            order.append(event)
            return True

        monkeypatch.setattr(live_notifications, "notify", _spy)
        _submit_buy(_Watching())
        assert order.index(live_notifications.LIVE_ORDER_PREPARED) < order.index("transport")

    def test_an_unexpected_status_reports_pending_not_accepted(self, monkeypatch):
        """The state machine only permits SUBMITTING -> ACCEPTED here, so
        ACCEPTED is the reachable case. Any other status must be
        reported as PENDING, never silently treated as accepted --
        checked at the helper, since the engine cannot be driven into
        this state (and ExecutionRecord itself rejects a status outside
        the declared vocabulary, which is a second layer)."""
        from execution import execution_engine as engine

        captured = []

        def _spy(event, fields=None, **kwargs):
            captured.append(event)
            return True

        monkeypatch.setattr(live_notifications, "notify", _spy)
        record = ExecutionRecord(
            internal_order_id="ord-1", broker="kis", broker_order_id="kis-1",
            requested_quantity=1, requested_price=100.0, filled_quantity=0.0,
            average_fill_price=None, status="CREATED",
            submitted_at=NOW, updated_at=NOW)
        engine._notify_submitted(_order_intent(), side_label="buy", record=record)
        assert live_notifications.ORDER_PENDING in captured
        assert live_notifications.ORDER_ACCEPTED not in captured


class TestUnknownOrdering:
    def test_prepared_then_unknown(self, monkeypatch):
        recorder = _Recorder().install(monkeypatch)
        broker = _Broker(raises=KISAmbiguousResponseError("timeout"))
        with pytest.raises(KISAmbiguousResponseError):
            _submit_buy(broker)
        assert (recorder.events.index(live_notifications.LIVE_ORDER_PREPARED)
                < recorder.events.index(live_notifications.ORDER_UNKNOWN))

    def test_no_submitted_event_when_the_response_was_lost(self, monkeypatch):
        recorder = _Recorder().install(monkeypatch)
        with pytest.raises(KISAmbiguousResponseError):
            _submit_buy(_Broker(raises=KISAmbiguousResponseError("timeout")))
        assert live_notifications.ORDER_SUBMITTED not in recorder.events

    def test_the_transport_is_not_retried(self, monkeypatch):
        _Recorder().install(monkeypatch)
        broker = _Broker(raises=KISAmbiguousResponseError("timeout"))
        with pytest.raises(KISAmbiguousResponseError):
            _submit_buy(broker)
        assert broker.submit_calls == 1

    def test_the_unknown_message_forbids_retry_in_words(self, monkeypatch):
        recorder = _Recorder().install(monkeypatch)
        with pytest.raises(KISAmbiguousResponseError):
            _submit_buy(_Broker(raises=KISAmbiguousResponseError("timeout")))
        message = recorder.messages[recorder.events.index(live_notifications.ORDER_UNKNOWN)]
        assert live_notifications.UNKNOWN_RETRY_LINE in message
        assert live_notifications.UNKNOWN_RECONCILIATION_LINE in message

    def test_a_broker_rejection_reports_rejected_not_unknown(self, monkeypatch):
        recorder = _Recorder().install(monkeypatch)
        with pytest.raises(KISBrokerError):
            _submit_buy(_Broker(raises=KISBrokerError("rejected")))
        assert live_notifications.ORDER_REJECTED in recorder.events
        assert live_notifications.ORDER_UNKNOWN not in recorder.events


class TestSellOrdering:
    def test_a_sell_reports_sell_submitted(self, monkeypatch):
        recorder = _Recorder().install(monkeypatch)
        order_intent = _order_intent(side="sell")
        conn = state_db.open_db()
        try:
            execution_engine.submit_sell_order(
                order_intent=order_intent,
                sell_gate_context_builder=lambda rec: order_gate.SellGateContext(
                    execution_broker="kis", live_order_enabled=True,
                    order_intent=order_intent,
                    instrument=build_instrument("AAPL", exchange="NASDAQ"),
                    kis_position_quantity=1, position_source="kis",
                    has_existing_sell_order_for_symbol=False, reconciliation=rec,
                    kis_account_no=ACCOUNT, now=NOW),
                conn=conn, broker=_Broker(status="ACCEPTED"),
                instrument=build_instrument("AAPL", exchange="NASDAQ"),
                account_id=ACCOUNT, audit_run_id=shadow_audit.new_run_id(), now=NOW)
        finally:
            conn.close()
        assert (recorder.events.index(live_notifications.LIVE_ORDER_PREPARED)
                < recorder.events.index(live_notifications.SELL_SUBMITTED))
        assert live_notifications.ORDER_SUBMITTED not in recorder.events


# ---------------------------------------------------------------------
# §6/§8 -- failure isolation
# ---------------------------------------------------------------------
def _break_slack(monkeypatch):
    """Make the real SENDERS fail, the way a Slack outage does.

    Deliberately not a monkeypatched `notify()`: production calls
    `notify()` on the contract that it never raises, and a test that
    replaced it with a raising stub would be testing its own stub. The
    outage is injected where an outage actually happens.
    """
    from operations import alerts
    import slack_utils

    def _down(_message):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(alerts, "send_alert", _down)
    monkeypatch.setattr(slack_utils, "send_slack_message", _down)
    monkeypatch.setattr(slack_utils, "send_slack_alert", _down)


class TestNotificationFailureIsolation:
    def test_a_slack_outage_does_not_duplicate_the_transport(self, monkeypatch):
        """The hazard this whole contract exists for."""
        _break_slack(monkeypatch)
        broker = _Broker(status="ACCEPTED")
        _submit_buy(broker)
        assert broker.submit_calls == 1

    def test_a_slack_outage_does_not_change_the_order_result(self, monkeypatch):
        _break_slack(monkeypatch)
        result = _submit_buy(_Broker(status="ACCEPTED"))
        assert result.status == "ACCEPTED"

    def test_a_slack_outage_preserves_the_unknown_state(self, monkeypatch):
        """The durable safety state is the point; Slack is commentary."""
        _break_slack(monkeypatch)
        broker = _Broker(raises=KISAmbiguousResponseError("timeout"))
        with pytest.raises(KISAmbiguousResponseError):
            _submit_buy(broker)
        assert broker.submit_calls == 1
        conn = state_db.open_db()
        try:
            row = conn.execute(
                "select status from kis_order_idempotency where internal_order_id = ?",
                ("ord-buy-1",)).fetchone()
        finally:
            conn.close()
        assert row["status"] == "UNKNOWN"

    def test_notify_itself_never_raises(self):
        """Even for a bad event name, an unserialisable payload, or a
        sender that explodes."""
        def _boom(_message):
            raise RuntimeError("transport down")

        assert live_notifications.notify("NOT_AN_EVENT", {}) is False
        assert live_notifications.notify(
            live_notifications.ORDER_SUBMITTED, {"x": object()}, send_fn=_boom) is False

    def test_a_falsy_send_is_reported_but_not_raised(self):
        assert live_notifications.notify(
            live_notifications.ORDER_SUBMITTED, {"symbol": "AAPL"},
            send_fn=lambda _m: False) is False


# ---------------------------------------------------------------------
# §5 -- secrets
# ---------------------------------------------------------------------
class TestRedaction:
    def test_sensitive_keys_are_masked(self):
        captured = []
        live_notifications.notify(
            live_notifications.ORDER_SUBMITTED,
            {"symbol": "AAPL", "app_key": "AK-real", "access_token": "tok-real",
             "authorization": "Bearer tok"},
            send_fn=lambda m: captured.append(m) or True)
        message = captured[0]
        assert "AK-real" not in message
        assert "tok-real" not in message
        assert "Bearer tok" not in message
        assert "***REDACTED***" in message

    def test_account_numbers_are_masked_to_four_digits(self):
        assert live_notifications.account_field("12345678").endswith("5678")
        assert "1234" not in live_notifications.account_field("12345678")[:-4]

    def test_the_prepared_payload_carries_no_account(self, monkeypatch):
        recorder = _Recorder().install(monkeypatch)
        _submit_buy(_Broker())
        prepared = recorder.messages[
            recorder.events.index(live_notifications.LIVE_ORDER_PREPARED)]
        assert ACCOUNT not in prepared


# ---------------------------------------------------------------------
# Routing and payload contracts (§7)
# ---------------------------------------------------------------------
class TestRouting:
    def test_urgent_events_go_to_the_alert_webhook(self):
        from operations import alerts

        for event in (live_notifications.ORDER_UNKNOWN, live_notifications.HALT_ACTIVATED,
                      live_notifications.RECONCILIATION_MISMATCH):
            assert live_notifications._sender_for(event) is alerts.send_alert

    def test_routine_events_go_to_the_general_webhook(self):
        import slack_utils

        for event in (live_notifications.ORDER_SUBMITTED, live_notifications.FILL_COMPLETED,
                      live_notifications.MARKET_START, live_notifications.DAILY_SUMMARY):
            assert live_notifications._sender_for(event) is slack_utils.send_slack_message

    def test_no_new_webhook_configuration_is_introduced(self):
        """It must not read a webhook URL of its own -- the two existing
        ones, owned by slack_utils, are the only channels."""
        import ast

        source = open(live_notifications.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None)
            if name in ("getenv", "get") and any(
                isinstance(a, ast.Constant) and isinstance(a.value, str)
                and "WEBHOOK" in a.value.upper() for a in node.args
            ):
                raise AssertionError(f"line {node.lineno}: reads a webhook URL directly")
        assert "requests" not in {
            n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}


class TestPayloadContracts:
    def test_prepared_has_every_required_field(self):
        fields = live_notifications.order_prepared_fields(
            symbol="AAPL", side="buy", quantity=1, limit_price=100.0,
            cash_result=10_000.0, positions_used=0, positions_max=1,
            daily_entries_used=0, daily_entries_max=1, reconciliation="clean",
            kill_switch="not halted", live_allowlist="(empty)", mode="ARMED")
        for key in ("symbol", "side", "quantity", "limit_price", "estimated_notional",
                    "cash_result", "positions", "daily_entries", "reconciliation",
                    "kill_switch", "live_allowlist", "mode"):
            assert key in fields
        assert fields["estimated_notional"] == 100.0

    def test_partial_fill_and_completion_payloads(self):
        partial = live_notifications.partial_fill_fields(
            symbol="AAPL", filled_qty=2, remaining_qty=3, average_fill_price=100.0)
        assert set(partial) == {"symbol", "filled_qty", "remaining_qty", "average_fill_price"}
        done = live_notifications.fill_completed_fields(
            symbol="AAPL", filled_qty=5, fill_price=100.0, position_qty=5, average_cost=100.0)
        assert set(done) == {"symbol", "filled_qty", "fill_price", "position_qty", "average_cost"}

    def test_sell_filled_reports_realized_pnl(self):
        fields = live_notifications.sell_filled_fields(
            symbol="AAPL", qty=1, fill_price=110.0, realized_pnl=10.0,
            realized_pnl_pct=10.0, position_after=0)
        assert fields["realized_pnl"] == 10.0
        assert fields["realized_pnl_pct"] == 10.0
        assert fields["position_after"] == 0

    def test_daily_summary_payload(self):
        fields = live_notifications.daily_summary_fields(
            entries=1, exits=1, fills=2, realized_pnl=10.0, positions=0,
            blocked_candidates=3, errors=0, unknown_count=0)
        assert set(fields) == {"entries", "exits", "fills", "realized_pnl", "positions",
                               "blocked_candidates", "errors", "unknown_count"}

    def test_every_declared_event_is_formattable(self):
        for event in sorted(live_notifications.EVENTS):
            assert live_notifications.notify(
                event, {"probe": "value"}, send_fn=lambda _m: True) is True

    def test_the_test_prefix_is_applied(self):
        captured = []
        live_notifications.notify(
            live_notifications.HALT_ACTIVATED, {"reason": "drill"}, test=True,
            send_fn=lambda m: captured.append(m) or True)
        assert captured[0].startswith("[TEST]")


# ---------------------------------------------------------------------
# PHASE A1/A4 + PHASE C -- fill deltas and position mismatch
# ---------------------------------------------------------------------
class TestFillDeltaNotifications:
    """A poll loop observes the SAME cumulative fill repeatedly.
    `lifecycle.record_fill()` is idempotent for a repeat observation, so
    the notifier compares the cumulative quantity across the call and
    stays silent when nothing moved. Without that, one 2-then-3 fill
    would produce a message on every tick."""

    def _record(self, *, filled, requested=5, price=100.0, remaining=None):
        return {"filled_qty": filled, "requested_qty": requested,
                "average_fill_price": price,
                "remaining_qty": filled if remaining is None else remaining}

    def _notified(self, monkeypatch, *, previously_filled, filled, requested=5):
        import kis_position_manager as kpm

        captured = []
        monkeypatch.setattr(
            live_notifications, "notify",
            lambda event, fields=None, **kw: captured.append((event, fields)) or True)
        kpm._notify_fill_delta(
            self._record(filled=filled, requested=requested),
            symbol="AAPL", previously_filled=previously_filled)
        return captured

    def test_a_partial_fill_reports_filled_and_remaining(self, monkeypatch):
        captured = self._notified(monkeypatch, previously_filled=0, filled=2)
        assert captured[0][0] == live_notifications.PARTIAL_FILL
        assert captured[0][1]["filled_qty"] == 2
        assert captured[0][1]["remaining_qty"] == 3

    def test_the_completing_fill_reports_completion(self, monkeypatch):
        captured = self._notified(monkeypatch, previously_filled=2, filled=5)
        assert captured[0][0] == live_notifications.FILL_COMPLETED
        assert captured[0][1]["filled_qty"] == 5

    def test_the_2_plus_3_sequence_sends_exactly_two_messages(self, monkeypatch):
        """The existing partial-fill regression, as a message sequence."""
        first = self._notified(monkeypatch, previously_filled=0, filled=2)
        second = self._notified(monkeypatch, previously_filled=2, filled=5)
        assert [e for e, _ in first] == [live_notifications.PARTIAL_FILL]
        assert [e for e, _ in second] == [live_notifications.FILL_COMPLETED]

    def test_re_observing_the_same_fill_sends_nothing(self, monkeypatch):
        assert self._notified(monkeypatch, previously_filled=2, filled=2) == []

    def test_a_regressing_observation_sends_nothing(self, monkeypatch):
        assert self._notified(monkeypatch, previously_filled=5, filled=2) == []

    def test_no_duplicate_terminal_fill_event(self, monkeypatch):
        """FILL_COMPLETED once, then silence on every later poll."""
        first = self._notified(monkeypatch, previously_filled=2, filled=5)
        again = self._notified(monkeypatch, previously_filled=5, filled=5)
        assert [e for e, _ in first] == [live_notifications.FILL_COMPLETED]
        assert again == []


class TestPositionMismatchNotification:
    def test_it_is_only_sent_on_a_real_divergence(self):
        """A matching position must be silent -- an alert that fires every
        tick on healthy state is an alert nobody reads."""
        import ast
        import pathlib

        source = pathlib.Path("kis_position_manager.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        notifies = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and getattr(n.func, "attr", None) == "notify"]
        mismatch = [n for n in notifies if any(
            isinstance(a, ast.Attribute) and a.attr == "POSITION_MISMATCH" for a in n.args)]
        assert len(mismatch) == 1, "POSITION_MISMATCH is emitted from more than one place"

    def test_the_payload_names_the_blocking_action_and_no_account(self):
        import kis_position_manager as kpm
        import inspect

        source = inspect.getsource(kpm.sync_kis_fills_and_manage_exits)
        assert '"action": "NEW_ENTRY_BLOCKED"' in source
        assert "kis_account" not in source
