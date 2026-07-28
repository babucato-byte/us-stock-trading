# CODEX_REVIEW

Review target: Stage 11 live-entry pipeline focused independent revalidation

Commits: `ae2b0fd`, `fc20574`

Validation package SHA-256: `941d3bf51ffb1876d1395e24a4d8f722ee2ec38584328c784b69299be2659163`

Branch: `orchestrator/20260725-013740-us-stock-trading`

Date: 2026-07-28

Overall verdict: **PASS_WITH_CONDITIONS**

Stage 3~11: **VALIDATED**

Limited live review: **READY_FOR_LIMITED_LIVE_REVIEW**

Live trading: **DO_NOT_ENABLE**

이번 검증은 구현자의 결과를 신뢰하지 않고 실제 `paper_strategy_order.main()` 호출 계측,
trusted usage 경계값, caller 입력 거부, affordability, timeout/5xx fault injection을 독립 실행했다.
이전 HIGH Finding인 CODEX-040의 legacy runtime 우회는 해소됐다. live 모드의 신규 진입은
Account → Risk → Sizing → Affordability → Execution Engine을 순서대로 통과했으며 legacy
`paper_strategy_order.submit_order()` 호출은 0회였다.

## Finding summary

| Finding | Status |
|---|---|
| CODEX-034 — ambiguous submission reservation | RESOLVED |
| CODEX-035 — HTTP 5xx classification/retry | RESOLVED |
| CODEX-036 — authoritative broker cash | RESOLVED |
| CODEX-037 — invalid sizing cap fail-closed | RESOLVED |
| CODEX-038 — operational-file isolation | RESOLVED |
| CODEX-039 — trusted usage policy | RESOLVED |
| CODEX-040 — actual main Execution Engine bypass | RESOLVED |
| CODEX-041 — affordability not wired | RESOLVED |

신규 CRITICAL/HIGH Finding은 발견되지 않았다.

## Focused verification

### 1. `paper_strategy_order.main()` live pipeline wiring

Status: **RESOLVED**

독립 런타임 계측 결과:

```text
main result: submitted=["AAPL"]
Account Engine calls: 1
Risk Engine calls: 1
Sizing Engine calls: 1
Execution Engine calls: 1
legacy paper_strategy_order.submit_order calls: 0
final fake broker calls: 1
```

- live branch는 `live_entry_pipeline.run_live_entry_pipeline()`을 반드시 호출한다.
- 신규 파이프라인은 Account → Risk → Sizing → Affordability → Execution 순으로 실행된다.
- 최종 broker 호출은 Execution Engine 이후에만 발생했다.
- paper 모드는 기존 paper 전용 경로를 유지한다. 이는 live-entry 안전 게이트 우회가 아니다.

### 2. legacy direct submit runtime

Status: **RESOLVED**

- live `main()` 실행 중 legacy wrapper 호출 spy는 **0회**였다.
- 제출 성공은 최종 Execution Engine이 검증된 명령을 broker boundary에 전달한 1회뿐이다.
- `AlpacaBroker.submit_order()` 자체는 최종 broker adapter이므로 호출 존재 자체가 legacy 우회를
  뜻하지 않는다. 검증 대상은 이를 직접 부르는 legacy application 경로이며 실제 live main에서는
  호출되지 않았다.

### 3. trusted `cash_usage_percent`

Status: **RESOLVED**

broker cash 30,000원, 동일 가격·환율 조건에서 신규 live pipeline을 end-to-end 실행했다.

| Trusted usage | Actual qty | Actual notional | Pipeline context |
|---:|---:|---:|---:|
| 100% | 30 | 30,000원 | 100% |
| 90% | 27 | 27,000원 | 90% |
| 50% | 15 | 15,000원 | 50% |

- trusted 설정 허용 범위는 정확히 1~100이다.
- 0, 101, NaN, Infinity, bool, 문자열은 fail-closed 처리된다.
- 50은 현재 보수적 trusted 설정값이지 별도 강제 maximum이 아니다.
- trusted 값을 90 또는 100으로 설정하면 각각 그대로 반영된다.

### 4. caller cash/percent isolation

Status: **RESOLVED**

- 신규 pipeline 공개 계약에는 caller `available_cash_krw` 및 `cash_usage_percent` 인자가 없다.
- 두 이름을 임의 전달한 직접 재현은 모두 `TypeError`였고 broker 호출은 0회였다.
- sizing 현금은 broker Account Engine snapshot에서만 가져왔다.
- usage는 trusted operator configuration에서만 가져왔다.
- 따라서 caller cash 3,000,000원 또는 caller percent 100%를 주입해도 sizing에 영향을 줄 수 없다.

### 5. affordability final-order enforcement

Status: **RESOLVED**

- sizing 성공 뒤 affordability 결과를 강제로 non-affordable로 만든 fault injection에서
  `LiveEntryPipelineError`가 발생했고 broker HTTP 호출은 0회였다.
- affordability는 표시용 결과가 아니라 Execution Engine 앞의 실제 차단 게이트다.

실제 경계 재현(broker cash 30,000원, trusted usage 90%, 주가 50,000원):

| Fractionable | Result | Qty | Broker calls |
|---|---|---:|---:|
| false | BLOCKED | 0 | 0 |
| true, minimum order 충족 | KEPT | 0.54 | 1 |

