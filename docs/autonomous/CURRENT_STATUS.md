# CURRENT_STATUS

마지막 갱신: 2026-07-25

## 현재 Phase
Phase 4 — VWAP 마이크로 풀백 전략 엔진 (Stage 3, `IMPLEMENTED`, Claude 자체 테스트 통과, Codex
검증 전). Phase 2 — 초단타 관심종목 선별 엔진 (`IMPLEMENTED`, CODEX-010~015 수정 완료, Codex 재검증
대기, 변경 없음).

Phase 1 최종 판정(유지): **Phase 1A(주문 진입 안전성) = VALIDATED**, **Phase 1B(부분체결·포지션 생명주기) = DEFERRED_TO_PHASE_5**.

Phase 3(1분봉 실시간 수집/폴링 인프라)은 이번 사이클에서도 착수하지 않음 — Stage 3는 전략 플러그인
인터페이스/레지스트리/`VWAP_MICRO_PULLBACK_MOMENTUM_V1` 로직 자체만 구현했고, 구성된 pandas
DataFrame을 입력으로 받아 테스트한다. 라이브 1분봉 폴링은 여전히 범위 외.

## Stage 3 — 전략 플랫폼(`strategy/`) 구현 완료 (2026-07-25)
`docs/autonomous/SCALPING_V1_ROADMAP.md` Phase 4 대응. 신규 패키지 `strategy/`(`interface.py`,
`status.py`, `registry.py`, `plugins/vwap_micro_pullback_v1.py`, `plugins/__init__.py`,
`plugins/_example_orb_stub.py`)와 `config/scalping_strategy_v1_config.py`를 추가했다.

- `TradingStrategy` ABC(`strategy/interface.py`): `strategy_id`/`version`/`status`를 생성 시점에
  fail-closed 검증. `evaluate_setup`/`generate_entry`/`calculate_stop`/`calculate_targets`는 Stage 3
  실 구현. `manage_position`/`invalidate`는 `NotImplementedError` 스텁(Stage 4/Phase 5 포지션
  생명주기 선행 필요, 코드 주석에 이유 명시).
- 전략 상태(`strategy/status.py`): `COLLECTED/STRUCTURED/REVIEWED/BACKTESTED/PAPER_APPROVED/
  LIMITED_LIVE_APPROVED/ACTIVE/PAUSED/REJECTED` 9종, `ORDER_GENERATING_STATUSES={ACTIVE}`로 주문
  생성 가능 여부를 단일 지점에서 정의.
- `StrategyRegistry`(`strategy/registry.py`): 등록 시점에 strategy_id/version/status 검증(fail-closed),
  ACTIVE 최대 1개를 구조적으로 강제(두 번째 ACTIVE 등록/활성화 시도는 `StrategyRegistrationError`로
  거부, 첫 번째를 암묵적으로 비활성화하지 않음 — 결정 근거 `DECISION_LOG.md`), `get_active_strategy()`
  (없으면 `None`), `require_active()`/`select_strategy_for_order()`(ACTIVE가 아니면
  `StrategyNotActiveError`, PAPER_APPROVED/LIMITED_LIVE_APPROVED도 차단).
- `VWAP_MICRO_PULLBACK_MOMENTUM_V1`(`strategy/plugins/vwap_micro_pullback_v1.py`): VWAP/EMA9/EMA21을
  pandas로 직접 계산(`indicators.py`는 일봉 HMA 계열 전용이라 재사용 대상 아님을 확인 후 판단).
  price>VWAP·EMA9>EMA21 → 초기 rally → 얕은 pullback(거래량 감소) → 재돌파(거래량 재확대) 순으로
  판정, 손절은 micro-pullback low + ATR 기반 최소 버퍼, 목표는 1R에서 50% 분할 익절(문서에 명시된
  값) + target_2 2R(ASSUMPTION, 근거 `DECISION_LOG.md`).
- 주문 경로 연결: `paper_strategy_order.submit_order()`에는 현재 `strategy_id` 개념 자체가 없어
  (하드코딩된 단일 스코어링만 존재) 가짜 연결점을 만들지 않았다 — `require_active()`/
  `select_strategy_for_order()`를 `strategy/registry.py`의 독립 함수로 구현하고
  `tests/test_strategy_platform.py`에서 직접 검증. Stage 4가 실제 주문 트리거 경로에서 호출할
  예정(코드 주석에 명시).
- 확장 패턴: `strategy/plugins/__init__.py` 모듈 docstring + `strategy/plugins/_example_orb_stub.py`
  (미구현 스텁, ORB류 신규 전략 추가 시 따라야 할 최소 형태 예시).
