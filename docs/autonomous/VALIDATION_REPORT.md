# VALIDATION_REPORT

## 2026-07-28 — CODEX-039/040/041 실제 운영 경로 배선 사이클

Codex 독립 검증(`CODEX_REVIEW.md`, 커밋 `9d294e3`/`40abc58`/`06a77c8`/`3494fe3`/`14f7a13` 포함
범위, overall verdict **FAIL**)이 CODEX-036을 `PARTIALLY_RESOLVED`로 재확인하고 신규
CODEX-039(MEDIUM)·CODEX-040(HIGH)·CODEX-041(MEDIUM)을 제기했다. 상세는
`REMEDIATION_PLAN.md`/`DECISION_LOG.md`의 동일 날짜 섹션 참고.

- **CODEX-039**(MEDIUM, trusted 50%가 강제 maximum이며 caller percent 미무시):
  `trusted_operator_config.get_cash_usage_percent()` 신설 — 인자 없이 트러스트 값을 그대로
  반환, caller percent와 결합하지 않음.
- **CODEX-040**(HIGH, 실제 main 흐름이 Execution Engine 우회): 신규
  `live_readiness/live_entry_pipeline.py`가 Account → Risk → Sizing → Affordability →
  Execution Engine을 실제로 orchestrate. `paper_strategy_order.main()`이 `is_live_mode`인
  `side="buy"` 진입에 대해 이 파이프라인을 호출하도록 배선(Paper 모드는 완전히 미변경).
- **CODEX-041**(MEDIUM, affordability 미배선): `live_entry_pipeline.py`가 Execution Engine
  직전 `evaluate_affordability()`를 재실행 — non-affordable candidate가 broker 호출 0회로 차단.

### 테스트 결과

```
venv/bin/python -m pytest -q                              1,331 passed, 0 failed, 2 warnings
```

이전 사이클 종료 시점(1,299 passed) 대비 32건 신규.

### 코드 변경 검증

- `live_readiness/trusted_operator_config.py`(신규 함수), `live_readiness/live_entry_pipeline.py`
  (신규), `live_readiness/execution_engine.py`(account_cash_snapshot 전달), `paper_strategy_order.py`
  (live-mode 분기 + FX rate/allow-list 헬퍼) — 모두 커밋 diff로 직접 확인.
- 안전 크리티컬 파일(`risk_config.py`, `broker/broker_config.py`, `kill_switch_state.py`,
  `order_intent_ledger.py`, `broker/alpaca_client.py`)는 SHA-256이 이전 사이클과 완전히 동일 —
  이번 사이클은 그 파일들을 전혀 건드리지 않았다.
- `order_gateway.py`의 기존 게이트 로직(CODEX-026~037)도 미변경 — 새 파이프라인은 그 게이트를
  대체하지 않고 그 위에서 broker.submit_order()를 호출한다.

### 미검증 영역

- watchlist 단계의 "사전" 일괄 affordability 필터(scanner 전체 후보군 대상)는 여전히
  `daily_candidate_scanner.py`에 배선되지 않았다 — 실행 직전 재검증(이번 사이클에서 구현)이
  안전 측면에서는 동등하지만, 효율성 측면(불필요한 analyze_stock() 호출 방지)은 여전히 미해결.
- 실제 FX rate provider 연동은 여전히 미구현(TBD_OPERATOR) — `LIVE_FX_RATE_KRW_PER_USD` 환경변수
  임시 소싱으로 파이프라인 배선만 완료했다.
- Limited live review(제한적 실거래 검토)는 여전히 BLOCKED, 실거래 없음.

## 2026-07-28 — Stage 11: Account/Risk/Sizing/Execution Engine 계층 분리

사용자 지시에 따라 주문 경로를 `Market Data → Strategy Engine → Signal → Risk Engine →
Account Engine → Sizing Engine → Execution Engine → Broker` 계층으로 분리했다. 이 사이클은
Codex 재검증 대상이 아니라 사용자가 CODEX-034~038 처리와 별도로 요청한 아키텍처 리팩터링이다.
상세는 `REMEDIATION_PLAN.md`/`DECISION_LOG.md`의 동일 날짜(Stage 11) 섹션 참고.

- `live_readiness/trusted_operator_config.py`(신규): 운영 정책 값의 단일 소스.
- `live_readiness/account_engine.py`(신규): authoritative `AccountSnapshot`.
- `live_readiness/risk_engine.py`(신규): 전략 수량 미신뢰, risk_based_qty 독자 계산.
- `live_readiness/sizing_engine.py`(신규): `actual_qty = min(balance/risk/strategy)`.
- `live_readiness/execution_engine.py`(신규): `ValidatedOrderCommand` + broker 호출 유일 경로,
  정적 grep 테스트로 강제.
- `live_readiness/watchlist_affordability.py`: `STALE_ACCOUNT_STATE` + 신규 필드 2종.
- `paper_strategy_order.py`: 동작 변경 없음, legacy compat 지위만 문서화.

### 테스트 결과

```
venv/bin/python -m pytest -q                              1,299 passed, 0 failed, 2 warnings
```

이전 사이클 종료 시점(1,125 passed) 대비 174건 신규.

### 코드 변경 검증

- 신규 5개 모듈(`trusted_operator_config.py`/`account_engine.py`/`risk_engine.py`/
  `sizing_engine.py`/`execution_engine.py`) + `account_cash.py`/`order_gateway.py`(상수
  재노출)/`watchlist_affordability.py`(신규 필드)/`paper_strategy_order.py`(docstring만) — 모두
  커밋 diff로 직접 확인.
- 안전 크리티컬 파일(`risk_config.py`, `broker/broker_config.py`, `kill_switch_state.py`,
  `order_intent_ledger.py`)는 SHA-256이 이전 사이클과 완전히 동일 — 이번 사이클은 그 파일들을
  전혀 건드리지 않았다.
- `broker/alpaca_client.py`도 이번 사이클에서 변경되지 않았다(CODEX-034~038 사이클 이후 재수정
  없음).

### 미검증 영역

- 신규 5개 엔진 모듈은 전부 building block이며, 실제 `daily_candidate_scanner.py`/
  `paper_strategy_order.py::main()` 파이프라인에는 배선되지 않았다 — Stage 10/CODEX-034
  watchlist affordability와 동일한 선례. 실제 운영 스캔·전략 루프가 이 계층을 통해 주문을
  제출하도록 만드는 것은 별도의 명시적 결정이 필요한 범위다.
- `ValidatedOrderCommand.reservation_id`/`entry_intent_id`가 command 자체가 아니라 broker 호출
  이후의 `ExecutionResult`에만 존재한다는 설계상의 제약(`DECISION_LOG.md` Stage 11 결정 2)은
  사용자 지시서의 문자 그대로("command 필수 필드")와 정확히 일치하지 않는다 — 이 저장소의 기존
  단일-예약-지점 아키텍처(CODEX-031 결정 4)와의 충돌을 피하기 위한 의도적 설계 결정으로 기록.
- Limited live review(제한적 실거래 검토)는 여전히 BLOCKED, 실거래 없음.

## 2026-07-27 — CODEX-034~038 최종 수정 사이클 해결

