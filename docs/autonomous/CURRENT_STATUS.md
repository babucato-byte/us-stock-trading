# CURRENT_STATUS

마지막 갱신: 2026-07-23

## 현재 Phase
Phase 2 — 초단타 관심종목 선별 엔진 (`IMPLEMENTED`, CODEX-010~015 수정 완료, Codex 재검증 대기)

Phase 1 최종 판정(유지): **Phase 1A(주문 진입 안전성) = VALIDATED**, **Phase 1B(부분체결·포지션 생명주기) = DEFERRED_TO_PHASE_5**.

Phase 3(1분봉 감시/지표/주문 로직)은 이번 사이클에서 착수하지 않음 — 사용자 지시에 따라 범위 외.

## 제한적 실거래 검토 사이클 — CODEX-016~019 (run 완료, 2026-07-23)
Codex 독립 검증(`docs/autonomous/CODEX_REVIEW.md`, 커밋 `e0dc855`) 판정 Overall verdict **FAIL**,
Limited live review **BLOCKED**에 따라 HIGH 2건(CODEX-016, CODEX-017)·MEDIUM 2건(CODEX-018, CODEX-019)을
전부 수정(t1~t4, 커밋 `6ad4841`/`79eaa81`/`00b0f68`/`50a097d`). 상세는
`docs/autonomous/REMEDIATION_PLAN.md`("제한적 실거래 검토 사이클 — CODEX-016~019")와
`docs/autonomous/VALIDATION_PACKAGE.md`("CODEX-016~019 수정 완료") 참고. 전체 회귀
`venv/bin/python -m pytest -q` 417 passed, 0 failed. **run 상태: `READY_FOR_CODEX_REVALIDATION`** —
Claude 자체 판정으로 확정하지 않으며 Codex의 독립 재검증 전까지 `docs/live_review/`의 제한적 실거래
검토는 재개하지 않는다. `docs/autonomous/CODEX_REVIEW.md`는 이번 run에서 수정하지 않고 그대로 보존.

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
267 passed, 0 failed (Phase 2 전용 118건 포함)

## 실패 테스트
없음

## 현재 블로커
없음. Phase 2는 Claude 자체 테스트 통과만으로 `VALIDATED` 처리하지 않음 — Codex 재검증의 `PROCEED` 판정 대기.

## 다음 작업
1. `VALIDATION_PACKAGE.md`/`VALIDATION_REPORT.md`/`REMEDIATION_PLAN.md`/`SCALPING_V1_ROADMAP.md`/`DECISION_LOG.md`를 CODEX-010~015 기준으로 갱신(진행 중).
2. Codex 재검증 요청. `PROCEED` 판정 시 Phase 2를 `VALIDATED`로 승격, Phase 3 착수 여부 보고.
3. `~/Projects/ai-orchestrator`를 통해 실거래 직전 준비(`READY_FOR_LIMITED_LIVE_REVIEW`) 작업 진행 중 — 포지션/일일 한도, kill switch, 재시작 후 중복 주문 방지, 모니터링 등 신규 항목은 오케스트레이터 run으로 별도 추적.
4. Phase 5 착수 전 사용자 결정이 필요한 SQLite 관련 항목(`DECISION_LOG.md`, `NEEDS_USER_DECISION`)은 여전히 대기 중 — Phase 2/3와는 무관.

## 최근 커밋
- `4f1f89d` Correct volume and premarket calculations (CODEX-015)
- `7ab8db7` Align watchlist lifecycle and timestamp validation (CODEX-014)
- `ac2b4b3` Make watchlist persistence failures explicit (CODEX-013)
- `044df60` Gate the pipeline behind trading-day and allowed-session checks (CODEX-012)
- `427958a` Enforce market data freshness and session gates for provider snapshots (CODEX-011)
- `a7736d5` Reject non-finite scalping candidate inputs (CODEX-010)

## 미반영 검증 지적사항
없음. Phase 1의 CODEX-001~009, Phase 2의 CODEX-010~015 전부 Claude 측 수정/테스트 완료. Codex 최종 재검증 대기 중(Phase 2 `PROCEED` 여부 미확정).
