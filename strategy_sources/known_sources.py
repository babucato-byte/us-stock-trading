"""The 8 source strategies named in the user's Stage 6 instruction: VWAP,
1:2 R:R, 50% partial exit, Turtle, multi-timeframe RSI, Bollinger
pullback, CCI/RSI/ADX, Ross Cameron micro pullback.

Honesty note (important -- read before editing): this module does not
have access to an actual YouTube video transcript or the user's actual
chart-analysis notes for most of these. Three of the eight (VWAP-based
entry, 1:2 R:R, 50% partial exit at 1R, and Ross Cameron-style micro
pullback) are already real, reviewed, implemented policy --
docs/autonomous/PROJECT_CONSTITUTION.md states them explicitly and
strategy/plugins/vwap_micro_pullback_v1.py implements them; those four
entries use origin=SOURCE with a real quoted excerpt from that document
and validation_status=REVIEWED (or COLLECTED where the specific numeric
choice, e.g. 2R for target_2, was recorded as an ASSUMPTION in
DECISION_LOG.md rather than stated in the constitution).

The remaining four (Turtle, multi-timeframe RSI, Bollinger pullback,
CCI/RSI/ADX) are well-known public trading concepts the user's Stage 6
instruction listed by name as things to catalog, but no specific source
document/video/chart was provided for any of them. Fabricating a
source_excerpt or a specific YouTube URL for these would violate the
project's core rule against inventing unverified specifics (mirrors
PROJECT_CONSTITUTION.md's 절대 금지사항 #13: "유튜브에서 추출한 전략을
검증 없이 주문 엔진에 연결하지 않는다"). Their claims are therefore all
origin=ASSUMPTION, their `reference` field is an explicit TBD_OPERATOR
marker, and validation_status stays at COLLECTED (the entry point) --
never higher -- until the user supplies real source material to review
against. seed_known_sources() must never be extended to invent one.
"""

from strategy_sources.models import (
    CATEGORY_ENTRY,
    CATEGORY_EXIT,
    CATEGORY_FILTER,
    CATEGORY_RISK,
    CATEGORY_STOP,
    CATEGORY_TARGET,
    ORIGIN_ASSUMPTION,
    ORIGIN_SOURCE,
    SOURCE_TYPE_USER_CHART_ANALYSIS,
    SOURCE_TYPE_YOUTUBE,
    StrategyClaim,
    StrategySource,
)
from strategy.status import COLLECTED, REVIEWED

CONSTITUTION_REF = "docs/autonomous/PROJECT_CONSTITUTION.md"
COLLECTED_AT = "2026-07-25T00:00:00+00:00"  # date this catalog was structured, not a claim about the source itself

TBD_REFERENCE = (
    "TBD_OPERATOR: 실제 사용자 차트 분석 자료 또는 YouTube 영상 링크/타임스탬프 미지정. "
    "아래 claims는 해당 개념에 대한 공개적으로 알려진 일반 서술이며, 특정 소스를 인용한 것이 아님."
)


