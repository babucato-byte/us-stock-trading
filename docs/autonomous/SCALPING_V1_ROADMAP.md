# SCALPING_V1_ROADMAP

상태값: `NOT_STARTED` / `IN_PROGRESS` / `BLOCKED` / `IMPLEMENTED` / `VALIDATED` / `COMPLETED`

이 로드맵은 매 작업 사이클마다 갱신한다. 마지막 갱신 시점의 커밋 해시를 각 Phase에 기록한다.

---

## Phase 0 — 현재 상태 기준선 확정

**상태: VALIDATED**

- 목적: 저장소 전체 구조/실행 경로/테스트 기준선을 확정하고 로드맵을 수립한다.
- 작업 목록:
  - 저장소 구조, 브랜치/커밋, 전체 테스트 실행 확인
  - 실행 진입점, cron/systemd/Slack/Alpaca 연동 확인
  - 기존 전략·주문·체결·청산 흐름 분석
  - 모듈 최상단 부작용 확인 (Phase 1 개발환경 정리 작업에서 이미 다수 확인됨)
  - 운영 파일 vs 테스트 파일 구분
  - 기존 전략 재사용 가능 영역 확인
- 완료 조건: 전체 흐름 문서화(본 문서 "현재 아키텍처 요약" 절), 테스트 기준선 기록, 위험 요소 목록, 로드맵 확정 — **모두 충족**.
- 관련 파일: 본 문서, `CURRENT_STATUS.md`, 기존 `docs/PHASE1_BASELINE_CLEANUP.md`(개발환경 정리 이력)
- 테스트 결과: 63 passed, 0 failed (기준선)
- 커밋 해시: 기준선 = `946caea`(Add paper order execution safety tests). 본 Phase 0 문서화 자체의 커밋 해시는 `CURRENT_STATUS.md`에 최신값 기록.
- 잔여 위험: 없음(분석 전용 Phase).

### 현재 아키텍처 요약 (Phase 0 산출물)

```
daily_candidate_scanner.py (전체 시장 스캔, RSI/MA200/거래량/breakout/trend/momentum)
  → candidates.csv → strong_candidates.csv → order_candidates.csv
  → gpt_analysis.py (참고용 AI 분석, 주문 결정권 없음)
  → slack_report.py (#value-report 일일 요약)
  → paper_strategy_order.py (독립 재점수화 + Paper 주문 검토/제출)
      → account_risk.py (일일 손실 한도)
      → order_safety.py (trading mode/position size/trade count/open positions)
      → broker/alpaca_client.py (AlpacaBroker, Paper/Live 분리)
      → order_history.csv 저장, Slack 알림
  → order_monitor.py (별도 프로세스, 체결/취소 폴링 + Slack 알림)
```

- 진입점: `daily_candidate_scanner.py`, `gpt_analysis.py`, `slack_report.py`, `daily_pipeline.py`, `paper_strategy_order.py`, `order_monitor.py` (README "Run" 섹션 공식 문서화)
- 배포: systemd 2개(`order-monitor`, `dashboard`) + cron(`premarket_scan_runner.py`, `universe_daily_runner.py`, `run_premarket.py`) + nginx, Oracle Cloud Ubuntu `/home/ubuntu/trading`
- 현재 전략은 **일봉 기반 스윙/데이트레이드 스코어링**(RSI/MA200/거래량)이며, 초단타(1분봉, VWAP/EMA9/EMA21) 로직은 **전혀 존재하지 않음** — Phase 2~4는 신규 구축.
- 재사용 가능 영역: `market_hours.py`(세션 판정), `market_guard.py`(거래일 판정), `broker/`(Paper/Live 분리 구조), `risk_config.py`/`order_safety.py`(주문 안전장치 골격), `slack_utils.py`, Slack 채널 구조, `daily_candidate_scanner.py`의 smart_money_score(관심종목 선별 시 참고 가능).
- 테스트 기준선: 63 passed (기존 47 + Phase2 order execution 16), `pytest.ini`가 `tests/`만 수집, 루트 스크래치 스크립트(`test.py` 등)는 미수집.

---

## Phase 1 — 주문 안전성과 실행 경로 검증

**상태: IN_PROGRESS**

