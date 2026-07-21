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

## 요약

CRITICAL 0건. HIGH 전부(001/002/003/005/006/007/008 — 003/005는 이전 사이클에서 이미 RESOLVED로 재확인) RESOLVED. MEDIUM 전부(004/009) RESOLVED. 남은 항목은 CODEX Finding이 아니라 Phase 1 자체 승인 기준(부분 체결의 포지션 상태 반영, Phase 5 범위)과, `order_history.csv`/`order_reconciliation.csv` 간 교차 파일 트랜잭션 정합성(`DECISION_LOG.md`에 `NEEDS_USER_DECISION`으로 기록, SQLite 전환 후보 — 아래 참고)이다.
