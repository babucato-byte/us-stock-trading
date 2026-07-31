# CODEX_REVIEW

## 1. 검증 대상

- 저장소: `us-stock-trading`
- 브랜치: `feature/kis-live-broker`
- 검증 커밋: `fe917a1338122a0d3d1c9ed17ed417cecf8c92f3`
- 기준 태그: `pre-kis-integration`
- 검증일: 2026-07-31
- 검증 시작 시 worktree: clean

HEAD는 지시된 `fe917a1`과 정확히 일치했다. 구현 코드 수정, 커밋, 병합, push, Oracle 배포,
실주문은 수행하지 않았다.

## 2. 최종 판정

Overall verdict: **BLOCKED**

KIS limited live review: **BLOCKED**

Oracle deployment: **DO_NOT_DEPLOY**

Live trading: **DO_NOT_ENABLE**

전체 테스트 1,613건은 두 번 모두 통과했다. 그러나 판정 기준에 명시된 차단 사유가 실제
런타임에서 재현됐다.

1. Alpaca 주문 비활성 플래그가 모두 false여도 `AlpacaBroker.submit_order()` 직접 호출이
   fake HTTP session에 도달했다.
2. `HALT=true`여도 `KISBroker.submit_order()` 직접 호출이 중앙 Execution Engine/Order Gate를
   거치지 않고 fake KIS 주문 endpoint에 도달했다.
3. 기존 UNKNOWN 주문과 내부에 없는 KIS 포지션이 있어도 buy pipeline이 reconciliation 결과를
   상수로 통과시키고 신규 매수를 제출했다.
4. KIS 매도 부분체결 2주가 `filled`로 오분류됐다.
5. 초기 제한적 실거래에서 분할익절, trailing, time stop, EOD 강제청산을 설정으로 비활성화할
   수 없다.
6. 계좌 불일치 오류가 실제 계좌번호와 허용 계좌번호 전체를 포함하며 Shadow 로그에도 기록될
   수 있다.

미해결 HIGH Finding이 있으므로 실거래 전환 조건을 충족하지 않는다.

## 3. 전체 테스트 결과

집중 KIS 안전 테스트:

```text
222 passed, 0 failed, 1 warning
```

첫 번째 전체 실행:

```text
venv/bin/python -m pytest -q
1613 passed, 0 failed, 0 skipped, 0 xfailed, 2 warnings
```

두 번째 전체 실행은 test 파일 순서를 역순으로 지정했다.

```text
rg --files tests | rg 'test_.*\.py$' | sort -r |
  xargs venv/bin/python -m pytest -q
1613 passed, 0 failed, 0 skipped, 0 xfailed, 2 warnings
```

두 실행 모두 통과해 관찰 가능한 순서 의존성은 재현되지 않았다. 경고는 로컬 LibreSSL/urllib3
호환 경고 1건과 미지원 scanner field를 의도적으로 skip하는 기존 테스트 경고 1건이다.

## 4. Findings

### CODEX-042 — HIGH — Alpaca data-only 정책을 direct broker 호출로 우회 가능

Status: **UNRESOLVED**

근거:

- `broker/broker_config.py:57-69`, `154-163`은 두 Alpaca 플래그가 기본 false라고 선언하지만
  `validate_alpaca_order_permitted()`이 `AlpacaBroker.submit_order()`에 배선되지 않았다고
  명시한다.
- `paper_strategy_order.submit_order()` wrapper는 정상적으로 차단하지만 broker 객체의 public
  method가 최종 경계에서 정책을 재검사하지 않는다.

직접 재현:

```text
TRADING_MODE=paper
ALPACA_ORDER_ENABLED=false
ALPACA_PAPER_ORDER_ENABLED=false
AlpacaBroker.submit_order(side="sell")

result.status_code = 200
fake Alpaca HTTP calls = 1
```

영향:

- 운영 경로에서 import·runtime injection·향후 caller가 wrapper를 우회할 수 있다.
- “운영 기본 설정의 Alpaca 주문 호출 수=0”이 final broker boundary에서 보장되지 않는다.
- 판정 기준의 “Alpaca 주문 우회 가능”에 해당한다.

필수 조치:

- 매수·매도 모두 `AlpacaBroker`의 실제 HTTP 직전 경계에서 data-only 정책을 fail-closed로
  강제한다.
- KIS migration 운영 모드에서 테스트 플래그 하나만으로 Alpaca 주문을 다시 열 수 없도록
  운영/test capability를 분리한다.
