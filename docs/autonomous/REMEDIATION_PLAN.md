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
- 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review: BLOCKED**,
  **Live trading: DO_NOT_ENABLE**을 유지한다.
