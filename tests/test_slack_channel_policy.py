"""One channel, one responsibility -- and every operator line in Korean.

What production actually showed
-------------------------------
stock-scanner carried "[실거래 매수] ORDER_SUBMITTED" beside scan
analytics; stock-live-trading emitted LIVE_ORDER_PREPARED, SELL_SUBMITTED
and ORDER_ACCEPTED for a single HDB sell; stock-system-health printed
"Market Data: STALE / Reason: STALE_QUOTE" in English; and the scanner's
weekly observation summary went to the trading-report channel reading
half-English. These pin each of those closed.
"""

import ast
from pathlib import Path

import pytest

from operations import live_notifications as ln
from operations import slack_presentation as sp


class TestChannelRouting:
    def test_no_execution_event_can_reach_the_scanner_channel(self):
        """The scanner monitor no longer owns a live-order formatter."""
        from scanners.notify import monitor

        for gone in ("format_buy", "format_fill", "format_sell",
                     "notify_buy", "notify_fill", "notify_sell",
                     "TAG_LIVE_BUY", "TAG_LIVE_FILL", "TAG_LIVE_SELL"):
            assert not hasattr(monitor, gone), gone

    def test_the_scanner_module_names_no_order_lifecycle_event(self):
        source = Path("scanners/notify/monitor.py").read_text(encoding="utf-8")
        # Comments explaining what was removed are not routing. Only real
        # code counts, so strip them before looking.
        code = "\n".join(line for line in source.splitlines()
                          if not line.lstrip().startswith("#"))
        for event in ("ORDER_SUBMITTED", "ORDER_ACCEPTED", "SELL_SUBMITTED",
                      "BUY_SUBMITTED", "LIVE_ORDER_PREPARED"):
            assert event not in code, event

    def test_every_lifecycle_event_routes_to_a_trading_channel(self):
        for event in ln.LIVE_TRADING_EVENTS:
            assert ln.channel_for(event) == "LIVE_TRADING", event
        for event in ln.URGENT_EVENTS:
            assert ln.channel_for(event) == "LIVE_ALERTS", event
        for event in ln.INTERNAL_EVENTS:
            assert ln.channel_for(event) is None, event

    def test_no_trading_channel_is_the_scanner_or_report_channel(self):
        import slack_utils

        roles = slack_utils.ROLE_WEBHOOK_ENV
        assert roles["LIVE_TRADING"] != roles["SCANNER"]
        assert roles["LIVE_TRADING"] != roles["TRADING_REPORT"]
        assert roles["LIVE_ALERTS"] != roles["SCANNER"]
        assert len(set(roles.values())) == len(roles)

    def test_the_weekly_scanner_summary_goes_to_the_scanner_channel(self):
        source = Path("scripts/run_scanner_report.py").read_text(encoding="utf-8")
        block = source[source.index("def _post_weekly_to_slack"):]
        block = block[:block.index("\ndef ")]
        assert "send_scanner_monitor_message" in block
        assert "SLACK_WEBHOOK_URL" not in block