Codex 독립 검증(`CODEX_REVIEW.md`, 커밋 `5da6662`/`5316cd1`/`72bbb6c` 포함 범위, overall verdict
**FAIL**)이 CODEX-034를 `PARTIALLY_RESOLVED`로 재확인하고 신규 CODEX-035/036/037(HIGH)·
CODEX-038(LOW)을 제기했다. 상세는 `REMEDIATION_PLAN.md`의 동일 날짜 섹션 참고.

- **CODEX-035**(HIGH, HTTP 5xx/408/425/429를 definitive rejection으로 오분류):
  `_is_ambiguous_broker_failure()`를 "response 유무" 대신 "definitive status code
  allowlist(400/401/403/404/409/410/422) + 파싱 가능한 JSON body" 기준으로 재작성.
- **CODEX-036**(HIGH, 잔고/사용비율이 caller assertion에 의존): `live_readiness/account_cash.py`
  신설 — `TRUSTED_CASH_USAGE_PERCENT_CEILING`(트러스트 코드 상수)과 `AccountCashSnapshot`/
  `fetch_account_cash_snapshot()`(broker.get_account() 기반 유일한 생성 경로).
  `validate_and_size_live_entry()`가 optional `account_cash_snapshot`으로 caller 선언 잔고를
  `min()`으로만 제한.
- **CODEX-037**(HIGH, NaN optional cap이 fail-open): 5개 optional numeric cap 전부에 예약 전
  finite/양수 검증 추가.
- **CODEX-038**(LOW, 테스트가 운영 CSV mtime 변경): `test_summary_csv_generation`에 누락된
  `STRATEGY_PERFORMANCE_FILE` 격리 추가.

### 테스트 결과

```
venv/bin/python -m pytest -q                              1,125 passed, 0 failed, 2 warnings
```

이전 사이클 종료 시점(1,044 passed) 대비 81건 신규.

### 코드 변경 검증

- `broker/alpaca_client.py`/`paper_strategy_order.py`(ambiguous-failure allowlist 재작성),
  `live_readiness/account_cash.py`(신규), `live_readiness/order_gateway.py`(trusted percent
  ceiling, account_cash_snapshot 파라미터, optional cap fail-closed 검증),
  `tests/test_performance_analytics.py`(STRATEGY_PERFORMANCE_FILE 격리) — 모두 커밋 diff로 직접
  확인.
- 안전 크리티컬 파일(`risk_config.py`, `broker/broker_config.py`, `kill_switch_state.py`,
  `order_intent_ledger.py`)는 SHA-256이 이전 사이클과 완전히 동일.

### 미검증 영역

- CODEX-036의 `account_cash_snapshot`은 opt-in이다 — 실제 production 호출 경로가
  `fetch_account_cash_snapshot()`을 호출해 스냅샷을 채워 넣는 배선은 아직 존재하지 않는다(실거래
  승인 이후의 별도 결정, `DECISION_LOG.md` 결정 2 참고). 스냅샷을 제공하지 않는 caller는
  CODEX-036 이전과 동일하게 caller 선언 잔고만으로 판단된다.
- `entry_reservation_ledger.reconcile_by_client_order_id()`는 여전히 재시작/크래시 복구 경로에
  자동 배선되지 않았다(CODEX-034 시점부터 이어진 잔여 위험).
- Limited live review(제한적 실거래 검토)는 여전히 BLOCKED, 실거래 없음.

## 2026-07-27 — CODEX-034 + 잔고 비율 기반 주문 사이징 사이클 해결

Codex 독립 검증(`CODEX_REVIEW.md`, 커밋 `5da6662` 포함 범위, overall verdict **FAIL**)이 신규
CODEX-034(HIGH)를 제기했다. 동시에 사용자 지시에 따라 고정 30,000원 파일럿 예산을 잔고
비율(`cash_usage_percent`) 모델로 전면 교체했다. 상세는 `REMEDIATION_PLAN.md`의 동일 날짜 섹션
참고.

- **CODEX-034**(HIGH, broker 응답 유실 시 reservation 해제로 중복 주문/예산 우회 허용):
  `live_entry_reservations`에 `client_order_id`(migration 5, UNIQUE) 추가, `SUBMISSION_UNKNOWN`
  상태 신설. Ambiguous(timeout/connection reset, `.response` 없음)는 release하지 않고
  `SUBMISSION_UNKNOWN`으로 유지, definitive(HTTPError with response, 또는 broker에 도달 못한
  사전 실패)만 release. `reconcile_by_client_order_id()`로 재시작/재시도 시 broker 재조회 화해 경로.
- **잔고 비율 사이징**(사용자 지시, 정책 변경): `PILOT_TOTAL_BUDGET_KRW=30_000` 상수 완전 제거.
  `max_allocatable_cash = available_cash_krw × cash_usage_percent/100`,
  `available_for_new_order = max_allocatable_cash - pending - unknown_submission -
  open_position_cost`. `actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)` —
  위험 초과 시 거부 대신 수량 축소로 변경.
- **watchlist affordability**(신규 building block): `live_readiness/watchlist_affordability.py`
  — 잔고 기준 매수 가능 종목 분류(6개 상태), `fractionable=true` 종목은 1주 가격이 잔고를 초과해도
  최소주문금액 충족 시 후보 유지. 기존 파이프라인에는 미배선.

### 테스트 결과

```
venv/bin/python -m pytest -q                              1,044 passed, 0 failed, 2 warnings
```

이전 사이클 종료 시점(986 passed) 대비 58건 신규.

### 코드 변경 검증

- `live_readiness/entry_reservation_ledger.py`(SUBMISSION_UNKNOWN/client_order_id/reconcile 신설),
  `live_readiness/order_gateway.py`(고정 예산 제거, 잔고 비율 모델, risk/strategy 재사이징),
  `live_readiness/watchlist_affordability.py`(신규), `broker/alpaca_client.py`/
  `paper_strategy_order.py`(ambiguous-vs-definitive 분류, flat try/except로 재작성),
  `state_store/schema.py`/`migrations.py`(migration 5) — 모두 커밋 diff로 직접 확인.
- 안전 크리티컬 파일(`risk_config.py`, `broker/broker_config.py`, `kill_switch_state.py`,
  `order_intent_ledger.py`)는 SHA-256이 이전 사이클과 완전히 동일.

### 미검증 영역

- `live_readiness/watchlist_affordability.py`는 실제 `daily_candidate_scanner.py`/
  `scalping_watchlist/pipeline.py`에 배선되지 않았다 — 순수 계산 모듈 단위 테스트만 존재하며,
  실제 스캔 파이프라인과의 통합은 별도 사이클 범위.
- `entry_reservation_ledger.reconcile_by_client_order_id()`는 단위 테스트로만 검증됐고, 실제
  재시작/크래시 복구 경로(`positions/lifecycle.py`의 `recover_on_restart()`류)에 아직 배선되지
  않았다 — 프로세스 재시작 시 SUBMISSION_UNKNOWN 예약을 자동으로 화해하는 훅은 이번 사이클 범위
  밖.
- Limited live review(제한적 실거래 검토)는 여전히 BLOCKED, 실거래 없음.

## 2026-07-26 — Stage 3~10 최종 통합 수정 사이클: CODEX-024/026/028/031/032/033 해결

