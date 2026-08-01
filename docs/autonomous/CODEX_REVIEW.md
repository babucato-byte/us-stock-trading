# CODEX_REVIEW — KIS 최종 독립 재검증

## 1. 검증 대상 고정

검증자: 구현자 완료 보고와 이전 Codex 결과를 재사용하지 않은 독립 검증

검증 시작 시 실행 결과:

```text
$ git status --short
(no output — clean)

$ git branch --show-current
feature/kis-live-broker

$ git rev-parse HEAD
c30f867c2522917833a5232a99f84d4b24869cb9

$ git show --stat --oneline HEAD
c30f867 CODEX-052: reconcile the KIS wire-format verification documentation
 brokers/kis_broker.py                           | 112 ++++++++++++++--
 docs/deployment/ORACLE_KIS_MIGRATION_RUNBOOK.md |  36 ++++-
 tests/test_kis_verification_matrix.py           | 168 ++++++++++++++++++++++++
 3 files changed, 303 insertions(+), 13 deletions(-)

$ git diff --check
(no output — pass)
```

필수 조건은 모두 일치했다. `TARGET_COMMIT_MISMATCH`가 아니므로 코드 검증을 진행했다.

collection도 새로 확인했다.

```text
2103 tests collected in 2.91s
```

## 2. 최종 판정

Overall verdict: **BLOCKED**

Oracle read-only deployment: **허용하지 않음 (현재 HEAD 기준)**

Real-order activation: **금지**

CODEX-052와 기존 CODEX-042~051 회귀는 통과했다. CODEX-053도 신규 buy/sell의 필수 audit
context와 transport 전 durability는 해결됐다. 그러나 cancel 성공/실패 lifecycle이 같은
`audit_run_id`의 terminal event 없이 끝난다.

독립 probe의 실제 cancel 결과:

```text
cancel transport 시점:
state = CANCEL_PENDING
events = [GATE_APPROVED, EXECUTION_PLANNED]

cancel 성공 반환 후:
events = [GATE_APPROVED, EXECUTION_PLANNED]
```

`SHADOW_COMPLETED`, block terminal event 또는 `SHADOW_ERROR`가 없다. 지시문의 “취소도 동일한
감사 lifecycle” 조건과 `BLOCKED` 기준인 “감사 생략/취소 감사 순서 미강제”에 해당한다.

## 3. CODEX-053 — audit_run_id 필수화

Status: **PARTIALLY_RESOLVED / BLOCKING REMAINDER**

### 통과한 항목

- `submit_buy_order`, `submit_sell_order`, `_submit_new_order`, `submit_cancel`의
  `audit_run_id`는 모두 default 없는 keyword-only 필수 인자다.
- non-test 운영 signature의 `audit_run_id=None`은 0건이다.
- `validate_audit_run_id()`는 None, empty, whitespace, int/float/bool/list/dict/object를
  `AUDIT_CONTEXT_MISSING`으로 차단한다.
- `_submit_new_order()`와 `submit_cancel()`은 idempotency/state/gate/authorization/transport보다
  먼저 ID를 검증한다.
- 엔진이 audit ID를 `uuid4`, `token_hex`, `audit_run_id or ...`로 자동 생성하지 않는다.
  ID는 buy/sell pipeline 시작점에서 생성되어 engine과 terminal event로 전달된다.
- operational engine caller의 `audit_run_id` 누락은 0건이다. 의도적 TypeError 테스트 한 곳만
  허용 예외로 존재한다.
- 유효 buy/sell pipeline은 동일 ID로 `SIGNAL_RECEIVED`, `GATE_APPROVED`,
  `EXECUTION_PLANNED`, `SHADOW_COMPLETED`를 기록한다.
- 새 주문 순서는 다음과 같이 코드와 별도-connection durability test에서 확인됐다.

```text
CAS APPROVED
-> durable GATE_APPROVED
-> CAS SUBMITTING
-> durable EXECUTION_PLANNED
-> broker transport
```

- cancel transport 시점에도 별도 DB connection으로 `CANCEL_PENDING`, `GATE_APPROVED`,
  `EXECUTION_PLANNED`이 이미 보였다.
- audit persistence 실패는 transport 0회와 `AUDIT_PERSISTENCE`로 차단된다. pipeline의 audit
  failure handler는 terminal `SHADOW_ERROR` 재시도와 운영 alert를 수행한다.

### 독립 probe: 누락/재시도 대조군

```text
invalid_reason AUDIT_CONTEXT_MISSING
invalid_transport 0
invalid_row None
control_status ACCEPTED control_transport 1
```

무효 ID가 idempotency key를 점유하지 않았고, 같은 order/signal에 유효 ID를 준 후 fake transport
1회로 정상 제출됐다.

