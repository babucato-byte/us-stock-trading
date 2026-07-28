# REMEDIATION_PLAN

검증 기준(최신): `CODEX_REVIEW.md` (2026-07-21, 대상 커밋 `9688a13`/`b93a08a`/`22a6651`/`962eb69`/`1cc784b`; overall verdict **FAIL**, Phase 2 **DO_NOT_PROCEED**). 이 리뷰는 CODEX-003/004/005를 **RESOLVED**로 최종 확인했고, CODEX-001/002/006을 **PARTIALLY_RESOLVED**로 되돌렸으며, 신규 **CODEX-007(HIGH)**, **CODEX-008(HIGH)**, **CODEX-009(MEDIUM)**를 제기했다. 이번 문서는 그 전체에 대한 최종 처리 결과다. 처리 순서는 지시서 기준 `CODEX-007 → 008 → 009`.

## 이전 사이클에서 RESOLVED로 최종 확인된 항목 (변경 없음)

### CODEX-003 — 비원자적 쓰기, 잠금 없는 동시성 (HIGH) — RESOLVED (재확인)
커밋 `b93a08a`. 독립 재검증에서 threading 5회 반복 + 실제 multiprocessing 재현 모두 통과 확인. 상세는 이전 리뷰 기록 참고.

### CODEX-004 — import 경로 실행 위치 의존 (MEDIUM) — RESOLVED (재확인)
커밋 `962eb69`. 저장소 루트/상위 4가지 pytest 실행 형태 모두 동일 결과 재확인.

### CODEX-005 — 상위 디렉터리 스크래치 스크립트 수집 (HIGH) — RESOLVED (재확인)
커밋 `962eb69`. socket 차단 환경에서도 97 passed, 외부 연결 시도 없음 재확인.

## 이번 사이클에서 실제로 닫은 항목

### CODEX-001 — Live endpoint 접근 위험 (HIGH) — RESOLVED
- 이전 판정: PARTIALLY_RESOLVED — broker 5개 메서드 자체는 안전했으나, `universe_builder.py`가 공통 게이트를 우회해 CODEX-009로 분리 기록됨.
- 이번 처리: CODEX-009로 완전히 흡수·해결(아래 참고). broker 쪽 자체는 이전 사이클에서 이미 RESOLVED였고 이번에도 재현되지 않음.
- 커밋 해시: `16a1ee4` (CODEX-009)

### CODEX-002 — 일일 주문 제한 fail-open / 서버 로컬 날짜 (HIGH) — RESOLVED
- 이전 판정: PARTIALLY_RESOLVED — fail-closed/ET 날짜 자체는 정상이었으나, `load_order_history()`가 날짜 파싱 성공 여부만 확인하고 `YYYY-MM-DD` 정규 형식을 강제하지 않아 `"2026-07-20 10:30:00"` 같은 파싱 가능한 비정규 값이 `count_orders_for_date()`의 정확 문자열 비교를 우회했다(→ CODEX-007로 분리).
- 이번 처리: CODEX-007로 완전히 흡수·해결(아래 참고).
- 커밋 해시: `05757fe` (CODEX-007)

### CODEX-006 — 부분 체결 및 broker reconciliation (HIGH) — RESOLVED
- 이전 판정: PARTIALLY_RESOLVED — client_order_id 생성/조회/재주문 금지 자체는 정상이었으나, `order_reconciliation.csv`에 잠금이 없어 동시 갱신 시 최신 `FILLED` 상태가 유실되고, 즉시 응답 스냅샷과 reconciliation 결과가 서로 다른 상태를 기록할 수 있었으며, reconciliation 저장 실패가 호출자에게 전파되지 않았다(→ CODEX-008로 분리).
- 이번 처리: CODEX-008로 완전히 흡수·해결(아래 참고).
- 커밋 해시: `0c2dab4` (CODEX-008)

### CODEX-007 — 파싱 가능한 비정규 주문일이 일일 한도를 우회함 (HIGH)
- 재현 여부: 재현됨 — `"2026-07-20 10:30:00"`, `"20260720"` 등이 `pd.to_datetime(errors="raise")`는 통과하지만 `count_orders_for_date()`의 정확 문자열 비교에서 다른 날짜로 취급되어 당일 집계가 0이 될 수 있었다.
- 원인: 느슨한 파싱 성공 여부만 검증, 정규 형식(`YYYY-MM-DD`) 강제 없음.
- 수정 방안: `validate_order_date_str()` 신설 — 정규식(`^\d{4}-\d{2}-\d{2}$`) + 실제 달력 유효성(`strptime`) + 원본과의 왕복(round-trip) 일치를 모두 요구. 공백/타임존/datetime 접미사/zero-padding 누락/존재하지 않는 날짜를 전부 차단. `load_order_history()`가 모든 행에 이 함수를 적용하고, 단 하나라도 비정규 값이면 전체 이력을 `CORRUPTED_HISTORY`로 판정해 신규 주문을 차단(자동 변환 없음). 진단 전용 `diagnose_order_history_dates()`를 추가해 파일을 변경하지 않고 문제 행을 보고.
- 수정 파일: `paper_strategy_order.py`
- 테스트: 정상/차단 값 세트(정확히 지시서 목록대로), 비정규 날짜 포함 시 신규 주문 전체 차단, 파싱 가능하지만 비정규인 값이 집계를 0으로 만들지 않음을 직접 재현, 정상 과거/당일 날짜와 섞여도 차단, ET 자정 경계 집계 정확성, 중복 검사보다 날짜 검증이 먼저 수행됨, 진단 함수가 파일을 변경하지 않음.
- 처리 상태: RESOLVED
- 커밋 해시: `05757fe`

### CODEX-008 — reconciliation 동시 갱신 유실과 상태 불일치 (HIGH)
- 재현 여부: 재현됨 — multiprocessing으로 두 프로세스가 서로 다른 주문을 동시 갱신하면 마지막 writer의 스냅샷이 앞선 갱신을 덮어썼다. 즉시 부분체결 응답에서 reconciliation=`PARTIALLY_FILLED`, history=`SUBMITTED` 불일치도 재현됨. reconciliation 저장 실패가 전파되지 않음도 재현됨.
- 원인: `order_reconciliation.csv`에 파일 잠금 없음, load-modify-save가 원자적 트랜잭션이 아님, 상태 전이 단조성 검사 없음, 두 파일(`order_history.csv`/`order_reconciliation.csv`)이 서로 다른 소스에서 상태 문자열을 각자 계산.
- 수정 방안: 기존 `order_history` 잠금 로직을 `_file_lock()`으로 일반화해 재사용, `order_reconciliation.csv` 전용 lock 도입. `merge_reconciliation_state()`가 상태 전이 단조성(PENDING_SUBMISSION→{SUBMITTED,UNKNOWN}→PARTIALLY_FILLED→FILLED, 종결 상태는 불변, UNKNOWN은 더 진전된 상태를 절대 덮어쓰지 않음), `filled_qty` 비감소, `average_fill_price` 비소거를 강제. `load_reconciliation()`이 손상 파일에서 `ReconciliationUnavailable`을 발생시켜 fail-closed(자동 빈 파일 초기화 금지, 단 파일이 아예 없는 최초 상태는 정상 빈 이력으로 허용 — `order_history.csv`와 달리 이 파일은 duplicate/일일한도 안전 게이트가 아니므로 신규 주문 자체를 막지는 않음). reconciliation 저장 실패는 `try_reserve_order()`를 통해 주문 자체를 차단하도록 전파. `main()`의 즉시 상태 갱신과 reconciliation 스냅샷이 동일한 `_local_status_from_response()` 결과를 공유하도록 통일.
- 수정 파일: `paper_strategy_order.py`
- 테스트: `merge_reconciliation_state` 단위 테스트(상태 후퇴 방지/UNKNOWN 보호/filled_qty 비감소/가격 비소거/정상 진행), 손상 파일 fail-closed, reconciliation 저장 실패 시 주문 차단, 잠금 타임아웃 시 파일 미변경, 쓰기 실패 시 원본 보존, 즉시 응답-history 일관성, 반복 reconciliation 멱등성, **실제 `multiprocessing.Process` 2건**(동일 주문 동시 갱신→최종 FILLED+filled_qty≥70, 서로 다른 주문 동시 갱신→둘 다 보존).
- 처리 상태: RESOLVED
- 커밋 해시: `0c2dab4`

### CODEX-009 — universe_builder.py의 endpoint 안전 게이트 우회 (MEDIUM)
- 재현 여부: 재현됨(정적 분석) — `universe_builder.py`가 `ALPACA_PAPER_BASE_URL`/레거시 `ALPACA_BASE_URL`로 URL을 직접 조합해 import 시점에 `requests.get()`을 실행, broker 공통 안전검사(`validate_order_allowed()`)를 거치지 않았다.
- 원인: 이 파일만 `AlpacaBroker` 공통 경로를 쓰지 않고 독자적으로 구현됨.
- 수정 방안: `AlpacaBroker.get_assets()` 신규 추가(기존 `_request()` 게이트 재사용). `universe_builder.py`를 `fetch_active_us_equity_rows()`/`build_universe()` 함수로 재작성하고 `if __name__ == "__main__":` 가드 적용(부수효과로 테스트 가능성도 확보 — 이전에는 import만 해도 실네트워크 호출과 파일 쓰기가 발생했음). 허용 endpoint는 `BrokerConfig`의 기존 정확 일치 검사(`base_url.rstrip("/") == PAPER_BASE_URL`)를 그대로 재사용 — 별도 URL 파서를 새로 만들지 않고 이미 검증된 로직을 재사용.
- 수정 파일: `broker/alpaca_client.py`, `universe_builder.py`, `tests/test_universe_builder.py`(신규)
- 저장소 전체 점검: `requests.get/post`, `ALPACA_*_BASE_URL`, `api.alpaca.markets` 문자열을 grep. `broker/` 외 Alpaca 직접 호출은 `test_alpaca_account.py`/`test_paper_order.py` 뿐이며 둘 다 CODEX-005의 `collect_ignore` 대상 스크래치 파일(운영 파이프라인 미포함).
- 테스트: 정상 Paper endpoint만 GET 1회 허용, Live/임의/HTTP/변조 endpoint 및 잘못된 mode에서 GET 0회(스킴 다운그레이드·유사 호스트명·비표준 포트·경로 조작·userinfo·query 조작 포함 8종 파라미터라이즈), client 생성 후 config 교체 시 차단, 기존 필터링 로직 결과 동일, 환경변수 미설정 시 Live URL로 폴백하지 않음.
- 처리 상태: RESOLVED
- 커밋 해시: `16a1ee4`

## 요약 (Phase 1, CODEX-001~009)

CRITICAL 0건. HIGH 전부(001/002/003/005/006/007/008 — 003/005는 이전 사이클에서 이미 RESOLVED로 재확인) RESOLVED. MEDIUM 전부(004/009) RESOLVED. 남은 항목은 CODEX Finding이 아니라 Phase 1 자체 승인 기준(부분 체결의 포지션 상태 반영, Phase 5 범위)과, `order_history.csv`/`order_reconciliation.csv` 간 교차 파일 트랜잭션 정합성(`DECISION_LOG.md`에 `NEEDS_USER_DECISION`으로 기록, SQLite 전환 후보 — 아래 참고)이다.

---

## Phase 2 수정 사이클 — CODEX-010~015

검증 기준: 사용자가 전달한 `CODEX_REVIEW.md` 결과, overall verdict **FAIL**, Phase 2 **FAIL**, Phase 3 **DO_NOT_PROCEED**. 처리 순서 지시: `CODEX-010 → 011 → 012 → 013 → 014 → 015`. 이번 사이클은 Phase 3 코드(1분봉 감시, VWAP/EMA 전략, 주문 로직)를 일절 포함하지 않음.

### CODEX-010 — NaN/Infinity가 후보 통과를 우회함 (HIGH) — RESOLVED
- 원인: `features.py`의 수치 계산이 `<`/`>` 비교 기반이라 NaN이 모든 비교에서 False가 되어 임계값 체크를 통과했다.
- 수정: `numeric_guard.require_finite_number()`(명시적 `math.isnan`/`math.isinf`) 신설, raw/derived 전 수치 필드에 적용.
- 커밋: `a7736d5`

### CODEX-011 — 운영 provider의 데이터 최신성 검증이 사실상 비활성 (HIGH) — RESOLVED
- 원인: 최신성 게이트가 provider 요청 시각(`provider_fetched_at`)만 보고 실제 시세 시각(`data_as_of`)을 보지 않아, 오래된 봉도 "방금 조회했으니 신선함"으로 오판했다.
- 수정: `SymbolSnapshot`에 `data_as_of`/`provider_fetched_at` 분리, `freshness.py` 신설(세션별 최대 허용 지연), 파이프라인이 `now_dt` 기준으로 `data_as_of`만으로 신선도 판정. Provider 레벨에서도 타임존 없음/미래시각/미정렬/중복 인덱스를 fail-closed 처리.
- 커밋: `427958a`

### CODEX-012 — 휴장일/비허용 세션 차단 없음 (MEDIUM) — RESOLVED
- 원인: 파이프라인이 주말/공휴일/장외 시간에도 그대로 실행되어 오래되거나 무의미한 데이터로 워치리스트를 갱신할 수 있었다.
- 수정: `calendar_guard.check_pipeline_allowed()` 신설, provider/파일 접근 이전에 게이트. 차단 시 `SKIPPED` 반환(빈 워치리스트를 "정상"으로 저장하지 않음).
- 커밋: `044df60`

### CODEX-013 — 저장 실패가 성공처럼 보임 (MEDIUM) — RESOLVED
- 원인: `save_watchlist_cycle()`이 단순 bool만 반환해, 쓰기 실패/부분 실패가 호출자에게 성공과 동일하게 보였다.
- 수정: `{success, persisted_count, error_code, error_message}` 구조로 반환, 쓰기 후 재읽기로 행 수/컬럼/중복 심볼을 재검증(`_verify_after_write`). `run_scan_cycle()` 결과에 `status`(`SUCCESS`/`SKIPPED`/`FAILED_PERSISTENCE`)/`error_code`/`error_message` 포함.
- 커밋: `ac2b4b3`