class TestLifecycleIsOneMessage:
    """PREPARED / SUBMITTED / ACCEPTED / PENDING are log-only."""

    SUPPRESSED = ("LIVE_ORDER_PREPARED", "ORDER_SUBMITTED", "ORDER_ACCEPTED",
                  "ORDER_PENDING", "SELL_SUBMITTED", "CANCEL_REQUESTED",
                  "EXIT_TRIGGERED", "PARTIAL_FILL")
    FINAL = ("FILL_COMPLETED", "SELL_FILLED", "CANCEL_COMPLETED",
             "ORDER_REJECTED", "ORDER_BLOCKED")

    def test_intermediate_events_send_nothing(self, monkeypatch):
        sent = []
        for name in self.SUPPRESSED:
            event = getattr(ln, name)
            assert event in ln.INTERNAL_EVENTS, name
            ln.notify(event, {"symbol": "HDB", "quantity": 5},
                      send_fn=lambda message: sent.append(message) or True)
        assert sent == []

    def test_each_final_event_is_exactly_one_message(self):
        for name in self.FINAL:
            event = getattr(ln, name)
            assert event in ln.LIVE_TRADING_EVENTS, name
            assert ln.channel_for(event) == "LIVE_TRADING", name

    def test_one_sell_lifecycle_produces_one_slack_message(self):
        """The HDB case: PREPARED + SELL_SUBMITTED + ACCEPTED + FILLED."""
        sent = []
        send = lambda message: sent.append(message) or True  # noqa: E731
        fields = {"symbol": "HDB", "quantity": 5, "strategy_id": "S6_ORB_BREAKOUT_V1",
                  "session": "REGULAR", "average_fill_price": 22.48}
        for event in (ln.LIVE_ORDER_PREPARED, ln.SELL_SUBMITTED, ln.ORDER_ACCEPTED):
            ln.notify(event, fields, send_fn=send)
        assert sent == []
        ln.notify(ln.SELL_FILLED, fields, send_fn=send)
        assert len(sent) == 1
        assert sent[0].startswith("[매도 체결]")


class TestKoreanPresentation:
    def test_titles_are_korean(self):
        assert sp.buy_filled({"symbol": "HDB"}).startswith("[매수 체결]")
        assert sp.sell_filled({"symbol": "HDB"}).startswith("[매도 체결]")
        assert sp.order_blocked({"symbol": "X"}).startswith("[매수 차단]")
        # side-aware, e.g. "[매수 주문 실패]" -- more specific than the
        # generic form and still distinct from "[매수 차단]"
        assert "주문 실패]" in sp.order_failed({"symbol": "X"}).splitlines()[0]
        assert "주문 취소]" in sp.order_cancelled({"symbol": "X"}).splitlines()[0]

    def test_machine_status_words_are_translated(self):
        # One canonical, context-neutral label: the same token names a
        # stale quote and a stale watchdog subject, and the field label
        # supplies the context.
        assert sp.status_label("STALE") == "오래된 상태"
        assert sp.status_label("FRESH") == "정상"
        assert sp.status_label("NOT_ATTEMPTED") == "시도하지 않음"
        assert sp.status_label("VERIFIED") == "검증 완료"
        assert sp.status_label("BLOCKED") == "차단"
        assert sp.status_label("UNKNOWN") == "확인 불가"
        assert sp.status_label("DISCONNECTED") == "연결 끊김"
        assert sp.status_label("ZERO_CANDIDATE") == "후보 없음"

    def test_an_unmapped_status_is_shown_not_guessed(self):
        assert sp.status_label("SOME_NEW_STATE") == "SOME_NEW_STATE"
        assert sp.status_label(None) == "확인 불가"

    def test_reason_codes_carry_a_korean_sentence_and_the_code(self):
        for code in ("STALE_QUOTE", "OFFICIAL_ORIGIN_NOT_COVERED",
                     "BROKER_REQUEST_FAILED", "UNKNOWN_BROKER_RESULT",
                     "RECONCILIATION_MISMATCH", "KILL_SWITCH_ACTIVE",
                     "DATA_ERROR", "RATE_LIMIT", "ROUTE_UNVERIFIED"):
            label, resolved = sp.reason_label(code)
            assert resolved == code
            assert label and not label.isascii(), code

    def test_an_unknown_code_keeps_the_code_and_says_so_in_korean(self):
        label, code = sp.reason_label("A_BRAND_NEW_CODE")
        assert code == "A_BRAND_NEW_CODE"
        assert label == "상세 사유를 확인할 수 없습니다."

    def test_optional_unavailable_fields_are_omitted_not_printed(self):
        message = sp.buy_filled({"symbol": "HDB", "quantity": 5,
                                 "average_fill_price": 22.74,
                                 "broker_order_id": None,
                                 "cash_result": "unavailable"})
        for noise in ("unavailable", "None", "null", "N/A"):
            assert noise not in message


