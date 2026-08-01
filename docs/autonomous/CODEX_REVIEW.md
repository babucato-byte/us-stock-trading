# CODEX_REVIEW — CODEX-057·058 최종 독립 재검증

## 1. 검증 대상 및 독립성

구현자 완료 보고와 기존 테스트 결과를 재사용하지 않고 코드, DB 오류, 다중 프로세스 lock과
entrypoint 종료 동작을 새로 검증했다.

검증 시작 시 실행 결과:

```text
$ git status --short
(no output — clean)

$ git branch --show-current
feature/kis-live-broker

$ git rev-parse HEAD
d783251e687686577e8ed29cb7e1837faec52df7

$ git show --stat --oneline HEAD
d783251 Normalize repository reads and fail-stop on unrecoverable connections
 execution/execution_engine.py                      |  90 +++-
 execution/idempotency.py                           |  54 +-
 execution/order_repository.py                      | 191 ++++++-
 scripts/run_health_report.py                       |  39 ++
 scripts/run_live_buy_entry.py                      |  45 +-
 scripts/run_reconciliation.py                      |  38 ++
 scripts/run_shadow_exit_evaluation.py              |  37 ++
 scripts/run_shadow_mode.py                         |  38 ++
 tests/test_cancel_audit_lifecycle.py               |  11 +-
 tests/test_persistence_failure_normalization.py    |   8 +-
 tests/test_repository_read_and_fatal_connection.py | 581 +++++++++++++++++++++
 11 files changed, 1070 insertions(+), 62 deletions(-)

$ git diff --check
(no output — pass)
```

필수 branch, exact HEAD, clean working tree와 diff check 조건은 모두 일치했다. 독립 수집 결과는
`2223 tests collected in 2.90s`였다.

## 2. 최종 판정

Overall verdict: **BLOCKED**

CODEX-057: **RESOLVED**

CODEX-058 repository invalidation/process-exit mechanism: **PARTIALLY RESOLVED**

Oracle read-only deployment: **허용하지 않음 (현재 HEAD 기준)**

Real-order activation: **금지**

repository와 5개 entrypoint 자체의 수정은 요구대로 작동한다. 그러나 실제 cancel post-transport
호출 경로가 `FatalRepositoryConnectionError`를 다른 예외로 변환해 entrypoint의 exit-code 4
handler까지 전달하지 않는 신규 HIGH가 있다.

### CODEX-059 — HIGH — cancel final-state handler가 fatal fail-stop 오류를 삼킴

독립 production-only probe에서 broker cancel 반환 후 final-state 저장이
`FatalRepositoryConnectionError`를 발생시키고 UNKNOWN fallback은 invalidated-connection 오류를
발생시키도록 했다.

```text
broker transport calls = 1
repository fatal raised = FatalRepositoryConnectionError
caller-visible exception = CancelPostTransportError
caller-visible reason_code = STATE_PERSISTENCE
caller-visible FatalRepositoryConnectionError = false
```

원인은 `_cancel_inner()`의 final `CANCELLED` 저장을 감싼 `except Exception`이 fatal 오류까지
UNKNOWN fallback 경로로 보내고, 마지막에 항상 `CancelPostTransportError`를 생성하기 때문이다.
`submit_cancel()` 바깥의 “fatal은 unchanged propagate” 주석과 달리 fatal 타입은 그 전에 소실된다.

결과적으로 해당 cancel이 5개 service 중 어느 실행 흐름에서 발생하더라도 entrypoint의
`except FatalRepositoryConnectionError`와 exit code 4 경로가 실행되지 않고 일반 오류 처리로
내려갈 수 있다. 이는 다음 BLOCKED 조건에 해당한다.

- entrypoint가 실제 호출 경로에서 fatal 오류를 받지 못함
- process가 exit-code 4 fail-stop 대신 계속 실행될 가능성
- close 실패 후 OS lock 회수 보장 상실
- 신규 HIGH 존재

필수 조치:

1. `_cancel_inner()`가 `FatalRepositoryConnectionError`를 별도 except로 즉시 재발생시켜 타입을
   보존한다.
