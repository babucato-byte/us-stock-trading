# CODEX_REVIEW

Review target: Stage 3~10 final remediation independent revalidation

Commits: `07548d1`, `55f3806`, `8a3be50`, `9c43862`, `45cf8f9`

Validation package SHA-256: `a5ba04d79af3b73a145775f7fb6146a86a0b9cc1fe32f4818a97741beb8bf764`

Date: 2026-07-26

Overall verdict: **FAIL**

Stage 3~10: **KEEP_IN_PROGRESS**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

CODEX-032의 rejected-exit 원자성, CODEX-031의 durable reservation 및 trusted 30,000원 상한,
CODEX-033의 governance 상태 정정은 코드와 fault-injection 테스트에서 확인됐다. 전체 986개
테스트도 네 실행 형태에서 모두 통과했다. 그러나 live entry의 broker 응답이 유실되면
`AlpacaBroker.submit_order()`가 broker 수신 여부를 확인할 수 없는 상태에서도 reservation을
즉시 `RELEASED`한다. 동일 entry 재시도가 허용되어 broker call 2회, 잠재 노출 54,000원,
authoritative snapshot 27,000원으로 직접 재현됐다. 이는 현재 최종 주문 경계에서 중복 주문과
30K 한도 우회를 일으키는 신규 HIGH Finding이므로 진행할 수 없다.

## Finding summary

| Finding | Status |
|---|---|
| CODEX-024 | RESOLVED |
| CODEX-026 | PARTIALLY_RESOLVED |
| CODEX-028 | RESOLVED |
| CODEX-029 | RESOLVED |
| CODEX-030 | RESOLVED |
| CODEX-031 | PARTIALLY_RESOLVED |
| CODEX-032 | RESOLVED |
| CODEX-033 | RESOLVED |
| CODEX-034 HIGH — broker 응답 유실 시 live-entry reservation 해제로 중복 주문·30K 우회 | UNRESOLVED |

## Finding verification

### [CODEX-024]

Status: **RESOLVED**

Evidence:

- exit intent는 broker 호출 전에 position의 `EXIT_SUBMITTED` 전이와 같은 SQLite transaction으로 저장된다.
- timeout/unknown submission 후 재시도는 동일 client order ID로 broker를 조회하며 sell을 다시 제출하지 않는다.
- concurrent stop/target/time/EOD 경로는 active exit intent와 broker sell을 한 건으로 제한한다.
- accepted/new 상태는 fill로 분류되지 않아 `remaining_qty`와 PnL을 변경하지 않는다.
- explicit rejection은 이제 `mark_aborted(commit=False)`와 position `MANUAL_REVIEW` 전이를
  `store.locked_position(conn=conn)`의 한 transaction에서 commit한다.
- position event write와 `mark_aborted()` 각각의 실패 주입에서 양쪽 상태가 함께 rollback되고,
  active intent가 남아 reconciliation 가능함을 집중 테스트로 확인했다.

Remaining risk:

- entry-side response-loss reconciliation은 exit-intent와 별개이며 신규 CODEX-034로 기록한다.

### [CODEX-026]

Status: **PARTIALLY_RESOLVED**

Evidence:

- trusted code constant `PILOT_TOTAL_BUDGET_KRW=30_000`, 주문별 cap, 일일 entry 2건,
  동시 position 1건을 gateway가 caller 제공 상한과 `min()`으로 결합한다.
- caller가 3,000,000원을 선언해도 sizing은 30,000원 이하로 제한된다.
- durable SQLite reservation의 RESERVED/COMMITTED 상태는 budget과 concurrent position 계산에 포함된다.
- missing/stale FX, allow-list 외 symbol, fractional 미지원 및 context/order symbol mismatch는
  broker session 호출 전에 차단된다.
- `AlpacaBroker.submit_order()` 직접 호출도 동일 live-buy gate를 통과해야 한다.

Remaining risk:

- broker timeout/응답 유실을 definitive rejection과 구분하지 않고 reservation을 `RELEASED`한다.
  실제 broker가 주문을 수신했을 수 있는데 budget과 position count에서 제외되어 재시도 주문을 허용한다.
- 따라서 30K 및 pending/reserved 강제는 success/rejection 정상 경로에는 적용되지만 unknown-submission
  경로에는 fail-closed가 아니다(CODEX-034).

