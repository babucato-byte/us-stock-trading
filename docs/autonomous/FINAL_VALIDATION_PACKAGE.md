# FINAL_VALIDATION_PACKAGE — Stage 3~10 + CODEX-023~033 (2026-07-26)

이 문서는 네 사이클의 최종 산출물이다: (1) 사용자의 "미국주식 초단타 자동매매 시스템 최종
자율개발 지시서"가 지정한 Stage 3~10 연속 구현, (2) 그 결과에 대한 Codex 1차 독립 검증(overall
verdict `FAIL`, CODEX-023~027)에 대한 통합 수정 사이클, (3) 그 수정에 대한 Codex 2차 통합
재검증(overall verdict `FAIL`, CODEX-024/026 PARTIALLY_RESOLVED + 신규 CODEX-028/029/030)에
대한 재수정 사이클, (4) 그 수정에 대한 Codex 3차 통합 재검증(overall verdict `FAIL`,
CODEX-024/026/028 PARTIALLY_RESOLVED + 신규 CODEX-031/032/033)에 대한 최종 통합 수정 사이클.
이 문서 자체는 4차 재검증 요청 전 최종 스냅샷이며, **실거래 승인이나 활성화를 의미하지 않는다.**

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

브랜치: `orchestrator/20260725-013740-us-stock-trading`. 네 구간으로 나뉜다.

### 1a. Stage 3~10 (커밋 `415c129`~`e3b9e9f`, 21개 커밋)

Codex 1차 독립 검증 대상(overall verdict `FAIL`, CODEX-023~027 제기).

### 1b. CODEX-023~027 통합 수정 사이클 (커밋 `530f888`~`e49753f`)

Codex 2차 검증 대상. CODEX-023/025/027은 §1c 재검증에서 `RESOLVED`로 재확인됐고, CODEX-024/026은
`PARTIALLY_RESOLVED`로 남았다.

### 1c. CODEX-024/026/028/029/030 재수정 사이클 (커밋 `f04a123`~`b78e444`)

Codex 3차 검증 대상. CODEX-029/030은 §1d 재검증에서 `RESOLVED`로 재확인됐고, CODEX-024/026/028은
`PARTIALLY_RESOLVED`로 남아 §1d에서 마저 해결됐다.

### 1d. Stage 3~10 최종 통합 수정 사이클 — CODEX-024/026/028/031/032/033 (신규, 이번 패키지의 실제 검증 대상)

| # | 커밋 | 내용 |
|---|---|---|
| 34 | `07548d1` | Record Codex independent review: FAIL, CODEX-024/026/028 PARTIALLY_RESOLVED, CODEX-031/032 HIGH, CODEX-033 MEDIUM |
| 35 | `55f3806` | Make rejected-exit intent-abort and position transition atomic (CODEX-032/024/028) |
| 36 | `8a3be50` | Enforce authoritative 30K budget/count/pending limits at the broker boundary (CODEX-031/026) |
| 37 | `9c43862` | Fix limited-live checklist's stale READY status (CODEX-033) (최신, `HEAD`) |

`07548d1`는 Codex 자신의 통합 재검증 결과(`CODEX_REVIEW.md`)를 그대로 기록한 커밋이며, 이
저장소는 그 파일을 손으로 편집한 적이 없다. #35/#36/#37이 실제 코드/문서 수정 커밋이다.

이 범위 이전(CODEX-001~022 원격 수정 사이클)은 이미 별도로 Codex 최종 독립 검증을 거쳐
`PASS_WITH_CONDITIONS`로 종결됨(`docs/autonomous/CODEX_REVIEW.md`의 해당 이력, 커밋 `d38cb95`).
이번 문서는 §1a+§1b+§1c+§1d 전체(42개 커밋)를 검증 대상으로 제출하되, 실질적으로 새로 검증이
필요한 것은 §1d(CODEX-024/026/028/031/032/033 수정)다 — §1a/§1b/§1c는 이미 세 차례 Codex의 눈을
거쳤고 그 결과가 바로 이번 수정의 근거이기 때문이다.

## 2. Stage/사이클별 변경 파일 및 테스트 결과

