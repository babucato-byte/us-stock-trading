# CODEX_REVIEW — CODEX-055·056 최종 독립 재검증

## 1. 검증 대상

검증 시작 시 실행 결과:

```text
$ git status --short
(no output — clean)

$ git branch --show-current
feature/kis-live-broker

$ git rev-parse HEAD
0a2a9eba2c31c9ab1651b568d6ccab43f5dfdf3c

$ git show --stat --oneline HEAD
0a2a9eb Normalize persistence failures and invalidate unrollbackable connections
 execution/execution_engine.py                   | 124 ++++--
 execution/order_repository.py                   | 141 ++++++-
 tests/test_cancel_audit_lifecycle.py            |   4 +-
 tests/test_order_repository_cas.py              |   5 +-
 tests/test_persistence_failure_normalization.py | 521 ++++++++++++++++++++++++
 5 files changed, 751 insertions(+), 44 deletions(-)

$ git diff --check
(no output — pass)
```

branch, exact HEAD, clean working tree와 diff check 조건은 모두 일치했다. 독립 수집 결과는
`2160 tests collected in 2.98s`였다.

## 2. 최종 판정

Overall verdict: **BLOCKED**

CODEX-055 UNKNOWN fallback 결함: **RESOLVED**

CODEX-056 rollback 실패 + 정상 close 경로: **RESOLVED**

Oracle read-only deployment: **허용하지 않음 (현재 HEAD 기준)**

Real-order activation: **금지**

핵심 수정 경로는 해결됐지만 지시문의 최종 기준을 위반하는 신규 HIGH 두 건을 독립 probe로
재현했다.

### CODEX-057 — HIGH — read repository 경계에서 raw SQLite 오류 탈출

`compare_and_set_state()`의 write 오류는 repository 전용 hierarchy로 정규화되지만
`order_repository.load()`와 `load_events()`는 DB 호출을 감싸지 않는다.

```text
load() injected failure:
type = sqlite3.OperationalError
raw sqlite3.Error = true

load_events() injected failure:
type = sqlite3.OperationalError
raw sqlite3.Error = true
```

이는 PASS 조건인 “raw SQLite 오류 저장소 경계 탈출 0건”과 BLOCKED 조건인 “raw SQLite 오류가
상위로 그대로 탈출”에 직접 해당한다. 특히 cancel 시작의 `load()` 실패는 repository
persistence hierarchy가 아니라 raw 오류로 caller까지 전파된다.

필수 조치:

1. `load()`와 `load_events()`의 raw `sqlite3.Error`를 민감정보 없는
   `OrderRepositoryPersistenceError`로 변환하고 원본을 exception chaining으로 보존한다.
2. closed/unusable connection과 SELECT failure도 같은 boundary contract로 고정한다.
3. repository의 공개 DB 함수별 raw SQLite escape negative test를 추가한다.

### CODEX-058 — HIGH — close 실패 시 connection이 실제로 invalidated되지 않음

commit 실패 → rollback 실패 → close 실패를 독립 probe로 실행했다.

```text
raised = OrderRepositoryRollbackError
cause = OperationalError (rollback failure preserved)
close calls = 1
operator alerts = 2
connection.in_transaction = true
same connection reuse = usable
new writer = OperationalError("database is locked")
```

close 실패 alert와 rollback cause 보존은 통과했다. 그러나 `invalidate_connection()`은 close가
실패하면 `False`만 반환한 뒤, 상위 오류 메시지는 connection이 invalidated됐다고 단정한다.
실제로는 transaction과 lock이 남고 실패 connection도 재사용 가능하다. 이는 BLOCKED 조건인
“rollback 실패 connection 재사용 가능”과 “새 writer가 database is locked”에 해당한다.

필수 조치:

1. close 실패를 “invalidated”로 보고하지 말고 poison 상태를 명시적으로 추적해 동일 connection
   재사용을 거부한다.
2. 가능한 connection owner 경계에서 underlying handle을 강제 폐기하거나 프로세스 격리/종료
   정책을 적용해 lock이 유지된 채 실행을 계속하지 않게 한다.
3. close 실패 후 same-connection 거부와 새 writer 성공을 독립 테스트로 고정한다.

## 3. CODEX-055 독립 재현

저장소 테스트 helper를 사용하지 않는 `/private/tmp` probe로 KIS cancel transport 이후 final
state와 UNKNOWN fallback을 순서대로 실패시켰다. UNKNOWN 오류 유형별 결과는 동일했다.

```text
OperationalError           -> CancelPostTransportError / STATE_PERSISTENCE
IntegrityError             -> CancelPostTransportError / STATE_PERSISTENCE
DatabaseError              -> CancelPostTransportError / STATE_PERSISTENCE
base sqlite3.Error         -> CancelPostTransportError / STATE_PERSISTENCE
OrderRepositoryPersistenceError -> CancelPostTransportError / STATE_PERSISTENCE
```

각 경로 공통 결과:

```text
normal success return = 0
transport calls = 1
events = GATE_APPROVED -> EXECUTION_PLANNED -> SHADOW_ERROR
SHADOW_BLOCKED = 0
SHADOW_COMPLETED = 0
terminal count = 1
operator alert >= 1
manual reconciliation 문구 존재
automatic re-cancel = 0
original fallback exception = __cause__로 보존
```

따라서 CODEX-055의 UNKNOWN fallback 원 결함은 해결됐다.

## 4. 민감정보 검증

raw 오류에 가짜 계좌번호, CANO, App Secret, Access Token, Authorization, broker raw payload,
SQL parameter를 삽입했다. 상위 예외 문자열, 운영 alert, Shadow audit, 캡처 로그를 합쳐 검색한
결과 원문 노출은 0건이었다.

