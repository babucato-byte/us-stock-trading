"""Korean display text for #scanner-monitor. Display only.

The boundary this module defends
--------------------------------
Everything here is a lookup from an internal identifier to the words an
operator reads. Nothing here is an identifier. `exit_reason` stays
`VOLUME_DECAY_PRICE_WEAKNESS` in the decision, in the trade record, in
the database and in every comparison; only the line printed in Slack says
"거래량 감소 + 가격 약화".

That separation is not tidiness. A localisation that reached the stored
values would make every historical row unqueryable by the code that wrote
it, and a `strategy_id` that differed by locale would break the position
limit silently -- the sort of failure that shows up as a missing refusal
rather than an error.

Unknown values pass through
---------------------------
Every lookup falls back to the original identifier rather than to a
placeholder. A scanner or an exit reason added later appears in Slack
under its English name, which is legible and truthful, instead of
"알 수 없음", which hides that something new exists. Failing to translate
must never look like failing to happen.

Codes travel with the words
---------------------------
Exit reasons and sessions print the original identifier alongside the
Korean, because those are the values an operator will grep for in the
logs and quote in a bug report. The Korean tells them what happened; the
code tells them what to search for.
"""

from typing import Optional

#: Scanner display names. The S1..S6 numbering is kept -- it is how the
#: strategies are referred to everywhere else, including by the operator.
SCANNER_LABELS = {
    "hma_early_trend": "S1 · HMA 초기추세",
    "accumulation": "S2 · 거래량 누적",
    "breakout_ready": "S3 · 돌파 준비",
    "premarket_momentum": "S4 · 프리마켓 모멘텀",
    "gap_pullback": "S5 · 갭 눌림",
    "orb": "S6 · 장초반 돌파",
}

#: Short strategy names for order messages, where the full scanner label
#: would repeat the S-number already in the header.
STRATEGY_LABELS = {
    "hma_early_trend": "HMA 초기추세",
    "accumulation": "거래량 누적",
    "S1_HMA_EARLY_TREND_V1": "HMA 초기추세",
    "S2_VOLUME_ACCUMULATION_V1": "거래량 누적",
}

SESSION_LABELS = {
    "OVERNIGHT_DAYTIME": "주간/오버나이트",
    "PREMARKET": "프리마켓",
    "REGULAR": "정규장",
    "AFTER_HOURS": "시간외",
    "CLOSED": "장 마감",
}

STATUS_LABELS = {
    "SUCCESS": "정상",
    "FAILED": "실패",
    "FAILED_NOT_BUILT": "실행 준비 실패",
    "FAILED_PROVIDER": "데이터 공급 실패",
    "FAILED_NO_UNIVERSE": "유니버스 없음",
    "FAILED_NO_SCANNER": "스캐너 없음",
    "PARTIAL": "부분 완료",
    "SKIPPED_MARKET_CLOSED": "장 마감으로 미실행",
    "DISCOVERY_ONLY": "분석 전용",
    "LIMITED_LIVE": "제한 실거래",
    "SCAN_ONLY": "스캔 전용",
    # The composite string scan_session builds. Translated whole rather
    # than by parts: splitting on "/" here would put a formatting rule
    # in a lookup table and break the moment the phrasing changes.
    "SCAN_ONLY / LIVE UNVERIFIED": "스캔 전용 (실거래 미검증)",
    "LIVE_ELIGIBLE": "실거래 가능",
    "LIVE_UNVERIFIED": "실거래 미검증",
    "REFERENCE_VERIFIED": "실거래 가능",
    "INSUFFICIENT_SAMPLE": "표본 부족",
    "PENDING_SETTLEMENT": "정산 대기",
    "ACCEPTED": "주문 접수",
    "PENDING": "대기",
    "REJECTED": "거부됨",
    "UNKNOWN": "확인 불가",
}

#: S2 exit reasons. The internal value is unchanged everywhere else.
EXIT_REASON_LABELS = {
    "VOLUME_DECAY": "거래량 모멘텀 감소",
    "VOLUME_DECAY_PRICE_WEAKNESS": "거래량 감소 + 가격 약화",
    "VWAP_FAILURE": "VWAP 이탈",
    "STRUCTURE_FAILURE": "가격 구조 붕괴",
    "HARD_STOP": "최대 손실 제한",
    "SESSION_EXIT": "세션 종료 청산",
    "EMERGENCY_LIQUIDATION": "긴급 청산",
    "EMERGENCY": "긴급 청산",
    # S1's own vocabulary, so one channel reads consistently.
    "S1_HARD_STOP": "최대 손실 제한",
    "S1_PROTECTIVE_STOP": "보호 손절",
    "S1_TREND_BREAKDOWN": "추세 이탈",
    "S1_TIME_EXIT": "보유 기간 만료",
}