class TestRouteVerificationReport:
    def _result(self, **over):
        base = {"session": "OVERNIGHT_DAYTIME", "trading_day": "2026-09-09",
                "mode": "서버 자동 검증", "market": "STALE",
                "buy": "NOT_ATTEMPTED", "cancel": "NOT_ATTEMPTED",
                "sell": "VERIFIED", "position": "UNKNOWN",
                "open_orders": "UNKNOWN", "route_state": "BLOCKED",
                "reason_code": "STALE_QUOTE"}
        base.update(over)
        return base

    def test_it_reads_in_korean_and_keeps_the_code(self):
        message = sp.route_verification(self._result())
        assert message.startswith("[데이장 주문 경로 검증]")
        for line in ("시장 데이터: 오래된 상태", "매수 경로: 시도하지 않음",
                     "취소 경로: 시도하지 않음", "매도 경로: 검증 완료",
                     "데이장 거래 상태: 차단", "원인 코드: STALE_QUOTE"):
            assert line in message, line
        assert "STALE\n" not in message
        assert "NOT_ATTEMPTED" not in message

    def test_fields_the_run_could_not_answer_are_omitted(self):
        message = sp.route_verification(self._result(position=None, open_orders=""))
        assert "최종 보유수량" not in message
        assert "미체결 주문" not in message

    def test_the_entry_point_renders_without_sending(self, capsys):
        import scripts.notify_route_verification as notifier

        assert notifier.main(["--print-only", "--market", "FRESH",
                              "--sell", "VERIFIED", "--route-state", "ALLOWED"]) == 0
        out = capsys.readouterr().out
        assert "시장 데이터: 정상" in out
        assert "매도 경로: 검증 완료" in out


class TestScannerReportIsKorean:
    def test_the_weekly_summary_has_no_english_operator_text(self):
        from scanners.analytics import weekly_report

        message = weekly_report.format_slack(
            {"start_day": "2026-08-31", "end_day": "2026-09-06",
             "total_signals": 0, "trading_days": [], "hit_horizon": "return_1d",
             "scanners": []}, run_health={"runs": 0})
        assert message.startswith("[S1~S5 스캐너 주간 리포트]")
        for gone in ("Month 1", "hit horizon", "Candidate Decision", "Scanner 주간 요약"):
            assert gone not in message, gone
        assert "성과 측정 기준: 신호 발생 후 1일 수익률" in message
        assert "관측 단계: 1개월차" in message


class TestSessionReadiness:
    def _report(self, session, ready=True, blocking=None):
        return {"session": session, "ready": ready, "trading_day": "2026-09-09",
                "release": "72421c24c0c5", "checks": {}, "blocking": blocking or [],
                "s6": {"live_orb_minutes": 5, "orders_allowed": True,
                       "fast_watch_active": True, "shadow_orb_minutes": 15,
                       "session_data": "OK" if ready else "NOT_COVERED"}}

    @pytest.mark.parametrize("session,title", [
        ("OVERNIGHT_DAYTIME", "데이장"), ("PREMARKET", "프리장"),
        ("REGULAR", "정규장"), ("AFTER_HOURS", "애프터장")])
    def test_each_session_announces_in_korean(self, session, title):
        message = sp.session_ready(self._report(session))
        assert message.startswith(f"[{title} 거래 준비 완료]")
        assert "S6: ORB5 실거래" in message
        assert "세션 데이터: 정상" in message

    def test_a_blocked_session_never_says_ready(self):
        message = sp.session_ready(self._report(
            "REGULAR", ready=False,
            blocking=[{"code": "OFFICIAL_ORIGIN_NOT_COVERED", "detail": None}]))
        assert message.startswith("[정규장 거래 준비 실패]")
        assert "준비 완료" not in message
        assert "사유: 세션 시작 구간 데이터를 확인할 수 없습니다." in message
        assert "원인 코드: OFFICIAL_ORIGIN_NOT_COVERED" in message
        assert "세션 데이터: 준비 실패" in message


