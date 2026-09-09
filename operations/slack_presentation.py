"""Korean presentation of live-trading Slack messages. Display only.

What this module is
-------------------
Every line an operator reads in Slack about the KIS live account is
worded here: the title, the field labels, the Korean description of a
reason code, and the channel role the message belongs to. Nothing here
decides anything about trading. The inputs are the payload dicts the
lifecycle already produces (`live_notifications.*_fields`) and the
outputs are strings.

The boundary it defends
-----------------------
Internal identifiers are never translated. `RANGE_REENTRY` stays
`RANGE_REENTRY` in the position row, the exit ledger, the audit trail
and the `원인 코드:` line; only the `사유:` line says "돌파 구간 재진입".
A reason this table does not know is printed as `상세 사유 미정` with its
original code beside it, so an untranslated code is legible and never
mistaken for a missing event.

Channel roles
-------------
Five channels, five jobs. A message is assigned a ROLE here and
`slack_utils` owns which webhook a role maps to:

    LIVE_TRADING    stock-live-trading   fills, cancels, failures, blocks,
                                         session readiness
    LIVE_ALERTS     stock-live-alerts    UNKNOWN responses, mismatches,
                                         kill switch, failed recovery
    SCANNER         stock-sanner         the daily S1-S5 summary only
    SYSTEM_HEALTH   stock-system-health  infrastructure
    TRADING_REPORT  sotck-trading-report the daily trading report only

Formatting rules
----------------
Money is printed as `$1,234.56`; percentages with a sign and two
decimals; quantities as `N주`. A value that is not known prints `-`,
never an invented number.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# -- channel roles ---------------------------------------------------------
LIVE_TRADING = "LIVE_TRADING"
LIVE_ALERTS = "LIVE_ALERTS"
SCANNER = "SCANNER"
SYSTEM_HEALTH = "SYSTEM_HEALTH"
TRADING_REPORT = "TRADING_REPORT"

CHANNEL_ROLES = (LIVE_TRADING, LIVE_ALERTS, SCANNER, SYSTEM_HEALTH, TRADING_REPORT)

#: Slack channel each role is expected to reach. Documentation for the
#: operator; the webhook itself is configured by environment variable
#: (see slack_utils.ROLE_WEBHOOK_ENV).
CHANNEL_NAMES = {
    LIVE_TRADING: "stock-live-trading",
    LIVE_ALERTS: "stock-live-alerts",
    SCANNER: "stock-sanner",
    SYSTEM_HEALTH: "stock-system-health",
    TRADING_REPORT: "sotck-trading-report",
}

# -- reason codes ----------------------------------------------------------
#: Internal reason code -> Korean description. The code is never replaced;
#: it is printed on its own `원인 코드:` line beside the description.
REASON_LABELS: Dict[str, str] = {
    # entry gates (no broker order was submitted)
    "REVALIDATION_SIGNAL_EXPIRED": "신호 유효시간 초과",
    "SOURCE_SIGNAL_TIMESTAMP_UNUSABLE": "신호 생성시각 확인 불가",
    "SIGNAL_VALIDITY_UNRESOLVED": "신호 유효시간 정책 확인 실패",
    "DUPLICATE_BLOCKED": "중복 주문 차단",
    "RECONCILIATION_BLOCKED": "계좌 상태 불일치로 주문 차단",
    "INSUFFICIENT_CASH": "주문 가능 금액 부족",
    "ORDERABLE_CASH_UNAVAILABLE": "주문 가능 금액 조회 실패",
    "PRICE_DEVIATION": "현재가 변동 허용범위 초과",
    "COMMON_STOCK_REQUIRED": "매수 허용 종목 유형 아님",
    "SECURITY_TYPE_REFUSED": "매수 허용 종목 유형 아님",
    "ROUTE_UNVERIFIED": "주문 경로 실거래 미검증",
    "KILL_SWITCH": "킬 스위치 작동",
    "REVALIDATION_HALT": "거래 중단(HALT) 상태",
    "REVALIDATION_ENTRY_OFF": "신규 진입 비활성",
    "REVALIDATION_ENTRY_DISABLED_ENV": "환경 설정으로 신규 진입 비활성",
    "REVALIDATION_EXIT_IN_FLIGHT": "동일 종목 청산 진행 중",
    "REVALIDATION_SYMBOL_HELD": "이미 보유 중인 종목",
    "REVALIDATION_STATE_UNREADABLE": "계좌 상태 확인 불가",
    "EXECUTION_ACCESS_HELD": "실행 잠금 사용 중 (다음 주기 재시도)",
    "ENTRY_BLOCKED": "진입 조건 미충족",
    "MARKET_DATA_FRESH": "시세 데이터 신선도 미달",
    "STALE_QUOTE": "시세 정지",
    # S6 entry quality (s6_live/entry_quality.py): momentum freshness
    "S6_BREAKOUT_STALE": "돌파 후 시간이 너무 경과함",
    "S6_RECENT_VOLUME_WEAK": "최근 거래량이 진입 기준에 미달",
    "S6_VOLUME_DECAY": "최근 거래량이 빠르게 감소함",
    "S6_SESSION_HIGH_STALE": "최근 고점 갱신이 오래됨",
    "S6_MOMENTUM_WEAKENING": "최근 상승 모멘텀이 약화됨",
    "S6_PREMARKET_LIQUIDITY_WEAK": "프리장 유동성이 부족함",
    "S6_QUALITY_UNAVAILABLE": "진입 품질 지표를 계산할 수 없음",
    "ENTRY_QUALITY": "진입 품질 기준 미충족",
    "PRICE_CHECK_FAILED": "현재가 재확인 실패",
    "ACCOUNT_READ_FAILED": "계좌 조회 실패",
    "OPEN_ORDERS_READ_FAILED": "미체결 주문 조회 실패",
    "POSITION_TRACKING_FAILED": "매수 후 포지션 기록 실패",
    # broker submission actually failed
    "BROKER_SUBMIT_FAILED": "한국투자증권 주문 요청 실패",
    "KISBrokerError": "한국투자증권 주문 요청 실패",
    "KISOrderRejected": "한국투자증권 주문 거부",
    "UNKNOWN_RESPONSE": "주문 결과 확인 불가",
    # cancels
    "BUY_FILL_TTL_EXPIRED": "매수 미체결 시간 초과",
    "BUY_NEVER_FILLED": "매수 주문 미체결 종료",
    "CANDIDATE_GONE": "후보 소멸",
    "SESSION_NOT_ORDERABLE": "세션 종료로 주문 불가",
    "ROUTE_VERIFICATION": "주문 경로 검증 주문",
    # exits
    "HARD_RISK_CAP": "구조적 손절선 이탈",
    "HARD_STOP": "최대 손실 제한",
    "RANGE_REENTRY": "돌파 구간 재진입",
    "VWAP_FAILURE": "VWAP 하향 이탈",
    "EMA_STRUCTURE_FAILURE": "단기 추세 구조 이탈",
    "STRUCTURE_FAILURE": "가격 구조 붕괴",
    "SESSION_EXIT": "세션 종료 청산",
    "EMERGENCY": "비상 청산",
    "EMERGENCY_LIQUIDATION": "비상 청산",
    "VOLUME_DECAY_PRICE_WEAKNESS": "거래량 감소와 가격 약화",
    "VOLUME_DECAY": "거래량 모멘텀 감소",
    "PEAK_GIVEBACK": "고점 대비 되돌림",
    "S1_HARD_STOP": "최대 손실 제한",
    "S1_PROTECTIVE_STOP": "보호 손절",
    "S1_TREND_BREAKDOWN": "추세 이탈",
    "S1_TIME_EXIT": "보유 기간 만료",
    "PARTIAL_TARGET_1": "1차 목표가 부분 청산",
    # faults
    "TRANSPORT_REJECTED": "브로커 전송 거부",
    "DB_FAILURE": "데이터베이스 기록 실패",
    "KIS_API_FAILURE": "한국투자증권 API 호출 실패",
}

#: Printed when a code is not in the table. The code itself always
#: travels on the next line, so nothing is lost.
UNMAPPED_REASON = "상세 사유를 확인할 수 없습니다."

#: Operator-facing causes that appear OUTSIDE the order lifecycle --
#: session health, data coverage, provider faults. Written as sentences
#: because they are read as an explanation, not as a label.
REASON_LABELS.update({
    "STALE_QUOTE": "최근 시세가 갱신되지 않았습니다.",
    "OFFICIAL_ORIGIN_NOT_COVERED": "세션 시작 구간 데이터를 확인할 수 없습니다.",
    "DATA_ORIGIN_UNAVAILABLE": "세션 시작 구간 데이터를 확인할 수 없습니다.",
    "DATA_ERROR": "시장 데이터 처리 중 오류가 발생했습니다.",
    "RATE_LIMIT": "시세 조회 요청 한도에 도달했습니다.",
    "BROKER_REQUEST_FAILED": "증권사 주문 요청 처리에 실패했습니다.",
    "UNKNOWN_BROKER_RESULT": "증권사 주문 결과를 확정하지 못했습니다.",
    "RECONCILIATION_MISMATCH": "주문/포지션 상태가 일치하지 않습니다.",
    "POSITION_MISMATCH": "보유 수량이 증권사 잔고와 일치하지 않습니다.",
    "KILL_SWITCH_ACTIVE": "안전 중지 기능이 활성화되어 있습니다.",
    "ORDER_EXPIRED": "주문 유효 조건이 종료되었습니다.",
    "NO_SIGNAL": "신호가 발생하지 않았습니다.",
    "ZERO_CANDIDATE": "조건을 만족하는 후보가 없었습니다.",
})

#: Machine STATUS WORDS, as an operator reads them. Distinct from
#: REASON_LABELS: a reason explains why something happened, a status names
#: what state a thing is in. `status_label` falls back to the original
#: token so an unmapped state is visible rather than silently dropped.
STATUS_LABELS = {
    "STALE": "오래된 상태",
    "FRESH": "정상",
    "ENTRY_DISABLED": "신규 진입 차단",
    "ENTRY_ENABLED": "신규 진입 허용",
    "ACTIVE": "정상 동작",
    "HALTED": "거래 중지",
    "OK": "정상",
    "NOT_ATTEMPTED": "시도하지 않음",
    "VERIFIED": "검증 완료",
    "PENDING": "대기",
    "FAILED": "실패",
    "BLOCKED": "차단",
    "ALLOWED": "허용",
    "ACCEPTED": "주문 접수 완료",
    "SUBMITTED": "주문 전송",
    "FILLED": "체결 완료",
    "CANCELLED": "주문 취소",
    "CANCELED": "주문 취소",
    "REJECTED": "주문 거부",
    "UNKNOWN": "확인 불가",
    "CONNECTED": "연결됨",
    "DISCONNECTED": "연결 끊김",
    "CONNECTED_ACTIVE": "연결됨 (수신 중)",
    "CONNECTED_NO_TRADES": "연결됨 (체결 없음)",
    "NO_SIGNAL": "신호 없음",
    "ZERO_CANDIDATE": "후보 없음",
    "ENABLED": "사용",
    "DISABLED": "사용 안 함",
    "TRUE": "예",
    "FALSE": "아니오",
}


def status_label(value, *, default=None) -> str:
    """A machine status word in Korean, or the token itself.

    An unmapped status is shown as it is rather than replaced by a guess:
    an operator can act on a code they can search for, and cannot act on
    a wrong translation.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return default if default is not None else "확인 불가"
    return STATUS_LABELS.get(text.upper(), text)


