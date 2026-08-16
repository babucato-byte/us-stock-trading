# Project Constitution — 미국주식 초단타 자동매매 시스템 v1.0

이 문서는 자율개발 전 기간 동안 변하지 않는 원칙을 기록한다. 다른 모든 `docs/autonomous/` 문서와 코드 변경은 이 문서와 상충할 수 없다.

## 프로젝트 목적

미국주식 전체 종목 탐색 → 프리마켓/정규장 초반 후보 선별 → 초단타 관심종목 축소 → 1분봉 감시 → VWAP·EMA·거래량 기반 진입 → Alpaca Paper 주문 → 체결 감시 → 자동 손절 → 50% 분할 익절 → 잔여 포지션 관리 → 시간 손절 → 장 마감 강제 청산 → Slack 관제 → 일일 성과 기록까지, 검증 가능한 초단타 전략 **한 개**를 사람 개입 없이 안정적으로 운영 가능한 수준까지 완성한다.

## 적용 전략 범위

- 1차 전략: `VWAP_MICRO_PULLBACK_MOMENTUM_V1` 단 하나만 활성화한다.
- 사용 지표는 원칙적으로 VWAP, EMA9, EMA21, 거래량/상대거래량, 가격 구조, ATR로 제한한다.
- RSI/MACD/CCI/ADX 등은 명확한 검증 근거 없이 추가하지 않는다.
- 근거 없이 승률을 높이기 위한 조건 추가를 금지한다.

## Paper Trading 전용 원칙

- 초기 v1.0의 거래 계정은 Alpaca **Paper Trading 전용**이다.
- 롱 포지션 우선, 오버나이트 보유 금지, 보유 기간은 수분~당일.
- 시장 전체 스캔은 저빈도로, 실시간 감시는 선별된 관심종목에만 적용한다.

## 계층 분리 원칙 (2026-07-28, CODEX-034~038 이후 추가)

주문 실행 경로는 다음 계층으로 분리하며, 각 계층은 자신의 책임 범위를 넘는 값을 신뢰하지 않는다:

```
Market Data → Strategy Engine → Signal → Risk Engine →
Account Engine → Sizing Engine → Execution Engine → Broker
```

- **Strategy Engine**은 매수·매도 신호와 전략 조건(진입가/손절가/목표가/신뢰도 등)만 결정한다.
  계좌 잔고, 사용 비율, 최종 주문 수량, 주문 가능 금액을 결정하거나 신뢰 기준으로 전달할 수
  없다. `strategy/interface.py::EvaluationResult`에는 애초에 이런 필드가 존재하지 않는다 —
  이 제약은 코드 구조 자체로 강제된다.
- **Account Engine**(`live_readiness/account_engine.py`)만이 authoritative 계좌 상태(broker
  cash, non-margin available cash, 진행 중인 예약/노출)를 산출한다. margin은 사용하지 않으며
  `effective_cash = min(broker_cash, non_margin_available_cash)`다.
- `cash_usage_percent`와 동시 포지션/일일 진입 한도는 오직
  `live_readiness/trusted_operator_config.py`에서만 읽으며, caller/Strategy가 전달한 값은
  절대 신뢰하지 않는다(min()으로만 낮출 수 있음).
- **Risk Engine**(`live_readiness/risk_engine.py`)은 전략의 최종 수량을 사용하지 않고, 진입가·
  손절가·일일 손실 잔여 한도로부터 독자적으로 risk_based_qty를 계산한다.
- **Sizing Engine**(`live_readiness/sizing_engine.py`)만이 최종 주문 수량을 계산한다:
  `actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)`.
- **Execution Engine**(`live_readiness/execution_engine.py`)만이 broker의 주문 제출 메서드를
  호출할 수 있다. 다른 모듈이 broker를 직접 호출하는 것은 금지되며, 이는 정적 grep 기반 테스트
  (`tests/test_execution_engine.py`)로 강제된다. 기존 `paper_strategy_order.py`의 broker 호출은
  삭제하지 않고 호환 계층(legacy compat)으로 유지한다.

이 원칙은 이후 모든 Phase/사이클의 코드 변경에 우선 적용되며, 다른 문서와 상충할 수 없다.

## 절대 금지사항 (모든 Phase에서 우선 적용)

1. Live Trading을 활성화하지 않는다.
2. Live API URL을 주문 기본값으로 사용하지 않는다.
3. 실제 운영 계좌로 주문하지 않는다.
4. 기존 일일 손실 한도를 임의로 완화하지 않는다.
5. 기존 주문 안전장치를 제거하지 않는다.
6. API Key, Secret, Slack Webhook을 출력하거나 커밋하지 않는다.
7. 운영 서버에 자동 배포하지 않는다.
8. 사용자 명시 승인 없이 Oracle Cloud 설정을 변경하지 않는다.
9. 사용자 승인 없이 `origin/main`에 push하지 않는다.
10. 테스트에서 실제 Alpaca 또는 Slack API를 호출하지 않는다.
11. 실제 운영 CSV와 로그 파일을 테스트 중 변경하지 않는다.
12. 백테스트 결과만으로 실거래 가능 판정을 내리지 않는다.
13. 유튜브에서 추출한 전략을 검증 없이 주문 엔진에 연결하지 않는다.

## 품질 기준

- 한 커밋 = 하나의 논리적 변경. 대규모 리팩터링 금지.
- 모든 신규 로직은 테스트를 동반한다. 기존 테스트는 항상 통과 상태를 유지한다(회귀 금지).
- 전략 임계값은 설정 파일로 분리하되, 대시보드나 외부 입력으로 즉시 변경되지 않도록 한다.
- 데이터가 오래되었거나(stale) look-ahead bias가 의심되면 신호를 발생시키지 않는다.
- 성과를 좋게 보이기 위해 검증 기준(Phase 8 게이트 등)을 완화하지 않는다.

## 변경 승인 기준 (사용자 판단 필요 — 자율 진행 중단)

- 실거래 활성화가 필요한 경우
- 운영 서버 변경이 필요한 경우
- 데이터 삭제 또는 복구 불가능한 마이그레이션
- 기존 핵심 전략을 완전히 교체해야 하는 경우
- 비용이 발생하는 신규 유료 API 도입
- 보안 키 교체
- 법적 또는 계정 정책상 판단이 필요한 경우
- 상충하는 요구사항으로 안전성을 보장할 수 없는 경우

위 목록에 해당하지 않는 코드 분석, 로드맵 갱신, 테스트 추가, 최소 리팩터링, 문서화, mock/fixture 작성, 로컬 Paper 환경 구현, 정적 분석, 버그 수정, 오류 처리 강화, 로컬 Git 커밋, 검증 보고서 작성은 사용자 확인 없이 자율 진행한다.