### 미해결 항목

`execution_engine.submit_cancel()`은 `GATE_APPROVED`, `EXECUTION_PLANNED`까지만 기록한다.
성공 시 `CANCELLED` state를 반환하지만 terminal Shadow audit를 기록하지 않는다. ambiguous,
broker error, gate block 경로도 이 함수 또는 다른 operational cancel caller가 같은 run ID로
terminal block/error event를 보장하지 않는다. 저장소 non-test call graph에는 `submit_cancel()`의
operational caller도 없다.

신규 테스트 `test_cancel_audits_its_approval_before_the_transport` 역시 기대값을 정확히 두 event로
고정해 이 누락을 검출하지 않는다.

필수 조치:

1. cancel orchestration owner를 명확히 하고 성공에는 `SHADOW_COMPLETED`, gate/persistence block에는
   terminal blocked outcome, ambiguous/error에는 `SHADOW_ERROR`를 같은 `audit_run_id`로 기록한다.
2. success, gate rejection, CAS conflict, audit failure, ambiguous transport 각각에 terminal event
   exactly-once test를 추가한다.
3. 취소 평가가 시작됐지만 terminal event가 없는 run이 `runs_without_terminal_event()`에 남지
   않도록 end-to-end로 강제한다.

## 4. CODEX-052 — KIS 검증 상태 문서 일관성

Status: **RESOLVED_WITH_EXTERNAL_CONFIRMATION_PENDING**

- `REFERENCE_VERIFIED`와 `LIVE_RESPONSE_PENDING`이 독립 축으로 명시됐다.
- `WireValueVerification` matrix는 name, 실제 value, reference status, live status, source를
  갖는다.
- `price_field_last=output.last`와 `cancel_tr_id_live=TTTT1004U`가 공식 reference source 및
  live pending 상태로 명시됐다.
- 런북은 현재가 field는 Oracle read-only quote 응답, cancel TR_ID는 실계좌가 아닌 KIS
  모의투자 주문/취소로 확인하도록 구체적으로 안내한다.
- `TBD_VERIFY_LIVE_DOCS`는 운영 코드와 런북에서 제거됐다.
- AST 검사에서 `VERIFICATION_MATRIX`와 `LIVE_RESPONSE_PENDING_ITEMS`는 주문 분기나 runtime
  feature flag로 사용되지 않는다.
- 이번 문서 변경에서 TR_ID, endpoint, quotation/order exchange code, `output.last`, cancel
  payload field 값은 바뀌지 않았다. 관련 테스트가 값을 literal로 고정한다.

남은 MEDIUM 외부 조건:

| Item | Reference | Live response | 해소 방법 |
|---|---|---|---|
| `price_field_last` | `REFERENCE_VERIFIED` | `LIVE_RESPONSE_PENDING` | Oracle read-only quote 응답 확인 |
| `cancel_tr_id_live` | `REFERENCE_VERIFIED` | `LIVE_RESPONSE_PENDING` | KIS 모의투자 주문/취소 확인; 실계좌 주문 금지 |

공식 예제와 코드 값의 명백한 충돌은 발견하지 않았다. 두 항목 자체는 코드 CRITICAL/HIGH가
아니지만 실주문 활성화 전 반드시 확인하고 변경 시 재검증해야 한다.

## 5. 기존 CODEX-042~051 회귀

| Finding | 결과 | 독립 확인 요약 |
|---|---|---|
| CODEX-042 | PASS | Alpaca direct/wrapper/alias order 목적은 final request boundary에서 차단; recording HTTP 0회 |
| CODEX-043 | PASS | KIS submit/cancel은 single-use central authorization 없이는 transport 0회; HALT 신규 주문 0회 |
| CODEX-044 | PASS | KIS read failure, mismatch, account-wide UNKNOWN, stale/wrong account/symbol snapshot 모두 transport 0회 |
| CODEX-045 | PASS | 2주 중 1주 fill은 PARTIALLY_FILLED; 잔여 1주만 관리하고 2주 재매도 없음 |
| CODEX-046 | PASS | partial/trailing/time/EOD 기본값 각각 false, 독립 enable |
| CODEX-047 | PASS | status mutation은 expected state/version CAS repository만 사용; row/event 동일 transaction |
| CODEX-048 | PASS for new orders | APPROVED/audit/SUBMITTING-or-CANCEL_PENDING/audit/transport 순서 및 audit failure fail-closed 확인. Cancel terminal 누락은 CODEX-053 remainder로 별도 차단 |
| CODEX-049 | PASS | entry/exit Shadow service 주문 호출 없음, live unit 기본 disabled, installer가 live enable/start 안 함, migration/preflight/reconciliation/shadow 절차 존재 |
| CODEX-050 | PASS | account/CANO/key/secret/token/Bearer/raw response/Python repr redaction 통과 |
| CODEX-051 | PASS | full lowercase 40-char SHA exact equality와 commit-object 존재 검사; short/invalid/ref/mismatch 거부 |