#: Block codes that describe a transient or purely internal condition.
#: They are logged and ledgered but not presented -- a symbol that is
#: already held is re-offered by the funnel every minute, and the
#: execution lock is contended on every tick a position is held.
SILENT_BLOCK_CODES = frozenset({
    "EXECUTION_ACCESS_HELD", "REVALIDATION_SYMBOL_HELD",
    "BROKER_REJECTED_ANNOUNCED", "BROKER_UNKNOWN_ANNOUNCED",
})

#: Free-text block reasons the entry runner logs, mapped to a code so the
#: message carries something an operator can grep. Matched by substring.
_BLOCK_TEXT_TO_CODE: Tuple[Tuple[str, str], ...] = (
    ("insufficient KIS orderable cash", "INSUFFICIENT_CASH"),
    ("orderable-amount read unusable", "ORDERABLE_CASH_UNAVAILABLE"),
    ("orderable cash", "INSUFFICIENT_CASH"),
    ("execution access is held", "EXECUTION_ACCESS_HELD"),
    ("became live in the canonical store", "REVALIDATION_SYMBOL_HELD"),
    ("already held", "REVALIDATION_SYMBOL_HELD"),
    ("reached the broker while this", "REVALIDATION_EXIT_IN_FLIGHT"),
    ("exceeded", "REVALIDATION_SIGNAL_EXPIRED"),
    ("signal expired", "REVALIDATION_SIGNAL_EXPIRED"),
    ("operations HALT was set", "REVALIDATION_HALT"),
    ("ENTRY_OFF was set", "REVALIDATION_ENTRY_OFF"),
    ("ENTRY_DISABLED", "REVALIDATION_ENTRY_DISABLED_ENV"),
    ("could not be re-read", "REVALIDATION_STATE_UNREADABLE"),
    ("duplicate", "DUPLICATE_BLOCKED"),
    ("reconciliation", "RECONCILIATION_BLOCKED"),
    ("kill switch", "KILL_SWITCH"),
    ("common stock", "COMMON_STOCK_REQUIRED"),
    ("ROUTE_UNVERIFIED", "ROUTE_UNVERIFIED"),
    ("no KIS order route is available", "ROUTE_UNVERIFIED"),
    ("execution-price check failed", "PRICE_DEVIATION"),
    ("KIS price re-check failed", "PRICE_CHECK_FAILED"),
    ("KIS account read failed", "ACCOUNT_READ_FAILED"),
    ("KIS open-orders read failed", "OPEN_ORDERS_READ_FAILED"),
    # These two are broker outcomes the engine has already announced as
    # 주문 실패 / 주문 결과 확인 불가; the funnel must not repeat them as 차단.
    ("KIS rejected the order", "BROKER_REJECTED_ANNOUNCED"),
    ("KIS did not confirm the order", "BROKER_UNKNOWN_ANNOUNCED"),
    ("position tracking failed after successful buy", "POSITION_TRACKING_FAILED"),
)

