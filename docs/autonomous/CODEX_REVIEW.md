# CODEX_REVIEW

Review target: CODEX-034 remediation + balance-percent live-entry sizing independent revalidation

Commits: `5da6662`, `5316cd1`, `72bbb6c`

Validation package SHA-256: `c56617668745161e5196c85fd196e964c621b67390ac204817125122289e48fb`

Branch: `orchestrator/20260725-013740-us-stock-trading`

Date: 2026-07-27

Overall verdict: **FAIL**

Stage 3~10: **KEEP_IN_PROGRESS**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

requests timeout/connection-loss 경로는 `SUBMISSION_UNKNOWN`을 유지하고 재시도 주문을 차단하며
client order ID reconciliation까지 가능해져 CODEX-034의 원래 반례는 해결됐다. 전체 1,044개
테스트도 네 실행 형태에서 모두 통과했다.

그러나 모든 `HTTPError`에 response가 있다는 이유만으로 definitive rejection으로 분류한다.
HTTP 500 fault injection에서 첫 27,000원 reservation이 `RELEASED`되고 두 번째 27,000원 주문이
실제 session에 도달해 broker call 2회로 재현됐다. 또한 잔고와 사용 비율은 trusted config/broker
account가 아니라 caller context만 신뢰해 30,000원 계정 가정에서 caller가 3,000,000원을 선언하면
2,997,000원 주문을 승인한다. NaN optional risk/order/strategy caps도 fractional 경로에서 무시되어
주문이 승인된다. 신규 HIGH Finding 3건이므로 진행할 수 없다.

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
| CODEX-034 HIGH — ambiguous broker outcome durable reconciliation | PARTIALLY_RESOLVED |
| CODEX-035 HIGH — HTTP 5xx/ambiguous HTTP response를 definitive rejection으로 오분류 | UNRESOLVED |
| CODEX-036 HIGH — available cash와 cash usage percent가 caller assertion에 의존 | UNRESOLVED |
| CODEX-037 HIGH — NaN optional sizing/risk caps가 fail-open | UNRESOLVED |
| CODEX-038 LOW — 테스트가 운영 CSV mtime 변경 | UNRESOLVED |

## Previous findings verification

### [CODEX-024]

Status: **RESOLVED**

Evidence:

- exit intent와 position submitted transition은 broker 호출 전 같은 SQLite transaction으로 저장된다.
- timeout 후 exit 재시도는 client order ID로 broker를 조회하고 sell을 재제출하지 않는다.
- explicit rejection의 intent `ABORTED`와 position `MANUAL_REVIEW`도 한 transaction으로 commit된다.
- accepted/new는 fill로 처리되지 않으며 remaining quantity와 PnL을 유지한다.
- 관련 exit/reconciliation 집중 회귀가 통과했다.

Remaining risk: 없음.

### [CODEX-026]

Status: **PARTIALLY_RESOLVED**

Evidence:

- allow-list, symbol identity, fresh FX/cash timestamp, daily entry 및 concurrent position은 final
  `AlpacaBroker.submit_order()` 경계에서 검사된다.
- RESERVED, SUBMISSION_UNKNOWN, open COMMITTED reservation은 SQLite snapshot에서 차감된다.
- caller가 요청한 qty는 gateway가 계산한 `actual_qty`로 대체된다.

Remaining risk:

- 새 정책에서 주문 예산의 유일한 상한인 `available_cash_krw × cash_usage_percent`의 두 값 모두
  caller가 전달하며 broker account/trusted operator config와 대조되지 않는다.
- optional 주문·risk cap의 NaN도 차단되지 않는다(CODEX-036/037).

### [CODEX-028]

Status: **RESOLVED**

Evidence:

- position, position events 및 exit intent/fill progress는 SQLite canonical state를 사용한다.
- JSON projection failure는 canonical state를 왜곡하지 않는다.
- partial 4 → projection failure → cumulative 10 회귀는 CLOSED, remaining 0, 전체 10주 PnL로 통과한다.
- transaction failure와 repeated/out-of-order reconciliation 회귀가 통과했다.

Remaining risk:

- entry reservation/reconciliation은 같은 SQLite DB에 있으나 entry order/fill lifecycle 전체와 자동
  recovery orchestration까지 통합되지는 않았다. CODEX-034의 timeout 경로는 cash를 계속 차감하므로
  즉시 fail-open은 아니며 수동 reconciliation 잔여 위험으로 별도 기록한다.

### [CODEX-029]

Status: **RESOLVED**

