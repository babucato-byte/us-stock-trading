# FINAL_VALIDATION_PACKAGE — Stage 3~10 + CODEX-023~030 (2026-07-26)

이 문서는 세 사이클의 최종 산출물이다: (1) 사용자의 "미국주식 초단타 자동매매 시스템 최종
자율개발 지시서"가 지정한 Stage 3~10 연속 구현, (2) 그 결과에 대한 Codex 1차 독립 검증(overall
verdict `FAIL`, CODEX-023~027)에 대한 통합 수정 사이클, (3) 그 수정에 대한 Codex 2차 통합
재검증(overall verdict `FAIL`, CODEX-024/026 PARTIALLY_RESOLVED + 신규 CODEX-028/029/030)에
대한 최종 재수정 사이클. 이 문서 자체는 3차 재검증 요청 전 최종 스냅샷이며, **실거래 승인이나
활성화를 의미하지 않는다.**

## 0. 최종 상태

```
상태: READY_FOR_FINAL_CODEX_REVALIDATION
```

- `approved: false`, `live_enabled: false` 유지([LIVE_APPROVAL_RECORD.md](../live_review/LIVE_APPROVAL_RECORD.md)).
- Live trading: **`DO_NOT_ENABLE`**.
- Limited live review: **`BLOCKED`**(이번 수정이 아직 Codex 재검증을 거치지 않았으므로).
- `main`/`origin`: 어느 것도 건드리지 않음(아래 §9).
- `READY_FOR_30K_KRW_LIMITED_LIVE_REVIEW`/`LIVE_READY`/`LIVE_APPROVED`/`PRODUCTION_READY` 등의
  표현은 이 문서를 포함해 어디에도 사용하지 않았다.

## 1. 검증 대상 커밋

브랜치: `orchestrator/20260725-013740-us-stock-trading`. 세 구간으로 나뉜다.

### 1a. Stage 3~10 (커밋 `415c129`~`e3b9e9f`, 21개 커밋)

Codex 1차 독립 검증 대상(overall verdict `FAIL`, CODEX-023~027 제기). 상세는 §1b/이전
`CODEX_REVIEW.md` 이력 참고.

### 1b. CODEX-023~027 통합 수정 사이클 (커밋 `530f888`~`e49753f`)

Codex 2차 검증 대상. CODEX-023/025/027은 이번(§1c) 재검증에서 `RESOLVED`로 재확인됐고,
CODEX-024/026은 `PARTIALLY_RESOLVED`로 남아 §1c에서 마저 해결됐다.

### 1c. Stage 3~10 최종 재수정 사이클 — CODEX-024/026/028/029/030 (신규, 이번 패키지의 실제 검증 대상)

| # | 커밋 | 내용 |
|---|---|---|
| 30 | `f04a123` | Inject deterministic clock into lifecycle checks (CODEX-030) |
| 31 | `aee663c` | Record Codex independent review: FAIL, CODEX-024/026 PARTIALLY_RESOLVED, CODEX-028/029 HIGH, CODEX-030 MEDIUM |
| 32 | `09b9237` | Make SQLite canonical for position/exit-intent state (CODEX-028/024) |
| 33 | `b78e444` | Enforce symbol-identity lock and close direct-broker bypass (CODEX-029/026) (최신, `HEAD`) |

`aee663c`는 Codex 자신의 통합 재검증 결과(`CODEX_REVIEW.md`)를 그대로 기록한 커밋이며, 이
저장소는 그 파일을 손으로 편집한 적이 없다. #30/#32/#33이 실제 코드 수정 커밋이다.

이 범위 이전(CODEX-001~022 원격 수정 사이클)은 이미 별도로 Codex 최종 독립 검증을 거쳐
`PASS_WITH_CONDITIONS`로 종결됨(`docs/autonomous/CODEX_REVIEW.md`의 해당 이력, 커밋 `d38cb95`).
이번 문서는 §1a+§1b+§1c 전체(38개 커밋)를 검증 대상으로 제출하되, 실질적으로 새로 검증이
필요한 것은 §1c(CODEX-024/026/028/029/030 수정)다 — §1a/§1b는 이미 두 차례 Codex의 눈을
거쳤고 그 결과가 바로 이번 수정의 근거이기 때문이다.