# -- sessions --------------------------------------------------------------
#: Session titles as the operator names them. The enum still prints
#: beside the Korean because it is what the logs say.
SESSION_TITLES = {
    "OVERNIGHT_DAYTIME": "데이장",
    "DAYTIME": "데이장",
    "PREMARKET": "프리장",
    "REGULAR": "정규장",
    "AFTER_HOURS": "애프터장",
    "AFTERMARKET": "애프터장",
}

STRATEGY_NUMBERS = {
    "S1_HMA_EARLY_TREND_V1": "S1",
    "S2_VOLUME_ACCUMULATION_V1": "S2",
    "S6_ORB_BREAKOUT_V1": "S6",
    "s6_orb_breakout": "S6",
    "hma_early_trend": "S1",
    "accumulation": "S2",
    "orb": "S6",
}


# -- lookups ---------------------------------------------------------------

def reason_label(code) -> Tuple[str, str]:
    """(Korean description, original code). Never loses the code.

    A `FAILED: detail`-shaped value is split so the detail survives as
    part of the description; the code keeps only the head.
    """
    if code is None or str(code).strip() == "":
        return UNMAPPED_REASON, "-"
    text = str(code).strip()
    head, sep, tail = text.partition(":")
    key = head.strip()
    label = REASON_LABELS.get(key)
    if label is None:
        # A Python exception class name may arrive as the reason.
        label = REASON_LABELS.get(key.split(".")[-1])
    if label is None:
        return UNMAPPED_REASON, text
    if sep and tail.strip():
        return f"{label} ({tail.strip()})", key
    return label, key


