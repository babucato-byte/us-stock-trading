# REMEDIATION_PLAN

검증 기준: `CODEX_REVIEW.md` (2026-07-21, 대상 커밋 `fe2988c`, `dc9bff9`; overall verdict **FAIL**, Phase 2 **DO_NOT_PROCEED**). 이 문서는 그 리뷰의 CODEX-001~006에 대한 이번 재수정 사이클의 처리 결과다. 우선순위/처리 순서는 지시서 기준 `CODEX-001 → 002 → 003 → 006 → 005 → 004`.

### CODEX-001 — Live endpoint 접근 위험 (HIGH)
- 재현 여부: 재현됨 — `AlpacaBroker._request()`(get_account/get_positions/get_recent_orders)가 `validate_order_allowed()`를 호출하지 않아, Paper 모드에 Live endpoint가 주입된 상태에서도 GET 요청이 안전검사 없이 나갈 수 있었다.
- 원인: 안전검사가 `submit_order()`에만 있었고 `_request()`(모든 GET의 공통 경로)에는 없었다.
- 수정 방안: `_request()` 최상단에서 `validate_order_allowed()`를 호출해 모든 broker API 호출(GET/POST)이 동일한 안전 게이트를 거치도록 통일. `self.config`를 매 호출마다 재검증하므로 생성 후 `.config`를 교체(변조)해도 이후 모든 호출이 차단됨.
- 수정 파일: `broker/alpaca_client.py`
- 테스트: `tests/test_broker_safety.py`에 6건 추가 — 잘못된 mode/Live endpoint에서 account/position GET 0회, Live endpoint order POST 차단, endpoint 변조 후 모든 호출 차단, Paper endpoint에서는 정상 mock 호출 허용.
- 처리 상태: RESOLVED
- 커밋 해시: `9688a13`

### CODEX-002 — 일일 주문 제한 fail-open, 서버 로컬 날짜 (HIGH)
- 재현 여부: 재현됨 — 이력 파일 누락/손상 시 0건으로 처리(fail-open), 날짜 계산이 서버 로컬 시간이라 서울-뉴욕 날짜가 갈리는 시간대에 한도 우회 가능.
- 원인: `load_order_history()`가 모든 예외를 삼키고 빈 DataFrame을 반환; `today = datetime.now().strftime(...)`가 `America/New_York` 대신 서버 로컬 시간 사용.
- 수정 방안: `load_order_history()`를 fail-closed로 전환 — 파일 없음(`MISSING_HISTORY`) 또는 파싱 실패/필수 컬럼 누락/`order_date` 파싱 실패(`CORRUPTED_HISTORY`) 시 `OrderHistoryUnavailable`을 발생시켜 신규 주문을 전부 차단. 명시적 초기화 함수 `initialize_order_history()`만 유효한 빈 이력 상태를 만들 수 있음. 날짜는 `market_hours.eastern_now()`(America/New_York, 기존 테스트로 DST 검증된 함수) 기준으로 통일.
- 수정 파일: `paper_strategy_order.py`
- 테스트: 이력 없음/컬럼 누락/`order_date` 파싱 실패/읽기 불가 각각 신규 주문 차단(4건), `initialize_order_history()` 정상 동작, ET 기준 날짜 계산(서울 저녁≠뉴욕 자정 경계) 검증.
- 처리 상태: RESOLVED
- 커밋 해시: `b93a08a`

### CODEX-003 — 비원자적 쓰기, 잠금 없는 동시성 (HIGH)
- 재현 여부: 재현됨 — 두 실행이 동시에 빈 이력을 읽고 각각 예약을 시도하면 한 행이 유실(lost update)될 수 있었고, `to_csv()`가 대상 파일에 직접 덮어써 쓰기 중 장애 시 손상 가능.
- 원인: 파일 잠금/트랜잭션 없음, in-place 쓰기.
- 수정 방안: `os.fdopen` 임시 파일 → `flush`/`fsync` → `os.replace()` 원자적 치환(`_atomic_write_csv`, macOS/Ubuntu). `fcntl.flock` 기반 프로세스 잠금(`_order_history_lock`, 타임아웃 시 예외로 주문 차단). 신규 `try_reserve_order()`가 잠금 안에서 이력을 다시 읽고, 중복/일일한도를 재검사한 뒤에만 `PENDING_SUBMISSION`을 기록. `update_order_status()`도 동일하게 잠금 하에 재조회 후 갱신해 다른 프로세스의 예약을 덮어쓰지 않음.
- 수정 파일: `paper_strategy_order.py`
- 테스트: 동일 심볼 동시 예약(1건만 성공), 서로 다른 심볼 동시 예약(둘 다 유실 없이 기록), 일일 한도 경계에서의 동시 예약(정확히 1건만 통과), 잠금 획득 타임아웃(파일 미변경 확인), 저장 실패 시 원본 파일 보존, 저장 실패 로그. 실제 `threading`으로 재현(단순 순차 mock 아님).
- 처리 상태: RESOLVED
- 커밋 해시: `b93a08a`