상위 오류는 고정 문구와 reason code만 사용하고, alert는 오류 message가 아닌 type만 기록한다.
broker 결과 logging은 `safe_repr()`를 사용한다. 기존 secret redaction 회귀 테스트도 통과했다.

## 5. CODEX-056 독립 재현

### Commit 실패, rollback 성공

`OrderRepositoryTransactionError`로 정규화되고 원본 commit 오류가 cause로 보존됐다.
transaction은 닫혔고 동일/새 connection 후속 write가 성공했다. 영구 lock은 없었다.

### Commit 실패, rollback 실패, close 성공

production-only DB lock probe 결과:

```text
writer A = BEGIN IMMEDIATE -> UPDATE -> event INSERT -> commit failure -> rollback failure
raised = OrderRepositoryRollbackError
connection close calls = 1
poisoned underlying connection reuse = ProgrammingError
writer B BEGIN IMMEDIATE/write/commit = success
database is locked = 0
```

새 connection에서 원래 state/event만 관찰되어 partial durable 성공으로 처리되지 않았다.
별도 audit connection의 `SHADOW_ERROR` 기록과 운영 alert도 가능했다.

### Close 실패

rollback 원인은 exception chain에 보존됐고 close 실패는 운영 alert로 전달됐으며 정상 반환은
없었다. 그러나 CODEX-058처럼 실제 connection 폐기·lock 해제가 실패해 전체 CODEX-056 판정은
완전 해결이 아니다.

## 6. 주문 저장소와 감사 저장소 분리

rollback 실패 뒤 close가 성공한 정상 invalidation 경로에서는 별도 audit connection이 같은 DB에
`SHADOW_ERROR`를 exactly once 기록했고 운영 alert도 전달됐다. poisoned order connection은 즉시
`ProgrammingError`로 재사용이 거부되고 새 writer도 성공했다.

close 자체가 실패하는 CODEX-058 경로에서는 같은 SQLite write lock 때문에 별도 writer/audit도
진행할 수 없다. 따라서 이 항목 전체는 FAIL이다.

## 7. CODEX-054 회귀 및 terminal uniqueness

Pre-transport gate 차단, CANCEL_PENDING CAS 실패, EXECUTION_PLANNED 감사 실패는 transport 0회와
`SHADOW_BLOCKED` 분류를 유지한다. Post-transport ambiguous, final-state 실패, UNKNOWN fallback
실패와 raw SQLite 오류는 정상 성공으로 반환하지 않고 `SHADOW_ERROR` exactly once다. 성공은
`SHADOW_COMPLETED` exactly once다.

독립 UNKNOWN probe의 모든 run은 같은 `audit_run_id`로 terminal 1건이며 zero/multiple terminal은
없었다. Migration 10 partial unique index 회귀도 집중 테스트에서 통과했다.

## 8. 기존 CODEX-042~054 회귀

집중 범위에서 다음을 재확인했다.

- Alpaca 실제 주문 HTTP 0회
- KIS unauthorized direct submit/cancel transport 0회
- reconciliation 실패 transport 0회, UNKNOWN account-wide 차단
- 부분체결 정확성, 고위험 exit 기본 false
- CAS 밖 상태 UPDATE 0건, `update_status` API 부재
- `audit_run_id` 필수, terminal audit 최대 1건
- secret redaction 유지, full 40-character SHA exact match
- Shadow entry/exit 실제 주문 0회
- KIS verification matrix 문서 일관성

기존 finding의 기능 회귀는 발견하지 않았다. 신규 CODEX-057/058이 최종 판정을 차단한다.

## 9. 테스트 결과

### 집중 안전 테스트

구현자 범위보다 넓게 관련 파일을 새로 선택해 실행했다.

```text
1641 passed
0 failed
0 skipped
0 xfailed
1 warning
68.99s
```

### 정방향 전체

```text
2160 passed
0 failed
0 skipped
0 xfailed
2 warnings
74.07s
```

### 역방향 전체

```text
2160 passed
0 failed
0 skipped
0 xfailed
2 warnings
92.13s
```

경고는 local LibreSSL/urllib3 호환 경고와 의도된 unsupported scanner-field 경고다.

## 10. 네트워크, 운영 파일, artifact

저장소 밖 socket guard가 `socket.connect`, `connect_ex`, `create_connection`을 차단·기록하는
상태에서 집중 및 전체 정·역순 테스트와 독립 probe를 실행했다.

```text
socket guard log = absent
Alpaca attempts = 0
KIS attempts = 0
Slack attempts = 0
other external socket attempts = 0
```

운영 파일의 전후 SHA-256, size, mtime는 모두 동일하다.

```text
order_history.csv
153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7 | 31 | 1784558966

universe.csv
9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3 | 833518 | 1784558966

strategy_performance.csv
ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8 | 69 | 1785083284
```

신규 저장소 artifact는 0건이다. 검증 전부터 존재한 ignored zero-byte lock
`KIS_ORDER_IDEMPOTENCY.lock`, `NOTIFICATION_HEALTH_STATE.lock`은 변경하지 않았다. 외부 probe,
DB/WAL/SHM 및 socket guard는 검증 후 저장소 밖에서 제거했다.

## 11. 남은 MEDIUM 및 Oracle read-only

기존 외부 조건 `price_field_last`, `cancel_tr_id_live`는 reference 확인 완료/실제 KIS 응답 미확인
상태이며 실주문 활성화 전에 Oracle read-only 또는 모의투자 확인이 필요하다. 그러나 현재는
신규 HIGH CODEX-057/058이 존재하므로 남은 조건이 외부 확인뿐이 아니다. Oracle read-only 단계는
현재 HEAD에서 허용하지 않는다.
