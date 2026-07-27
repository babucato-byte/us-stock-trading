# FINAL_VALIDATION_PACKAGE — Stage 3~11 + CODEX-023~038 (2026-07-28)

이 문서는 일곱 사이클의 최종 산출물이다: (1) Stage 3~10 연속 구현, (2)~(5) Codex 1~4차 독립
검증(CODEX-023~034, 매 사이클 상세는 이전 버전 참고, git 이력에 보존됨)에 대한 통합 수정, (6)
Codex 5차 통합 재검증(overall verdict `FAIL`, CODEX-034 PARTIALLY_RESOLVED + 신규
CODEX-035/036/037 HIGH, CODEX-038 LOW)에 대한 최종 수정 사이클, (7) 그 직후 사용자가 별도로
지시한 **Stage 11 — Account/Risk/Sizing/Execution Engine 계층 분리**. Stage 11은 Codex
재검증에 대한 응답이 아니라 사용자가 명시적으로 요청한 아키텍처 리팩터링이며, CODEX-034~038의
수정 내용을 대체하지 않고 그 위에 새 계층을 추가한다. 이 문서 자체는 다음 Codex 재검증 요청 전
최종 스냅샷이며, **실거래 승인이나 활성화를 의미하지 않는다.**

## 0. 최종 상태

```
상태: READY_FOR_FINAL_CODEX_REVALIDATION
```

- `approved: false`, `live_enabled: false` 유지([LIVE_APPROVAL_RECORD.md](../live_review/LIVE_APPROVAL_RECORD.md)).
- Live trading: **`DO_NOT_ENABLE`**.
- Limited live review: **`BLOCKED`**(이번 수정이 아직 Codex 재검증을 거치지 않았으므로).
- `main`/`origin`: 어느 것도 건드리지 않음(아래 §8).
- `READY_FOR_30K_KRW_LIMITED_LIVE_REVIEW`/`LIVE_READY`/`LIVE_APPROVED`/`PRODUCTION_READY` 등의
  표현은 이 문서를 포함해 어디에도 사용하지 않았다.

## 1. 검증 대상 커밋

브랜치: `orchestrator/20260725-013740-us-stock-trading`.

### 1a~1f. Stage 3~10 ~ CODEX-035/036/037/038 (커밋 `415c129`~`06a77c8`)

이전 `FINAL_VALIDATION_PACKAGE.md`(§1a~§1f, 커밋 `06a77c8` 시점)에서 이미 다섯 차례 Codex 검증을
거친 46개 커밋. 상세는 이전 버전 참고(git 이력에 보존됨).

### 1g. Stage 11 — Account/Risk/Sizing/Execution Engine 계층 분리 (신규, 이번 패키지의 실제 검증 대상)

| # | 커밋 | 내용 |
|---|---|---|
| 42 | `3494fe3` | Layer Account/Risk/Sizing/Execution Engines per architecture requirement (최신, `HEAD`) |

이 커밋은 **Codex 재검증에 대한 응답이 아니라 사용자가 직접 지시한 아키텍처 리팩터링**이다 —
CODEX-034~038 각각에 대한 회귀 테스트는 그대로 유지된다(§2). 이 범위 이전(CODEX-001~022 원격
수정 사이클)은 이미 별도로 Codex 최종 독립 검증을 거쳐 `PASS_WITH_CONDITIONS`로 종결됨
(`docs/autonomous/CODEX_REVIEW.md`의 해당 이력, 커밋 `d38cb95`). 이번 문서는 §1a~§1g 전체(47개
커밋)를 검증 대상으로 제출하되, 실질적으로 새로 검증이 필요한 것은 §1g(Stage 11 계층 분리)다 —
§1a~§1f는 이미 다섯 차례 Codex의 눈을 거쳤고 그 결과가 바로 이번 리팩터링의 근거이기 때문이다.

## 2. Stage/사이클별 변경 파일 및 테스트 결과

| 범위 | 신규 테스트 | 결과 |
|---|---|---|
| Stage 3~10(21개 커밋) | 251건 | 통과 |
| CODEX-023~027(1차 수정 사이클) | 103건 | 통과 |
| CODEX-024/026/028/029/030(2차 재수정 사이클) | 50건 | 통과 |
| CODEX-024/026/028/031/032/033(3차 통합 수정 사이클) | 13건 | 통과 |
| CODEX-034 + 잔고 비율 사이징(4차 수정 사이클, watchlist affordability 포함) | 108건 | 통과 |
| CODEX-035/036/037/038(5차 수정 사이클) | 81건 | 통과 |
| **Stage 11**(trusted_operator_config/account_engine/risk_engine/sizing_engine/execution_engine + watchlist affordability 확장) | 174건 | 통과 |
| **합계(§1g만)** | **174건 신규**(직전 1,125 → 1,299) | **통과** |