- direct method runtime negative test를 추가한다.

### CODEX-043 — HIGH — KIS direct submit/cancel과 HALT가 중앙 게이트를 우회

Status: **UNRESOLVED**

근거:

- `brokers/kis_broker.py:281-338`의 `submit_order()`는 KIS credential/order-enabled와 limit
  type만 검사한다. Execution Engine provenance, Order Gate 승인, HALT, reconciliation,
  idempotency를 검사하지 않는다.
- `cancel_order()`도 동일하게 public이며 중앙 엔진에 cancel orchestration이 없다.
- `execution/execution_engine.py:49-128`은 broker를 호출하는 권장 경로지만 broker boundary가
  이를 기술적으로 강제하지 않는다.
- sell adapter와 sell Execution Engine은 `operations.kill_switch.is_halted()`를 검사하지 않는다.

직접 재현:

```text
OPERATIONS HALT = true
KIS live_order_enabled = true
KISBroker.submit_order(limit buy) 직접 호출

result = ACCEPTED
fake KIS order endpoint calls = 1
```

영향:

- 전략, 신규 entrypoint, runtime injection이 중앙 Order Gate와 멱등성을 건너뛸 수 있다.
- HALT가 “매수·매도 포함 모든 자동 주문 중지”라는 운영 계약을 최종 경계에서 보장하지 않는다.
- 판정 기준의 “KIS 실주문 게이트 우회 가능”에 해당한다.

필수 조치:

- 외부에 노출되는 KIS state-mutating method가 검증된 one-time authorization/capability 없이는
  HTTP에 도달하지 못하도록 한다.
- buy/sell/cancel 최종 경계에서 HALT를 매 호출 재확인한다.
- cancel을 포함한 모든 state mutation을 중앙 엔진 상태머신과 멱등성 정책에 연결한다.

### CODEX-044 — HIGH — 실제 리콘실리에이션 결과 대신 안전 사실을 상수로 통과

Status: **UNRESOLVED**

근거:

- `kis_live_trading.py:215-225`는 `has_order_for_signal_id=False`,
  `reconciliation_ok=True`, `has_unknown_order=False`를 상수로 주입한다.
- `reconcile_positions`와 `reconcile_unknown_order`를 import하지만 buy gate context 작성에
  실제 결과를 사용하지 않는다.
- KIS position read 실패도 `existing_positions=[]`로 처리한다(`kis_live_trading.py:198-203`).
- sell adapter도 `reconciliation_ok=True`, `has_unknown_order=False`를 상수로 주입한다
  (`brokers/kis_broker_adapter.py:163-169`).

직접 재현:

```text
durable idempotency row: UNKNOWN (MSFT buy)
KIS actual position: TSLA 1주
internal position: 없음
new candidate: AAPL

result.submitted = ["AAPL"]
broker submit calls = 1
```

영향:

- UNKNOWN 또는 내부↔KIS 잔고 불일치 상태에서 신규 매수가 진행된다.
- “복구 전 fail-closed”와 “리콘실리에이션 실패 시 신규 매수 차단”을 위반한다.
- 판정 기준의 명시적 BLOCKED 조건이다.

필수 조치:

- account/position/open-order/fill/idempotency reconciliation을 실제로 실행해 gate context에
  공급한다.
- 조회 실패·손상 DB·UNKNOWN 미해결을 모두 `reconciliation_ok=False`로 처리한다.
- 매수와 매도 모두 동일한 durable snapshot 안에서 이 사실을 확정한다.

### CODEX-045 — HIGH — KIS 매도 부분체결을 전체 체결로 오분류

Status: **UNRESOLVED**

근거:

- `brokers/kis_broker_adapter.py:240-250`은 동일 주문의 fill row에서 `filled_qty>0`이면
  요청수량과 무관하게 status를 항상 `"filled"`로 반환한다.
- `positions/lifecycle.py`는 `"filled"`를 전체 exit 체결로 처리하므로 부분 수량만 체결돼도
  해당 exit intent의 요청수량 전체가 체결된 것으로 반영될 수 있다.
- 여러 fill row를 누적·정렬하지 않고 첫 일치 row만 사용한다.

직접 재현:

```text
requested sell quantity: 5
KIS fill row quantity: 2
KISBrokerAdapter.get_order_by_client_order_id():
  {"status": "filled", "filled_qty": 2.0, ...}
```