class TestSlackNeverSteersTrading:
    def test_no_notification_module_imports_an_execution_module(self):
        for path in ("operations/slack_presentation.py",
                     "scanners/notify/monitor.py",
                     "scripts/notify_route_verification.py"):
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)
            forbidden = ("brokers", "risk", "execution.execution_engine",
                         "execution.order_gate", "execution.risk_gate")
            assert not [m for m in modules if m.startswith(forbidden)], path

    def test_a_raising_sender_never_escapes_notify(self):
        def boom(message):
            raise RuntimeError("slack is down")

        assert ln.notify(ln.FILL_COMPLETED, {"symbol": "HDB"},
                         send_fn=boom) is False


# --- stock-live-alerts: the Korean guarantee ---------------------------------

#: Every URGENT payload production actually emits, keyed by the call site.
#: Kept literal so a new field in a caller fails HERE rather than in Slack.
PRODUCTION_CRITICAL_PAYLOADS = {
    "WATCHDOG_ESCALATED": {
        "status": "STALE", "detail": "-", "symbol": "XYZ",
        "silent_minutes": 45, "kill_switch": "ENTRY_DISABLED",
        "sell_path": "유지"},
    "DB_FAILURE": {
        "symbol": "XYZ", "reason": "POSITION_TRACKING_FAILED",
        "detail": "sqlite3.OperationalError: locked"},
    "DB_FAILURE_ENGINE": {
        "stage": "SUBMITTING", "symbol": "XYZ",
        "consequence": "order state unrecorded"},
    "KILL_SWITCH_ACTIVATED": {
        "state": "ENTRY_DISABLED", "reason": "KILL_SWITCH_ACTIVE",
        "source": "watchdog", "new_entries_blocked": True,
        "previous_state": "ACTIVE", "incident_id": "inc_01"},
    "POSITION_MISMATCH": {
        "symbol": "XYZ", "kis_qty": 5, "local_qty": 3,
        "reconciliation_state": "UNKNOWN", "action": "HOLD"},
    "ORDER_UNKNOWN": {
        "symbol": "XYZ", "side": "buy", "quantity": 5, "limit_price": 22.7,
        "broker_order_id": "003", "internal_order_id": "int_1",
        "durable_state": "SUBMITTED"},
    "CANCEL_FAILED": {
        "symbol": "XYZ", "broker_order_id": "003", "reason": "CANCEL_REJECTED",
        "durable_state": "ACCEPTED"},
    "HALT_ACTIVATED": {
        "reason": "RECONCILIATION_MISMATCH", "source": "reconciler",
        "new_entries_blocked": True, "note": "manual review"},
    "RECONCILIATION_MISMATCH": {
        "symbol": "XYZ", "reason": "RECONCILIATION_MISMATCH",
        "action": "RECONCILE"},
    "KIS_API_FAILURE": {"symbol": "XYZ", "reason": "KIS_API_FAILURE"},
}

#: The raw keys that reached production Slack before this was fixed.
FORBIDDEN_RAW_LABELS = ("status:", "detail:", "silent_minutes:", "kill_switch:",
                        "sell_path:", "stage:", "consequence:", "note:",
                        "reason_code:", "action:", "state:")


