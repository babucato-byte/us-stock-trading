# CODEX_REVIEW

Review target: Phase 1 CODEX-001~006 최종 독립 재검증

Commits: `9688a13`, `b93a08a`, `22a6651`, `962eb69`, `1cc784b`

Phase: Phase 1 — 주문 안전성과 실행 경로 검증

Date: 2026-07-21

Overall verdict: **FAIL**

보고된 97개 테스트는 모든 지정 실행 형태에서 통과했고 `order_history.csv` 프로세스 잠금도 multiprocessing으로 확인했다. 그러나 parseable non-canonical `order_date`가 일일 한도를 우회하며, 잠금 없는 reconciliation 동시 갱신에서 실제 상태 유실이 재현됐다. 둘 다 HIGH이므로 Phase 1을 검증 완료할 수 없다.

## Previous findings

### [CODEX-001]

Status: **PARTIALLY_RESOLVED**

Evidence:

- `AlpacaBroker`의 `get_account`, `get_positions`, `get_recent_orders`, `get_order_by_client_order_id`, `submit_order`를 invalid mode, Live mode, Paper+Live URL, 임의 URL 설정으로 각각 직접 호출했다. 모든 잘못된 설정에서 HTTP 기록은 0회였다.
- client 생성 후 `broker.config`를 임의 endpoint 설정으로 교체한 뒤 위 다섯 메서드를 다시 호출해도 HTTP 기록은 0회였다.
- 정상 기본 endpoint는 `https://paper-api.alpaca.markets`이고 정상 Paper 설정만 Dummy/Recording session 호출까지 도달한다.
- `_request()`와 `get_order_by_client_order_id()`는 네트워크 전에 `validate_order_allowed()` 및 credential 검사를 수행한다. 주문 POST도 직전에 같은 검사를 수행한다.
- Live 주문 dry-run 분기는 안전검사보다 앞에 있지만 로컬 `BrokerResponse`만 반환하고 HTTP를 수행하지 않는다.

Remaining risk:

- `universe_builder.py`는 `AlpacaBroker` 공통 게이트를 사용하지 않고 `ALPACA_PAPER_BASE_URL → ALPACA_BASE_URL → Paper URL`로 URL을 선택해 import 시 직접 `requests.get()`을 실행한다. `ALPACA_BASE_URL`에 Live/임의 endpoint가 들어가도 차단되지 않는다. 주문 경로는 아니지만 “다른 Alpaca 호출 경로도 공통 검사를 우회하지 않는다”는 저장소 전체 주장에는 맞지 않는다. 신규 CODEX-009(MEDIUM)로 기록한다.
- Live URL 상수와 `ALPACA_LIVE_BASE_URL`은 broker 설정에 남아 있으나 기본/fallback 주문 endpoint로 사용되지는 않는다.

### [CODEX-002]

Status: **PARTIALLY_RESOLVED**

Evidence:

- 누락 파일, CSV 파싱 실패, 필수 컬럼 누락, 파싱 불가능 날짜는 `OrderHistoryUnavailable`을 발생시켜 신규 주문을 차단한다. 명시적 `initialize_order_history()`만 정상적인 빈 파일을 만든다.
- `eastern_now()`는 `America/New_York`을 사용한다. UTC/ET 날짜가 다른 시점, ET 자정 직전·직후, 2026년 DST 시작·종료 순간을 직접 검증했고 변환 결과는 정확했다.
- 한도-1은 허용, 한도 정확히 도달 및 초과는 차단됐다.
- 모든 저장 행을 상태와 무관하게 집계한다. `PENDING_SUBMISSION`, `SUBMITTED`, `REJECTED`, `CANCELLED`, `EXPIRED`, `SUBMISSION_FAILED`, `FILLED`가 모두 보수적으로 한도를 소비했다.
- 정상 이력 재시작 및 잠금 하 재조회는 기존 테스트와 multiprocessing 재현에서 유지됐다.

Remaining risk:

- `load_order_history()`는 `pd.to_datetime(..., errors="raise")` 성공 여부만 검사하고 실제 값을 `YYYY-MM-DD`로 정규화하거나 정확한 형식을 강제하지 않는다. `2026-07-20 10:30:00`과 `20260720`은 검증을 통과하지만 `count_orders_for_date(..., "2026-07-20")` 결과가 0으로 재현됐다. 일일 주문 한도를 우회할 수 있으므로 신규 CODEX-007(HIGH)이다.
- DST 변환 코드는 직접 검증상 정상이나, 보고와 달리 DST 시작·종료 전용 회귀 테스트는 저장소에 없다.
- 실제 broker order ID가 `order_history.csv`에 없어 외부에서 이미 중복된 행을 identity 기준으로 정리할 수는 없다. 현재 집계는 중복 행도 모두 세므로 한도 측면에서는 보수적이다.

