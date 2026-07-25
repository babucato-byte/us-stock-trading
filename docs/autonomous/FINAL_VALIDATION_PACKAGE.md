# FINAL_VALIDATION_PACKAGE — Stage 3~10 + CODEX-023~027 (2026-07-26)

이 문서는 두 사이클의 최종 산출물이다: (1) 사용자의 "미국주식 초단타 자동매매 시스템 최종
자율개발 지시서"가 지정한 Stage 3~10 연속 구현, (2) 그 결과에 대한 Codex 독립 검증(overall
verdict `FAIL`, CODEX-023~027)에 대한 통합 수정 사이클. 이 문서 자체는 재검증 요청 전 최종
스냅샷이며, **실거래 승인이나 활성화를 의미하지 않는다.**

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

브랜치: `orchestrator/20260725-013740-us-stock-trading`. 두 구간으로 나뉜다.

### 1a. Stage 3~10 (이전 패키지, 커밋 `530f888`/`64a5551`에서 이미 검증 대상으로 제출됨)

`415c129`(Stage 3 첫 커밋)부터 `e3b9e9f`(Stage 10 문서 갱신, 21개 커밋). 이 구간에 대한 Codex의
1차 독립 검증 결과는 §1b 참고(overall verdict `FAIL`, CODEX-023~027 제기).

### 1b. CODEX-023~027 통합 수정 사이클 (신규, 이번 패키지의 실제 검증 대상)

| # | 커밋 | 내용 |
|---|---|---|
| 22 | `530f888` | Add FINAL_VALIDATION_PACKAGE.md for Stage 3-10, ready for Codex validation |
| 23 | `64a5551` | Mark Stage 3-10 workflow complete, ready for Codex validation |
| 24 | `f2afb4e` | Record Codex independent review: FAIL, CODEX-023~026 HIGH, CODEX-027 MEDIUM |
| 25 | `0f60ec9` | Validate fill quantities and prices (CODEX-027) |
| 26 | `c5c56c4` | Fail closed on corrupted position store (CODEX-025) |
| 27 | `ee6dae2` | Separate order acceptance from fills and add durable exit intents (CODEX-023/024) |
| 28 | `f482e90` | Enforce 30000 KRW budget and allow-list at the order boundary (CODEX-026) |
| 29 | `4de0714` | Update governance docs for CODEX-023~027 remediation cycle (최신, `HEAD`) |

`f2afb4e`는 Codex 자신의 독립 검증 결과(`CODEX_REVIEW.md`)를 그대로 기록한 커밋이며, 이 저장소는
그 파일을 손으로 편집한 적이 없다. #25~28이 실제 코드 수정 커밋이다.

이 범위 이전(CODEX-001~022 원격 수정 사이클)은 이미 별도로 Codex 최종 독립 검증을 거쳐
`PASS_WITH_CONDITIONS`로 종결됨(`docs/autonomous/CODEX_REVIEW.md`의 해당 이력, 커밋 `d38cb95`).
이번 문서는 §1a+§1b 전체(29개 커밋)를 검증 대상으로 제출하되, 실질적으로 새로 검증이 필요한 것은
§1b(CODEX-023~027 수정)이다 — §1a는 이미 한 차례 Codex의 눈을 거쳤고 그 결과가 바로 이번 수정의
근거이기 때문이다.

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
| **CODEX-027**(fill 검증) | 4 files | `test_fill_validation.py` 18 + `test_position_lifecycle.py` +6 = 24건 | 통과 |
| **CODEX-025**(fail-closed 복구) | 4 files | `test_position_store.py` +14 + `test_position_lifecycle.py` +4 = 18건 | 통과 |
| **CODEX-023/024**(accepted≠filled, durable exit intent) | 10 files | `test_exit_reconciliation.py` 20 + `test_exit_intent_ledger.py` 13 + `test_state_store.py` +3 = 36건 | 통과 |
| **CODEX-026**(30K 예산/allow-list 배선) | 3 files | `test_live_order_gateway.py` 25건 | 통과 |
| **합계** | **105 files(중복 제외)** | **354건 신규**(Stage 3~10 251건 + CODEX-023~027 103건) | **통과** |