Evidence:

- context, sizing, reservation, order argument 및 payload symbol은 byte-exact 일치를 요구한다.
- AAPL context와 TSLA order/payload는 wrapper와 direct broker 경로 모두 session 호출 0회로 차단된다.
- 현재 실제 live order network method인 `AlpacaBroker.submit_order()`가 final gate를 직접 실행한다.

Remaining risk:

- 향후 별도 주문 method가 추가되면 gate를 구조적으로 상속하지 않는다. 현재 우회 method가 없으므로
  LOW future-maintenance risk다.

### [CODEX-030]

Status: **RESOLVED**

Evidence:

- lifecycle/EOD 판단은 injected Clock/timezone-aware now를 사용한다.
- FrozenClock 기반 정규장, premarket, EOD 전후 및 DST 회귀가 통과했다.
- 실제 실행 시간이 다른 네 전체 suite 결과가 모두 1,044개로 동일했다.

Remaining risk: 없음.

### [CODEX-031]

Status: **PARTIALLY_RESOLVED**

Evidence:

- durable SQLite ledger의 snapshot-read/reserve는 file lock 아래 원자화된다.
- pending, unknown submission, open position cost, daily entry count 및 active position count는 caller
  counters가 아니라 SQLite에서 산출된다.
- concurrent reservation 회귀는 한 entry만 허용한다.

Remaining risk:

- ledger deduction은 authoritative하지만 그 기준 금액인 current cash와 사용 비율은 authoritative하지
  않다. caller가 실제보다 큰 cash/percent를 전달하면 최종 broker boundary가 그대로 승인한다
  (CODEX-036).
- HTTP 5xx에서 reservation이 release되어 ledger가 실제 잠재 exposure를 다시 과소계상한다
  (CODEX-035).

### [CODEX-032]

Status: **RESOLVED**

Evidence:

- rejected exit의 abort와 position transition은 shared SQLite transaction이다.
- intent-side 및 position/event-side failure injection에서 양쪽 mutation이 함께 rollback된다.

Remaining risk: 없음.

### [CODEX-033]

Status: **RESOLVED**

Evidence:

- `LIMITED_LIVE_REVIEW_CHECKLIST.md` 최종 상태는 계속 `BLOCKED`다.
- `FINAL_VALIDATION_PACKAGE.md` 및 `CURRENT_STATUS.md`의 재검증 대기 상태와 일치한다.

Remaining risk: 없음.

### [CODEX-034]

Status: **PARTIALLY_RESOLVED**

Evidence:

- migration 5가 `live_entry_reservations.client_order_id`와 unique index를 추가한다.
- reservation은 broker 호출 전에 client order ID와 함께 durable하게 저장된다.
- `requests.exceptions.Timeout` 직접 재현:
  - 첫 broker session call 후 reservation `SUBMISSION_UNKNOWN`.
  - 같은 크기 retry는 status 423, session call 총 1회.
  - broker가 client order ID를 accepted로 반환하면 reconciliation 결과 `COMMITTED`.
- broker lookup failure/None은 reservation을 unknown 상태로 유지하며 새 주문을 제출하지 않는다.
- accepted/new/partial/filled 계열은 release하지 않고 committed exposure로 유지한다.

Remaining risk:

- ambiguous outcome 판정이 “HTTP response 존재 여부”에만 의존한다. HTTP 5xx, 408 또는 gateway/proxy
  오류는 response가 있어도 주문 미수신을 증명하지 않는데 definitive rejection으로 처리된다.
- reconciliation은 restart lifecycle에 자동 배선되지 않아 운영자가 수동 실행해야 한다. 현금이 계속
  차감되어 중복 주문보다는 availability block으로 귀결되므로 이 부분만은 MEDIUM operational risk다.

## New findings

### [CODEX-035] HIGH — HTTP 5xx/ambiguous HTTP response를 definitive rejection으로 오분류

Status: **UNRESOLVED**

Evidence:

- `_is_ambiguous_broker_failure()`과 wrapper 복사본은 `HTTPError`에 `.response`가 있으면 항상 False를
  반환한다.
- `submit_order()` exception handler는 False 결과에서 reservation을 `RELEASED`한다.
- HTTP status나 Alpaca의 명시적 rejected/canceled 상태를 확인하지 않는다.

Direct reproduction:

1. isolated SQLite/kill-switch 환경과 fake session을 사용했다.
2. 첫 POST가 broker 수신 후 upstream HTTP 500을 반환한 상황을 모사했다.
3. `raise_for_status()`가 response를 가진 `HTTPError`를 발생시켰다.
4. 첫 27,000원 reservation이 `RELEASED`.
5. 같은 조건의 두 번째 27,000원 주문이 status 200으로 session에 도달.
6. session call 총 2회; ledger는 첫 잠재 exposure를 차감하지 않았다.

Impact:

- broker/API gateway의 5xx, 408 또는 일부 proxy failure에서 첫 주문이 실제 수신됐어도 재시도 주문이
  허용된다.
- 중복 entry와 account cash/position limit 우회가 가능한 현재 final-boundary HIGH 결함이다.

Required behavior:

- 명시적으로 주문 미생성이 확정되는 broker rejection만 RELEASED 처리한다.
- timeout 계열, 408, 425, 429, 5xx 및 의미가 불명확한 HTTP body는 SUBMISSION_UNKNOWN으로 유지한다.
- HTTP status뿐 아니라 Alpaca order/rejection contract에 근거한 allowlist 방식으로 definitive outcome을
  분류한다.
- HTTP 500/502/503/504 response-loss fault injection에서 retry session call 총 1회를 검증한다.

### [CODEX-036] HIGH — actual cash와 cash usage percent가 caller assertion에 의존

Status: **UNRESOLVED**

Evidence:

- `LiveEntryContext.available_cash_krw`와 `cash_usage_percent`는 final broker boundary의 caller argument다.
- timestamp는 caller가 함께 제공한 `cash_as_of`가 최근인지만 검사하며 cash 값의 출처/서명/계좌
  snapshot identity는 확인하지 않는다.
- `cash_usage_percent`를 operator setting이라고 설명하지만 trusted runtime config나 approval record에서
  읽거나 caller 값과 교차 검증하는 production code가 없다.
- tests 밖에서 `LiveEntryContext`를 구성해 broker account balance와 operator percentage를 주입하는
  production call site도 없다.

Direct reproduction:

- available cash 30,000원, operator 의도값 10%에서는 $10 한 주를 살 수 없어 차단됐다.
- 동일 caller가 `cash_usage_percent=100`으로 바꾸면 qty 2, 27,000원 승인.
- caller가 `available_cash_krw=3,000,000`, percent 100을 선언하면 qty 222,
  **2,997,000원** 승인.
- 어떤 경우에도 broker account/cash endpoint 조회는 0회였다.

Impact:

- stale/buggy/조작된 context가 실제 계좌 현금 또는 승인된 운영 비율보다 큰 주문을 final network
  boundary에서 허용한다.
- 고정 trusted ceiling을 제거한 현재 설계에서는 이 caller assertion이 유일한 금액 상한이므로 HIGH다.

Required behavior:

- cash usage percent는 caller가 올릴 수 없는 trusted deployment config/approval record에서 읽고 final
  boundary에서 강제한다.
- available cash는 동일 broker/account의 fresh authoritative snapshot 또는 검증 가능한 snapshot
  object로 전달하고, arbitrary numeric context만으로 승인하지 않는다.
- caller는 trusted percentage/cash를 오직 더 낮추는 추가 cap만 제공할 수 있어야 한다.
- 실제 30,000원 account snapshot + caller 3,000,000원/100% 반례가 HTTP 0회로 차단되는 테스트를 추가한다.

### [CODEX-037] HIGH — NaN optional sizing/risk caps가 fail-open

Status: **UNRESOLVED**

Evidence:

- `max_order_notional_krw`, `max_daily_loss_krw`, `max_risk_per_trade_krw`,
  `strategy_max_quantity`, `stop_price_usd`의 공통 finite/type validation이 없다.
- Python `min(valid_value, float("nan"))` 비교는 NaN cap을 안정적인 제한으로 적용하지 않는다.
- whole-share 일부 경로는 `math.floor(nan)`의 raw `ValueError`로 우연히 차단되지만 fractional 경로에서는
  NaN이 그대로 무시된다.

Direct reproduction:

- fractional entry + `max_risk_per_trade_krw=NaN` → qty `0.222222...`, 3,000원 주문 승인.
- fractional entry + `strategy_max_quantity=NaN` → 동일 qty/금액 승인.
- whole-share entry + `max_order_notional_krw=NaN` → qty 2, 27,000원 주문 승인.

Impact:

- malformed market/config/strategy 값이 “불명확하면 차단”되지 않고 risk, strategy 또는 per-order cap을
  제거한다.
- risk control을 우회해 실제 주문 수량을 키울 수 있는 final sizing HIGH 결함이다.

