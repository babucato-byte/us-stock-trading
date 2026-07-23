# CURRENT_STATUS

마지막 갱신: 2026-07-24

## 현재 Phase
Phase 2 — 초단타 관심종목 선별 엔진 (`IMPLEMENTED`, CODEX-010~015 수정 완료, Codex 재검증 대기)

Phase 1 최종 판정(유지): **Phase 1A(주문 진입 안전성) = VALIDATED**, **Phase 1B(부분체결·포지션 생명주기) = DEFERRED_TO_PHASE_5**.

Phase 3(1분봉 감시/지표/주문 로직)은 이번 사이클에서 착수하지 않음 — 사용자 지시에 따라 범위 외.

## 제한적 실거래 검토 사이클 — CODEX-020·CODEX-018 잔여분 수정 (2026-07-24)
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
489 passed, 0 failed, 2 warnings

## 실패 테스트
없음

## 현재 블로커
CODEX-020·CODEX-018 잔여분 수정에 대한 Codex 독립 재검증 대기. `approved: false`,
`live_enabled: false` 유지. **Limited live review: BLOCKED**, **Live trading: DO_NOT_ENABLE**.

## 다음 작업
1. `VALIDATION_PACKAGE.md`/`VALIDATION_REPORT.md`/`REMEDIATION_PLAN.md`/`DECISION_LOG.md`/
   `docs/live_review/*.md`를 CODEX-020·CODEX-018 잔여분 기준으로 갱신(완료, 이번 커밋).
2. Codex 재검증 요청. `PROCEED` 판정 시 CODEX-016~020 전체를 RESOLVED로 최종 확정하고 limited
   live review 재개 여부 판단.
3. `~/Projects/ai-orchestrator`를 통해 실거래 직전 준비 작업 진행 중 — 신규 항목은 오케스트레이터
   run으로 별도 추적.
4. Phase 5 착수 전 사용자 결정이 필요한 SQLite 관련 항목(`DECISION_LOG.md`, `NEEDS_USER_DECISION`)은 여전히 대기 중 — Phase 2/3와는 무관.

## 최근 커밋
- `ed452da` CODEX-018 잔여분: 공통 gate 에서 현재 credentials 재검증
- `66eda8a` CODEX-020: broker `_request` 공통 경로에 kill switch 게이트 추가
- `4f1f89d` Correct volume and premarket calculations (CODEX-015)
- `7ab8db7` Align watchlist lifecycle and timestamp validation (CODEX-014)
- `ac2b4b3` Make watchlist persistence failures explicit (CODEX-013)
- `044df60` Gate the pipeline behind trading-day and allowed-session checks (CODEX-012)
- `427958a` Enforce market data freshness and session gates for provider snapshots (CODEX-011)
- `a7736d5` Reject non-finite scalping candidate inputs (CODEX-010)

## 미반영 검증 지적사항
없음. Phase 1의 CODEX-001~009, Phase 2의 CODEX-010~015, 제한적 실거래 검토의 CODEX-016~020 전부
Claude 측 수정/테스트 완료. Codex 최종 재검증 대기 중(Phase 2 `PROCEED` 여부, CODEX-020·018 잔여분
`RESOLVED` 여부 모두 미확정).