| 범위 | 신규 테스트 | 결과 |
|---|---|---|
| Stage 3~10(21개 커밋) | 251건 | 통과 |
| CODEX-023~027(1차 수정 사이클, RESOLVED로 재확인) | 103건 | 통과 |
| CODEX-024/026/028/029/030(2차 재수정 사이클, 029/030 RESOLVED로 재확인) | 50건 | 통과 |
| **CODEX-032/024/028**(rejected exit 원자성) | `test_exit_reconciliation.py` 4건 | 통과 |
| **CODEX-031/026**(authoritative 30K/count/pending) | `live_readiness/entry_reservation_ledger.py` 신설 + `test_live_order_gateway.py` 전면 재작성(약 20건) | 통과 |
| **CODEX-033**(governance 문서 정합성) | 코드 변경 없음(문서만) | 통과 |
| **합계(§1d만)** | **13건 신규**(직전 973 → 986) | **통과** |

## 3. 전체 테스트 결과

```
$ venv/bin/python -m pytest -q
986 passed, 0 failed, 2 warnings

$ venv/bin/pytest -q
986 passed, 0 failed, 2 warnings

$ (상위 디렉터리에서) python -m pytest us-stock-trading -q
986 passed, 0 failed, 2 warnings

$ (상위 디렉터리에서) pytest us-stock-trading -q
986 passed, 0 failed, 2 warnings
```

- 이 문서 작성 직전 최종 실행 결과(2026-07-26), 네 가지 실행 형태 모두 동일. 실패 0건. 전체
  회귀를 두 차례 반복 실행해 안정성을 재확인했다.
- Stage 3~10 착수 시점 베이스라인 613 passed → Stage 3~10 완료 시점 820 passed → CODEX-023~027
  수정 완료 시점 923 passed → CODEX-024/026/028/029/030 수정 완료 시점 973 passed → 이번
  CODEX-024/026/028/031/032/033 수정 완료 시점 986 passed.
- 두 warning은 기존 urllib3(LibreSSL) 경고와 `test_scanner.py`의 의도된 unknown-field 경고로,
  이번 범위와 무관한 기존 항목이다.

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
    │  durable exit intent(exit_intents) — 예약·확정·rejection abort 전부 position과
    │  동일 SQLite 트랜잭션에서 커밋(CODEX-024/028/032)
    │  POSITION_STORE.json은 커밋 후 재생성 가능한 best-effort projection일 뿐
    │  fail-closed store corruption 감지(→ Kill Switch MANUAL_REVIEW 자동 전환)
    │  Clock 주입(clock.py) — EOD/시간 판단이 실행 시각이 아닌 명시적 now/clock에 의존
    ▼
Live 진입 게이트 (live_readiness/) ── side="buy" AND is_live_mode에만 적용
    │  allow-list/FX rate/symbol 동일성 fail-closed 검증(order_gateway.py)
    │  30,000원/일일진입/동시포지션은 caller 입력이 아닌 durable ledger에서 authoritative 산출
    │  (entry_reservation_ledger.py, SQLite) — 신뢰 가능한 코드 상수와 caller 값을 min()으로 교차
    │  paper_strategy_order.submit_order() + AlpacaBroker.submit_order() 양쪽에 배선,
    │  AlpacaBroker 인스턴스에는 이중 예약 방지를 위해 단일 지점만 예약
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
- 현재 활성 전략: `VWAP_MICRO_PULLBACK_MOMENTUM_V1` 단 하나.

### 5.2 포지션 생명주기 (CODEX-023~033 전체로 대폭 강화됨)
- `positions/states.py`: 13개 생명주기 상태 + 6개 예외 상태.
- `positions/store.py`(CODEX-028): SQLite(`positions`/`position_events`)가 유일한 canonical
  저장소, `POSITION_STORE.json`은 best-effort projection.
- `positions/order_status.py`(CODEX-023), `positions/fill_validation.py`(CODEX-027): accepted≠
  filled 분리, fill 수량/가격 검증 — 이번 사이클 회귀 재확인, 변경 없음.