## 2. Stage/사이클별 변경 파일 및 테스트 결과

| 범위 | 변경 파일 수 | 신규 테스트 | 결과 |
|---|---|---|---|
| Stage 3 — 전략 인터페이스·Registry·VWAP 플러그인 | 12 files, +1445/-9 | `test_strategy_platform.py` 44건 | 통과 |
| Stage 4 — 포지션 생명주기 | 13 files, +1846/-29 | `test_position_states.py` 31 + `test_position_store.py` 15 + `test_position_lifecycle.py` 23 = 69건 | 통과 |
| Stage 5 — 거래 상태 저장소(SQLite) | 11 files, +835/-8 | `test_state_store.py` 20건 | 통과 |
| Stage 6 — 사용자/YouTube 전략 자료 구조화 | 17 files, +1395/-11 | `test_strategy_sources.py` 33건 | 통과 |
| Stage 7 — 전략 평가 엔진(백테스트/리플레이) | 10 files, +1223/-16 | `test_backtest_engine.py` 29건 | 통과 |
| Stage 8 — 전략 선택 엔진 | 8 files, +676/-16 | `test_strategy_selection.py` 27건 | 통과 |
| Stage 9 — 운영 관제(Dashboard/CLI) | 6 files, +551/-19 | `test_ops_dashboard.py` 16건 | 통과 |
| Stage 10 — 30,000원 제한 실거래 준비 | 7 files, +428/-15 | `test_live_readiness.py` 12건 | 통과 |
| CODEX-023~027(1차 수정 사이클, RESOLVED로 재확인) | 21 files | 103건 | 통과 |
| **CODEX-030**(Clock 주입) | 3 files(신규 `clock.py` 포함) | `test_clock.py` 23건 | 통과 |
| **CODEX-028/024**(SQLite canonical, exit intent 단일 트랜잭션) | 10 files | `test_position_store.py`(SQLite 이식+신규) + `test_exit_reconciliation.py` CODEX-028 전용 신규 | 통과 |
| **CODEX-029/026**(symbol 동일성, direct broker 우회 차단) | 8 files | `test_live_order_gateway.py` 신규 다수 + 경쟁 조건 재현 1건 | 통과 |
| **합계(§1c만)** | **약 21 files(중복 제외)** | **50건 신규**(직전 923 → 973) | **통과** |

## 3. 전체 테스트 결과

```
$ venv/bin/python -m pytest -q
973 passed, 0 failed, 2 warnings

$ venv/bin/pytest -q
973 passed, 0 failed, 2 warnings

$ (상위 디렉터리에서) python -m pytest us-stock-trading -q
973 passed, 0 failed, 2 warnings
```

- 이 문서 작성 직전 최종 실행 결과(2026-07-26), 세 가지 실행 형태 모두 동일. 실패 0건.
- Stage 3~10 착수 시점 베이스라인 613 passed → Stage 3~10 완료 시점 820 passed → CODEX-023~027
  수정 완료 시점 923 passed → 이번 CODEX-024/026/028/029/030 수정 완료 시점 973 passed.
- 두 warning은 기존 urllib3(LibreSSL) 경고와 `test_scanner.py`의 의도된 unknown-field 경고로,
  이번 범위와 무관한 기존 항목이다.
- 동시성 테스트(`test_concurrent_exit_attempts_submit_broker_sell_exactly_once` 등)는 20회 반복
  실행으로 안정성을 추가 확인했다(전체 회귀 중 드물게 발현하던 경쟁 조건 1건을 발견해 즉시
  수정 — §5.2/§10 참고).

## 4. 아키텍처 요약