- 목적: candidate → scoring → account risk → duplicate/held check → market/session check → order safety → broker submission → order history → Slack notification 전 경로를 mock 기반으로 검증.
- 1차 완료(커밋 `946caea`, 16개 테스트): 정상 Paper 주문, Live URL 차단, Paper 모드 미확인 차단, 중복 주문 차단, 보유 종목 재매수 차단, 일일 거래 제한, 일일 손실 한도, 시장 외 주문 차단, API timeout 안전 처리, rejected 주문 처리, 주문 이력 저장 실패 로깅, Slack 실패 격리.
- 2차 완료(커밋 `fe2988c`): 명시적 Paper 모드/공식 Paper endpoint 강제, 재시작 후 당일 주문 횟수 복구, 제출 전 `PENDING_SUBMISSION` 예약 영속화 — **단, 독립 재검증(`CODEX_REVIEW.md`)에서 PARTIALLY_RESOLVED 판정**(GET 경로 안전검사 누락, fail-open 이력 읽기, 로컬시간 날짜, 비원자적/무잠금 쓰기 등).
- 3차 완료(커밋 `9688a13`/`b93a08a`/`22a6651`/`962eb69`, CODEX-001~006 재수정): broker GET 호출에도 동일 안전검사 강제, 이력 fail-closed 전환, America/New_York 기준 날짜, 원자적 쓰기+프로세스 잠금+잠금 하 재조회, `order_reconciliation.csv`를 통한 client_order_id/체결 상태 추적(+broker 대조, 재주문 없음), 저장소 루트 `conftest.py`로 상위 디렉터리 실행 시 스크래치 스크립트 수집 차단+import 경로 고정 — **단, 독립 재검증에서 CODEX-001/002/006이 다시 PARTIALLY_RESOLVED로 판정**(universe_builder.py의 endpoint 우회, 비정규 날짜의 일일한도 우회, reconciliation 무잠금 lost update).
- 4차 완료(커밋 `05757fe`/`0c2dab4`/`16a1ee4`, CODEX-007~009 재수정): `order_date`를 정확히 `YYYY-MM-DD`로만 엄격 검증(비정규 값 하나라도 있으면 전체 이력 차단), `order_reconciliation.csv` 전용 잠금+상태 후퇴 방지 단조 병합(실제 multiprocessing으로 검증), `universe_builder.py`가 broker 공통 endpoint 안전검사를 거치도록 재작성. 이로써 CODEX-001/002/006도 함께 RESOLVED로 승격. 상세는 `REMEDIATION_PLAN.md` 참고. CRITICAL/HIGH 미해결 0건.
- **아직 미충족(명시적 잔여 항목, Phase 1 완료 기준 자체)**: 부분 체결이 broker와의 reconciliation까지는 반영되지만, "포지션 상태"로의 완전한 반영은 Phase 5(포지션 생명주기 상태 머신)가 선행되어야 함. **명시적 잔여 위험으로 기록**.
- 완료 조건: 실제 API 호출 0회, 실제 Slack 발송 0회, 전체 테스트 통과, 주문 경로 잔여 위험 문서화 — 앞의 3개는 충족, 마지막(부분 체결의 포지션 상태 반영)은 Phase 5로 이관 조건부 충족.
- 관련 파일: `paper_strategy_order.py`, `account_risk.py`, `order_safety.py`, `risk_config.py`, `broker/broker_config.py`, `broker/alpaca_client.py`, `universe_builder.py`, `conftest.py`, `tests/test_broker_safety.py`, `tests/test_paper_order_execution.py`, `tests/test_universe_builder.py`
- 테스트 결과: **149 passed, 0 failed, 2 warnings.** 저장소 루트/상위 디렉터리(`pytest`/`python -m pytest`, 경로 명시) 4가지 조합 전부 동일 결과. 동시성(threading+multiprocessing) 테스트 5회 반복 안정.
- 커밋 해시: `946caea`(1차) → `fe2988c`/`dc9bff9`(2차) → `9688a13`/`b93a08a`/`22a6651`/`962eb69`(3차) → `05757fe`/`0c2dab4`/`16a1ee4`(4차, CODEX-001~009 전부 RESOLVED)
- 잔여 위험: 부분 체결의 "포지션 상태" 완전 반영은 Phase 5 선행 필요. `run_order_safety_check` 예외 발생 시 해당 실행의 나머지 후보가 함께 스킵됨(의도된 보수적 동작). `order_history.csv`와 `order_reconciliation.csv`는 각자 원자적/잠금이지만 두 파일에 걸친 단일 트랜잭션은 없음(안전 크리티컬 판단은 `order_history.csv`에만 의존하므로 실거래 안전성 자체는 영향 없음) — SQLite 전환 필요성을 `DECISION_LOG.md`에 `NEEDS_USER_DECISION`으로 기록.

---

## Phase 2 — 초단타 관심종목 선별 엔진

**상태: NOT_STARTED**

- 목적: 기존 전체 시장 스캐너(`daily_candidate_scanner.py`)를 재사용하되, 초단타 감시에 적합한 관심종목만 별도로 축소한 `scalping_watchlist.csv`를 생성.
- 작업 목록: 거래 가능 여부/최소가/최소평균거래량/최소평균거래대금/당일 상대거래량/프리마켓 거래량/갭 상승률/스프레드 또는 유동성 대체지표/ATR/뉴스의존성 표시/반복탐지횟수/smart_money_score 재사용 판단 로직 구현. 시장 전체를 1분마다 조회하지 않도록 스캐너와 실시간 감시 대상을 분리.
- 완료 조건: `scalping_watchlist.csv`(필드: symbol, detected_at, latest_price, gap_percent, relative_volume, average_dollar_volume, spread_estimate/liquidity_score, repeat_count, source_score, eligibility_reason, rejection_reason, status) 생성 로직 + 단위 테스트.
- 관련 파일(예정): `scalping/watchlist_builder.py`(신규), `config/scalping_watchlist_rules.json`(신규)
- 테스트 결과: 미착수
- 커밋 해시: 없음
- 잔여 위험: 스프레드/유동성 대체지표 산출 방식은 Alpaca가 실시간 호가창을 제공하는지 확인 필요(무료 티어 제약 가능성) — 착수 시 재검토.

