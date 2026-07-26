# CODEX_REVIEW

Review target: Stage 3~10 최종 수정분 독립 재검증

Commits: `f04a123`, `aee663c`, `09b9237`, `b78e444`, `fe3e9b7`

Validation package SHA-256: `1dc78103ffa64757308136ae09292428698713788fca9628ded8bc1f82d82712`

Date: 2026-07-26

Overall verdict: **FAIL**

Stage 3~10: **KEEP_IN_PROGRESS**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

Clock 주입, accepted-vs-filled, timeout 중복 sell 차단, symbol identity 및 direct `AlpacaBroker.submit_order()` 게이트, partial-fill/JSON projection 장애의 SQLite canonical 처리는 재현됐다. 전체 973개 테스트도 네 실행 형태에서 모두 통과했다. 그러나 30,000원 상한과 일일·동시·pending 위험 수치는 신뢰할 수 없는 caller context가 직접 정하며 authoritative 저장소에서 산출되지 않는다. 실제로 300만원 context가 2,997,000원 주문을 승인했다. 또한 broker 명시적 rejection에서는 exit intent를 `ABORTED`로 먼저 commit한 뒤 position을 별도 commit하여 두 번째 write 실패 시 `EXIT_SUBMITTED` position과 terminal intent가 영구 불일치한다. 신규 HIGH Finding 2건이므로 진행할 수 없다.

## Finding summary

| Finding | Status |
|---|---|
| CODEX-024 | PARTIALLY_RESOLVED |
| CODEX-026 | PARTIALLY_RESOLVED |
| CODEX-028 | PARTIALLY_RESOLVED |
| CODEX-029 | RESOLVED |
| CODEX-030 | RESOLVED |
| CODEX-031 HIGH — 30K·count·pending limits가 caller assertion에 의존 | UNRESOLVED |
| CODEX-032 HIGH — rejection 시 ABORTED intent와 position commit 분리 | UNRESOLVED |
| CODEX-033 MEDIUM — limited-live checklist가 최신 FAIL과 모순 | UNRESOLVED |

## Finding verification

### [CODEX-024]

Status: **PARTIALLY_RESOLVED**

Evidence:

- fresh exit는 broker 호출 전 `exit_intents.RESERVED`와 position의 `EXIT_SUBMITTED`/`PARTIAL_EXIT_SUBMITTED`를 같은 SQLite transaction으로 commit한다.
- timeout 후 재시도는 기존 client order ID를 조회하며 sell을 다시 제출하지 않는다.
- stop/target 동시 실행과 concurrent exit 테스트에서 active intent 및 broker sell은 한 건이었다.
- restart recovery는 pending intent의 client order ID로 broker를 조회한다.
- accepted/new는 remaining quantity와 PnL을 변경하지 않는다.

Remaining risk:

- broker 명시적 rejection 경로는 `eil.mark_aborted()`를 독립 commit한 뒤 position을 별도 `locked_position()` transaction에서 `MANUAL_REVIEW`로 바꾼다.
- 두 번째 transaction 실패 시 terminal intent만 남고 position은 `EXIT_SUBMITTED`에 고정된다(CODEX-032).

### [CODEX-026]

Status: **PARTIALLY_RESOLVED**

Evidence:

- wrapper와 direct Alpaca broker live-buy 경계 모두 context 누락, allow-list 위반, symbol mismatch, stale/missing FX 및 context가 보고한 count/limit 위반을 HTTP 전에 차단한다.
- gateway가 산출한 qty로 caller qty를 대체한다.
- paper order와 liquidation은 live entry gate에 막히지 않는다.

Remaining risk:

- 30,000원 ceiling, available cash, 일일 entry count, open position count, pending/reserved exposure가 authoritative 저장소에서 계산되지 않는다.
- production code에는 `LiveEntryContext` 생성기나 durable-state snapshot builder가 없고 모든 값은 주문 caller가 제공한다.
- pending/reserved 주문을 합산하는 코드와 테스트도 없다.
- `max_order_notional_krw` 자체에 30,000원 절대 상한이 없다(CODEX-031).