CODEX-051 독립 probe:

```text
1자리 prefix       rejected
7자리 short SHA    rejected
39자리             rejected
uppercase          rejected
HEAD               rejected
empty              rejected
```

코드는 41자리, whitespace, 존재하지 않는 SHA, 다른 full SHA도 거부하며 관련 negative tests가
통과했다.

CODEX-050 독립 probe:

```text
input:
Authorization: Bearer token-123456 {'CANO':'12345678','appsecret':'secret-x'}

output:
Authorization: ***REDACTED*** {'CANO':'***REDACTED***','appsecret':'***REDACTED***'}
```

## 6. 독립 probe 및 네트워크 차단

probe는 `/private/tmp`/OS temp directory에 작성했고 저장소 코드는 수정하지 않았다. 먼저 유효
audit ID 대조군이 fake transport 평가점까지 실제 1회 도달함을 확인한 뒤 무효 ID를 주입했다.

별도의 `/private/tmp` `sitecustomize` socket guard로 `socket.connect`, `connect_ex`,
`create_connection`을 기록·차단한 상태에서 집중 및 전체 정·역순 테스트를 실행했다.

```text
socket guard log: absent
actual TCP/HTTP connect attempts: 0
Alpaca: 0
KIS: 0
Slack: 0
other external socket: 0
```

fake session/broker의 in-memory 호출은 각 safety assertion의 대조군으로만 사용했으며 외부
network가 아니다. Shadow entry/exit tests는 read/evaluation 경로를 실행하면서 state-mutating
broker call 0회를 확인한다.

probe source, socket guard와 probe DB/WAL/SHM/lock 디렉터리는 검증 후 모두 제거했다.

## 7. 테스트 결과

### 집중 안전 테스트

구현자 보고 766건보다 넓게 broker/config/authorization/engine/CAS/reconciliation/lifecycle/
Shadow/redaction/Oracle package 관련 1,026건을 명시적으로 수집해 실행했다.

```text
1026 passed
0 failed
0 skipped
0 xfailed
1 warning
33.96s
```

### 정방향 전체

```text
2103 passed
0 failed
0 skipped
0 xfailed
2 warnings
66.35s
```

### 역방향 전체

```text
2103 passed
0 failed
0 skipped
0 xfailed
2 warnings
67.18s
```

두 경고는 local LibreSSL/urllib3 호환 경고와 의도된 unsupported scanner-field 방어 경고다.
테스트 결과에는 영향을 주지 않았다. Oracle Python의 OpenSSL 1.1.1+ 확인은 실제 KIS HTTPS
read-only 단계 전 조건으로 유지한다.

## 8. 운영 파일 및 stray artifact

검증 전후 값은 동일하다.

| File | SHA-256 | Size | mtime |
|---|---|---:|---:|
| `order_history.csv` | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| `universe.csv` | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| `strategy_performance.csv` | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

- 새 repository-path `*.db`, WAL, SHM, journal: 없음
- 새 `shadow-*.jsonl`: 없음
- test log/env/probe artifact: 없음
- 테스트가 생성한 `/private/tmp`/OS temp probe artifact: 제거 완료
- 검증 전부터 존재하던 ignored zero-byte lock은 운영 파일 변경으로 계산하지 않았으며 삭제하지
  않았다.
- 보고서 수정 전 `git status --short`: clean
- 보고서 수정 전 `git diff --check`: pass

## 9. Oracle 및 실주문 조건

현재 HEAD는 CODEX-053 cancel terminal audit 누락 때문에 Oracle read-only 배포도 승인하지 않는다.
수정 후 동일 검증을 다시 통과해야 한다.

그 후에도 다음은 실주문 활성화 전 필수 외부 조건이다.

1. Oracle Python/OpenSSL 및 KIS read-only 인증·계좌·position/open-order/fill 조회 확인
2. `price_field_last` 실제 quote response 확인
3. `cancel_tr_id_live`와 cancel payload를 KIS 모의투자에서 확인
4. 결과에 맞춰 matrix status를 갱신하고 값이 다르면 코드 수정 후 독립 재검증
5. 그 전까지 Alpaca/KIS order flags와 live rollout/exit flags disabled, `ENTRY_DISABLED=true`,
   live service disabled 유지

코드·테스트·커밋·push·merge·배포·실주문·환경변수 활성화는 수행하지 않았다. 이 보고서 파일만
변경했다.