def _reviewed_sources():
    """VWAP entry filter, 1:2 R:R, and 50% partial exit at 1R -- explicitly
    stated in PROJECT_CONSTITUTION.md and already implemented/tested in
    strategy/plugins/vwap_micro_pullback_v1.py + config/
    scalping_strategy_v1_config.py."""

    vwap_entry = StrategySource(
        source_id="VWAP_MOMENTUM_ENTRY",
        source_type=SOURCE_TYPE_USER_CHART_ANALYSIS,
        title="VWAP·EMA·거래량 기반 모멘텀 진입",
        reference=CONSTITUTION_REF,
        collected_at=COLLECTED_AT,
        version=1,
        validation_status=REVIEWED,
        derived_strategy_id="VWAP_MICRO_PULLBACK_MOMENTUM_V1",
        claims=[
            StrategyClaim(
                category=CATEGORY_ENTRY,
                statement="VWAP, EMA, 거래량 기반으로 진입한다",
                origin=ORIGIN_SOURCE,
                source_excerpt="VWAP·EMA·거래량 기반 진입 → Alpaca Paper 주문",
                confidence=1.0,
            ),
            StrategyClaim(
                category=CATEGORY_FILTER,
                statement="사용 지표는 원칙적으로 VWAP, EMA9, EMA21, 거래량/상대거래량, 가격 구조, ATR로 제한한다",
                origin=ORIGIN_SOURCE,
                source_excerpt="사용 지표는 원칙적으로 VWAP, EMA9, EMA21, 거래량/상대거래량, 가격 구조, ATR로 제한한다.",
                confidence=1.0,
            ),
        ],
        notes="strategy/plugins/vwap_micro_pullback_v1.py로 이미 구현·테스트됨 (Stage 3).",
    )

    rr_1_to_2 = StrategySource(
        source_id="RISK_REWARD_1_TO_2",
        source_type=SOURCE_TYPE_USER_CHART_ANALYSIS,
        title="1:2 손익비(Risk:Reward) 목표 구조",
        reference=CONSTITUTION_REF,
        collected_at=COLLECTED_AT,
        version=1,
        validation_status=COLLECTED,
        derived_strategy_id="VWAP_MICRO_PULLBACK_MOMENTUM_V1",
        claims=[
            StrategyClaim(
                category=CATEGORY_TARGET,
                statement="1R에서 1차 목표(target_1)를 설정한다",
                origin=ORIGIN_SOURCE,
                source_excerpt="자동 손절 → 50% 분할 익절 → 잔여 포지션 관리",
                confidence=1.0,
            ),
            StrategyClaim(
                category=CATEGORY_TARGET,
                statement="잔여 포지션의 2차 목표(target_2)는 진입 리스크의 2배(2R)로 설정한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.6,
            ),
        ],
        notes=(
            "target_2=2R은 지시서 원문에 명시되지 않은 ASSUMPTION이며 "
            "DECISION_LOG.md Stage 3 결정 근거로 기록되어 있음 (config."
            "scalping_strategy_v1_config.TARGET_2_R_MULTIPLE=2.0)."
        ),
    )

    partial_exit_50 = StrategySource(
        source_id="PARTIAL_EXIT_50_AT_1R",
        source_type=SOURCE_TYPE_USER_CHART_ANALYSIS,
        title="1R 도달 시 50% 분할 익절",
        reference=CONSTITUTION_REF,
        collected_at=COLLECTED_AT,
        version=1,
        validation_status=REVIEWED,
        derived_strategy_id="VWAP_MICRO_PULLBACK_MOMENTUM_V1",
        claims=[
            StrategyClaim(
                category=CATEGORY_EXIT,
                statement="1R 도달 시 보유 수량의 50%를 분할 익절한다",
                origin=ORIGIN_SOURCE,
                source_excerpt="자동 손절 → 50% 분할 익절 → 잔여 포지션 관리",
                confidence=1.0,
            ),
            StrategyClaim(
                category=CATEGORY_STOP,
                statement="분할 익절 이후 잔여 포지션의 손절가는 손익분기(진입가)로 이동한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.5,
            ),
        ],
        notes=(
            "50% 분할 자체는 constitution에 명시(REVIEWED). 분할 이후 손절가를 손익분기로 "
            "이동하는 정책은 Stage 4에서 결정한 ASSUMPTION (positions/lifecycle.py, "
            "DECISION_LOG.md Stage 4 참고)."
        ),
    )

    ross_cameron = StrategySource(
        source_id="ROSS_CAMERON_MICRO_PULLBACK",
        source_type=SOURCE_TYPE_YOUTUBE,
        title="Ross Cameron 스타일 마이크로 눌림목(micro pullback) 진입",
        reference=(
            "TBD_OPERATOR: 사용자가 참고한 실제 Ross Cameron 영상 링크 미지정. "
            "VWAP_MICRO_PULLBACK_MOMENTUM_V1의 설계 근거로 이미 채택되어 구현됨 "
            "(strategy/plugins/vwap_micro_pullback_v1.py, DECISION_LOG.md Stage 3)."
        ),
        collected_at=COLLECTED_AT,
        version=1,
        validation_status=COLLECTED,
        derived_strategy_id="VWAP_MICRO_PULLBACK_MOMENTUM_V1",
        claims=[
            StrategyClaim(
                category=CATEGORY_ENTRY,
                statement="초기 rally 이후 얕은 pullback(거래량 감소)에서 재돌파(거래량 재확대) 시 진입한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.7,
            ),
            StrategyClaim(
                category=CATEGORY_STOP,
                statement="손절은 micro-pullback 저점 또는 ATR 기반 최소 버퍼 중 더 보수적인 값으로 설정한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.6,
            ),
        ],
        notes=(
            "실제 영상 링크가 지정되지 않았으므로 origin=SOURCE를 사용하지 않았다. "
            "구현은 이미 존재하지만(derived_strategy_id), 원 소스 자료 자체는 미검증 상태로 "
            "validation_status=COLLECTED를 유지한다."
        ),
    )

    return [vwap_entry, rr_1_to_2, partial_exit_50, ross_cameron]