2. terminal audit는 별도 connection으로 best-effort 시도하되 fatal을 변환하거나 덮지 않는다.
3. 실제 `submit_cancel()` → service entrypoint call chain에서 exit code 4를 검증하는 테스트를
   추가한다. entrypoint 함수에 fatal을 직접 주입하는 테스트만으로는 부족하다.
4. audit DB도 lock에 막힐 때 원래 fatal을 보존하고 process가 종료되는지 고정한다.

## 3. CODEX-057 결과

### Repository read API 목록

직접 검증한 public read API:

- `order_repository.load()`
- `order_repository.load_events()`
- `idempotency.find_existing()`
- `idempotency.has_unknown_order()`
- `idempotency.list_orders_by_status()`
- `idempotency.list_unknown_orders()`
- `idempotency.list_orders_with_broker_id()`

각 API에 `OperationalError`, `IntegrityError`, `DatabaseError`, base `sqlite3.Error`를 주입했다.
모든 경우 상위 타입은 `OrderRepositoryReadError`였고 원본 타입은 `__cause__`로 보존됐다. raw
SQLite escape는 0건이었다. 상위 메시지에는 SQL, binding, row, account 또는 payload가 없다.

### Not-found와 read failure 구분

```text
load(missing) = None
load_events(missing) = []
query failure = OrderRepositoryReadError
```

read failure가 None 또는 빈 이벤트 목록으로 변환되는 경로는 발견하지 않았다.

### 취소 대상 read failure

실제 존재하는 취소 대상과 유효 audit ID를 구성하고 대상 order SELECT만 실패시켰다.

```text
transport calls = 0
exception reason_code = STATE_READ_FAILURE
events = [SHADOW_ERROR]
SHADOW_BLOCKED = 0
SHADOW_COMPLETED = 0
terminal count = 1
operator alert = 1
```

정상 not-found는 transport 0, `SHADOW_BLOCKED` 1건을 유지해 정책 거절과 저장소 장애가 구분됐다.

## 4. CODEX-058 connection invalidation

rollback 실패 시 connection은 close 시도 전에 identity registry에 표시된다. registry는 object
reference 자체를 value로 유지하므로 표시된 object가 살아 있는 동안 `id()`가 재사용되지 않는다.
정상 connection의 false invalidation은 없었고 invalidated connection은 임의 복구되지 않는다.

registry는 process lifetime 동안 항목을 제거하지 않으므로 이론상 누적되지만, 정상 설계에서는
close까지 실패한 첫 fatal에서 즉시 process fail-stop하고 restart 시 module state가 초기화된다.

invalidated connection으로 `load`, `load_events`, `append_creation_event`, `advance`,
`compare_and_set_state`와 idempotency read를 시도한 집중 테스트는 SQL 실행 전에
`OrderRepositoryConnectionInvalidatedError`를 발생시켰다.

### Rollback/close 결과

```text
commit fail + rollback success:
OrderRepositoryTransactionError, transaction closed, later writer success

commit fail + rollback fail + close success:
OrderRepositoryRollbackError, connection unusable, later writer success

commit fail + rollback fail + close fail:
FatalRepositoryConnectionError, HALT set, connection registry-invalidated
```

close 실패 alert는 CRITICAL 문구와 process restart 필요를 포함하고 rollback 원인은 exception
chain에 보존된다. 정상 success return은 없다. 단, CODEX-059 때문에 cancel 실제 호출 경로의
fatal 전달은 실패한다.

## 5. 5개 entrypoint와 systemd

각 entrypoint work function에 `FatalRepositoryConnectionError`를 직접 주입한 독립 결과:

```text
run_live_buy_entry.main() = 4
run_reconciliation.main() = 4
run_shadow_mode.main() = 4
run_shadow_exit_evaluation.main() = 4
run_health_report.main() = 4
```

각 handler는 CRITICAL alert를 시도하고 정상 0으로 종료하지 않았다. 관련 5개 systemd unit은
모두 `Restart=on-failure`이며 exit 4를 restart 방지 목록에 두지 않아 재시작 대상이다.

그러나 이것은 fatal을 entrypoint에 직접 주입한 대조군이다. 실제 cancel 내부에서 fatal이
CODEX-059처럼 변환되면 이 handler에 도달하지 못하므로 end-to-end 결과는 FAIL이다.