Codex 통합 재검증(`CODEX_REVIEW.md`, 대상 커밋 `f04a123`/`aee663c`/`09b9237`/`b78e444`/`fe3e9b7`,
overall verdict **FAIL**)이 CODEX-029/030을 `RESOLVED`로 재확인하고 CODEX-024/026/028을
`PARTIALLY_RESOLVED`로, 신규 CODEX-031(HIGH)/CODEX-032(HIGH)/CODEX-033(MEDIUM)을 제기했다.
상세는 `REMEDIATION_PLAN.md`의 동일 날짜 섹션 참고.

- **CODEX-032**(HIGH, rejected exit의 intent/position 비원자적 갱신) + CODEX-024/028 잔여분:
  `eil.mark_aborted()`를 `store.locked_position(conn=conn)`의 동일 SQLite 트랜잭션 안으로
  이동(`commit=False`), 원자적 커밋 보장.
- **CODEX-031**(HIGH, 30K/count/pending이 caller 선언에 의존) + CODEX-026 잔여분:
  `live_readiness/entry_reservation_ledger.py` 신설(SQLite migration 4) — durable 예약/authoritative
  스냅샷. `PILOT_TOTAL_BUDGET_KRW=30_000`/`MAX_CONCURRENT_LIVE_POSITIONS=1`/
  `MAX_DAILY_LIVE_ENTRIES=2` 신뢰 가능한 코드 상수와 caller 값을 `min()`으로 교차.
- **CODEX-033**(MEDIUM, governance 문서 불일치): `LIMITED_LIVE_REVIEW_CHECKLIST.md` §8을
  `BLOCKED`로 정정.

### 테스트 결과

```
venv/bin/python -m pytest -q                              986 passed, 0 failed, 2 warnings
venv/bin/pytest -q                                         986 passed, 0 failed, 2 warnings
(상위 디렉터리) python -m pytest us-stock-trading -q       986 passed, 0 failed, 2 warnings
(상위 디렉터리) pytest us-stock-trading -q                 986 passed, 0 failed, 2 warnings
```

이전 사이클 종료 시점(973 passed) 대비 13건 신규.

### 코드 변경 검증

- `positions/lifecycle.py`(원자적 rejection 처리), `live_readiness/entry_reservation_ledger.py`
  (신규), `live_readiness/order_gateway.py`(authoritative 예산/카운트), `broker/alpaca_client.py`/
  `paper_strategy_order.py`(예약 commit/release, 이중 예약 방지), `state_store/schema.py`/
  `migrations.py`(migration 4) — 모두 커밋 diff로 직접 확인.
- 안전 크리티컬 파일(`risk_config.py`, `broker/broker_config.py`, `kill_switch_state.py`,
  `order_intent_ledger.py`)는 SHA-256이 이전 사이클과 완전히 동일.

### 미검증 영역

- 실제 Alpaca 계좌를 이용한 authoritative 예산 집행 E2E(fake session/broker로만 검증).
- entry 경로의 broker-call-succeeded-but-local-exception 경쟁 상황에 대한 crash-safe
  reconciliation(Phase 1B의 기존 잔여 위험과 동일 범주, 이번 사이클 범위 밖으로 명시적으로 남김).
- 실제 FX provider, broker minimum/fractional 정책, live data feed는 여전히 미검증.

### 안전 관련 변경 사항

- `entry_reservation_ledger`의 예약은 broker 호출 **전에** durable하게 기록되며, 실패/거부/
  dry-run/예외 시 반드시 release되어 예산이 영구적으로 잠기지 않는다(release 자체가
  best-effort로 실패해도 fail-closed 방향 — 예약이 계속 활성으로 남아 더 보수적으로 집계됨).
- 30,000원 캡을 caller가 절대 완화할 수 없다는 것이 이번 사이클의 핵심 안전 강화다.

### 운영 영향

- 없음. 코드/테스트/문서 변경만 수행했으며 운영 파일과 승인 상태는 변경하지 않았다.

### 잔여 위험

- `docs/autonomous/DECISION_LOG.md`의 이번 사이클 섹션(결정 1~6) 참고. 특히 결정 5(entry 경로
  crash-safe reconciliation 미구현)는 향후 Codex 재검증에서 재확인이 필요하다.

---

## 2026-07-26 — Stage 3~10 최종 재수정 사이클: CODEX-024/026/028/029/030 해결

Codex 통합 재검증(`CODEX_REVIEW.md`, 대상 커밋 `4de0714`/`e49753f`, overall verdict **FAIL**)이
CODEX-023/025/027을 `RESOLVED`로, CODEX-024/026을 `PARTIALLY_RESOLVED`로 재확인하고 신규
CODEX-028(HIGH)/CODEX-029(HIGH)/CODEX-030(MEDIUM)을 제기했다. 상세 재현·수정 내용은
`REMEDIATION_PLAN.md`의 동일 날짜 섹션 참고. 이 문서는 최종 검증 결과만 요약한다.

- **CODEX-030**(MEDIUM, wall-clock 의존 테스트): `clock.py` 신설(Clock/ProductionClock/
  FrozenClock), `check_and_manage()`/`check_invalidation()`이 명시적 `now`/`clock`을 받도록
  변경. 실제 결함은 테스트 쪽(`now` 미전달)에 있었으므로 모든 관련 테스트에 고정 시각을 전달.
- **CODEX-028**(HIGH, SQLite/JSON commit 불일치) + **CODEX-024 잔여분**(단일 트랜잭션 아님):
  `positions/store.py`를 SQLite(`positions`/`position_events`) canonical로 재작성,
  `POSITION_STORE.json`은 커밋 후에만 쓰는 재생성 가능한 projection으로 재정의.
  `locked_position(conn=...)`이 exit intent 커밋과 동일 트랜잭션을 공유.
- **CODEX-029**(HIGH, live context symbol과 실제 주문 symbol 불일치) + **CODEX-026 잔여분**
  (direct broker 우회): `validate_and_size_live_entry(ctx, order_symbol)`에 엄격한 symbol
  동일성 검사 추가, `AlpacaBroker.submit_order()` 자체에도 동일 게이트 배선.
- 부수 발견 및 수정: `_execute_exit()`의 lock-없는 `existing_intent` 읽기로 인한 드문 경쟁
  조건(`CLOSED -> EXIT_SUBMITTED` 불법 전이) 1건, 발견 즉시 수정 및 결정적 재현 테스트 추가.
  `tests/test_position_store.py`/`tests/test_ops_dashboard.py`의 `STATE_STORE_DB_FILE` 격리
  누락(실제 저장소 루트 DB 파일에 쓰던 문제) 발견 즉시 수정.

### 테스트 결과

```
venv/bin/python -m pytest -q       973 passed, 0 failed, 2 warnings
venv/bin/pytest -q                 973 passed, 0 failed, 2 warnings
(상위 디렉터리) python -m pytest us-stock-trading -q   973 passed, 0 failed, 2 warnings
```

이전 사이클 종료 시점(923 passed) 대비 50건 신규(CODEX-030 24건, CODEX-028/029 각각 다수,
CODEX-025 테스트의 SQLite 계층 이식 포함).

### 코드 변경 검증