- `state_store/exit_intent_ledger.py`(CODEX-024): durable exit intent 예약. **이번 사이클
  (CODEX-032)**: broker rejection 시 `mark_aborted()`가 더 이상 독립 커밋되지 않고
  `store.locked_position(conn=conn)`의 position `MANUAL_REVIEW` 전이와 **같은 SQLite 트랜잭션**
  (commit=False)으로 묶였다 — 이전에는 두 번째(position) write 실패 시 intent만 terminal
  ABORTED로 남고 position이 영구히 EXIT_SUBMITTED에 갇히는 실제 재현 가능한 결함이 있었다.
- `positions/lifecycle.py::recover_on_restart()`: 손상 store 감지 시 Kill Switch를
  `MANUAL_REVIEW`로 자동 전환.
- `clock.py`(CODEX-030): `Clock`/`ProductionClock`/`FrozenClock` — 이번 사이클 회귀 재확인,
  변경 없음.

### 5.3 SQLite 저장소 구조
- `state_store/schema.py`: 기존 7개 테이블 + `exit_intents`(migration 2) +
  `positions.projection_status`/`projection_updated_at`(migration 3) + **신규
  `live_entry_reservations`(migration 4, CODEX-031)**.
- `orders`/`fills` 테이블은 여전히 미사용 — 진입 주문 이력은 여전히 CSV 기반(`DECISION_LOG.md`
  결정 1, 이번 사이클에서도 유지).

### 5.4~5.5 사용자/YouTube 전략 자료, 전략 평가/선택
- Stage 6/7/8과 동일, 이번 사이클에서 미변경.

### 5.6 Kill Switch
- 이번 사이클에서 변경 없음.

### 5.7 운영 관제
- Stage 9와 동일, 이번 사이클에서 미변경.

### 5.8 30,000원 제한 실거래 준비 (CODEX-031로 authoritative화됨)
- `live_readiness/sizing.py`/`allowlist.py`: Stage 10과 동일, 미변경.
- **신규 `live_readiness/entry_reservation_ledger.py`(CODEX-031)**: 모든 live 진입 시도가
  broker 호출 전 SQLite에 durable하게 예산을 예약한다(`live_entry_reservations` 테이블). 30,000원
  총 예산은 파일럿 전체에 걸친 누적(lifetime) 배분으로 취급되어 포지션 종료로 반환되지 않는 반면,
  동시 포지션 수는 예약이 연결된 position이 canonical SQLite에서 terminal 상태가 되면 자동으로
  카운트에서 제외된다(서로 다른 시간 범위, `DECISION_LOG.md` 결정 3).
- `live_readiness/order_gateway.py`가 이제 `LiveEntryContext`의 `max_order_notional_krw`/
  `max_daily_loss_krw`/`max_position_count`/`current_open_position_count`/`max_daily_entries`/
  `today_entry_count`를 신뢰하지 않는다 — 신뢰 가능한 코드 상수(`PILOT_TOTAL_BUDGET_KRW=30_000`,
  `MAX_CONCURRENT_LIVE_POSITIONS=1`, `MAX_DAILY_LIVE_ENTRIES=2`)와 ledger에서 산출한 실제
  사용량을 근거로 판단하고, caller 값은 `min()`으로 교차해 상한을 완화할 수 없게 했다.
  `validate_and_size_live_entry()`는 이제 `LiveEntryApproval(quantity, reservation_id)`을
  반환하며, 스냅샷 읽기~예약 전체가 전용 파일 락으로 원자화되어 동시 진입 두 건이 합계 한도를
  넘지 못한다.
- `paper_strategy_order.submit_order()`/`AlpacaBroker.submit_order()` 둘 다 broker 응답에 따라
  예약을 commit(성공)/release(실패·거부·dry-run·예외)한다. `broker`가 실제 `AlpacaBroker`
  인스턴스면 wrapper가 자신의 게이트 사본을 건너뛰어 이중 예약을 방지한다(`DECISION_LOG.md`
  결정 4).

## 6. 외부 API 호출 현황

네 사이클 전체 구현·테스트 과정에서 실제 Alpaca API, 실제 Slack Webhook, 실제 Yahoo/기타 외부
데이터 API를 호출한 적이 **0회**다. 모든 테스트는 fake/sequenced broker, 실제 `AlpacaBroker` +
네트워크 호출 시 예외를 던지는 세션 더블(`_NetworkForbiddenSession`), tmp_path 격리 파일로만
동작한다.