def block_code_for(reason_text) -> str:
    """A reason code for a free-text block reason, or ENTRY_BLOCKED."""
    text = str(reason_text or "")
    if text.isupper() and " " not in text and text in REASON_LABELS:
        return text
    lowered = text.lower()
    for needle, code in _BLOCK_TEXT_TO_CODE:
        if needle.lower() in lowered:
            return code
    return "ENTRY_BLOCKED"


def strategy_label(value) -> str:
    """"S6" from a strategy id, a scanner name or a source name."""
    if value is None:
        return "-"
    text = str(value)
    if text in STRATEGY_NUMBERS:
        return STRATEGY_NUMBERS[text]
    head = text.split("_")[0].split("-")[0].upper()
    if len(head) == 2 and head[0] == "S" and head[1].isdigit():
        return head
    return text


def session_label(value, *, with_code: bool = True) -> str:
    if value is None:
        return "-"
    text = str(value).upper()
    korean = SESSION_TITLES.get(text)
    if korean is None:
        return text
    return f"{korean} ({text})" if with_code else korean


def session_title(value) -> str:
    return SESSION_TITLES.get(str(value or "").upper(), str(value or "세션"))


# -- value formatting --------------------------------------------------------

def money(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"${number:,.2f}"


def percent(value, *, signed: bool = True) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:+.2f}%" if signed else f"{number:.2f}%"


def shares(value) -> str:
    try:
        return f"{int(value)}주"
    except (TypeError, ValueError):
        return "-"


def count(value, suffix="") -> str:
    try:
        return f"{int(value):,}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _first(fields: Dict[str, Any], *names, default=None):
    for name in names:
        value = fields.get(name)
        if value is not None and value != "":
            return value
    return default


def _side(fields: Dict[str, Any], default="buy") -> str:
    text = str(fields.get("side") or default).strip().lower()
    return "sell" if text == "sell" else "buy"


def _side_word(side: str) -> str:
    return "매도" if side == "sell" else "매수"


def _notional(quantity, price):
    try:
        return float(quantity) * float(price)
    except (TypeError, ValueError):
        return None


def _join(lines: Iterable[str]) -> str:
    return "\n".join(lines)


# -- message builders --------------------------------------------------------
# Each takes the payload dict a lifecycle caller already produces and
# returns the complete Slack text. Missing keys print as `-`.

def buy_filled(fields: Dict[str, Any]) -> str:
    quantity = _first(fields, "filled_qty", "quantity", "qty")
    price = _first(fields, "average_fill_price", "fill_price", "average_cost")
    notional = _first(fields, "notional") or _notional(quantity, price)
    remaining = _first(fields, "position_qty")
    state = "포지션 보유"
    if remaining is not None:
        try:
            state = "포지션 보유" if int(remaining) > 0 else "포지션 없음"
        except (TypeError, ValueError):
            pass
    lines = ["[매수 체결]", "",
             f"종목: {fields.get('symbol', '-')}",
             f"전략: {strategy_label(_first(fields, 'strategy_id', 'strategy', 'source'))}",
             f"세션: {session_label(_first(fields, 'session', 'entry_session'))}"]
    if fields.get("orb_minutes") is not None:
        lines.append(f"ORB: {fields['orb_minutes']}분")
    lines += [f"수량: {shares(quantity)}",
             f"평균 체결가: {money(price)}",
             f"총 체결금액: {money(notional)}",
             f"상태: {state}"]
    order_id = _first(fields, "broker_order_id", "kis_order")
    if order_id and str(order_id) != "unknown":
        lines.append(f"주문번호: {order_id}")
    if fields.get("breakout_age_minutes") is not None:
        lines.append(f"돌파 후 경과: {_minutes(fields['breakout_age_minutes'])}")
    if fields.get("recent_volume_state"):
        lines.append(f"최근 거래량: {fields['recent_volume_state']}")
    lines += _fill_diagnostics(fields)
    return _join(lines)


#: Provenance a fill notice may carry (s6_live.fill_notice): the two
#: timestamps whose gap explained the DT post-mortem, the entry state,
#: and one line per gate. Printed after the money lines, only when
#: present, under Korean labels; a gate that was UNAVAILABLE stays visible
#: as its own line rather than hiding in a summary word.
_DIAGNOSTIC_LABELS = (
    ("candidate_generated_at", "후보 생성시각"),
    ("market_data_asof", "시세 기준시각"),
    ("candidate_rank", "후보 순위"),
    ("candidate_score", "후보 점수"),
    ("entry_state", "진입 상태"),
)