### CODEX-014 — lifecycle 상태머신이 문서와 불일치, 손상 timestamp가 TTL 우회 (MEDIUM) — RESOLVED
- 원인: 신규 선정 행이 곧바로 `ACTIVE`로 생성되어 문서화된 `NEW → ACTIVE` 전이가 실제로 발생하지 않았고, `_apply_expiry()`가 타임스탬프 파싱 실패 시 해당 행을 그대로 방치해 손상된 값이 TTL/만료 비교 자체를 우회, 행이 영구히 ACTIVE로 남을 수 있었다.
- 수정: `first_detected_at`/`last_detected_at`/`updated_at` 3필드로 분리(모두 timezone-aware ISO 8601 필수), `repeat_tracker`의 `detect_count`를 기준으로 최초 탐지=`NEW`, 2회차부터=`ACTIVE`로 실제 전이. `models.validate_lifecycle_timestamps()`가 존재/비-sentinel/timezone-aware/파싱 가능/순서(`last_detected_at`, `expires_at` ≥ `first_detected_at`)를 모두 검사, 실패 시 해당 행을 `REJECTED`(사유 `INVALID_LIFECYCLE_TIMESTAMP`)로 즉시 처리.
- 정책 기록: 만료 후 같은 거래일 내 재탐지는 `repeat_tracker`의 `detect_count` 기억이 워치리스트 행의 영속 상태와 독립적이므로 `NEW`가 아니라 `ACTIVE`로 재개(이미 지속성을 증명한 심볼이라는 판단, `DECISION_LOG.md` 기록).
- 커밋: `7ab8db7`

### CODEX-015 — 평균거래량/premarket 시간 범위 계산 오류 (LOW) — RESOLVED
- 원인: 평균거래량 계산이 당일(미완료) 거래 봉을 포함해 장중에 평균을 인위적으로 낮추고 `relative_volume`을 부풀렸다. premarket 구간 필터링 로직이 실yfinance 호출 내부에 묻혀 있어 순수 단위 테스트가 불가능했다.
- 수정: `_compute_average_volume()`이 최근(당일) 봉을 제외 후 `AVERAGE_VOLUME_LOOKBACK_DAYS` 범위에서 평균, `MIN_VALID_VOLUME_DAYS` 미만이면 `None`. premarket 04:00~09:30 ET 필터링을 `filter_premarket_rows()` 순수함수로 분리(UTC/ET 변환, DST 포함 단위 테스트 가능), `premarket_coverage_complete` 필드로 부분 구간 여부를 명시.
- 커밋: `4f1f89d`

## 요약 (Phase 2, CODEX-010~015)

CRITICAL 0건. HIGH 전부(010/011) RESOLVED. MEDIUM 전부(012/013/014) RESOLVED. LOW(015) RESOLVED. 미해결 Finding 없음. Phase 3 코드는 이번 사이클에서 작성하지 않음. Codex 재검증(`PROCEED` 여부) 대기 — Claude 자체 판정으로 `VALIDATED` 승격하지 않음.

---

## 제한적 실거래 검토 사이클 — CODEX-016~019

검증 기준: `docs/autonomous/CODEX_REVIEW.md`(커밋 `e0dc855`에서 그대로 기록, 대상 커밋
`337ba16`~`b6f4924`). Overall verdict **FAIL**, Limited live review **BLOCKED**, Live trading
recommendation **DO_NOT_ENABLE**. 판정 요지: 이전 사이클(모듈 t1~t5)이 만든 다단계 kill switch
(`kill_switch_state.py`)와 Slack 헬스 모니터(`notification_health.py`)는 정확히 구현·단위테스트
되었지만 실제 주문/알림 경로(`paper_strategy_order.py`)에는 배선되지 않았다.

### [CODEX-016] HIGH — 다단계 kill switch가 실제 주문 경로를 차단하지 않음

```
Finding ID: CODEX-016
Severity: HIGH
Reproduced: 예 — Codex가 ENTRY_DISABLED 상태에서 paper_strategy_order.submit_order()를 호출해
  broker가 실제로 1회 호출되고 HTTP 200이 반환됨을 격리 재현으로 확인. is_entry_allowed()/
  is_liquidation_allowed()는 저장소 전체 운영 코드에서 호출되지 않고 테스트에만 존재함을 확인.
Root cause: paper_strategy_order.py가 kill_switch.is_trading_halted()(기존 binary halt)만 검사하고,
  이전 사이클에서 만든 kill_switch_state.py의 4단계 상태 모델은 실제 주문 진입점에 연결되지 않았다.
Affected path: paper_strategy_order.py (submit_order, main)
Required behavior: submit_order()가 매 호출마다 kill_switch_state의 현재 상태를 재조회해, 매수(entry)면
  is_entry_allowed(), 매도/청산이면 is_liquidation_allowed()를 확인하고 불허 시 broker 호출 없이 안전
  차단 응답을 반환한다. 기존 binary halt 체크는 유지(두 게이트 모두 통과해야 주문 진행). 상태 파일
  손상 시 fail-closed.
Implementation: `paper_strategy_order.py`에 `kill_switch_state.is_entry_allowed`/`is_liquidation_allowed`
  import 추가. `submit_order(symbol, qty=1, broker=None, client_order_id=None, side="buy")`에 `side`
  파라미터 신규 추가, 기존 `kill_switch.is_trading_halted()` 게이트 통과 직후 `is_liquidation_allowed()`
  (side="sell") 또는 `is_entry_allowed()`(그 외)를 재조회해 불허 시 `broker.submit_order()` 호출 전에
  HTTP 423 `BrokerResponse(data={"halted": True, "side": side})`를 반환한다. `main()`의 신규 진입 호출부는
  `submit_order(..., side="buy")`로 명시 배선.
Regression tests: `tests/test_paper_strategy_order_kill_switch_state.py` (12건) —
  `test_active_state_allows_buy_order`/`test_active_state_allows_sell_order`(ACTIVE에서 양방향 허용),
  `test_entry_disabled_blocks_buy_order`/`test_entry_disabled_allows_liquidation_sell_order`(ENTRY_DISABLED는
  신규 진입만 차단, 청산은 허용), `test_all_trading_disabled_and_manual_review_block_both_sides`(파라미터라이즈,
  ALL_TRADING_DISABLED/MANUAL_REVIEW는 양방향 차단), `test_corrupted_state_file_blocks_order_fail_closed`,
  `test_missing_state_file_defaults_to_active_existing_behavior`(기존 동작 회귀 없음),
  `test_binary_halt_still_blocks_even_when_state_is_active`(기존 binary halt 게이트 유지 확인),
  `test_main_blocks_new_orders_when_entry_disabled`/`test_main_submits_normally_when_active`/
  `test_main_blocks_new_orders_in_all_trading_disabled_and_manual_review`(main() 진입점까지 실제 배선 검증).
Status: RESOLVED
Commit: `6ad4841`
Remaining risk: 없음. `main()`의 청산/포지션 정리 경로가 향후 별도 함수로 분리될 경우 해당 호출부도
  `side="sell"`로 배선해야 함(현재는 `submit_order` 단일 진입점만 존재).
```

### [CODEX-017] HIGH — Slack health monitor가 운영 알림 경로에 연결되지 않음

```
Finding ID: CODEX-017
Severity: HIGH
Reproduced: 예 — 운영 Slack wrapper가 실패를 반환하도록 격리 재현한 결과 notification status가
  UNKNOWN으로 남고 상태 파일이 생성되지 않음을 확인.
Root cause: paper_strategy_order._safe_send_slack_alert()가 notification_health.py의
  send_with_health_tracking()/record_success()/record_failure()를 거치지 않고 send_slack_alert()를
  직접 호출한다.
Affected path: paper_strategy_order.py (_safe_send_slack_alert)
Required behavior: _safe_send_slack_alert()가 notification_health.send_with_health_tracking()을 통해
  발송하도록 변경, 성공/실패가 실제로 기록되고 연속 실패 임계값 도달 시 kill switch가 ENTRY_DISABLED로
  자동 상승(ACTIVE일 때만)하며, 이 상승이 CODEX-016의 배선을 통해 실제 주문 차단으로 이어진다.
Implementation: `paper_strategy_order.py`에 `import notification_health` 추가.
  `_safe_send_slack_alert(message)`가 기존처럼 `send_slack_alert()`를 직접 호출하는 대신
  `notification_health.send_with_health_tracking(send_slack_alert, message)`를 호출하도록 변경
  (send_with_health_tracking이 내부에서 성공/실패를 `record_success()`/`record_failure()`로 기록하고
  임계값 도달 시 kill switch를 자동 상승시킴 — `notification_health.py` 자체 로직은 이전 사이클에서
  이미 구현·단위테스트됨, 이번 변경은 운영 호출부 배선). 외부 `try/except`는 이중 안전장치로 유지.
Regression tests: `tests/test_paper_strategy_order_notification_health.py` (6건, `tests/conftest.py`에
  공용 fixture 28줄 추가) — `test_safe_send_slack_alert_records_failure_via_health`/
  `test_safe_send_slack_alert_records_success_via_health`(실제 호출부가 상태 파일에 기록됨을 확인),
  `test_safe_send_slack_alert_swallows_exception_and_records_failure`(예외 발생 시에도 기록되고 호출자에
  전파되지 않음), `test_consecutive_slack_failures_escalate_and_block_buy_order`(연속 실패 임계값 도달 시
  kill switch가 ENTRY_DISABLED로 상승하고 CODEX-016 배선을 통해 실제 매수 주문이 차단됨을 end-to-end로
  확인), `test_recovery_after_failures_restores_healthy`(성공 시 연속 실패 카운트 리셋),
  `test_slack_failure_does_not_change_order_outcome_via_main`(Slack 장애 자체는 정상 주문 흐름을 막지
  않음 — kill switch 상승 전까지는 주문 결과에 영향 없음).
Status: RESOLVED
Commit: `79eaa81`
Remaining risk: 없음. 실제 Slack webhook 호출은 여전히 monkeypatch로 대체(외부 호출 금지 원칙 유지).
```

### [CODEX-018] MEDIUM — 주문 직전 환경 재검증 함수가 선언만 되고 사용되지 않음

```
Finding ID: CODEX-018
Severity: MEDIUM
Reproduced: 예(정적 분석) — validate_order_allowed_now()는 broker_config.py와 테스트에서만 참조되고
  AlpacaBroker._request()/get_order_by_client_order_id()/submit_order()는 생성 시점의 self.config만
  검증함을 확인.
Root cause: BrokerConfig.from_env()로 import-time 고정 문제는 해결됐으나(CODEX-011 사이클 전 항목,
  이전 run), "주문 직전 재검증" 함수 자체가 실제 배선 없이 선언만 되어 있음.
Affected path: broker/alpaca_client.py (AlpacaBroker._request 및 이를 경유하는 모든 메서드)
Required behavior: _request()가 실제 요청 직전에 validate_order_allowed_now()(또는 동등한 최신 환경
  재검증)를 호출해 불안전하면 요청을 보내지 않고 예외를 발생시킨다. 기존 endpoint 검증/Live 차단
  로직은 그대로 유지, 추가로 재검증을 더한다.
Implementation: `broker/alpaca_client.py`(이 항목 범위로만 한시 개방)에서
  `from .broker_config import BrokerConfig, validate_order_allowed_now`로 import 확장.
  `AlpacaBroker._request()`가 기존 `self.config.validate_order_allowed()`/`validate_for_request()`
  (생성 시점 스냅샷 검증) 직후, 실제 `self.session.request()` 호출 바로 직전에
  `validate_order_allowed_now()`를 호출해 `os.environ`을 그 자리에서 다시 읽는다. 모든 메서드가
  `_request()`를 경유하므로 `get_account`/`get_positions`/`submit_order`/
  `get_order_by_client_order_id` 전부 동일하게 재검증을 거친다.
Regression tests: `tests/test_alpaca_client_runtime_revalidation.py` (6건) —
  `test_env_flipped_to_bad_live_endpoint_after_construction_blocks_request`(broker 생성 후 환경변수를
  안전하지 않은 값으로 바꾸면 이후 요청이 차단됨), `test_paper_mode_with_correct_endpoint_proceeds_normally`,
  `test_live_mode_with_arbitrary_endpoint_blocks_request`, `test_get_account_blocked_when_env_flips_before_request`/
  `test_get_positions_blocked_when_env_flips_before_request`(_request()를 경유하는 다른 메서드에도 재검증이
  적용됨을 확인), `test_get_positions_proceeds_normally_when_env_stays_safe`(안전한 환경에서는 기존 동작
  회귀 없음).
Status: RESOLVED
Commit: `00b0f68`
Remaining risk: 없음.
```

### [CODEX-019] MEDIUM — 신규 상태 저장소의 동시 갱신 lost-update 가능성

```
Finding ID: CODEX-019
Severity: MEDIUM
Reproduced: 예(정적 분석) — kill_switch_state.activate()/release()와 notification_health.
  record_success()/record_failure()가 read-modify-write 전체에 파일 잠금을 쓰지 않음을 확인.
  concurrency 회귀 테스트 없음.
Root cause: temp+os.replace로 단일 파일 쓰기 원자성은 확보했으나, 두 프로세스가 동시에 읽고 쓰면
  감사 이력/연속 실패 카운트가 마지막 writer 값으로 덮일 수 있다.
Affected path: kill_switch_state.py, notification_health.py
Required behavior: order_history.csv/order_reconciliation.csv(CODEX-008)와 atomic_io.py가 쓰는 것과
  동일한 fcntl.flock 기반 락을 재사용해, 락 안에서 최신 파일을 재읽기 후 병합·쓰기.
Implementation: `kill_switch_state.py`, `notification_health.py` 양쪽에 동일한 구조로
  `_resolve_lock_path()`(상태 파일 경로에서 `.lock` 확장자로 파생 — 테스트가 `STATE_FILE`을 `tmp_path`로
  monkeypatch하면 락 파일도 자동으로 격리됨)와 `_state_lock(timeout=LOCK_TIMEOUT_SECONDS=5.0)`
  contextmanager(`fcntl.flock(LOCK_EX | LOCK_NB)` 폴링, 0.05s 간격)를 추가.
  `kill_switch_state.activate()`/`release()`는 각각 `path = _resolve_state_path()` 이후
  `with _state_lock(timeout=lock_timeout):` 블록 안에서 재읽기(`_load`)→병합→`_atomic_write`를 수행하고,
  락 획득 실패 시 파일에 아무것도 쓰지 않고 `KillSwitchStateError`를 raise한다(양쪽 다 신규 `lock_timeout`
  키워드 인자 추가, 기본값은 상수와 동일해 기존 호출부 무변경).
  `notification_health.record_success()`/`record_failure()`도 동일한 락 안에서 재읽기→병합→저장하되,
  이 둘은 "절대 raise하지 않는다"는 기존 계약을 유지하기 위해 락 타임아웃을 `RuntimeError`/`OSError`로
  잡아 상태 파일은 손대지 않은 채 마지막으로 영속화된 레코드를 반환하고 `_fallback_log()`에
  `success_lock_timeout`/`failure_lock_timeout` 이벤트를 남긴다.
Regression tests: `tests/test_state_store_concurrency.py` (6건, `multiprocessing` 기반) —
  `test_concurrent_activate_preserves_every_audit_entry`(여러 프로세스가 동시에 `activate()`를 호출해도
  감사 이력(history)의 모든 항목이 유실 없이 보존됨), `test_concurrent_record_failure_preserves_every_increment`
  (동시 `record_failure()` 호출의 `consecutive_failures` 증가분이 하나도 유실되지 않음 — lost-update 재현),
  `test_kill_switch_activate_lock_timeout_leaves_file_unchanged`/
  `test_notification_health_record_failure_lock_timeout_leaves_file_unchanged`(락을 고의로 선점한 상태에서
  타임아웃이 발생해도 상태 파일이 전혀 변경되지 않음을 확인), `test_corrupted_kill_switch_state_file_still_fails_closed`/
  `test_corrupted_notification_health_state_file_does_not_crash`(락 도입 후에도 기존 손상 파일 fail-closed
  동작이 회귀하지 않음).
Status: RESOLVED
Commit: `50a097d`
Remaining risk: 없음. 5초 락 타임아웃은 임의 값이며 실제 동시 접근 빈도에 대한 프로덕션 관측치는 아직
  없음(운영 중 관찰 후 조정 가능).
```

