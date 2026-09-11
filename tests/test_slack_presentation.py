"""The Korean Slack presentation layer and the channel policy.

What is pinned here
-------------------
* the wording of the final lifecycle messages (매수 체결, 매도 체결, 주문
  취소, 주문 실패, 매수 차단) and the three-way distinction between a
  block (no order was sent), a failure (the broker refused) and a cancel
  (an accepted order was withdrawn);
* the reason-code mapping, its fallback, and that the original code is
  never lost;
* that one normal BUY and one normal SELL lifecycle each produce exactly
  ONE Slack message, with every intermediate event kept as a log line;
* channel routing: critical -> live alerts, infrastructure -> system
  health, the S1-S5 daily summary -> scanner, the daily trading report ->
  trading report, session readiness -> live trading;
* that a BLOCKED session never reads 준비 완료;
* that the durable audit trail (order_state_events) is untouched by any
  of this -- the engine still records every transition it recorded before.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import slack_utils  # noqa: E402
from operations import live_notifications as ln  # noqa: E402
from operations import slack_presentation as sp  # noqa: E402


# ---------------------------------------------------------------------
# Korean messages
# ---------------------------------------------------------------------
class TestBuyFillMessage:
    def test_the_korean_buy_fill(self):
        text = ln._format(ln.FILL_COMPLETED, ln.fill_completed_fields(
            symbol="NVDA", filled_qty=2, fill_price=178.35, position_qty=2,
            average_cost=178.35, strategy_id="S6_ORB_BREAKOUT_V1", session="REGULAR"))
        assert text.splitlines()[0] == "[매수 체결]"
        assert "종목: NVDA" in text
        assert "전략: S6" in text
        assert "세션: 정규장 (REGULAR)" in text
        assert "수량: 2주" in text
        assert "평균 체결가: $178.35" in text
        assert "총 체결금액: $356.70" in text
        assert "상태: 포지션 보유" in text

    def test_unknown_values_print_as_dashes_not_numbers(self):
        text = sp.buy_filled({"symbol": "X"})
        assert "수량: -" in text and "평균 체결가: -" in text and "총 체결금액: -" in text


class TestSellFillMessage:
    def test_the_korean_sell_fill_with_reason_and_code(self):
        text = ln._format(ln.SELL_FILLED, ln.sell_filled_fields(
            symbol="VG", qty=9, fill_price=14.70, realized_pnl=None, realized_pnl_pct=None,
            position_after=0, reason="RANGE_REENTRY", strategy_id="S6_ORB_BREAKOUT_V1",
            session="PREMARKET", average_buy_price=14.89))
        assert text.splitlines()[0] == "[매도 체결]"
        assert "매수가: $14.89" in text and "매도가: $14.70" in text
        assert "실현손익: $-1.71" in text
        assert "수익률: -1.28%" in text
        assert "매도 사유: 돌파 구간 재진입" in text
        assert "원인 코드: RANGE_REENTRY" in text
        assert "상태: 포지션 종료" in text

    def test_a_partial_sell_says_how_much_remains(self):
        text = sp.sell_filled({"symbol": "VG", "qty": 4, "fill_price": 10.0,
                               "average_buy_price": 9.0, "position_after": 5,
                               "reason": "VWAP_FAILURE"})
        assert "일부 청산 (잔여 5주)" in text
        assert "VWAP 하향 이탈" in text


class TestCancelBlockFailureAreDistinct:
    def test_a_cancel_is_an_accepted_order_withdrawn(self):
        text = ln._format(ln.CANCEL_COMPLETED, {
            "symbol": "ABC", "broker_order_id": "0030001", "state": "CANCELLED",
            "side": "buy", "quantity": 2, "reason": "BUY_FILL_TTL_EXPIRED",
            "strategy_id": "S6_ORB_BREAKOUT_V1"})
        assert text.splitlines()[0] == "[매수 주문 취소]"
        assert "상태: 취소 완료" in text
        assert "취소 사유: 매수 미체결 시간 초과" in text
        assert "원인 코드: BUY_FILL_TTL_EXPIRED" in text

    def test_a_partially_filled_cancel_shows_all_three_quantities(self):
        text = sp.order_cancelled({"symbol": "ABC", "side": "buy", "quantity": 5,
                                   "filled_quantity": 2, "reason": "CANDIDATE_GONE"})
        assert "주문수량: 5주" in text
        assert "체결수량: 2주" in text
        assert "취소수량: 3주" in text

    def test_a_block_means_no_order_was_submitted(self):
        text = ln._format(ln.ORDER_BLOCKED, ln.order_blocked_fields(
            symbol="ABC", reason_code="REVALIDATION_SIGNAL_EXPIRED",
            strategy_id="S6_ORB_BREAKOUT_V1", session="REGULAR"))
        assert text.splitlines()[0] == "[매수 차단]"
        assert "상태: 주문 미제출" in text
        assert "사유: 신호 유효시간 초과" in text
        assert "원인 코드: REVALIDATION_SIGNAL_EXPIRED" in text

    def test_a_failure_means_the_broker_was_asked_and_refused(self):
        text = ln._format(ln.ORDER_REJECTED, {
            "symbol": "ABC", "side": "buy", "quantity": 2, "limit_price": 6.4,
            "reason": "KISBrokerError", "strategy_id": "S6_ORB_BREAKOUT_V1"})
        assert text.splitlines()[0] == "[매수 주문 실패]"
        assert "상태: 주문 실패" in text
        assert "사유: 한국투자증권 주문 요청 실패" in text
        assert "원인 코드: KISBrokerError" in text
        assert "주문 미제출" not in text

    def test_the_cancel_context_supplies_the_reason_the_engine_lacks(self):
        sent = []
        with ln.cancel_context(reason="BUY_FILL_TTL_EXPIRED", session="PREMARKET"):
            ln.notify(ln.CANCEL_COMPLETED, {"symbol": "ABC", "broker_order_id": "1",
                                            "state": "CANCELLED", "side": "buy",
                                            "quantity": 1},
                      send_fn=lambda m: sent.append(m) or True, track_health=False)
        assert "매수 미체결 시간 초과" in sent[0]
        assert "프리장 (PREMARKET)" in sent[0]
        # Outside the context the reason is not remembered.
        sent.clear()
        ln.notify(ln.CANCEL_COMPLETED, {"symbol": "ABC", "broker_order_id": "1",
                                        "state": "CANCELLED"},
                  send_fn=lambda m: sent.append(m) or True, track_health=False)
        assert "매수 미체결 시간 초과" not in sent[0]


# ---------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------
class TestReasonMapping:
    @pytest.mark.parametrize("code,korean", [
        ("REVALIDATION_SIGNAL_EXPIRED", "신호 유효시간 초과"),
        ("SOURCE_SIGNAL_TIMESTAMP_UNUSABLE", "신호 생성시각 확인 불가"),
        ("DUPLICATE_BLOCKED", "중복 주문 차단"),
        ("RECONCILIATION_BLOCKED", "계좌 상태 불일치로 주문 차단"),
        ("INSUFFICIENT_CASH", "주문 가능 금액 부족"),
        ("ORDERABLE_CASH_UNAVAILABLE", "주문 가능 금액 조회 실패"),
        ("PRICE_DEVIATION", "현재가 변동 허용범위 초과"),
        ("COMMON_STOCK_REQUIRED", "매수 허용 종목 유형 아님"),
        ("RANGE_REENTRY", "돌파 구간 재진입"),
        ("VWAP_FAILURE", "VWAP 하향 이탈"),
        ("EMA_STRUCTURE_FAILURE", "단기 추세 구조 이탈"),
        ("SESSION_EXIT", "세션 종료 청산"),
        ("SIGNAL_VALIDITY_UNRESOLVED", "신호 유효시간 정책 확인 실패"),
        ("UNKNOWN_RESPONSE", "주문 결과 확인 불가"),
        ("HARD_RISK_CAP", "구조적 손절선 이탈"),
        ("EMERGENCY", "비상 청산"),
        ("VOLUME_DECAY_PRICE_WEAKNESS", "거래량 감소와 가격 약화"),
    ])
    def test_the_required_codes_are_mapped(self, code, korean):
        assert sp.reason_label(code) == (korean, code)

    def test_an_unknown_code_falls_back_and_keeps_the_code(self):
        label, code = sp.reason_label("SOMETHING_NEW_V2")
        assert label == sp.UNMAPPED_REASON
        assert code == "SOMETHING_NEW_V2"
        text = sp.order_blocked({"symbol": "X", "reason_code": "SOMETHING_NEW_V2"})
        assert "사유: 상세 사유를 확인할 수 없습니다." in text
        assert "원인 코드: SOMETHING_NEW_V2" in text

    def test_the_code_survives_a_detail_suffix(self):
        label, code = sp.reason_label("HARD_RISK_CAP: floor 9.9 breached")
        assert code == "HARD_RISK_CAP"
        assert label.startswith("구조적 손절선 이탈")

    def test_the_mapping_never_touches_the_internal_value(self):
        fields = {"symbol": "X", "reason": "RANGE_REENTRY"}
        sp.sell_filled(fields)
        assert fields["reason"] == "RANGE_REENTRY"

    def test_free_text_block_reasons_get_a_code(self):
        assert sp.block_code_for("insufficient KIS orderable cash for even 1 share") == "INSUFFICIENT_CASH"
        assert sp.block_code_for("execution access is held by another cycle") == "EXECUTION_ACCESS_HELD"
        assert sp.block_code_for("something nobody mapped") == "ENTRY_BLOCKED"
        assert sp.block_code_for("KIS rejected the order (rt_cd=7)") == "BROKER_REJECTED_ANNOUNCED"

    def test_how_many_codes_are_mapped(self):
        assert len(sp.REASON_LABELS) >= 50

    def test_execution_liquidity_reason_codes_are_mapped(self):
        # s6_live/execution_liquidity.py's full vocabulary, presented
        # via bare-code order_blocked_fields(reason_code=...), not
        # free-text matching -- confirm every code has a label.
        from s6_live import execution_liquidity as el

        for code in el.REASON_CODES:
            assert code in sp.REASON_LABELS, code

    def test_order_too_large_for_liquidity_free_text_gets_a_code(self):
        assert sp.block_code_for(
            "ORDER_TOO_LARGE_FOR_LIQUIDITY: even 1 share exceeds the "
            "recent-volume cap for RIG") == "ORDER_TOO_LARGE_FOR_LIQUIDITY"


# ---------------------------------------------------------------------
# One lifecycle, one message
# ---------------------------------------------------------------------
class TestOneMessagePerLifecycle:
    def _capture(self):
        sent = []
        return sent, (lambda m: sent.append(m) or True)

    def test_a_normal_buy_produces_exactly_one_message(self):
        sent, send = self._capture()
        sequence = [
            (ln.BUY_CANDIDATE_SELECTED, {"symbol": "NVDA"}),
            (ln.LIVE_ORDER_PREPARED, {"symbol": "NVDA", "side": "buy"}),
            (ln.ORDER_SUBMITTED, {"symbol": "NVDA", "side": "buy", "state": "ACCEPTED"}),
            (ln.ORDER_ACCEPTED, {"symbol": "NVDA", "side": "buy", "state": "ACCEPTED"}),
            (ln.PARTIAL_FILL, {"symbol": "NVDA", "filled_qty": 1, "remaining_qty": 1}),
            (ln.FILL_COMPLETED, {"symbol": "NVDA", "filled_qty": 2, "fill_price": 178.35,
                                 "position_qty": 2, "average_cost": 178.35}),
        ]
        for event, fields in sequence:
            ln.notify(event, fields, send_fn=send, track_health=False)
        assert len(sent) == 1
        assert sent[0].startswith("[매수 체결]")

    def test_a_normal_sell_produces_exactly_one_message(self):
        sent, send = self._capture()
        sequence = [
            (ln.EXIT_TRIGGERED, {"symbol": "NVDA", "reason": "RANGE_REENTRY"}),
            (ln.SELL_SUBMITTED, {"symbol": "NVDA", "side": "sell", "state": "ACCEPTED"}),
            (ln.ORDER_ACCEPTED, {"symbol": "NVDA", "side": "sell", "state": "ACCEPTED"}),
            (ln.SELL_FILLED, {"symbol": "NVDA", "qty": 2, "fill_price": 180.0,
                              "average_buy_price": 178.35, "realized_pnl": 3.3,
                              "realized_pnl_pct": 0.92, "reason": "RANGE_REENTRY"}),
        ]
        for event, fields in sequence:
            ln.notify(event, fields, send_fn=send, track_health=False)
        assert len(sent) == 1
        assert sent[0].startswith("[매도 체결]")

    def test_a_cancelled_buy_produces_only_the_cancel(self):
        sent, send = self._capture()
        for event, fields in [
                (ln.ORDER_SUBMITTED, {"symbol": "ABC", "side": "buy"}),
                (ln.ORDER_ACCEPTED, {"symbol": "ABC", "side": "buy"}),
                (ln.CANCEL_REQUESTED, {"symbol": "ABC", "state": "CANCEL_PENDING"}),
                (ln.CANCEL_COMPLETED, {"symbol": "ABC", "state": "CANCELLED", "side": "buy",
                                       "quantity": 1})]:
            ln.notify(event, fields, send_fn=send, track_health=False)
        assert [m.splitlines()[0] for m in sent] == ["[매수 주문 취소]"]

    def test_a_blocked_buy_produces_only_the_block(self):
        sent, send = self._capture()
        ln.notify(ln.BUY_CANDIDATE_SELECTED, {"symbol": "ABC"}, send_fn=send, track_health=False)
        ln.notify(ln.ORDER_BLOCKED, ln.order_blocked_fields(
            symbol="ABC", reason_code="INSUFFICIENT_CASH"), send_fn=send, track_health=False)
        assert [m.splitlines()[0] for m in sent] == ["[매수 차단]"]

    def test_transient_blocks_are_silent(self):
        sent, send = self._capture()
        for code in sorted(sp.SILENT_BLOCK_CODES):
            ln.notify(ln.ORDER_BLOCKED, ln.order_blocked_fields(symbol="ABC", reason_code=code),
                      send_fn=send, track_health=False)
        assert sent == []

    def test_the_same_fill_seen_by_two_ticks_is_announced_once(self):
        """The notification ledger, on a real state store."""
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            sent, send = self._capture()
            fields = ln.fill_completed_fields(symbol="VG", filled_qty=9, fill_price=14.89,
                                              position_qty=9, average_cost=14.89)
            for _ in range(2):
                ln.notify(ln.FILL_COMPLETED, fields, send_fn=send, track_health=False,
                          dedupe_conn=conn, dedupe_subject="s6pos_1", dedupe_version="OPENED")
            assert len(sent) == 1
        finally:
            conn.close()

    def test_a_failed_send_gives_the_claim_back(self):
        """One webhook hiccup must not swallow a fill message forever."""
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            fields = ln.fill_completed_fields(symbol="VG", filled_qty=9, fill_price=14.89,
                                              position_qty=9, average_cost=14.89)
            assert ln.notify(ln.FILL_COMPLETED, fields, send_fn=lambda m: False,
                             track_health=False, dedupe_conn=conn,
                             dedupe_subject="s6pos_2", dedupe_version="OPENED") is False
            sent = []
            assert ln.notify(ln.FILL_COMPLETED, fields, send_fn=lambda m: sent.append(m) or True,
                             track_health=False, dedupe_conn=conn,
                             dedupe_subject="s6pos_2", dedupe_version="OPENED") is True
            assert len(sent) == 1
        finally:
            conn.close()

    def test_the_same_block_is_announced_once_per_day(self):
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            sent, send = self._capture()
            for _ in range(3):
                ln.notify(ln.ORDER_BLOCKED,
                          ln.order_blocked_fields(symbol="VG", reason_code="INSUFFICIENT_CASH"),
                          send_fn=send, track_health=False, dedupe_conn=conn)
            assert len(sent) == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------
class TestRouting:
    @pytest.fixture
    def webhooks(self, monkeypatch):
        calls = []
        monkeypatch.setattr(slack_utils, "_send",
                            lambda url, message: calls.append((url, message)) or True)
        for role, env in slack_utils.ROLE_WEBHOOK_ENV.items():
            monkeypatch.setenv(env, f"https://hooks.test/{role}")
        return calls

    def test_critical_failures_go_to_live_alerts(self, webhooks):
        for event in sorted(ln.URGENT_EVENTS):
            ln.notify(event, {"symbol": "X"}, track_health=False)
        assert {url for url, _ in webhooks} == {"https://hooks.test/LIVE_ALERTS"}
        assert all(m.startswith("🚨 [") for _, m in webhooks)

    def test_fills_and_blocks_go_to_live_trading(self, webhooks):
        ln.notify(ln.FILL_COMPLETED, {"symbol": "X", "filled_qty": 1, "fill_price": 1.0},
                  track_health=False)
        ln.notify(ln.ORDER_BLOCKED, {"symbol": "X", "reason_code": "INSUFFICIENT_CASH"},
                  track_health=False)
        assert [url for url, _ in webhooks] == ["https://hooks.test/LIVE_TRADING"] * 2

    def test_infrastructure_failures_go_to_system_health(self, webhooks, tmp_path):
        from scanners.notify import slack as scanner_alerts
        from scripts import notify_system_health

        assert scanner_alerts._send("⚠️ [시스템 상태] 스캐너 실행 실패") is True
        notify_system_health.main(["COLLECTOR_RESTART", "HEARTBEAT_STALE", "--no-dedupe"])
        assert {url for url, _ in webhooks} == {"https://hooks.test/SYSTEM_HEALTH"}
        assert any("Collector 자동 재시작" in m for _, m in webhooks)

    def test_the_legacy_fault_alert_goes_to_live_alerts_in_korean(self, webhooks):
        from operations import alerts

        alerts.send_alert("*CRITICAL: trading process fail-stop*\n- stage: x")
        assert [url for url, _ in webhooks] == ["https://hooks.test/LIVE_ALERTS"]
        assert webhooks[0][1].startswith("🚨 [거래 프로세스 비상 정지]")

    def test_the_scanner_daily_summary_goes_only_to_the_scanner_channel(self, webhooks, monkeypatch):
        from scanners.analytics import daily_scanner_summary as dss
        from scripts import run_daily_scanner_summary as script

        monkeypatch.setattr(dss, "build_by_session", lambda day, conn=None: {
            name: {"trading_day": day, "rows": [], "best": None,
                   "failures": [], "total_signals": 0}
            for name in ("PREMARKET", "REGULAR", "AFTER_HOURS", "OVERNIGHT_DAYTIME")})
        script.main(["--trading-day", "2026-09-08"])
        assert [url for url, _ in webhooks] == ["https://hooks.test/SCANNER"]
        assert webhooks[0][1].startswith("[스캐너 세션별 일일 성과]")

    def test_the_daily_trading_report_goes_only_to_the_report_channel(self, webhooks):
        from scripts import run_daily_trading_report as script

        script.main(["--trading-day", "2026-09-08"])
        assert [url for url, _ in webhooks] == ["https://hooks.test/TRADING_REPORT"]
        assert webhooks[0][1].startswith("[일일 거래 리포트]")

    def test_session_readiness_goes_to_live_trading(self, webhooks):
        from operations import session_readiness

        report = {"session": "REGULAR", "trading_day": "2026-09-08", "ready": True,
                  "blocking": [], "warnings": [], "checks": {}, "release": "72421c24"}
        session_readiness.announce(report)
        assert [url for url, _ in webhooks] == ["https://hooks.test/LIVE_TRADING"]
        assert webhooks[0][1].startswith("[정규장 거래 준비 완료]")

    def test_no_live_event_reaches_a_paper_webhook(self):
        forbidden = {slack_utils.send_slack_message, slack_utils.send_slack_alert}
        for event in sorted(ln.EVENTS):
            assert ln._sender_for(event) not in forbidden, event


# ---------------------------------------------------------------------
# Session readiness
# ---------------------------------------------------------------------
class TestSessionReadiness:
    def _health(self, **fails):
        names = ["trading_mode", "release", "kis_token", "kis_account", "reconciliation",
                 "collector:connected", "collector:subscriptions", "kill_switch"]
        return {"checks": [{"name": n, "verdict": fails.get(n, "OK"), "detail": "d"}
                           for n in names]}

    class _Broker:
        def get_account_cash_usd(self):
            return 1250.4

        def get_positions(self):
            return [type("P", (), {"quantity": 9, "symbol": "VG"})()]

        def get_open_orders(self):
            return []

    def test_a_ready_session_reads_as_ready(self, monkeypatch, tmp_path):
        from operations import session_readiness as sr

        universe = tmp_path / "universe.csv"
        universe.write_text("symbol\n" + "\n".join(f"S{i}" for i in range(13409)))
        env = {"KIS_LIVE_ORDER_ENABLED": "true", "DEPLOYED_COMMIT": "72421c24c0c5",
               "SCANNER_UNIVERSE_FILE": str(universe), "SCANNER_DATA_ROOT": str(tmp_path)}
        monkeypatch.setattr(sr, "_session_capable", lambda now, session: True)
        monkeypatch.setattr(sr, "_ranking_fresh", lambda now: True)
        report = sr.build(session="REGULAR", env=env, broker=self._Broker(),
                          health_report=self._health(), trading_day="2026-09-08")
        assert report["ready"] is True
        text = sp.session_ready(report)
        assert text.startswith("[정규장 거래 준비 완료]")
        assert "주문 가능 금액: $1,250.40" in text
        assert "보유 종목: 1개" in text
        assert "Universe: 13,409" in text
        assert "배포 버전: 72421c24" in text

    def test_a_blocked_session_never_says_ready(self, monkeypatch, tmp_path):
        from operations import session_readiness as sr

        monkeypatch.setattr(sr, "_session_capable", lambda now, session: True)
        monkeypatch.setattr(sr, "_ranking_fresh", lambda now: True)
        env = {"KIS_LIVE_ORDER_ENABLED": "true", "SCANNER_DATA_ROOT": str(tmp_path)}
        report = sr.build(session="PREMARKET", env=env, broker=self._Broker(),
                          health_report=self._health(reconciliation="FAIL"),
                          trading_day="2026-09-08")
        assert report["ready"] is False
        text = sp.session_ready(report)
        assert text.startswith("[프리장 거래 준비 실패]")
        assert "거래 준비 완료" not in text
        assert "상태: 거래 차단" in text
        assert "원인 코드: RECONCILIATION_BLOCKED" in text

    def test_every_session_has_a_korean_title(self):
        assert sp.session_title("OVERNIGHT_DAYTIME") == "데이장"
        assert sp.session_title("PREMARKET") == "프리장"
        assert sp.session_title("REGULAR") == "정규장"
        assert sp.session_title("AFTER_HOURS") == "애프터장"


# ---------------------------------------------------------------------
# Daily summaries
# ---------------------------------------------------------------------
class TestDailyScannerSummary:
    def test_it_reports_only_what_was_measured(self):
        from scanners.analytics import daily_scanner_summary as dss

        signals = [{"signal_id": "a", "scanner_name": "accumulation"},
                   {"signal_id": "b", "scanner_name": "accumulation"},
                   {"signal_id": "c", "scanner_name": "gap_pullback"}]
        performance = {"a": {"return_1h": 1.5, "mfe_1h": 3.1, "mae_1h": -0.7},
                       "b": {"return_1h": -0.5, "mfe_1h": 0.2, "mae_1h": -1.0}}
        manifests = [{"outcomes": [{"scanner_name": "accumulation", "symbols_seen": 5960}]}]
        summary = dss.build("2026-09-08", signals=signals, performance=performance,
                            manifests=manifests, conn=None)
        rows = {r["label"]: r for r in summary["rows"]}
        assert rows["S2"]["candidates"] == 2 and rows["S2"]["wins"] == 1 and rows["S2"]["losses"] == 1
        assert rows["S2"]["avg_return"] == pytest.approx(0.5)
        assert rows["S5"]["wins"] is None and rows["S5"]["unmeasured"] == 1
        assert rows["S1"]["candidates"] is None
        assert summary["best"] == "S2"
        text = dss.format_message(summary)
        assert text.startswith("[스캐너 일일 성과]")
        assert "S1\n  데이터 없음" in text
        assert "오늘 최고 성과: S2" in text
        assert "매수 연결: 0 (분석 전용)" in text

    def test_no_data_at_all_names_no_winner(self):
        from scanners.analytics import daily_scanner_summary as dss

        summary = dss.build("2026-09-08", signals=[], performance={}, manifests=[], conn=None)
        assert summary["best"] is None
        assert "오늘 최고 성과: 측정 불가" in dss.format_message(summary)


class TestDailyTradingReport:
    def test_it_counts_positions_not_events(self):
        from operations import daily_trading_report as dtr
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            rows = [
                ("p1", "S6_ORB_BREAKOUT_V1", "VG", 9, 14.89, 14.70, "RANGE_REENTRY", "CLOSED"),
                ("p2", "S6_ORB_BREAKOUT_V1", "SAN", 10, 14.93, 15.10, "VWAP_FAILURE", "CLOSED"),
                ("p3", "S6_ORB_BREAKOUT_V1", "ATAI", None, None, None, "BUY_FILL_TTL_EXPIRED", "CLOSED"),
                ("p4", "S6_ORB_BREAKOUT_V1", "XYL", 1, 106.85, None, None, "OPEN"),
            ]
            for pid, strat, sym, qty, entry, exit_price, reason, status in rows:
                conn.execute(
                    "INSERT INTO s6_positions (position_id, strategy_id, variant, symbol, "
                    "quantity, entry_price, exit_price, exit_reason, status, submitted_at, "
                    "created_at, closed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, strat, "S6-P", sym, qty, entry, exit_price, reason, status,
                     "2026-09-08T09:00:00+00:00", "2026-09-08T09:00:00+00:00",
                     "2026-09-08T10:00:00+00:00" if status == "CLOSED" else None,
                     "2026-09-08T10:00:00+00:00"))
            conn.commit()
            report = dtr.build(conn, "2026-09-08")
        finally:
            conn.close()
        assert report["buy_fills"] == 3
        assert report["sell_fills"] == 2
        assert report["cancelled_entries"] == 1
        assert report["open_positions"] == 1
        assert report["wins"] == 1 and report["losses"] == 1
        assert report["win_rate"] == pytest.approx(50.0)
        assert report["realized_pnl"] == pytest.approx(-1.71 + 1.70, abs=1e-6)
        assert report["best"]["symbol"] == "SAN" and report["worst"]["symbol"] == "VG"
        text = dtr.format_message(report)
        assert text.startswith("[일일 거래 리포트]")
        assert "매수 체결: 3건" in text and "매도 체결: 2건" in text
        assert "승률: 50.00%" in text
        assert "돌파 구간 재진입 (RANGE_REENTRY)" in text


# ---------------------------------------------------------------------
# Safety: the durable trail and the trading path are untouched
# ---------------------------------------------------------------------
class TestDurableBehaviourUnchanged:
    def test_the_engine_still_records_every_transition(self):
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text()
        assert 'event_type="TRANSPORT_SUBMITTING"' in source or "TRANSPORT_SUBMITTING" in source
        assert 'event_type="TRANSPORT_RESULT"' in source
        assert "_notify_submitted(order_intent, side_label=side_label, record=execution_record)" in source

    def test_notify_results_never_steer_control_flow(self):
        """Every production notify() call is a bare statement."""
        import ast

        offenders = []
        for path in [REPO_ROOT / "s6_live" / "exit_runtime.py",
                     REPO_ROOT / "scripts" / "run_live_buy_entry.py",
                     REPO_ROOT / "execution" / "execution_engine.py",
                     REPO_ROOT / "s6_live" / "entry_timeout.py"]:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.If, ast.While, ast.Assert, ast.Return)):
                    for inner in ast.walk(node.test if hasattr(node, "test") else node):
                        if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                                and inner.func.attr == "notify"
                                and isinstance(node, (ast.If, ast.While, ast.Assert))):
                            offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == []

    def test_the_fill_sync_notifies_after_the_durable_write_and_never_raises(self, monkeypatch):
        from s6_live import exit_runtime

        calls = []
        monkeypatch.setattr(ln, "notify", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError("slack down")))
        # A raising notifier must not propagate out of the announce helper.
        exit_runtime._announce_buy_fill(None, {"strategy_id": "S6_ORB_BREAKOUT_V1",
                                              "entry_session": "PREMARKET"},
                                        "p1", "VG", 9, {"average_fill_price": 14.89})
        assert calls and calls[0][0] == ln.FILL_COMPLETED

    def test_the_health_report_is_korean_and_keeps_the_codes(self):
        import trading_health_check as thc

        report = {"now_kst": "2026-09-08 18:00:00 KST", "market_day": True,
                  "checks": [{"name": "trading_mode", "verdict": "OK", "detail": "live (kis)"},
                             {"name": "release", "verdict": "OK", "detail": "abc == VALIDATED"},
                             {"name": "kis_token", "verdict": "WARN", "detail": "expiring"}],
                  "failed": [], "warned": ["kis_token"], "overall": "NORMAL",
                  "performance": {"available": False, "detail": "no db"}}
        text = thc.format_message(report)
        assert text.startswith("📊 [시스템 상태 점검]")
        assert "KIS 토큰: 주의 (WARN) expiring" in text
        assert "종합: 정상 (NORMAL)" in text
        assert "주의 항목: kis_token" in text