## 7. 운영 파일 변경 현황

`order_history.csv`, `universe.csv`, `strategy_performance.csv`는 네 사이클 내내 **바이트 단위로
불변**(md5 해시 동일, §11 참고). 이번 사이클에서 `tests/test_broker_safety.py`/
`tests/test_paper_order_execution.py`가 `LiveEntryContext`를 사용하면서도 `STATE_STORE_DB_FILE`을
격리하지 않아, CODEX-031의 authoritative 게이트가 SQLite에 실제로 쓰기 시작하자 실제 저장소 루트
`TRADING_STATE.db`에 기록하던 것을 발견해 즉시 격리를 추가했다(생성된 stray 파일은 gitignored,
커밋되지 않음, 즉시 삭제). 이후 전체 회귀를 두 차례 반복 실행해 해당 파일 및
`LIVE_ENTRY_RESERVATION.lock`이 생성되지 않음을 재확인했다.

## 8. main/origin 및 approved/live_enabled 현황

- `main`은 네 사이클 내내 전혀 이동하지 않았다.
- `origin`으로 push한 적 없음.
- `approved: false`, `live_enabled: false`는 변경하지 않았다.
- Kill Switch 해제, Live API Key 입력, 실제 주문 실행, 테스트 삭제/완화 등 금지된 행위는
  수행하지 않았다.

## 9. 남은 TBD_OPERATOR 항목

`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md` + `docs/live_review/LIMITED_LIVE_30K_KRW_
PLAYBOOK.md` §7: 실계좌, 실환율(FX rate provider 연동 자체가 미구현), Live API Key, 실 주문 금액
한도의 실제 KRW 값(코드는 이제 강제하지만 값 자체는 미기입), 실 승인자, 배포 시각, 롤백 담당자,
실제 Alpaca 최소 주문 금액, 실제 파일럿 종목 allow-list 내용. 어느 항목도 추정하여 확정하지
않았다.

## 10. 알려진 위험 (Codex 재검증 시 특히 확인 필요)

1. **SQLite canonical 범위가 orders/fills까지 포함하지 않음**: 진입 주문 이력은 여전히 CSV
   기반(`DECISION_LOG.md` 결정 1).
2. **`ENTRY_DISABLED` 자동 배선 미완료**: `NEEDS_USER_DECISION`으로 유지.
3. **CODEX-026/029 게이트가 `AlpacaBroker`의 향후 신규 메서드를 자동으로 보호하지 않음**:
   `submit_order()`에만 배선(`DECISION_LOG.md` 결정 4).
4. **entry 경로의 crash-safe reconciliation 미구현(신규, CODEX-031 사이클)**: broker 호출이
   실제로는 성공했지만 로컬에서 예외가 발생해 `entry_reservation_ledger`의 예약이 release되는
   경쟁 상황은 예산을 실제보다 적게 집계할 수 있다. Phase 1B의 기존 "다중 파일 트랜잭션 부재"
   잔여 위험과 동일한 성격이며, 이번 사이클 범위에서 명시적으로 남겨둔 것이다
   (`DECISION_LOG.md` 결정 5).
5. **비용/정책 ASSUMPTION 다수**: 백테스트 비용 가정, 선택 엔진 가중치, 사이징 최소 주문 금액 등.
6. **미검증 YouTube 전략 후보 4건**: 어떤 주문 경로와도 연결되어 있지 않다.
7. **"마지막 성공 실행 시각"이 근사치**(`ops_dashboard/`).
8. **Phase 3(1분봉 실시간 수집/폴링 인프라) 미착수**.
9. **동시성 경쟁 조건 발견 이력**: 이전 사이클(CODEX-029/030)에서 `_execute_exit()`의 lock 없는
   읽기로 인한 경쟁 조건 1건을 발견·수정했고, 유사한 패턴이 코드베이스 다른 곳에 더 있는지는
   아직 전수 조사하지 않았다.

## 11. 검증 중점 영역 (Codex에게 요청)

1. CODEX-032의 rejection 처리 원자성이 실제로 fault-injection(intent write 실패/position write
   실패 양쪽)에서 두 상태를 함께 롤백하는지, 롤백 후 재시도가 안전하게 재조정되는지 재현 검증.