```
사용자 차트/YouTube 자료 (strategy_sources/)
    │  구조화(source/assumption/unknown), 버전 관리, 유사도 분석 — 절대 ACTIVE 자동 승격 없음
    ▼
전략 플러그인 구현 (strategy/interface.py, strategy/plugins/)
    │  TradingStrategy ABC, StrategyRegistry(ACTIVE 최대 1개 구조적 강제)
    ▼
백테스트/리플레이 (backtest/)
    │  1분봉 룩어헤드 방지 리플레이, 비용 분리, 동일봉 충돌 보수적 처리, 세션 분리
    ▼
전략 선택 엔진 (strategy_selection/)
    │  설명가능한 규칙 기반 점수(비-LLM), SELECTED는 추천일 뿐 — registry 미참조
    ▼
포지션 생명주기 (positions/) ──── SQLite canonical (state_store/, positions/position_events)
    │  진입/체결 분리 확인/1R 50% 분할익절/2R·손절 전량청산/시간손절/EOD강제청산
    │  durable exit intent(exit_intents 테이블) + 포지션 상태가 동일 SQLite 트랜잭션에서 커밋
    │  POSITION_STORE.json은 커밋 후 재생성 가능한 best-effort projection일 뿐
    │  fail-closed store corruption 감지(→ Kill Switch MANUAL_REVIEW 자동 전환)
    │  Clock 주입(clock.py) — EOD/시간 판단이 실행 시각이 아닌 명시적 now/clock에 의존
    ▼
Live 진입 게이트 (live_readiness/order_gateway.py) ── side="buy" AND is_live_mode에만 적용
    │  allow-list/예산/FX rate/최대포지션/일일진입/손절위험/symbol 동일성 fail-closed 검증
    │  paper_strategy_order.submit_order() + AlpacaBroker.submit_order() 양쪽에 배선
    ▼
주문 실행 경계 (paper_strategy_order.py → broker/alpaca_client.py)
    │  RequestPurpose 게이트, kill switch 이중 검사 — CODEX-016~022 기검증, 이번 라운드 미변경
    ▼
운영 관제 (ops_dashboard/)
```

## 5. 각 구성요소 상세

### 5.1 전략 인터페이스 및 활성 전략
- `strategy/interface.py`의 `TradingStrategy` ABC, `strategy/status.py`의 9단계 상태,
  `strategy/registry.py`의 `StrategyRegistry`(ACTIVE 최대 1개 구조적 강제).
- 현재 활성 전략: `VWAP_MICRO_PULLBACK_MOMENTUM_V1` 단 하나. `PROJECT_CONSTITUTION.md`와 일치.

### 5.2 포지션 생명주기 (CODEX-023~030 전체로 대폭 강화됨)
- `positions/states.py`: 13개 생명주기 상태 + 6개 예외 상태, 명시적 `TRANSITIONS` 인접 테이블.
- **`positions/store.py`(CODEX-028로 전면 재작성)**: SQLite(`positions`/`position_events`
  테이블)가 유일한 canonical 저장소. `POSITION_STORE.json`은 SQLite 커밋 성공 후에만 쓰는
  best-effort projection(`positions.projection_status` 컬럼으로 성공/실패 기록,
  `store.regenerate_projection()`으로 언제든 재생성 가능) — JSON 손상은 더 이상 store 손상이
  아니다. `locked_position(conn=...)`이 `positions/lifecycle.py`의 exit-intent
  예약/재조정과 동일 SQLite 트랜잭션을 공유해, "SQLite intent 커밋 후 position 반영 유실"이라는
  CODEX-028의 재현과 CODEX-024의 "단일 트랜잭션 아님" 잔여 위험을 함께 닫았다.
  `load_all()`/`load_non_terminal()`은 SQLite 전체 손상 시 여전히 `PositionStoreCorruptedError`를
  발생(CODEX-025 의미론 유지, 대상만 SQLite로 이동). `check_store_health()` 진단 함수도 SQLite
  대상으로 재작성.
- `positions/order_status.py`(CODEX-023): broker 주문 상태를
  `NOT_FILLED`/`PARTIALLY_FILLED`/`FILLED`/`UNKNOWN`으로 분류 — accepted/new/pending_*는
  체결이 아님.