영향:

- 내부 remaining quantity와 KIS 실제 보유수량이 달라지고 position이 조기 종료될 수 있다.
- 부분체결 후 잔여수량 보존이라는 필수 조건을 만족하지 않는다.

필수 조치:

- requested quantity와 KIS 누적 체결수량을 비교해 `partially_filled`/`filled`를 구분한다.
- 여러 체결 row를 order identity 기준으로 멱등적으로 누적하고 out-of-order/repeated row를
  검증한다.

### CODEX-046 — HIGH — 초기 고위험 매도 기능을 설정으로 비활성화 불가

Status: **UNRESOLVED**

근거:

- `positions/lifecycle.py:631-668`은 EOD 강제청산, time stop, stop, 분할익절, target 2,
  trailing 전이를 항상 평가한다.
- `config/scalping_strategy_v1_config.py:84-103`에는 임계값과 비율만 있고 각 기능의 enable
  switch가 없다.
- KIS position manager는 이 lifecycle을 그대로 호출한다.

영향:

- 초기 제한적 실거래에서 요구된 분할익절, trailing stop, time stop, EOD 강제청산을 선택적으로
  차단할 수 없다.
- 지시문의 필수 판정에 따라 HIGH 이상이다.

필수 조치:

- KIS initial rollout용 fail-closed feature flags를 추가하고 final sell decision 경계에서
  강제한다.
- 기본값은 모두 disabled로 두고 stop-loss 등 허용할 기능을 운영자가 명시적으로 승인하게 한다.

### CODEX-047 — MEDIUM — 주문 상태머신이 Execution Engine의 durable transition을 강제하지 않음

Status: **UNRESOLVED**

근거:

- `execution/order_state_machine.py`는 11개 상태와 전이 그래프를 정의한다.
- 실제 Execution Engine은 `transition()`을 호출하지 않고 DB status를 CREATED에서
  SUBMITTING, 이후 broker record status로 직접 덮어쓴다(`execution/execution_engine.py:75-87`,
  `113-125`).
- VALIDATING/APPROVED 전이는 durable row에 기록되지 않는다.
- cancel path는 중앙 상태머신에 연결되지 않는다.

영향:

- 불법 전이 방지 테스트는 pure helper에만 적용되고 실제 주문 persistence에는 적용되지 않는다.
- 취소 요청과 fill/update 경쟁을 하나의 상태 전이 규칙으로 직렬화하지 못한다.

### CODEX-048 — MEDIUM — Shadow Mode가 모든 승인·차단 시도를 완전하게 기록하지 않음

Status: **UNRESOLVED**

근거:

- Shadow record는 order intent와 gate context가 완성된 뒤에만 만들어진다.
- symbol allow-list, 분석 실패, KIS 가격/계좌/open-order 조회 실패, 현금 부족 등 앞단 차단은
  `results`에만 남고 JSONL에는 기록되지 않는다.
- 승인 record는 broker 호출 이후에 persist되어 “주문 직전 전체 경로 기록”이 아니다.
- `shadow_mode.persist()`는 file lock/fsync/rotation/size limit 없이 append한다.
- malformed trailing line은 읽을 때 조용히 skip하므로 손상 사실을 audit에서 숨길 수 있다.

영향:

- 승인·차단 전체에 대한 완전한 shadow audit을 보장하지 못한다.
- 동시 프로세스 기록과 장기 운영 용량 정책이 없다.

### CODEX-049 — MEDIUM — Oracle 배포 런북이 현재 구현 및 서비스 구조와 불일치

Status: **UNRESOLVED**

근거:

- 런북 §13은 현재 존재하는 `shadow_mode.py`를 “미구현”이라고 설명하고 대체 절차를 제시한다.
- systemd 전환 절차는 기존 `order-monitor`/dashboard만 재시작하며 KIS buy cycle,
  `sync_kis_fills_and_manage_exits()`, reconciliation-first startup을 실행하는 실제 service/timer
  entrypoint를 지정하지 않는다.
- 일반 취소 TR_ID와 현재가 response field가 `TBD_VERIFY_LIVE_DOCS`인 상태다.
- migration 6 적용 확인은 전체 테스트에 간접 의존하고 운영 DB의 사전 백업/복사본 migration
  검증 명령이 없다.

영향:

- Oracle 접근 운영자가 런북만으로 추가 설계 없이 안전하게 배포할 수 없다.