## 3. 전체 테스트 결과

```
$ venv/bin/python -m pytest -q
923 passed, 0 failed, 2 warnings
```

- 이 문서 작성 직전 최종 1회 실행 결과(2026-07-26). 실패 0건.
- Stage 3~10 착수 시점 베이스라인 613 passed → Stage 3~10 완료 시점 820 passed → CODEX-023~027
  수정 완료 시점 923 passed.
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
포지션 생명주기 (positions/) ──── 거래 상태 저장소 (state_store/, SQLite)
    │  진입/체결 분리 확인/1R 50% 분할익절/2R·손절 전량청산/시간손절/EOD강제청산
    │  durable exit intent(exit_intents 테이블, broker 호출 전 원자적 예약) + 재시작 복구
    │  fail-closed store corruption 감지(→ Kill Switch MANUAL_REVIEW 자동 전환)
    ▼
Live 진입 게이트 (live_readiness/order_gateway.py) ── side="buy" AND is_live_mode에만 적용
    │  allow-list/예산/FX rate/최대포지션/일일진입/손절위험 fail-closed 검증
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

### 5.2 포지션 생명주기 (CODEX-023/024/025/027로 대폭 강화됨)
- `positions/states.py`: 13개 생명주기 상태 + 6개 예외 상태, 명시적 `TRANSITIONS` 인접 테이블.
- `positions/store.py`: 원자적 JSON 저장소. **CODEX-025**: `load_all()`/`load_non_terminal()`이
  전체 파일 손상 시 `PositionStoreCorruptedError`를 발생(빈 dict 반환 대신) — "손상됨"과
  "포지션 0개"가 구조적으로 구분됨. `check_store_health()` 진단 함수 신설.
- `positions/order_status.py`(신규, CODEX-023): broker 주문 상태를
  `NOT_FILLED`/`PARTIALLY_FILLED`/`FILLED`/`UNKNOWN`으로 분류 — accepted/new/pending_*는
  체결이 아님.
- `positions/fill_validation.py`(신규, CODEX-027): 음수/NaN/상한초과/퇴행 fill을 mutation 전에
  차단.
- `state_store/exit_intent_ledger.py`(신규, CODEX-024, SQLite migration 2): 청산 주문을 broker
  호출 **전에** durable하게 예약. `positions/lifecycle.py::_execute_exit()`가 3단계(예약+상태전환
  → broker 호출 → 결과반영)로 재설계되어 timeout/크래시 후 재시도해도 sell이 중복 제출되지 않음.
  `reconcile_pending_exit()`가 재시도/재시작의 공통 해소 경로 — 절대 재주문하지 않음.
- `positions/lifecycle.py::recover_on_restart()`: 손상 store 감지 시 Kill Switch를
  `MANUAL_REVIEW`로 자동 전환하고 `RestartRecoveryResult`(status/positions/reason)를 반환.
  pending exit intent가 있는 포지션은 `reconcile_pending_exit()` 경로로 재조정.

### 5.3 SQLite 저장소 구조 (병행 인프라, CODEX-024로 exit intent 추가)
- `state_store/schema.py`: 기존 7개 테이블 + **신규 `exit_intents`**(CODEX-024, migration 2).
- **실제 포지션 저장 경로는 여전히 JSON**(`positions/store.py`) — exit intent만 SQLite로 분리한
  설계 근거는 `DECISION_LOG.md` CODEX-023~027 섹션 결정 1 참고.
- `state_store/csv_import.py`/`export.py`: Stage 5와 동일, 미변경.

### 5.4 사용자/YouTube 전략 자료 구조
- Stage 6과 동일, 이번 사이클에서 미변경. 8개 카탈로그 소스(`docs/strategy/sources/*.json`).