### [CODEX-028]

Status: **PARTIALLY_RESOLVED**

Evidence:

- `positions`, `position_events`, `exit_intents`가 동일 SQLite DB에 있으며 JSON은 projection으로만 사용된다.
- partial fill 4 → JSON projection 실패 → cumulative fill 10 회귀는 `CLOSED`, `remaining_qty=0`, 10주 전체 PnL로 통과한다.
- position + position events + exit fill progress는 shared connection 및 `commit=False`를 사용해 한 transaction으로 commit한다.
- DB commit failure는 position/event mutation을 rollback하고 JSON failure는 canonical SQLite를 변경하지 않는다.
- repeated/out-of-order reconciliation 회귀가 통과한다.

Remaining risk:

- “exit intent와 position이 항상 동일 transaction”이라는 주장은 rejection/abort 경로에는 적용되지 않는다.
- terminal `ABORTED`가 position보다 먼저 commit되는 실제 inconsistency가 CODEX-032로 재현됐다.
- entry `orders`/`fills` 테이블은 여전히 canonical flow에 연결되지 않았으며 CSV/ledger와 SQLite 사이 단일 transaction이 없다. 이번 직접 재현은 exit rejection이므로 단순 미래 위험으로만 볼 수 없다.

### [CODEX-029]

Status: **RESOLVED**

Evidence:

- `validate_and_size_live_entry(ctx, order_symbol)`이 context symbol과 실제 order symbol의 byte-exact 일치를 요구한다.
- AAPL context + TSLA order, 대소문자·공백 변형, 빈/None symbol은 wrapper와 direct broker 양쪽에서 차단된다.
- `AlpacaBroker.submit_order()` 자체가 live buy gateway를 실행하므로 wrapper 우회에서도 session 호출은 0회다.
- broker가 구성하는 final payload symbol은 검증에 사용된 동일 `symbol` 인자에서 파생된다.

Remaining risk:

- 향후 별도 주문 제출 메서드가 추가되면 자동으로 이 gate를 상속하지 않는다. 현재 코드에 다른 live order network method가 없어 미래 확장 위험은 LOW 조건으로 기록한다.

### [CODEX-030]

Status: **RESOLVED**

Evidence:

- `check_and_manage()`와 `check_invalidation()`은 timezone-aware `now` 또는 injected Clock을 사용하며 naive datetime을 거부한다.
- `FrozenClock` 테스트가 정규장, EOD 직전/정확한 cutoff/이후, premarket, DST spring/fall 및 UTC/ET 날짜 경계를 고정 입력으로 재현한다.
- 이전 wall-clock 의존 lifecycle 테스트는 고정된 mid-session 시각을 전달한다.
- 실제 장 마감 이후 실행한 이번 전체 suite에서도 이전 4개 EOD 오염 실패가 재발하지 않았다.

## New findings

### [CODEX-031] HIGH — “30,000원 제한”과 count/exposure가 caller가 선언한 값에 불과함

Status: **UNRESOLVED**

Evidence:

- `LiveEntryContext.max_order_notional_krw`, `available_cash_krw`, `max_daily_loss_krw`, `max_position_count`, `current_open_position_count`, `max_daily_entries`, `today_entry_count`는 모두 caller 입력이다.
- gateway 또는 broker boundary가 이를 order history, positions, active order intents, broker account/open orders에서 재계산하지 않는다.
- pending/reserved entry notional을 일일/총예산에 포함하는 구현이 없다.
- 코드에 immutable `PILOT_TOTAL_BUDGET_KRW = 30_000` 또는 동등한 absolute ceiling이 없다.

Direct reproduction:

- context의 `available_cash_krw`, `max_order_notional_krw`, `max_daily_loss_krw`를 각각 3,000,000원으로 설정하고 AAPL $10, FX 1,350을 전달했다.
- gateway는 qty 222, notional **2,997,000원**을 승인했다.

Impact:

- wrapper/direct broker 게이트가 존재해도 동일 caller가 context limit과 counters를 높이거나 0으로 보고해 30K, 일일 진입, 동시 position 및 pending budget 제한을 우회할 수 있다.
- 테스트의 “30,001원 차단”은 context ceiling을 30,000원으로 이미 신뢰한 상태에서 한 주 가격이 예산을 넘는 경우만 검증한다.

