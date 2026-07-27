# CODEX_REVIEW

Review target: Stage 11 Account/Risk/Sizing/Execution Engine + CODEX-034~038 focused revalidation

Commits: `9d294e3`, `40abc58`, `06a77c8`, `3494fe3`, `14f7a13`

Validation package SHA-256: `8c325b7b4e65616019f6086fc7fc2c8517cc46841f3534cbe246c6fa598c8a4b`

Branch: `orchestrator/20260725-013740-us-stock-trading`

Date: 2026-07-28

Overall verdict: **FAIL**

Stage 3~11: **KEEP_IN_PROGRESS**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

CODEX-034/035의 timeout·HTTP 5xx ambiguous submission 처리는 `SUBMISSION_UNKNOWN`을 유지하고
재시도 broker call을 차단하며 client order ID reconciliation까지 정상 동작한다. CODEX-037의
NaN sizing 후보도 fail-closed이고, CODEX-038의 운영 CSV mtime 오염도 재발하지 않았다.

그러나 trusted cash usage percent는 현재 50% 강제 상한이다. trusted 90%/100% 정책을 선택해도
각각 27,000원/30,000원을 사용할 수 없고 15,000원으로 축소된다. 더 중요하게 Stage 11의
Account/Risk/Sizing/Execution Engine과 affordability는 실제 `paper_strategy_order.main()`에
전혀 배선되지 않았다. 런타임 계측에서 Execution Engine 호출 0회, affordability 호출 0회,
legacy `broker.submit_order()` 1회로 실제 주문이 제출됐다. direct Alpaca broker 경계에서도
authoritative account snapshot은 optional이어서 caller가 cash 3,000,000원을 선언하면 account
GET 없이 1,500,000원 POST가 가능했다. 미해결 HIGH Finding이 있으므로 진행할 수 없다.

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
| CODEX-034 | RESOLVED |
| CODEX-035 | RESOLVED |
| CODEX-036 | PARTIALLY_RESOLVED |
| CODEX-037 | RESOLVED |
| CODEX-038 | RESOLVED |
| CODEX-039 MEDIUM — 50%가 기본값이 아니라 강제 최대값이며 caller percent도 무시되지 않음 | UNRESOLVED |
| CODEX-040 HIGH — 실제 main 주문 흐름이 Stage 11 Execution Engine 전체를 우회 | UNRESOLVED |
| CODEX-041 MEDIUM — affordability가 실제 후보/주문 차단에 미배선 | UNRESOLVED |

## Focused verification

### 1. cash_usage_percent policy

Status: **FAIL**

Evidence:

- `trusted_operator_config._validate_percent()`는 정확히 `(0, 100]`, 즉 1~100 범위를 허용한다.
- trusted value 0, 101, NaN, Infinity, bool, 문자열은 모두 `TrustedConfigError`로 차단됐다.
- trusted operator 값별 런타임 계산, broker cash 30,000원:
  - trusted 100%, caller 100% → 30,000원.
  - trusted 90%, caller 100% → 27,000원.
  - trusted 50%, caller 100% → 15,000원.
- 저장소의 실제 `CASH_USAGE_PERCENT_CEILING`은 **50**이다.
- 현재 설정에서는 caller 100%, 90%, 50%가 모두 15,000원으로 강제 축소된다.
- caller percent는 무시되지 않는다. trusted 100%에서도 caller 50%를 전달하면 15,000원이고,
  현재 trusted 50%에서 caller 40%를 전달하면 12,000원이다.

Conclusion:

- 50%는 단순 default가 아니라 코드 변경 전까지 적용되는 강제 maximum이다.
- 사용자 요구사항의 trusted 90%/100% 동작과 “caller percent 무시”를 만족하지 않는다
  (CODEX-039).

### 2. actual operational order path

Status: **FAIL — HIGH**

Runtime trace:

1. isolated `paper_strategy_order.main()`에 regular session, AAPL high-score candidate, fake broker를 주입했다.
2. `live_readiness.execution_engine.submit_validated_command()`에는 call spy를 설치했다.
3. 결과:
   - `main_result={"submitted": ["AAPL"], ...}`
   - legacy broker `submit_order` calls: `[("AAPL", 1)]`
   - Execution Engine calls: **0**