### CODEX-050 — HIGH — 계좌번호 전체가 오류 및 Shadow 로그에 노출 가능

Status: **UNRESOLVED**

근거:

- buy gate 계좌 불일치 예외는 실제 `kis_account_no`와 `allowed_account_no` 전체를 문자열에
  포함한다(`execution/order_gate.py:90-95`).
- `kis_live_trading.py`는 gate 예외 전체를 `rejection_reason`으로 Shadow JSONL에 기록한다.

영향:

- 로그·예외에 계좌번호 전체를 노출하지 말라는 비밀정보 요구를 위반한다.
- 판정 기준의 “민감정보 노출”에 해당한다.

필수 조치:

- 계좌번호를 마스킹하거나 irreversible identifier로 비교·기록한다.
- 기존 생성 가능 로그를 운영 배포 전에 점검하고 접근권한/보존정책을 정의한다.

## 5. Alpaca 주문 차단 근거

부분 통과:

- `paper_strategy_order.submit_order()`를 통한 기본 paper/live buy/sell은 두 Alpaca 주문
  플래그가 false일 때 fake session 호출 0회로 차단된다.
- KIS pipeline 자체에는 Alpaca order client import가 없다.
- KIS 실패 시 자동으로 Alpaca로 fallback하는 분기 역시 발견되지 않았다.

실패:

- 최종 `AlpacaBroker.submit_order()` 경계는 동일 플래그를 검사하지 않는다.
- direct runtime 재현에서 fake HTTP 호출 1회가 발생했다.

결론: 운영 기본 설정에서 구조적으로 Alpaca 주문 호출 0회를 보장하지 못한다.

## 6. KIS 매수 경로 근거

구현된 정상 경로:

```text
Alpaca-derived candidate
→ signal TTL
→ KIS price
→ price deviation
→ KIS account/orderable cash
→ integer sizing
→ idempotency register
→ buy Order Gate
→ KIS limit order
→ accepted/unknown/rejected persistence
```

확인된 안전 요소:

- fractional quantity, market order, extended-hours, insufficient cash, allow-list 외 symbol,
  leveraged/inverse/short/margin 정책은 정상 경로의 config/gate에서 차단된다.
- signal TTL과 KIS 가격 편차가 검사된다.
- timeout/5xx는 UNKNOWN으로 남고 동일 signal/id의 재제출은 durable uniqueness로 차단된다.

미충족:

- reconciliation과 UNKNOWN facts가 상수로 우회된다(CODEX-044).
- KIS broker direct method가 전체 경로를 우회한다(CODEX-043).
- 자동 instrument 분류 대신 운영 allow-list가 leveraged/inverse 판별 책임을 가진다.

## 7. KIS 매도 경로 근거

구현된 부분:

- KIS actual position quantity와 internal remaining quantity가 일치해야 lifecycle exit 평가가
  진행된다.
- stop/target은 entry signal price가 아니라 KIS fill의 average fill price에서 계산된다.
- sell gate는 보유수량 초과와 동일 symbol open sell order를 차단한다.
- exit intent는 broker 호출 전에 durable하게 예약되며 UNKNOWN은 자동 재제출되지 않는다.

미충족:

- 부분체결 row가 전체 체결로 오분류된다(CODEX-045).
- sell gate의 reconciliation/UNKNOWN facts가 상수다(CODEX-044).
- HALT는 sell adapter/Execution Engine/final broker boundary에서 강제되지 않는다(CODEX-043).
- 고위험 exit 기능별 initial disable switch가 없다(CODEX-046).

## 8. 멱등성·상태머신 근거

통과:

- migration 6은 `internal_order_id`와
  `(signal_id, symbol, side, trading_date)` UNIQUE constraint를 생성한다.
- 같은 DB를 재개방해 migration 6을 재실행해도 version 6 상태가 유지된다.
- 동일 ID/signal 재시도, thread/process 경쟁 테스트가 통과한다.
- ambiguous broker response는 UNKNOWN으로 기록되고 동일 identity의 재제출은 차단된다.

미충족:

- direct KIS broker 호출은 migration 6을 사용하지 않는다.
- actual Execution Engine이 pure state machine transition graph를 사용하지 않는다.
- cancel state/race가 중앙 엔진에서 durable하게 직렬화되지 않는다.

## 9. 리콘실리에이션 근거

