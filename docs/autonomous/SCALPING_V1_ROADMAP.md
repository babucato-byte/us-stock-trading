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
  → slack_report.py (#stock-trading-report 일일 요약)
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

**상태: Phase 1A VALIDATED / Phase 1B DEFERRED_TO_PHASE_5**

Phase 1은 두 부분으로 나뉜다:
- **Phase 1A — 주문 진입 안전성**: candidate → scoring → account risk → duplicate/held check → market/session check → order safety → broker submission → order history → Slack notification. **VALIDATED** (Codex 최종 검증 `PASS_WITH_CONDITIONS`, CODEX-001~009 전부 RESOLVED, 신규 Finding 없음, 회귀 없음).
- **Phase 1B — 부분 체결 및 포지션 생명주기 통합**: reconciliation 데이터를 실제 "포지션 상태"(손절/익절/강제청산 판단)로 연결하는 부분. **DEFERRED_TO_PHASE_5** — Phase 5(포지션 생명주기 상태 머신)가 선행되어야 하는 별도 범위이며, Codex도 이를 "Phase 2 관심종목 선별과 독립적"이라고 명시함.

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
- 잔여 위험: (Phase 1B로 이관) 부분 체결의 "포지션 상태" 완전 반영은 Phase 5 선행 필요. `run_order_safety_check` 예외 발생 시 해당 실행의 나머지 후보가 함께 스킵됨(의도된 보수적 동작, Phase 1A 범위 내에서는 수용 가능한 설계로 판단). `order_history.csv`와 `order_reconciliation.csv`는 각자 원자적/잠금이지만 두 파일에 걸친 단일 트랜잭션은 없음(안전 크리티컬 판단은 `order_history.csv`에만 의존하므로 실거래 안전성 자체는 영향 없음) — **Phase 5 착수 전 SQLite 전환 여부를 사용자가 결정해야 함**(`DECISION_LOG.md`의 `NEEDS_USER_DECISION` 참고, Phase 2에서는 이 결정을 요구하지 않음).
- **Codex 최종 판정 (2026-07-21)**: Overall verdict `PASS_WITH_CONDITIONS`. CODEX-001~009 전부 RESOLVED, 신규 Finding 없음. 전체 테스트 149 passed, 집중 테스트 106 passed, 동시성 테스트 6 passed×5회, 실제 외부 API 호출 0회, 운영 CSV/runtime 변경 없음. Phase 1 자체 판정은 `KEEP_IN_PROGRESS`(Phase 1B가 Phase 5 선행 조건으로 남아있어), **Phase 2 판정은 `PROCEED`**.

---

## Phase 2 — 초단타 관심종목 선별 엔진

**상태: IMPLEMENTED** (Claude 자체 테스트 통과 — Codex `PROCEED` 판정 전까지 `VALIDATED`로 승격하지 않음)

- 목적: 미국주식 전체를 매분 조회하지 않고, 기존 저빈도 시장 스캐너를 재사용해 초단타(`VWAP_MICRO_PULLBACK_MOMENTUM_V1`)가 1분봉으로 집중 감시할 관심종목만 선별. 결과물은 주문 신호가 아니며, VWAP/EMA 진입 판단은 Phase 3·4 범위.
- 구현: Stage A(거래 가능성, `universe_builder.py`가 이미 필터링한 결과 재검증만 수행) → B(가격/유동성) → C(당일 움직임) → D(지속성/반복탐지) → E(설명 가능한 가중합 점수) 5단계 파이프라인. 재사용/신규 판단 근거는 `DECISION_LOG.md` 참고 — `calculate_rsi`/`calculate_atr`/`market_hours`/`market_guard`는 그대로 재사용, 기존 JSON 룰 엔진(fail-open 설계)은 "불명확하면 포함하지 않는다" 원칙과 배치되어 재사용하지 않고 Phase 2 전용 명시적 필터 함수로 새로 작성.
- 완료 조건 충족 현황: `scalping_watchlist.csv` 생성(23개 필드, 지시서 22개 필드 + `expires_at` 계산 포함) ✅, 모든 종목 포함/제외 사유 기록 ✅, 반복탐지 추적(ET 기준, 중간탈락 재등장 구분, 동시성 lost-update 방지) ✅, TTL 기반 NEW→ACTIVE→COOLING→EXPIRED 만료 처리 ✅, 손상/누락 데이터 fail-closed(watchlist 파일) 또는 개별 심볼 제외(provider 오류) ✅, 실제 외부 API 호출 0회(FakeMarketDataProvider) ✅, 운영 파일 변경 없음(`order_history.csv` 해시 불변 확인) ✅, 기존 전체 테스트 통과(183 passed) ✅, 기존 주문/리스크 로직 미변경 ✅, Codex 검증용 패키지 생성 ✅(`VALIDATION_PACKAGE.md`).
- 관련 파일: `config/scalping_watchlist_config.py`(신규), `scalping_watchlist/`(신규 패키지: `models.py`, `data_provider.py`, `features.py`, `eligibility.py`, `repeat_tracker.py`, `scorer.py`, `repository.py`, `atomic_io.py`, `pipeline.py`), `tests/test_scalping_watchlist.py`(신규, 34건).
- 테스트 결과: 신규 34 passed(동시성 5회 반복 안정), 전체 회귀 183 passed(기존 149 + 신규 34), 저장소 루트/상위 디렉터리 모두 동일.
- 커밋 해시: `4a96883` (Add scalping watchlist selection engine)
- 잔여 위험/알려진 한계:
  - `spread_estimate`는 실제 호가 데이터 소스가 없어 항상 `NOT_AVAILABLE`(허위 값 생성 금지 원칙 준수). 대신 `average_dollar_volume` 기반 `liquidity_score` 대체 지표를 유동성 게이트로 사용 — 실제 호가 데이터 확보 시 재검토 필요(`DECISION_LOG.md`).
  - `smart_money_score`는 이번 버전에서 `NOT_EVALUATED`(daily_candidate_scanner의 MA200/RSI 전체 재계산이 필요해 시간/스코프 제약으로 보류) — 점수 가중치의 해당 성분은 항상 0으로 기여, 향후 통합 여지 있음.
  - 모든 `SCORING_WEIGHTS`와 Stage B/C 임계값은 과거 성과로 검증되지 않은 초기 가정(`DECISION_LOG.md`에 근거 기록) — Phase 6 백테스트 이전까지 잠정값.
  - `scalping_watchlist.csv`/`scalping_repeat_state.csv`는 각자 독립적인 파일 잠금을 가지나(Phase 1의 `order_history.csv`/`order_reconciliation.csv`와 마찬가지로) 두 파일 간 단일 트랜잭션은 없음 — 안전 크리티컬이 아닌 후보 선별 데이터이므로 Phase 1의 SQLite 논의와는 별개로 낮은 우선순위로 기록.
- 판정: `IMPLEMENTED`. Codex 재검증에서 `PROCEED` 판정을 받은 뒤에만 `VALIDATED`로 승격한다.

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

**상태: IMPLEMENTED** (Stage 3, Claude 자체 테스트 통과 — Codex 검증 전까지 `VALIDATED`로 승격하지 않음)

- 목적: `VWAP_MICRO_PULLBACK_MOMENTUM_V1`을 독립 모듈로 구현, 구조화된 결과(`strategy_id, symbol, evaluated_at, state, signal, entry_reason, rejection_reasons, entry_price, stop_price, target_1, target_2, risk_per_share, confidence_score, input_snapshot`) 반환.
- Stage 3 구현 범위: 전략 플러그인 인터페이스(`strategy/interface.py`의 `TradingStrategy` ABC +
  `EvaluationResult`), 전략 상태 상수(`strategy/status.py`, `COLLECTED`~`REJECTED` 9종), 전략
  레지스트리(`strategy/registry.py`, ACTIVE 최대 1개 구조적 강제 + `require_active()`/
  `select_strategy_for_order()` 주문 생성 가드), 1차 전략 플러그인
  (`strategy/plugins/vwap_micro_pullback_v1.py`) — VWAP/EMA9/EMA21은 pandas로 직접 계산(기존
  `indicators.py`는 일봉 HMA/MACD/SQZMOM 전용이라 재사용 대상 아님, 확인 후 판단),
  price>VWAP·EMA9>EMA21·초기 rally·얕은 pullback(거래량 감소 동반)·재돌파(거래량 재확대) 순으로
  `evaluate_setup()`이 판정, `generate_entry()`가 진입가(돌파 봉 종가)·손절(micro-pullback low, ATR
  기반 최소 버퍼)·목표(1R에서 50% 분할, ASSUMPTION인 2R 잔여 목표)를 계산. 확장 패턴 예시는
  `strategy/plugins/__init__.py`와 `strategy/plugins/_example_orb_stub.py`.
- Stage 3 범위 밖(의도적 미구현, Stage 4로 이관): `manage_position()`/`invalidate()`는
  `NotImplementedError` 스텁(포지션 생명주기 상태 머신이 Phase 5 선행 필요). 신호 중복 방지,
  추격진입 방지, 실시간 스프레드/유동성 저하 차단, stale 데이터 차단은 1분봉 실시간 수집(Phase 3,
  `NOT_STARTED`)이 존재해야 의미가 있는 항목이라 Stage 3에서는 구현하지 않음 — Phase 3 착수 후
  재검토.
- 완료 조건 충족 현황(Stage 3 한정): VWAP/EMA 조건 ✅, 눌림 깊이/거래량 감소 ✅, 재돌파 거래량 증가
  ✅, 손절 위치·최소손익비 계산 ✅(단위 테스트로 stop<entry, target>entry, 1R 수식 검증), 각 조건별
  단위 테스트 ✅. 신호 중복 방지/추격진입 방지/스프레드·유동성 차단/stale 데이터 차단은 위 사유로
  Stage 3 범위 밖.
- 관련 파일: `strategy/interface.py`, `strategy/status.py`, `strategy/registry.py`,
  `strategy/plugins/vwap_micro_pullback_v1.py`, `strategy/plugins/__init__.py`,
  `strategy/plugins/_example_orb_stub.py`, `config/scalping_strategy_v1_config.py`(신규, 임계값 근거는
  `DECISION_LOG.md`), `tests/test_strategy_platform.py`(신규, 43건).
- 테스트 결과: 신규 43 passed, 전체 회귀 613 passed(기존 570 + 신규 43), 저장소 루트 기준.
- 커밋 해시: 본 커밋(CURRENT_STATUS.md에 최신값 기록).
- 잔여 위험: 임계값(눌림 깊이 %, rally 최소 %, target_2 R-배수 등)의 실증 근거 확보 필요 — Phase 6
  백테스트 이전에는 잠정값으로 명시(코드 `# ASSUMPTION` 주석 + `DECISION_LOG.md`). 실시간 1분봉
  피드가 없어 stale 데이터/추격진입/신호 중복 방지는 Phase 3 착수 후 별도 구현 필요.

---

## Phase 5 — 포지션 생명주기 및 자동 청산

**상태: IMPLEMENTED**

- 목적: `SETUP_DETECTED→ARMED→ENTRY_RESERVED→ENTRY_SUBMITTED→PARTIALLY_FILLED→FILLED→STOP_ACTIVE→TARGET_1_ACTIVE→PARTIAL_EXIT_SUBMITTED→PARTIAL_EXITED→TRAILING→EXIT_SUBMITTED→CLOSED` 상태 머신 구현, 부분체결/분할익절/시간손절/강제청산 처리.
- 구현 내용:
  - `positions/states.py`: 13개 생명주기 상태 + 6개 예외 상태(REJECTED/CANCELLED/EXPIRED/UNKNOWN/MANUAL_REVIEW/RECOVERY_REQUIRED), 명시적 `TRANSITIONS` 인접 테이블(임의 상태 전이 차단), `FAIL_CLOSED_STATE = RECOVERY_REQUIRED`.
  - `positions/store.py`: 포지션별 JSON 원자적 저장소(`fcntl.flock`, tempfile+fsync+os.replace 패턴 재사용), 레코드별 fail-closed 검증(손상/필드누락/미인식 상태 → RECOVERY_REQUIRED), `locked_position()` 컨텍스트 매니저로 "읽기-판단-브로커 호출-쓰기" 전체 구간을 단일 락으로 보호(중복 청산 방지의 핵심 메커니즘, 스레딩 테스트로 검증).
  - `positions/lifecycle.py`: `enter_position()`(전략 ACTIVE 검증→`try_reserve_order`→`submit_order(side="buy")`, ledger commit/abort), `record_fill()`(부분/완전 체결, FILLED→STOP_ACTIVE 자동 전이), `check_and_manage()`(우선순위: EOD 강제청산 > 시간손절 > 손절 > 1R 50% 분할익절 > 2R 전량청산, 손익분기 트레일링), `check_invalidation()`(전략 무효화 신호 시 전량청산), `recover_on_restart()`(브로커 재조회 실패/불확실 시 RECOVERY_REQUIRED로 fail-closed). 모든 청산 주문은 `paper_strategy_order.submit_order(side="sell")`을 직접 호출(진입 전용 일일 중복 방지 로직을 우회하되 kill switch/자격증명/RequestPurpose 게이트는 그대로 통과) — 근거는 `DECISION_LOG.md` Stage 4 섹션 참고.
- 완료 조건: 상태 전이 테스트(23건, `tests/test_position_lifecycle.py` + 31건 `tests/test_position_states.py`), 부분체결 테스트, 손절·익절 테스트, 시간손절·EOD강제청산 테스트, 재시작 복구 테스트(불확실/확인됨/이미RECOVERY_REQUIRED/종결상태 스킵), 동시성 기반 중복 청산 방지 테스트, 청산 주문 side="sell" 보증, Kill Switch 정책 유지(모두 통과) — **모두 충족**.
- 관련 파일: `positions/states.py`, `positions/store.py`, `positions/lifecycle.py`, `tests/test_position_states.py`, `tests/test_position_store.py`, `tests/test_position_lifecycle.py`
- 테스트 결과: 전체 스위트 683 passed, 0 failed (Phase 5 관련 신규 69건 포함). 실제 Alpaca/Slack/네트워크 호출 0회, 운영 CSV 변경 0건.
- 커밋 해시: `a78ab1b`(states), `2058614`(store), `f9a2d1f`(locked_position + invalidate), `b3d8cf4`(lifecycle)
- 잔여 위험: 상태 영속화가 여전히 파일(JSON) 기반 — Phase 5 자체는 완료되었으나, `order_history.csv`/`positions` 저장소가 별개 파일로 분리되어 있어 두 파일에 걸친 단일 트랜잭션은 없음(Phase 1B에서 이미 문서화된 동일 잔여 위험). 트레일링 정책은 "1R 50% 분할 후 손절을 손익분기로 이동"이라는 최소 규칙으로, 정교한 트레일링 알고리즘이 아님(의도된 초기 정책, `DECISION_LOG.md` 참고).
- **Stage 5 부속 갱신(2026-07-25)**: 위 잔여 위험(다중 파일 트랜잭션 부재)을 해결하기 위해 `state_store/`(SQLite 기반 orders/fills/positions/position_events/strategy_runs/risk_events/kill_switch_events 스키마, 마이그레이션, 읽기 전용 CSV 가져오기, 내보내기/롤백)를 병행 인프라로 구축했다. **실제 운영 경로는 전환하지 않았다** — `paper_strategy_order.py`/`positions/lifecycle.py`는 여전히 CSV/JSON을 유일한 판단 근거로 사용한다. 전환 여부는 `DECISION_LOG.md` Stage 5 섹션에 `NEEDS_USER_DECISION`으로 기록. 신규 테스트 20건, 전체 회귀 703 passed. 커밋 `bf05098`.

---

## Stage 6 — 사용자/YouTube 전략 자료 구조화 (부속, 2026-07-25 완료)

**상태: IMPLEMENTED**

이 항목은 원래 Phase 1~8 로드맵에 별도 번호가 없던 신규 범위(사용자 지시서 Stage 6)라 여기 부속
섹션으로 기록한다. `strategy_sources/`(`models.py`/`repository.py`/`similarity.py`/
`known_sources.py`) 신규 구현: 사용자 차트 분석·YouTube 전략 자료를 source/assumption/unknown로
분리한 스키마, 버전 관리되는 append-only JSON 저장소, 결정론적(비-LLM) 유사도 채점. 지시서에
명시된 8개 소스(VWAP 진입/1:2 R:R/50% 분할 익절/Ross Cameron 마이크로 눌림목은
`PROJECT_CONSTITUTION.md` 실제 인용 + `REVIEWED`; Turtle/멀티 RSI/볼린저 눌림목/CCI·RSI·ADX는
실제 소스 미지정이라 전부 `ASSUMPTION`+`TBD_OPERATOR`)를 `docs/strategy/sources/*.json`에 시딩.
`validation_status`는 절대 `ACTIVE`에 도달할 수 없도록 구조적으로 제한(`strategy/status.py`
상태 재사용, 앞 4단계만 허용). 신규 테스트 33건, 전체 회귀 736 passed. 커밋 `639af97`.

## Phase 6 — 초단타 백테스트 및 리플레이 (Stage 7, 2026-07-26 완료)

**상태: IMPLEMENTED**

- 목적: 1분봉 리플레이로 전략의 `generate_entry()`/`invalidate()`를 과거 데이터에 통과시켜 Phase 5
  실거래 청산 정책(1R 50% 분할, 2R/손절 전량 청산, 시간 손절, 장 마감 강제 청산)을 동일하게
  시뮬레이션. 수수료/스프레드/슬리피지/부분체결 가정 반영, look-ahead 방지, 단일 최대 수익 거래
  제거 결과 별도 산출 — 착수 직전 사용자가 명시한 10개 제약(비용 미조정, 동일봉 충돌 보수적 처리,
  비용 항목 분리 표시, look-ahead 금지, 프리마켓/정규장 분리, 부분체결·거래량 제약, 최대수익거래
  제거 결과 별도 출력, 데이터부족 시 INSUFFICIENT_DATA, YouTube 후보 비-자동활성화, 자체+회귀
  테스트 통과 후 다음 Stage) 그대로 구현.
- 관련 파일: `backtest/config.py`(비용·정책 가정, 전부 결과를 보기 전에 고정), `backtest/models.py`
  (Trade/CostBreakdown/BacktestResult), `backtest/engine.py`(리플레이 루프, look-ahead 구조적 차단),
  `backtest/metrics.py`(승률/평균R/PF/기대값/MDD/연속손실/최대수익거래제거/시간대·가격대·유동성·
  슬리피지 민감도 분해), `backtest/compare.py`(비교 테이블 전용, `strategy.registry` 미참조 —
  AST 기반 테스트로 검증).
- 테스트 결과: `tests/test_backtest_engine.py` 29건. 전체 회귀 **765 passed, 0 failed**(기존 736 +
  신규 29). 실제 네트워크 호출 0회, 운영 CSV 변경 0건.
- 커밋 해시: `59958cf`.
- 잔여 위험: `nominal_qty=100`/`spread_bps=5.0`/`slippage_bps=5.0` 등은 전부 ASSUMPTION(근거
  `DECISION_LOG.md` Stage 7 섹션) — 실제 측정치 확보 전까지 잠정값. 동일봉 충돌 정책은
  `STOP_FIRST` 한 가지만 지원(보수적 선택, 다른 정책 필요 시 별도 결정 필요).

## Stage 8 — 전략 선택 엔진 (부속, 2026-07-26 완료)

**상태: IMPLEMENTED**

원 로드맵의 Phase 1~8 번호에 별도 슬롯이 없는 신규 범위(사용자 지시서 Stage 8)라 부속 섹션으로
기록한다. `strategy_selection/`(`models.py`/`scoring.py`/`engine.py`) 신규 구현: 후보 전략 풀 중
설명가능한 점수/규칙 기반(비-LLM)으로 최대 1개를 `SELECTED`로 결정. 자격 게이트 자체가 설명가능 —
`REJECTED`/`PAUSED` → `DISABLED`, `COLLECTED`/`STRUCTURED`(검토 전) → `INSUFFICIENT_DATA`, 백테스트
결과 없음/`INSUFFICIENT_DATA`/거래 10건 미만 → `INSUFFICIENT_DATA`, 선호 시장상태 불일치 →
`MARKET_MISMATCH`. 나머지만 점수 계산(백테스트 성과/Paper 성과/표본크기/MDD/슬리피지 민감도/
시장상태 적합도/종목상태 적합도, 전부 요소별 breakdown 노출). 엔진은 `strategy.registry`를
import하지 않으며 `ACTIVE` 승격을 전혀 호출하지 않음 — 선택은 추천일 뿐, 실제 활성화는 별도
운영자 승인 절차. 신규 테스트 27건, 전체 회귀 792 passed. 커밋 `2094adf`.

## Phase 7 — Paper Trading 운영 관제 (Stage 9, 2026-07-26 완료)

**상태: IMPLEMENTED**

- 목적: 지시서 원문 7번 섹션(기존 Slack 채널 재사용, 중복/과다 알림 방지) 중 알림 중복/에스컬레이션
  부분은 이미 이전 사이클의 `notification_health.py`(CODEX-016~019 계열)로 구현되어 있었다. 이번
  Stage 9는 사용자 지시서의 별도 항목 "운영 관제 Dashboard/CLI"를 신규 구현: 현재 모드/활성
  전략/시장상태/관심종목/신호/주문/포지션/손절·목표가/실현·미실현 PnL/일일 주문 수/일일 손실/
  Kill Switch/Slack 상태/broker 상태/reconciliation/마지막 성공 실행 시각을 로컬에서 확인 가능하게
  했다.
- 구현: `ops_dashboard/`(`snapshot.py`, `cli.py`). 모든 섹션이 로컬 파일/env 기반 config에서만
  조립되며, 실제 Alpaca/Slack API를 전혀 호출하지 않는다 — "Slack이 다운돼도 로컬에서 계속 확인
  가능"이 폴백 경로가 아니라 애초에 어떤 섹션도 Slack 가용성에 의존하지 않는 구조로 보장됨(Slack
  섹션은 webhook 환경변수 존재 여부만 확인, broker 섹션은 `BrokerConfig`의 env 파생 값만 읽음).
  각 섹션은 개별적으로 장애 허용적(`SectionResult.ok=False`) — 데이터 소스 하나가 깨져도 나머지
  대시보드는 정상 렌더링.
