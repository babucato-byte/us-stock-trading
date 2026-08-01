# CODEX_REVIEW — CODEX-054 최종 독립 재검증

## 1. 검증 대상

검증 시작 시 실행 결과:

```text
$ git status --short
(no output — clean)

$ git branch --show-current
feature/kis-live-broker

$ git rev-parse HEAD
8fb936c86a0bc68d47ea315cff862840eeffb4ee

$ git show --stat --oneline HEAD
8fb936c Fix cancel post-transport audit classification
 execution/execution_engine.py        | 183 ++++++++++++++++++++++-------
 execution/order_repository.py        |  12 +-
 tests/test_cancel_audit_lifecycle.py | 219 +++++++++++++++++++++++++++++++++++
 3 files changed, 368 insertions(+), 46 deletions(-)

$ git diff --check
(no output — pass)
```

필수 branch, exact HEAD, clean working tree와 diff check 조건이 모두 일치했다. 테스트는 새로
수집했으며 결과는 `2139 tests collected in 2.72s`였다.

## 2. 최종 판정

Overall verdict: **BLOCKED**

CODEX-054 exact reproduction: **RESOLVED**

Oracle read-only deployment: **허용하지 않음 (현재 HEAD 기준)**

Real-order activation: **금지**

CODEX-054의 원래 결함은 해결됐다. 그러나 지시문이 별도로 요구한 raw SQLite UNKNOWN fallback과
rollback-failure 경로에서 신규 HIGH 두 건을 독립 probe로 재현했다.

### CODEX-055 — HIGH — raw SQLite UNKNOWN fallback 오류가 정규화·alert를 우회

최종 `CANCELLED` 저장이 raw `sqlite3.OperationalError`로 실패한 뒤 UNKNOWN fallback도 raw
SQLite 오류를 내면 fallback 오류가 그대로 전파된다.

```text
transport calls = 1
exception = OperationalError("raw fallback failure")
reason_code = None
durable state = CANCEL_PENDING
events = GATE_APPROVED -> EXECUTION_PLANNED -> SHADOW_ERROR
operator alerts = 0
```

terminal 분류 자체는 `SHADOW_ERROR` 1건으로 맞지만 다음 필수 조건을 위반한다.

- `reason_code=STATE_PERSISTENCE`로 명확히 정규화되지 않는다.
- 원래 final-state 오류가 UNKNOWN fallback의 raw 오류에 가려진다.
- 수동 reconciliation 필요를 명시하는 운영 alert가 발생하지 않는다.

원인은 `_force_unknown_reported()`가 `OrderStateTransitionError`와 `OrderRepositoryError`만 잡고
raw `sqlite3.Error`는 잡지 않는 것이다. 이 예외는 `_cancel_inner()`의 final-state except 블록
안에서 새로 발생하므로 같은 except에 다시 잡히지 않고 바깥으로 탈출한다.

### CODEX-056 — HIGH — rollback 실패 시 열린 write transaction과 DB lock 유지

`compare_and_set_state()`의 commit 실패 후 rollback도 실패하도록 한 독립 probe 결과:

```text
raised exception = OperationalError("commit failed")
connection.in_transaction = True
subsequent independent writer = OperationalError("database is locked")
operator alert = 없음
```

구현은 rollback 오류를 `except sqlite3.Error: pass`로 버린다. 따라서 지시문의 명시적 BLOCKED
조건인 “commit 실패 후 열린 transaction 유지”에 해당하며, rollback 실패 운영 alert와 명확한
persistence 상태도 제공하지 않는다.

## 3. CODEX-054 정확한 재현

저장소 테스트 helper에 의존하지 않는 `/private/tmp` production-only probe에서 final
`CANCELLED` write를 실패시키고 UNKNOWN write는 정상 동작하게 했다.

```text
transport calls = 1
transport 시 state = CANCEL_PENDING
transport 시 events = [GATE_APPROVED, EXECUTION_PLANNED]
최종 durable state = UNKNOWN
exception = CancelPostTransportError
reason_code = CANCEL_FINAL_STATE_PERSISTENCE
terminal = SHADOW_ERROR ×1
SHADOW_BLOCKED = 0
SHADOW_COMPLETED = 0
자동 재취소 = 0
```

같은 `audit_run_id`로 기록됐고 broker 결과는 `safe_repr()`를 거쳐 로그에 기록된다. 운영 alert는
reconciliation과 자동 재취소 금지를 명시한다. 정상 성공 반환은 없었다.

## 4. pre/post transport 분류

분류 기준은 공유 marker `transport["attempted"]`이며, broker 호출 직전에 true로 설정된다.

Pre-transport 경로인 gate 거절, 대상 없음/non-open, 계좌 불일치, 중복 cancel,
CANCEL_PENDING CAS 실패, GATE_APPROVED/EXECUTION_PLANNED 감사 저장 실패는 transport 0회다.
감사 저장소 자체가 해당 terminal도 저장 불가능한 경우를 제외하면 같은 run ID의
`SHADOW_BLOCKED` exactly once이며 ERROR/COMPLETED는 없다.

Post-transport timeout, connection reset, ambiguous, 명확한 broker 오류, 미확인 응답,
최종 state CAS/update/event/commit 실패는 `SHADOW_ERROR` exactly once이고 자동 재취소가 없다.
단, CODEX-055의 UNKNOWN fallback raw 오류는 terminal은 맞아도 예외 정책과 alert를 우회한다.