class TestLiveAlertsAreKorean:
    def _event(self, name):
        return getattr(ln, name.replace("_ENGINE", ""))

    @pytest.mark.parametrize("name", sorted(PRODUCTION_CRITICAL_PAYLOADS))
    def test_no_production_alert_prints_a_raw_english_label(self, name):
        text = sp.critical(self._event(name), PRODUCTION_CRITICAL_PAYLOADS[name])
        for raw in FORBIDDEN_RAW_LABELS:
            assert raw not in text, (name, raw)

    @pytest.mark.parametrize("name", sorted(PRODUCTION_CRITICAL_PAYLOADS))
    def test_every_operator_line_carries_korean(self, name):
        import re

        text = sp.critical(self._event(name), PRODUCTION_CRITICAL_PAYLOADS[name])
        assert re.search(r"[가-힣]", text.splitlines()[0])
        for line in text.splitlines()[1:]:
            if not line.strip():
                continue
            label = line.split(":", 1)[0]
            # The LABEL is always Korean. The value may be technical text
            # (an exception message, a broker id) and stays as written.
            assert re.search(r"[가-힣]", label), (name, line)

    @pytest.mark.parametrize("name", sorted(PRODUCTION_CRITICAL_PAYLOADS))
    def test_every_production_field_has_a_label(self, name):
        """No production field may fall into the 추가 정보 bucket."""
        text = sp.critical(self._event(name), PRODUCTION_CRITICAL_PAYLOADS[name])
        assert "추가 정보" not in text, name

    def test_an_unlabelled_field_never_leaks_its_key(self):
        text = sp.critical(ln.DB_FAILURE, {"symbol": "X", "some_new_key": "boom"})
        assert "some_new_key" not in text
        assert "추가 정보: boom" in text

    def test_status_values_use_the_shared_mapping(self):
        text = sp.critical(ln.WATCHDOG_ESCALATED, {"status": "STALE"})
        assert "상태: 오래된 상태" in text
        assert sp.status_label("FRESH") == "정상"
        assert sp.status_label("ENTRY_DISABLED") == "신규 진입 차단"
        assert sp.status_label("VERIFIED") == "검증 완료"
        assert sp.status_label("BLOCKED") == "차단"
        assert sp.status_label("UNKNOWN") == "확인 불가"

    def test_booleans_are_yes_or_no(self):
        text = sp.critical(ln.KILL_SWITCH_ACTIVATED,
                           {"new_entries_blocked": True, "state": "ENTRY_DISABLED"})
        assert "신규 진입 차단: 예" in text
        assert "True" not in text
        off = sp.critical(ln.KILL_SWITCH_ACTIVATED, {"new_entries_blocked": False})
        assert "신규 진입 차단: 아니오" in off
        assert "False" not in off

    @pytest.mark.parametrize("action,korean", [
        ("HOLD", "유지"), ("RETRY", "재시도"), ("HALT", "거래 중지"),
        ("RECONCILE", "상태 재확인")])
    def test_known_actions_are_translated(self, action, korean):
        text = sp.critical(ln.POSITION_MISMATCH, {"symbol": "X", "action": action})
        assert f"조치: {korean}" in text

    def test_an_unknown_action_is_shown_not_guessed(self):
        text = sp.critical(ln.POSITION_MISMATCH, {"symbol": "X", "action": "NEW_VERB"})
        assert "조치: NEW_VERB" in text

    def test_a_technical_detail_keeps_its_text_behind_a_korean_label(self):
        text = sp.critical(ln.DB_FAILURE,
                           {"symbol": "X", "detail": "sqlite3.OperationalError: locked"})
        assert "상세: sqlite3.OperationalError: locked" in text

    def test_the_reason_keeps_both_halves(self):
        text = sp.critical(ln.HALT_ACTIVATED, {"reason": "A_BRAND_NEW_CODE"})
        assert "사유: 상세 사유를 확인할 수 없습니다." in text
        assert "원인 코드: A_BRAND_NEW_CODE" in text

    def test_the_formatter_never_raises_into_the_caller(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("boom")

        # notify() swallows a formatting failure; nothing reaches trading.
        assert ln.notify(ln.DB_FAILURE, {"symbol": Hostile()},
                         send_fn=lambda m: True) is False


class TestLegacyModulesAreNotScheduled:
    """Documented as legacy/inactive, not refactored in this task."""

    LEGACY = ("order_monitor", "daily_pipeline", "daily_candidate_scanner",
              "slack_report", "run_manual_watchlist")

    def test_no_release_cron_script_invokes_a_legacy_slack_module(self):
        from pathlib import Path

        for script in Path("deploy/cron").glob("*.sh"):
            text = script.read_text(encoding="utf-8")
            for name in self.LEGACY:
                assert name not in text, (script.name, name)