## 3. 전체 테스트 결과

```
$ venv/bin/python -m pytest -q
1299 passed, 2 warnings
```

- 이 문서 작성 직전 최종 실행 결과(2026-07-28). 실패 0건.
- Stage 3~10 착수 시점 베이스라인 613 passed → Stage 3~10 완료 시점 820 passed → CODEX-023~027
  수정 완료 시점 923 passed → CODEX-024/026/028/029/030 수정 완료 시점 973 passed →
  CODEX-024/026/028/031/032/033 수정 완료 시점 986 passed → CODEX-034/잔고 비율 사이징 수정 완료
  시점 1,044 passed → CODEX-035/036/037/038 수정 완료 시점 1,125 passed → 이번 Stage 11 완료
  시점 **1,299 passed**.
- 두 warning은 기존 urllib3(LibreSSL) 경고와 `test_scanner.py`의 의도된 unknown-field 경고로,
  이번 범위와 무관한 기존 항목이다.

## 4. 아키텍처 요약

```
Market Data
    ▼
Strategy Engine (strategy/interface.py::EvaluationResult)
    │  strategy_id/signal/entry_price/stop_price/target/confidence만 산출
    │  계좌 잔고·비율·최종 수량·주문 가능 금액은 절대 산출/신뢰하지 않음 (구조적으로 강제)
    ▼
Signal
    ▼
Risk Engine (live_readiness/risk_engine.py) — 신규
    │  전략의 수량을 사용하지 않고 entry/stop price + daily-loss-remaining으로
    │  risk_based_qty를 독자 계산. 모든 숫자 finite 검증, 하나라도 무효면 전체 차단.
    ▼
Account Engine (live_readiness/account_engine.py) — 신규
    │  AccountSnapshot: broker.get_account() + entry_reservation_ledger 기반
    │  effective_cash = min(broker_cash, non_margin_available_cash) (margin 미사용)
    │  cash_usage_percent/포지션·진입 한도는 trusted_operator_config.py에서만
    ▼
(신규 building block, 미배선) 관심종목 잔고 affordability 필터 (live_readiness/watchlist_affordability.py)
    │  STALE_ACCOUNT_STATE 신설, fractionable 종목은 1주 가격이 잔고 초과해도
    │  최소주문금액 충족 시 후보 유지
    ▼
Sizing Engine (live_readiness/sizing_engine.py) — 신규
    │  actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)
    │  세 값 모두 명시적으로 유효할 때만 계산, apply_entry_price_buffer()로 슬리피지 반영
    ▼
Execution Engine (live_readiness/execution_engine.py) — 신규
    │  ValidatedOrderCommand + broker 호출 유일 경로 (정적 grep 테스트로 강제)
    │  만료/변조/symbol 불일치/기존 예약 불일치 시 broker 호출 0회
    ▼
포지션 생명주기 (positions/) ──── SQLite canonical (state_store/, positions/position_events)
    │  (Stage 3~10과 동일, 이번 사이클 미변경)
    ▼
Broker (broker/alpaca_client.py::AlpacaBroker.submit_order())
    │  live_entry_context 게이트, 예산 authoritative 산출, 정의된 rejection만 release,
    │  ambiguous 실패는 SUBMISSION_UNKNOWN 유지 (CODEX-026~037, 이번 사이클 미변경)
    ▼
운영 관제 (ops_dashboard/)
```

`paper_strategy_order.py`(기존 Paper 주문 흐름, `AlpacaBroker.submit_order()`로의 기존 직접
호출 포함)는 **legacy compat 경로로 명시적으로 유지**되며, 동작을 전혀 바꾸지 않았다 —
`docs/autonomous/PROJECT_CONSTITUTION.md`의 "계층 분리 원칙" 참고.

## 5. 각 구성요소 상세

### 5.1~5.8

이전 `FINAL_VALIDATION_PACKAGE.md`(커밋 `06a77c8`) §5.1~§5.8과 동일, 이번 사이클에서 미변경
(broker/alpaca_client.py, live_readiness/order_gateway.py의 핵심 게이트 로직은 건드리지 않음 —
`order_gateway.py`는 `trusted_operator_config.py`에서 상수를 가져오도록 import만 변경).

### 5.9 Stage 11 — Account/Risk/Sizing/Execution Engine 계층 분리 (신규)