- `positions/fill_validation.py`(CODEX-027): 음수/NaN/상한초과/퇴행 fill을 mutation 전에 차단.
- `state_store/exit_intent_ledger.py`(CODEX-024, SQLite migration 2; CODEX-028로 `commit=False`
  옵션 추가): 청산 주문을 broker 호출 **전에** durable하게 예약하고, 그 예약이 이제 position의
  상태 전이와 **같은 SQLite 트랜잭션**으로 커밋된다. `positions/lifecycle.py::_execute_exit()`가
  3단계(예약+상태전환 → broker 호출 → 결과반영)로 재설계되어 timeout/크래시 후 재시도해도
  sell이 중복 제출되지 않음. `reconcile_pending_exit()`가 재시도/재시작의 공통 해소 경로.
- **부수 발견 및 수정(경쟁 조건)**: `_execute_exit()`의 lock 없는 `eil.get_active_intent()`
  읽기가, 실제 lock 획득 시점에는 포지션이 이미 CLOSED로 해소된 상태를 보지 못해
  `CLOSED -> EXIT_SUBMITTED`라는 불법 전이를 시도할 수 있었다(전체 회귀 중 1회 관측). lock
  아래에서 다시 읽은 실제 상태만 신뢰하도록 수정, 결정적 재현 테스트 추가.
- `positions/lifecycle.py::recover_on_restart()`: 손상 store 감지 시 Kill Switch를
  `MANUAL_REVIEW`로 자동 전환하고 `RestartRecoveryResult`(status/positions/reason)를 반환.
- `clock.py`(CODEX-030 신규): `Clock`/`ProductionClock`/`FrozenClock`. `check_and_manage()`/
  `check_invalidation()`이 timezone-aware `now`/`clock`을 명시적으로 받고, naive datetime은
  즉시 거부. 프로덕션 기본 동작은 이전과 동일(실제 시스템 시각) — 실제 결함은 테스트가 `now`를
  전달하지 않아 EOD 근처 실행 시각에 결과가 좌우된 것이었다.

### 5.3 SQLite 저장소 구조 (CODEX-028로 canonical 전환)
- `state_store/schema.py`: 기존 7개 테이블 + `exit_intents`(CODEX-024, migration 2) +
  `positions.projection_status`/`projection_updated_at` 컬럼(CODEX-028, migration 3).
- **`positions`/`position_events` 테이블이 이제 canonical**(§5.2). `orders`/`fills` 테이블은
  여전히 미사용 — 진입 주문 이력은 여전히 `order_history.csv`/`order_intent_ledger.csv`가
  담당한다. 이 범위 결정의 근거는 `DECISION_LOG.md` CODEX-024/026/028/029/030 섹션 결정 1 참고.
- `state_store/csv_import.py`/`export.py`: Stage 5와 동일, 미변경.

### 5.4 사용자/YouTube 전략 자료 구조
- Stage 6과 동일, 이번 사이클에서 미변경. 8개 카탈로그 소스(`docs/strategy/sources/*.json`).

### 5.5 전략 평가(백테스트) 및 선택 방식
- Stage 7/8과 동일, 이번 사이클에서 미변경.

### 5.6 Kill Switch
- `kill_switch.py`/`kill_switch_state.py` 자체는 이번 사이클에서도 변경하지 않음.
- `recover_on_restart()`의 손상 store 감지 시 `MANUAL_REVIEW` 자동 전환(CODEX-025)은 대상이
  SQLite로 바뀌었을 뿐 동작은 유지.
- 여전히 미구현: "첫 주문 오류 시 `ENTRY_DISABLED` 자동 배선"(Stage 10에서
  `NEEDS_USER_DECISION`으로 기록, 이번 사이클도 변경하지 않음).

### 5.7 운영 관제
- Stage 9와 동일한 기능. `test_ops_dashboard.py`가 `STATE_STORE_DB_FILE`을 격리하지 않아
  실제 저장소 루트 DB에 쓰던 격리 누락을 발견해 수정(§6/§10 참고) — 기능 자체는 미변경.