4. `main()`은 고정 `order_qty=1`을 만든 뒤 `try_reserve_order()`와 legacy
   `paper_strategy_order.submit_order()`를 거쳐 broker를 직접 호출한다.
5. Account Engine, Risk Engine, Sizing Engine, ValidatedOrderCommand를 생성하거나 검증하는 runtime
   단계가 없다.

Additional evidence:

- `execution_engine.py`의 static guard는 `paper_strategy_order.py`를 명시적으로 allowlist한다.
- 따라서 guard는 actual operating legacy bypass를 탐지하도록 설계되지 않았다.
- validation package도 Stage 11 엔진이 `paper_strategy_order.py::main()`에 미배선이라고 명시한다.

Conclusion:

- 모든 신규 진입이 Account/Risk/Sizing/Execution Engine을 통과하지 않는다.
- 요청 판정 기준에 따라 **CODEX-040 HIGH**다.

### 3. CODEX-034/035

Status: **RESOLVED**

Direct fault injection:

| Failure | First reservation | Retry status | Broker calls | Reconciliation |
|---|---|---:|---:|---|
| requests timeout | SUBMISSION_UNKNOWN | 423 | 1 | COMMITTED |
| HTTP 500 | SUBMISSION_UNKNOWN | 423 | 1 | COMMITTED |
| HTTP 502 | SUBMISSION_UNKNOWN | 423 | 1 | COMMITTED |
| HTTP 503 | SUBMISSION_UNKNOWN | 423 | 1 | COMMITTED |
| HTTP 504 | SUBMISSION_UNKNOWN | 423 | 1 | COMMITTED |

Evidence:

- 408/425/429/5xx/unrecognized HTTP status는 ambiguous allowlist 정책으로 release되지 않는다.
- definitive rejection은 제한된 status allowlist와 JSON object body를 모두 요구한다.
- timeout/5xx 이후 동일 조건의 재주문은 active reservation/position count에서 차단되어 session에
  두 번째로 도달하지 않는다.
- `reconcile_by_client_order_id()`가 accepted broker record를 `COMMITTED`로 전환한다.

Remaining risk:

- reconciliation은 restart orchestration에 자동 배선되지 않아 수동 실행이 필요하다. reservation이
  계속 cash를 차감하므로 중복 주문보다 availability block으로 귀결되는 MEDIUM operational risk다.

### 4. CODEX-036

Status: **PARTIALLY_RESOLVED**

Resolved portion:

- `fetch_account_cash_snapshot()`은 broker `get_account()`의 cash를 KRW로 변환하고 invalid/missing
  response를 fail-closed 처리한다.
- supplied `AccountCashSnapshot`이 있으면 sizing은
  `min(caller_cash, broker_cash_snapshot)`을 사용한다.
- Account Engine은 `min(broker_cash, non_marginable_buying_power)`를 사용해 margin/leverage를
  상한으로 사용하지 않는다.

Unresolved direct reproduction:

1. 실제 broker cash를 30,000원으로 가정하고 caller context cash를 3,000,000원으로 설정했다.
2. `account_cash_snapshot`을 생략한 direct `AlpacaBroker.submit_order()`를 실행했다.
3. final boundary는 account GET을 수행하지 않았고 POST 한 번만 실행했다.
4. current trusted 50% ceiling만 적용되어 qty 1,500, notional **1,500,000원**이 payload에 실렸다.

Cause:

- `account_cash_snapshot`은 broker와 wrapper 모두 optional이다.
- 생략 시 `LiveEntryContext.available_cash_krw`를 그대로 사용한다.
- production `main()`은 Account Engine/snapshot을 구성하지 않는다.
- caller cash와 caller percentage가 actual sizing에 영향을 준다.

Conclusion:

- authoritative snapshot 기능은 존재하지만 실제 final/operational 경계에서 필수가 아니므로
  CODEX-036은 계속 **PARTIALLY_RESOLVED**이며 HIGH 위험이 남는다.

### 5. CODEX-037

Status: **RESOLVED**

Direct reproduction:

- `balance_based_qty` 입력을 NaN으로 만드는 available cash → `SizingEngineError`.
- `risk_based_qty=NaN` → `SizingEngineError`.
- `strategy_max_qty=NaN` → `SizingEngineError`.
- 세 시나리오 모두 broker/HTTP 호출 0회.
- legacy `order_gateway`도 optional order/daily-loss/per-trade-risk/strategy/stop cap을 reservation 전에
  type/finite/positive 검증한다.
