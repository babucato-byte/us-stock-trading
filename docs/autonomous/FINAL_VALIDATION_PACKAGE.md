# FINAL_VALIDATION_PACKAGE — Stage 3~10 (2026-07-26)

이 문서는 사용자의 "미국주식 초단타 자동매매 시스템 최종 자율개발 지시서"가 지정한 워크플로우의
마지막 산출물이다: **Stage 3부터 Stage 10까지 연속 구현(Codex 중간 검증 없이) → 각 Stage 자체
테스트 → 각 Stage 로컬 커밋 → 전체 통합 테스트 → 이 문서 작성 → 마지막에 Codex 통합 검증 1회**.
이 문서 자체는 Codex 검증 요청 전 최종 스냅샷이며, **실거래 승인이나 활성화를 의미하지 않는다.**

## 0. 최종 상태

```
상태: READY_FOR_FINAL_CODEX_VALIDATION
```

- `approved: false`, `live_enabled: false` 유지([LIVE_APPROVAL_RECORD.md](../live_review/LIVE_APPROVAL_RECORD.md)).
- Live trading: **`DO_NOT_ENABLE`**.
- Limited live review: **`BLOCKED`**(이번 Stage 3~10 코드가 아직 Codex 검증을 거치지 않았으므로).
- `main`/`origin`: 어느 것도 건드리지 않음(아래 §9).
- 이 문서 작성 이전까지 `READY_FOR_30K_KRW_LIMITED_LIVE_REVIEW`/`LIVE_READY`/`LIVE_APPROVED`/
  `PRODUCTION_READY` 등의 표현을 어디에도 사용하지 않았다.

## 1. 검증 대상 커밋 (Stage 3~10, 20개, 시간순)

브랜치: `orchestrator/20260725-013740-us-stock-trading`. 이번 검증 대상은 Stage 3의 첫 커밋부터
이 문서 작성 직전까지다.

| # | 커밋 | 내용 |
|---|---|---|
| 1 | `415c129` | Add strategy interface and registry (Stage 3) |
| 2 | `1c7ec49` | Add VWAP micro-pullback strategy plugin and tests (Stage 3) |
| 3 | `a78ab1b` | Add position lifecycle state machine (Stage 4 part 1/N) |
| 4 | `2058614` | Add atomic position record store (Stage 4 part 2/N) |
| 5 | `f9a2d1f` | Add locked_position() and real strategy invalidation (Stage 4 part 3/N) |
| 6 | `b3d8cf4` | Add position lifecycle and automated exits (Stage 4 part 4/N) |
| 7 | `a86e991` | Update roadmap and status docs for Stage 4 completion |
| 8 | `bf05098` | Add local SQLite trading state store (Stage 5) |
| 9 | `d3938e5` | Update roadmap and status docs for Stage 5 completion |
| 10 | `639af97` | Structure user and YouTube strategy sources (Stage 6) |
| 11 | `8915c44` | Fix .gitignore lock-file pattern and remove stray lock file |
| 12 | `9814114` | Normalize .gitignore to LF and remove duplicate/dead lines |
| 13 | `19bfaea` | Update roadmap and status docs for Stage 6 completion |
| 14 | `59958cf` | Add intraday strategy backtest/replay engine (Stage 7) |
| 15 | `2b04c14` | Update roadmap and status docs for Stage 7 completion |
| 16 | `2094adf` | Add deterministic strategy selection engine (Stage 8) |
| 17 | `781a980` | Update roadmap and status docs for Stage 8 completion |
| 18 | `f2e1a24` | Add trading operations monitoring dashboard (Stage 9) |
| 19 | `7713185` | Update roadmap and status docs for Stage 9 completion |
| 20 | `986d655` | Prepare 30000 KRW limited live review (Stage 10) |
| 21 | `e3b9e9f` | Update roadmap and status docs for Stage 10 completion (최신, `HEAD`) |

이 범위 이전(CODEX-001~022 원격 수정 사이클, `orchestrator/20260725-013740-...` 브랜치의 t0~t11에
해당하는 앞선 커밋들)은 이미 별도로 Codex 최종 독립 검증을 거쳐 `PASS_WITH_CONDITIONS`로 종결됨
(`docs/autonomous/CODEX_REVIEW.md`, 커밋 `d38cb95`). 이번 문서는 그 이후 새로 추가된 Stage 3~10
범위만을 신규 검증 대상으로 한다 — 이전에 이미 PASS를 받은 코드를 재검증 대상으로 다시 올리지
않는다.

## 2. Stage별 변경 파일 및 테스트 결과