- `positions/store.py`(SQLite canonical 재작성), `positions/lifecycle.py`(exit intent conn
  공유, 경쟁 조건 수정, Clock 주입), `state_store/exit_intent_ledger.py`(`commit=False` 옵션),
  `state_store/schema.py`/`migrations.py`(migration 3: `projection_status` 컬럼),
  `clock.py`(신규), `live_readiness/order_gateway.py`(symbol 동일성 검사),
  `broker/alpaca_client.py`(broker-level 게이트), `paper_strategy_order.py`(live_entry_context
  전달) — 모두 커밋 diff로 직접 확인.
- 안전 크리티컬 파일(`risk_config.py`, `broker/broker_config.py`, `kill_switch_state.py`,
  `order_intent_ledger.py`)는 SHA-256이 이전 사이클과 완전히 동일 — 이번 사이클에서 전혀 건드리지
  않았음을 재확인.

### 미검증 영역

- 실제 Alpaca live 계좌를 통한 E2E symbol-mismatch/direct-broker-bypass 재현(모두 fake
  session/broker로만 검증).
- 실제 프로세스 kill/전원 차단을 이용한 SQLite WAL 파일 복구 시나리오(파일 손상 시뮬레이션은
  garbage bytes 덮어쓰기로 대체).
- 장시간(수 시간) 반복 실행을 통한 동시성 경쟁 조건의 통계적 재현율 측정(20회 반복 실행으로
  안정성만 확인).

### 안전 관련 변경 사항

- `positions/lifecycle.py::recover_on_restart()`가 SQLite 손상 시 Kill Switch를
  `MANUAL_REVIEW`로 자동 전환하는 기존 동작은 대상이 SQLite로 바뀌었을 뿐 유지.
- `AlpacaBroker.submit_order()`가 live 모드 buy에 대해 자체적으로 CODEX-026/029 게이트를
  실행하는 것이 신규 동작 — Paper 거래와 모든 청산은 완전히 영향받지 않음(테스트로 확인).

### 운영 영향

- 없음. 코드/테스트/문서 변경만 수행했으며 운영 파일(`order_history.csv`, `universe.csv`,
  `strategy_performance.csv`)과 승인 상태(`approved`/`live_enabled`)는 변경하지 않았다.

### 잔여 위험

- `docs/autonomous/DECISION_LOG.md`의 이번 사이클 섹션에 기록된 6개 결정 참고. 특히 결정 1
  (orders/fills 테이블은 여전히 canonical 대상 밖)과 결정 4(broker-level 게이트가
  `_request()` 자체가 아니라 `submit_order()`에만 배선됨 — 동일 클래스의 다른 신규 메서드가
  추가되면 재검토 필요)는 향후 Codex 재검증에서 특히 확인이 필요하다.

---

## 2026-07-26 — Stage 3~10 통합 수정 사이클: CODEX-023~027 해결

Codex 독립 검증(`CODEX_REVIEW.md`, 대상 범위 `415c129`~`64a5551`, overall verdict **FAIL**,
Stage 3~10 판정 **KEEP_IN_PROGRESS**)이 신규 HIGH 4건(CODEX-023~026) + MEDIUM 1건(CODEX-027)을
제기했다. 상세 재현·수정 내용은 `REMEDIATION_PLAN.md`의 동일 날짜 섹션 참고. 이 문서는 최종
검증 결과만 요약한다.

- **CODEX-023**(HIGH, accepted를 체결로 오판): `positions/order_status.py` 신설, 청산 경로가
  broker의 실제 주문 상태(accepted/new/... vs partially_filled/filled)를 구분하도록 재작성.
- **CODEX-024**(HIGH, timeout 후 중복 sell 가능): `state_store/exit_intent_ledger.py` 신설,
  broker 호출 전에 durable exit intent를 SQLite에 원자적으로 예약하는 3단계 청산 흐름으로 재설계.
- **CODEX-025**(HIGH, 손상 store가 빈 결과로 보임): `positions/store.py::load_all()`이 전체 파일
  손상 시 예외를 발생시키도록 변경, `recover_on_restart()`가 구조적으로 구분되는
  `RestartRecoveryResult`를 반환.
- **CODEX-026**(HIGH, 30,000원/allow-list 미배선): `live_readiness/order_gateway.py` 신설,
  `paper_strategy_order.submit_order()`의 live-mode 진입 경로에 배선(Paper 거래·청산은 미적용).
- **CODEX-027**(MEDIUM, 비정상 fill 허용): `positions/fill_validation.py` 신설,
  `record_fill()`이 mutation 전에 검증하도록 변경.

### 실행 명령 및 결과
```
./venv/bin/python -m pytest -q
```
```
923 passed, 0 failed, 2 warnings
```
- CODEX-023~027 착수 전 기준선(820 passed, 이전 Stage 3~10 완료 시점) 대비 신규 103건 추가.
- 실제 Alpaca/Slack/Yahoo 네트워크 호출 0회 (`FakeBroker`/`SequencedBroker`/실제 `AlpacaBroker`
  + 세션 호출 시 예외를 던지는 더블만 사용).
- 실제 운영 CSV(`order_history.csv`/`universe.csv`/`strategy_performance.csv`) 변경 0건(md5
  재확인). 실제 저장소 루트 `TRADING_STATE.db`가 테스트 중 생성되지 않음을 전용 테스트로 확인
  (청산 경로의 신규 SQLite 의존성이 격리되지 않았던 실제 버그를 발견·수정한 뒤).

### 코드 변경 검증
- 청산 경로 재작성이 기존 duplicate-exit 방지(포지션 락 기반)를 대체한 것이 아니라, 그 위에
  크래시/timeout 생존 가능한 durable intent 계층을 추가한 것임을 동시성 테스트로 재확인
  (`positions/store.py::locked_position()`은 변경하지 않음).
- CODEX-026의 live-mode 게이트가 `broker.config.is_live_mode`로 정확히 분기해 Paper 거래 경로를
  전혀 건드리지 않음을, 기존 수백 건의 Paper 경로 테스트가 전부 그대로 통과하는 것으로 확인.
- `getattr(broker.config, ...)`이 `.config` 없는 테스트 더블에서 `AttributeError`를 유발하던
  실제 버그를 발견·수정하고 전용 회귀 테스트를 추가.

### 테스트하지 못한 영역
- 실제 Alpaca 계정에서의 accepted→filled 실제 이벤트 순서/타이밍(이번 사이클은 시뮬레이션된
  broker 응답으로만 검증).
- 실제 SQLite 파일을 여러 프로세스(스레드가 아닌)가 동시에 사용하는 시나리오 — 스레딩 동시성은
  테스트했으나 멀티프로세스 재현은 이번 사이클 범위 밖.
- 실제 FX 데이터 제공자 연동 — `live_readiness/order_gateway.py`는 FX rate를 호출자가 주입하는
  구조이며, 실제 제공자 연결은 아직 존재하지 않음(§7 TBD_OPERATOR, `LIMITED_LIVE_30K_KRW_
  PLAYBOOK.md`).

### 안전 관련 변경
- 전부 기존 동작을 더 보수적으로 만드는 방향(청산을 더 신중하게 확인, 손상 store를 더 명확히
  차단, 실거래 진입에 새 게이트 추가, 비정상 fill 차단) — 기존 리스크 한도를 완화한 곳 없음.

### 운영 영향
- 없음. 운영 서버 미접속, systemd/cron/nginx 미변경, `.env` 실값 미변경, `main`/`origin` 미변경.