pure reconciler는 internal↔KIS position/account/order mismatch를 검출하고 자동 반대주문을 만들지
않는다. KIS에만 존재하는 position도 mismatch로 보고한다.

그러나 실제 buy/sell gate context에 이 결과가 연결되지 않았다. 직접 재현에서 UNKNOWN과 KIS-only
position이 동시에 있어도 신규 buy가 제출됐다. 따라서 recovery 전 fail-closed 운영 계약은
충족하지 않는다.

## 10. Shadow Mode 근거

필수 dataclass field 18개는 모두 존재하고 정상 gate 승인/차단 결과는 JSONL로 기록된다. 테스트
경로는 임시 파일로 격리됐다.

다만 앞단 차단 전체가 기록되지 않고, 승인 로그가 주문 이후 작성되며, 동시 writer lock과
rotation/size policy가 없다. 완전한 운영 Shadow Mode로 판정할 수 없다.

## 11. 환경·비밀정보

통과:

- tracked real secret/key file은 발견되지 않았다. `.env.example`만 추적된다.
- `.env`, `*.lock`, 운영 DB/CSV/log는 gitignore 대상이다.
- KIS read/order flags와 live rollout은 기본 false다.
- missing credentials는 network 전에 차단된다.

미충족:

- `ENTRY_DISABLED=true`는 코드 기본 상태가 아니라 런북 환경 설정에 의존한다.
- KIS 계좌번호 전체가 gate error/Shadow log에 노출될 수 있다(CODEX-050).
- `.env.example`은 KIS 안전 기본값을 포함하지 않아 런북과 설정 template이 불일치한다.

## 12. Oracle 배포 준비도

판정: **NOT_READY**

백업, 별도 release directory, venv, secret file, migration, test, readonly, rollback의 큰 순서는
존재한다. 그러나 Shadow 설명이 현재 코드와 불일치하고, 실제 KIS cycle/reconciliation-first
service entrypoint가 런북에 없으며, 미확인 KIS API field/TR_ID가 남아 있다. 더 중요하게
CODEX-042~046/050의 HIGH 안전 결함이 해결되지 않았다.

## 13. 네트워크 및 운영 파일 안전

- 실제 Alpaca/KIS/Slack/Yahoo 호출은 0회다.
- “HTTP call” 재현은 주입된 in-memory fake session의 call counter다.
- 테스트 전후 운영 파일:

| File | SHA-256 | Size | mtime |
|---|---|---:|---:|
| `order_history.csv` | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| `universe.csv` | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| `strategy_performance.csv` | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

- hash, size, mtime 모두 전후 불변이다.
- root `TRADING_STATE.db*`, `SHADOW_MODE_LOG.jsonl`, `KIS_ORDER_IDEMPOTENCY.lock`,
  `OPERATIONS_HALT_STATE.json` 생성 없음.
- 보고서 갱신 전 `git diff --check`는 clean이었다.

## 14. 실거래 활성화 전 필수 조건

1. CODEX-042~046 및 CODEX-050 HIGH Finding을 모두 해결하고 직접 runtime regression test를
   추가한다.
2. Alpaca/KIS state-mutating broker boundary가 중앙 authorization 없이 절대 HTTP에 도달하지
   않도록 한다.
3. 실제 reconciliation/UNKNOWN state를 buy/sell gate에 연결하고 recovery-first startup을
   구현한다.
4. 부분체결 누적·상태·잔여수량·PnL을 KIS 실제 체결내역 기준으로 end-to-end 검증한다.
5. initial rollout의 고위험 exit 기능을 기본 disabled로 만들고 운영 승인 항목을 문서화한다.
6. 상태머신과 cancel concurrency를 actual durable execution path에 연결한다.
7. Shadow Mode 완전성, 동시 기록, rotation과 민감정보 마스킹을 보강한다.
8. Oracle runbook을 실제 KIS service/timer와 current Shadow 구현에 맞게 갱신한다.
9. Oracle의 DB 복사본에서 migration 6, readonly KIS 응답 field/TR_ID, 재부팅 후 reconciliation을
   검증한다.
10. 수정 커밋을 다시 독립 검증하기 전까지 `KIS_LIVE_ORDER_ENABLED=false`,
    `LIVE_ROLLOUT_ENABLED=false`, `ENTRY_DISABLED=true`를 유지한다.

현재 커밋은 merge, push, Oracle deploy 또는 실거래 활성화 대상으로 승인하지 않는다.