## 6. 실제 다중 프로세스 DB lock

실제 SQLite 파일과 별도 Python process를 사용했다.

대조군:

```text
process A: BEGIN IMMEDIATE + INSERT, process alive
process B: BEGIN IMMEDIATE -> OperationalError(database is locked)
```

장애군:

```text
process A: real write transaction + injected commit/rollback/close failure
repository result: FatalRepositoryConnectionError
process exit code: 4
HALT: true
process A 종료 후 process B BEGIN IMMEDIATE/write/commit: success
```

따라서 fatal process가 실제로 종료되면 OS가 lock을 회수하고 영구 lock은 없다. 차단점은 이
종료 메커니즘이 cancel actual call chain에서 CODEX-059 때문에 호출되지 않는다는 것이다.

## 7. Order connection과 audit connection

close 성공 invalidation 경로에서는 poisoned order connection 재사용 없이 별도 connection으로
`SHADOW_ERROR` terminal, alert, HALT 기록이 가능했고 terminal은 최대 1건이었다.

동일 DB lock 때문에 audit 저장이 실패하는 경우에도 `_finalize_cancel(..., best_effort=True)`는
추가 alert를 시도한다. 하지만 fatal은 반드시 원형대로 보존돼야 하는데 final-state handler가
먼저 `CancelPostTransportError`로 바꾸므로 이 항목은 end-to-end FAIL이다.

## 8. Redaction

raw SQLite 메시지에 가짜 계좌번호, CANO, App Key/Secret, token, Authorization, SQL, binding,
broker payload와 connection 표현을 삽입한 저장소 및 process 검증에서 다음 출력의 원문 노출은
0건이었다.

- `OrderRepositoryReadError`
- `FatalRepositoryConnectionError`
- 운영 alert
- Shadow audit/HALT 기록
- 일반 로그 및 process stderr

독립 stderr에서도 planted account, secret, SQL payload 검색 결과는 모두 0건이었다.

## 9. 기존 CODEX-042~056 회귀

집중 범위에서 다음을 재확인했다.

- Alpaca 실제 주문 socket 0회
- KIS central authorization/gate 우회 0건
- reconciliation 실패 transport 0회, UNKNOWN account-wide 차단
- 부분체결 정확성, 고위험 exit 기본 false
- CAS 밖 상태 UPDATE 0건, `update_status` API 부재
- audit ID 필수, terminal exactly once
- cancel pre-transport `SHADOW_BLOCKED`, post-transport `SHADOW_ERROR`
- UNKNOWN fallback raw SQLite 정규화
- commit 실패 + rollback 성공 경로 정상
- full SHA exact match, secret redaction
- Shadow entry/exit 실제 주문 0회

기존 기능 회귀는 발견하지 않았다. 신규 CODEX-059가 최종 판정을 차단한다.

## 10. 테스트 결과

### 집중 안전 테스트

구현자 범위보다 넓게 관련 파일을 새로 선택했다.

```text
1704 passed
0 failed
0 skipped
0 xfailed
1 warning
66.23s
```

### 정방향 전체

```text
2223 passed
0 failed
0 skipped
0 xfailed
2 warnings
72.48s
```

### 역방향 전체

```text
2223 passed
0 failed
0 skipped
0 xfailed
2 warnings
75.28s
```

경고는 local LibreSSL/urllib3 호환 경고와 의도된 unsupported scanner-field 경고다.

## 11. 네트워크, 운영 파일, artifact

저장소 밖 socket guard가 `socket.connect`, `connect_ex`, `create_connection`을 차단·기록하는
상태에서 집중 및 전체 정·역순 테스트와 독립 process probe를 실행했다.

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
DB/WAL/SHM, 임시 env/lock/log와 socket guard는 검증 후 저장소 밖에서 제거했다.

## 12. 남은 MEDIUM 및 Oracle read-only

`price_field_last`, `cancel_tr_id_live`는 공식 reference 확인 완료/실제 KIS 응답 미확인 상태이며
실주문 전 Oracle read-only 또는 모의투자 확인이 필요하다. 그러나 현재는 신규 HIGH CODEX-059가
존재해 남은 조건이 외부 확인뿐이 아니다. Oracle read-only 단계는 허용하지 않는다.