## 요약 (제한적 실거래 검토 사이클, CODEX-016~019)

CRITICAL 0건. HIGH 전부(016/017) RESOLVED. MEDIUM 전부(018/019) RESOLVED. 미해결 Finding 없음. 신규
회귀 테스트 4개 파일 30건(`tests/test_paper_strategy_order_kill_switch_state.py` 12건,
`tests/test_paper_strategy_order_notification_health.py` 6건(+`tests/conftest.py` 공용 fixture),
`tests/test_alpaca_client_runtime_revalidation.py` 6건, `tests/test_state_store_concurrency.py` 6건)
추가. 대상 커밋: `6ad4841`(CODEX-016), `79eaa81`(CODEX-017), `00b0f68`(CODEX-018), `50a097d`(CODEX-019).
전체 회귀 `venv/bin/python -m pytest -q` 417 passed, 0 failed, 2 warnings(기존과 동일한 urllib3/scanner
경고만, 신규 안전 관련 warning 없음). `order_history.csv` 등 운영 파일과 `.env`는 미변경, 실제
Alpaca/Slack/Yahoo 호출 0회(전부 monkeypatch/fake). Claude 자체 판정으로 `VALIDATED` 승격하지 않으며,
run 상태는 **`READY_FOR_CODEX_REVALIDATION`**으로 기록한다 — Codex의 독립 재검증(CODEX-016~019 재확인,
`PROCEED`/`FAIL` 여부) 없이는 제한적 실거래 검토를 재개하지 않는다. 상세 근거는
`docs/autonomous/VALIDATION_PACKAGE.md`의 "CODEX-016~019 수정 완료" 패키지 참고.

---

## CODEX-016·018 최종 보완 사이클

검증 기준: `docs/autonomous/CODEX_REVIEW.md` 커밋 `cf4ada9`. CODEX-017과 CODEX-019는
RESOLVED 상태를 유지하며 변경하지 않았다.

### CODEX-016 — sell side end-to-end 전달 누락 (HIGH)

- 재현: wrapper에 `side="sell"`을 전달해도 fake broker kwargs에 side가 없고 실제
  `AlpacaBroker.submit_order()` 기본값인 buy가 사용됐다.
- 원인: wrapper의 side는 kill-switch 판단에만 쓰이고 broker 호출에 전달되지 않았으며 두
  계층 모두 암묵적 buy 기본값을 제공했다.
- 수정: `paper_strategy_order.submit_order()`와 `AlpacaBroker.submit_order()`의 side를
  keyword-only 필수 인자로 변경했다. 정확한 `buy`/`sell`만 허용하고 None, 빈 문자열,
  대문자, 공백, 오타 및 기타 타입은 broker/HTTP 호출 전에 차단한다. wrapper가 side를
  broker에 명시적으로 전달하고 POST payload가 같은 값을 보존한다.
- 회귀 테스트: side 누락·잘못된 값 차단, buy/sell wrapper kwargs, buy/sell POST payload,
  main의 명시적 buy, sell이 buy로 변환되지 않음을 검증한다.
- 처리 상태: **IMPLEMENTED — READY_FOR_CODEX_REVALIDATION**
- 구현 커밋: `47ee8d6`

### CODEX-018 — POST·reconciliation runtime 재검증 우회 (MEDIUM)

- 재현: safe Paper config로 broker를 생성한 뒤 환경을 unsafe Live로 바꿔도
  `submit_order()`가 직접 `session.post()`를 1회 호출했다.
- 원인: GET 일부만 `_request()`를 사용하고 주문 POST와 client_order_id reconciliation
  조회는 직접 session 메서드를 호출했다.
- 수정: `_validate_runtime_safety()`에서 생성 시점 config와 현재 환경을 모두 검사하고,
  모든 외부 호출을 단일 `_request()`로 통합했다. 주문 POST, reconciliation GET, account,
  positions, recent orders, assets 및 신규 cancel DELETE가 동일한 gate를 사용한다.
- 회귀 테스트: 안전 Paper의 POST/GET/DELETE 허용, 생성 후 env 변경·config 교체·endpoint
  변조·invalid mode에서 session 호출 0회, reconciliation과 cancel 공통 gate를 검증한다.
- 처리 상태: **IMPLEMENTED — READY_FOR_CODEX_REVALIDATION**
- 구현 커밋: `47ee8d6`

### 검증 결과

- 집중 안전 테스트: **188 passed, 0 failed, 1 warning**
- 전체: 저장소 루트/상위 디렉터리, `pytest`/`python -m pytest` 네 조합 모두
  **443 passed, 0 failed, 2 warnings**
- broker 내부 직접 session 호출은 `_request()` 한 곳만 남았다.
- 실제 Alpaca/Slack/Yahoo 호출 0회, 운영 CSV 내용·크기·mtime 불변.
- main 병합, origin push, 운영 배포, 실거래 활성화 없음.

---

## CODEX-020·CODEX-018 잔여분 수정 사이클 (2026-07-24)

검증 기준: `docs/autonomous/CODEX_REVIEW.md`(대상 커밋 `47ee8d6`/`03962d3`/`cf4ada9`, overall
verdict **FAIL**). CODEX-016/017/019는 RESOLVED로 재확인되어 변경하지 않았다. CODEX-018(MEDIUM)이
PARTIALLY_RESOLVED로 남았고, 신규 **CODEX-020(HIGH)**이 제기됐다.

### CODEX-020 — direct broker network boundary가 Kill Switch를 우회함 (HIGH)

- 재현: binary halt와 `ENTRY_DISABLED` 각각에서 `AlpacaBroker.submit_order()`를
  `paper_strategy_order.py` wrapper를 거치지 않고 직접 호출하면 fake session request가 1회
  실제로 나가는 것을 격리 재현으로 확인했다.
- 원인: `broker/alpaca_client.py`가 kill-switch 모듈을 import하거나 검사하지 않았다. wrapper의
  binary/4-state gate는 `paper_strategy_order.submit_order()`에만 있었고 broker를 직접 호출하면
  이 계층을 완전히 우회했다.
- 수정: `AlpacaBroker._request()`에 `order_side` 키워드 전용 필수 인자(주문이 아니면 `None`,
  매수/매도면 `"buy"`/`"sell"`)를 추가했다. 신규 `_check_kill_switch(order_side)`가 매 호출마다
  `kill_switch.is_trading_halted()`(binary)와 `order_side`에 따라
  `kill_switch_state.is_entry_allowed()`(buy)/`is_liquidation_allowed()`(sell)를 다시 조회해,
  불허 시 세션 요청 전에 `RuntimeError`를 발생시킨다. `order_side`가 `None`이면 kill switch 검사
  자체를 건너뛴다. `get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order` 등 조회·취소 경로는 `order_side=None`으로
  명시해 kill switch 정책과 무관하게 계속 동작하도록 분리했다. `order_side`는 기본값이 없으므로
  `_request()`를 우회해 이 인자를 생략하면 네트워크 호출 전에 `TypeError`로 즉시 차단된다.
- 수정 파일: `broker/alpaca_client.py`
- 테스트: `tests/test_broker_kill_switch_gate.py`(신규, 25건) — direct broker 호출이 binary
  halt/4-state(ENTRY_DISABLED·ALL_TRADING_DISABLED·MANUAL_REVIEW) 각각에서 차단되는지, buy/sell
  구분이 정확한지(ENTRY_DISABLED에서 sell/청산은 허용), 조회·취소 경로가 kill switch와 무관하게
  계속 동작하는지, `order_side` 생략 시 `TypeError`가 세션 호출 전에 발생하는지, wrapper 경로와
  direct 경로의 판정이 일치하는지 검증한다.
- 처리 상태: **IMPLEMENTED — READY_FOR_CODEX_REVALIDATION**
- 구현 커밋: `66eda8a`

### CODEX-018 잔여분 — 공통 gate에서 현재 credentials 재검증 누락 (MEDIUM)

- 재현: `_validate_runtime_safety()`가 `validate_order_allowed_now()`(mode/endpoint 재검증)는
  호출하지만, kill switch와 현재 credentials(API key/secret)까지 포함하는 검증 요청 기준에는
  미달했다 — credential이 rotation/삭제된 뒤에도 생성 시점에 캡처된 값으로 계속 요청이 나갔다.
- 원인: 주문 직전 환경 재검증(`validate_order_allowed_now()`)은 CODEX-018 이전 사이클에서 이미
  배선됐지만, credential 자체의 최신성은 검사 대상이 아니었다.
- 수정: `_validate_runtime_safety()`에 `_validate_current_credentials_match_captured()`를
  추가했다. 매 요청마다 `BrokerConfig.from_env()`로 현재 프로세스 환경의 API key/secret을 다시
  읽어, `self.config`가 생성 시점에 캡처한 값과 `hmac.compare_digest()`로 상수시간 비교한다.
  현재 값이 누락/공백이거나, 환경 읽기 자체가 실패하거나, 캡처된 값과 하나라도 다르면 세션 요청
  전에 `RuntimeError`를 발생시킨다. credential 값 자체는 예외 메시지에 포함하지 않는다(존재
  여부/일치 여부만 노출). Credential rotation은 새 `BrokerConfig`/`AlpacaBroker` 인스턴스를
  만드는 방식으로만 가능하며, 이 gate는 기존 인스턴스의 값을 자동으로 재캡처하지 않고 오직
  차단만 한다.
- 수정 파일: `broker/alpaca_client.py`
- 테스트: `tests/test_alpaca_client_runtime_revalidation.py` 확장(44건) — credential
  삭제/회전/공백/환경 읽기 실패 각각을 POST(order)·GET(account/positions/reconciliation)·
  DELETE(cancel) 3개 경로에 파라미터라이즈해 세션 호출 전에 차단되는지 검증. 기존
  `tests/test_broker_safety.py`, `tests/test_universe_builder.py`의 fake broker 호출부를
  `order_side` 키워드 인자에 맞춰 갱신(회귀 아님, 시그니처 변경 반영).
- 처리 상태: **IMPLEMENTED — READY_FOR_CODEX_REVALIDATION**
- 구현 커밋: `ed452da`

### 검증 결과

- 집중 안전 테스트(`test_broker_kill_switch_gate.py` + `test_alpaca_client_runtime_revalidation.py`
  + `test_broker_safety.py` + `test_universe_builder.py` + `test_paper_strategy_order_kill_switch_state.py`
  + `test_paper_order_execution.py`): **208 passed, 1 warning**
- 전체: `venv/bin/python -m pytest -q` **489 passed, 0 failed, 2 warnings**(신규 안전 관련
  warning 없음, 기존 urllib3/scanner 경고만).
- broker 내부 직접 session 호출은 `_request()` 한 곳만 유지, kill switch 검사와 credential
  재검증 모두 이 경로 안에 포함됐다.
- 실제 Alpaca/Slack/Yahoo 호출 0회, `order_history.csv`/`universe.csv` SHA-256 불변, `.env`·kill
  switch/notification 상태 파일 변경 없음.
- main 병합, origin push, 운영 배포, 실거래 활성화 없음.
- 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review: BLOCKED**,
  **Live trading: DO_NOT_ENABLE**을 유지한다.

---

## CODEX-021 해결 및 CODEX-020 잔여분 종결 사이클 (2026-07-25)

검증 기준: `docs/autonomous/CODEX_REVIEW.md`(대상 커밋 `66eda8a`/`ed452da`/`cf5601d`/`edc5ad5`,
overall verdict **FAIL**). CODEX-016/017/018/019는 RESOLVED로 재확인되어 이번 사이클에서 코드를
변경하지 않았다. CODEX-020(HIGH)이 PARTIALLY_RESOLVED로 남았고, 신규 **CODEX-021(HIGH)**이
제기됐다.

### CODEX-021 — order-shaped `_request()`가 explicit `order_side=None`으로 Kill Switch를 우회함 (HIGH)

- 재현: `_request("POST", "/v2/orders", order_side=None, ...)`를 ENTRY_DISABLED와 binary halt
  각각에서 직접 호출하면 `_check_kill_switch(None)`이 HTTP method/path를 확인하지 않고 즉시
  반환해, fake session request가 1회씩 실제로 나가는 것을 격리 재현으로 확인했다.
- 원인: `_request()`는 `order_side` 누락만 Python `TypeError`로 막았고, `_check_kill_switch()`는
  `order_side is None`이면 무조건 반환했다. 이 `None`이 "주문 아님"과 "주문인데 명시를 생략함"
  두 의미를 동시에 가리켜, 후자로 호출해도 전자로 취급됐다. 이전 사이클 검증 패키지가 주장한
  method+path 백스톱은 실제로 구현되지 않았다.
- 수정: `broker/alpaca_client.py`에 신규 `RequestPurpose` enum(`READ_ONLY`/`ENTRY_ORDER`/
  `EXIT_ORDER`/`CANCEL_ORDER`/`RECONCILIATION`)을 도입했다. `_request()`의 `purpose`를 기본값
  없는 keyword-only 필수 인자로 만들고 `isinstance(purpose, RequestPurpose)`를 요구해 `None`을
  포함한 잘못된 값은 `ValueError`로 세션 접근 전에 차단한다. 신규 `_METHOD_PURPOSES` 매트릭스가
  HTTP method(GET/POST/DELETE)와 purpose의 허용 조합을 강제해, 불일치(예: POST가
  `READ_ONLY`를 주장, GET이 `ENTRY_ORDER`를 주장)는 세션 호출 전 `ValueError`. 신규
  `_check_kill_switch(purpose, order_side=None)`는 `purpose`가 `ENTRY_ORDER`/`EXIT_ORDER`일
  때만 binary halt/4-state 정책을 재조회하며, `order_side`는 이제 payload의 `side`와 `purpose`가
  일치하는지 확인하는 2차 방어선일 뿐 단독으로 kill switch를 판단하지 않는다.