### CODEX-006 — 부분 체결 및 broker reconciliation 미구현 (HIGH)
- 재현 여부: 재현됨 — broker order id/체결 상태를 저장하지 않아 부분 체결과 실제 상태 불일치를 감지할 방법이 없었다.
- 원인: 주문 이력이 HTTP 제출 응답(200/201 여부)만 반영, 실제 체결 여부를 조회하지 않음.
- 수정 방안: 스키마 동결 원칙(`order_history.csv` 컬럼 변경 금지)을 지키기 위해 별도 파일 `order_reconciliation.csv`를 신설(키: `symbol`+`order_date`, `order_history.csv`에서 이미 유일). 예약 시 `client_order_id` 생성 후 broker 제출에 전달. `AlpacaBroker.get_order_by_client_order_id()` 신규 추가. `reconcile_pending_orders()`가 매 실행 시작 시 비종결 상태(PENDING_SUBMISSION/SUBMITTED/PARTIALLY_FILLED) 행을 broker와 대조해 `SUBMITTED/PARTIALLY_FILLED/FILLED/REJECTED/CANCELLED/EXPIRED/UNKNOWN`으로 갱신하고, broker가 모르는 주문은 `MANUAL_REVIEW`로만 표시(재주문 없음). `order_history.csv`의 `status` 컬럼(기존 컬럼, 값만 확장)도 함께 갱신해 duplicate-check와 일관성 유지.
- 수정 파일: `broker/alpaca_client.py`, `paper_strategy_order.py`
- 테스트: filled/partially_filled 구분, 알 수 없는 broker 주문 → MANUAL_REVIEW(+재주문 없음 확인), rejected/cancelled/expired/미지 상태 매핑(파라미터라이즈드), 조회 실패 시 상태 유지(재시도 가능), 재실행 멱등성(행 중복 없음), client_order_id 생성 및 broker 전달 확인.
- 처리 상태: RESOLVED — 단, Phase 1 완료(=VALIDATED) 조건은 이 항목만이 아니라 부분 체결이 실제로 "포지션 상태"까지 반영되는 것을 요구하며, 이는 Phase 5(포지션 생명주기) 범위. 이번 사이클은 지시서 6번이 명시한 "완전한 생명주기가 아닌 최소 안전 상태"까지만 구현.
- 커밋 해시: `22a6651`

### CODEX-005 — 상위 디렉터리 실행 시 네트워크성 스크립트 수집 (HIGH, 신규)
- 재현 여부: 재현됨 — `pytest.ini`의 `testpaths`는 경로 인자가 없을 때만 적용되므로, `pytest us-stock-trading -q`처럼 경로를 명시하면 무시되고 pytest 기본 패턴(`test_*.py`/`*_test.py`)이 저장소 전체에 적용되어 루트의 스크래치 스크립트(yfinance/Alpaca/Slack 실호출 코드 포함)가 수집될 수 있었다.
- 원인: `testpaths`는 "인자 없음" 조건부 설정이라는 pytest의 알려진 동작.
- 수정 방안: 저장소 루트에 `conftest.py`를 추가하고 `collect_ignore`로 8개 스크래치 파일을 명시적으로 제외. `collect_ignore`는 ini 해석 여부와 무관하게 pytest가 각 디렉터리를 실제로 순회할 때 참조하므로 더 견고함.
- 수정 파일: `conftest.py` (신규)
- 테스트: `cd repository-parent && pytest us-stock-trading -q` / `python -m pytest us-stock-trading -q` 모두 97 passed, 스크래치 파일 미수집, 네트워크 시도 없음 확인(수동 재현).
- 처리 상태: RESOLVED
- 커밋 해시: `962eb69`

### CODEX-004 — import 경로가 실행 위치에 의존 (MEDIUM)
- 재현 여부: 재현됨 — `pytest.ini`의 `pythonpath = .`가 모든 호출 형태에서 일관되게 적용된다는 보장이 없었다(상위 디렉터리에서 명시적 경로로 호출 시 ini 자체가 다르게 해석될 수 있음).
- 원인: 저장소 루트 import 가능 여부가 ini 해석 시점에 의존.
- 수정 방안: 동일한 신규 `conftest.py`가 수집 시작 시점에 `sys.path`에 저장소 루트를 직접 삽입 — ini가 적용되지 않는 호출 형태에서도 동작. 대규모 패키지화/editable install은 지시서 원칙대로 도입하지 않음.
- 수정 파일: `conftest.py` (CODEX-005와 동일 파일, 동일 커밋)
- 테스트: 저장소 루트/상위 디렉터리, `pytest`/`python -m pytest` 4가지 조합 모두 동일하게 97개 테스트 수집·통과.
- 처리 상태: RESOLVED
- 커밋 해시: `962eb69`

## 요약

CRITICAL 0건, HIGH 5건(001/002/003/006/005) 전부 RESOLVED, MEDIUM 1건(004) RESOLVED. 남은 항목은 CODEX Finding이 아니라 Phase 1 자체의 승인 기준(부분 체결의 포지션 상태 반영, Phase 5 범위)이며 `SCALPING_V1_ROADMAP.md`/`CURRENT_STATUS.md`에 별도로 추적한다. 재검증을 위한 `VALIDATION_PACKAGE.md`를 갱신했다.