- **`live_readiness/trusted_operator_config.py`**: `cash_usage_percent` 트러스트 상한(50%)과
  `MAX_CONCURRENT_LIVE_POSITIONS`/`MAX_DAILY_LIVE_ENTRIES`의 단일 소스, 매 호출 재검증
  (`get_*()` 함수 형태, 손상된 값은 fail-closed).
- **`live_readiness/account_engine.py`**: `AccountSnapshot`(frozen dataclass) —
  `effective_cash_krw = min(broker_cash_krw, non_margin_available_cash_krw)`, pending/unknown/
  reconciliation-required/open-position 노출은 `entry_reservation_ledger`의 durable SQLite
  집계. broker 조회 실패/cash 무효/Paper-Live 모호/계좌 ID 불일치 시
  `AccountEngineError`(fail-closed).
- **`live_readiness/risk_engine.py`**: `compute_risk_decision()`이 전략의 진입가/손절가와
  daily-loss-remaining으로 risk_based_qty를 독자 계산 — 전략이 수량을 선언할 방법 자체가 없다
  (`EvaluationResult`에 그런 필드가 없음). 모든 숫자 finite 검증.
- **`live_readiness/sizing_engine.py`**: `compute_sizing_decision()`이
  `actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)`를 계산. 세 후보 모두
  None/NaN/Infinity/bool/문자열/음수 검증을 통과해야 함. `strategy_max_qty=0`은 무효 입력(캡
  없음은 `None`으로 표현) — CODEX-037의 optional cap 규약과 동일.
- **`live_readiness/execution_engine.py`**: `ValidatedOrderCommand`(frozen) + broker 호출의
  유일한 사전 검증 지점. `submit_validated_command()`가 broker 호출 전에 (1) 타입 검증 (2) 만료
  검증 (3) qty×price==estimated_notional 검증 (4) symbol 일치 검증 (5) `client_order_id`의 기존
  SQLite 예약과의 symbol 불일치 검증을 수행 — 5개 중 하나라도 실패하면 broker 호출 0회.
  `reservation_id`는 command가 아니라 반환되는 `ExecutionResult`에 담긴다(이 저장소의 유일한
  예약 지점이 여전히 `broker.submit_order()` 내부이므로 — `DECISION_LOG.md` Stage 11 결정 2).
- **아키텍처 경계 강제**: `tests/test_execution_engine.py`의 정적 grep 테스트가 저장소 전체에서
  `broker.submit_order(`/`self.broker.submit_order(` 패턴의 실제 호출부(변수명 기준)를 찾아
  허용 목록(`execution_engine.py`, `broker/alpaca_client.py`, `paper_strategy_order.py`) 밖에
  있으면 실패시킨다.
- **`live_readiness/watchlist_affordability.py`**: `STATUS_STALE_ACCOUNT_STATE`(존재하지만 만료된
  스냅샷, `UNKNOWN_ACCOUNT_STATE`와 별도) + `buffered_entry_price`/`account_snapshot_at` 필드.

## 6. 외부 API 호출 현황

일곱 사이클 전체 구현·테스트 과정에서 실제 Alpaca API, 실제 Slack Webhook, 실제 Yahoo/기타 외부
데이터 API를 호출한 적이 **0회**다. 모든 테스트는 fake/sequenced broker, 실제 `AlpacaBroker` +
네트워크 호출 시 예외를 던지는 세션 더블, tmp_path 격리 파일로만 동작한다. 신규 Account/Execution
Engine 테스트도 전부 fake broker 객체(duck-typed `get_account()`/`submit_order()`)만 사용한다.

## 7. 운영 파일 변경 현황

`order_history.csv`, `universe.csv`, `strategy_performance.csv`는 일곱 사이클 내내 **바이트
단위 및 mtime까지 불변**(md5/mtime 동일, §12 참고). 신규 SQLite 관련 테스트(`test_account_
engine.py`, `test_execution_engine.py`)는 `STATE_STORE_DB_FILE`/`POSITION_STORE_FILE`을
`tmp_path`로 격리하고 `entry_reservation_ledger._LOCK_FILE`을 monkeypatch — 전체 회귀 실행
전후 실제 저장소 루트 `TRADING_STATE.db*`/`LIVE_ENTRY_RESERVATION.lock`이 생성되지 않음을
확인했다.

## 8. main/origin 및 approved/live_enabled 현황