### [CODEX-003]

Status: **RESOLVED**

Evidence:

- `try_reserve_order()`는 `fcntl.flock` 획득 → 최신 이력 재조회 → duplicate/일일한도 재검사 → `PENDING_SUBMISSION` 원자적 저장 순서로 동작한다.
- `_atomic_write_csv()`는 같은 디렉터리의 임시 파일에 쓰고 flush/fsync 후 `os.replace()`한다. 실패 시 임시 파일을 제거하며 기존 파일 보존 테스트가 통과했다.
- lock timeout은 주문을 차단하고, context manager의 `finally`에서 unlock/close를 수행한다.
- threading 동시성 테스트 4건을 5회 반복해 모두 통과했다.
- 별도 multiprocessing 재현에서도 동일 symbol은 1건만 성공, 다른 symbol은 두 행 모두 보존, 마지막 한도 슬롯은 한 프로세스만 통과했다.
- macOS의 실제 `fcntl.flock` 동작을 확인했다. Ubuntu도 동일 POSIX API 대상이지만 이 검증 환경에서 직접 실행하지는 않았다.
- 테스트는 history/lock/reconciliation 경로를 모두 `tmp_path`로 분리한다.

Remaining risk:

- 파일 자체는 fsync하지만 `os.replace()` 후 부모 디렉터리를 fsync하지 않아 전원 손실 수준의 rename 내구성까지 보장하지는 않는다. 정상 프로세스 충돌/종료에서의 원자성 문제는 재현되지 않았으며 LOW 수준이다.
- reconciliation 파일은 이 잠금의 보호 대상이 아니다. 이는 CODEX-006/CODEX-008에서 별도로 판정한다.

### [CODEX-004]

Status: **RESOLVED**

Evidence:

- 저장소 루트의 `pytest -q`, `python -m pytest -q`가 각각 동일한 97개 테스트를 통과했다.
- 저장소 상위에서 `pytest us-stock-trading -q`, `python -m pytest us-stock-trading -q`도 각각 동일한 97개 테스트를 통과했다.
- `conftest.py`가 자신의 절대 경로로 저장소 루트를 `sys.path` 선두에 넣으므로 실행 cwd와 무관하게 현재 프로젝트 모듈을 가져온다.
- 테스트 결과의 warning 경로와 import 위치는 현재 저장소를 가리켰고 시스템 전역 동명 모듈 오염은 관찰되지 않았다.

Remaining risk:

- `conftest.py`의 수동 `sys.path` 변경은 패키지 설치 기반 격리보다 약하지만 이번 네 가지 실행 형태에서 오동작은 재현되지 않았다.

### [CODEX-005]

Status: **RESOLVED**

Evidence:

- 상위 디렉터리에서 프로젝트 경로를 명시한 두 pytest 실행 모두 97개 공식 테스트만 수집했다.
- 이전에 Yahoo/Alpaca/Slack 요청을 import 시 실행하던 루트 ad-hoc 파일은 `collect_ignore`로 수집되지 않았다.
- socket connect/create_connection을 강제로 실패시키는 임시 `sitecustomize` 아래에서 저장소 루트 및 상위 실행이 모두 97 passed였다. 외부 socket 연결 시도는 탐지되지 않았다.
- `collect_ignore`는 저장소 root `conftest.py` 기준 상대 경로여서 상위 cwd에서도 적용됐고 다른 저장소의 수집 설정을 변경하지 않았다.

Remaining risk:

- 차단 목록은 명시적 파일명 목록이므로 향후 새 네트워크성 root 스크립트가 추가되면 자동으로 보호되지 않는다. 현재 파일 집합에서는 우회가 재현되지 않았으며 LOW 수준이다.

### [CODEX-006]

Status: **PARTIALLY_RESOLVED**

Evidence:

- 각 예약은 UUID 기반 `client_order_id`를 생성하고 같은 ID를 broker 제출에 전달한다.
- 재시작 시 `PENDING_SUBMISSION`, `SUBMITTED`, `PARTIALLY_FILLED` 행을 ID로 조회한다. broker order가 있으면 로컬 이력과 reconciliation을 갱신하며 자동 재주문하지 않는다.
- broker 404/미인식은 `MANUAL_REVIEW`, 조회 예외는 기존 상태 유지, rejected/canceled/expired/unknown은 각 로컬 상태로 매핑된다.
- `partially_filled`는 `PARTIALLY_FILLED`로 기록되고 filled와 구분된다. requested/filled/remaining/average price 기본 사례는 일관됐다.
- 단일 프로세스 반복 reconciliation은 client ID 행을 append하지 않고 갱신해 기존 idempotence 테스트가 통과했다.

Remaining risk:

- `order_reconciliation.csv`의 load-modify-save에는 lock이 없다. 두 multiprocessing worker가 서로 다른 client ID를 동시에 `FILLED`로 갱신하도록 동기화했을 때 최종 결과는 한 행만 `FILLED`, 다른 행은 `PENDING_SUBMISSION`으로 남았다. 실제 상태 갱신 유실/오판이므로 명시된 기준에 따라 신규 CODEX-008(HIGH)이다.
- `_record_pending_reconciliation()`은 `save_reconciliation()`의 False 반환을 무시한다. 저장을 강제로 실패시킨 재현에서 reconciliation 파일이 없는 상태로 `try_reserve_order()`가 성공과 client ID를 반환했다. 호출자는 이후 broker 주문을 제출할 수 있어 부분체결/재시작 reconciliation 보장이 사라진다.
- 즉시 broker 응답이 `partially_filled`인 재현에서 reconciliation은 `PARTIALLY_FILLED`였지만 `order_history.csv`는 뒤이어 `SUBMITTED`로 기록됐다. 두 권위 파일의 상태가 다음 실행까지 불일치한다.
- reconciliation 손상은 빈 DataFrame으로 degrade되며 fail-closed하지 않는다. history 예약이 남아 동일 symbol/date 재주문은 막지만 실제 체결 상태를 복구할 ID가 사라질 수 있다.
- 상태 전이의 단조성 검사가 없다. 동시 또는 stale 응답이 더 오래된 상태를 나중에 저장하면 상태 후퇴가 가능하다.
- `filled_qty > requested_qty` 등에 대한 검증/상한 처리 없이 음수 remaining quantity가 가능하다. unknown 상태는 non-terminal 집합에 없으므로 이후 자동 대조가 중단된다.

## New findings

### [CODEX-007] HIGH — 파싱 가능한 비정규 주문일이 일일 한도를 우회함

Status: **PARTIALLY_RESOLVED**

Evidence:

- 이력 무결성 검사는 날짜 파싱 성공만 확인하지만 일일 집계는 원문 문자열과 `YYYY-MM-DD`를 정확히 비교한다.
- `2026-07-20 10:30:00`, `20260720` 행이 모두 정상 load되면서 `2026-07-20` 집계는 0이었다.

Remaining risk:

- 손상·레거시·수동 편집 이력이 parseable non-canonical 날짜를 포함하면 기존 주문 수를 무시하고 신규 주문을 허용한다.

### [CODEX-008] HIGH — reconciliation 동시 갱신 유실과 상태 불일치

Status: **PARTIALLY_RESOLVED**

Evidence:

- multiprocessing barrier로 두 프로세스가 같은 초기 reconciliation을 읽은 뒤 서로 다른 주문을 갱신하도록 했다.
- 두 원자적 replace가 모두 성공했지만 마지막 writer의 snapshot이 앞선 갱신을 덮어써 `{'id-a': 'PENDING_SUBMISSION', 'id-m': 'FILLED'}`가 남았다.
- 즉시 부분체결 응답에서도 reconciliation=`PARTIALLY_FILLED`, history=`SUBMITTED` 불일치를 재현했다.
- reservation 시 reconciliation 저장 실패가 호출자에게 전파되지 않는 것도 재현했다.

Remaining risk:

- 실제 filled/partial/rejected 상태를 오래된 상태로 오판할 수 있다. 현재 duplicate/일일한도는 history 예약 때문에 직접 우회되지 않지만, Phase 1 부분체결 추적의 정확성을 깨고 향후 Phase 5 청산 판단에 잘못된 입력을 제공한다. 사용자 판정 기준상 HIGH이다.

### [CODEX-009] MEDIUM — broker 외 Alpaca GET 경로가 endpoint 안전 게이트를 우회함

Status: **PARTIALLY_RESOLVED**

Evidence:

- `universe_builder.py`가 import 시 환경변수 URL로 직접 `/v2/assets`를 조회하며 공식 Paper URL 검사를 하지 않는다.
- broker의 계좌/포지션/주문 GET·POST는 안전하게 차단되므로 주문 우회는 아니다.

Remaining risk:

- 잘못된 `ALPACA_BASE_URL` 또는 `ALPACA_PAPER_BASE_URL`로 Live/임의 host에 credential을 포함한 GET을 보낼 수 있다.

## Executed tests

- 저장소 루트 `venv/bin/pytest -q` → **97 passed, 2 warnings**
- 저장소 루트 `venv/bin/python -m pytest -q` → **97 passed, 2 warnings**
- 저장소 상위 `pytest us-stock-trading -q` → **97 passed, 2 warnings**
- 저장소 상위 `python -m pytest us-stock-trading -q` → **97 passed, 2 warnings**
- 집중 테스트 → **54 passed, 1 warning**
- 동시성 선택 테스트 → **4 passed, 40 deselected**, 5회 반복 모두 통과
- 최소 환경변수 + socket 차단 실행 → **97 passed, 2 warnings**
- 추가 직접 검증: broker 5개 네트워크 메서드 설정 행렬, ET/UTC/DST 경계, 한도-1/정확히/초과, 상태별 집계, multiprocessing history 경쟁 3종, reconciliation multiprocessing lost update, reconciliation 저장 실패, 즉시 부분체결 상태 일치.