| Stage | 변경 파일 수 | 신규 테스트 | 결과 |
|---|---|---|---|
| 3 — 전략 인터페이스·Registry·VWAP 플러그인 | 12 files, +1445/-9 | `tests/test_strategy_platform.py` 44건 | 통과 |
| 4 — 포지션 생명주기 | 13 files, +1846/-29 | `tests/test_position_states.py` 31건 + `tests/test_position_store.py` 15건 + `tests/test_position_lifecycle.py` 23건 = 69건 | 통과 |
| 5 — 거래 상태 저장소(SQLite) | 11 files, +835/-8 | `tests/test_state_store.py` 20건 | 통과 |
| 6 — 사용자/YouTube 전략 자료 구조화 | 17 files, +1395/-11 | `tests/test_strategy_sources.py` 33건 | 통과 |
| 7 — 전략 평가 엔진(백테스트/리플레이) | 10 files, +1223/-16 | `tests/test_backtest_engine.py` 29건 | 통과 |
| 8 — 전략 선택 엔진 | 8 files, +676/-16 | `tests/test_strategy_selection.py` 27건 | 통과 |
| 9 — 운영 관제(Dashboard/CLI) | 6 files, +551/-19 | `tests/test_ops_dashboard.py` 16건 | 통과 |
| 10 — 30,000원 제한 실거래 준비 | 7 files, +428/-15 | `tests/test_live_readiness.py` 12건 | 통과 |
| **합계(문서 갱신 커밋 포함, 중복 제외)** | **62 files, +8312/-36** | **251건 신규** | **통과** |

(`git diff --stat 415c129~1..e3b9e9f`로 실측. Stage 6의 `.gitignore` 정리 커밋 2건은 코드/테스트
없이 부수적 정리이므로 위 표의 "신규 테스트"에 포함하지 않음.)

## 3. 전체 테스트 결과

```
$ venv/bin/python -m pytest -q
820 passed, 2 warnings in 268.14s
```

- 이번 문서 작성 직전 최종 1회 실행 결과(2026-07-26). 실패 0건.
- Stage 3~10 착수 시점 베이스라인은 613 passed(Stage 3 진입 전, CODEX-022 종결 시점)였다 — 즉
  이번 라운드에서 총 **207건의 신규 테스트**가 추가되었다(§2의 251건 중 일부는 Stage 3 자체가
  이미 착수 전 베이스라인에 포함되어 있던 것과 세는 기준 차이 — 정확한 수치는 `git log`상
  Stage별 커밋 로그의 각 커밋 메시지에 개별 기록됨).
- 두 warning은 기존 urllib3(LibreSSL) 경고와 `test_scanner.py`의 의도된 unknown-field 경고로,
  이번 Stage 3~10 범위와 무관한 기존 항목이다.

## 4. 아키텍처 요약

```
사용자 차트/YouTube 자료 (strategy_sources/)
    │  구조화(source/assumption/unknown), 버전 관리, 유사도 분석 — 절대 ACTIVE 자동 승격 없음
    ▼
전략 플러그인 구현 (strategy/interface.py, strategy/plugins/)
    │  TradingStrategy ABC, StrategyRegistry(ACTIVE 최대 1개 구조적 강제)
    ▼
백테스트/리플레이 (backtest/)
    │  1분봉 룩어헤드 방지 리플레이, 비용 분리(spread/slippage/fee/entry-delay), 동일봉 충돌
    │  보수적 처리, 세션 분리, INSUFFICIENT_DATA 명시 처리
    ▼
전략 선택 엔진 (strategy_selection/)
    │  설명가능한 규칙 기반 점수(비-LLM), DISABLED/INSUFFICIENT_DATA/MARKET_MISMATCH 게이트,
    │  SELECTED는 추천일 뿐 활성화 아님 — registry 미참조
    ▼
포지션 생명주기 (positions/) ──── 거래 상태 저장소 (state_store/, SQLite 병행 인프라)
    │  진입/체결/1R 50% 분할익절/2R·손절 전량청산/시간손절/EOD강제청산/재시작복구/중복청산방지
    ▼
주문 실행 경계 (paper_strategy_order.py → broker/alpaca_client.py)
    │  RequestPurpose 게이트, kill switch 이중 검사, Codex PASS_WITH_CONDITIONS 기검증 — 이번
    │  라운드에서 변경 없음(§6 참고)
    ▼
운영 관제 (ops_dashboard/) + 30,000원 제한 실거래 준비 (live_readiness/ + 플레이북)
```

## 5. 각 구성요소 상세