- `main`은 일곱 사이클 내내 전혀 이동하지 않았다.
- `origin`으로 push한 적 없음.
- `approved: false`, `live_enabled: false`는 변경하지 않았다.
- Kill Switch 해제, Live API Key 입력, 실제 주문 실행, 테스트 삭제/완화, 기존 리스크 한도 완화
  등 금지된 행위는 수행하지 않았다 — Stage 11은 기존 게이트(order_gateway.py/alpaca_client.py의
  CODEX-034~037 로직)를 대체하지 않고 그 위에 신규 계층을 추가했을 뿐이며, 기존 회귀 테스트
  1,125건은 단 하나도 수정되지 않았다(단, `test_watchlist_affordability.py`의 헬퍼가 신규
  STALE_ACCOUNT_STATE 검증을 통과하도록 `as_of`/`now` 인자를 추가로 받게 됐다 — 기존 assertion은
  그대로 유지).

## 9. 남은 TBD_OPERATOR 항목

`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md` + `docs/live_review/LIMITED_LIVE_30K_KRW_
PLAYBOOK.md` §7: 실계좌, 실환율(FX rate provider 연동 자체가 미구현), Live API Key,
`cash_usage_percent`/`TRUSTED_CASH_USAGE_PERCENT_CEILING`의 실제 배포값(50%가 최종 승인값인지
운영자 확인 필요), 실 승인자, 배포 시각, 롤백 담당자, 실제 Alpaca 최소 주문 금액, 실제 파일럿
종목 allow-list 내용. 어느 항목도 추정하여 확정하지 않았다.

## 10. 알려진 위험 (Codex 재검증 시 특히 확인 필요)

1. **SQLite canonical 범위가 orders/fills까지 포함하지 않음**: 진입 주문 이력은 여전히 CSV
   기반(`DECISION_LOG.md` 결정 1).
2. **`ENTRY_DISABLED` 자동 배선 미완료**: `NEEDS_USER_DECISION`으로 유지.
3. **CODEX-026/029 게이트가 `AlpacaBroker`의 향후 신규 메서드를 자동으로 보호하지 않음**:
   `submit_order()`에만 배선.
4. **entry 경로의 crash-safe reconciliation이 여전히 수동 트리거**: `reconcile_by_client_order_id()`
   는 단위 테스트로만 검증됐고, 재시작/크래시 복구 경로에 자동 배선되지 않았다.
5. **`account_cash_snapshot` 전달이 opt-in이며 production 배선이 아직 없음**: 실제 broker 잔고를
   조회해 `LiveEntryContext`에 채워 넣는 production caller가 아직 존재하지 않는다.
6. **watchlist affordability + Stage 11 신규 엔진 5종이 실제 스캔·주문 파이프라인에 미배선**
   (신규 초점): `trusted_operator_config.py`/`account_engine.py`/`risk_engine.py`/
   `sizing_engine.py`/`execution_engine.py`는 전부 순수 building block으로만 존재 — 실제
   `daily_candidate_scanner.py`/`paper_strategy_order.py::main()`과의 통합은 이번 사이클 범위
   밖이며, 별도의 명시적 배선 결정이 필요하다.
7. **`ValidatedOrderCommand.reservation_id`가 command 생성 시점이 아니라 broker 호출 이후에만
   확정됨**(신규): 사용자 지시서의 "필수 필드" 문구와 완전히 일치하지 않는 설계 결정
   (`DECISION_LOG.md` Stage 11 결정 2) — 이 저장소의 기존 단일-예약-지점 아키텍처와의 충돌을
   피하기 위함.
8. **비용/정책 ASSUMPTION 다수**: 백테스트 비용 가정, 선택 엔진 가중치, 사이징 최소 주문 금액 등.
9. **미검증 YouTube 전략 후보 4건**: 어떤 주문 경로와도 연결되어 있지 않다.
10. **Phase 3(1분봉 실시간 수집/폴링 인프라) 미착수**.
11. **동시성 경쟁 조건 발견 이력**: 이전 사이클(CODEX-029/030)에서 1건을 발견·수정했고, 유사한
    패턴이 코드베이스 다른 곳에 더 있는지는 아직 전수 조사하지 않았다.

## 11. 검증 중점 영역 (Codex에게 요청)

1. Stage 11이 기존 CODEX-034~037의 안전장치(HTTP status 분류, authoritative 예산/비율, NaN cap
   차단)를 실질적으로 대체하거나 우회하지 않는지 — `order_gateway.py`/`alpaca_client.py`의 핵심
   로직이 전혀 수정되지 않았음을 diff로 재확인.
2. `live_readiness/execution_engine.py`의 정적 grep 아키텍처 가드가 실제로 신뢰할 수 있는지(우회
   가능한 패턴이 있는지), 그리고 이 가드가 "Strategy가 직접 Broker를 호출할 수 없다"는 요구사항의
   충분한 강제 수단인지, 아니면 런타임 강제가 추가로 필요한지 판단 요청.