### [CODEX-028]

Status: **RESOLVED**

Evidence:

- SQLite `positions`, `position_events`, `exit_intents`가 position/fill/exit-intent의 canonical source다.
- JSON projection failure는 canonical transaction의 성공/실패 판정에 영향을 주지 않는다.
- partial fill 4 → JSON projection failure → cumulative fill 10 회귀는 `CLOSED`,
  `remaining_qty=0`, 전체 10주 기준 PnL로 통과한다.
- position, events, fill progress 및 rejected-exit abort 전이의 failure injection은 transaction 전체를 rollback한다.
- repeated/out-of-order reconciliation은 멱등적으로 처리된다.

Remaining risk:

- entry orders/fills가 canonical position transaction과 완전히 통합되지 않은 구조는 CODEX-034의
  unknown entry 상태에서 실제 결함으로 이어지므로 그 Finding에서 HIGH로 평가한다.

### [CODEX-029]

Status: **RESOLVED**

Evidence:

- context, strategy, sizing, reservation 및 payload symbol의 byte-exact 일치가 요구된다.
- context=AAPL, order/payload=TSLA와 case/whitespace mutation은 HTTP 호출 0회로 차단된다.
- direct `AlpacaBroker.submit_order()`도 allow-list, symbol, 30K context 없이 session에 도달하지 않는다.
- 현재 live order network method는 `submit_order()` 하나이며 final payload는 검증된 동일 symbol에서 구성된다.

Remaining risk:

- 향후 별도 주문 method가 추가되면 gate 적용을 보장하는 구조적 interface가 없다. 현재 우회 method가
  존재하지 않으므로 LOW future-maintenance risk다.

### [CODEX-030]

Status: **RESOLVED**

Evidence:

- lifecycle/EOD decision은 timezone-aware `now` 또는 injected Clock을 사용하고 naive datetime을 거부한다.
- FrozenClock 회귀가 장중, EOD 전후, premarket, DST spring/fall 및 UTC/ET 날짜 경계를 고정 입력으로 재현한다.
- 서로 다른 실제 시각에 수행한 네 전체 suite가 동일한 986 결과를 냈다.

Remaining risk: 없음.

### [CODEX-031]

Status: **PARTIALLY_RESOLVED**

Evidence:

- `live_entry_reservations` SQLite ledger가 snapshot-read와 reserve를 process lock 아래 원자화한다.
- caller가 risk limit/count를 늘려도 trusted 30K/1 position/2 daily entry ceiling을 넘길 수 없다.
- concurrent reservation 테스트에서 한 entry만 승인되고 다른 entry는 차단된다.
- released attempt도 당일 entry count에는 포함되고, RESERVED/COMMITTED notional은 budget에 포함된다.

Remaining risk:

- unknown submission에서 reservation을 해제하므로 durable ledger가 실제 broker exposure를 과소계상한다.
- reservation schema에는 broker reconciliation에 필요한 `client_order_id`/broker order identity가 없으며
  restart reconciliation 경로도 없다(CODEX-034).

### [CODEX-032]

Status: **RESOLVED**

Evidence:

- broker 422 rejection에서 exit intent `ABORTED`와 position `MANUAL_REVIEW`가 한 transaction으로 commit된다.
- position-event insert 실패 시 position은 `EXIT_SUBMITTED`, intent는 non-terminal active 상태로 함께 rollback된다.
- `mark_aborted()` 실패 시 position 전이도 commit되지 않는다.
- 실패 후 `check_and_manage()`/restart reconciliation은 sell을 재제출하지 않고 reconciliation-required로 유지한다.

Remaining risk: 없음.

### [CODEX-033]

Status: **RESOLVED**

Evidence:

- `docs/live_review/LIMITED_LIVE_REVIEW_CHECKLIST.md` 최종 상태가 `BLOCKED`로 수정됐다.
- `FINAL_VALIDATION_PACKAGE.md`, `CURRENT_STATUS.md`의 재검증 대기 상태와 일치한다.
- 과거 CODEX-016~022 판정이 현재 readiness 근거가 아님을 명시한다.

Remaining risk: 없음.

## New findings