2. CODEX-031의 authoritative 예산/카운트 산출이 caller가 제공하는 어떤 값으로도 30,000원/
   `MAX_CONCURRENT_LIVE_POSITIONS`/`MAX_DAILY_LIVE_ENTRIES`를 넘길 수 없는지, 동시 진입 두 건이
   합계 한도를 넘지 못하는지(스레드 테스트 포함) 재현 검증.
3. §10.4(entry 경로 crash-safe reconciliation 미구현)이 실제 위험도를 어떻게 평가하는지 — 이번
   사이클에서 의도적으로 범위 밖으로 남긴 결정이 타당한지 판단 요청.
4. CODEX-033의 문서 정정이 실제로 `LIMITED_LIVE_REVIEW_CHECKLIST.md`와 `FINAL_VALIDATION_PACKAGE.md`
   /`CURRENT_STATUS.md` 사이의 모순을 완전히 해소했는지 확인.
5. 전체 테스트(986건)가 실제 네트워크/운영 파일 변경 없이 격리되어 있는지 임의 표본 재확인,
   특히 CODEX-031 관련 신규 테스트가 실제 저장소 루트 `TRADING_STATE.db`/
   `LIVE_ENTRY_RESERVATION.lock`을 생성하지 않는지.
6. 이전 사이클에서 RESOLVED로 재확인된 CODEX-023/025/027/029/030이 이번 코드 변경으로 인해
   실질적으로 회귀하지 않았는지.

## 12. SHA-256 (주요 안전 크리티컬 파일, 네 사이클 내내 미변경 확인용)

```
1d6109a92b874acce83dacf44162ca7151e63d57580a2fa0bae6d5174e0c0737  docs/autonomous/CODEX_REVIEW.md
27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe  docs/live_review/LIVE_APPROVAL_RECORD.md
043a30a5dc9751c062a36a82d4d75fdbb47903a040fb7b9ab86509f047843d84  risk_config.py
34411cf9ff530e850e8de5700a19c47aa71369528f6f541c8edd0e553b9df65e  broker/broker_config.py
408e94af606ce9045b46c0b3c8aeb07f4c9ee140a595f8bab5d198397700f389  kill_switch_state.py
d06ef475cc0fa721faedd986f1eaaab6b4ac0e0192ed4bedd3b0d4d009e6c991  order_intent_ledger.py
```

(`CODEX_REVIEW.md`의 SHA-256은 이전 패키지 대비 변경됨 — Codex 자신의 최신 통합 재검증 결과가
그 파일에 기록됐기 때문이며, 파일 손상이나 수동 편집이 아니다. 나머지 5개 안전 크리티컬 파일은
이전 패키지와 SHA-256이 완전히 동일 — 네 사이클 내내 전혀 건드리지 않았음을 재확인한다.
`positions/lifecycle.py`, `broker/alpaca_client.py`, `live_readiness/order_gateway.py`는
이번 사이클에서 변경됐으나 이 목록의 "안전 크리티컬 파일"에는 원래부터 포함되지 않았다 — 변경
내용은 §5.2/§5.8에 상세 기술.)

운영 파일(md5, §7 근거):
```
a61104cf03499860ae89d4e194dc8c07  order_history.csv
09c77d24f6f392a49100d13d90d61aad  universe.csv
9054d0158cf10c47d0e01e8394daaeca  strategy_performance.csv
```

이 문서 작성 시점 `HEAD`: `9c43862063ff24477461b7a4648226d85109e4d5`

## 13. 다음 단계

1. 사용자에게 이 문서 완성을 보고.
2. Codex 통합 재검증 요청(§11의 검증 중점 영역 전달).
3. 판정 결과에 따라:
   - `PASS`/`PASS_WITH_CONDITIONS`: `CODEX_REVIEW.md`에 기록, §10 잔여 위험에 대한 후속 조치
     여부를 사용자와 논의(코드 변경이 필요하면 새로운 별도 사이클).
   - `FAIL`: 지적된 CRITICAL/HIGH를 동일한 패턴으로 수정하고 재검증 요청.
4. 어떤 결과든 `approved`/`live_enabled`/`main`/`origin`/실거래 활성화는 사용자의 명시적 승인
   없이는 건드리지 않는다.