Required behavior:

- 30K pilot absolute total ceiling은 caller가 올릴 수 없는 trusted config/approval record에 고정한다.
- 최종 broker boundary가 durable order reservations, pending/open orders, filled positions 및 당일 entry history에서 사용·예약 금액과 count를 lock-protected snapshot으로 계산한다.
- caller context는 시장 입력(FX/price 등)만 제공하고 risk limits/counters의 권위 있는 출처가 되어서는 안 된다.
- concurrent entry 두 건이 각각 사전 검사를 통과해 합계 한도를 넘지 못하도록 reservation과 검증을 원자화한다.

### [CODEX-032] HIGH — rejected exit의 intent와 position이 원자적으로 갱신되지 않음

Status: **UNRESOLVED**

Evidence:

- `_execute_exit()`의 non-200/201 경로는 `eil.mark_aborted(conn, intent_id)`를 default `commit=True`로 먼저 실행한다.
- 이후 별도 `store.locked_position(conn=conn)`에서 position을 `MANUAL_REVIEW`로 바꾼다.

Fault-injection reproduction:

1. stop-loss exit intent와 `EXIT_SUBMITTED` position을 정상 예약.
2. broker가 HTTP 422 rejected를 반환.
3. intent `ABORTED` commit 이후 position row write만 실패시킴.
4. 결과: position `EXIT_SUBMITTED`, exit intent `ABORTED`, active intent 없음.
5. `recover_on_restart()` 결과 status는 `OK`; position은 계속 `EXIT_SUBMITTED`이고 reconciliation 대상 intent는 없다.

Impact:

- 실제 포지션은 청산되지 않았는데 로컬 상태가 영구 submitted에 머물며 자동 재청산과 reconciliation이 모두 중단된다.
- stop/EOD/time exit가 실패한 실제 open position을 관리하지 못하는 HIGH 안전 문제다.

Required behavior:

- rejection의 intent `ABORTED`와 position `MANUAL_REVIEW`를 shared connection의 한 transaction에서 commit한다.
- commit failure 시 둘 다 기존 pending 상태로 rollback되어 restart reconciliation 가능한 intent가 남아야 한다.
- abort-before-position-write, position-before-abort, hard crash fault-injection 테스트를 추가한다.

### [CODEX-033] MEDIUM — governance checklist가 최신 검증 상태와 모순됨

Status: **UNRESOLVED**

Evidence:

- `FINAL_VALIDATION_PACKAGE.md`는 재검증 전 limited live review를 `BLOCKED`로 기록한다.
- `CURRENT_STATUS.md`도 최신 재검증 대기 상태를 설명한다.
- 하지만 `docs/live_review/LIMITED_LIVE_REVIEW_CHECKLIST.md`의 최종 상태는 과거 CODEX-016~022 PASS를 근거로 이미 `READY_FOR_LIMITED_LIVE_REVIEW`다.
- 최신 Stage 3~10 HIGH Finding과 이번 재검증 판정을 최종 상태에 반영하지 않는다.

Impact:

- 운영자가 최신 package보다 checklist만 확인하면 잘못된 live-review readiness를 판단할 수 있다.

## Regression

### CODEX-016~023, CODEX-025, CODEX-027

Status: **RESOLVED — no observed regression**

Evidence:

- RequestPurpose/purpose-side-payload consistency, runtime credential/mode/endpoint, binary/4-state Kill Switch, notification health, entry intent, accepted-vs-filled, corrupted SQLite fail-closed 및 strict fill validation 집중 테스트가 통과했다.
- 손상 SQLite는 빈 position으로 처리되지 않고 recovery escalation/new position 차단 경로로 이동한다.

## Executed tests