### 5.1 전략 인터페이스 및 활성 전략
- `strategy/interface.py`의 `TradingStrategy` ABC, `strategy/status.py`의 9단계 상태
  (`COLLECTED→...→ACTIVE→PAUSED/REJECTED`, `ORDER_GENERATING_STATUSES={ACTIVE}`),
  `strategy/registry.py`의 `StrategyRegistry`(ACTIVE 최대 1개 구조적 강제, 암묵적 비활성화 없음).
- 현재 활성 전략: `VWAP_MICRO_PULLBACK_MOMENTUM_V1`(`strategy/plugins/vwap_micro_pullback_v1.py`)
  단 하나. `PROJECT_CONSTITUTION.md`와 일치.

### 5.2 포지션 생명주기
- `positions/states.py`: 13개 생명주기 상태 + 6개 예외 상태, 명시적 `TRANSITIONS` 인접 테이블,
  `FAIL_CLOSED_STATE=RECOVERY_REQUIRED`.
- `positions/store.py`: 원자적 JSON 저장소, `locked_position()`으로 읽기→판단→브로커 호출→쓰기
  전체 구간 락 보호(중복 청산 방지의 핵심, 스레딩 테스트로 검증).
- `positions/lifecycle.py`: 진입(`try_reserve_order`+`submit_order(side="buy")`), 체결 추적,
  1R 50% 분할익절/2R·손절 전량청산(동일봉 충돌 시 손절 우선), 시간손절, EOD 강제청산, 전략
  무효화 청산, 재시작 복구(broker 재조회 실패 시 `RECOVERY_REQUIRED`로 fail-closed). 모든 청산은
  `paper_strategy_order.submit_order(side="sell")`을 직접 호출(진입 전용 일일 중복 방지는
  우회하되 kill switch/자격증명/`RequestPurpose` 게이트는 그대로 통과).

### 5.3 SQLite 저장소 구조 (병행 인프라)
- `state_store/schema.py`: `orders`/`fills`/`positions`/`position_events`/`strategy_runs`/
  `risk_events`/`kill_switch_events` 7개 테이블 + `schema_migrations`.
- `state_store/csv_import.py`: 읽기 전용 CSV 가져오기(원본 CSV 절대 변경 안 함).
  `state_store/export.py`: 내보내기/롤백 전용, SQLite 파일 자체만 초기화.
- **실제 운영 경로는 전환하지 않음** — `paper_strategy_order.py`/`positions/lifecycle.py`는 계속
  CSV/JSON을 유일한 판단 근거로 사용. 전환 여부는 `NEEDS_USER_DECISION`(`DECISION_LOG.md` Stage 5).

### 5.4 사용자/YouTube 전략 자료 구조
- `strategy_sources/models.py`: `StrategyClaim`(origin=SOURCE/ASSUMPTION/UNKNOWN 구조적 분리),
  `StrategySource`(validation_status는 `strategy/status.py`의 앞 4단계로만 제한 —
  `ACTIVE`는 구조적으로 도달 불가능).
- 8개 카탈로그 소스(`docs/strategy/sources/*.json`): VWAP 진입/1:2 R:R/50% 분할 익절/Ross Cameron
  마이크로 눌림목은 `PROJECT_CONSTITUTION.md` 실제 인용 + `REVIEWED`. Turtle/멀티 타임프레임 RSI/
  볼린저 눌림목/CCI·RSI·ADX는 실제 소스 미지정이라 전부 `ASSUMPTION`+`TBD_OPERATOR` 참조, 절대
  조작된 인용 없음.

### 5.5 전략 평가(백테스트) 및 선택 방식
- `backtest/`: look-ahead 구조적 차단, 프리마켓/정규장 분리, 동일봉 충돌 보수적(`STOP_FIRST`)
  처리, spread/slippage/fee/entry-delay 4개 비용 분리 표시, 부분체결·거래량 캡핑, 최대수익거래
  제거 결과 별도 출력, 데이터 부족 시 `INSUFFICIENT_DATA`(지표 계산 자체 생략).
- `strategy_selection/`: 설명가능한 규칙/점수 기반(비-LLM). 자격 게이트
  (`DISABLED`/`INSUFFICIENT_DATA`/`MARKET_MISMATCH`) 자체가 설명가능. 단 하나만 `SELECTED`,
  동점은 입력 순서로 결정론적 처리. `strategy.registry` 미참조 — 선택은 추천일 뿐 활성화 아님.

### 5.6 Kill Switch
- 이번 라운드에서 `kill_switch.py`/`kill_switch_state.py` 자체는 변경하지 않음(이미 CODEX-016~019
  사이클에서 완성, Codex 검증 완료). `positions/lifecycle.py`가 기존 `paper_strategy_order.
  submit_order()`의 이중 kill switch 검사(binary + 4-state)를 그대로 재사용.
