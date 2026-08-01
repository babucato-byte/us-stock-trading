# CODEX_REVIEW — KIS 취소 감사 lifecycle 최종 독립 재검증

## 1. 검증 대상 고정

검증 시작 시 실행 결과:

```text
$ git status --short
(no output — clean)

$ git branch --show-current
feature/kis-live-broker

$ git rev-parse HEAD
642fb65529dd24565a7ee7549652c54e4d1eea98

$ git show --stat --oneline HEAD
642fb65 Complete the terminal audit lifecycle for cancel execution
 brokers/kis_broker_adapter.py         |  32 +-
 execution/execution_engine.py         | 157 ++++++++-
 kis_live_trading.py                   |  37 ++-
 scripts/run_shadow_exit_evaluation.py |  13 +-
 shadow_audit.py                       | 103 ++++++
 state_store/migrations.py             |   4 +-
 state_store/schema.py                 |  25 ++
 tests/test_audit_context_required.py  |   5 +-
 tests/test_cancel_audit_lifecycle.py  | 576 ++++++++++++++++++++++++++++++++++
 tests/test_shadow_audit_durability.py |  37 ++-
 10 files changed, 958 insertions(+), 31 deletions(-)

$ git diff --check
(no output — pass)
```

필수 branch, exact HEAD, clean working tree, diff check 조건이 모두 일치했다. 새 수집 결과는
`2127 tests collected in 2.77s`였다.

## 2. 최종 판정

Overall verdict: **BLOCKED**

Oracle read-only deployment: **허용하지 않음 (현재 HEAD 기준)**

Real-order activation: **금지**

신규 HIGH finding **CODEX-054**가 있다. KIS가 취소를 확인한 뒤 최종 `CANCELLED` 상태 저장이
실패하는 경로는 주문을 `UNKNOWN`으로 보존하지만 같은 run의 terminal을 `SHADOW_BLOCKED`로
기록한다. 검증 지시문은 이 “최종 상태 저장 실패”를 오류 lifecycle로 명시하며
`SHADOW_ERROR` exactly once를 요구한다.

```text
독립 probe 결과
transport calls: 1
durable order state: UNKNOWN
caller exception: ExecutionEngineError(reason_code=STATE_PERSISTENCE)
events: GATE_APPROVED -> EXECUTION_PLANNED -> SHADOW_BLOCKED
expected terminal: SHADOW_ERROR
```

원인은 `execution_engine.submit_cancel()`의 `except ExecutionEngineError`가 transport 이전 차단과
transport 이후 최종 상태 저장 실패를 구분하지 않고 모두 `SHADOW_BLOCKED`로 finalize하기 때문이다.

필수 조치:

1. transport 도달 여부 또는 명시적 오류 유형을 기준으로 최종 상태 저장 실패를
   `SHADOW_ERROR`로 finalize한다.
2. KIS confirmed cancel + `CANCELLED` CAS/persistence failure 조합을 독립 테스트로 고정한다.
3. 수정 후 이 지시문의 전체 정·역순 및 독립 probe를 다시 실행한다.

## 3. 취소 성공 lifecycle

성공 경로는 PASS다. fake broker 내부의 별도 DB connection에서 transport 시점에
`GATE_APPROVED`, `EXECUTION_PLANNED`, `CANCEL_PENDING`이 durable했고 terminal event는 없었다.
broker 반환 및 상태 저장 뒤 fresh connection에서 같은 `audit_run_id`의
`SHADOW_COMPLETED`가 정확히 1건이었다.

```text
transport: events=[GATE_APPROVED, EXECUTION_PLANNED], state=CANCEL_PENDING
return: status=CANCELLED
final events=[GATE_APPROVED, EXECUTION_PLANNED, SHADOW_COMPLETED]
terminal count=1
```

성공 terminal 저장 실패는 정상 성공으로 반환하지 않고
`ExecutionEngineError(reason_code=AUDIT_PERSISTENCE)`를 발생시키며, 이미 확인된
`CANCELLED` durable state를 숨기지 않는 테스트가 통과했다.

## 4. 취소 차단 lifecycle

gate 거절, 대상 없음, non-open 상태, 계좌 불일치, 중복 cancel, CAS 충돌,
`GATE_APPROVED` 저장 실패, `EXECUTION_PLANNED` 저장 실패는 transport 0회와 같은 run ID의
terminal exactly once를 확인했다. 독립 probe의 gate/account/duplicate 경로는 각각
`[SHADOW_BLOCKED]`, transport 0회였다.

누락·빈 문자열·공백·비문자열 audit context는 `AUDIT_CONTEXT_MISSING`으로 idempotency 점유,
상태 전이, transport 전에 차단된다.

차단 terminal 저장 실패는 재시도와 운영 alert 후 원래 차단 예외를 보존하는 테스트가 통과했다.

## 5. 취소 오류 lifecycle

다음은 PASS다.

| 경로 | transport | durable state | terminal |
|---|---:|---|---|
| ambiguous timeout | 1 | UNKNOWN | SHADOW_ERROR ×1 |
| connection/broker error | 1 | UNKNOWN | SHADOW_ERROR ×1 |
| 미확인 broker status | 1 | UNKNOWN | SHADOW_ERROR ×1 |

각 경로는 자동 재취소하지 않았고 같은 audit run ID를 사용했다. 오류 terminal 저장 실패도
재시도·alert 후 원래 broker 예외를 보존했다.

최종 상태 저장 실패만 FAIL이다. broker transport와 confirmed result 이후 발생했음에도
`SHADOW_BLOCKED`로 분류되어 오류 lifecycle 의미가 훼손된다.

## 6. terminal unique invariant 및 migration 10