def _unverified_candidate_sources():
    """Turtle, multi-timeframe RSI, Bollinger pullback, CCI/RSI/ADX --
    named in the Stage 6 instruction but with no specific source document
    or video provided. All claims are ORIGIN_ASSUMPTION, reference is an
    explicit TBD_OPERATOR marker, and none has a derived_strategy_id --
    none of these has been implemented, and PROJECT_CONSTITUTION.md
    explicitly restricts indicators to VWAP/EMA/volume/ATR "명확한 검증
    근거 없이" adding RSI/MACD/CCI/ADX, so these stay catalogued-only."""

    turtle = StrategySource(
        source_id="TURTLE_TREND_FOLLOWING",
        source_type=SOURCE_TYPE_YOUTUBE,
        title="터틀 트레이딩(Turtle Trading) 추세추종 규칙",
        reference=TBD_REFERENCE,
        collected_at=COLLECTED_AT,
        version=1,
        validation_status=COLLECTED,
        claims=[
            StrategyClaim(
                category=CATEGORY_ENTRY,
                statement="N일 신고가/신저가 돌파 시 진입한다 (Donchian 채널 브레이크아웃)",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.5,
            ),
            StrategyClaim(
                category=CATEGORY_RISK,
                statement="포지션 크기는 ATR 기반 변동성 단위(N)로 결정한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.5,
            ),
            StrategyClaim(
                category=CATEGORY_STOP,
                statement="손절은 진입가로부터 2N(ATR 2배) 거리에 설정한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.4,
            ),
        ],
        notes="장기·스윙 트레이딩 규칙으로, 이 프로젝트의 초단타(수분~당일) 범위와 시간 프레임이 다름 — 그대로 적용 불가, 참고용.",
    )

    multi_tf_rsi = StrategySource(
        source_id="MULTI_TIMEFRAME_RSI",
        source_type=SOURCE_TYPE_YOUTUBE,
        title="멀티 타임프레임 RSI 확인 진입",
        reference=TBD_REFERENCE,
        collected_at=COLLECTED_AT,
        version=1,
        validation_status=COLLECTED,
        claims=[
            StrategyClaim(
                category=CATEGORY_FILTER,
                statement="상위 타임프레임(예: 5분봉) RSI가 추세 방향과 일치할 때만 하위 타임프레임(1분봉) 신호를 채택한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.5,
            ),
            StrategyClaim(
                category=CATEGORY_ENTRY,
                statement="하위 타임프레임 RSI가 과매도(30 이하)에서 반등할 때 진입한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.5,
            ),
        ],
        notes="PROJECT_CONSTITUTION.md는 RSI를 '명확한 검증 근거 없이 추가하지 않는다'고 명시 — 채택 전 백테스트(Phase 6/Stage 7) 필요.",
    )

    bollinger_pullback = StrategySource(
        source_id="BOLLINGER_BAND_PULLBACK",
        source_type=SOURCE_TYPE_YOUTUBE,
        title="볼린저 밴드 눌림목(pullback) 진입",
        reference=TBD_REFERENCE,
        collected_at=COLLECTED_AT,
        version=1,
        validation_status=COLLECTED,
        claims=[
            StrategyClaim(
                category=CATEGORY_ENTRY,
                statement="가격이 볼린저 밴드 상단을 돌파한 뒤 중심선(20일 이동평균)까지 눌림 후 재상승 시 진입한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.5,
            ),
            StrategyClaim(
                category=CATEGORY_STOP,
                statement="손절은 볼린저 밴드 하단 이탈 시로 설정한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.4,
            ),
        ],
        notes="VWAP 대신 이동평균 기반 밴드를 사용 — VWAP_MICRO_PULLBACK_MOMENTUM_V1과 유사한 구조(눌림목 재돌파) 후보로 similarity 분석 대상.",
    )

    cci_rsi_adx = StrategySource(
        source_id="CCI_RSI_ADX_COMBO",
        source_type=SOURCE_TYPE_YOUTUBE,
        title="CCI·RSI·ADX 복합 지표 진입",
        reference=TBD_REFERENCE,
        collected_at=COLLECTED_AT,
        version=1,
        validation_status=COLLECTED,
        claims=[
            StrategyClaim(
                category=CATEGORY_FILTER,
                statement="ADX가 임계값(예: 25) 이상일 때만 추세 추종 신호를 채택한다 (횡보장 필터)",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.5,
            ),
            StrategyClaim(
                category=CATEGORY_ENTRY,
                statement="CCI가 +100을 상향 돌파하고 RSI가 50 이상일 때 진입한다",
                origin=ORIGIN_ASSUMPTION,
                confidence=0.4,
            ),
        ],
        notes="PROJECT_CONSTITUTION.md는 CCI/ADX를 '명확한 검증 근거 없이 추가하지 않는다'고 명시 — 채택 전 백테스트(Phase 6/Stage 7) 필요.",
    )

    return [turtle, multi_tf_rsi, bollinger_pullback, cci_rsi_adx]


def all_known_sources():
    return _reviewed_sources() + _unverified_candidate_sources()


def seed_known_sources(*, sources_dir=None):
    """Save every source in all_known_sources() as version 1, skipping
    (not erroring on) any source_id that already has a version 1 on disk --
    safe to call repeatedly (e.g. from a setup script) without duplicating
    or overwriting existing catalog entries."""
    from strategy_sources import repository

    saved, skipped = [], []
    for source in all_known_sources():
        if repository.load_source(source.source_id, version=1, sources_dir=sources_dir) is not None:
            skipped.append(source.source_id)
            continue
        repository.save_source(source, sources_dir=sources_dir)
        saved.append(source.source_id)
    return {"saved": saved, "skipped": skipped}