### 5.5 전략 평가(백테스트) 및 선택 방식
- Stage 7/8과 동일, 이번 사이클에서 미변경.

### 5.6 Kill Switch
- `kill_switch.py`/`kill_switch_state.py` 자체는 이번 사이클에서도 변경하지 않음.
- **신규(CODEX-025)**: `recover_on_restart()`가 손상된 store 감지 시 `kill_switch_state.
  activate(MANUAL_REVIEW, ...)`를 **자동 호출**한다 — 이전에는 존재하지 않던 자동 에스컬레이션.
- 여전히 미구현: "첫 주문 오류 시 `ENTRY_DISABLED` 자동 배선"(Stage 10에서
  `NEEDS_USER_DECISION`으로 기록, 이번 사이클도 변경하지 않음 — 위 자동 에스컬레이션은 이것과
  별개의, 더 좁은 범위(store corruption 전용)다).

### 5.7 운영 관제
- Stage 9와 동일, 이번 사이클에서 미변경.

### 5.8 30,000원 제한 실거래 준비 (CODEX-026으로 실배선됨)
- `live_readiness/sizing.py`/`allowlist.py`: Stage 10과 동일, 미변경.
- **신규 `live_readiness/order_gateway.py`**(CODEX-026): `validate_and_size_live_entry()`가
  allow-list/예산/FX rate/최대 동시 포지션/일일 진입 횟수/손절 위험금액을 전부 fail-closed
  검증하고, **`paper_strategy_order.submit_order()`의 `side="buy" AND broker.config.
  is_live_mode` 경로에 실제로 배선됨**(Stage 10 종료 시점의 "문서·계산 모듈만 존재, 미배선"
  상태에서 진전). Paper 거래·청산 주문에는 전혀 적용되지 않음(설계 근거는
  `DECISION_LOG.md` 결정 3).
- 여전히 미해결: `paper_strategy_order.submit_order()`를 우회하는 direct broker 호출은 이 게이트의
  보호를 받지 못함(§10 참고).

## 6. 외부 API 호출 현황

Stage 3~10 및 CODEX-023~027 전체 구현·테스트 과정에서 실제 Alpaca API, 실제 Slack Webhook, 실제
Yahoo/기타 외부 데이터 API를 호출한 적이 **0회**다. 모든 테스트는 fake/sequenced broker, 실제
`AlpacaBroker` + 네트워크 호출 시 예외를 던지는 세션 더블, 구성된 pandas DataFrame, tmp_path 격리
파일로만 동작한다. CODEX-026 게이트의 "차단된 주문은 세션 호출 0회"는 실제 `AlpacaBroker`를 사용한
통합 테스트(`tests/test_live_order_gateway.py`)로 직접 증명했다.

## 7. 운영 파일 변경 현황

`order_history.csv`, `universe.csv`, `strategy_performance.csv`는 이번 두 사이클 내내 **바이트
단위로 불변**(md5 해시 동일, §12 참고). `KILL_SWITCH_STATE.json` 등 런타임 상태 파일도 변경되지
않았다(모든 테스트가 `tmp_path`/env 변수 오버라이드로 격리됨). CODEX-023/024 수정 과정에서
실제 저장소 루트 `TRADING_STATE.db`가 격리되지 않은 테스트로 인해 생성되는 버그를 발견해 즉시
수정했고(`tests/test_position_lifecycle.py`에 `STATE_STORE_DB_FILE` 격리 추가), 이후 전체 회귀에서
해당 파일이 생성되지 않음을 전용 테스트로 재확인했다.

## 8. main/origin 및 approved/live_enabled 현황