### 남은 위험
- `paper_strategy_order.submit_order()`를 우회해 `broker.submit_order()`를 직접 호출하는 경로는
  CODEX-026 게이트의 적용을 받지 않음(현재 이 저장소 내 어떤 진입 경로도 그렇게 하지 않음을 확인했으나,
  향후 신규 코드가 이 경로를 우회하지 않도록 유지 관리 필요).
- 첫 오류 시 `ENTRY_DISABLED` 자동 배선은 여전히 미구현(Stage 10에서 `NEEDS_USER_DECISION`으로
  기록, 이번 사이클도 변경하지 않음).

---

## 2026-07-25 — CODEX-022 해결 및 CODEX-021 잔여분 종결 (validate_order_intent 3자 일치 검증)

Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ae3ca`/`c133e01`/`cc740a5`, overall verdict
**FAIL**)에서 CODEX-016/017/018/019는 RESOLVED로 재확인됐으나, CODEX-021(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 CODEX-022(HIGH)가 제기됐다: `RequestPurpose` 재설계(커밋
`c133e01`) 이후에도 `_request()`가 주문 POST의 payload `side`와 `order_side`, `purpose` 세
값을 서로 대조하지 않아, `purpose=EXIT_ORDER`를 선언한 채 매수 payload(`json={"side": "buy"}`)를
보내면 `ENTRY_DISABLED` 상태에서도 HTTP가 실제로 나갔다. CODEX-021도 이 잔여 위험(`order_side`가
payload와 대조되지 않는 공백 있는 2차 방어선) 때문에 PARTIALLY_RESOLVED로 남아 있었다.

- **CODEX-022**: `broker/alpaca_client.py`에 `_PURPOSE_REQUIRED_SIDE`(`ENTRY_ORDER→"buy"`,
  `EXIT_ORDER→"sell"`) 매핑과 신규 `validate_order_intent(purpose, order_side, payload)`를
  추가했다. `ENTRY_ORDER`/`EXIT_ORDER`는 `order_side`와 `payload["side"]`가 모두 존재하고
  정확히 요구되는 문자열과 완전히 일치해야 하며(`isinstance(..., str)`으로 `bool`/`int`와
  대소문자·공백 변형도 거부), `READ_ONLY`/`RECONCILIATION`/`CANCEL_ORDER`는 반대로 둘 다 없어야
  한다. `_request()`는 `_validate_runtime_safety()`와 `_check_kill_switch()`보다도 먼저 이
  함수를 호출해, 세 값 중 하나라도 불일치하면 세션 호출이 0회임을 보장한다.
- **CODEX-021 잔여분**: 위와 동일한 `validate_order_intent()`로 함께 닫혔다. `order_side`가
  이제 실제로 payload `side`와 대조되므로, 2차 방어선으로서의 실질적 방어력을 갖는다.

CODEX-016~019는 이번 사이클에서 재작업하지 않았다 — 관련 회귀 테스트만 재실행해 회귀 없음을
확인했다(`tests/test_paper_strategy_order_kill_switch_state.py`,
`tests/test_paper_strategy_order_notification_health.py`,
`tests/test_state_store_concurrency.py`, 도합 **36 passed, 1 warning**).

검증: 신규 `tests/test_broker_order_intent_gate.py`(신규 파일, 17건, CODEX-022의 3가지 직접
재현 시나리오 전부 세션 호출 0회 차단, payload 누락/비-dict/알 수 없는 side 값 차단,
`submit_order()` 경유 정상 buy/sell은 세션 호출 1회 유지)와
`tests/test_broker_request_purpose.py`의 `test_post_allows_entry_and_exit_purpose` 갱신(이전에
ENTRY_ORDER/EXIT_ORDER 양쪽에 동일한 buy payload를 쓰던 결함을 실제 buy/sell 조합으로 수정).

전체: `venv/bin/python -m pytest -q` **570 passed, 0 failed, 2 warnings**(신규 경고 없음, 기존
urllib3/scanner 경고만). 집중 안전 테스트(`test_broker_kill_switch_gate.py` +
`test_broker_request_purpose.py` + `test_broker_order_intent_gate.py` +
`test_alpaca_client_runtime_revalidation.py` + `test_broker_safety.py` +
`test_universe_builder.py` + `test_paper_strategy_order_kill_switch_state.py` +
`test_paper_order_execution.py`) **289 passed, 1 warning**. 실제 Alpaca/Slack/Yahoo 호출 0회.
`order_history.csv`/`universe.csv`는 이전 사이클 기록값과 동일(불변). `.env`, kill
switch/notification 상태 파일, 승인 레코드는 변경하지 않았다. 상태는
`READY_FOR_CODEX_REVALIDATION`이며 독립 재검증 전까지 **Limited live review: BLOCKED**,
**Live trading: DO_NOT_ENABLE**이다.

---

## 2026-07-25 — CODEX-021 해결 및 CODEX-020 잔여분 종결 (RequestPurpose 재설계)

Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `66eda8a`/`ed452da`/`cf5601d`/`edc5ad5`, overall
verdict **FAIL**)에서 CODEX-016/017/018/019는 RESOLVED로 재확인됐으나, CODEX-020(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 CODEX-021(HIGH)이 제기됐다: `_request()`의 `order_side`는
필수 인자였지만 POST 경로와 의미적으로 결합되지 않아, `order_side=None`을 명시하면
`_check_kill_switch(None)`이 HTTP method/path를 확인하지 않고 즉시 반환해 direct
`_request("POST", "/v2/orders", order_side=None, ...)` 호출이 binary halt와 4-state kill
switch를 모두 우회했다.

- **CODEX-021**: `broker/alpaca_client.py`에 신규 `RequestPurpose` enum
  (`READ_ONLY`/`ENTRY_ORDER`/`EXIT_ORDER`/`CANCEL_ORDER`/`RECONCILIATION`)을 도입하고,
  `_request()`의 `purpose`를 기본값 없는 keyword-only 필수 인자로 만들었다. `isinstance`
  검사로 `None`을 포함한 잘못된 값을 `ValueError`로 세션 접근 전에 차단하고, 신규
  `_METHOD_PURPOSES` 매트릭스가 HTTP method와 purpose의 허용 조합을 강제한다(GET은
  `READ_ONLY`/`RECONCILIATION`만, POST는 `ENTRY_ORDER`/`EXIT_ORDER`만, DELETE는
  `CANCEL_ORDER`만). `_check_kill_switch()`는 `purpose`가 `ENTRY_ORDER`/`EXIT_ORDER`일 때만
  kill switch를 재조회하며, `order_side`는 payload의 `side`와 `purpose`가 일치하는지 확인하는
  2차 방어선으로만 쓰인다.
- **CODEX-020 잔여분**: 위 재설계로 함께 닫혔다. method+path 기반 주문 감지 백스톱 부재
  지적이 `_METHOD_PURPOSES` 매트릭스로 해결됐다. 조회·취소 경로(`get_account`,
  `get_positions`, `get_recent_orders`, `get_assets`, `get_order_by_client_order_id`,
  `cancel_order`)는 각각 `RequestPurpose.READ_ONLY`/`RECONCILIATION`/`CANCEL_ORDER`를 명시해
  kill switch 정책과 무관하게 계속 동작한다.

CODEX-016~019는 이번 사이클에서 재작업하지 않았다 — 관련 회귀 테스트만 재실행해 회귀 없음을
확인했다(`tests/test_paper_strategy_order_kill_switch_state.py`,
`tests/test_paper_strategy_order_notification_health.py`,
`tests/test_state_store_concurrency.py`, 도합 **36 passed, 1 warning**).

검증: 신규 `tests/test_broker_request_purpose.py`(신규 파일, purpose=None 명시적 거부, method+
purpose 불일치 거부, order payload side/purpose 불일치 거부 등)와
`tests/test_broker_kill_switch_gate.py` 확장(`purpose` 시그니처 반영 + 신규 테스트 3건).

전체: `venv/bin/python -m pytest -q` **536 passed, 0 failed, 2 warnings**(신규 경고 없음, 기존
urllib3/scanner 경고만). 집중 안전 테스트(`test_broker_kill_switch_gate.py` +
`test_broker_request_purpose.py` + `test_alpaca_client_runtime_revalidation.py` +
`test_broker_safety.py` + `test_universe_builder.py` +
`test_paper_strategy_order_kill_switch_state.py` + `test_paper_order_execution.py`) **255 passed,
1 warning**. 실제 Alpaca/Slack/Yahoo 호출 0회. `order_history.csv`/`universe.csv`는 이전 사이클
기록값과 동일(불변). `.env`, kill switch/notification 상태 파일, 승인 레코드는 변경하지 않았다.
상태는 `READY_FOR_CODEX_REVALIDATION`이며 독립 재검증 전 Limited live review는 `BLOCKED`,
실거래는 `DO_NOT_ENABLE`이다.

---

## 2026-07-24 — CODEX-020·CODEX-018 잔여분 수정

Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ee8d6`/`03962d3`/`cf4ada9`, overall verdict
**FAIL**)에서 CODEX-016/017/019는 RESOLVED로 재확인됐으나, CODEX-018(MEDIUM)이
PARTIALLY_RESOLVED로 남았고 신규 CODEX-020(HIGH)이 제기됐다: direct
`AlpacaBroker.submit_order()`가 `paper_strategy_order.py` wrapper의 kill switch 게이트를 거치지
않고 호출되면 binary halt와 다단계 kill switch 상태(`ENTRY_DISABLED` 등)를 모두 우회해 실제
HTTP가 나갔다. CODEX-018은 `_validate_runtime_safety()`가 현재 credentials(API key/secret)를
재검증하지 않는다는 지적이었다.