- 신규 테스트: `tests/test_strategy_platform.py` 43건(레지스트리 검증/ACTIVE 1개 강제/가드/플러그인
  entry-present·VWAP-EMA 실패·pullback 없음·stop/target 정합성·Stage4 스텁 등).
- 전체 회귀: 저장소 루트 `venv/bin/python -m pytest -q` 기준 **613 passed, 0 failed, 2 warnings**
  (기존 570 + 신규 43). 실제 네트워크 호출 0회(모두 구성된 pandas DataFrame 사용), `order_history.csv`
  /`universe.csv`/`strategy_performance.csv` MD5 불변 확인, `broker/`·`order_safety.py`·
  `config/scanner_presets.json`·`.env`·kill switch 상태 파일 변경 없음.
- 잔여 위험: Stage 4(Phase 5, 포지션 생명주기)가 `manage_position`/`invalidate`를 실제 구현하고
  `require_active()`를 실제 주문 트리거 경로에 배선해야 함. 임계값(눌림 깊이 %, rally 최소 %,
  target_2 R-배수 등)은 Phase 6 백테스트 이전까지 잠정값(`DECISION_LOG.md`). 신호 중복 방지/추격진입
  방지/실시간 스프레드·유동성 차단/stale 데이터 차단은 Phase 3(1분봉 실시간 수집, 여전히
  `NOT_STARTED`) 착수 후 별도 구현 필요.