- 신규 테스트: `tests/test_ops_dashboard.py` 16건. 작성 중 실제 크로스 파일 테스트 격리 버그를
  발견·수정(`test_ai_analysis.py`가 `sys.modules.pop("paper_strategy_order", ...)`를 실행하는
  것과 상호작용해 모듈 레벨 import가 stale해지는 문제 — 커밋 메시지 참고).
- 전체 회귀: **808 passed, 0 failed**(기존 792 + 신규 16). 실제 네트워크 호출 0회, 운영 CSV 변경
  0건.
- 커밋 해시: `f2e1a24`.
- 잔여 위험: "마지막 성공 실행 시각"은 전용 마커 파일이 없어 `order_history.csv`/
  `order_reconciliation.csv`의 mtime을 근사치로 사용(ASSUMPTION, 정확한 실행 완료 시각이 아님).
  일일 손실(`daily_loss`)은 실제 broker 계좌 스냅샷(`account` dict)을 호출자가 명시적으로 주입해야
  계산됨 — 라이브 API 호출 없이는 `NOT_AVAILABLE`.

## Stage 10 — 30,000원 제한 실거래 준비 (부속, 2026-07-26 완료)

**상태: 문서화·계산 모듈 완료 (실거래 준비 완료 아님)**

`live_readiness/`(`sizing.py`/`allowlist.py`) 신규 순수 계산 모듈 + `docs/live_review/
LIMITED_LIVE_30K_KRW_PLAYBOOK.md`. 마이크로 주문 수량 계산(소수점 확인·최소 주문 금액 확인
포함)과 종목 allow-list fail-closed 검사를 실제 코드로 구현·테스트했으나, **실제 주문 제출
경로(`paper_strategy_order.py`/`positions/lifecycle.py`)에는 배선하지 않았다** — 그 경로는 이미
Codex `PASS_WITH_CONDITIONS` 검증을 거친 안전 크리티컬 경계이며, 이번 Stage 3~10 연속 구현
사이클에서 재검증 없이 다시 건드리지 않기로 결정(근거는 플레이북 §6/§7 `NEEDS_USER_DECISION`).
첫 오류 시 `ENTRY_DISABLED` 전환은 기존 `kill_switch_state.py` 상태를 활용한 **수동 운영 절차**로
문서화(자동 배선 여부는 별도 결정 사항). 신규 테스트 12건, 전체 회귀 820 passed. 커밋 `986d655`.
TBD_OPERATOR: 실계좌/실환율/Live API Key/실 주문 금액 한도/실 승인자/배포 시각/롤백 담당자/실제
Alpaca 최소 주문 금액/실제 allow-list 내용 — 전부 미확정 상태로 명시적으로 남김.