- **CODEX-020**: `broker/alpaca_client.py`의 `AlpacaBroker._request()`에 `order_side` 키워드
  전용 필수 인자(주문이 아니면 `None`, 매수/매도면 `"buy"`/`"sell"`)를 추가하고, 신규
  `_check_kill_switch()`가 `kill_switch.is_trading_halted()`와
  `kill_switch_state.is_entry_allowed()`/`is_liquidation_allowed()`를 매 호출마다 재조회해
  불허 시 세션 요청 전에 `RuntimeError`를 발생시킨다. 조회·취소 경로(`get_account`,
  `get_positions`, `get_recent_orders`, `get_assets`, `get_order_by_client_order_id`,
  `cancel_order`)는 `order_side=None`으로 명시해 kill switch와 무관하게 계속 동작한다.
  `order_side`를 생략하고 `_request()`를 호출하면 네트워크 접근 전에 `TypeError`가 발생한다.
  커밋 `66eda8a`.
- **CODEX-018 잔여분**: `_validate_runtime_safety()`에 `_validate_current_credentials_match_captured()`를
  추가해 매 요청마다 `BrokerConfig.from_env()`로 현재 환경 credentials를 다시 읽고, 생성 시점에
  캡처된 값과 `hmac.compare_digest()`로 비교한다. 누락/공백/회전/삭제/환경 읽기 실패는 모두
  요청 전에 차단한다. 커밋 `ed452da`.

검증: 신규 회귀 `tests/test_broker_kill_switch_gate.py`(25건, direct broker 호출이 binary/4-state
kill switch를 준수하는지, 조회·취소 경로는 영향받지 않는지, wrapper 경로와 direct 경로 판정이
일치하는지 검증) + `tests/test_alpaca_client_runtime_revalidation.py` 확장(44건, credential
삭제/회전/공백/읽기실패 각각 POST·GET·DELETE 3경로 파라미터라이즈) + `tests/test_broker_safety.py`,
`tests/test_universe_builder.py` 기존 fake broker 호출부를 `order_side` 키워드에 맞춰 갱신.

전체: `venv/bin/python -m pytest -q` **489 passed, 0 failed, 2 warnings**(신규 경고 없음, 기존
urllib3/scanner 경고만). 집중 안전 테스트(`test_broker_kill_switch_gate.py` +
`test_alpaca_client_runtime_revalidation.py` + `test_broker_safety.py` + `test_universe_builder.py`
+ `test_paper_strategy_order_kill_switch_state.py` + `test_paper_order_execution.py`) **208 passed,
1 warning**. 실제 Alpaca/Slack/Yahoo 호출 0회. `order_history.csv`/`universe.csv` SHA-256이
`CODEX_REVIEW.md`에 기록된 값과 동일(불변). `.env`, kill switch/notification 상태 파일, 승인
레코드는 변경하지 않았다. 상태는 `READY_FOR_CODEX_REVALIDATION`이며 독립 재검증 전
Limited live review는 `BLOCKED`, 실거래는 `DO_NOT_ENABLE`이다.

---

## 2026-07-23 — CODEX-016·018 최종 보완

Codex 재검증에서 남은 CODEX-016(HIGH, sell side 누락)과 CODEX-018(MEDIUM,
POST/reconciliation runtime gate 우회)을 `47ee8d6`에서 보완했다. side는 두 주문
계층에서 keyword-only 필수값이며 정확한 `buy`/`sell`만 허용한다. 모든 Alpaca HTTP는
생성 시점 config와 요청 시점 환경을 검사하는 단일 `_request()`를 사용한다.

검증: 집중 188 passed, 전체 네 실행 방식 모두 **443 passed, 0 failed, 2 warnings**.
실제 외부 호출 0회, 운영 CSV 해시·크기·mtime 불변. 상태는
`READY_FOR_CODEX_REVALIDATION`이며 독립 재검증 전 limited live review는 `BLOCKED`,
실거래는 `DO_NOT_ENABLE`이다.

---

## 2026-07-22 — Phase 2 구현 완료 (초단타 관심종목 선별 엔진)

`scalping_watchlist/` 패키지로 Stage A(거래가능성 재검증)~E(설명 가능한 가중합 점수) 파이프라인을 구현했다(커밋 `4a96883`).

- 재사용: `daily_candidate_scanner.calculate_rsi`/`calculate_atr`, `market_hours.eastern_now`/`get_us_market_session`, `market_guard.is_us_trading_day`.
- 재사용하지 않기로 한 것: 기존 JSON 룰 엔진(`evaluate_filter`, 불명확 필드 시 fail-open) — Phase 2 원칙("불명확하면 포함하지 않는다")과 배치되어 전용 함수로 신규 작성. 근거는 `DECISION_LOG.md`.
- 신규 구현(저장소에 대응 로직 없었음, 확인됨): 다중 사이클 반복탐지 스트릭 추적(`repeat_tracker.py`), 유동성 대체지표(`liquidity_score`, `spread_estimate`는 데이터 소스 부재로 항상 `NOT_AVAILABLE`), Stage E 점수 엔진.
- 파일 안전성: `order_history.csv`와 동일한 기법(temp file+fsync+os.replace, `fcntl.flock`)을 `scalping_watchlist/atomic_io.py`에 독립 재구현(Phase 1 파일 미변경 원칙 준수).