- Stage/broker/position/SQLite/clock 집중 9개 파일 → **249 passed, 0 failed, 1 warning**
- 저장소 루트 `venv/bin/python -m pytest -q` → **973 passed, 0 failed, 2 warnings**
- 저장소 루트 `venv/bin/pytest -q` → **973 passed, 0 failed, 2 warnings**
- 저장소 상위 `us-stock-trading/venv/bin/python -m pytest us-stock-trading -q` → **973 passed, 0 failed, 2 warnings**
- 저장소 상위 `us-stock-trading/venv/bin/pytest us-stock-trading -q` → **973 passed, 0 failed, 2 warnings**
- `git diff --check` 통과.

## Concurrency verification

- concurrent exit 및 stop/target 동시 실행 회귀가 통과했고 broker sell은 한 건이었다.
- repeated/out-of-order exit reconciliation은 idempotent였다.
- 신규 CODEX-031의 concurrent live-entry budget reservation은 구현·검증되지 않았다.
- 신규 CODEX-032의 rejection commit fault는 기존 concurrency suite 범위 밖이다.

## SQLite consistency verification

- partial 4 → projection failure → cumulative 10 정상 결과와 projection regeneration을 재현했다.
- canonical position/event/exit progress transaction rollback 테스트가 통과했다.
- 실제 root `TRADING_STATE.db*` 및 `POSITION_STORE.json`은 생성되지 않았다.
- rejection/abort transition만 여전히 transaction 밖에서 먼저 commit된다.

## Order boundary verification

- context/order symbol mismatch와 direct broker wrapper bypass는 session 호출 0회로 차단된다.
- missing/stale context와 allow-list 위반도 차단된다.
- caller-supplied qty는 gateway sizing으로 대체된다.
- absolute 30K와 authoritative counts/pending exposure는 강제되지 않는다.

## Clock determinism

- FrozenClock의 장중/EOD/premarket/DST 테스트가 통과했다.
- 네 전체 실행이 실제 실행 시각 차이에도 동일한 973 결과를 냈다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 또는 기타 외부 API 호출을 수행하지 않았다.
- HTTP 관련 검증은 fake broker 및 network-forbidden recording session을 사용했다.
- 테스트 출력과 저장소 변경에서 실제 socket 연결 증거는 없었다.

## Operational file safety

- `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966` 불변.
- `universe.csv`: SHA-256 `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`, 833518 bytes, mtime `1784558966` 불변.
- `strategy_performance.csv`: SHA-256 `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`, 69 bytes 불변. 테스트가 mtime만 변경하여 검증 기준 `1785038260`으로 복원했다.
- `docs/live_review/LIVE_APPROVAL_RECORD.md`: SHA-256 `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`, `approved: false`, `live_enabled: false` 불변.
- `.env`, credential, Kill Switch, notification state 및 운영 데이터는 변경하지 않았다.
- main은 `158671e`, 검증 branch HEAD는 `fe3e9b7`; merge/push/deploy 없음.

## Residual risks

1. Entry orders/fills와 CSV/ledger/SQLite 미통합은 현재 CODEX-031의 authoritative pending/count 부재와 결합해 실제 한도 우회로 이어지므로 **HIGH**, 단순 미래 위험이 아니다.
2. 향후 `AlpacaBroker`에 새 order method가 생길 때 gateway를 자동 상속하지 않는 점은 현재 다른 제출 method가 없으므로 **LOW future-maintenance risk**다.
3. 일반 주문 오류의 자동 `ENTRY_DISABLED` 전환은 계속 `NEEDS_USER_DECISION`이다.
4. 실제 FX provider, broker minimum/fractional policy 및 live data feed는 미검증이다.

## Required next action

1. CODEX-031: trusted 30K ceiling 및 durable pending/reserved/order/position 기반 atomic risk snapshot을 최종 broker entry boundary에 연결한다.
2. CODEX-032: rejected exit의 intent abort와 position manual-review 전이를 한 SQLite transaction으로 묶고 fault-injection 회귀를 추가한다.
3. CODEX-033: 최신 FAIL/BLOCKED 판정을 limited-live checklist에 반영한다.
4. 재검증 전 Stage 3~10 `KEEP_IN_PROGRESS`, limited live review `BLOCKED`, live trading `DO_NOT_ENABLE`을 유지한다.