### [CODEX-034] HIGH — broker 응답 유실 시 live-entry reservation을 해제해 중복 주문과 30K 우회 허용

Status: **UNRESOLVED**

Evidence:

- `broker/alpaca_client.py::AlpacaBroker.submit_order()`는 `_request()`의 모든 exception에서
  `_release_live_entry_reservation()`을 호출한다.
- timeout/connection reset은 broker가 주문을 받지 않았다는 definitive rejection이 아니다.
- `entry_reservation_ledger`는 `RESERVED`, `COMMITTED`, `RELEASED`만 가지며 submission-unknown 상태,
  `client_order_id`, broker order ID 또는 reconciliation API 연결이 없다.
- 코드 주석과 validation package도 response-loss 후 실제 exposure under-count 가능성을 명시하지만,
  이를 scope residual로 분류했다.

Direct reproduction:

1. isolated SQLite/kill-switch 파일과 recording session을 사용하고 실제 network는 사용하지 않았다.
2. AAPL 27,000원 live entry의 첫 session call이 “broker accepted, response lost” timeout을 반환하도록 했다.
3. 첫 결과는 `TimeoutError`; reservation은 즉시 `RELEASED`.
4. 동일 조건으로 두 번째 entry를 재시도하자 status 200, session call 총 2회.
5. ledger 최종 상태는 첫 27,000원 `RELEASED`, 둘째 27,000원 `COMMITTED`;
   authoritative snapshot은 27,000원/1 position만 인식했다.
6. 실제 broker가 첫 주문을 수신한 가정에서는 잠재 주문·노출은 2건/54,000원이다.

Impact:

- response-loss 및 process exception 후 동일 entry가 중복 제출될 수 있다.
- 30,000원 pilot total budget과 동시 position 한도가 실제 broker exposure에 대해 fail-open 된다.
- 이는 미래 order method 확장 위험이 아니라 현재 `AlpacaBroker.submit_order()`의 live entry exception 경로다.

Required behavior:

- broker 호출 전에 reconciliation 가능한 durable entry intent를 만들고 `client_order_id`를 저장한다.
- timeout/connection reset/프로세스 종료는 `RELEASED`가 아니라 submission-unknown/pending으로 유지해
  budget, daily entry, concurrent position 계산에 계속 포함한다.
- restart/retry 시 client order ID로 broker를 조회하여 accepted/new/partial/filled/rejected를 구분한 뒤
  definitive rejection에서만 release한다.
- “broker accepted then response lost → retry” 회귀에서 broker submit 총 1회, active reservation 유지,
  30K ceiling 불변을 검증한다.

## Regression

### CODEX-016~023, CODEX-025, CODEX-027

Status: **RESOLVED — no observed regression**

Evidence:

- trading mode/endpoint/credential revalidation, RequestPurpose와 purpose-side-payload consistency,
  binary/4-state Kill Switch, notification health, entry intent, strict fill validation,
  accepted-vs-filled 및 corrupted SQLite fail-closed 회귀가 통과했다.
- 손상 SQLite는 빈 position으로 처리되지 않고 recovery escalation/new entry 차단 경로로 이동한다.

## Executed tests

- exit/live-order/position/SQLite/broker 집중 9개 파일:
  **294 passed, 0 failed, 1 warning**
- 저장소 루트 `venv/bin/python -m pytest -q`:
  **986 passed, 0 failed, 2 warnings**
- 저장소 루트 `venv/bin/pytest -q`:
  **986 passed, 0 failed, 2 warnings**
- 저장소 상위 `us-stock-trading/venv/bin/python -m pytest us-stock-trading -q`:
  **986 passed, 0 failed, 2 warnings**
- 저장소 상위 `us-stock-trading/venv/bin/pytest us-stock-trading -q`:
  **986 passed, 0 failed, 2 warnings**
- direct response-loss reproduction: first reservation `RELEASED`, second broker submit allowed,
  session calls 2, ledger-recognized notional 27,000원.
- `git diff --check`: 통과.

Warnings review:

- `urllib3`의 macOS LibreSSL `NotOpenSSLWarning` 1건은 test/runtime 환경 호환 경고이며 주문 판정과 무관하다.
- unsupported scanner field를 의도적으로 skip하는 회귀 테스트의 `RuntimeWarning` 1건은 기대된 경고다.
- 안전성과 직접 관련된 신규 warning은 없다.