- invalid cap을 unset으로 간주하거나 나머지 후보만으로 주문하지 않는다.

### 6. affordability wiring

Status: **PARTIALLY_IMPLEMENTED — NOT WIRED**

Pure calculation:

- cash 30,000원, usage 90% → `available_for_new_order_krw=27,000`.
- 50,000원 종목, `fractionable=false` → `NOT_FRACTIONABLE`, 최종 affordability candidate 제외.
- 같은 종목, `fractionable=true`, minimum order 충족 → `AFFORDABLE_FRACTIONAL`, candidate 유지.

Runtime operational trace:

1. `paper_strategy_order.main()`에 cash 30,000원 account, 50,000원 non-fractionable 성격의 candidate를
   모사했다.
2. `evaluate_affordability()` call spy와 Execution Engine spy를 설치했다.
3. 결과:
   - affordability calls: **0**
   - Execution Engine calls: **0**
   - legacy broker submit calls: `[("EXP", 1)]`
   - main result: `submitted`.

Conclusion:

- affordability 결과는 실제 scanner/main/order boundary의 차단 조건이 아니다.
- 90% 계산 자체도 trusted 50% operator ceiling과 연결되지 않아 actual order budget 정책과 다르다.
- 표시용 building block만 존재하고 운영 후보 필터는 완료되지 않았다(CODEX-041).

### 7. operational-file isolation

Status: **RESOLVED**

Full-suite before/after:

| File | SHA-256 | Size | mtime |
|---|---|---:|---:|
| `order_history.csv` | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| `universe.csv` | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| `strategy_performance.csv` | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

- 세 파일 모두 hash, size, mtime 전후 동일.
- root `TRADING_STATE.db*`, `POSITION_STORE.json`, `LIVE_ENTRY_RESERVATION.lock`, `.env` 생성 없음.
- CODEX-038은 재발하지 않아 RESOLVED.

## New findings

### [CODEX-039] MEDIUM — 50%가 default가 아니라 강제 maximum이며 caller percent도 무시되지 않음

Status: **UNRESOLVED**

Evidence:

- `CASH_USAGE_PERCENT_CEILING = 50`.
- `effective_percent=min(caller_percent, trusted_ceiling)`.
- trusted 90/100 정책을 runtime input/config로 선택할 수 없고 코드 상수를 변경해야 한다.
- caller가 더 작은 percent를 전달하면 sizing이 달라지므로 caller percent는 무시되지 않는다.

Impact:

- 실제 현금 이상을 쓰게 하는 fail-open은 아니지만, 사용자가 요구한 1~100 trusted policy와
  90%/100% 운용 선택을 막는 기능·정책 불일치다.

Required behavior:

- trusted operator value 자체를 1~100 범위의 단일 source로 사용한다.
- 50은 승인 전 보수적 default일 수 있지만 별도의 immutable maximum이어서는 안 된다.
- final sizing에서 caller percent를 완전히 무시하거나 trusted value보다 낮추는 별도 명시적 cap으로
  이름/계약을 분리한다.

### [CODEX-040] HIGH — 실제 main 주문 흐름이 Execution Engine 전체를 우회

Status: **UNRESOLVED**

Evidence:

- runtime `main()`에서 Account/Risk/Sizing/Execution Engine call 0회.
- legacy wrapper가 broker `submit_order()`를 직접 호출해 order를 submitted로 기록했다.
- static architecture test가 legacy module을 allowlist해 이 우회를 정상으로 간주한다.
- validation package도 Stage 11을 production pipeline 미배선 building block으로 기록한다.

Impact:

- strategy/main이 `ValidatedOrderCommand`, authoritative AccountSnapshot, risk decision, sizing decision,
  command TTL/mutation 검사를 거치지 않고 broker에 도달한다.
- 새 계층의 안전 보장은 실제 운영 신규 진입에 적용되지 않는다.

Required behavior:

- operational `main()`의 모든 buy entry를
  Account Engine → Risk Engine → Sizing Engine → Execution Engine으로 배선한다.
- legacy compat wrapper는 외부 호환 facade로만 남기고 운영 main에서는 호출하지 않는다.
- runtime integration test가 valid path에서 네 engine을 순서대로 정확히 1회 호출하고,
  각 engine failure에서 broker call 0회를 확인해야 한다.