- `main`은 두 사이클 내내 전혀 이동하지 않았다.
- `origin`으로 push한 적 없음.
- `approved: false`, `live_enabled: false`는 변경하지 않았다.
- Kill Switch **해제**(`release`), Live API Key 입력, 실제 주문 실행 등 사용자 승인이 필요한 행위는
  수행하지 않았다. (참고: `recover_on_restart()`의 자동 `MANUAL_REVIEW` **활성화**는 해제가
  아니라 오히려 더 보수적인 방향으로의 자동 전환이며, 사용자가 사전에 명시한 금지 목록 — 실제
  주문/Live API Key 입력/`approved=true`/`live_enabled=true`/Kill Switch **해제**/main 병합/
  origin push/운영 배포/운영 데이터 삭제/기존 리스크 한도 완화 — 중 어디에도 해당하지 않는다.)

## 9. 남은 TBD_OPERATOR 항목

`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md`(2026-07-26 갱신: 항목 #3·#4는 "코드 미강제"에서
"코드는 이제 강제하나 실제 값이 미기입"으로 하향 조정됨) +
`docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md` §7: 실계좌, 실환율(FX rate provider 연동
자체가 미구현 — `live_readiness/order_gateway.py`는 호출자가 값을 주입하는 구조), Live API Key,
실 주문 금액 한도(`max_order_notional_krw`의 실제 KRW 값), 실 승인자, 배포 시각, 롤백 담당자,
실제 Alpaca 최소 주문 금액, 실제 파일럿 종목 allow-list 내용. 어느 항목도 추정하여 확정하지
않았다.

## 10. 알려진 위험 (Codex 재검증 시 특히 확인 필요)

1. **SQLite 병행 인프라 미배선**(포지션 저장 자체): 실제 포지션 저장은 여전히 JSON —
   exit intent만 SQLite로 분리했을 뿐, 두 저장소 간 진짜 단일 트랜잭션은 없음(`DECISION_LOG.md`
   결정 1에 설계 근거 기록). 다중 파일 트랜잭션 부재라는 기존 잔여 위험(Phase 1B/Phase 5)의
   축소된 형태로 남아 있음.
2. **`ENTRY_DISABLED` 자동 배선 미완료**: 첫 주문 오류 시 자동으로 진입을 차단하는 코드는 여전히
   없음(store corruption에 대해서만 CODEX-025로 `MANUAL_REVIEW` 자동 전환이 신규 추가됨 — 일반
   주문 오류는 대상 아님). `NEEDS_USER_DECISION`.
3. **CODEX-026 게이트가 direct broker 호출을 막지 못함**: `paper_strategy_order.submit_order()`를
   우회해 `broker.submit_order()`를 직접 호출하는 경로는 allow-list/예산 검증을 받지 않는다.
   현재 이 저장소 내 어떤 진입 경로도 그렇게 하지 않음을 코드 검색으로 확인했으나, 향후 신규
   코드가 이 경로를 우회하지 않도록 유지 관리가 필요하다(`DECISION_LOG.md` 결정 4).
4. **비용/정책 ASSUMPTION 다수**: 백테스트 비용 가정, 선택 엔진 가중치, 사이징 최소 주문 금액,
   CODEX-026 게이트의 `max_fx_rate_age_seconds` 등 기본값이 실측치가 아닌 문서화된 가정값이다.
5. **미검증 YouTube 전략 후보 4건**: Turtle/멀티 타임프레임 RSI/볼린저 눌림목/CCI·RSI·ADX는
   구현되지 않았고 어떤 주문 경로와도 연결되어 있지 않다.
6. **"마지막 성공 실행 시각"이 근사치**(`ops_dashboard/`): 전용 마커 파일이 없어 CSV mtime 대리.
7. **Phase 3(1분봉 실시간 수집/폴링 인프라) 미착수**: 실제 라이브 데이터 피드/브로커 연동은
   범위 밖이며, `live_readiness/order_gateway.py`의 FX rate도 실제 provider 연동이 없다.

## 11. 검증 중점 영역 (Codex에게 요청)