`finalize_audit_run()` 검증 결과:

- 동일 terminal 재호출: idempotent no-op, DB terminal 1건
- 상충 terminal 재호출: `AuditInvariantError`, DB terminal 1건, alert 시도
- 서로 다른 terminal의 동시 finalize: 승자 1건, 패자는 durable 사실을 재조회한 뒤
  `AuditInvariantError`, corruption 0건
- 독립 probe integrity report: zero-terminal run `[]`, multiple-terminal run `[]`

Migration 10은 version 9 다음에 등록됐고 partial unique index
`idx_shadow_audit_terminal_once`를 생성한다.

독립 migration probe:

```text
fresh/current DB: schema version 10
v9 DB -> v10: success, legacy audit row count 1 preserved
duplicate-terminal v9 DB -> v10: StateStoreError, duplicate rows 2 preserved
repeat open/init: idempotent
```

즉 기존 정상 데이터는 보존되고, 중복 terminal 데이터는 조용히 삭제되지 않고 명확히 실패한다.

## 7. 기존 매수·매도 및 CODEX-042~053 회귀

매수 성공/차단/ambiguous, 매도 성공/차단/오류, Shadow exit 평가의 terminal exactly once 관련
테스트가 통과했다. 이전 매수 ambiguous 분기의 직접 `SHADOW_ERROR` + finally 중복 기록은 제거되어
finalizer 한 곳에서 종료된다. 운영 buy/sell/cancel finalization call site는
`finalize_audit_run()`을 사용한다.

기존 집중 회귀도 통과했다: Alpaca 주문 HTTP 0회, KIS unauthorized direct submit/cancel 0회,
reconciliation failure transport 0회, UNKNOWN account-wide 차단, 부분체결 수량 정확성,
고위험 exit 기본 false, CAS 밖 주문 상태 UPDATE 0건, `update_status` API 부재,
Shadow entry/exit 주문 0회, secret 원문 노출 0건, short SHA 승인 0건, KIS 검증 matrix 일관성.

`submit_buy_order`, `submit_sell_order`, `submit_cancel`, `_submit_new_order`의 audit ID는 필수이며
None/empty/whitespace/non-string을 상태 변경 전에 차단한다. 내부 자동 ID 생성 또는 조건부 감사
생략은 발견하지 않았다.

## 8. 독립 probe

저장소 밖 probe는 production module만 사용하고 저장소 테스트 helper에 의존하지 않았다.

```text
cancel success: CANCELLED, terminal SHADOW_COMPLETED ×1
cancel gate block: transport 0, terminal SHADOW_BLOCKED ×1
cancel account block: transport 0, terminal SHADOW_BLOCKED ×1
cancel duplicate: transport 0, terminal SHADOW_BLOCKED ×1
cancel ambiguous: transport 1, UNKNOWN, terminal SHADOW_ERROR ×1
cancel broker error: transport 1, UNKNOWN, terminal SHADOW_ERROR ×1
cancel unconfirmed: transport 1, UNKNOWN, terminal SHADOW_ERROR ×1
cancel final-state persistence failure: transport 1, UNKNOWN, terminal SHADOW_BLOCKED ×1 (FAIL)
concurrent conflicting finalize: DB terminal 1, losing writer AuditInvariantError
integrity report: zero=[], multiple=[]
```

매수 ambiguous 중복 회귀는 집중 테스트의 production pipeline 경로로 별도 확인했다. probe source,
DB/WAL/SHM, socket guard는 검증 후 저장소 밖에서 제거했다.

## 9. 테스트 결과

### 집중 안전 테스트

관련 50개 파일을 새로 수집·실행했다.

```text
1556 passed
0 failed
0 skipped
0 xfailed
1 warning
60.37s
```

### 정방향 전체

```text
2127 passed
0 failed
0 skipped
0 xfailed
2 warnings
68.12s
```

### 역방향 전체

```text
2127 passed
0 failed
0 skipped
0 xfailed
2 warnings
69.94s
```

추가 lifecycle/migration 집중 실행은 `77 passed, 0 failed, 0 skipped, 0 xfailed`였다.
경고는 local LibreSSL/urllib3 경고와 의도된 unsupported scanner-field 방어 경고다.

## 10. 네트워크, 운영 파일, stray artifact

저장소 외부 `sitecustomize` guard가 `socket.connect`, `connect_ex`, `create_connection`을
차단·기록하도록 한 상태에서 집중 및 전체 정·역순 테스트와 probe를 실행했다.

```text
socket guard log: absent
Alpaca actual calls: 0
KIS actual calls: 0
Slack actual calls: 0
other external socket attempts: 0
```

운영 파일은 전후 SHA-256, size, mtime가 모두 동일했다.

```text
order_history.csv
153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7 | 31 | 1784558966

universe.csv
9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3 | 833518 | 1784558966

strategy_performance.csv
ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8 | 69 | 1785083284
```

신규 저장소 artifact는 0건이다. 검증 전부터 존재한 ignored zero-byte lock
`KIS_ORDER_IDEMPOTENCY.lock`, `NOTIFICATION_HEALTH_STATE.lock` 두 개는 변경하지 않았으며,
probe 및 guard artifact는 모두 저장소 밖에서 제거했다.

## 11. 남은 MEDIUM 및 Oracle read-only

기존 외부 확인 항목인 KIS `price_field_last`와 `cancel_tr_id_live`의 실제 응답 검증은 여전히
남아 있다. 그러나 현재는 신규 HIGH CODEX-054가 존재하므로 “남은 항목이 실제 KIS 응답 확인뿐”
조건을 만족하지 않는다. 따라서 이 HEAD에서는 Oracle read-only 단계도 허용하지 않는다.