테스트: 신규 34건(정상 선별/점수순 정렬/최대 관심종목 수/동점 결정성, 가격·거래량·거래대금·상대거래량·변동성·유동성 부족 차단, 데이터 누락·지연·비정상치 차단, 최초/재등장/타거래일 초기화/ET 경계/재등장 구분/동시성 lost-update 방지, 하위점수-가중치 일치/점수 범위/NaN·Infinity 차단/입력순서 무관성, 원자적쓰기 실패 시 원본 보존/잠금 타임아웃/손상파일 fail-closed, Fake provider only/개별 provider 오류 격리) 전부 통과, 동시성 테스트 5회 반복 안정.

전체 회귀: **183 passed, 0 failed** (기존 149 + 신규 34), 저장소 루트/상위 디렉터리 동일 결과. 실제 Alpaca/Slack/Yahoo 호출 0회. `order_history.csv` 해시 불변 — Phase 1 운영 로직/파일 미변경 확인.

Phase 2 상태: **`IMPLEMENTED`**(Claude 자체 검증). Codex의 `PROCEED` 판정 전까지 `VALIDATED`로 승격하지 않음.

---

## 2026-07-21 — Phase 1 최종 Codex 판정 및 Phase 2 착수

`CODEX_REVIEW.md` 최종 독립 검증(대상 커밋 `05757fe`/`0c2dab4`/`16a1ee4`/`56e11be`) 결과: **overall verdict PASS_WITH_CONDITIONS**. CODEX-001~009 전부 RESOLVED, 신규 Finding 없음, 회귀 없음. 전체 테스트 149 passed, 집중 테스트 106 passed, 동시성 테스트 6 passed×5회, 실제 외부 API 호출 0회, 운영 CSV/runtime 변경 없음.

Phase 판정:
- **Phase 1A(주문 진입 안전성): VALIDATED**
- **Phase 1B(부분체결·포지션 생명주기): DEFERRED_TO_PHASE_5** — Phase 1 자체 판정은 `KEEP_IN_PROGRESS`(Codex 표현), Codex Finding이 아니라 Phase 1 승인 기준 자체의 미충족 항목.
- **Phase 2: PROCEED**

이 결과를 `SCALPING_V1_ROADMAP.md`/`CURRENT_STATUS.md`/`DECISION_LOG.md`에 반영하고 Phase 2(초단타 관심종목 선별 엔진) 착수. Phase 2는 Claude 자체 테스트만으로 `VALIDATED` 처리하지 않고 `IMPLEMENTED`로 표기하며, Codex의 `PROCEED` 판정 후에만 `VALIDATED`로 승격한다.

---

## 2026-07-21 — Phase 1 추가 수정 사이클 (CODEX-007~009)

독립 재검증(대상 커밋 `9688a13`/`b93a08a`/`22a6651`/`962eb69`/`1cc784b`, verdict FAIL)이 CODEX-003/004/005는 RESOLVED로 최종 확인했지만, CODEX-001/002/006을 PARTIALLY_RESOLVED로 되돌리고 신규 CODEX-007(HIGH)/008(HIGH)/009(MEDIUM)를 제기했다. 지시서 우선순위(007→008→009)대로 처리했다.

- **CODEX-007**: `load_order_history()`가 날짜 파싱 성공 여부만 확인하던 것을 `validate_order_date_str()`(정규식+실제 달력 유효성+원본 왕복 일치)로 교체. 단 하나의 비정규 `order_date`도 전체 이력을 `CORRUPTED_HISTORY`로 판정해 신규 주문을 차단한다(자동 마이그레이션 없음, 진단 전용 `diagnose_order_history_dates()` 별도 제공). 이로써 CODEX-002의 잔여 위험이 해소되어 CODEX-002도 RESOLVED로 승격. (`05757fe`)
- **CODEX-008**: `order_reconciliation.csv` 전용 `fcntl.flock` 잠금 도입(`order_history`용 잠금 로직을 `_file_lock()`으로 일반화해 재사용). `merge_reconciliation_state()`가 상태 후퇴 금지·`filled_qty` 비감소·가격 비소거를 강제하는 단조 병합을 수행하며, 손상된 reconciliation 파일은 `ReconciliationUnavailable`로 fail-closed(자동 재초기화 금지). reconciliation 저장 실패는 이제 주문 예약 자체를 차단하도록 전파되고, `main()`의 즉시 상태 갱신과 reconciliation 스냅샷이 동일한 함수 결과를 공유해 두 파일이 서로 다른 즉시 상태를 기록하는 문제도 제거. **실제 `multiprocessing.Process` 2건**으로 동시 갱신 시 최종 상태가 후퇴하지 않고 lost update가 없음을 재현 검증. 이로써 CODEX-006의 잔여 위험도 해소되어 RESOLVED로 승격. (`0c2dab4`)
- **CODEX-009**: `universe_builder.py`가 공통 broker 안전검사를 우회해 환경변수 기반 URL로 직접 GET하던 것을, `AlpacaBroker.get_assets()`(기존 `_request()` 게이트 재사용)로 교체. 8종 endpoint 변조 시나리오(스킴 다운그레이드·유사 호스트명·비표준 포트·경로/쿼리 조작·userinfo·빈값/공백)를 파라미터라이즈드 테스트로 검증. 저장소 전체 grep으로 다른 Alpaca 직접 호출 경로가 없음(스크래치 파일 2개 제외, 이미 collect_ignore 대상)을 확인. 이로써 CODEX-001의 잔여 위험도 해소되어 RESOLVED로 승격. (`16a1ee4`)

검증: 저장소 루트/상위 디렉터리 4가지 pytest 조합 모두 **149 passed, 0 failed**. 집중 테스트(broker_safety + paper_order_execution + universe_builder) **106 passed**. 동시성 관련 테스트(threading + multiprocessing) 5회 반복 모두 **6 passed**로 안정. `git diff --check` 통과. `order_history.csv` 해시/크기/mtime 사이클 전후 불변.

**잔여 판단(NEEDS_USER_DECISION)**: `order_history.csv`와 `order_reconciliation.csv`는 각각 자체 잠금과 원자적 쓰기를 갖지만, 두 파일에 걸친 단일 트랜잭션은 없다. 안전 크리티컬 판단(중복/일일한도)은 전적으로 `order_history.csv`에만 의존하므로 이 잔여 위험이 실거래 안전성 자체를 위협하지는 않지만, 프로세스가 두 파일에 대한 쓰기 사이에 강제 종료되면 다음 `reconcile_pending_orders()` 실행 전까지 두 파일이 일시적으로 불일치할 수 있다. SQLite 전환 여부는 `DECISION_LOG.md`에 사용자 판단 대기 항목으로 기록했다(임의 전환하지 않음).