`CancelPostTransportError`는 실제 final-state persistence 경로에서 사용되며 UNKNOWN 성공 시
`CANCEL_FINAL_STATE_PERSISTENCE`, UNKNOWN도 저장하지 못한 정규화 경로에서는
`STATE_PERSISTENCE`를 의도한다. raw SQLite fallback은 이 정규화 경로에 도달하지 못한다.

## 5. DB rollback 검증

정상 rollback 가능한 commit 실패는 `compare_and_set_state()`가 rollback을 호출하여 후속
처리를 가능하게 하고 관련 테스트가 통과했다. state UPDATE 오류와 event INSERT 오류도 기존
transaction handler가 rollback한다.

그러나 rollback 자체 실패는 CODEX-056처럼 열린 transaction을 남기고 후속 감사/상태 writer를
lock out한다. 따라서 DB rollback 검증 전체는 **FAIL**이다.

필수 조치:

1. commit 실패 후 rollback 실패를 삼키지 말고 운영 alert와 persistence 오류 문맥을 남긴다.
2. transaction을 안전하게 해제할 수 없다면 오염된 connection을 폐기/격리하여 후속 writer가
   잠기지 않게 한다.
3. 독립 connection의 후속 write 성공을 테스트로 고정한다.

## 6. raw sqlite3.Error 검증

최종 state UPDATE/event/commit에서 직접 발생한 raw SQLite 오류는 outer cancel lifecycle까지
도달하여 `SHADOW_ERROR`로 종료된다. 하지만 UNKNOWN fallback에서 다시 발생한 raw SQLite 오류는
CODEX-055처럼 정규화와 alert를 우회하므로 이 항목은 **FAIL**이다.

필수 조치:

1. `_force_unknown_reported()`에서 raw `sqlite3.Error`도 “UNKNOWN 미저장” 결과로 보존한다.
2. 원래 confirmed-cancel persistence 오류와 fallback 오류를 모두 문맥에 남긴
   `CancelPostTransportError(reason_code=STATE_PERSISTENCE)`를 발생시킨다.
3. 수동 reconciliation 운영 alert와 `SHADOW_ERROR` exactly once를 보장한다.

## 7. terminal uniqueness 및 transport durability

성공은 `SHADOW_COMPLETED`, pre-transport 차단은 `SHADOW_BLOCKED`, 일반 post-transport 오류는
`SHADOW_ERROR`가 각각 정확히 1건이었다. exact CODEX-054와 raw fallback probe도 terminal은
`SHADOW_ERROR` 1건이며 zero/multiple terminal은 없었다. Migration 10 partial unique index와
`finalize_audit_run()`의 idempotent/conflict 처리는 기존 집중 회귀에서 통과했다.

fake broker 내부의 별도 connection에서 transport 직전 `GATE_APPROVED`,
`EXECUTION_PLANNED`, `CANCEL_PENDING`은 durable했고 terminal은 없었다. 반환 후 fresh connection에서
terminal 1건이 조회됐다.

## 8. 기존 CODEX-042~053 회귀

집중 범위에서 다음을 재확인했다.

- Alpaca 실제 주문 HTTP 0회
- KIS unauthorized direct submit/cancel transport 0회
- reconciliation 실패 transport 0회와 UNKNOWN account-wide 차단
- 부분체결 수량 정확성, 고위험 exit 기본 false
- CAS 밖 상태 UPDATE 0건, `update_status` API 부재
- `audit_run_id` 필수 및 terminal 최대 1건
- secret redaction, full 40-character SHA exact match
- Shadow entry/exit 실제 주문 0회

기존 finding의 기능 회귀는 발견하지 않았다. 신규 CODEX-055/056이 최종 판정을 차단한다.

## 9. 테스트 결과

### 집중 안전 테스트

구현자 범위보다 넓은 관련 파일을 새로 선택해 실행했다.

```text
1591 passed
0 failed
0 skipped
0 xfailed
1 warning
58.36s
```

### 정방향 전체

```text
2139 passed
0 failed
0 skipped
0 xfailed
2 warnings
67.77s
```

### 역방향 전체

```text
2139 passed
0 failed
0 skipped
0 xfailed
2 warnings
68.16s
```

경고는 local LibreSSL/urllib3 호환 경고와 의도된 unsupported scanner-field 경고다.

## 10. 네트워크, 운영 파일, artifact

저장소 외부 socket guard가 `socket.connect`, `connect_ex`, `create_connection`을 차단·기록하는
상태에서 집중 및 전체 정·역순 테스트와 probe를 실행했다.

```text
socket guard log = absent
Alpaca socket attempts = 0
KIS socket attempts = 0
Slack socket attempts = 0
other external socket attempts = 0
```

운영 파일의 검증 전후 SHA-256, size, mtime는 모두 동일하다.

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
DB/WAL/SHM, socket guard는 검증 후 저장소 밖에서 제거했다.

## 11. 남은 MEDIUM 및 Oracle read-only

기존 KIS 실제 응답 확인 항목(`price_field_last`, `cancel_tr_id_live`)은 남아 있다. 그러나 남은
항목이 외부 확인뿐이 아니며 신규 HIGH CODEX-055/056이 존재한다. 따라서 현재 HEAD에서 Oracle
read-only 단계는 허용하지 않는다.