#: Operational message tags.
TAG_LABELS = {
    "LIVE BUY": "실거래 매수",
    "LIVE SELL": "실거래 매도",
    "LIVE FILL": "체결",
    "BUY FILL": "매수 체결",
    "SELL FILL": "매도 체결",
    "RISK": "위험 관리",
    "WATCHDOG": "시스템 감시",
    "RECONCILIATION": "계좌 대조",
    "DAILY SUMMARY": "일일 스캐너 요약",
}

#: Field labels. Kept in one table so a message cannot use "상태:" in one
#: place and "스테이터스:" in another.
FIELD_LABELS = {
    "Scanner": "스캐너",
    "Status": "상태",
    "Mode": "운영 모드",
    "Session": "세션",
    "Candidates": "후보 수",
    "Scanned": "분석 종목 수",
    "Generated at": "생성 시각",
    "Trading day": "거래일",
    "Rank": "순위",
    "Score": "점수",
    "Price": "현재가",
    "Signal Price": "신호 가격",
    "Volume Multiple": "거래량 배수",
    "VWAP": "VWAP",
    "Position": "보유 상태",
    "Quantity": "수량",
    "Avg Fill": "평균 체결가",
    "Order ID": "주문번호",
    "Reason": "사유",
    "Realized PnL": "실현손익",
    "Holding Time": "보유시간",
    "Errors": "오류",
    "Symbol": "종목",
    "Session execution": "세션 주문",
}


def _lookup(table, value, default=None):
    """Translate, or hand back what came in.

    Never a placeholder. An untranslated value appearing under its
    English name is legible and truthful; "알 수 없음" hides that
    something new exists, and failing to translate must not look like
    failing to happen.
    """
    if value is None:
        return default
    return table.get(str(value), str(value))


def scanner(name) -> str:
    return _lookup(SCANNER_LABELS, name)


def strategy(name) -> str:
    return _lookup(STRATEGY_LABELS, name)


def session(name, *, with_code: bool = True) -> Optional[str]:
    """"정규장 (REGULAR)".

    The code travels because it is what an operator greps for. Omitted
    only when the value did not translate -- "REGULAR (REGULAR)" is
    noise, not information.
    """
    if name is None:
        return None
    korean = _lookup(SESSION_LABELS, name)
    if not with_code or korean == str(name):
        return korean
    return f"{korean} ({name})"


def status(value) -> Optional[str]:
    """Translate a status, including the compound "FAILED: reason" form.

    Split on the first colon so a runner-supplied detail survives: the
    detail is usually the only thing that says WHY, and dropping it to
    keep the lookup simple would leave a failure message that names no
    cause.
    """
    if value is None:
        return None
    text = str(value)
    if ":" in text:
        head, _, tail = text.partition(":")
        return f"{_lookup(STATUS_LABELS, head.strip())}: {tail.strip()}"
    return _lookup(STATUS_LABELS, text)


def exit_reason(value, *, with_code: bool = True) -> Optional[str]:
    """"거래량 감소 + 가격 약화 (VOLUME_DECAY_PRICE_WEAKNESS)"."""
    if value is None:
        return None
    korean = _lookup(EXIT_REASON_LABELS, value)
    if not with_code or korean == str(value):
        return korean
    return f"{korean} ({value})"


def tag(value) -> str:
    return _lookup(TAG_LABELS, value)


def fill_tag(side) -> str:
    """매수 체결 / 매도 체결, by the ACTUAL side.

    Not by the event name: a fill event carries both directions, and
    labelling a sell "매수 체결" would make the channel lie about a real
    order. An unknown side degrades to the neutral "체결" rather than
    guessing a direction.
    """
    text = str(side or "").strip().lower()
    if text == "buy":
        return TAG_LABELS["BUY FILL"]
    if text == "sell":
        return TAG_LABELS["SELL FILL"]
    return TAG_LABELS["LIVE FILL"]


def field(name) -> str:
    return _lookup(FIELD_LABELS, name)


def dual_time(moment) -> Optional[str]:
    """ET and KST on two lines, from one timestamp.

    Converts an existing instant rather than computing a new one -- no
    clock is read here and no session boundary is decided. If the value
    cannot be converted it is returned as it arrived, because a
    timestamp an operator can still read is better than none.
    """
    if moment is None:
        return None
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        if isinstance(moment, str):
            text = moment.replace("Z", "+00:00")
            moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        et = moment.astimezone(ZoneInfo("America/New_York"))
        kst = moment.astimezone(ZoneInfo("Asia/Seoul"))
        return (f"{et.strftime('%Y-%m-%d %H:%M')} ET\n"
                f"{kst.strftime('%Y-%m-%d %H:%M')} KST")
    except Exception:  # noqa: BLE001 - a display helper must not raise
        return str(moment)