1. CODEX-023/024 재작성이 실제로 accepted-but-unfilled 청산을 CLOSED로 오판하지 않는지, timeout
   후 재시도가 실제로 broker sell을 중복 제출하지 않는지 재현 검증.
2. CODEX-025의 `PositionStoreCorruptedError`가 `positions/lifecycle.py`/`ops_dashboard/` 등 모든
   호출부에서 올바르게 처리되는지(예외를 삼켜 다시 fail-open으로 회귀하는 곳이 없는지).
3. CODEX-026 게이트가 실제로 side="buy"+is_live_mode 조합에서만 활성화되고, Paper 거래 경로와
   청산 경로는 전혀 건드리지 않는지 코드 검색으로 재확인.
4. CODEX-027의 fill 검증이 entry(record_fill)와 exit(청산 경로의 `_apply_exit_fill_progress`)
   양쪽에 일관되게 적용되는지.
5. §10의 7개 위험 항목, 특히 §10.1(exit intent만 SQLite로 분리한 설계의 진짜 원자성 한계)과
   §10.3(direct broker 호출 미차단)이 실제로 안전에 영향을 주는지 판단.
6. 전체 테스트(923건)가 실제 네트워크/운영 파일 변경 없이 격리되어 있는지 임의 표본 재확인,
   특히 CODEX-023/024의 SQLite 관련 신규 테스트(`test_exit_reconciliation.py`,
   `test_exit_intent_ledger.py`)가 실제 저장소 루트 `TRADING_STATE.db`를 생성하지 않는지.

## 12. SHA-256 (주요 안전 크리티컬 파일, 두 사이클 내내 미변경 확인용)

```
ace396bc7271a34896737ae9674ac657798ad810c72ac0207a5c273819026f1e  docs/autonomous/CODEX_REVIEW.md
27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe  docs/live_review/LIVE_APPROVAL_RECORD.md
043a30a5dc9751c062a36a82d4d75fdbb47903a040fb7b9ab86509f047843d84  risk_config.py
34411cf9ff530e850e8de5700a19c47aa71369528f6f541c8edd0e553b9df65e  broker/broker_config.py
408e94af606ce9045b46c0b3c8aeb07f4c9ee140a595f8bab5d198397700f389  kill_switch_state.py
d06ef475cc0fa721faedd986f1eaaab6b4ac0e0192ed4bedd3b0d4d009e6c991  order_intent_ledger.py
```

(`CODEX_REVIEW.md`의 SHA-256은 이전 패키지 대비 변경됨 — 이는 Codex 자신의 최신 독립 검증 결과가
그 파일에 기록됐기 때문이며, 파일 손상이나 수동 편집이 아니다. 나머지 5개 안전 크리티컬 파일은
이전 패키지와 SHA-256이 동일 — 이번 사이클에서 전혀 건드리지 않았음을 재확인한다.)

운영 파일(md5, §7 근거):
```
a61104cf03499860ae89d4e194dc8c07  order_history.csv
09c77d24f6f392a49100d13d90d61aad  universe.csv
9054d0158cf10c47d0e01e8394daaeca  strategy_performance.csv
```

이 문서 작성 시점 `HEAD`: `4de0714c77e0ec6b859900490a466d305e3f28b5`

## 13. 다음 단계

1. 사용자에게 이 문서 완성을 보고.
2. Codex 통합 재검증 요청(§11의 검증 중점 영역 전달).
3. 판정 결과에 따라:
   - `PASS`/`PASS_WITH_CONDITIONS`: `CODEX_REVIEW.md`에 기록, §10 잔여 위험에 대한 후속 조치
     여부를 사용자와 논의(코드 변경이 필요하면 새로운 별도 사이클).
   - `FAIL`: 지적된 CRITICAL/HIGH를 동일한 패턴으로 수정하고 재검증 요청.
4. 어떤 결과든 `approved`/`live_enabled`/`main`/`origin`/실거래 활성화는 사용자의 명시적 승인
   없이는 건드리지 않는다.