CRITICAL 0건, HIGH 전부(001/002/003/005/006/007/008) RESOLVED, MEDIUM 전부(004/009) RESOLVED. Phase 1은 부분 체결의 "포지션 상태" 완전 반영이 Phase 5 범위라 여전히 `IN_PROGRESS`.

---

## 2026-07-21 — Phase 1 재수정 사이클 (CODEX-001~006)

`CODEX_REVIEW.md`(대상 커밋 `fe2988c`/`dc9bff9`, verdict FAIL, Phase 2 DO_NOT_PROCEED)의 지시서 우선순위(001→002→003→006→005→004)대로 재수정했다.

- **CODEX-001**: `AlpacaBroker._request()`(GET 경로)가 `submit_order()`와 동일한 안전검사를 거치지 않던 문제 수정. 모든 broker 호출이 매번 `self.config`를 재검증하도록 통일. (`9688a13`)
- **CODEX-002**: `load_order_history()`를 fail-closed로 전환(`MISSING_HISTORY`/`CORRUPTED_HISTORY` 구분), 거래일 판정을 서버 로컬 시간에서 `market_hours.eastern_now()`(America/New_York) 기준으로 변경. (`b93a08a`)
- **CODEX-003**: `order_history.csv` 쓰기를 임시파일+fsync+`os.replace()` 원자적 방식으로 전환, `fcntl.flock` 기반 프로세스 잠금 도입. `try_reserve_order()`가 잠금 하에 이력을 다시 읽고 중복/일일한도를 재검사한 뒤에만 기록. `threading` 기반 실제 동시성 재현 테스트로 lost update 없음을 확인. (`b93a08a`)
- **CODEX-006**: 스키마 동결 원칙을 지키며 별도 파일 `order_reconciliation.csv`로 `client_order_id`/체결 상태 추적을 추가. 매 실행 시작 시 비종결 상태를 broker와 대조(`reconcile_pending_orders`), partially_filled≠filled 유지, 미인식 주문은 `MANUAL_REVIEW`(재주문 없음). (`22a6651`)
- **CODEX-005**: 저장소 루트 `conftest.py`에 `collect_ignore` 추가 — 상위 디렉터리에서 경로를 명시해 pytest를 실행해도(이 경우 `testpaths`가 무시됨) 루트 스크래치 스크립트가 수집되지 않도록 함. (`962eb69`)
- **CODEX-004**: 동일 `conftest.py`가 수집 시점에 저장소 루트를 `sys.path`에 직접 삽입 — 실행 위치/ini 해석 여부와 무관하게 import가 안정적으로 동작. (`962eb69`)

검증: 저장소 루트(`pytest -q`, `python -m pytest -q`)와 저장소 상위 디렉터리에서 경로 명시(`pytest us-stock-trading -q`, `python -m pytest us-stock-trading -q`) 4가지 조합 모두 **97 passed, 0 failed**. 동시성 테스트 5회 반복 재실행으로 플레이키니스 없음 확인. `git diff --check` 통과. Live URL이 코드 어디에서도 기본값/폴백으로 쓰이지 않음을 grep으로 재확인. `order_history.csv` 해시가 이번 사이클 전후로 불변(`a61104cf...`) — 실제 운영 파일 미변경.

CRITICAL 0건, HIGH 5건(001/002/003/006/005) 전부 RESOLVED, MEDIUM 1건(004) RESOLVED. Phase 1은 부분 체결의 "포지션 상태 반영"이 Phase 5 범위라 여전히 `IN_PROGRESS`(정책적으로 `VALIDATED`로 올리지 않음 — 상세는 `CURRENT_STATUS.md`/`SCALPING_V1_ROADMAP.md`).

---

## 2026-07-21 — Codex 독립 검증 수정 사이클

- HIGH 3건과 MEDIUM 1건을 실제 코드/테스트 실행으로 재현하고 모두 수정했다.
- 주문 모드는 정확히 `paper`이고 endpoint는 공식 Alpaca Paper URL인 경우에만 허용한다.
- 주문 이력에서 당일 주문 수를 복구하며, 제출 전에 `PENDING_SUBMISSION` 예약을 저장한다.
- `pytest.ini`의 import 경로를 고정했다.
- 회귀 테스트 5건을 추가/갱신했고 전체 결과는 70 passed, 0 failed, 2 warnings다.
- 실제 Alpaca/Slack 호출, 운영 서버 변경, Live 활성화, 데이터 삭제는 수행하지 않았다.
- Phase 1 부분 체결 승인 기준은 미충족이므로 상태는 `IN_PROGRESS`다.

Claude 자체 검증 결과 기록 (외부 검증자의 `CODEX_REVIEW.md`와는 별개).

---

## 2026-07-21 — Phase 0 + Phase 1 갭 수정 사이클

### 범위
- `docs/autonomous/` 8종 문서 신규 생성
- `paper_strategy_order.py`의 `position_rate` 하드코딩(0.01) 버그 수정
- `tests/test_paper_order_execution.py`에 비정상 주문 금액 차단 테스트 2건 추가

### 실행 명령 및 결과
```
./venv/bin/python -m pytest -q
```
```
65 passed, 2 warnings in 1.68s
```
- 이전 기준선(63) 대비 신규 2건 추가, 기존 63건 전부 유지(회귀 없음).
- 실제 Alpaca/Slack 네트워크 호출: 0회 (전부 `FakeBroker`/`DummySession`/monkeypatch).
- 실제 운영 CSV(`order_history.csv` 등) 변경: 0건 (전부 `tmp_path`).

### 코드 변경 검증
- `position_rate = (order_qty * result["price"]) / equity` (equity<=0이면 `inf`로 안전 측 처리) — `risk_config.MAX_POSITION_RATE` 등 기존 임계값은 미변경, 값을 실제로 연결만 함.
- 기존 happy-path 테스트(등가/가격 비율 0.01)가 그대로 통과함을 확인 — 회귀 없음.
- 신규 테스트로 equity 대비 과도한 주문가치(20%)가 실제로 `run_order_safety_check`에서 차단됨을 확인.

### 테스트하지 못한 영역
- 부분 체결(partially_filled) 처리 — Phase 5(포지션 생명주기) 선행 필요, 현재 아키텍처에 해당 개념이 없어 의미 있는 테스트 불가. `SCALPING_V1_ROADMAP.md` Phase 1/5에 명시.
- `analyze_stock`의 RSI/MA200/거래량 계산 자체의 수치 정확성 — 이번 사이클은 안전장치 경로만 검증, 계산 로직은 monkeypatch로 우회.

### 안전 관련 변경
- `position_rate` 실계산 도입은 기존에 사실상 비활성 상태였던 안전장치를 활성화하는 방향이므로 리스크를 낮추는 변경. 임계값 자체는 무변경.

### 운영 영향
- 없음. 운영 서버 미접속, systemd/cron/nginx 미변경, `.env` 실값 미변경.

### 남은 위험
- `run_order_safety_check` 호출부에 여전히 try/except가 없어, 한 심볼에서 안전장치가 발동하면 해당 실행의 나머지 후보도 함께 스킵됨(의도된 보수적 동작으로 유지, `DECISION_LOG.md` 참고).
- `position_rate` 계산에 사용하는 `equity`는 매 실행 시 1회만 조회되며 루프 중 갱신되지 않음(기존 동작과 동일, 이번 변경으로 새로 생긴 위험은 아님).