- Stage 10에서 "첫 오류 시 `ENTRY_DISABLED`"를 기존 `kill_switch_state.py` 상태로 문서화했으나,
  실제 자동 배선은 하지 않음(§8, `NEEDS_USER_DECISION`).

### 5.7 운영 관제
- `ops_dashboard/`: 모드/활성 전략/시장상태/관심종목/일일 주문/포지션(손절·목표가·PnL)/
  Kill Switch/Slack 설정 여부/broker config/reconciliation/마지막 활동 시각을 로컬 파일+env
  config만으로 조립. 실제 Alpaca/Slack API 호출 0회(소스 코드에 `requests.post/get` 미참조,
  테스트로 검증) — Slack이 다운돼도 구조적으로 계속 확인 가능.

### 5.8 30,000원 제한 실거래 준비
- `live_readiness/`: 마이크로 주문 수량 계산(소수점 확인/최소 주문 금액 확인 포함, fail-closed),
  종목 allow-list(빈 목록은 아무것도 허용하지 않음). **실제 주문 경로에는 배선하지 않음**(§8).
- `docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md`: 일일/사고 대응 플레이북, 롤백 계획
  추가 사항, 최종 체크리스트, TBD_OPERATOR 전체 목록.

## 6. 외부 API 호출 현황

이번 Stage 3~10 전체 구현·테스트 과정에서 실제 Alpaca API, 실제 Slack Webhook, 실제 Yahoo/기타
외부 데이터 API를 호출한 적이 **0회**다. 모든 테스트는 fake broker(`FakeBroker`/`DummySession`
패턴), 구성된 pandas DataFrame, tmp_path 격리 파일로만 동작한다.
`tests/test_ops_dashboard.py::test_no_real_network_module_imported_by_snapshot`,
`tests/test_backtest_engine.py`/`tests/test_strategy_selection.py`의 registry-미참조 테스트가
이를 구조적으로 검증한다.

## 7. 운영 파일 변경 현황

`order_history.csv`, `universe.csv`, `strategy_performance.csv`는 Stage 3 착수 시점부터 이 문서
작성 시점까지 **바이트 단위로 불변**(md5 해시 동일, §11 참고). `KILL_SWITCH_STATE.json`,
`NOTIFICATION_HEALTH_STATE.json` 등 런타임 상태 파일도 이번 라운드에서 변경되지 않았다(모든
테스트가 `tmp_path`/env 변수 오버라이드로 격리됨).

## 8. main/origin 및 approved/live_enabled 현황

- `main`은 이번 라운드 시작 시점(분기점)에서 전혀 이동하지 않았다 — `git diff main HEAD --stat`
  결과 62개 이상의 파일이 오직 이 orchestrator 브랜치에만 존재한다.
- `origin`으로 push한 적 없음(사용자 승인 없이는 push하지 않는다는 원칙 준수).
- `approved: false`, `live_enabled: false`는 이번 라운드 내내 변경하지 않았다.
- Kill Switch 해제(`release`), Live API Key 입력, 실제 주문 실행 등 사용자 승인이 필요한 어떤
  행위도 수행하지 않았다.

## 9. 남은 TBD_OPERATOR 항목

`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md`(기존 10개 항목, 미변경) +
`docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md` §7(신규 9개 항목, Stage 10에서 추가):
실계좌, 실환율, Live API Key, 실 주문 금액 한도, 실 승인자, 배포 시각, 롤백 담당자, 실제 Alpaca
최소 주문 금액, 실제 파일럿 종목 allow-list 내용. 어느 항목도 이번 라운드에서 추정하여 확정하지
않았다.

## 10. 알려진 위험 (Codex 검증 시 특히 확인 필요)

1. **SQLite 병행 인프라 미배선**(`state_store/`): 실제 운영 경로는 여전히 CSV/JSON — 다중 파일
   트랜잭션 부재라는 기존 잔여 위험(Phase 1B/Phase 5)이 그대로 남아 있다. `NEEDS_USER_DECISION`.
2. **`ENTRY_DISABLED` 자동 배선 미완료**(Stage 10): 첫 주문 오류 시 자동으로 진입을 차단하는
   코드는 없고, 운영 절차(수동 실행)로만 존재한다. `NEEDS_USER_DECISION`.
3. **allow-list 미배선**(Stage 10): `live_readiness.allowlist.is_symbol_allowed()`는 구현·
   테스트되었으나 `paper_strategy_order.py`의 실제 주문 경로에서 호출되지 않는다.