3. `account_engine.py`의 `effective_cash = min(broker_cash, non_margin_available_cash)` 계산이
   실제 Alpaca 계좌 응답 스키마(margin 계좌 포함)에서도 안전한지 — `non_marginable_buying_power`
   필드가 없는 계좌 유형에서의 폴백 동작(같은 cash 값 사용) 검증.
4. `sizing_engine.py`의 `actual_qty = min(balance/risk/strategy)`가 세 후보 중 하나라도 무효일 때
   반드시 예외를 던지고 부분 결과를 절대 반환하지 않는지 fault-injection으로 재확인.
5. §10.7의 `reservation_id` 설계 결정이 실거래 승인 시점에 실제 문제가 되는지, 아니면 현재
   구조(broker 호출 결과에 실림)로 충분한지 평가 요청.
6. 전체 테스트(1,299건)가 실제 네트워크/운영 파일 변경 없이 격리되어 있는지 임의 표본 재확인.
7. 이전 사이클에서 RESOLVED/PARTIALLY_RESOLVED였던 항목들이 Stage 11의 코드 변경(상수 이전,
   신규 모듈 추가)으로 인해 실질적으로 재발/회귀하지 않았는지.

## 12. SHA-256 (주요 안전 크리티컬 파일, 일곱 사이클 내내 미변경 확인용)

```
8b8c358fe87634ef74bed0699a6ba0f8e1ebe345cd58aa238c795fd88b179514  docs/autonomous/CODEX_REVIEW.md
27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe  docs/live_review/LIVE_APPROVAL_RECORD.md
043a30a5dc9751c062a36a82d4d75fdbb47903a040fb7b9ab86509f047843d84  risk_config.py
34411cf9ff530e850e8de5700a19c47aa71369528f6f541c8edd0e553b9df65e  broker/broker_config.py
408e94af606ce9045b46c0b3c8aeb07f4c9ee140a595f8bab5d198397700f389  kill_switch_state.py
d06ef475cc0fa721faedd986f1eaaab6b4ac0e0192ed4bedd3b0d4d009e6c991  order_intent_ledger.py
1194ccc44ebd2fafd98f1fb07d56f5823fadbdaad29a0ef9e2e8aadd63b7e1e3  broker/alpaca_client.py
```

(`CODEX_REVIEW.md`의 SHA-256은 CODEX-035/036/037/038 사이클(커밋 `9d294e3`) 이후 변경되지 않았다
— Stage 11은 Codex 재검증에 대한 응답이 아니므로 이 파일을 건드리지 않았다. 나머지 6개 파일은
이전 패키지와 SHA-256이 완전히 동일 — 일곱 사이클 내내 전혀 건드리지 않았음을 재확인한다.
`broker/alpaca_client.py`도 이번 목록에 추가했지만 이번 Stage 11 사이클에서는 변경되지 않았다
— CODEX-035/036/037 사이클(커밋 `40abc58`) 이후의 값과 동일함을 재확인한 것뿐이다.
`live_readiness/order_gateway.py`, `live_readiness/account_cash.py`,
`live_readiness/watchlist_affordability.py`, `paper_strategy_order.py`는 이번 사이클에서
변경됐으나 이 목록의 "안전 크리티컬 파일"에는 원래 포함되지 않았다 — 변경 내용은 §5.9에 상세
기술.)

운영 파일(md5, §7 근거):
```
a61104cf03499860ae89d4e194dc8c07  order_history.csv
09c77d24f6f392a49100d13d90d61aad  universe.csv
9054d0158cf10c47d0e01e8394daaeca  strategy_performance.csv
```

이 문서 작성 시점 `HEAD`: `3494fe3270322b6d649c2e2bb34ece33a5dcea8c`

## 13. 다음 단계

1. 사용자에게 이 문서 완성을 보고.
2. Codex 통합 재검증 요청(§11의 검증 중점 영역 전달).
3. 판정 결과에 따라:
   - `PASS`/`PASS_WITH_CONDITIONS`: `CODEX_REVIEW.md`에 기록, §10 잔여 위험에 대한 후속 조치
     여부를 사용자와 논의(코드 변경이 필요하면 새로운 별도 사이클).
   - `FAIL`: 지적된 CRITICAL/HIGH를 동일한 패턴으로 수정하고 재검증 요청.
4. 어떤 결과든 `approved`/`live_enabled`/`main`/`origin`/실거래 활성화는 사용자의 명시적 승인
   없이는 건드리지 않는다.