Required behavior:

- 모든 numeric input에 bool 제외, finite, 허용 부호/범위 검사를 reservation 이전에 적용한다.
- optional cap이 제공됐는데 invalid하면 unset으로 취급하지 말고 `LiveOrderBlockedError`로 차단한다.
- NaN, ±Infinity, bool, string, zero, negative 조합을 whole/fractional 양쪽에서 검증하고 reservation/HTTP
  호출 0회를 확인한다.

### [CODEX-038] LOW — 테스트가 운영 CSV mtime 변경

Status: **UNRESOLVED**

Evidence:

- 전체 테스트 전후 `strategy_performance.csv` content SHA-256과 크기는 동일했다.
- mtime은 `1785082147`에서 `1785083284`로 변경됐다.
- git working tree에는 content diff가 없어 나타나지 않지만 filesystem metadata는 변경됐다.

Impact:

- 데이터 내용 손상은 없으나 mtime 기반 운영 모니터링/증분 작업에 불필요한 변화를 만들 수 있다.

Required behavior:

- 해당 테스트를 tmp_path로 완전 격리하거나 원본 mtime까지 복원한다.

## Regression

### CODEX-016~023, CODEX-025, CODEX-027

Status: **RESOLVED — no observed regression**

Evidence:

- mode/endpoint/credential revalidation, RequestPurpose, purpose-side-payload identity,
  binary/4-state Kill Switch, notification health, entry intent, strict fill validation,
  accepted-vs-filled 및 corrupted SQLite fail-closed 회귀가 통과했다.

## Balance-percent sizing verification

- `cash_usage_percent`의 None/string/bool/NaN/Infinity/0/음수/>100은 차단된다.
- fresh cash timestamp와 fresh FX timestamp가 모두 필요하다.
- pending, unknown submission 및 open-position cost는 SQLite snapshot에서 각각 차감된다.
- `actual_qty=min(balance,risk,strategy)` 정상 finite 입력은 올바르게 축소되고 실제 resized notional만
  reservation에 저장된다.
- risk/strategy resizing 후 0 또는 minimum order 미만이면 reservation 전에 차단된다.
- caller-independent daily entry 2건 및 concurrent position 1건 ceiling은 유지된다.
- authoritative balance/percent 출처 부재와 optional cap NaN fail-open은 CODEX-036/037로 남는다.

## Watchlist affordability verification

- pure calculation 모듈은 missing/nonfinite/negative account deductions를 fail-closed 결과로 반환한다.
- whole share 비구매 가능 + fractionable=true + minimum order 충족 시 fractional candidate를 유지한다.
- non-fractionable과 minimum-order 미충족은 구분된 상태로 제외한다.
- `estimated_entry_price_usd`는 finite/positive 검증된다.
- `estimated_slippage_usd=NaN`은 `max(nan, 0)` 비교 특성상 명시적 invalid 상태로 차단되지 않을 수
  있으나, 이 모듈은 현재 실제 scanner/watchlist pipeline에 미배선이고 final broker gate가 별도로
  동작하므로 MEDIUM implementation-completeness risk로 기록한다.
- 실제 pipeline 미배선 상태에서 “관심종목 필터 완료”로 판정하지 않는다.

## Executed tests

- live gateway/watchlist/broker/order/exit 집중 8개 파일:
  **345 passed, 0 failed, 1 warning**
- 저장소 루트 `venv/bin/python -m pytest -q`:
  **1044 passed, 0 failed, 2 warnings**
- 저장소 루트 `venv/bin/pytest -q`:
  **1044 passed, 0 failed, 2 warnings**
- 저장소 상위 `us-stock-trading/venv/bin/python -m pytest us-stock-trading -q`:
  **1044 passed, 0 failed, 2 warnings**
- 저장소 상위 `us-stock-trading/venv/bin/pytest us-stock-trading -q`:
  **1044 passed, 0 failed, 2 warnings**
- direct timeout/retry/reconciliation, HTTP 500/retry, inflated cash/percent 및 NaN cap scripts 실행.
- `git diff --check`: 통과.

Warnings review:

- urllib3의 macOS LibreSSL `NotOpenSSLWarning`은 환경 호환 경고이며 주문 판정과 무관하다.
- unknown scanner field를 의도적으로 skip하는 회귀의 `RuntimeWarning`은 기대된 경고다.
- safety-related warning은 없지만 CODEX-035~037 반례는 warning 없이 fail-open한다.

## Concurrency verification