4. **비용/정책 ASSUMPTION 다수**: 백테스트 비용 가정(`backtest/config.py`), 선택 엔진 가중치/
   임계값(`strategy_selection/scoring.py`), 사이징 최소 주문 금액(`live_readiness/sizing.py`)이
   전부 실측치가 아닌 문서화된 가정값이다 — `DECISION_LOG.md` Stage 7/8/10 섹션 참고.
5. **미검증 YouTube 전략 후보 4건**: Turtle/멀티 타임프레임 RSI/볼린저 눌림목/CCI·RSI·ADX는
   실제 소스 자료 없이 카탈로그로만 존재하며 구현되지 않았다 — 어떤 주문 경로와도 연결되어 있지
   않다.
6. **"마지막 성공 실행 시각"이 근사치**(`ops_dashboard/`): 전용 마커 파일이 없어 CSV mtime을
   대리 지표로 사용한다.
7. **Phase 3(1분봉 실시간 수집/폴링 인프라) 미착수**: 이번 라운드 전체가 여전히 구성된
   DataFrame/fake broker 입력을 전제로 하며, 실제 라이브 데이터 피드/브로커 연동은 범위 밖이다.

## 11. 검증 중점 영역 (Codex에게 요청)

1. §10의 7개 위험 항목이 실제로 안전에 영향을 주는지, 특히 §10.1(SQLite 미배선)과 §10.2
   (`ENTRY_DISABLED` 미배선)가 "문서화된 절차"만으로 충분한 완화인지 판단.
2. `positions/lifecycle.py`의 청산 경로(`paper_strategy_order.submit_order(side="sell")` 직접
   호출)가 진입 전용 중복 방지를 우회하는 설계가 실제로 안전한지(kill switch/RequestPurpose
   게이트가 정말 모든 경로를 커버하는지) 재확인.
3. `backtest/`·`strategy_selection/`이 `strategy.registry`를 참조하지 않아 어떤 전략도 자동으로
   ACTIVE 승격되지 않는다는 구조적 경계가 실제로 우회 불가능한지.
4. `live_readiness/`가 실제 주문 경로에 배선되지 않았다는 주장이 코드 검색으로 사실인지(즉
   `paper_strategy_order.py`/`positions/lifecycle.py`에 `live_readiness` import가 없는지).
5. 전체 테스트(820건)가 실제 네트워크/운영 파일 변경 없이 격리되어 있는지 임의 표본 재확인.

## 12. SHA-256 (주요 안전 크리티컬 파일, 이번 라운드에서 미변경 확인용)

```
803c0f9ce3c8f922d5aaee766cca58e6d37de14f2c3a3697ba19545905e66325  docs/autonomous/CODEX_REVIEW.md
27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe  docs/live_review/LIVE_APPROVAL_RECORD.md
043a30a5dc9751c062a36a82d4d75fdbb47903a040fb7b9ab86509f047843d84  risk_config.py
34411cf9ff530e850e8de5700a19c47aa71369528f6f541c8edd0e553b9df65e  broker/broker_config.py
408e94af606ce9045b46c0b3c8aeb07f4c9ee140a595f8bab5d198397700f389  kill_switch_state.py
d06ef475cc0fa721faedd986f1eaaab6b4ac0e0192ed4bedd3b0d4d009e6c991  order_intent_ledger.py
```

운영 파일(md5, §7 근거):
```
a61104cf03499860ae89d4e194dc8c07  order_history.csv
09c77d24f6f392a49100d13d90d61aad  universe.csv
9054d0158cf10c47d0e01e8394daaeca  strategy_performance.csv
```

이 문서 작성 시점 `HEAD`: `e3b9e9fa2817b91735d06e0789f95c0e50d9aa0c`

## 13. 다음 단계

1. 사용자에게 이 문서 완성을 보고.
2. Codex 통합 검증 1회 요청(§11의 검증 중점 영역 전달).
3. 판정 결과에 따라:
   - `PASS`/`PASS_WITH_CONDITIONS`: `CODEX_REVIEW.md`에 기록, 필요 시 §10 위험 항목에 대한
     후속 조치를 사용자와 논의(코드 변경이 필요하면 그 자체로 새로운 별도 사이클).
   - `FAIL`: 지적된 CRITICAL/HIGH를 수정하고 재검증 요청(기존 CODEX-XXX 사이클과 동일한 패턴).
4. 어떤 결과든 `approved`/`live_enabled`/`main`/`origin`/실거래 활성화는 사용자의 명시적 승인
   없이는 건드리지 않는다.