Warnings:

- urllib3 `NotOpenSSLWarning`: Python의 LibreSSL 2.8.3과 urllib3 v2 지원 조건 불일치. 이번 mock 테스트 실패나 주문 우회는 아니지만 실제 HTTPS 환경 호환성 위험은 남는다.
- scanner unknown-field `RuntimeWarning`: 의도된 unsupported-field skip 테스트에서 발생하며 주문 안전성과 무관하다.

## Concurrency verification

- history lock: threading 5회 반복 및 실제 multiprocessing에서 동일 symbol, 다른 symbol, 마지막 한도 슬롯 모두 기대대로 동작했다.
- lock timeout은 fail-closed이며 예외 후 lock 해제 구조를 확인했다.
- reconciliation: multiprocessing lost update를 실제 재현했다. 원자적 파일 replace는 부분 파일을 방지할 뿐 read-modify-write serialization을 제공하지 않는다.

## Reconciliation verification

- client ID 생성/전달, 재시작 조회, partial/filled 구분, terminal 매핑, lookup 실패 fail-closed, 자동 재주문 금지는 기본 사례에서 확인했다.
- 단일 실행 idempotence는 확인됐지만 동시 실행 serializability, 저장 실패 전파, history와 reconciliation의 즉시 일관성은 충족하지 못했다.
- `order_reconciliation.csv` 문제는 중복 주문을 직접 만들지는 않지만 실제 체결 상태 오판을 재현했으므로 LOW/MEDIUM 보조파일 위험으로 축소할 수 없다.

## Network safety

- 공식 97개 테스트는 socket-level 차단 환경에서도 통과해 Alpaca, Slack, Yahoo 또는 기타 외부 HTTP 연결 시도가 없었다.
- broker의 잘못된 설정 직접 검증에서도 session HTTP 호출은 모두 0회였다.
- `universe_builder.py` 직접 실행은 하지 않았으나 정적 경로상 공통 endpoint 검증을 우회한다.

## Operational file safety

- 검증 전후 `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966`으로 동일했다.
- `order_reconciliation.csv`, `order_history.lock`, `.env`는 검증 전후 모두 존재하지 않았다.
- 저장소 내 실제 log/pid/runtime 파일 변화는 없었다.
- 모든 장애·동시성 재현 파일은 임시 디렉터리만 사용했다.

## Document consistency

- 테스트 수 97, 집중 테스트 54, 동시성 반복 5/5, 코드 커밋 해시는 실제 결과와 일치한다.
- Phase 1은 `IN_PROGRESS`로 유지돼 완료로 과장되지는 않았다. 부분체결 tracking과 Phase 5 포지션 생명주기의 구분도 문서화돼 있다.
- 그러나 `CRITICAL/HIGH 미해결 0건`, CODEX-001~006 전부 RESOLVED, reconciliation 무잠금이 낮은 우선순위라는 주장은 직접 재현 결과와 불일치한다.
- 문서는 `eastern_now()`에 “기존 테스트로 DST 검증”이 있다고 주장하지만 DST 시작/종료 전용 테스트는 없다.
- Phase 2 진입은 외부 재검증 결과에 따르도록 기록돼 있어 조건 자체는 명확하다.

## Unverified areas

- Ubuntu에서의 실제 `fcntl.flock` 및 filesystem rename 내구성은 실행 환경 부재로 직접 검증하지 않았다.
- SIGKILL/전원 손실 순간의 디렉터리 내구성은 실제 장애 주입하지 않았다.
- 실제 Alpaca Paper 계정 E2E와 실제 부분체결은 외부 호출 금지 원칙에 따라 수행하지 않았다.
- 운영 서버 timezone/cron 중복 실행/운영 환경변수는 확인하지 않았다.
- 향후 Phase 5가 reconciliation 상태를 소비하는 방식은 아직 구현되지 않아 검증할 수 없다.

## Phase 1 decision

**FAIL**

CODEX-007과 CODEX-008 HIGH가 남아 있으므로 `VALIDATE` 또는 단순 `KEEP_IN_PROGRESS` 판정이 불가능하며 재수정이 필요하다.

## Phase 2 recommendation

**DO_NOT_PROCEED**

기존 또는 신규 HIGH가 하나라도 미해결이면 진행하지 않는다는 게이트 기준에 따라 Phase 2로 진행할 수 없다.