## Stage 11 — Account/Risk/Sizing/Execution Engine 계층 분리 (부속, 2026-07-28 완료)

**상태: 신규 계층 모듈 구현·테스트 완료 (실거래 배선 아님, 기존 경로 호환 유지)**

`docs/autonomous/PROJECT_CONSTITUTION.md`의 "계층 분리 원칙"을 코드로 구현: `live_readiness/
trusted_operator_config.py`(cash_usage_percent 트러스트 상한 + 동시포지션/일일진입 한도의 단일
소스), `account_engine.py`(AccountSnapshot, broker.get_account() + entry_reservation_ledger
기반 authoritative 잔고/노출), `risk_engine.py`(risk_based_qty, 전략 수량 미신뢰), `sizing_engine.py`
(actual_qty=min(balance/risk/strategy), 전부 finite 검증), `execution_engine.py`
(ValidatedOrderCommand + broker 호출 단일 지점, 정적 grep 테스트로 강제). Stage 10과 동일한
"building block, 아직 운영 파이프라인 미배선" 패턴 — `paper_strategy_order.py`의 기존 broker 호출
경로는 legacy compat으로 명시적으로 유지(삭제하지 않음). 신규 테스트 174건, 전체 회귀 1,299
passed. 커밋 `3494fe3`.

## Phase 8 — Paper 검증 게이트

**상태: NOT_STARTED**
목적/완료조건: 지시서 원문 8번 섹션의 수치 기준(최소 100회 체결, 20거래일, PF≥1.20 등) 그대로 적용. 착수 시 실제 자본 규모/설정을 근거로 재검토하되 완화하지 않는다.

---

## 유튜브 전략 정보 연결 (병행 트랙, Phase와 독립)

**상태: NOT_STARTED**
상태 흐름 `COLLECTED→STRUCTURED→REVIEWED→BACKTESTED→PAPER_APPROVED→ACTIVE`를 관리할 저장 구조가 아직 없음. `ACTIVE` 이전 전략은 주문 엔진에 연결하지 않는다. 이번 사이클에서는 착수하지 않음.