- 수정 파일: `broker/alpaca_client.py`
- 테스트: `tests/test_broker_request_purpose.py`(신규, `purpose=None`/누락/잘못된 타입 거부,
  method-purpose 불일치 거부, order payload `side`-`purpose` 불일치 거부, 조회·취소 경로가
  kill switch와 무관하게 계속 동작함을 검증) + `tests/test_broker_kill_switch_gate.py`(기존
  호출부를 `purpose` 키워드로 갱신, `purpose` 누락 시 `TypeError`/`order_side`만 있고 `purpose`가
  없을 때 `TypeError`/`purpose=None` 명시 시 `ValueError` 신규 3건 추가).
- 처리 상태: RESOLVED
- 구현 커밋: `c133e01`

### CODEX-020 잔여분 — method+path 기반 주문 감지 백스톱 부재 (HIGH)

- 재현: CODEX-021과 동일한 재현(위 참고). Codex가 이전 사이클 검증 패키지의 method+path 백스톱
  주장을 재현으로 반증했다.
- 원인: CODEX-021과 동일 — `order_side`만으로는 주문 여부를 신뢰성 있게 판단할 수 없었다.
- 수정: CODEX-021과 동일한 `RequestPurpose`/`_METHOD_PURPOSES` 재설계로 함께 해결됐다(별도
  구현 없음). 조회·취소 경로(`get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order`)는 각각 `RequestPurpose.READ_ONLY`/
  `RECONCILIATION`/`CANCEL_ORDER`를 명시해 kill switch 정책과 무관하게 계속 동작하도록 재확인됐다.
- 수정 파일: `broker/alpaca_client.py`(CODEX-021과 동일 변경)
- 테스트: CODEX-021과 동일(위 참고).
- 처리 상태: RESOLVED
- 구현 커밋: `c133e01`

### CODEX-016~019 — 재작업 아님, 회귀만 확인

이번 사이클은 CODEX-016(다단계 kill switch 배선)·017(Slack health 배선)·018(주문 직전
credential/환경 재검증)·019(상태 저장소 파일 잠금)의 코드를 변경하지 않았다. Codex가 네 항목
모두 RESOLVED로 재확인했으므로, 관련 회귀 테스트만 재실행해 회귀가 없음을 확인했다:
`tests/test_paper_strategy_order_kill_switch_state.py`(12건),
`tests/test_paper_strategy_order_notification_health.py`(6건),
`tests/test_state_store_concurrency.py`(6건), `tests/test_alpaca_client_runtime_revalidation.py`
(44건, `purpose` 키워드 시그니처 변경만 반영, 로직 변경 없음) — 도합 회귀 없음.

### 검증 결과

- 집중 안전 테스트(`test_broker_kill_switch_gate.py` + `test_broker_request_purpose.py`(신규) +
  `test_alpaca_client_runtime_revalidation.py` + `test_broker_safety.py` +
  `test_universe_builder.py` + `test_paper_strategy_order_kill_switch_state.py` +
  `test_paper_order_execution.py`): **255 passed, 1 warning**
- CODEX-016~019 회귀 전용(`test_paper_strategy_order_kill_switch_state.py` +
  `test_paper_strategy_order_notification_health.py` + `test_state_store_concurrency.py`):
  **36 passed, 1 warning**
- 전체: `venv/bin/python -m pytest -q` **536 passed, 0 failed, 2 warnings**(신규 안전 관련
  warning 없음, 기존 urllib3/scanner 경고만).
- broker 내부 직접 session 호출은 `_request()` 한 곳만 유지, purpose 기반 kill switch 검사와
  credential 재검증 모두 이 경로 안에 포함됐다.
- 실제 Alpaca/Slack/Yahoo 호출 0회, `order_history.csv`/`universe.csv`는 이전 사이클 기록값과
  동일(불변), `.env`·kill switch/notification 상태 파일 변경 없음.
- main 병합, origin push, 운영 배포, 실거래 활성화 없음.
- 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review: BLOCKED**,
  **Live trading: DO_NOT_ENABLE**을 유지한다.

---

## CODEX-022 해결 및 CODEX-021 잔여분 종결 사이클 (2026-07-25)

검증 기준: `docs/autonomous/CODEX_REVIEW.md`(대상 커밋 `47ae3ca`/`c133e01`/`cc740a5`, overall
verdict **FAIL**). CODEX-016/017/018/019는 RESOLVED로 재확인되어 이번 사이클에서 코드를 변경하지
않았다. CODEX-021(HIGH)이 PARTIALLY_RESOLVED로 남았고, 신규 **CODEX-022(HIGH)**가 제기됐다.

### CODEX-022 — `_request()`가 purpose/order_side/payload side 3자를 서로 대조하지 않음 (HIGH)

- 재현: `ENTRY_DISABLED` 상태에서 `purpose=EXIT_ORDER`, `order_side="sell"`, JSON `side="buy"`
  (및 `order_side=None`, `order_side="buy"` 변형)로 `_request("POST", "/v2/orders", ...)`를 직접
  호출하면 매번 fake session에 buy payload가 실제로 1회 전달되는 것을 격리 재현으로 확인했다.
- 원인: `RequestPurpose` 재설계(CODEX-021, 커밋 `c133e01`)는 `purpose`를 HTTP method와만
  결합했을 뿐, POST의 실제 의미(주문이 매수인지 매도인지)는 payload의 `side` 필드로 결정되는데
  `_check_kill_switch()`는 `purpose`만 보고 `order_side`와 payload `side`를 서로 대조하지
  않았다. 검증 패키지가 제시한 신규 테스트(`test_post_allows_entry_and_exit_purpose`)조차 두
  purpose 모두 동일한 buy payload를 사용해 이 불일치를 가려버렸다.
- 수정: `broker/alpaca_client.py`에 `_PURPOSE_REQUIRED_SIDE`(`ENTRY_ORDER→"buy"`,
  `EXIT_ORDER→"sell"`) 매핑과 신규 `validate_order_intent(purpose, order_side, payload)`를
  추가했다. `ENTRY_ORDER`/`EXIT_ORDER`는 `order_side`와 `payload["side"]`가 모두 존재하고 정확히
  요구되는 문자열과 완전히 일치해야 한다(`isinstance(..., str)` 검사로 `bool`/`int` 및 대소문자
  ·공백 변형까지 거부). `READ_ONLY`/`RECONCILIATION`/`CANCEL_ORDER`는 반대로 `order_side`와
  payload의 `side`가 둘 다 없어야 한다. `_request()`는 `_validate_runtime_safety()`와
  `_check_kill_switch()`보다도 먼저 이 함수를 호출해, 세 값 중 하나라도 불일치하면 세션 호출이
  0회임을 보장한다. 이로써 CODEX-021의 잔여 위험(2차 방어선인 `order_side`가 payload와
  실제로 대조되지 않던 문제)도 동일 지점에서 함께 닫혔다.
- 수정 파일: `broker/alpaca_client.py`
- 테스트: `tests/test_broker_order_intent_gate.py`(신규, 17건) — CODEX-022가 지적한 3가지 직접
  재현 시나리오(purpose/order_side/payload side 불일치 각 조합) 전부가 세션 호출 0회로
  차단됨을 확인, payload 누락/비-dict/알 수 없는 side 값(대소문자·공백·`True`/`1` 포함)도
  전부 차단, `submit_order()`를 경유한 정상 buy/sell은 세션 호출 1회로 정상 진행됨을 확인.
  `tests/test_broker_request_purpose.py`의 기존 `test_post_allows_entry_and_exit_purpose`를
  ENTRY_ORDER/EXIT_ORDER 각각 실제로 다른(buy/sell) `order_side`+payload를 사용하도록 갱신해
  이전에 두 purpose가 같은 payload로 가려지던 결함을 테스트 자체에서도 제거했다.
- 처리 상태: RESOLVED
- 구현 커밋: `5aac75b`

### CODEX-021 잔여분 — `order_side`가 payload와 대조되지 않던 2차 방어선 공백 (HIGH)

- 재현: CODEX-022와 동일한 재현(위 참고). Codex가 CODEX-021을 PARTIALLY_RESOLVED로 유지한
  근거가 CODEX-022와 동일한 근본 원인이었다.
- 원인: CODEX-022와 동일 — `order_side`는 필수 인자였지만 payload `side`와 비교되지 않아
  "2차 방어선"으로서 실질적인 방어력이 없었다.
- 수정: CODEX-022와 동일한 `validate_order_intent()`로 함께 해결됐다(별도 구현 없음).
- 수정 파일: `broker/alpaca_client.py`(CODEX-022와 동일 변경)
- 테스트: CODEX-022와 동일(위 참고).
- 처리 상태: RESOLVED
- 구현 커밋: `5aac75b`

### CODEX-016~021(CODEX-022 제외분) — 재작업 아님, 회귀만 확인

이번 사이클은 CODEX-016(다단계 kill switch 배선)·017(Slack health 배선)·018(주문 직전
credential/환경 재검증)·019(상태 저장소 파일 잠금)의 코드를 변경하지 않았다. Codex가 네 항목
모두 RESOLVED로 재확인했으므로, 관련 회귀 테스트만 재실행해 회귀가 없음을 확인했다:
`tests/test_paper_strategy_order_kill_switch_state.py`(12건),
`tests/test_paper_strategy_order_notification_health.py`(6건),
`tests/test_state_store_concurrency.py`(6건) — 도합 **36 passed, 1 warning**, 회귀 없음.

### 검증 결과

- 집중 안전 테스트(`test_broker_kill_switch_gate.py` + `test_broker_request_purpose.py` +
  `test_broker_order_intent_gate.py`(신규) + `test_alpaca_client_runtime_revalidation.py` +
  `test_broker_safety.py` + `test_universe_builder.py` +
  `test_paper_strategy_order_kill_switch_state.py` + `test_paper_order_execution.py`):
  **289 passed, 1 warning**
- CODEX-016~019 회귀 전용(`test_paper_strategy_order_kill_switch_state.py` +
  `test_paper_strategy_order_notification_health.py` + `test_state_store_concurrency.py`):
  **36 passed, 1 warning**
- 전체: `venv/bin/python -m pytest -q` **570 passed, 0 failed, 2 warnings**(신규 안전 관련
  warning 없음, 기존 urllib3/scanner 경고만).
- broker 내부 직접 session 호출은 `_request()` 한 곳만 유지, `validate_order_intent()`가
  `_check_kill_switch()`보다 먼저 실행되도록 배선됐다.
- 실제 Alpaca/Slack/Yahoo 호출 0회, `order_history.csv`/`universe.csv`는 이전 사이클 기록값과
  동일(불변, SHA-256 재확인), `.env`·kill switch/notification 상태 파일 변경 없음.
- main 병합, origin push, 운영 배포, 실거래 활성화 없음.

## Codex 최종 독립 재검증 결과 (2026-07-25, 커밋 `a31290b`/`5aac75b`/`8803252` 대상)