## Concurrency verification

- concurrent exit와 stop/target 동시 실행은 active intent/broker sell 한 건으로 제한된다.
- rejected-exit shared transaction 양방향 failure injection이 rollback된다.
- concurrent live-entry snapshot/reserve lock은 정상 경로에서 한 entry만 승인한다.
- unknown submission을 `RELEASED`하는 CODEX-034 때문에 timeout 이후의 순차 또는 재시작 retry는
  concurrency lock과 무관하게 허용된다.

## SQLite consistency verification

- canonical partial/cumulative fill, PnL, event rollback 및 JSON projection 장애 회귀가 통과했다.
- rejected exit의 intent/position transition도 한 SQLite transaction으로 확인됐다.
- response-loss entry reservation은 DB 손상이 아니라 application이 명시적으로 `RELEASED`로 저장한
  잘못된 authoritative state이므로 store 무결성 검사로 탐지되지 않는다.

## Order boundary verification

- symbol mismatch, missing context, allow-list, stale FX 및 caller-inflated limits는 HTTP 전에 차단된다.
- direct broker entry도 동일 gate를 통과한다.
- 정상 broker response 경로에서는 reservation이 budget/count를 강제한다.
- timeout/response-loss 경로에서는 final boundary 자체가 reservation을 해제해 retry를 허용한다(CODEX-034).

## Clock determinism

- FrozenClock 기반 lifecycle/EOD/DST 회귀가 통과했다.
- 네 전체 실행 형태가 동일한 986개 결과를 냈다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 또는 기타 외부 API 호출은 수행하지 않았다.
- 모든 HTTP 검증과 신규 timeout 재현은 fake broker/recording session을 사용했다.
- 테스트 suite의 Slack/network forbidden spy 및 broker session call-count 검증이 통과했다.

## Operational file safety

- `order_history.csv`: SHA-256
  `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`,
  31 bytes, mtime `1784558966`, 전후 불변.
- `universe.csv`: SHA-256
  `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`,
  833518 bytes, mtime `1784558966`, 전후 불변.
- root `TRADING_STATE.db*`, `POSITION_STORE.json`, `LIVE_ENTRY_RESERVATION.lock`, `.env`는 생성되지 않았다.
- `strategy_performance.csv` content SHA-256
  `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`,
  69 bytes로 유지됐다. 이번 실행 전 mtime baseline은 별도 캡처하지 않아 mtime 불변은 검증하지 못했다.
- `LIVE_APPROVAL_RECORD.md`: SHA-256
  `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`,
  `approved: false`, `live_enabled: false` 불변.
- main `158671e`, 검증 branch HEAD `45cf8f9`; merge/push/deploy 없음.
- 보고서 갱신 전 working tree는 clean이었고, 구현 파일은 수정하지 않았다.

## Residual risks

1. entry orders/fills와 canonical position SQLite의 미통합은 response-loss 후 실제 중복 주문,
   budget under-count 및 상태 불일치로 직접 이어져 **HIGH (CODEX-034)** 다.
2. 향후 추가될 다른 order method가 gateway를 구조적으로 상속하지 않는 점은 현재 다른 live submit
   method가 없어 **LOW future-maintenance risk** 다.
3. 일반 주문 오류의 자동 `ENTRY_DISABLED` 전환은 계속 `NEEDS_USER_DECISION`이다.
4. 실제 FX provider, broker minimum/fractional policy, live market data 및 실계좌 reconciliation은 미검증이다.

## Required next action

1. CODEX-034를 해결한다: entry intent/client order identity를 broker 호출 전에 durable하게 예약하고,
   ambiguous timeout/crash에서는 release하지 말고 restart reconciliation 전까지 모든 risk count에 포함한다.
2. broker의 definitive rejected/canceled 상태가 확인된 경우에만 reservation을 release한다.
3. response-loss, process restart, repeated/out-of-order reconciliation 및 multiprocessing retry 회귀를 추가한다.
4. 재검증 전 Stage 3~10 `KEEP_IN_PROGRESS`, limited live review `BLOCKED`,
   live trading `DO_NOT_ENABLE`을 유지한다.