### 5.8 30,000원 제한 실거래 준비 (CODEX-026/029로 완성됨)
- `live_readiness/sizing.py`/`allowlist.py`: Stage 10과 동일, 미변경.
- `live_readiness/order_gateway.py`(CODEX-026, CODEX-029로 강화): `validate_and_size_live_entry
  (ctx, order_symbol)`가 allow-list/예산/FX rate/최대 동시 포지션/일일 진입 횟수/손절
  위험금액에 더해, **`ctx.symbol`과 실제 주문 symbol의 완전 일치(대소문자/공백 정규화 없음)**를
  검사한다(CODEX-029). `paper_strategy_order.submit_order()`에 배선된 것에 더해,
  **`broker/alpaca_client.py::AlpacaBroker.submit_order()` 자체에도 동일 게이트가 배선됨**
  (CODEX-026의 "direct broker 호출이 게이트를 우회한다"는 잔여 위험 해소). 두 경로 모두
  `side="buy" AND broker.config.is_live_mode`에만 적용 — Paper 거래·청산 주문에는 전혀
  적용되지 않는다(설계 근거는 `DECISION_LOG.md` 결정, 결정 3/4).
- 여전히 남은 범위: `AlpacaBroker`의 향후 신규 메서드가 이 게이트를 자동으로 상속받지 않음(§10
  참고).

## 6. 외부 API 호출 현황

Stage 3~10 및 CODEX-023~030 전체 구현·테스트 과정에서 실제 Alpaca API, 실제 Slack Webhook, 실제
Yahoo/기타 외부 데이터 API를 호출한 적이 **0회**다. 모든 테스트는 fake/sequenced broker, 실제
`AlpacaBroker` + 네트워크 호출 시 예외를 던지는 세션 더블(`_NetworkForbiddenSession`), 구성된
pandas DataFrame, tmp_path 격리 파일로만 동작한다. CODEX-026/029 게이트의 "차단된 주문은 세션
호출 0회"는 `paper_strategy_order.submit_order()` 경로와 `AlpacaBroker.submit_order()` direct
호출 경로 양쪽 모두 실제 `AlpacaBroker`를 사용한 통합 테스트(`tests/test_live_order_gateway.py`)
로 직접 증명했다.

## 7. 운영 파일 변경 현황

`order_history.csv`, `universe.csv`, `strategy_performance.csv`는 세 사이클 내내 **바이트
단위로 불변**(md5 해시 동일, §12 참고). `KILL_SWITCH_STATE.json` 등 런타임 상태 파일도 변경되지
않았다(모든 테스트가 `tmp_path`/env 변수 오버라이드로 격리됨). CODEX-028 사이클에서 SQLite가
canonical이 된 직후, `tests/test_position_store.py`/`tests/test_ops_dashboard.py`가
`POSITION_STORE_FILE`만 격리하고 `STATE_STORE_DB_FILE`은 격리하지 않아 실제 저장소 루트
`TRADING_STATE.db`에 테스트 포지션을 쓰고 있던 것을 즉시 발견해 격리를 추가하고, 생성된 stray
파일(gitignored, 커밋되지 않음)을 삭제했다. 이후 전체 회귀를 두 차례 반복 실행해 해당 파일이
생성되지 않음을 재확인했다.

## 8. main/origin 및 approved/live_enabled 현황

- `main`은 세 사이클 내내 전혀 이동하지 않았다.
- `origin`으로 push한 적 없음.
- `approved: false`, `live_enabled: false`는 변경하지 않았다.
- Kill Switch **해제**(`release`), Live API Key 입력, 실제 주문 실행 등 사용자 승인이 필요한 행위는
  수행하지 않았다. (참고: `recover_on_restart()`의 자동 `MANUAL_REVIEW` **활성화**는 해제가
  아니라 오히려 더 보수적인 방향으로의 자동 전환이며, 사용자가 사전에 명시한 금지 목록 — 실제
  주문/Live API Key 입력/`approved=true`/`live_enabled=true`/Kill Switch **해제**/main 병합/
  origin push/운영 배포/운영 데이터 삭제/기존 리스크 한도 완화 — 중 어디에도 해당하지 않는다.)

## 9. 남은 TBD_OPERATOR 항목