- architecture guard에서 operational legacy bypass를 허용하지 않는다.

### [CODEX-041] MEDIUM — affordability가 실제 후보/주문 차단에 미배선

Status: **UNRESOLVED**

Evidence:

- calculation module과 단위 테스트만 존재한다.
- runtime main trace에서 affordability call 0회.
- 50,000원 non-fractionable candidate가 cash 30,000원 account 모사에서도 broker까지 제출됐다.
- pure affordability의 percent는 trusted operator config가 아닌 caller field다.

Impact:

- 구매 불가능 candidate가 전략 감시 및 legacy broker 제출 단계까지 남는다.
- broker rejection에 의존하며 watchlist 단계의 의도된 fail-closed 필터가 작동하지 않는다.

Required behavior:

- authoritative AccountSnapshot과 trusted percentage로 affordability account state를 구성한다.
- 실제 scanner/watchlist/main 후보 흐름에서 non-affordable result를 제거한다.
- Execution Engine 직전에도 affordability/sizing 결과를 재검증해 표시용 필드로만 남지 않게 한다.

## Regression

### CODEX-016~035, CODEX-037~038

Status: **RESOLVED — no observed regression**

Evidence:

- mode/endpoint/credential, RequestPurpose, symbol/payload identity, Kill Switch, notification health,
  exit/entry intent, SQLite consistency, Clock determinism, ambiguous submission 및 NaN cap 회귀가 통과했다.

## Executed tests

- trusted config/account/risk/sizing/execution/live gateway/affordability/main 집중 8개 파일:
  **427 passed, 0 failed, 1 warning**
- 저장소 루트 `venv/bin/python -m pytest -q`:
  **1299 passed, 0 failed, 2 warnings**
- direct runtime scripts:
  - trusted 100/90/50 percentage calculation and invalid trusted values.
  - `paper_strategy_order.main()` Execution Engine/affordability call trace.
  - direct broker inflated-cash/no-snapshot submission.
  - timeout and HTTP 500/502/503/504 retry/reconciliation.
  - NaN balance/risk/strategy sizing.
  - 50,000원 fractional/non-fractional affordability.
- `git diff --check`: 통과.

Warnings review:

- urllib3 macOS LibreSSL 경고와 intentional unknown scanner field 경고뿐이다.
- safety-related warning은 없다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 또는 기타 외부 API 호출 없음.
- account/broker/HTTP 검증은 fake broker와 local recording session만 사용했다.

## Operational safety

- `approved: false`, `live_enabled: false` 불변.
- `LIVE_APPROVAL_RECORD.md` SHA-256:
  `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`.
- main `158671e`, validation branch HEAD `14f7a13`; merge/push/deploy 없음.
- 검증 중 생성된 notification-health 계측 산출물 2개는 즉시 정확한 대상만 `/private/tmp`로 이동해
  검증 시작 전 clean 상태를 복원했다.
- 구현 코드는 수정하지 않았다.

## Residual risks

1. operational Execution Engine bypass: **HIGH (CODEX-040)**.
2. authoritative cash snapshot optional/unwired: **HIGH (CODEX-036 remainder)**.
3. trusted percent 50% forced maximum/policy mismatch: **MEDIUM (CODEX-039)**.
4. affordability operational pipeline unbound: **MEDIUM (CODEX-041)**.
5. SUBMISSION_UNKNOWN reconciliation manual trigger: **MEDIUM operational availability risk**.
6. future broker order method gate inheritance: **LOW maintenance risk**.

## Required next action

1. `paper_strategy_order.main()`의 실제 신규 진입을 Account/Risk/Sizing/Execution Engine에 배선하고
   runtime bypass를 제거한다.
2. AccountSnapshot을 live entry의 필수 input으로 만들어 caller cash만으로 final broker POST가
   불가능하게 한다.
3. 50%를 강제 maximum이 아닌 trusted operator default로 바꾸고 1~100 trusted value를 그대로
   적용하며 caller percent는 sizing 정책에서 제거한다.
4. affordability를 실제 candidate/main 흐름에 배선한다.
5. 재검증 전 Stage 3~11 `KEEP_IN_PROGRESS`, limited live review `BLOCKED`,
   live trading `DO_NOT_ENABLE`을 유지한다.