## Codex 최종 독립 재검증: PASS_WITH_CONDITIONS (2026-07-25, 커밋 `a31290b`/`5aac75b`/`8803252` 대상)
Overall verdict **`PASS_WITH_CONDITIONS`**. CODEX-016~022 전부 **RESOLVED**로 최종 확정, 신규
CRITICAL/HIGH/MEDIUM Finding 없음. Limited live review 권고: **`READY_FOR_LIMITED_LIVE_REVIEW`**
— 단 **Live trading: DO_NOT_ENABLE`**을 유지하며, 이 권고 자체가 실거래 승인을 의미하지 않는다.
남은 조건은 전부 코드 Finding이 아니라 운영자가 실제로 채워야 하는 `TBD` 항목(실제 Alpaca
계정/credential, 현재 포지션·미체결 주문·broker reconciliation, 허용 종목·거래시간·주문당 절대
한도, 승인자·검토 시각·롤백 담당자)이며, `docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md`에 각
항목의 권장값 초안·근거·위험·승인 필요 여부가 정리되어 있다. `approved: false`,
`live_enabled: false`는 변경하지 않았다. 상세: `docs/autonomous/CODEX_REVIEW.md`(커밋 `d38cb95`).

## 제한적 실거래 검토 사이클 — CODEX-022 해결 및 CODEX-021 잔여분 종결 (2026-07-25, 이전 기록)
Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ae3ca`/`c133e01`/`cc740a5`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/018/019는 RESOLVED로 재확인됐으나, CODEX-021(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-022(HIGH)**가 제기됐다 — `RequestPurpose` 재설계
(커밋 `c133e01`) 이후에도 `_request()`가 주문 POST의 payload `side`와 `order_side`, `purpose`
세 값을 서로 대조하지 않아, `purpose=EXIT_ORDER`를 선언한 채 매수 payload(`json={"side":
"buy"}`)를 전달하면 `ENTRY_DISABLED` 상태에서도 HTTP가 실제로 나갔다.

이번 사이클(t1)에서 `broker/alpaca_client.py`에 신규 `validate_order_intent(purpose, order_side,
payload)`를 도입해 `_request()`가 세션 호출 전, 다른 어떤 안전장치보다도 먼저 이 3자 일치를
검증하도록 배선했다(커밋 `5aac75b`):
- **CODEX-022 (HIGH)**: `_PURPOSE_REQUIRED_SIDE` 매핑(`ENTRY_ORDER→"buy"`, `EXIT_ORDER→"sell"`)
  기준으로, `ENTRY_ORDER`/`EXIT_ORDER`는 `order_side`와 payload의 `side`가 모두 존재하고 정확히
  요구되는 문자열과 완전히 일치해야 한다(대소문자·공백·`bool`/`int` 변형도 거부). 불일치·누락·
  비-dict body는 모두 `ValueError`로 세션 호출 전에 차단된다.
- **CODEX-021 잔여분 (HIGH)**: 위와 동일한 함수로 함께 닫혔다 — `order_side`가 이제 실제로
  payload `side`와 대조되므로 2차 방어선으로서 실질적 방어력을 갖는다.

CODEX-016~019(다단계 kill switch 배선, Slack health 배선, 주문 직전 credential/환경 재검증,
상태 저장소 파일 잠금)는 이번 사이클에서 **재작업하지 않았다** — 관련 회귀 테스트
(`tests/test_paper_strategy_order_kill_switch_state.py` 12건,
`tests/test_paper_strategy_order_notification_health.py` 6건,
`tests/test_state_store_concurrency.py` 6건, 도합 36 passed)로 회귀 없음만 확인했다.

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **570 passed, 0 failed, 2
warnings**다. 집중 테스트(`tests/test_broker_kill_switch_gate.py` +
`tests/test_broker_request_purpose.py` + `tests/test_broker_order_intent_gate.py`(신규) +
`tests/test_alpaca_client_runtime_revalidation.py` + `tests/test_broker_safety.py` +
`tests/test_universe_builder.py` + `tests/test_paper_strategy_order_kill_switch_state.py` +
`tests/test_paper_order_execution.py`) **289 passed, 1 warning**. `order_history.csv`/
`universe.csv` SHA-256은 이전 사이클 기록값과 동일(불변), `.env`·kill switch/notification 상태
파일 변경 없음. 현재 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지
**Limited live review: BLOCKED**, **Live trading: DO_NOT_ENABLE**을 유지한다.

## 이전 사이클 — CODEX-021 해결 및 CODEX-020 잔여분 종결 (2026-07-25, 역사적 기록)
Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `66eda8a`/`ed452da`/`cf5601d`/`edc5ad5`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/018/019는 RESOLVED로 재확인됐으나, CODEX-020(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-021(HIGH)**이 제기됐다 — `_request()`의 `order_side`가
필수 인자이긴 했지만 POST 경로와 의미적으로 결합되지 않아, `broker._request("POST", "/v2/orders",
order_side=None, ...)`처럼 명시적으로 `None`을 전달하면 `_check_kill_switch(None)`이 method/path를
전혀 확인하지 않고 즉시 반환해 kill switch를 우회할 수 있었다.

이번 사이클(t1)에서 `AlpacaBroker._request()`를 `order_side` 단일 신호가 아니라 신규
`RequestPurpose` enum(`READ_ONLY`/`ENTRY_ORDER`/`EXIT_ORDER`/`CANCEL_ORDER`/`RECONCILIATION`)
기반으로 재설계했다(커밋 `c133e01`):
- **CODEX-021 (HIGH)**: `_request()`에 기본값 없는 keyword-only `purpose` 인자를 추가하고,
  `isinstance(purpose, RequestPurpose)`를 요구해 `None`을 포함한 잘못된 값은 `ValueError`로
  세션 접근 전에 차단한다. `_METHOD_PURPOSES` 매트릭스가 HTTP method(GET/POST/DELETE)와
  purpose의 허용 조합을 명시적으로 검사해, 예컨대 POST가 `READ_ONLY`를 주장하거나 GET이
  `ENTRY_ORDER`를 주장하는 불일치를 세션 호출 전에 거부한다. `_check_kill_switch()`는 이제
  `purpose`가 `ENTRY_ORDER`/`EXIT_ORDER`일 때만 kill switch를 검사하며, `order_side`는 payload의
  `side`와 `purpose`가 일치하는지 확인하는 2차 방어선으로만 쓰인다(`submit_order()`가
  `_SIDE_TO_PURPOSE`로 파생한 `purpose`와 payload의 `order["side"]`가 다르면 세션 호출 전에
  `RuntimeError`).
- **CODEX-020 잔여분 (HIGH)**: 위와 동일한 재설계로 함께 닫혔다 — method+path 기반 주문 감지
  백스톱이 없다는 지적이 `_METHOD_PURPOSES` 매트릭스로 해결됐다. 조회·취소 경로
  (`get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order`)는 각각 `RequestPurpose.READ_ONLY`/
  `RECONCILIATION`/`CANCEL_ORDER`를 명시해 kill switch 정책과 무관하게 계속 동작한다.

CODEX-016~019(다단계 kill switch 배선, Slack health 배선, 주문 직전 credential/환경 재검증,
상태 저장소 파일 잠금)는 이번 사이클에서 **재작업하지 않았다** — 관련 회귀 테스트
(`tests/test_paper_strategy_order_kill_switch_state.py` 12건, `tests/test_paper_strategy_order_notification_health.py`
6건, `tests/test_state_store_concurrency.py` 6건, 도합 36 passed)로 회귀 없음만 확인했다.

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **536 passed, 0 failed, 2 warnings**다.
집중 테스트(`tests/test_broker_kill_switch_gate.py` + `tests/test_broker_request_purpose.py`(신규) +
`tests/test_alpaca_client_runtime_revalidation.py` + `tests/test_broker_safety.py` +
`tests/test_universe_builder.py` + `tests/test_paper_strategy_order_kill_switch_state.py` +
`tests/test_paper_order_execution.py`) **255 passed, 1 warning**. `order_history.csv`/`universe.csv`
SHA-256은 이전 사이클 기록값과 동일(불변), `.env`·kill switch/notification 상태 파일 변경 없음.
현재 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review:
BLOCKED**, **Live trading: DO_NOT_ENABLE**을 유지한다.

## 이전 사이클 — CODEX-020·CODEX-018 잔여분 수정 (2026-07-24, 역사적 기록)
최신 Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ee8d6`/`03962d3`/`cf4ada9`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/019는 RESOLVED로 재확인됐으나, CODEX-018(MEDIUM)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-020(HIGH)**이 제기됐다 — direct
`AlpacaBroker.submit_order()`가 `paper_strategy_order.py` wrapper를 거치지 않고 직접 호출되면
binary kill switch(`kill_switch.is_trading_halted()`)와 다단계 kill switch
(`kill_switch_state.is_entry_allowed()`/`is_liquidation_allowed()`)를 모두 우회해 HTTP가 실제로
나갔다. 또한 CODEX-018의 "현재 credentials 재검증" 요구사항이 `_validate_runtime_safety()`에
아직 배선되지 않았다는 지적도 함께 남아 있었다.

이번 사이클(t1~t2)에서 두 항목을 broker 공통 경로에 배선했다:
- **CODEX-020 (HIGH)**: `AlpacaBroker._request()`에 `order_side`(주문이 아니면 `None`, 매수/매도면
  `"buy"`/`"sell"`) 키워드 전용 필수 인자를 추가하고, 내부에서 신규 `_check_kill_switch()`가
  binary halt와 side별 4-state(`is_entry_allowed`/`is_liquidation_allowed`) 정책을 매 요청마다
  다시 조회해 불허 시 HTTP 호출 전에 `RuntimeError`를 발생시키도록 배선했다(커밋 `66eda8a`).
  `get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order` 등 조회·취소 경로는 `order_side=None`으로 명시해
  kill switch 정책과 무관하게 계속 동작하도록 분리했다. `_request()`를 우회해 `order_side`를
  생략하면 네트워크 호출 전에 `TypeError`로 즉시 차단된다.
- **CODEX-018 잔여분 (MEDIUM)**: `_validate_runtime_safety()`에 `_validate_current_credentials_match_captured()`를
  추가해, 매 요청마다 `BrokerConfig.from_env()`로 현재 환경의 API key/secret을 다시 읽어
  생성 시점에 캡처된 값과 `hmac.compare_digest()`로 상수시간 비교한다. 누락/공백/회전/삭제/환경
  읽기 실패 시 모두 요청 전에 차단하며, credential 값 자체는 예외 메시지에 포함하지 않는다(커밋 `ed452da`).

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **489 passed, 0 failed, 2 warnings**다.
집중 테스트(`tests/test_broker_kill_switch_gate.py` 25건, `tests/test_alpaca_client_runtime_revalidation.py`
44건 포함) 208 passed. `order_history.csv`/`universe.csv` SHA-256은 `CODEX_REVIEW.md`에 기록된
값과 동일(불변), `.env`·kill switch/notification 상태 파일 변경 없음. 현재 상태는
**`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review: BLOCKED**,
**Live trading: DO_NOT_ENABLE**을 유지한다.

## 마지막 완료 작업 (CODEX-010~015 수정 사이클)
- CODEX-010 (HIGH): `numeric_guard.require_finite_number()` 도입, `features.py`의 모든 raw/derived 수치에 NaN/Infinity 명시 차단 적용.
- CODEX-011 (HIGH): `SymbolSnapshot`에 `data_as_of`/`provider_fetched_at` 분리, `freshness.py` 신규(세션별 최대 데이터 나이), `YFinanceMarketDataProvider`가 손상/미래/타임존無 타임스탬프를 fail-closed 반환.
- CODEX-012 (MEDIUM): `calendar_guard.py` 신규 — 휴장일(`market_guard.is_us_trading_day`)/허용 세션/정규장 오픈 윈도우를 provider·파일 접근 이전에 게이트, 차단 시 `SKIPPED`(미저장).
- CODEX-013 (MEDIUM): `save_watchlist_cycle()`이 `{success, persisted_count, error_code, error_message}` 반환 + 쓰기 후 재검증(`_verify_after_write`), `run_scan_cycle()` 결과에 `status/error_code/error_message` 포함.
- CODEX-014 (MEDIUM): `first_detected_at/last_detected_at/updated_at` 3분리, `detect_count` 기반 실제 NEW→ACTIVE 전이, `validate_lifecycle_timestamps()`로 손상된 타임스탬프를 가진 행은 방치 대신 REJECTED 처리(TTL 우회 차단).
- CODEX-015 (LOW): `_compute_average_volume()`이 당일(미완료) 봉을 제외하고 최소 완료일수 미만이면 `None` 반환; `filter_premarket_rows()` 순수함수로 04:00~09:30 ET premarket 구간 분리, `premarket_coverage_complete` 필드로 부분 구간 여부 명시.
- 신규 테스트 65건 (`tests/test_scalping_watchlist.py` 103건 → 118건: CODEX-015분 15건 포함).
- 전체 회귀 267 passed(레포 루트 `pytest -q`/`python -m pytest -q` 동일), 실제 외부 API 호출 0회, `order_history.csv` 해시 불변, 운영 파일 변경 없음 확인.

## 현재 테스트 수
613 passed, 0 failed, 2 warnings (Stage 3 전략 플랫폼 신규 43건 포함)

## 실패 테스트
없음

## 현재 블로커
CODEX-022 해결 및 CODEX-021 잔여분 종결(`validate_order_intent()` 3자 일치 검증)에 대한 Codex
독립 재검증 대기. `approved: false`, `live_enabled: false` 유지. **Limited live review: BLOCKED**,
**Live trading: DO_NOT_ENABLE**.

## 다음 작업
1. `VALIDATION_PACKAGE.md`/`VALIDATION_REPORT.md`/`REMEDIATION_PLAN.md`/`DECISION_LOG.md`/
   `docs/live_review/*.md`를 CODEX-022 해결 및 CODEX-021 잔여분 종결 기준으로 갱신(완료, 이번 커밋).
2. Codex 재검증 요청. `PROCEED` 판정 시 CODEX-016~022 전체를 RESOLVED로 최종 확정하고 limited
   live review 재개 여부 판단.
3. `~/Projects/ai-orchestrator`를 통해 실거래 직전 준비 작업 진행 중 — 신규 항목은 오케스트레이터
   run으로 별도 추적.
4. Phase 5 착수 전 사용자 결정이 필요한 SQLite 관련 항목(`DECISION_LOG.md`, `NEEDS_USER_DECISION`)은 여전히 대기 중 — Phase 2/3와는 무관.

## 최근 커밋
- `5aac75b` CODEX-022 해결 + CODEX-021 잔여분 종결: `_request()`에 중앙 집중식 3자 일치(purpose/order_side/payload side) 검증 추가 및 회귀 테스트
- `a31290b` Codex 독립 재검증 기록: FAIL, CODEX-021 partial, CODEX-022 신규
- `c133e01` CODEX-021/CODEX-020 잔여분: `_request()`를 RequestPurpose 기반으로 재설계하고 회귀 테스트 추가
- `47ae3ca` Codex 독립 재검증 기록: FAIL, CODEX-020 partial, CODEX-021 신규
- `ed452da` CODEX-018 잔여분: 공통 gate 에서 현재 credentials 재검증
- `66eda8a` CODEX-020: broker `_request` 공통 경로에 kill switch 게이트 추가
- `4f1f89d` Correct volume and premarket calculations (CODEX-015)
- `7ab8db7` Align watchlist lifecycle and timestamp validation (CODEX-014)
- `ac2b4b3` Make watchlist persistence failures explicit (CODEX-013)
- `044df60` Gate the pipeline behind trading-day and allowed-session checks (CODEX-012)

## 미반영 검증 지적사항
없음. Phase 1의 CODEX-001~009, Phase 2의 CODEX-010~015, 제한적 실거래 검토의 CODEX-016~022 전부
Claude 측 수정/테스트 완료. Codex 최종 재검증 대기 중(Phase 2 `PROCEED` 여부, CODEX-022 해결 및
CODEX-021 잔여분 종결의 `RESOLVED` 여부 모두 미확정).