---

## Phase 3 — 1분봉 감시 및 지표 엔진

**상태: NOT_STARTED**

- 목적: 선별된 관심종목만 대상으로 1분봉 수집·정규화·지표(VWAP/EMA9/EMA21/상대거래량/ATR/눌림구조) 계산.
- 완료 조건: 고정 샘플 데이터 기반 지표 계산 테스트, 타임존 테스트, 누락/중복 데이터 테스트 통과, look-ahead bias 없음(과거 시점 계산 시 미래 봉 미참조).
- 관련 파일(예정): `scalping/bar_feed.py`, `scalping/indicators.py`(신규)
- 테스트 결과: 미착수
- 커밋 해시: 없음
- 잔여 위험: 1분봉 데이터 소스(Alpaca Market Data 무료 티어의 지연/제한) 확인 필요 — 착수 시 재검토, 유료 API 필요 시 "비용 발생 신규 API 도입"에 해당하여 사용자 승인 필요.

---

## Phase 4 — VWAP 마이크로 풀백 전략 엔진

**상태: NOT_STARTED**

- 목적: `VWAP_MICRO_PULLBACK_MOMENTUM_V1`을 독립 모듈로 구현, 구조화된 결과(`strategy_id, symbol, evaluated_at, state, signal, entry_reason, rejection_reasons, entry_price, stop_price, target_1, target_2, risk_per_share, confidence_score, input_snapshot`) 반환.
- 완료 조건: VWAP/EMA 조건, 눌림 깊이/거래량 감소, 재돌파 거래량 증가, 손절 위치·최소손익비 계산, 신호 중복 방지, 추격진입 방지, 스프레드/유동성 저하 차단, stale 데이터 차단 — 각각 단위 테스트.
- 관련 파일(예정): `scalping/strategies/vwap_micro_pullback_v1.py`, `config/scalping_strategy_v1.json`
- 테스트 결과: 미착수
- 커밋 해시: 없음
- 잔여 위험: 임계값(눌림 깊이 %, 손익비 등)의 실증 근거 확보 필요 — Phase 6 백테스트 이전에는 잠정값으로 명시하고 `UNKNOWN`/가정으로 표시.

---

## Phase 5 — 포지션 생명주기 및 자동 청산

**상태: NOT_STARTED**

- 목적: `DETECTED→ARMED→ENTRY_SUBMITTED→PARTIALLY_FILLED→FILLED→PARTIAL_EXIT_SUBMITTED→PARTIAL_EXITED→TRAILING→EXIT_SUBMITTED→CLOSED` 상태 머신 구현, 부분체결/분할익절/시간손절/강제청산 처리.
- 완료 조건: Phase 1에서 이관된 "부분 체결 처리" 포함, 재시작 후 상태 복구, 중복 청산 방지 테스트 통과.
- 관련 파일(예정): `scalping/position_lifecycle.py`
- 테스트 결과: 미착수
- 커밋 해시: 없음
- 잔여 위험: 상태 영속화 방식(파일 vs 경량 DB) 결정 필요.

---

## Phase 6 — 초단타 백테스트 및 리플레이

**상태: NOT_STARTED**
목적/완료조건: 지시서 원문 6번 섹션 그대로 적용(수수료/스프레드/슬리피지/부분체결 가정 반영, look-ahead bias 방지, 단일 수익 거래 제거 결과 별도 산출). 착수 시 세부 작업 목록을 본 절에 추가.

## Phase 7 — Paper Trading 운영 관제

**상태: NOT_STARTED**
목적/완료조건: 지시서 원문 7번 섹션 그대로 적용(기존 Slack 채널 재사용, 중복/과다 알림 방지). 착수 시 세부 작업 목록을 본 절에 추가.

## Phase 8 — Paper 검증 게이트

**상태: NOT_STARTED**
목적/완료조건: 지시서 원문 8번 섹션의 수치 기준(최소 100회 체결, 20거래일, PF≥1.20 등) 그대로 적용. 착수 시 실제 자본 규모/설정을 근거로 재검토하되 완화하지 않는다.

---

## 유튜브 전략 정보 연결 (병행 트랙, Phase와 독립)

**상태: NOT_STARTED**
상태 흐름 `COLLECTED→STRUCTURED→REVIEWED→BACKTESTED→PAPER_APPROVED→ACTIVE`를 관리할 저장 구조가 아직 없음. `ACTIVE` 이전 전략은 주문 엔진에 연결하지 않는다. 이번 사이클에서는 착수하지 않음.