def _fill_diagnostics(fields: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for key, label in _DIAGNOSTIC_LABELS:
        value = fields.get(key)
        if value is not None and value != "":
            lines.append(f"{label}: {value}")
    gates = [(k, v) for k, v in fields.items() if str(k).startswith("gate_")]
    if gates:
        for key, value in gates:
            lines.append(f"게이트 {str(key)[5:].upper()}: {value}")
    elif fields.get("gates"):
        lines.append(f"게이트: {fields['gates']}")
    return lines


def sell_filled(fields: Dict[str, Any]) -> str:
    quantity = _first(fields, "qty", "filled_qty", "quantity")
    entry = _first(fields, "average_buy_price", "entry_price", "average_cost")
    exit_price = _first(fields, "fill_price", "exit_price", "average_fill_price")
    pnl = _first(fields, "realized_pnl")
    pct = _first(fields, "realized_pnl_pct")
    if pnl is None and entry is not None and exit_price is not None and quantity:
        try:
            pnl = (float(exit_price) - float(entry)) * int(quantity)
            pct = (float(exit_price) / float(entry) - 1.0) * 100.0 if float(entry) else None
        except (TypeError, ValueError):
            pnl, pct = None, None
    label, code = reason_label(_first(fields, "reason", "exit_reason"))
    remaining = _first(fields, "position_after", "remaining_qty")
    state = "포지션 종료"
    try:
        if remaining is not None and int(remaining) > 0:
            state = f"일부 청산 (잔여 {int(remaining)}주)"
    except (TypeError, ValueError):
        pass
    lines = ["[매도 체결]", "",
             f"종목: {fields.get('symbol', '-')}",
             f"전략: {strategy_label(_first(fields, 'strategy_id', 'strategy', 'source'))}",
             f"세션: {session_label(_first(fields, 'session', 'exit_session'))}",
             f"수량: {shares(quantity)}",
             f"매수가: {money(entry)}",
             f"매도가: {money(exit_price)}",
             f"실현손익: {money(pnl) if not isinstance(pnl, str) else '-'}",
             f"수익률: {percent(pct) if not isinstance(pct, str) else '-'}",
             f"매도 사유: {label}",
             f"원인 코드: {code}",
             f"상태: {state}"]
    return _join(lines)


def order_cancelled(fields: Dict[str, Any]) -> str:
    side = _side(fields)
    quantity = _first(fields, "quantity", "requested_qty", "qty")
    filled = _first(fields, "filled_quantity", "filled_qty")
    label, code = reason_label(_first(fields, "reason", "cancel_reason"))
    lines = [f"[{_side_word(side)} 주문 취소]", "",
             f"종목: {fields.get('symbol', '-')}",
             f"전략: {strategy_label(_first(fields, 'strategy_id', 'strategy', 'source'))}",
             f"세션: {session_label(_first(fields, 'session', 'entry_session'))}",
             f"수량: {shares(quantity)}", ""]
    if filled is not None:
        try:
            cancelled = max(0, int(quantity) - int(filled))
            lines += [f"주문수량: {shares(quantity)}",
                      f"체결수량: {shares(filled)}",
                      f"취소수량: {shares(cancelled)}", ""]
        except (TypeError, ValueError):
            pass
    lines += ["상태: 취소 완료",
              f"취소 사유: {label}",
              f"원인 코드: {code}"]
    order_id = _first(fields, "broker_order_id")
    if order_id:
        lines.append(f"주문번호: {order_id}")
    return _join(lines)


def order_failed(fields: Dict[str, Any]) -> str:
    """A broker submission was attempted and did not produce an accepted
    order. Distinct from a block: the wire was touched."""
    side = _side(fields)
    raw = _first(fields, "reason", "error", default="BROKER_SUBMIT_FAILED")
    label, code = reason_label(raw)
    if label == UNMAPPED_REASON:
        label = REASON_LABELS["BROKER_SUBMIT_FAILED"]
    response = _first(fields, "broker_response", "detail", "msg1")
    lines = [f"[{_side_word(side)} 주문 실패]", "",
             f"종목: {fields.get('symbol', '-')}",
             f"전략: {strategy_label(_first(fields, 'strategy_id', 'strategy', 'source'))}",
             f"세션: {session_label(_first(fields, 'session'))}",
             f"수량: {shares(_first(fields, 'quantity', 'qty'))}", "",
             "상태: 주문 실패",
             f"사유: {label}",
             f"원인 코드: {code}"]
    if response:
        lines.append(f"브로커 응답: {str(response)[:200]}")
    return _join(lines)


def order_blocked(fields: Dict[str, Any]) -> str:
    """No broker order was submitted: an internal gate stopped it."""
    side = _side(fields)
    code_in = _first(fields, "reason_code") or block_code_for(_first(fields, "reason"))
    label, code = reason_label(code_in)
    detail = _first(fields, "detail", "reason")
    lines = [f"[{_side_word(side)} 차단]", "",
             f"종목: {fields.get('symbol', '-')}",
             f"전략: {strategy_label(_first(fields, 'strategy_id', 'strategy', 'source'))}",
             f"세션: {session_label(_first(fields, 'session'))}"]
    if fields.get("orb_minutes") is not None:
        lines.append(f"ORB: {fields['orb_minutes']}분")
    lines += ["", "상태: 주문 미제출",
              f"사유: {label}",
              f"원인 코드: {code}"]
    quality = _quality_lines(fields)
    if quality:
        lines += [""] + quality
    if detail and str(detail) != str(code) and not quality:
        lines.append(f"상세: {str(detail)[:200]}")
    return _join(lines)


def _minutes(value) -> str:
    try:
        return f"{float(value):.0f}분"
    except (TypeError, ValueError):
        return "-"


def _multiple(value) -> str:
    try:
        return f"{float(value):.2f}x"
    except (TypeError, ValueError):
        return "-"


def _quality_lines(fields: Dict[str, Any]) -> List[str]:
    """The compact entry-quality context: four lines, never the full
    snapshot. Full diagnostics live in the shadow log and the position."""
    lines: List[str] = []
    if fields.get("breakout_age_minutes") is not None:
        lines.append(f"돌파 후 경과: {_minutes(fields['breakout_age_minutes'])}")
    if fields.get("minutes_since_session_high") is not None:
        lines.append(f"최근 고점 경과: {_minutes(fields['minutes_since_session_high'])}")
    if fields.get("rvol_5m") is not None:
        lines.append(f"최근 5분 RVOL: {_multiple(fields['rvol_5m'])}")
    if fields.get("rvol_15m") is not None:
        lines.append(f"최근 15분 RVOL: {_multiple(fields['rvol_15m'])}")
    return lines


#: Korean titles for the events that must reach a person urgently.
CRITICAL_TITLES = {
    "ORDER_UNKNOWN": "주문 결과 확인 불가",
    "CANCEL_FAILED": "주문 취소 실패",
    "RECONCILIATION_MISMATCH": "계좌 대조 불일치",
    "POSITION_MISMATCH": "포지션 수량 불일치",
    "KIS_API_FAILURE": "한국투자증권 API 장애",
    "DB_FAILURE": "주문 상태 기록 실패",
    "HALT_ACTIVATED": "거래 중단(HALT) 발동",
    "KILL_SWITCH_ACTIVATED": "킬 스위치 작동",
    "WATCHDOG_ESCALATED": "감시 장치 작동: 신규 진입 차단",
    "ORDER_REJECTED_REPEATED": "반복된 브로커 주문 거부",
}

#: Field names translated on critical messages. Anything else prints
#: under its internal name -- those are the identifiers to search for.
CRITICAL_FIELD_LABELS = {
    "symbol": "종목", "side": "방향", "quantity": "수량", "limit_price": "주문가",
    "broker_order_id": "주문번호", "idempotency_key": "내부 주문 ID",
    "internal_order_id": "내부 주문 ID",
    "durable_state": "기록 상태", "reason": "사유", "state": "상태",
    "kis_qty": "KIS 수량", "local_qty": "내부 수량", "action": "조치",
    "source": "발생 위치", "previous_state": "이전 상태", "incident_id": "사건 ID",
    "new_entries_blocked": "신규 진입 차단", "reconciliation_state": "대조 상태",
    # Added after the alert channel was found printing these raw.
    "status": "상태", "detail": "상세", "note": "비고",
    "silent_minutes": "무응답 시간(분)", "kill_switch": "안전 중지 상태",
    "sell_path": "매도 경로", "stage": "단계", "consequence": "영향",
}

#: Values of `action`-shaped fields, as an operator reads them. Only the
#: actions production actually emits: an unknown action is NOT guessed,
#: it is shown as its own code.
ACTION_LABELS = {
    "HOLD": "유지", "RETRY": "재시도", "HALT": "거래 중지",
    "RECONCILE": "상태 재확인", "BLOCK": "차단", "NONE": "조치 없음",
}

#: Fields whose value is a machine STATUS word and must be rendered
#: through `status_label`.
_STATUS_VALUED_FIELDS = frozenset({
    "status", "state", "previous_state", "durable_state",
    "reconciliation_state", "kill_switch",
})

#: Fields whose value is free technical text that stays as written --
#: an exception message, an incident id, a broker id. Never translated,
#: but always behind a Korean label.
_TECHNICAL_FIELDS = frozenset({
    "detail", "note", "broker_order_id", "internal_order_id",
    "idempotency_key", "incident_id", "consequence", "sell_path", "stage",
})

_SIDE_WORDS = {"buy": "매수", "sell": "매도", "BUY": "매수", "SELL": "매도"}


def _critical_value(key: str, value: Any) -> str:
    """One alert field's value, in the operator's language.

    Status words go through the shared `STATUS_LABELS`; booleans become
    예/아니오; an `action` becomes its Korean verb, or stays as its own
    code when production emits one this table has never seen -- guessing
    at an unknown instruction is worse than showing it verbatim.
    """
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if key == "side":
        return _SIDE_WORDS.get(str(value), str(value))
    if key == "action":
        return ACTION_LABELS.get(str(value).strip().upper(), str(value))
    if key in _TECHNICAL_FIELDS:
        return str(value)
    if key in _STATUS_VALUED_FIELDS:
        # A composite such as "ENTRY_DISABLED (지금 차단됨)" already
        # carries its own Korean; only a bare token is translated.
        text = str(value).strip()
        return status_label(text) if text and " " not in text else text
    return str(value)


def critical(event: str, fields: Dict[str, Any]) -> str:
    """A serious alert for stock-live-alerts.

    Every operator-visible label is Korean. A key with no label is NOT
    printed under its raw English name -- that is how `status:`,
    `detail:` and `silent_minutes:` reached production Slack. Its VALUE
    is preserved under one 추가 정보 line and the key itself goes to the
    log, so nothing is lost and nothing English is shown.
    """
    title = CRITICAL_TITLES.get(event, event)
    lines = [f"🚨 [{title}]", ""]
    unlabelled = []
    for key, value in (fields or {}).items():
        if key == "reason":
            korean, code = reason_label(value)
            lines.append(f"사유: {korean}")
            lines.append(f"원인 코드: {code}")
            continue
        label = CRITICAL_FIELD_LABELS.get(key)
        rendered = _critical_value(key, value)
        if label is None:
            unlabelled.append((key, rendered))
            continue
        lines.append(f"{label}: {rendered}")
    if unlabelled:
        logger.warning("critical alert %s has unlabelled fields: %s", event,
                       [key for key, _ in unlabelled])
        lines.append("추가 정보: " + " · ".join(v for _, v in unlabelled))
    if event == "ORDER_UNKNOWN":
        lines += ["", "조치: 자동 재시도 금지 · 계좌 대조 완료 전 신규 주문 불가"]
    elif event in ("RECONCILIATION_MISMATCH", "POSITION_MISMATCH"):
        lines += ["", "조치: 자동 수정 없음 · 신규 진입 차단 · 사람 확인 필요"]
    elif event in ("HALT_ACTIVATED", "KILL_SWITCH_ACTIVATED", "WATCHDOG_ESCALATED"):
        lines += ["", "조치: 신규 진입 차단 · 기존 포지션 청산 경로는 유지"]
    return _join(lines)


# -- session readiness --------------------------------------------------------

def _yes_no(value) -> str:
    if value is None:
        return "-"
    return "정상" if value else "실패"


def _s6_status_lines(s6: Dict[str, Any]) -> List[str]:
    """"S6: ORB5 실거래" plus the shadow line when one runs beside it."""
    if not s6 or s6.get("error"):
        return []
    live = s6.get("live_orb_minutes")
    if live is None:
        return []
    mode = "실거래" if s6.get("orders_allowed") else "관찰 전용"
    lines = [f"S6: ORB{live} {mode}"]
    if s6.get("fast_watch_active"):
        lines.append("빠른 감시: 활성 (1분 평가)")
    if s6.get("shadow_orb_minutes") is not None:
        lines.append(f"ORB{s6['shadow_orb_minutes']}: 비교 관찰")
    data_state = s6.get("session_data")
    if data_state == "OK":
        lines.append("세션 데이터: 정상")
    elif data_state:
        # The reason code stays English inside the parentheses; the
        # operator-facing verdict is the Korean half.
        lines.append(f"세션 데이터: 준비 실패 ({data_state})")
    return lines


def session_ready(report: Dict[str, Any]) -> str:
    """The one readiness message per enabled session.

    `report` is the dict `operations.session_readiness.build` returns.
    A BLOCKED report never says 준비 완료.
    """
    session = report.get("session")
    title = session_title(session)
    if not report.get("ready"):
        lines = [f"[{title} 거래 준비 실패]", "",
                 f"거래일: {report.get('trading_day', '-')}",
                 f"세션: {session}",
                 "상태: 거래 차단", ""]
        lines += _s6_status_lines(report.get("s6") or {})
        if lines[-1]:
            lines.append("")
        for item in report.get("blocking") or []:
            label, code = reason_label(item.get("code"))
            # 사유 is always the Korean explanation. A raw detail is
            # debugging text and travels as 상세, never as the reason.
            lines.append(f"사유: {label}")
            lines.append(f"원인 코드: {code}")
            detail = str(item.get("detail") or "").strip()
            if detail and detail != label:
                lines.append(f"상세: {detail}")
        lines += ["", f"배포 버전: {report.get('release', '-')}"]
        return _join(lines)
    checks = report.get("checks") or {}
    lines = [f"[{title} 거래 준비 완료]", "",
             f"거래일: {report.get('trading_day', '-')}",
             f"세션: {session}",
             "상태: 거래 가능", "",
             f"KIS 연결: {_yes_no(checks.get('kis_connected'))}",
             f"계좌 확인: {_yes_no(checks.get('account_match'))}",
             f"주문 가능 금액: {money(report.get('orderable_cash_usd'))}",
             f"보유 종목: {count(report.get('open_positions'), '개')}",
             f"미체결 주문: {count(report.get('open_orders'), '건')}", "",
             f"Collector: {_yes_no(checks.get('collector'))}",
             f"구독: {report.get('subscriptions', '-')}",
             f"Reconciliation: {_yes_no(checks.get('reconciliation'))}",
             f"Universe: {count(report.get('universe_size'))}",
             f"Ranking: {_yes_no(checks.get('ranking'))}", ""]
    lines += _s6_status_lines(report.get("s6") or {})
    lines += [f"실거래: {'활성' if report.get('live_enabled') else '비활성'}",
              f"배포 버전: {report.get('release', '-')}"]
    warnings = report.get("warnings") or []
    if warnings:
        lines += ["", "주의:"] + [f"- {w}" for w in warnings]
    return _join(lines)


# -- system health ------------------------------------------------------------

HEALTH_TITLES = {
    "COLLECTOR_RESTART": "Collector 자동 재시작",
    "COLLECTOR_UNHEALTHY_NO_RESTART": "Collector 이상 (재시작 보류)",
    "COLLECTOR_DISCONNECTED": "Collector 연결 끊김",
    "CRON_MISSING": "예약 작업 누락",
    "CRON_DUPLICATE": "예약 작업 중복",
    "DB_ERROR": "데이터베이스 오류",
    "SCHEMA_MISMATCH": "스키마 버전 불일치",
    "MIGRATION_FAILED": "마이그레이션 실패",
    "DISK_WARNING": "디스크 사용량 경고",
    "DISK_CRITICAL": "디스크 사용량 위험",
    "KIS_TOKEN_PROBLEM": "KIS 토큰/인증 문제",
    "RANKING_STALE": "활동 순위(Ranking) 오래됨",
    "UNIVERSE_MISSING": "유니버스 파일 없음",
    "FAILED_NO_UNIVERSE": "스캐너 유니버스 없음",
    "SCANNER_FAILED": "스캐너 실행 실패",
    "PROCESS_CRASH": "프로세스 비정상 종료",
    "PREFLIGHT_BLOCKED": "사전 점검 차단",
    "DATA_PIPELINE_FAILURE": "데이터 파이프라인 실패",
    "TOKEN_CACHE_REJECTED": "KIS 토큰 캐시 거부",
    "RATE_LIMITER_UNAVAILABLE": "KIS 요청 제한기 사용 불가",
    "ACTIVE_WATCH_FAILURE": "S6 빠른 감시 실패",
    "ACTIVE_WATCH_STALE": "S6 빠른 감시 데이터 지연",
    "DATA_ORIGIN_UNAVAILABLE": "S6 공식 세션 기점 데이터 없음",
}


def system_health(code: str, detail: str = "", *, host: Optional[str] = None,
                  release: Optional[str] = None) -> str:
    title = HEALTH_TITLES.get(str(code), str(code))
    lines = [f"⚠️ [시스템 상태] {title}", "", f"원인 코드: {code}"]
    if detail:
        lines.append(f"내용: {str(detail)[:400]}")
    if host:
        lines.append(f"호스트: {host}")
    if release:
        lines.append(f"배포 버전: {release}")
    return _join(lines)


# -- legacy alert headlines ---------------------------------------------------

#: The fixed English headlines `operations.alerts.send_alert` callers use,
#: mapped to Korean. The body lines stay as the diagnostic identifiers
#: they are.
LEGACY_ALERT_TITLES = {
    "KIS cancel confirmed but final state not persisted": "주문 취소 확인 후 상태 기록 실패",
    "CRITICAL: fatal order-state connection fault during cancel": "취소 중 치명적 DB 연결 장애",
    "Order state could not be read": "주문 상태 조회 실패",
    "Reconciliation snapshot commit is uncertain": "계좌 대조 스냅샷 기록 불확실",
    "KIS shared rate limiter unavailable": "KIS 요청 제한기 사용 불가",
    "KIS rate-limit state directory holds an unexpected file": "KIS 요청 제한 상태 디렉터리 이상",
    "KIS token cache rejected": "KIS 토큰 캐시 거부",
    "CRITICAL: trading process fail-stop": "거래 프로세스 비상 정지",
    "Shadow audit invariant violated": "감사 불변식 위반",
    "KIS order blocked": "주문 차단",
    "KIS reconciliation mismatch": "계좌 대조 불일치",
    "KIS order status UNKNOWN": "주문 결과 확인 불가",
}


def legacy_alert(message: str) -> str:
    """Prefix a Korean headline onto a legacy `*Title*\\n- k: v` alert."""
    text = str(message or "")
    first = text.split("\n", 1)[0].strip().strip("*")
    for english, korean in LEGACY_ALERT_TITLES.items():
        if first.startswith(english):
            return f"🚨 [{korean}]\n{text}"
    return f"🚨 [운영 경고]\n{text}"


#: Ordered rows of the daytime route-verification report. The keys are the
#: raw result fields; the labels are what the operator reads.
ROUTE_VERIFICATION_ROWS = (
    ("market", "시장 데이터"),
    ("buy", "매수 경로"),
    ("cancel", "취소 경로"),
    ("sell", "매도 경로"),
    ("position", "최종 보유수량"),
    ("open_orders", "미체결 주문"),
)


def route_verification(result: Dict[str, Any]) -> str:
    """The daytime order-route verification report, for system-health.

    Every value is a machine status word (`STALE`, `NOT_ATTEMPTED`,
    `VERIFIED`, ...) rendered through `status_label`, and the cause keeps
    both halves: the Korean explanation an operator acts on, and the code
    they can search for. Fields the run could not answer are omitted
    rather than printed as `unavailable`.
    """
    session = result.get("session") or "OVERNIGHT_DAYTIME"
    lines = [f"[{session_title(session)} 주문 경로 검증]", ""]
    if result.get("trading_day"):
        lines.append(f"검증일: {result['trading_day']}")
    if result.get("mode"):
        lines.append(f"실행 방식: {result['mode']}")
    if lines[-1]:
        lines.append("")
    for key, label in ROUTE_VERIFICATION_ROWS:
        if result.get(key) in (None, ""):
            continue
        lines.append(f"{label}: {status_label(result[key])}")
    if lines[-1]:
        lines.append("")
    if result.get("route_state") not in (None, ""):
        lines.append(f"{session_title(session)} 거래 상태: "
                     f"{status_label(result['route_state'])}")
    code = result.get("reason_code") or result.get("reason")
    if code:
        label, resolved = reason_label(code)
        lines += ["", f"사유: {result.get('reason_detail') or label}",
                  f"원인 코드: {resolved}"]
    if result.get("orders_submitted") is not None:
        lines += ["", f"실제 주문 전송: {count(result['orders_submitted'], '건')}"]
    return _join(lines)