- reservation snapshot-read-write lock과 concurrent live-entry 회귀가 통과했다.
- SUBMISSION_UNKNOWN은 active position/budget deduction에 포함된다.
- timeout 후 순차 retry는 broker session에 도달하지 않는다.
- HTTP 5xx 후 reservation이 release되므로 lock/concurrency 보호와 무관하게 retry가 허용된다.

## SQLite consistency verification

- migration 5의 client order ID column/unique index와 existing database migration 회귀가 통과했다.
- RESERVED → SUBMISSION_UNKNOWN → COMMITTED/RELEASED transition 및 terminal-state protection이 동작한다.
- timeout/lookup failure에서 reservation은 durable하게 남는다.
- HTTP 500 경로는 DB 실패가 아니라 application이 명시적으로 RELEASED를 기록하므로 consistency
  checker로 탐지되지 않는다.

## Order boundary verification

- missing context, allow-list, stale cash/FX, symbol mismatch 및 active reservation은 final
  `AlpacaBroker.submit_order()`에서 HTTP 전에 차단된다.
- direct broker 정상 entry는 gateway가 계산한 qty/client order ID를 payload에 사용한다.
- HTTP 5xx, untrusted cash/percent 및 nonfinite optional cap은 final boundary에서 차단되지 않는다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 또는 기타 외부 API를 호출하지 않았다.
- 모든 broker 검증은 fake/recording/network-forbidden session을 사용했다.
- HTTP 500 및 timeout 재현도 local session double에서만 수행했다.

## Operational file safety

- `order_history.csv`: SHA-256
  `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`,
  31 bytes, mtime `1784558966`, 전후 불변.
- `universe.csv`: SHA-256
  `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`,
  833518 bytes, mtime `1784558966`, 전후 불변.
- `strategy_performance.csv`: SHA-256
  `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`,
  69 bytes로 content는 불변이나 mtime은 `1785082147` → `1785083284`로 변경(CODEX-038).
- root `TRADING_STATE.db*`, `POSITION_STORE.json`, `LIVE_ENTRY_RESERVATION.lock`, `.env`는 생성되지 않았다.
- `LIVE_APPROVAL_RECORD.md`: SHA-256
  `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`,
  `approved: false`, `live_enabled: false` 불변.
- `risk_config.py`, `broker/broker_config.py`, `kill_switch_state.py`, `order_intent_ledger.py`의 package
  SHA-256과 실제 SHA-256이 일치한다.
- main `158671e`, 검증 branch HEAD `72bbb6c`; merge/push/deploy 없음.
- 검증 전 working tree는 clean이었으며 구현 코드는 수정하지 않았다.

## Residual risks

1. HTTP error ambiguity가 중복 주문과 cash/position limit 우회로 직접 이어짐:
   **HIGH (CODEX-035)**.
2. actual cash 및 operator percentage에 authoritative source가 없어 caller가 금액 상한을 완화 가능:
   **HIGH (CODEX-036)**.
3. NaN sizing/risk cap이 fractional/whole 경로에서 제한을 제거:
   **HIGH (CODEX-037)**.
4. reconciliation은 수동 trigger이며 restart recovery에 미배선:
   **MEDIUM operational availability risk**.
5. watchlist affordability는 실제 scanner/pipeline에 미배선:
   **MEDIUM implementation-completeness risk**.
6. 향후 별도 broker order method의 gate 자동 상속 부재:
   **LOW future-maintenance risk**.
7. 테스트의 운영 CSV mtime 변경:
   **LOW (CODEX-038)**.
8. 실제 FX/cash provider, Alpaca fractional/minimum policy 및 live account behavior는 미검증이다.

## Required next action

1. CODEX-035: HTTP status/body 기반의 conservative ambiguous classification으로 408/5xx 등에서
   SUBMISSION_UNKNOWN을 유지하고 retry broker call을 차단한다.
2. CODEX-036: broker/account-derived fresh cash snapshot과 trusted operator percentage를 final
   boundary에 연결한다. caller context는 이를 늘릴 수 없어야 한다.
3. CODEX-037: 모든 optional numeric cap의 type/finite/range를 reservation 이전에 fail-closed 검증한다.
4. CODEX-038: strategy performance 관련 테스트를 tmp_path로 격리해 mtime도 변경하지 않는다.
5. 재검증 전 Stage 3~10 `KEEP_IN_PROGRESS`, limited live review `BLOCKED`,
   live trading `DO_NOT_ENABLE`을 유지한다.