- `available_for_new_order_krw`는 27,000원 이하였다.
- non-fractionable 종목은 1주를 살 수 없어 최종 주문에서 차단됐다.
- fractionable 종목은 최소 주문 조건을 충족해 후보 및 주문 가능 상태를 유지했다.

### 6. CODEX-034/035 timeout and HTTP 5xx

Status: **RESOLVED**

real ledger + real `AlpacaBroker` + local fake session으로 pipeline 전체를 실행했다.

| Failure | First state | Retry result | Total POST calls | Reconciliation |
|---|---|---|---:|---|
| timeout | SUBMISSION_UNKNOWN | blocked | 1 | COMMITTED |
| HTTP 500 | SUBMISSION_UNKNOWN | blocked | 1 | COMMITTED |
| HTTP 502 | SUBMISSION_UNKNOWN | blocked | 1 | COMMITTED |
| HTTP 503 | SUBMISSION_UNKNOWN | blocked | 1 | COMMITTED |
| HTTP 504 | SUBMISSION_UNKNOWN | blocked | 1 | COMMITTED |

- ambiguous 응답 뒤 reservation은 release되지 않고 `SUBMISSION_UNKNOWN`으로 유지됐다.
- 동일 조건 재주문은 reservation 단계에서 차단되어 두 번째 POST가 발생하지 않았다.
- `client_order_id` broker 조회 reconciliation은 reservation을 `COMMITTED`로 전환했다.

## Executed tests

집중 테스트:

```text
venv/bin/python -m pytest -q \
  tests/test_main_live_entry_wiring.py \
  tests/test_live_entry_pipeline.py \
  tests/test_trusted_operator_config.py \
  tests/test_account_engine.py \
  tests/test_risk_engine.py \
  tests/test_sizing_engine.py \
  tests/test_execution_engine.py \
  tests/test_live_order_gateway.py \
  tests/test_watchlist_affordability.py

378 passed, 0 failed, 1 warning
```

전체 회귀:

```text
venv/bin/python -m pytest -q
1331 passed, 0 failed, 2 warnings
```

추가 수동 런타임 재현:

- live main Engine/legacy call spy
- trusted 100/90/50 end-to-end sizing
- caller cash/percent keyword injection
- forced affordability failure
- 30,000원/50,000원 fractional boundary
- timeout 및 HTTP 500/502/503/504 reservation/retry/reconciliation

## Warnings review

1. urllib3 `NotOpenSSLWarning`: 로컬 Python이 LibreSSL 2.8.3으로 빌드된 환경 호환 경고다.
   주문 로직 실패 또는 실제 네트워크 접근 증거가 아니다.
2. scanner unknown-field `RuntimeWarning`: 의도적으로 미지원 필드의 fail-closed skip을 검증하는
   기존 테스트가 발생시킨 예상 경고다.

안전 관련 신규 Finding으로 분류할 경고는 없다.

## Network safety

- 모든 broker/session은 fake 또는 monkeypatched local object였다.
- 실제 Alpaca, Slack, Yahoo 연결은 수행하지 않았다.
- fault-injection HTTP call count는 fake session 내부 계수이며 외부 socket 호출이 아니다.

## Operational file safety

테스트 전후 값:

| File | SHA-256 | Size | mtime |
|---|---|---:|---:|
| `order_history.csv` | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| `universe.csv` | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| `strategy_performance.csv` | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

- 세 파일 모두 hash, size, mtime 불변.
- root `TRADING_STATE.db*`, `POSITION_STORE.json`, `LIVE_ENTRY_RESERVATION.lock` 생성 없음.
- `git diff --check` 통과.
- 검증 시작 전 worktree는 clean이었고, 구현 파일은 수정하지 않았다.

## Residual risks and conditions

1. `SUBMISSION_UNKNOWN` reconciliation은 기능상 동작하지만 운영 restart orchestration에서 자동
   실행되는지까지 이번 검증에서 입증하지 않았다. 자동 화해 전까지 reservation이 유지되어
   중복 주문보다 availability block으로 귀결되는 운영 위험이다.
2. 저수준 `AlpacaBroker.submit_order()`는 최종 adapter로 계속 공개돼 있다. 현재 실제 live main은
   authoritative pipeline을 통과하지만, 미래에 추가되는 application entrypoint가 pipeline을
   우회하지 않도록 architecture test와 code review 규칙을 유지해야 한다.
3. 실제 credentials, 실계좌 provider 응답, 실제 거래소 주문은 검증하지 않았다.
4. `approved: false`, `live_enabled: false` 상태이므로 이 판정은 제한적 live review 준비 상태에만
   적용되며 live trading 승인이 아니다.

위 항목은 현재 재현 가능한 중복 주문·한도 우회 결함이 아니라 운영/미래 확장 조건이므로
CRITICAL/HIGH Finding으로 판정하지 않는다.

## Required next action

- 제한적 live review 전에 operator TBD, kill-switch 절차, reconciliation runbook을 사람이 검토한다.
- 실제 live trading은 별도 승인과 별도 최종 검증 전까지 활성화하지 않는다.
- merge, push, 배포, 승인값 변경은 이번 검증 범위에 포함하지 않는다.