`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md`(항목 #3·#4는 CODEX-026/029로 "코드 미강제"에서
"코드는 이제 강제하나 실제 값이 미기입"으로 하향 조정됨) +
`docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md` §7: 실계좌, 실환율(FX rate provider 연동
자체가 미구현 — `live_readiness/order_gateway.py`는 호출자가 값을 주입하는 구조), Live API Key,
실 주문 금액 한도(`max_order_notional_krw`의 실제 KRW 값), 실 승인자, 배포 시각, 롤백 담당자,
실제 Alpaca 최소 주문 금액, 실제 파일럿 종목 allow-list 내용. 어느 항목도 추정하여 확정하지
않았다.

## 10. 알려진 위험 (Codex 재검증 시 특히 확인 필요)

1. **SQLite canonical 범위가 orders/fills까지 포함하지 않음**: `positions`/`position_events`/
   `exit_intents`만 canonical이고, 진입 주문 이력(`order_history.csv`,
   `order_intent_ledger.csv`)은 여전히 CSV 기반이다(`DECISION_LOG.md` 결정 1). CODEX-028의
   실제 재현·요구사항은 이 범위로 충분히 해소됐다고 판단했으나, 진입 주문 이력의 CSV/SQLite
   불일치는 별도의(아직 제기되지 않은) 위험으로 남아 있을 수 있다.
2. **`ENTRY_DISABLED` 자동 배선 미완료**: 첫 주문 오류 시 자동으로 진입을 차단하는 코드는 여전히
   없음(store corruption에 대해서만 `MANUAL_REVIEW` 자동 전환이 있음 — 일반 주문 오류는 대상
   아님). `NEEDS_USER_DECISION`.
3. **CODEX-026/029 게이트가 `AlpacaBroker`의 향후 신규 메서드를 자동으로 보호하지 않음**:
   `submit_order()`에만 배선되어 있다 — 동일 클래스에 새 주문 제출 경로가 추가되면 이 게이트를
   다시 배선해야 한다(구조적으로 강제되지 않음, `DECISION_LOG.md` 결정 4).
4. **비용/정책 ASSUMPTION 다수**: 백테스트 비용 가정, 선택 엔진 가중치, 사이징 최소 주문 금액,
   게이트의 `max_fx_rate_age_seconds` 등 기본값이 실측치가 아닌 문서화된 가정값이다.
5. **미검증 YouTube 전략 후보 4건**: Turtle/멀티 타임프레임 RSI/볼린저 눌림목/CCI·RSI·ADX는
   구현되지 않았고 어떤 주문 경로와도 연결되어 있지 않다.
6. **"마지막 성공 실행 시각"이 근사치**(`ops_dashboard/`): 전용 마커 파일이 없어 CSV mtime 대리.
7. **Phase 3(1분봉 실시간 수집/폴링 인프라) 미착수**: 실제 라이브 데이터 피드/브로커 연동은
   범위 밖이며, `live_readiness/order_gateway.py`의 FX rate도 실제 provider 연동이 없다.
8. **동시성 경쟁 조건 발견 이력**: 이번 사이클에서 `_execute_exit()`의 lock 없는 읽기로 인한
   드문(회귀 중 1회) 경쟁 조건을 발견·수정했다(§5.2). 유사한 "lock 밖에서 읽고 lock 안에서
   결정"하는 패턴이 코드베이스 다른 곳에 더 있는지는 이번 사이클 범위에서 전수 조사하지 않았다.

## 11. 검증 중점 영역 (Codex에게 요청)

1. CODEX-028의 SQLite-canonical 전환이 실제로 "SQLite 커밋 후 JSON 실패"/"DB 커밋 실패" 양쪽
   fault-injection에서 거래 상태를 정확히 보존하는지, `positions/lifecycle.py`의 exit-intent
   예약이 정말로 position 전이와 같은 트랜잭션으로 커밋되는지 재현 검증.