Overall verdict **`PASS_WITH_CONDITIONS`**. CODEX-016~022 전부 **RESOLVED**로 최종 확정. 신규
CRITICAL/HIGH/MEDIUM Finding 없음. Limited live review 권고: **`READY_FOR_LIMITED_LIVE_REVIEW`**
(단, **Live trading: DO_NOT_ENABLE`** 유지 — 실거래 활성화를 의미하지 않음). 남은 조건은 코드
Finding이 아니라 운영자 `TBD` 항목(실제 계좌/credential, 현재 포지션·미체결 주문·reconciliation,
허용 종목·거래시간·주문당 절대 한도, 승인자·검토 시각·롤백 담당자)이며,
`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md`에 각 항목의 권장값 초안이 정리되어 있다.
상세는 `docs/autonomous/CODEX_REVIEW.md`(커밋 `d38cb95`에서 그대로 기록) 참고.

## Stage 3~10 통합 수정 사이클 — CODEX-023~027 (2026-07-26)

검증 기준: `CODEX_REVIEW.md`(2026-07-26, 대상 범위 `415c129`~`64a5551`; overall verdict **FAIL**,
Stage 3~10 판정 **KEEP_IN_PROGRESS**). 신규 Finding: CODEX-023(HIGH), CODEX-024(HIGH),
CODEX-025(HIGH), CODEX-026(HIGH), CODEX-027(MEDIUM). 처리 순서: CODEX-027 → 025 → 023/024(통합,
같은 재작성 대상이므로) → 026.

### CODEX-027 — record_fill이 비정상·퇴행 fill을 허용함 (MEDIUM) — RESOLVED
- 재현 여부: 재현됨 — `filled_qty=-3`, `filled_qty=NaN`, `average_fill_price=-5`가 모두 저장 허용,
  더 작은 cumulative quantity 전달 시 기존 fill을 감소시킴.
- 수정: `positions/fill_validation.py` 신설 — `validate_cumulative_fill()`(finite/양수/상한/비퇴행
  강제, bool·문자열 명시 거부), `validate_exit_qty()`/`validate_fill_price()`(청산 경로용).
  `positions/lifecycle.py::record_fill()`이 mutation 전에 검증 호출, 실패 시 레코드 완전 불변.
  동일 cumulative 값 반복 관측은 예외가 아니라 멱등적 no-op으로 처리.
- 테스트: `tests/test_fill_validation.py` 18건 + `tests/test_position_lifecycle.py` 6건.
- 처리 상태: RESOLVED
- 구현 커밋: `0f60ec9`

### CODEX-025 — 손상된 position store가 restart recovery에서 "포지션 없음"으로 보임 (HIGH) — RESOLVED
- 재현 여부: 재현됨 — 손상 JSON에서 `load_position()`은 `RECOVERY_REQUIRED`였지만
  `load_all()`/`load_non_terminal()`/`recover_on_restart()`는 빈 결과를 반환(fail-open 결과).
- 수정: `positions/store.py::load_all()`/`load_non_terminal()`이 전체 파일 손상 시
  `PositionStoreCorruptedError`를 발생시켜(빈 dict 반환 대신) 손상과 "포지션 없음"을 구조적으로
  구분. 신규 `check_store_health()`(MISSING/VALID_EMPTY/VALID_WITH_POSITIONS/CORRUPTED/
  SCHEMA_MISMATCH/READ_FAILURE 분류). `positions/lifecycle.py::recover_on_restart()`가
  `RestartRecoveryResult`(status/positions/reason/broker_positions)를 반환 — bare list와 구조적으로
  구분되어 "STORE_UNAVAILABLE, 결과 없음"이 "정상, 포지션 0개"와 절대 혼동될 수 없음. 손상 감지 시
  Kill Switch를 `MANUAL_REVIEW`로 자동 전환(best-effort), broker 전체 포지션 조회 시도(best-effort),
  손상 파일 자동 초기화 없음(원본 보존). `store.create_position()`은 이미 손상 파일에 쓰기를
  거부하고 있었음을 재확인(신규 진입 차단은 기존에 이미 보장되어 있었음).
- 테스트: `tests/test_position_store.py` 14건(손상/스키마불일치/정상빈파일/권한오류/truncated 파일
  분류 포함) + `tests/test_position_lifecycle.py` 4건(STORE_UNAVAILABLE 결과, kill switch 에스컬레이션,
  broker 포지션 best-effort 조회, 손상 store에서 신규 진입 거부 재확인).
- 처리 상태: RESOLVED
- 구현 커밋: `c5c56c4`

### CODEX-023 — accepted 주문을 체결로 오판하여 조기 CLOSED 처리 (HIGH) — RESOLVED
### CODEX-024 — 청산 timeout 후 durable intent 부재로 중복 sell 가능 (HIGH) — RESOLVED
같은 `positions/lifecycle.py` 청산 경로 재작성으로 함께 처리(분리 시 중간 상태가 서로를 깨뜨림).

- 재현 여부: 둘 다 재현됨 — accepted-but-unfilled 청산이 로컬에서 즉시 `CLOSED`/remaining=0으로
  기록됨. timeout 이후 재실행 시 동일 수량 sell이 중복 제출되고 상태는 `STOP_ACTIVE`로 되돌아감.
- 수정(CODEX-023): `positions/order_status.py` 신설 —
  `classify_broker_order_status()`가 accepted/new/pending_new/pending_replace/pending_cancel/
  calculated/held/suspended를 `NOT_FILLED`로, partially_filled/filled만 실제 체결로 분류(그 외는
  `UNKNOWN`, fail-closed). 청산은 이제 접수 즉시 `EXIT_SUBMITTED`/`PARTIAL_EXIT_SUBMITTED`로
  전환되고 머무르며, 실제 filled/partially_filled 확인 시에만 수량·PnL이 변경된다. 반복 관측은
  CODEX-027과 동일한 단조성 규율로 중복 반영을 차단.
- 수정(CODEX-024): `state_store/exit_intent_ledger.py` 신설(migration 2, `exit_intents` 테이블) —
  broker 호출 **전에** durable exit intent를 SQLite에 원자적으로 예약. `positions/lifecycle.py`의
  `_execute_exit()`가 3단계(예약+상태전환 → broker 호출 → 결과 반영)로 재설계되어, 예약과 상태전환은
  broker 호출 전에 디스크에 커밋됨 — 이전 설계(단일 락 블록 안에서 broker 호출까지 수행)는 예외 발생
  시 예약 자체가 롤백되어 재시도가 사실을 기억하지 못하는 근본 원인이었다. `reconcile_pending_exit()`
  가 재시도/재시작 시의 공통 해소 경로 — 절대 재주문하지 않고 broker를 client_order_id로 조회.
  broker 조회 실패/주문 미확인은 `RECONCILIATION_REQUIRED`로 flag, 자동 재주문 없음.
  `recover_on_restart()`도 pending exit intent가 있는 포지션을 동일 경로로 재조정.
- 테스트: `tests/test_exit_reconciliation.py` 20건(accepted/new/partial/filled 분리, 반복 이벤트
  멱등성, timeout-후-재시도 sell 1회, 동시 청산 sell 1회, stop·target 동시 트리거 sell 1회,
  intent 저장 실패 시 broker 호출 0회, 재시작 reconciliation, broker 미확인/조회실패 시 재주문 0회,
  stale RESERVED intent 자동 재주문 금지 등) + `tests/test_exit_intent_ledger.py` 13건.
  부수적으로 SQLite 첫 사용 동시성 경합(`state_store/db.py::init_db()`)과, 청산 경로의 신규 SQLite
  의존성이 격리되지 않은 테스트 파일에서 실제 저장소 루트 `TRADING_STATE.db`를 생성하던 버그를
  발견·수정(`tests/test_position_lifecycle.py`에 `STATE_STORE_DB_FILE` 격리 추가).
- 처리 상태: RESOLVED (둘 다)
- 구현 커밋: `ee6dae2`

### CODEX-026 — 30,000원과 allow-list가 실제 주문을 제한하지 않음 (HIGH) — RESOLVED
- 재현 여부: 재현됨 — `live_readiness.sizing`/`allowlist`가 `paper_strategy_order.py`/
  `positions/lifecycle.py`/`broker/alpaca_client.py` 어디에서도 import/호출되지 않아 실제 주문
  경계에 아무 효과가 없었음.
- 수정: `live_readiness/order_gateway.py` 신설 — `validate_and_size_live_entry()`가 allow-list,
  최대 동시 포지션, 일일 진입 횟수, 환율 존재·유효성·최신성(naive/stale/미래 타임스탬프 차단),
  가용 현금, `max_order_notional_krw` 상한(사전에 예산을 캡핑해 sizing 결과가 구조적으로 상한을
  넘을 수 없도록 함), 소수점 정책, 손절 기준 위험금액의 `max_daily_loss_krw` 상한을 전부
  fail-closed로 검증. `paper_strategy_order.submit_order()`에 배선하되 **side="buy" AND
  broker.config.is_live_mode일 때만** 활성화(Paper 거래는 전혀 영향받지 않음, 청산은 항상 게이트
  없음 — 기존 kill_switch_state의 ACTIVE/ENTRY_DISABLED 비대칭과 동일한 설계 원칙). 이 범위 결정은
  `DECISION_LOG.md`에 기록. `paper_strategy_order.submit_order()`를 우회해 `broker.submit_order()`를
  직접 호출하는 경로는 이 Python 레벨 게이트의 적용을 받지 않음 — 이 저장소 내 어떤 진입 경로도
  현재 그렇게 하지 않는다는 점과 함께 잔여 범위로 명시.
- 부수 버그 발견·수정: `getattr(broker.config, "is_live_mode", False)`가 `broker.config`를 먼저
  즉시 평가해, `.config` 속성이 아예 없는 테스트 더블(기존 테스트 전반의 FakeBroker 대다수)에서
  `AttributeError`를 유발 — `getattr(broker, "config", None)`으로 먼저 broker 자체를 안전하게 조회
  하도록 수정.
- 테스트: `tests/test_live_order_gateway.py` 25건(모든 차단 사유 단위 테스트 + 실제 `AlpacaBroker`와
  `.request()` 호출 시 예외를 던지는 세션 더블을 사용한 통합 테스트로 "차단된 live 진입은 실제
  네트워크 호출 0회"를 증명, sell은 절대 게이트되지 않음 확인, Paper 모드는 기존 경로 그대로 도달함
  확인, `.config` 없는 broker 더블 회귀 테스트 포함).
- 처리 상태: RESOLVED
- 구현 커밋: `f482e90`

### 검증 결과 (CODEX-023~027 전체)

- 전체 회귀: `venv/bin/python -m pytest -q` **923 passed, 0 failed, 2 warnings**(신규 안전 관련
  warning 없음, 기존 urllib3/scanner 경고만). Stage 3~10 착수 전 기준선 613 passed 대비, 이번
  Stage 3~10 + CODEX-023~027 전체로 310건 신규.
- 실제 Alpaca/Slack/Yahoo 호출 0회. 실제 저장소 루트 `TRADING_STATE.db`가 테스트 중 생성되지 않음을
  전용 테스트로 재확인(발견된 버그 수정 후).
- `order_history.csv`/`universe.csv`/`strategy_performance.csv`는 이전 사이클 기록값과 동일(md5
  재확인), `.env`·kill switch/notification 상태 파일 변경 없음.
- main 병합, origin push, 운영 배포, 실거래 활성화, `approved`/`live_enabled` 변경 없음.
- 기존 리스크 한도(`risk_config.py`, `order_safety.py`)를 완화한 곳 없음 — 이번 사이클은 오직
  새로운 fail-closed 검증을 추가했을 뿐, 기존 한도값을 낮추거나 우회 경로를 넓힌 곳이 없다.

## Stage 3~10 최종 재수정 사이클 — CODEX-024/026/028/029/030 (2026-07-26)

Codex 통합 재검증(`CODEX_REVIEW.md`, 대상 커밋 `4de0714`/`e49753f`, overall verdict **FAIL**)이
이전 사이클의 CODEX-024/026을 `PARTIALLY_RESOLVED`로, CODEX-023/025/027을 `RESOLVED`로 재확인하고,
신규 HIGH 2건(CODEX-028, CODEX-029) + MEDIUM 1건(CODEX-030)을 제기했다. 이번 사이클은
CODEX-024/026/028/029/030 5건만 수정하고, RESOLVED로 재확인된 CODEX-023/025/027은 회귀 테스트만
재실행했다.

### CODEX-030(MEDIUM, wall-clock 의존 테스트)

- 재현: `positions/lifecycle.py::check_and_manage()`가 `now=None`이면 실제 `eastern_now()`(진짜
  시스템 시각)를 쓰는데, `tests/test_position_lifecycle.py`의 여러 테스트가 `now`를 명시적으로
  넘기지 않아 실제 미국 동부 장 마감 근처 시각에 실행되면 EOD 강제 청산 우선순위 규칙에 의해
  target/stop/no-action 검증이 `EOD_FORCED_CLOSE`로 바뀌어 실패했다(4건).
- 수정: `clock.py` 신설(`Clock`/`ProductionClock`/`FrozenClock`). `check_and_manage()`/
  `check_invalidation()`이 `now`/`clock` 파라미터를 명시적으로 받고, naive datetime은 즉시
  거부한다. 프로덕션 기본값(`clock.DEFAULT_CLOCK`)은 실제 시스템 시각으로 이전과 동일하게
  동작 — 실제 버그는 테스트 쪽에 있었으므로, `tests/test_position_lifecycle.py`/
  `tests/test_exit_reconciliation.py`의 모든 `check_and_manage()`/`check_invalidation()` 호출에
  고정된 `MID_SESSION_NOW`(2026-07-15 11:00 ET, 평일·비휴장일·DST 적용 중)를 명시적으로 전달하도록
  변경했다.
- 신규 테스트: `tests/test_clock.py` 23건 — Clock 프로토콜 단위 테스트(ProductionClock/FrozenClock/
  naive 거부) + 정규장/EOD 직전/EOD 정확히/EOD 이후/프리마켓/휴장일/DST 시작·종료/UTC-ET 날짜
  경계/반복 실행 동일성/실제 시스템 시각 무관(poisoned `market_hours.eastern_now` 주입으로 증명)/
  기존 실패 4건(target/stop/no-action) 고정 재현.
- 처리 상태: RESOLVED
- 구현 커밋: `f04a123`

### CODEX-028(HIGH, SQLite/JSON commit 불일치) + CODEX-024 잔여분(단일 트랜잭션 아님)

- 재현: `positions/lifecycle.py::_apply_exit_fill_progress()`가 SQLite `exit_intents`(즉시 커밋)와
  JSON position(별도 파일 쓰기)을 서로 다른 시점에 커밋해, partial fill 4주 확정 후 JSON 쓰기만
  실패하면 SQLite intent의 `confirmed_filled_qty`만 앞서가고, 이후 cumulative 10주 이벤트가 delta
  6만 반영해 `state=CLOSED`인데 `remaining_qty=4`인 모순 상태가 영속화됐다.
  - CODEX-024 잔여분: "SQLite intent와 JSON position은 단일 트랜잭션이 아니다."
- 수정: `positions/store.py`를 재작성해 SQLite(`positions`/`position_events` 테이블, 이미 Stage 5
  스키마에 존재했으나 미사용)를 유일한 canonical 저장소로 삼았다. `POSITION_STORE.json`은 SQLite
  커밋 **이후에만** 쓰는 best-effort projection(`positions.projection_status` 컬럼으로 성공/실패
  기록, `store.regenerate_projection()`으로 언제든 재생성 가능)이 됐다. `positions.store.
  locked_position()`이 `conn` 파라미터를 받아 `positions/lifecycle.py`의 exit-intent
  예약/재조정 호출과 **같은 SQLite 트랜잭션**을 공유하도록 재배선했고(`state_store/
  exit_intent_ledger.py`의 각 mutation 함수에 `commit=False` 옵션 추가), 이로써 CODEX-024
  잔여분도 함께 해소됐다. CODEX-025의 손상 감지 의미론은 SQLite 파일 대상으로 이식했다(JSON
  projection 단독 손상은 더 이상 store corruption이 아님).
- 신규/이관 테스트: `tests/test_position_store.py`(CODEX-025 테스트 SQLite 대상으로 전면 이식 +
  SQLite-succeeds-JSON-fails/DB-commit-failure-rollback/projection-regenerate 등 CODEX-028
  전용 신규), `tests/test_exit_reconciliation.py`에 partial4→cumulative10→remaining0/CLOSED/전체
  PnL, delta 4/3/3 순차 반영, out-of-order regression 차단, JSON 손상 중 청산 흐름 무영향,
  반복/동시 reconciliation 멱등성 추가.
- 부수 발견: `tests/test_position_store.py`/`tests/test_ops_dashboard.py`가 `POSITION_STORE_FILE`만
  격리하고 `STATE_STORE_DB_FILE`은 격리하지 않아, SQLite가 canonical이 된 이후 실제 저장소 루트
  `TRADING_STATE.db`에 테스트 포지션을 쓰고 있었다 — 즉시 발견해 격리를 추가하고 생성된 stray
  파일(gitignored, 커밋되지 않음)을 삭제했다.
- 처리 상태: RESOLVED
- 구현 커밋: `09b9237`

### CODEX-029(HIGH, live context symbol과 실제 주문 symbol 불일치) + CODEX-026 잔여분(direct broker 우회)

- 재현: `validate_and_size_live_entry(ctx)`가 `ctx.symbol`만 allow-list와 대조하고 실제
  `submit_order(symbol)` 인자와 비교하지 않아, `ctx.symbol="AAPL"`로 승인받고 실제로는
  `symbol="TSLA"`를 제출해도 통과했다.
  - CODEX-026 잔여분: `AlpacaBroker.submit_order()`를 직접 호출하면 게이트를 전혀 거치지 않음.
- 수정: `live_readiness/order_gateway.py::validate_and_size_live_entry(ctx, order_symbol)`에
  `order_symbol` 필수 인자를 추가하고, `ctx.symbol`과의 완전 일치(대소문자/공백 정규화 없음)를
  최우선으로 검사한다. `broker/alpaca_client.py::AlpacaBroker.submit_order()`가 동일 게이트를
  자체적으로 실행하도록 배선해(`side="buy" AND is_live_mode`에만 적용, 범위는 CODEX-026과 동일)
  direct broker 호출도 더 이상 우회할 수 없다. `paper_strategy_order.submit_order()`는 자체
  게이트를 유지(방어 심층화 + AlpacaBroker가 아닌 테스트 더블 보호)하고 `live_entry_context`를
  broker 호출로 전달하도록 갱신했다.
- 신규 테스트: `tests/test_live_order_gateway.py`에 symbol 불일치/대소문자·공백 변형/빈 문자열/
  None 차단, direct broker 호출(context 없음/allow-list 불일치/symbol 불일치/stale FX/유효
  전량일치) 각각 세션 호출 0회 검증, 30,000원 정확한 경계 테스트 추가.
- 부수 수정: 기존 안전 테스트(`test_broker_safety.py`, `test_paper_order_execution.py`)의
  `broker.submit_order(side="buy")` 직접 호출부가 이제 broker-level 게이트를 통과해야 하므로
  최소 유효 `LiveEntryContext`를 추가로 전달하도록 갱신 — 원래 검증하려던 "실거래는 항상 비활성화"
  주장 자체는 변경 없음.
- 처리 상태: RESOLVED
- 구현 커밋: `b78e444`

### 부수 발견: `_execute_exit()` 동시성 경쟁 조건 (finding 목록에 없었으나 발견 즉시 수정)

- 전체 회귀 실행 중 1회, `test_concurrent_exit_attempts_submit_broker_sell_exactly_once`가
  `InvalidTransitionError("CLOSED -> EXIT_SUBMITTED")`로 실패. lock 없이 읽는
  `eil.get_active_intent()` 스냅샷이 실제 lock 획득 시점에는 이미 CLOSED로 해소된 경우를
  처리하지 못하던 기존(CODEX-024 사이클부터 존재) 경쟁 조건.
- 수정: lock 아래에서 다시 읽은 실제 상태만으로 전이 여부를 결정하도록 `positions/
  lifecycle.py::_execute_exit()`의 `existing_intent` 분기를 재작성. 결정적 재현 테스트
  (`test_stale_existing_intent_read_after_position_already_closed_does_not_raise`) 추가,
  동시성 테스트 20회 반복 실행으로 안정성 확인.
- 처리 상태: RESOLVED(CODEX-029 커밋 `b78e444`에 포함)

### 검증 결과 (CODEX-024/026/028/029/030 전체)

- 전체 회귀: `venv/bin/python -m pytest -q` / `venv/bin/pytest -q` / 상위 디렉터리에서
  `python -m pytest us-stock-trading -q` 세 가지 실행 형태 모두 **973 passed, 0 failed,
  2 warnings**(신규 안전 관련 warning 없음). 이전 사이클 종료 시점 923 passed 대비 50건 신규
  (CODEX-030 24건 + CODEX-028 다수 + CODEX-029 다수 + 기존 CODEX-025 테스트 SQLite 이식분 포함).
- 실제 Alpaca/Slack/Yahoo 호출 0회(모든 direct-broker 테스트가 `_NetworkForbiddenSession`으로
  세션 호출 0회를 직접 검증). 실제 저장소 루트 `TRADING_STATE.db*`가 두 차례 전체 회귀 실행
  전후 존재하지 않음을 확인.
- `order_history.csv`/`universe.csv`/`strategy_performance.csv`는 이전 사이클 기록값과 동일(md5
  재확인), `.env`·kill switch/notification 상태 파일 변경 없음.
- `git diff --check`(`415c129^..HEAD`) 통과 — whitespace 오류 없음.
- main 병합, origin push, 운영 배포, 실거래 활성화, `approved`/`live_enabled` 변경 없음.
- 기존 리스크 한도(`risk_config.py`, `order_safety.py`)를 완화한 곳 없음.

## Stage 3~10 최종 통합 수정 사이클 — CODEX-024/026/028/031/032/033 (2026-07-26)

Codex 통합 재검증(`CODEX_REVIEW.md`, 대상 커밋 `f04a123`/`aee663c`/`09b9237`/`b78e444`/`fe3e9b7`,
overall verdict **FAIL**)이 CODEX-029/030을 `RESOLVED`로 재확인하고, CODEX-024/026/028을
`PARTIALLY_RESOLVED`로, 신규 HIGH 2건(CODEX-031, CODEX-032) + MEDIUM 1건(CODEX-033)을 제기했다.
이번 사이클은 CODEX-024/026/028/031/032/033만 수정하고, RESOLVED로 재확인된 CODEX-029/030은
회귀 테스트만 재실행했다.

### CODEX-032(HIGH, rejected exit의 intent/position 비원자적 갱신) + CODEX-024/028 잔여분

- 재현: `_execute_exit()`의 broker 명시적 rejection 경로가 `eil.mark_aborted(conn, intent_id)`를
  독립 커밋(default commit=True)한 뒤, 별도의 `store.locked_position(conn=conn)` 트랜잭션에서
  position을 `MANUAL_REVIEW`로 전이했다. fault-injection으로 두 번째 write만 실패시키면 intent는
  terminal `ABORTED`, position은 `EXIT_SUBMITTED`에 영구 고정되고, `recover_on_restart()`도 active
  intent가 없어 이를 재조정하지 못했다.
- 수정: `eil.mark_aborted(conn, intent_id, commit=False)`를 `store.locked_position(conn=conn)`
  블록 안으로 이동해 position의 `MANUAL_REVIEW` 전이와 같은 SQLite 트랜잭션에서 커밋되도록 재작성.
- 신규 테스트: `tests/test_exit_reconciliation.py`에 정상 원자적 커밋 확인, position write 실패 시
  intent도 함께 롤백, intent write 실패 시 position도 변경되지 않음, 롤백 후 재시도가 안전하게
  `RECONCILIATION_REQUIRED`로 귀결(맹목적 재제출 없음) 4건 추가.
- 처리 상태: RESOLVED
- 구현 커밋: `55f3806`

### CODEX-031(HIGH, 30K/count/pending 제한이 caller 선언에 의존) + CODEX-026 잔여분

- 재현: `LiveEntryContext`의 `max_order_notional_krw`/`available_cash_krw`/`max_daily_loss_krw`/
  `max_position_count`/`current_open_position_count`/`max_daily_entries`/`today_entry_count`가
  전부 caller 입력이었다. context를 각각 300만원으로 설정하면 2,997,000원 주문이 승인됐다.
- 수정: `live_readiness/entry_reservation_ledger.py` 신설(SQLite migration 4,
  `live_entry_reservations` 테이블) — 모든 live 진입 시도가 broker 호출 전에 예산을 durable하게
  예약한다. `live_readiness/order_gateway.py`가 caller 입력을 신뢰하는 대신
  `entry_reservation_ledger.build_snapshot()`에서 산출한 authoritative 예산/카운트를 사용하고,
  신뢰 가능한 코드 상수(`PILOT_TOTAL_BUDGET_KRW=30_000`, `MAX_CONCURRENT_LIVE_POSITIONS=1`,
  `MAX_DAILY_LIVE_ENTRIES=2`)와 caller 값을 `min()`으로 교차해 caller가 상한을 완화할 수 없게
  했다. 30,000원 예산은 파일럿 전체 누적 배분(포지션 종료로 반환되지 않음), 동시 포지션 수는
  canonical `positions` 테이블과 조인해 실제 종료 여부를 반영(결정 3 참고). 스냅샷 읽기부터
  예약까지 전체를 `reservation_lock()`으로 원자화해 동시 진입 두 건이 각각 사전 검사를 통과해
  합계 한도를 넘는 경쟁 조건을 차단했다. `validate_and_size_live_entry()`는 이제
  `LiveEntryApproval(quantity, reservation_id)`을 반환하며, 두 제출 경로(`paper_strategy_order.
  submit_order()`/`AlpacaBroker.submit_order()`) 모두 broker 응답에 따라 예약을 commit/release한다.
  `AlpacaBroker` 인스턴스에 대해서는 wrapper가 자신의 게이트를 건너뛰어 이중 예약을 방지한다
  (`DECISION_LOG.md` 결정 4).
- 신규 테스트: `tests/test_live_order_gateway.py`를 authoritative 모델 기준으로 전면 재작성 —
  caller 인플레이션 무시, RESERVED/COMMITTED 예산 합산, RELEASED 예산 제외, 종료된 position의
  예약이 카운트에서 제외(예산은 유지), 동시 진입 2건 중 1건만 승인(스레드 테스트), 30,000원 정확한
  경계/30,001원 차단.
- 부수 발견: `tests/test_broker_safety.py`/`tests/test_paper_order_execution.py`가 CODEX-026
  사이클에서 이미 `LiveEntryContext`를 사용하고 있었으나 `STATE_STORE_DB_FILE`을 격리하지 않아,
  이번 사이클에서 게이트가 SQLite에 실제로 쓰기 시작하자 실제 저장소 루트 `TRADING_STATE.db`에
  기록하고 있던 것을 발견해 즉시 격리를 추가했다.
- 처리 상태: RESOLVED
- 구현 커밋: `8a3be50`

### CODEX-033(MEDIUM, governance 문서 불일치)

- 재현: `docs/live_review/LIMITED_LIVE_REVIEW_CHECKLIST.md` §8이 CODEX-016~022의
  `PASS_WITH_CONDITIONS`만을 근거로 `READY_FOR_LIMITED_LIVE_REVIEW`를 유지하고 있었으나, 그 이후
  Stage 3~10에 대한 반복적인 Codex `FAIL` 판정(같은 문서 §1.5/§1.6에는 정확히 기록됨)을 §8에는
  반영하지 않아 `FINAL_VALIDATION_PACKAGE.md`/`CURRENT_STATUS.md`의 `BLOCKED`/`KEEP_IN_PROGRESS`
  판정과 모순됐다.
- 수정: §8 최종 상태를 `BLOCKED`로 되돌리고, CODEX-016~022의 `PASS_WITH_CONDITIONS` 자체는
  여전히 유효하며 `BLOCKED`의 원인이 그 이후 Stage 3~10에서 발견된 별개 Finding임을 명시.
  `FINAL_VALIDATION_PACKAGE.md`를 최신 검증 상태의 단일 진실 공급원으로 문서에 명시.
- 처리 상태: RESOLVED
- 구현 커밋: `9c43862`

### 검증 결과 (CODEX-024/026/028/031/032/033 전체)

- 전체 회귀: `venv/bin/python -m pytest -q` / `venv/bin/pytest -q` / 상위 디렉터리에서
  `python -m pytest us-stock-trading -q` / `pytest us-stock-trading -q` 네 가지 실행 형태 모두
  **986 passed, 0 failed, 2 warnings**. 이전 사이클 종료 시점 973 passed 대비 13건 신규.
- 실제 Alpaca/Slack/Yahoo 호출 0회. 실제 저장소 루트 `TRADING_STATE.db*`/`LIVE_ENTRY_RESERVATION.lock`
  이 두 차례 전체 회귀 실행 전후 존재하지 않음을 확인.
- `order_history.csv`/`universe.csv`/`strategy_performance.csv`는 이전 사이클 기록값과 동일(md5
  재확인), `.env`·kill switch/notification 상태 파일 변경 없음.
- `git diff --check`(`415c129^..HEAD`) 통과.
- main 병합, origin push, 운영 배포, 실거래 활성화, `approved`/`live_enabled` 변경 없음.
- 기존 리스크 한도 완화 없음 — 이번 사이클은 기존 한도를 오히려 더 엄격하게(caller가 완화할 수
  없도록) 만들었을 뿐이다.

## CODEX-034 + 잔고 비율 기반 주문 사이징 사이클 (2026-07-27)

### CODEX-034(HIGH, broker 응답 유실 시 reservation 해제로 중복 주문/예산 우회 허용)

- 재현(`CODEX_REVIEW.md` 기록): AAPL 27,000원 live entry에서 첫 broker 세션 호출이 timeout →
  `_release_live_entry_reservation()`이 즉시 호출됨 → 재시도가 성공 → 세션 호출 총 2회, 실제
  노출 최대 54,000원/2 포지션이 가능하지만 authoritative 스냅샷은 27,000원/1 포지션만 반영.
- 수정: `state_store/schema.py`/`migrations.py`에 migration 5 — `live_entry_reservations`에
  `client_order_id`(UNIQUE) 컬럼 추가. `entry_reservation_ledger.py`에 `STATE_SUBMISSION_UNKNOWN`
  신설, `reserve()`가 `client_order_id`를 필수 인자로 요구, `mark_submission_unknown()`/
  `reconcile_by_client_order_id()`(broker에 client_order_id로 재조회해 최종 상태 확정) 신규.
  `build_snapshot()`이 `unknown_submission_reservations_krw`를 별도 항목으로 집계해 예산 계산에서
  계속 차감 상태로 유지.
- `broker/alpaca_client.py::AlpacaBroker.submit_order()`: 기존 중첩 try/except를 단일 flat
  try/except로 재작성(중첩 구조는 `SUBMISSION_UNKNOWN`이 non-terminal이라 외부 핸들러가 재차
  `mark_released()`를 호출해 상태를 되돌릴 수 있는 설계 결함이 있었음 — 구현 중 자체 코드 리뷰로
  발견, 테스트 실패로 발견된 것 아님). `_is_ambiguous_broker_failure(exc)`가
  `requests.exceptions.HTTPError`(`.response` 있음)/사전-네트워크 예외는 definitive(release),
  `requests.exceptions.RequestException`(`.response` 없음)은 ambiguous(SUBMISSION_UNKNOWN)로 분류.
  `paper_strategy_order.py`도 동일 로직의 별도 헬퍼로 동일하게 처리(모듈 최상단에 `requests` 의존을
  추가하지 않기 위해 로컬 import).
- 재시도 차단 메커니즘: 별도의 "동일 의도 식별" 중복 감지 시스템을 만들지 않고,
  `SUBMISSION_UNKNOWN`이 `unknown_submission_reservations_krw`에 계속 집계되는 것만으로 동일
  심볼/유사 크기의 재시도가 `available_for_new_order_krw <= 0`에서 자연히 차단되도록 설계를
  단순화했다(Codex가 요구한 "broker submit 총 1회" 테스트와 일치).
- 신규 테스트: `tests/test_live_order_gateway.py`에
  `test_ambiguous_broker_failure_marks_submission_unknown_not_released` — timeout 후 예약이
  `SUBMISSION_UNKNOWN`으로 남고, 동일 조건 재시도가 broker 세션 호출 없이(session.calls==1 유지)
  423으로 차단됨을 검증.
- 처리 상태: RESOLVED

### 잔고 비율 기반 주문 사이징 (사용자 지시, 고정 30,000원 예산 제거)

- 수정: `live_readiness/order_gateway.py`에서 `PILOT_TOTAL_BUDGET_KRW=30_000` 상수를 완전히
  삭제. `LiveEntryContext`에 `available_cash_krw`/`cash_usage_percent`(1~100, NaN/Infinity/bool/
  문자열/None 차단)/`cash_as_of`(FX rate와 동일한 staleness 검증) 신설.
  `max_allocatable_cash = available_cash_krw × cash_usage_percent/100` →
  `available_for_new_order = max_allocatable_cash - pending_buy_reservations -
  unknown_submission_reservations - current_open_position_cost`(전부
  `entry_reservation_ledger.build_snapshot()`의 SQLite 집계, caller 선언 아님). margin/leverage는
  전혀 사용하지 않음 — `available_cash_krw` 하나만이 기준.
- 최종 수량: `actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)`. 이전 설계는
  손절 위험이 한도를 넘으면 주문 전체를 거부했으나, 사용자가 명시한 흐름("리스크 기준 수량과 잔고
  기준 수량 중 작은 값 선택")에 맞춰 거부 대신 수량 축소로 변경. `max_risk_per_trade_krw`/
  `strategy_max_quantity` 신규 optional 필드(미지정 시 무제한, 기존 caller 동작 불변 — 회귀
  테스트에서 이전 71건이 로직 변경 후에도 동일하게 통과함을 확인).
- 신규 테스트: `tests/test_live_order_gateway.py`를 새 모델 기준으로 전면 재작성(78건) —
  100%/90% 경계, 잔고 변경 즉시 반영, pending/unknown/open-position 차감, cash 조회 실패/stale/
  NaN/Infinity/음수/0 차단, `cash_usage_percent` 잘못된 값(0/음수/101/None/문자열/NaN/Infinity/
  bool) 차단, 동시 진입 원자성, 위험/전략 캡 축소 및 0 이하로 축소 시 차단, 재사이징된 실제 수량이
  reservation notional에 반영됨. `tests/test_broker_safety.py`/`test_paper_order_execution.py`의
  `_live_entry_context()` 헬퍼를 새 필드셋으로 갱신.
- 처리 상태: RESOLVED

### 관심종목 잔고 기준 매수 가능 종목 필터 (신규 building block)

- 신설: `live_readiness/watchlist_affordability.py` — 순수 계산 모듈(SQLite/네트워크/파일 I/O
  없음). `AccountState`(스캔당 1회 계산, 모든 후보가 공유)와 `WatchlistCandidate`
  (symbol/latest_price/estimated_entry_price/fractionable/minimum_order_amount/slippage)를 입력받아
  `AffordabilityResult`(6개 상태: `AFFORDABLE_WHOLE_SHARE`/`AFFORDABLE_FRACTIONAL`/
  `INSUFFICIENT_BALANCE`/`NOT_FRACTIONABLE`/`BELOW_MINIMUM_ORDER`/`UNKNOWN_ACCOUNT_STATE`)를 반환.
  `fractionable=true` 종목은 1주 가격이 잔고를 초과해도 최소주문금액을 충족하면 후보로 유지
  (명시적 요구사항 — 절대 단순 가격 비교만으로 배제하지 않음). 수량 계산은 기존
  `live_readiness/sizing.py::calculate_micro_order_quantity()`를 재사용(fractionable=true →
  `budget/price`, false → `floor(budget/price)`, 최소주문금액 미달 시 별도 상태로 구분).
- 배선 범위 결정: `daily_candidate_scanner.py`/`scalping_watchlist/pipeline.py`에는 아직 배선하지
  않음(Stage 10 선례와 동일하게 building block으로 보류, `DECISION_LOG.md` 결정 6 참고).
- 신규 테스트: `tests/test_watchlist_affordability.py`(30건) — account-state 검증(cash/percent/fx
  각각 invalid 값), 100%/90% 공식, pending/unknown/open exposure 차감, fractionable 고가 종목 후보
  유지, non-fractionable 고가 종목 제외, 최소주문금액 미달(whole/fractional 양쪽) 제외, slippage가
  수량에 반영, `filter_watchlist()`가 순서 보존 및 전체 결과(제외 사유 포함) 반환.
- 처리 상태: RESOLVED

### 검증 결과 (CODEX-034 + 잔고 비율 사이징 전체)

- 전체 회귀: `venv/bin/python -m pytest -q` **1,044 passed, 0 failed, 2 warnings**. 이전 사이클
  종료 시점 986 passed 대비 58건 신규(CODEX-034/사이징 78건 + watchlist affordability 30건 −
  기존 `test_live_order_gateway.py`의 이전 버전 건수 차이 반영).
- 실제 Alpaca/Slack/Yahoo 호출 0회. 실제 저장소 루트 `TRADING_STATE.db*`/
  `LIVE_ENTRY_RESERVATION.lock`이 회귀 실행 전후 존재하지 않음을 확인.
- `order_history.csv`/`universe.csv`/`strategy_performance.csv` 변경 없음, `.env`·kill switch/
  notification 상태 파일 변경 없음.
- `git diff --check` 통과.
- main 병합, origin push, 운영 배포, 실거래 활성화, `approved`/`live_enabled` 변경 없음.
- 기존 리스크 한도 완화 없음 — `MAX_CONCURRENT_LIVE_POSITIONS`/`MAX_DAILY_LIVE_ENTRIES`는 변경하지
  않았고, 고정 30,000원 상수 제거는 사용자가 명시적으로 지시한 정책 변경(예시 값 → 잔고 비율
  모델)이지 임의의 한도 완화가 아니다 — `cash_usage_percent`가 100이어도 실제 계좌 현금을 초과할
  수 없다는 제약은 그대로 유지된다.

## CODEX-034~038 최종 수정 사이클 (2026-07-27)

### CODEX-034(PARTIALLY_RESOLVED → 잔여 분류 결함, Codex 4차 검증)

- 재현(`CODEX_REVIEW.md` 기록): 모든 `HTTPError`가 response를 가졌다는 이유만으로 definitive
  rejection으로 분류됨. HTTP 500 fault injection에서 첫 27,000원 reservation이 `RELEASED`되고
  두 번째 27,000원 주문이 실제 session에 도달(broker call 2회).
- 처리 상태: CODEX-035로 전환/해결(아래).

### CODEX-035(HIGH, HTTP 5xx/ambiguous HTTP response를 definitive rejection으로 오분류)

- 수정: `broker/alpaca_client.py::_is_ambiguous_broker_failure()`와
  `paper_strategy_order.py::_is_ambiguous_wrapper_broker_failure()`를 "response 유무"가 아니라
  "definitive rejection status code allowlist(400/401/403/404/409/410/422) + 파싱 가능한 JSON
  body" 기준으로 재작성. allowlist에 없는 코드(408/425/429/5xx/미인식 코드) 또는 body 파싱 실패는
  전부 ambiguous(SUBMISSION_UNKNOWN) 기본값.
- 신규 테스트: `tests/test_live_order_gateway.py`에 HTTP 408/425/429/500/502/503/504
  fault-injection(각각 ambiguous 확인 + 동일 조건 재시도가 session 호출 0회로 차단), 미인식
  status code(418) ambiguous 확인, definitive 코드 + 파싱 불가 body ambiguous 확인,
  definitive allowlist 코드(400/401/403/404/409/410/422) + 파싱 가능 body가 실제로 `RELEASED`되고
  재시도가 broker에 도달함을 확인.
- 처리 상태: RESOLVED

### CODEX-036(HIGH, available cash와 cash usage percent가 caller assertion에 의존)

- 재현(`CODEX_REVIEW.md` 기록): 실제 계좌 30,000원 가정에서 caller가 `available_cash_krw=
  3,000,000`/`cash_usage_percent=100`을 선언하면 2,997,000원 주문이 승인됨. broker account/cash
  endpoint 조회 0회.
- 수정: `live_readiness/account_cash.py` 신설.
  `TRUSTED_CASH_USAGE_PERCENT_CEILING=50`(신뢰 가능한 코드 상수, `MAX_CONCURRENT_LIVE_POSITIONS`/
  `MAX_DAILY_LIVE_ENTRIES`와 동일 패턴)이 `cash_usage_percent`를 항상 caller-untightenable하게
  제한한다. `AccountCashSnapshot`/`fetch_account_cash_snapshot(broker, fx_rate)`가 유일한 잔고
  스냅샷 생성 경로(`broker.get_account()` 기반). `validate_and_size_live_entry()`가 신규 optional
  `account_cash_snapshot` 인자를 받아 `min(caller 선언값, 실제 스냅샷)`으로 caller가 잔고를 부풀릴
  수 없게 한다. `AccountCashSnapshot`이 아닌 타입이 전달되면 즉시 차단, snapshot staleness도 검증.
- 설계 결정(`DECISION_LOG.md` 결정 2 참고): `AlpacaBroker.submit_order()` 내부에서 매 호출 자동
  fetch하는 최초 설계는 이 저장소의 pre-live 안전 게이트(dry-run 여부와 무관하게 모든 live 모드
  broker 호출 차단)와 충돌해 dry-run/sizing-only 검증까지 깨뜨렸으므로 폐기. 스냅샷을 이미 만들어진
  객체로 전달받는 방식으로 재설계 — 실제 fetch 배선은 향후 실거래 승인 이후 production caller의
  책임으로 명시적으로 남겼다.
- 신규 테스트: `tests/test_live_order_gateway.py`에 Codex의 정확한 반례(30,000원 실제 + 3,000,000원
  선언 → 15,000원 이하로 capping), 실제 잔고가 더 큰 경우도 caller 선언값을 초과하지 않음, 잘못된
  타입 차단, stale snapshot 차단, snapshot 미제공 시 기존 동작 유지(하위 호환) 검증.
  `tests/test_account_cash.py`(17건) 신설 — 정상 fetch/USD→KRW 변환, cash 필드 누락/비숫자/음수/
  NaN/Infinity 차단, FX rate 무효값 차단(broker 호출 전 차단), 네트워크 실패 wrapping,
  `RuntimeError`(pre-network 안전 게이트) 비-wrapping 전파 확인.
- 처리 상태: RESOLVED

### CODEX-037(HIGH, NaN optional sizing/risk caps가 fail-open)

- 재현(`CODEX_REVIEW.md` 기록): fractional entry + `max_risk_per_trade_krw=NaN` → qty
  0.222222..., 3,000원 주문 승인. fractional + `strategy_max_quantity=NaN` 동일. whole-share +
  `max_order_notional_krw=NaN` → qty 2, 27,000원 주문 승인. 원인은 `NaN <= 0`/`NaN > 0`이 모두
  False이고 `min(x, nan)`이 인자 순서에 따라 x를 그대로 반환할 수 있다는 Python의 NaN 비교
  특성.
- 수정: `live_readiness/order_gateway.py`에 `_validate_optional_positive_number()` 신설,
  `max_order_notional_krw`/`max_daily_loss_krw`/`max_risk_per_trade_krw`/`strategy_max_quantity`/
  `stop_price_usd` 5개 전부에 reservation lock 진입 전 적용(bool 제외, finite, 양수 검증;
  `None`만 통과 허용).
- 신규 테스트: `tests/test_live_order_gateway.py`에 5개 필드 × {NaN/Infinity/-Infinity/True/False/
  문자열/음수/0} 조합 차단 확인, Codex의 정확한 3개 반례(fractional+risk NaN, fractional+strategy
  NaN, whole-share+notional NaN)가 reservation 0건으로 차단됨을 확인.
- 처리 상태: RESOLVED

### CODEX-038(LOW, 테스트가 운영 CSV mtime 변경)

- 재현(`CODEX_REVIEW.md` 기록): 전체 테스트 전후 `strategy_performance.csv` content SHA-256/크기는
  동일하나 mtime이 `1785082147` → `1785083284`로 변경.
- 원인: `tests/test_performance_analytics.py::test_summary_csv_generation`이
  `PERFORMANCE_SUMMARY_FILE`/`PERFORMANCE_TRADES_FILE`만 격리하고 `STRATEGY_PERFORMANCE_FILE`은
  격리하지 않음 — `write_performance_files()`가 `strategy_df` 미지정 시 항상
  `build_strategy_performance()`를 호출해 실제 저장소 루트 `strategy_performance.csv`에 기록.
- 수정: 해당 테스트에 `monkeypatch.setattr(analytics, "STRATEGY_PERFORMANCE_FILE", tmp_path / ...)`
  추가(1줄). `write_performance_files()` 자체는 변경하지 않음(정책/동작 불변).
- 처리 상태: RESOLVED

### 검증 결과 (CODEX-034~038 전체)

- 전체 회귀: `venv/bin/python -m pytest -q` **1,125 passed, 0 failed, 2 warnings**. 이전 사이클
  종료 시점 1,044 passed 대비 81건 신규.
- 실제 Alpaca/Slack/Yahoo 호출 0회. 실제 저장소 루트 `TRADING_STATE.db*`/
  `LIVE_ENTRY_RESERVATION.lock`이 회귀 실행 전후 존재하지 않음을 확인.
- `order_history.csv`/`universe.csv`/`strategy_performance.csv` content **및 mtime** 모두 불변
  (CODEX-038 수정 이후 재확인) — `.env`·kill switch/notification 상태 파일 변경 없음.
- `git diff --check` 통과.
- main 병합, origin push, 운영 배포, 실거래 활성화, `approved`/`live_enabled` 변경 없음.
- 테스트 삭제/조건 완화 없음 — 4건 모두 신규 검증 추가 또는 기존 테스트의 격리 누락 보완이며,
  기존에 통과하던 assertion을 약화한 사례는 없다(단, 트러스트 퍼센트 상한 도입으로 값이 바뀐
  기존 사이징 테스트 일부는 새 기대값으로 갱신 — 검증 자체의 엄격도는 동일하거나 강화됨).

## Stage 11: Account/Risk/Sizing/Execution Engine 계층 분리 (2026-07-28)

### 배경

사용자가 CODEX-034~038 처리와 별개로, 주문 경로 전체를 `Market Data → Strategy Engine → Signal →
Risk Engine → Account Engine → Sizing Engine → Execution Engine → Broker` 계층으로 명시적으로
분리하라고 지시했다. 핵심 원칙: Strategy는 신호/진입가/손절가/목표가만 결정하고 계좌 잔고·비율·
최종 수량·주문 가능 금액을 결정하거나 신뢰 기준으로 전달할 수 없다.

### 구현

- `live_readiness/trusted_operator_config.py`(신규) — `cash_usage_percent` 트러스트 상한과
  동시 포지션/일일 진입 한도의 단일 소스(`get_cash_usage_percent_ceiling()`/
  `get_max_concurrent_live_positions()`/`get_max_daily_live_entries()`, 매 호출 재검증).
  `account_cash.py`/`order_gateway.py`가 여기서 값을 가져오도록 갱신(기존 상수명은 하위 호환을
  위해 재노출).
- `live_readiness/account_engine.py`(신규) — `AccountSnapshot`(frozen dataclass):
  broker_cash_krw, non_margin_available_cash_krw, effective_cash_krw(=min), pending/unknown/
  reconciliation-required/open-position 노출(entry_reservation_ledger 기반), active_position_
  count, today_entry_count, as_of, trading_mode, account_id, reconciliation_complete.
  `build_account_snapshot(broker, fx_rate, conn, ...)`가 fail-closed로 조립하며
  `expected_account_id`/`expected_trading_mode`로 계좌 식별자·Paper/Live 불일치도 차단.
  `compute_max_allocatable_cash_krw()`/`compute_available_for_new_order_krw()`가 트러스트 상한과
  ledger 차감을 적용.
- `live_readiness/risk_engine.py`(신규) — `compute_risk_decision(entry_price, stop_price,
  fx_rate, daily_loss_remaining_krw, max_risk_per_trade_krw=None, ...)`가 risk_based_qty를
  계산. 모든 입력 finite 검증, stop_price가 entry_price보다 낮지 않으면 차단.
  `compute_daily_loss_remaining_krw()` 헬퍼 포함.
- `live_readiness/sizing_engine.py`(신규) — `compute_sizing_decision(available_for_new_order_krw,
  buffered_entry_price_usd, fx_rate, fractionable, risk_based_qty, strategy_max_qty=None, ...)`가
  `actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)`를 계산(세 값 모두
  None/NaN/Infinity/bool/문자열/음수 검증 통과 시에만). `strategy_max_qty=0`은 무효(하나
  "cap 없음"은 반드시 `None`). `apply_entry_price_buffer(price, buffer_bps, slippage_usd)`가
  가격 버퍼를 적용.
- `live_readiness/execution_engine.py`(신규) — `ValidatedOrderCommand`(frozen dataclass:
  command_id/signal_id/strategy_id/symbol/side/purpose/qty/estimated_price/estimated_notional/
  account_snapshot_id/risk_decision_id/sizing_decision_id/client_order_id/created_at/expires_at)
  + `build_validated_order_command()` + `submit_validated_command(command, broker,
  live_entry_context, ...)`. broker 호출 전 (1) command 타입 검증 (2) 만료 검증
  (3) qty*price==estimated_notional 검증(변조 탐지) (4) live_entry_context.symbol==command.symbol
  검증 (5) `client_order_id`로 기존 SQLite 예약 조회 후 symbol 불일치 시 차단 — 5개 모두 통과해야
  `broker.submit_order()`를 호출한다. `reservation_id`/`entry_intent_id`는 command 자체가 아니라
  반환되는 `ExecutionResult`에 실린다(근거는 `DECISION_LOG.md` 결정 2 — 이 저장소의 유일한
  예약 지점은 여전히 `broker.submit_order()` 내부이므로 broker 호출 전에는 존재하지 않는다).
- `live_readiness/watchlist_affordability.py` — `STATUS_STALE_ACCOUNT_STATE`(존재하지만 만료된
  `AccountState.as_of`) 신설, `AccountState.staleness_error()`로 검증(`UNKNOWN_ACCOUNT_STATE`와는
  별도 실패 경로). `AffordabilityResult`에 `buffered_entry_price`/`account_snapshot_at` 필드 추가.
- `paper_strategy_order.py` — 동작 변경 없음, 모듈 docstring에 "legacy compat, 신규 작업은
  Execution Engine 경유" 명시만 추가.

### 아키텍처 경계 강제

`tests/test_execution_engine.py::test_only_execution_engine_and_legacy_compat_call_broker_submit_
order`가 저장소 전체를 grep해 `broker.submit_order(`/`self.broker.submit_order(` 패턴의 실제
호출부(변수명 기준, docstring의 클래스명 언급은 제외)를 찾고, 허용 목록(`execution_engine.py`,
`broker/alpaca_client.py`, `paper_strategy_order.py`) 밖에 있으면 실패시킨다.

### 검증 결과

- 전체 회귀: `venv/bin/python -m pytest -q` **1,299 passed, 0 failed, 2 warnings**. 이전 사이클
  종료 시점 1,125 passed 대비 174건 신규.
- 실제 Alpaca/Slack/Yahoo 호출 0회. 실제 저장소 루트 `TRADING_STATE.db*`/
  `LIVE_ENTRY_RESERVATION.lock`이 회귀 실행 전후 존재하지 않음을 확인.
- `order_history.csv`/`universe.csv`/`strategy_performance.csv` content 및 mtime 모두 불변.
- `git diff --check` 통과.
- main 병합, origin push, 운영 배포, 실거래 활성화, `approved`/`live_enabled` 변경 없음.
- 기존 테스트 파일(broker/paper order execution/order gateway/watchlist affordability 등) 중
  단 하나도 삭제하거나 조건을 완화하지 않았다 — `test_watchlist_affordability.py`의 기존 헬퍼에
  `as_of`/`now` 인자가 추가된 것은 새 STALE_ACCOUNT_STATE 검증을 통과시키기 위한 확장이며, 기존
  assertion은 전부 그대로 유지된다.
- 신규 모듈은 전부 building block(순수 계산/검증 함수)이며, 실제 운영 스캔·주문 파이프라인
  (`daily_candidate_scanner.py`, `paper_strategy_order.py::main()`)에는 아직 배선하지 않았다 —
  Stage 10/CODEX-034 watchlist affordability와 동일한 선례.

## CODEX-039/040/041 실제 운영 경로 배선 사이클 (2026-07-28)

### CODEX-039(MEDIUM, 50%가 default가 아니라 강제 maximum이며 caller percent도 무시되지 않음)

- 재현(`CODEX_REVIEW.md` 기록): trusted 100%/90%/50% 어느 값을 선택해도 코드 상수
  `CASH_USAGE_PERCENT_CEILING=50`에 막혀 15,000원으로 축소됨. caller가 더 작은 percent를
  전달하면 그 값이 그대로 반영되어(예: caller 40% → 12,000원) caller percent가 완전히
  무시되지도 않음.
- 수정: `trusted_operator_config.get_cash_usage_percent()` 신설 — 인자 없이 트러스트 값을 그대로
  반환. `live_entry_pipeline.py`가 이 함수만 사용하며 caller/전략 쪽에서 percent를 넘길 방법 자체가
  없다(파이프라인 함수 시그니처에 그런 파라미터가 없음). 기존 `get_cash_usage_percent_ceiling()`
  (`min(caller, trusted)` 계약)은 `order_gateway.py`의 레거시 `LiveEntryContext.cash_usage_percent`
  필드 전용으로 이름을 분리해 유지 — 값 자체(50)는 변경하지 않음.
- 신규 테스트: `tests/test_trusted_operator_config.py`에 `get_cash_usage_percent()`가 인자를
  받지 않음, ceiling 함수와 동일한 값을 반환함, 손상된 설정에서 fail-closed됨을 확인(9건).
- 처리 상태: RESOLVED

### CODEX-040(HIGH, 실제 main 주문 흐름이 Execution Engine 전체를 우회)

- 재현(`CODEX_REVIEW.md` 기록): `paper_strategy_order.main()` 런타임 계측에서 legacy broker
  `submit_order` 1회, Execution Engine 0회, Account/Risk/Sizing Engine 미사용으로 실제 주문이
  제출됨.
- 수정: 신규 `live_readiness/live_entry_pipeline.py::run_live_entry_pipeline()` — Account Engine
  (`build_account_snapshot`, broker.get_account() 기반) → 트러스트 cash_usage_percent → Risk
  Engine(`compute_risk_decision`, `risk_config.STOP_LOSS_RATE` 기반 기본 손절가) → Sizing Engine
  (`compute_sizing_decision`) → Affordability Filter(`evaluate_affordability`) → Execution
  Engine(`submit_validated_command`)을 순서대로 호출, 각 단계 실패 시 `LiveEntryPipelineError`로
  즉시 차단(broker 호출 0회). `paper_strategy_order.main()`의 주문 제출 블록이
  `broker.config.is_live_mode`일 때 이 파이프라인을, 아닐 때(Paper) 기존 `submit_order()`를
  호출하도록 분기됐다 — Paper 경로는 코드 한 줄도 바뀌지 않음.
- 신규 헬퍼: `_get_current_fx_rate_krw_per_usd()`/`_get_live_entry_allow_list()` —
  `LIVE_FX_RATE_KRW_PER_USD`/`LIVE_ENTRY_ALLOW_LIST` 환경변수를 fail-closed로 읽음(TBD_OPERATOR,
  실제 FX provider/allow-list 연동은 여전히 미구현 상태로 남김).
- `execution_engine.submit_validated_command()`에 optional `account_cash_snapshot` 파라미터 추가
  — 공급되면 `broker.submit_order()`로 전달(older 테스트 더블이 이 kwarg를 몰라도 깨지지 않도록
  공급됐을 때만 전달).
- 신규 테스트: `tests/test_live_entry_pipeline.py`(11건, 순수 유닛/통합) — 정상 경로 broker
  1회 호출, 각 엔진 실패 시 broker 0회, client_order_id 재사용 시 기존 예약과의 symbol 불일치
  차단, strategy_max_qty가 잔고 기준 상한을 넘길 수 없음 확인.
  `tests/test_main_live_entry_wiring.py`(9건, `paper_strategy_order.main()` 런타임 통합) —
  live 모드 정상 경로에서 Account/Risk/Sizing/Execution Engine이 각각 정확히 1회 호출됨, 레거시
  `submit_order()` wrapper가 절대 호출되지 않음, Account Engine 실패/FX rate 미설정/allow-list
  불일치/잔고 부족/예약 충돌이 각각 broker 호출 0회로 차단됨, Paper 모드 `main()`은 신규 엔진을
  전혀 호출하지 않고 기존 동작 그대로임(기존 `FakeBroker` 재사용).
- 처리 상태: RESOLVED

### CODEX-041(MEDIUM, affordability가 실제 후보/주문 차단에 미배선)

- 재현(`CODEX_REVIEW.md` 기록): 30,000원 계좌 모사에서 50,000원 non-fractionable candidate가
  `evaluate_affordability()` 호출 0회, Execution Engine 호출 0회로 legacy broker에 제출됨.
- 수정: `live_entry_pipeline.py`가 Sizing Engine 직후, Execution Engine 호출 직전에
  `watchlist_affordability.evaluate_affordability()`를 재실행 — watchlist 후보 선별 단계와
  **동일한 함수**를 사용해 두 단계가 서로 다른 결론에 도달할 수 없게 했다. non-affordable이면
  `LiveEntryPipelineError`로 broker 호출 0회 차단.
- 범위 결정(`DECISION_LOG.md` 결정 4): watchlist 단계의 "사전" 일괄 필터링(scanner 전체 후보군에서
  미리 걸러내는 것, 효율성 목적)은 이번 사이클에서 배선하지 않았다 — `paper_strategy_order.main()`
  자체가 종목을 하나씩 순회하는 구조라 "watchlist 일괄 필터" 단계가 애초에 존재하지 않는다. Codex의
  실제 반례(50,000원 non-fractionable candidate가 broker까지 제출됨)를 정확히 재현·차단하는
  지점은 실행 직전 재검증이며, 이 지점을 구현한 것으로 실제 안전 위험은 해소됐다.
- 처리 상태: RESOLVED

### 검증 결과 (CODEX-039/040/041 전체)

- 전체 회귀: `venv/bin/python -m pytest -q` **1,331 passed, 0 failed, 2 warnings**. 이전 사이클
  종료 시점 1,299 passed 대비 32건 신규.
- 실제 Alpaca/Slack/Yahoo 호출 0회. 실제 저장소 루트 `TRADING_STATE.db*`/
  `LIVE_ENTRY_RESERVATION.lock`이 회귀 실행 전후 존재하지 않음을 확인.
- `order_history.csv`/`universe.csv`/`strategy_performance.csv` content 및 mtime 모두 불변.
- `git diff --check` 통과.
- main 병합, origin push, 운영 배포, 실거래 활성화, `approved`/`live_enabled` 변경 없음.
- Paper 모드 `main()` 동작은 코드 diff 및 런타임 통합 테스트(신규 엔진 호출 0회 확인) 양쪽에서
  완전히 미변경임을 재확인했다 — 기존 테스트 400건 이상 단 하나도 수정하지 않았다.

## 자동 운영 구조 전환 착수 (2026-07-28, 진행 중)

**Codex 독립 재검증 결과 반영**: `CODEX_REVIEW.md`(커밋 `ebce9d0`)가 CODEX-039/040/041 배선
사이클(커밋 `ae2b0fd`/`fc20574`)을 독립 재검증해 `PASS_WITH_CONDITIONS`를 부여했다. CODEX-034~041
전 항목 RESOLVED, 신규 CRITICAL/HIGH 없음. Limited live review 상태가 `BLOCKED` →
`READY_FOR_LIMITED_LIVE_REVIEW`로 상승했으나 Live trading은 `DO_NOT_ENABLE` 그대로다. Codex가
명시한 필수 후속 조건(사람이 수행): operator TBD 검토, kill-switch 절차 검토, reconciliation
runbook 검토 — 셋 다 아직 수행되지 않았다.

**사용자 신규 지시 처리 현황**:
1. `cash_usage_percent` 기본값 50 → 90 — 완료(커밋 `a0f0ae2`), 4개 기존 테스트 기대값 갱신,
   전체 회귀 1,333 passed.
2. "소수점 주문 금지"/"최소 1주 이상 매수 가능한 종목만 주문" — 이미 코드에 배선돼 있음을
   확인하고 회귀 테스트로 고정(같은 커밋).
3. 수동 allow-list/일일 진입 횟수/최대 동시 포지션 등을 TBD 필수 조건에서 제거 — **보류**.
   실제 "시장 전체 후보 자동 선별" 기능이 구현되기 전까지 문서만 앞서 갱신하지 않기로 결정
   (`DECISION_LOG.md` 결정 4).
4. 전략 기반 자동 매수·매도·손절·익절·분할익절·트레일링스탑·무효화·시간손절·EOD청산 —
   사용자 지시 메시지가 도중에 잘려 나머지 내용을 재요청한 상태(`DECISION_LOG.md` 결정 5),
   미착수.

**다음 단계**: 사용자로부터 전략 lifecycle 지시의 나머지 부분을 수신하는 대로, (a) 시장 전체
후보 자동 선별 구현, (b) 전략 인터페이스에 entry/stop/take-profit/partial-exit/trailing-stop/
invalidation/time-exit/EOD-exit 필드 확장, (c) 두 기능이 실제로 배선된 뒤에야
`TBD_REVIEW_RECOMMENDATIONS.md`/`LIMITED_LIVE_REVIEW_CHECKLIST.md`의 관련 TBD 항목 갱신,
(d) 9종 거버넌스 문서 전체 갱신 및 `FINAL_VALIDATION_PACKAGE.md` 재생성을 한 번에 진행한다.