2. CODEX-029의 symbol 동일성 검사가 `paper_strategy_order.submit_order()` 경로와
   `AlpacaBroker.submit_order()` direct 호출 경로 양쪽에서 빠짐없이 강제되는지, 대소문자/공백
   변형이 실제로 차단되는지 재현 검증.
3. CODEX-030의 Clock 주입이 `check_and_manage()`/`check_invalidation()` 외에 다른 시간 의존
   로직(예: `market_hours.py`의 세션 게이트)에도 유사한 wall-clock 의존이 남아있지 않은지 확인.
4. §5.2의 경쟁 조건 수정이 실제로 동시성 안전한지(제출된 재현 테스트 외의 다른 타이밍으로도
   검증), §10의 8개 위험 항목 중 특히 §10.1(orders/fills 미포함 범위)과 §10.3(게이트의 구조적
   미강제 범위)이 실제로 안전에 영향을 주는지 판단.
5. 전체 테스트(973건)가 실제 네트워크/운영 파일 변경 없이 격리되어 있는지 임의 표본 재확인,
   특히 CODEX-028 관련 신규 테스트가 실제 저장소 루트 `TRADING_STATE.db`를 생성하지 않는지.
6. 이전 사이클에서 RESOLVED로 재확인된 CODEX-023/025/027이 이번 코드 변경(특히 CODEX-028의
   store.py 재작성)으로 인해 실질적으로 회귀하지 않았는지.

## 12. SHA-256 (주요 안전 크리티컬 파일, 세 사이클 내내 미변경 확인용)

```
eae4ee3ce511bd81ec6c5b0507531575e734a79275279d2417a2a399d497dd98  docs/autonomous/CODEX_REVIEW.md
27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe  docs/live_review/LIVE_APPROVAL_RECORD.md
043a30a5dc9751c062a36a82d4d75fdbb47903a040fb7b9ab86509f047843d84  risk_config.py
34411cf9ff530e850e8de5700a19c47aa71369528f6f541c8edd0e553b9df65e  broker/broker_config.py
408e94af606ce9045b46c0b3c8aeb07f4c9ee140a595f8bab5d198397700f389  kill_switch_state.py
d06ef475cc0fa721faedd986f1eaaab6b4ac0e0192ed4bedd3b0d4d009e6c991  order_intent_ledger.py
```

(`CODEX_REVIEW.md`의 SHA-256은 이전 패키지 대비 변경됨 — 이는 Codex 자신의 최신 통합 재검증
결과가 그 파일에 기록됐기 때문이며, 파일 손상이나 수동 편집이 아니다. 나머지 5개 안전 크리티컬
파일은 이전 패키지와 SHA-256이 완전히 동일 — 이번 사이클에서 전혀 건드리지 않았음을 재확인한다.
`broker/alpaca_client.py`는 이번 사이클에서 CODEX-026/029 게이트 배선을 위해 변경됐으나, 이
목록의 "안전 크리티컬 파일"에는 원래부터 포함되지 않았다 — 변경 내용은 §5.8/§11에 상세 기술.)

운영 파일(md5, §7 근거):
```
a61104cf03499860ae89d4e194dc8c07  order_history.csv
09c77d24f6f392a49100d13d90d61aad  universe.csv
9054d0158cf10c47d0e01e8394daaeca  strategy_performance.csv
```

이 문서 작성 시점 `HEAD`: `b78e444bd5ee942cbc5aebe93e1bd3b9a76fa655`

## 13. 다음 단계

1. 사용자에게 이 문서 완성을 보고.
2. Codex 통합 재검증 요청(§11의 검증 중점 영역 전달).
3. 판정 결과에 따라:
   - `PASS`/`PASS_WITH_CONDITIONS`: `CODEX_REVIEW.md`에 기록, §10 잔여 위험에 대한 후속 조치
     여부를 사용자와 논의(코드 변경이 필요하면 새로운 별도 사이클).
   - `FAIL`: 지적된 CRITICAL/HIGH를 동일한 패턴으로 수정하고 재검증 요청.
4. 어떤 결과든 `approved`/`live_enabled`/`main`/`origin`/실거래 활성화는 사용자의 명시적 승인
   없이는 건드리지 않는다.
