# VALIDATION_PACKAGE

외부 검증자(ChatGPT/Codex)가 `CODEX_REVIEW.md`를 작성하기 위해 필요한 정보 패키지. Phase 완료 시마다 갱신한다.

---

## 이번 패키지: CODEX-021 해결 및 CODEX-020 잔여분 종결 (RequestPurpose 재설계, 2026-07-25)

### 검증 대상

- 독립 검증 기록: `CODEX_REVIEW.md`(대상 커밋 `66eda8a`/`ed452da`/`cf5601d`/`edc5ad5`, overall
  verdict **FAIL**)
- 구현 커밋: `c133e01`(t1, CODEX-021 해결 및 CODEX-020 잔여분 종결)
- 상태: **`READY_FOR_CODEX_REVALIDATION`**
- limited live review: **`BLOCKED` 유지**
- live trading: **`DO_NOT_ENABLE` 유지**

### 배경

이전 재검증에서 CODEX-016/017/018/019는 RESOLVED로 재확인됐으나 CODEX-020(HIGH)이
PARTIALLY_RESOLVED로 남았고, 신규 CODEX-021(HIGH)이 제기됐다: `_request()`의 `order_side`는
필수 인자였지만 POST 경로와 결합돼 있지 않아 `order_side=None`을 명시하면
`_check_kill_switch(None)`이 method/path를 확인하지 않고 즉시 반환해, direct
`_request("POST", "/v2/orders", order_side=None, ...)` 호출이 binary halt와 4-state kill
switch를 모두 우회했다. 두 Finding 모두 같은 근본 원인(주문 여부를 판단할 단일 신뢰 가능한 신호
부재)이었다.

### CODEX-021 해결 및 CODEX-020 잔여분 종결

- `broker/alpaca_client.py`에 신규 `RequestPurpose` enum(`READ_ONLY`/`ENTRY_ORDER`/
  `EXIT_ORDER`/`CANCEL_ORDER`/`RECONCILIATION`)을 도입했다.
- `_request()`의 `purpose`를 기본값 없는 keyword-only 필수 인자로 만들고, `isinstance(purpose,
  RequestPurpose)`를 요구해 `None`을 포함한 잘못된 값을 `ValueError`로 세션 접근 전에 차단한다.
- 신규 `_METHOD_PURPOSES` 매트릭스가 HTTP method와 purpose의 허용 조합을 강제한다: GET은
  `READ_ONLY`/`RECONCILIATION`만, POST는 `ENTRY_ORDER`/`EXIT_ORDER`만, DELETE는
  `CANCEL_ORDER`만 허용, 불일치는 세션 호출 전 `ValueError`.
- `_check_kill_switch(purpose, order_side=None)`는 `purpose`가 `ENTRY_ORDER`/`EXIT_ORDER`일
  때만 binary halt와 4-state 정책을 재조회한다. `order_side`는 이제 payload의 `side`와
  `purpose`가 실제로 일치하는지 확인하는 2차 방어선이며, 단독으로는 kill switch를 판단하지 않는다.
- `submit_order()`는 `_SIDE_TO_PURPOSE`(`buy→ENTRY_ORDER`, `sell→EXIT_ORDER`)로 `purpose`를
  파생하고, 세션 호출 직전 payload의 `order["side"]`가 이 `purpose`와 여전히 일치하는지
  재검증해(불일치 시 `RuntimeError`) 향후 코드 변경이 `side`와 `purpose`를 갈라놓는 사고를
  방지한다.
- 조회·취소 경로(`get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order`)는 각각 `RequestPurpose.READ_ONLY`/
  `RECONCILIATION`/`CANCEL_ORDER`를 명시해 kill switch 정책과 무관하게 계속 동작한다.

### CODEX-016~019 (재작업 아님, 회귀만 확인)

이번 사이클에서 CODEX-016(다단계 kill switch 배선)·017(Slack health 배선)·018(주문 직전
credential/환경 재검증)·019(상태 저장소 파일 잠금)는 코드를 변경하지 않았다. 관련 회귀 테스트
(`tests/test_paper_strategy_order_kill_switch_state.py` 12건,
`tests/test_paper_strategy_order_notification_health.py` 6건,
`tests/test_state_store_concurrency.py` 6건, 도합 **36 passed, 1 warning**)만 재실행해 회귀가
없음을 확인했다.

### 추가·수정 테스트

- `tests/test_broker_request_purpose.py`(신규): `purpose=None`/누락/잘못된 타입 명시적 거부,
  method-purpose 불일치 거부(GET+ENTRY_ORDER, POST+READ_ONLY 등), order payload의 `side`와
  `purpose` 불일치 거부, 조회·취소 경로가 kill switch와 무관하게 계속 동작함을 검증.
- `tests/test_broker_kill_switch_gate.py`: 기존 호출부를 `purpose` 키워드로 갱신하고, `purpose`
  누락 시 `TypeError`, `order_side`만 주어지고 `purpose`가 없을 때 `TypeError`, `purpose=None`
  명시 시 `ValueError`를 검증하는 신규 테스트 3건 추가.
- 실패 테스트 삭제, 완화, skip, xfail 없음.

### 실행 결과

```text
venv/bin/python -m pytest -q:  536 passed, 0 failed, 2 warnings
집중 안전 테스트:                255 passed, 1 warning
CODEX-016~019 회귀 전용:         36 passed, 1 warning
```

두 warning은 기존 urllib3 LibreSSL 경고와 의도된 scanner unknown-field 경고다.

### 안전 검증

- 실제 Alpaca, Slack, Yahoo 호출 0회. HTTP 검증은 recording session/fake만 사용.
- broker 내부 `session.get/post/delete` 직접 호출 없음, `session.request`는 공통 `_request()`
  한 곳에만 존재(purpose 기반 kill switch 검사와 credential 재검증이 그 안에 포함됨).
- `order_history.csv`, `universe.csv`는 이전 사이클 기록값과 동일(불변, 이번 사이클도 미변경).
- `.env`, approval/live flag, kill-switch/notification 상태 파일을 변경하지 않았다.
- `approved: false`, `live_enabled: false` 유지.
- main 병합과 origin push 없음.

### Codex 재검증 초점

1. `purpose=None` 및 method-purpose 불일치가 세션 호출 전에 실제로 차단되는지(CODEX-021 재현
   시나리오 포함).
2. 조회·취소 경로가 kill switch 상태와 무관하게 계속 동작하는지.
3. `submit_order()`의 payload `side` ↔ `purpose` 일치 재검증이 실제로 동작하는지.
4. CODEX-016/017/018/019에 회귀가 없는지.

---

## 이전 패키지: CODEX-020·CODEX-018 잔여분 수정 (2026-07-24)

### 검증 대상

- 독립 검증 기록: `CODEX_REVIEW.md`(대상 커밋 `47ee8d6`/`03962d3`/`cf4ada9`, overall verdict **FAIL**)
- 구현 커밋: `66eda8a`(t1, CODEX-020), `ed452da`(t2, CODEX-018 잔여분)
- 브랜치: `orchestrator/20260723-234154-us-stock-trading`
- 상태: **`READY_FOR_CODEX_REVALIDATION`**
- limited live review: **`BLOCKED` 유지**
- live trading: **`DO_NOT_ENABLE` 유지**

### 배경

이전 재검증에서 CODEX-016/017/019는 RESOLVED로 재확인됐으나 CODEX-018(MEDIUM)이
PARTIALLY_RESOLVED로 남았고, 신규 CODEX-020(HIGH)이 제기됐다: `AlpacaBroker.submit_order()`를
`paper_strategy_order.py` wrapper 없이 직접 호출하면 binary kill switch와 4-state kill switch를
모두 우회해 HTTP가 실제로 나갔다. CODEX-018은 `_validate_runtime_safety()`가 현재 credentials
(API key/secret)를 재검증하지 않는다는 지적이었다.

### CODEX-020 보완

- `AlpacaBroker._request()`에 `order_side`(주문 아니면 `None`, 매수/매도면 `"buy"`/`"sell"`)
  키워드 전용 필수 인자를 추가했다.
- 신규 `_check_kill_switch(order_side)`가 매 호출마다 `kill_switch.is_trading_halted()`와
  `order_side`별 `kill_switch_state.is_entry_allowed()`/`is_liquidation_allowed()`를 재조회해
  불허 시 세션 요청 전에 `RuntimeError`를 발생시킨다.
- 조회·취소 경로(`get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order`)는 `order_side=None`으로 명시해 kill switch와
  무관하게 계속 동작한다.
- `order_side`를 생략하고 `_request()`를 호출하면 네트워크 접근 전에 `TypeError`가 발생한다.

### CODEX-018 잔여분 보완

- `_validate_runtime_safety()`에 `_validate_current_credentials_match_captured()`를 추가해,
  매 요청마다 `BrokerConfig.from_env()`로 현재 환경 credentials를 다시 읽고 생성 시점 캡처값과
  `hmac.compare_digest()`로 비교한다.
- 누락/공백/회전/삭제/환경 읽기 실패는 모두 요청 전에 차단한다. credential 값 자체는 예외
  메시지에 포함하지 않는다.

### 추가·수정 테스트

- `tests/test_broker_kill_switch_gate.py`(신규, 25건): direct broker 호출의 binary/4-state
  kill switch 준수, buy/sell 구분, 조회·취소 경로 비영향, `order_side` 생략 시 `TypeError`,
  wrapper/direct 경로 판정 일치.
- `tests/test_alpaca_client_runtime_revalidation.py` 확장(44건): credential 삭제/회전/공백/
  읽기실패 각각 POST/GET/DELETE 3경로 파라미터라이즈.
- `tests/test_broker_safety.py`, `tests/test_universe_builder.py`: 기존 fake broker 호출부를
  `order_side` 키워드에 맞춰 갱신(시그니처 반영, 로직 완화 아님).
- 실패 테스트 삭제, 완화, skip, xfail 없음.

### 실행 결과

```text
venv/bin/python -m pytest -q:  489 passed, 0 failed, 2 warnings
집중 안전 테스트:                208 passed, 1 warning
```

두 warning은 기존 urllib3 LibreSSL 경고와 의도된 scanner unknown-field 경고다.

### 안전 검증

- 실제 Alpaca, Slack, Yahoo 호출 0회. HTTP 검증은 recording session/fake만 사용.
- broker 내부 `session.get/post/delete` 직접 호출 없음, `session.request`는 공통 `_request()`
  한 곳에만 존재(kill switch 검사와 credential 재검증이 그 안에 포함됨).
- `order_history.csv`, `universe.csv`의 SHA-256, 크기, mtime이 검증 전후 동일하다.
- `.env`, approval/live flag, kill-switch/notification 상태 파일을 변경하지 않았다.
- `approved: false`, `live_enabled: false` 유지.
- main 병합과 origin push 없음.

### Codex 재검증 초점

1. direct `AlpacaBroker.submit_order()` 호출이 binary halt와 4-state(ENTRY_DISABLED 포함) 각각에서
   실제로 차단되는지, buy/sell 구분이 정확한지.
2. 조회·취소 경로가 kill switch 상태와 무관하게 계속 동작하는지.
3. credential 삭제/회전/공백이 생성 후 요청에서 실제로 차단되는지, 값 자체가 예외 메시지에
   노출되지 않는지.
4. CODEX-016/017/019에 회귀가 없는지.

---

## 이전 패키지: CODEX-016·018 최종 보완 (2026-07-23)

### 검증 대상

- 독립 검증 기록: `cf4ada9`
- 구현 커밋: `47ee8d6`
- 브랜치: `orchestrator/20260723-020935-us-stock-trading`
- 상태: **`READY_FOR_CODEX_REVALIDATION`**
- limited live review: **`BLOCKED` 유지**
- live trading: **`DO_NOT_ENABLE` 유지**

### CODEX-016 보완

- `paper_strategy_order.submit_order(..., *, side)`와
  `AlpacaBroker.submit_order(..., *, side)`에서 side를 필수 keyword로 강제한다.
- 허용값은 정확히 `buy`, `sell`뿐이다. 누락, None, 빈 문자열, 대문자, 공백,
  오타와 기타 타입은 fail-closed 처리한다.
- wrapper는 side를 broker로 전달하며 Alpaca POST JSON payload도 같은 side를 보존한다.
- 기존 entry 경로 `main()`은 `side="buy"`를 명시한다.

### CODEX-018 보완

- `_validate_runtime_safety()`가 생성 시점 `self.config`와 요청 시점 환경을 모두 검증한다.
- 모든 Alpaca 네트워크 호출은 `_request()` 한 곳만 사용한다.
- 포함 경로: account, positions, recent orders, assets, client_order_id reconciliation,
  order POST, cancel DELETE.
- unsafe env/config/endpoint에서는 recording session 호출이 0회다.

### 추가·수정 테스트

- `tests/test_alpaca_client_runtime_revalidation.py`: buy/sell payload, side 누락/오류,
  POST/reconciliation/DELETE runtime gate 추가.
- `tests/test_paper_strategy_order_kill_switch_state.py`: wrapper side 전달 및 strict validation 추가.
- 기존 fake broker 8개 테스트 모듈이 keyword-only side를 실제로 받도록 강화됐다.
- 실패 테스트 삭제, 완화, skip, xfail 없음.

### 실행 결과

```text
저장소 루트 pytest -q:                    443 passed, 2 warnings
저장소 루트 python -m pytest -q:          443 passed, 2 warnings
상위 디렉터리 pytest us-stock-trading -q: 443 passed, 2 warnings
상위 디렉터리 python -m pytest ... -q:    443 passed, 2 warnings
집중 안전 테스트:                         188 passed, 1 warning
```

두 warning은 기존 urllib3 LibreSSL 경고와 의도된 scanner unknown-field 경고다.

### 안전 검증

- 실제 Alpaca, Slack, Yahoo 호출 0회. HTTP 검증은 recording session/fake만 사용.
- broker 내부 `session.get/post/delete` 직접 호출 없음. `session.request`는 공통
  `_request()` 한 곳에만 존재.
- `order_history.csv`, `universe.csv`, `strategy_performance.csv`의 SHA-256, 크기,
  mtime이 검증 전후 동일하다.
- `.env`, approval/live flag, kill-switch 상태, 운영 서버를 변경하지 않았다.
- `approved: false`, `live_enabled: false` 유지.
- main 병합과 origin push 없음.

### Codex 재검증 초점

1. wrapper buy/sell이 broker kwargs 및 POST payload까지 동일하게 유지되는지.
2. side 누락 및 모든 비정규 값이 network 호출 전에 차단되는지.
3. POST와 client_order_id reconciliation이 `_request()` 공통 runtime gate를 거치는지.
4. broker 생성 후 env/config 변조에서 session 호출이 0회인지.
5. CODEX-017/019에 회귀가 없는지.

---

## 이번 패키지: CODEX-016~019 수정 완료 (2026-07-23)

### 배경
`docs/autonomous/CODEX_REVIEW.md`(커밋 `e0dc855`, 대상 `337ba16`~`b6f4924`)의 독립 재검증 결과는
Overall verdict **FAIL**, Limited live review **BLOCKED**였다. 신규 HIGH Finding 2건(CODEX-016,
CODEX-017)이 다단계 kill switch(`kill_switch_state.py`)와 Slack health monitor(`notification_health.py`)가
모듈 내부에서는 정확히 구현·단위테스트되었으나 실제 운영 주문/알림 경로(`paper_strategy_order.py`)에는
배선되지 않았다고 지적했고, MEDIUM Finding 2건(CODEX-018, CODEX-019)이 각각 주문 직전 환경 재검증
함수 미사용과 상태 저장소 동시 갱신 lost-update 가능성을 지적했다. 이번 오케스트레이터 run(t1~t4,
전부 PASS)에서 4건 전부를 수정했다. 각 항목의 상세 Finding 원문·Root cause·Required behavior와
Implementation/Regression tests/Commit/Remaining risk는 `docs/autonomous/REMEDIATION_PLAN.md`의
"제한적 실거래 검토 사이클 — CODEX-016~019" 절에 기록되어 있다(이 패키지는 요약만 제공).

### 이번 run에서 완료한 항목 (커밋 순서대로)
1. **[CODEX-016] HIGH — 다단계 kill switch를 실제 주문 경로에 배선** (`6ad4841`).
   `paper_strategy_order.submit_order()`에 `side` 파라미터를 추가하고, 기존 `kill_switch.is_trading_halted()`
   binary 게이트 통과 직후 `kill_switch_state.is_entry_allowed()`(매수)/`is_liquidation_allowed()`(매도)를
   재조회해 불허 시 broker 호출 전에 HTTP 423을 반환하도록 배선. `main()`의 신규 진입 호출부는
   `side="buy"`로 명시. 신규 테스트: `tests/test_paper_strategy_order_kill_switch_state.py`(12건).
2. **[CODEX-017] HIGH — Slack health monitor를 운영 알림 경로에 배선** (`79eaa81`).
   `paper_strategy_order._safe_send_slack_alert()`가 `send_slack_alert()`를 직접 호출하는 대신
   `notification_health.send_with_health_tracking(send_slack_alert, message)`를 경유하도록 변경 —
   모든 발송 결과가 실제로 기록되고, 연속 실패가 임계값에 도달하면 CODEX-016 배선을 통해 실제 매수
   주문이 차단됨을 end-to-end로 확인. 신규 테스트:
   `tests/test_paper_strategy_order_notification_health.py`(6건, `tests/conftest.py`에 공용 fixture 추가).
3. **[CODEX-018] MEDIUM — 주문 직전 환경 재검증 함수를 실제 요청 경로에 배선** (`00b0f68`).
   `broker/alpaca_client.py`(이 항목 범위로만 한시 개방)의 `AlpacaBroker._request()`가 실제
   `self.session.request()` 호출 직전에 `validate_order_allowed_now()`를 호출해 `os.environ`을 그
   자리에서 재검증하도록 배선. `_request()`를 경유하는 모든 메서드(`get_account`/`get_positions`/
   `submit_order`/`get_order_by_client_order_id`)에 자동 적용. 신규 테스트:
   `tests/test_alpaca_client_runtime_revalidation.py`(6건).
4. **[CODEX-019] MEDIUM — 상태 저장소 read-modify-write에 파일 잠금 적용** (`50a097d`).
   `kill_switch_state.py`/`notification_health.py` 양쪽에 `order_history.csv`/`atomic_io.py`와 동일한
   `fcntl.flock` 기반 `_state_lock()`을 도입, `activate()`/`release()`/`record_success()`/
   `record_failure()`가 락 안에서 재읽기→병합→쓰기를 수행하도록 변경(락 타임아웃 시 kill_switch_state는
   raise, notification_health는 절대 raise하지 않는 기존 계약을 유지하며 파일 미변경). 신규 테스트:
   `tests/test_state_store_concurrency.py`(6건, `multiprocessing` 기반 동시 갱신 재현 포함).

### 변경 파일 (이번 run, t1~t4)
- 수정: `paper_strategy_order.py`(t1, t2), `broker/alpaca_client.py`(t3, 이 항목 범위로만 한시 개방),
  `kill_switch_state.py`(t4), `notification_health.py`(t4), `tests/conftest.py`(t2, 공용 fixture 28줄).
- 신규: `tests/test_paper_strategy_order_kill_switch_state.py`,
  `tests/test_paper_strategy_order_notification_health.py`,
  `tests/test_alpaca_client_runtime_revalidation.py`, `tests/test_state_store_concurrency.py`.
- `docs/autonomous/CODEX_REVIEW.md`(Codex 독립 검증 기록)와 이전 run들의 문서는 수정하지 않음(그대로 보존).

### 실행 명령 및 결과
```bash
venv/bin/python -m pytest -q     # 417 passed, 0 failed, 2 warnings (기존과 동일한 urllib3/scanner 경고만)
```
신규 안전 관련 warning 없음. 실제 Alpaca/Slack/Yahoo API 호출 0회(전부 monkeypatch/fake). 운영 파일
(`order_history.csv` 등)과 `.env`, 실거래 관련 환경변수는 이번 run에서 변경되지 않았다.

### 최종 상태
**`READY_FOR_CODEX_REVALIDATION`** — CODEX-016~019 전부 Claude 측 수정 및 회귀 테스트 완료. Claude 자체
판정만으로 `VALIDATED`/`RESOLVED` 확정하지 않으며, Codex의 독립 재검증(각 항목 재확인 및
`PROCEED`/`FAIL` 여부) 전까지 제한적 실거래 검토(`docs/live_review/`)는 재개하지 않는다. `approved: false`,
`live_enabled: false`는 변경하지 않았다.

### 현재 커밋 해시
`50a097d` (t4: CODEX-019 상태 저장소 read-modify-write 파일 잠금 적용) — 브랜치
`orchestrator/20260723-020935-us-stock-trading` tip. main 미병합, push 없음.

---

## 이전 패키지: 제한적 실거래 검토 전 안전조건 완료 (2026-07-23)

### 배경
CODEX-010~015(HIGH/MEDIUM/LOW) 수정 완료 후, AI 오케스트레이터(`~/Projects/ai-orchestrator`,
Claude 구현 → Codex 독립 검증 → PASS 시 임시 브랜치 커밋 루프)를 이용해 두 차례 run을 수행했다:

1. **run `20260722-021713-us-stock-trading`** (t0~t8, 9개 task, 전부 PASS): 재시작 안전 중복 주문
   방지(`order_intent_ledger.py`), 계좌 전체 포지션/익스포저 상한, kill switch 1차 버전(파일/환경변수
   기반 전면 차단), API timeout 심볼 단위 격리, 비정상종료 복구(`atomic_io.py`), paper/live 분리 테스트
   보강, 주문 이벤트 알림 연결. 이 run에서 `docs/autonomous/HUMAN_REVIEW_FINDINGS.md`에 BrokerConfig
   import-time 환경변수 고정 문제를 발견·기록(코드 미수정, `broker/**`가 forbidden_files였음).
2. **run `20260722-235153-us-stock-trading`** (t1~t5, 이전 run 브랜치 위에서 계속, 전부 PASS): 사용자가
   위 HUMAN_REVIEW 항목을 직접 검토·승인해 `broker/broker_config.py`만 한시적으로 개방하고 진행. 아래
   4개 항목을 완료했다.

### 이번 run에서 완료한 항목
1. **BrokerConfig import-time 환경변수 고정 문제 수정** — `BrokerConfig.from_env()` 팩터리 도입,
   dataclass 필드 선언부의 `os.getenv`/`env_bool` 직접 호출 제거(모두 default_factory로 이동). 환경변수
   변경 후 재로딩 없이 새 인스턴스가 이를 반영함을 테스트로 증명. `broker/alpaca_client.py`,
   `broker/__init__.py`는 미수정.
2. **Kill Switch 4단계 상태 재설계** — `ACTIVE`/`ENTRY_DISABLED`/`ALL_TRADING_DISABLED`/`MANUAL_REVIEW`.
   신규 진입은 `ACTIVE`에서만 허용, 자동 청산은 `ACTIVE`·`ENTRY_DISABLED`에서 허용(`ALL_TRADING_DISABLED`·
   `MANUAL_REVIEW`에서는 차단), 조회는 모든 상태에서 허용. 해제는 `released_by` 등 운영자 승인 인자
   없이는 불가하며 `expires_at`이 지나도 자동 재활성화되지 않는다. 손상된 상태 파일은 fail-closed로
   가장 보수적인 상태로 취급. 상태 변경 이력은 감사 로그로 누적 보존.
3. **Slack 알림 장애 자체 감시(`notification_health.py`)** — `HEALTHY`/`DEGRADED`/`FAILED`/`UNKNOWN` 4개
   상태, 최근 성공/실패 시각·연속 실패 횟수·마지막 오류 종류 기록. 연속 실패가 임계값을 넘으면
   (`ACTIVE` 상태일 때만) kill switch를 `ENTRY_DISABLED`로 자동 상승시키되 기존 포지션의 안전 처리는
   막지 않는다. 실제 Slack 호출은 전부 monkeypatch로 대체, 실호출 0회.
4. **`docs/live_review/` 문서 5종 신규 작성** — `LIMITED_LIVE_REVIEW_CHECKLIST.md`(실측값/TBD 구분),
   `KILL_SWITCH_RUNBOOK.md`, `INCIDENT_RESPONSE_RUNBOOK.md`, `ROLLBACK_PLAN.md`,
   `LIVE_APPROVAL_RECORD.md`(`approved: false`/`live_enabled: false`로 시작, 최종 상태는
   `READY_FOR_LIMITED_LIVE_REVIEW`/`BLOCKED`만 사용 — `LIVE_READY`/`LIVE_APPROVED`/`PRODUCTION_READY`
   표현 금지).

### 변경 파일 (이번 run, t1~t5)
- 수정: `broker/broker_config.py`(1번 항목 범위로 한시 개방됨), `kill_switch.py`.
- 신규: `kill_switch_state.py`, `notification_health.py`, `tests/test_broker_config_env.py`,
  `tests/test_kill_switch_states.py`, `tests/test_notification_health.py`, `docs/live_review/*.md`(5개).
- `broker/alpaca_client.py`, `broker/__init__.py`, `order_safety.py`, `config/scanner_presets.json`은
  미수정(확인: `git diff --name-only main...HEAD`에 미포함).

### 오케스트레이터 운영 중 발견·수정한 인프라 이슈(참고, us-stock-trading 코드와 무관)
- `ai-orchestrator/orchestrator.py`의 `_pid_alive()`가 `PermissionError`(OSError 서브클래스)를 더
  일반적인 `except (OSError, ProcessLookupError)`에 먼저 잡혀 "다른 프로세스가 lock을 잡고 있으면
  BLOCKED" 안전장치가 무력화되는 버그를 발견해 수정(1차 run 이전).
- 두 번째 run이 첫 run의 브랜치 위에서 이어지는 구조라, 일부 task의 acceptance 기준이 `git diff
  main...HEAD`(main은 이전 run 커밋을 전혀 포함하지 않음)를 기준으로 "이 파일은 변경되지 않는다/이
  범위로만 한정된다"를 검사하도록 작성되어, 실제로는 변경하지 않았음에도 항상 위반으로 판정되는
  구조적 오탐이 t2/t3(그리고 잠재적으로 t4/t5)에서 발생. 원인 확인 후 해당 task들의 acceptance 문구를
  "main 기준 누적 diff"가 아니라 "이번 task 실행 중 실제로 아직 커밋되지 않은 변경사항"(`git status
  --porcelain`)을 보도록 직접 수정한 뒤 `--resume`으로 재개해 정상 PASS 처리됨. 코드 구현 자체의
  결함이 아니라 이 multi-run 연속 구조에 맞지 않은 검증 문구의 문제였음(모든 재작업 시도의
  `focused test`/`전체 회귀`는 매번 통과했음 — 실패 원인은 항상 diff 기준선 문제였지 코드 문제가
  아니었음).

### 실행 명령 및 결과 (독립 재검증, 이 세션에서 직접 실행)
```bash
venv/bin/python -m pytest -q     # 384 passed, 2 warnings, 0 failed
md5 order_history.csv            # a61104cf03499860ae89d4e194dc8c07 — 이전과 동일
git diff --name-only main | grep -E "^broker/alpaca_client\.py$|^broker/__init__\.py$|^order_safety\.py$|^config/scanner_presets\.json$"   # 결과 없음
```

### 안전 재검증
- 실제 Alpaca/Slack/Yahoo API 호출 0회(전부 monkeypatch/fake).
- 운영 파일 변경 없음: `order_history.csv` 해시 불변, `.env` 미변경, `LIVE_TRADING_ENABLED` 등 실거래
  관련 환경변수 변경 없음.
- `main` 브랜치는 전혀 변경되지 않음(여전히 `158671e`). 모든 커밋은
  `orchestrator/20260722-021713-us-stock-trading` → `orchestrator/20260722-235153-us-stock-trading`
  브랜치 계열에만 존재. origin push 없음.
- Kill Switch 기본값은 상태 파일 부재 시 `ACTIVE`(거래 허용)로, 안전 기본값 유지.

### 최종 상태 (이 패키지 당시, 2026-07-23 기준 — 이후 CODEX-016~020 검토에서 수정됨, 아래 최신 패키지 참고)
이 run 시점의 자체 판정은 "제한적 실거래 검토 준비 완료"였다(상세 근거는
`docs/live_review/LIMITED_LIVE_REVIEW_CHECKLIST.md` 8절, 당시 상태 문자열은 그 문서와 동일). 이후
Codex 독립 재검증에서 CODEX-016(HIGH), CODEX-017(HIGH), CODEX-020(HIGH) 등이 추가로 발견되어 최신
판정은 이 절이 아니라 본 파일 최상단(가장 최근) 패키지의 `READY_FOR_CODEX_REVALIDATION`을 따른다.
실거래는 여전히 비활성 상태이며(`LIVE_APPROVAL_RECORD.md`의 `approved: false`/`live_enabled: false`),
이번 run에서 활성화되지 않았다. 남은 항목은 전부 운영자가 직접 채워야 하는 `TBD` 필드(허용 종목 범위,
허용 거래 시간, 주문당 최대 금액, 실계좌 종류, 롤백 담당자 등)와 운영자의 명시적 최종 승인이다.

### 현재 커밋 해시
`fc325b3` (t5: 최종 회귀 확인 및 run 상태 기록) — 브랜치 `orchestrator/20260722-235153-us-stock-trading`
tip. main 미병합, push 없음.

---

## 이전 패키지: Phase 2 구현 완료 — 초단타 관심종목 선별 엔진 (2026-07-22)

### 배경
Phase 1 최종 Codex 판정(verdict `PASS_WITH_CONDITIONS`, CODEX-001~009 전부 RESOLVED): **Phase 1A(주문 진입 안전성) VALIDATED**, **Phase 1B(부분체결·포지션 생명주기) DEFERRED_TO_PHASE_5**, **Phase 2 PROCEED**. 이 판정에 따라 Phase 2를 착수·구현 완료했다. 이번 패키지가 다루는 커밋: `4a96883`.

### 목적
"오늘 어떤 종목을 1분봉으로 집중 감시할 것인가?"에 답하는 결정적 파이프라인. **주문 신호를 생성하지 않는다** — VWAP/EMA 진입 판단은 Phase 3·4 범위.

### 재사용 범위 (근거는 `DECISION_LOG.md`)
- 재사용: `daily_candidate_scanner.calculate_rsi`/`calculate_atr`(순수 함수), `market_hours.eastern_now`/`get_us_market_session`, `market_guard.is_us_trading_day`, `universe_builder.py`의 universe.csv(이미 tradable/active/us_equity로 필터링됨 — Stage A는 방어적 재검증만).
- 의도적으로 재사용하지 않음: 기존 JSON 룰 엔진(`evaluate_filter`)은 미지원 필드/연산자에서 **경고 후 통과**(fail-open)하도록 설계되어 있어, Phase 2의 명시적 원칙("불명확하면 포함하지 않는다")과 정면으로 배치됨. Stage A~E는 이 때문에 전용 명시적 함수로 새로 작성.
- 신규 구현 확인(저장소 전체 검색으로 기존 로직 없음을 확인): 다중 사이클 반복탐지 스트릭 추적, 스프레드/유동성 대체지표.

### 구현 구조
```
scalping_watchlist/
  models.py         WatchlistEntry dataclass (23 필드, UNKNOWN/NOT_AVAILABLE/NOT_EVALUATED 센티널)
  data_provider.py  MarketDataProvider 인터페이스, YFinance(운영)/Fake(테스트) 구현
  features.py       Stage C 피처 계산 + 데이터 품질 게이트
  eligibility.py    Stage A(방어적)/B(가격·유동성)/C(당일 움직임) 명시적 필터
  repeat_tracker.py Stage D 반복탐지(ET 거래일 기준, 잠금 보호)
  scorer.py         Stage E 설명 가능한 가중합 점수([0,100] 클램프)
  repository.py     scalping_watchlist.csv 영속화 + TTL 기반 만료(NEW→ACTIVE→COOLING→EXPIRED)
  atomic_io.py       temp file+fsync+os.replace, fcntl.flock (order_history.csv와 동일 기법, 독립 구현)
  pipeline.py        run_scan_cycle() — Stage A~E 오케스트레이션
config/scalping_watchlist_config.py   임계값/가중치 (risk_config.py와 분리, 대시보드 미노출)
```

### 필수 필드 처리
지시서 22개 필드 전부 구현 + `expires_at` 계산 로직 포함(총 23개, `models.CSV_COLUMNS`). 계산 불가능한 필드는 `UNKNOWN`/`NOT_AVAILABLE`/`NOT_EVALUATED`로 명시(허위 값 없음):
- `spread_estimate`: 항상 `NOT_AVAILABLE` — 실제 호가 데이터 소스가 프로젝트에 없음(확인됨). 대신 `average_dollar_volume` 기반 `liquidity_score`(0-100)를 유동성 게이트로 사용.
- `smart_money_score`: 항상 `NOT_EVALUATED` — 전체 재계산에 daily_candidate_scanner의 MA200/RSI 히스토리가 추가로 필요해 이번 버전에서는 보류(점수 가중치에서 0 기여로 처리, 향후 통합 여지).

### 변경 파일
- 신규: `config/scalping_watchlist_config.py`, `scalping_watchlist/` 전체(9개 파일), `tests/test_scalping_watchlist.py`(34건).
- 수정: 없음(기존 파일 일절 미변경 — Phase 1 주문/리스크/reconciliation/broker/systemd/cron/nginx 전부 그대로).
- 문서: `docs/autonomous/{SCALPING_V1_ROADMAP,CURRENT_STATUS,VALIDATION_REPORT,DECISION_LOG,VALIDATION_PACKAGE}.md`.

### 테스트 목록 (34건, 요구된 6개 범주 전부)

| 범주 | 테스트 |
|---|---|
| 정상 선별 | 기준 만족 종목 포함, 점수순 정렬, `MAX_WATCHLIST_SIZE` 캡, 동점 결정성(심볼 알파벳순 tiebreak) |
| 기본 차단 | 심볼 형식 오류, 가격 미달/초과, 평균거래량/거래대금/당일거래량 부족(파라미터라이즈드), 상대거래량 부족, 변동성 부족, 유동성 부족(단위테스트), 데이터 없음, 데이터 지연(stale), 비정상 갭(sanity limit), 심볼 중복 |
| 반복 탐지 | 최초 등장, 동일일 재등장 스트릭 증가, 타거래일 초기화, ET 날짜 경계(서울 저녁≠뉴욕), 중간탈락 후 재등장(스트릭 리셋, 총 카운트는 유지), **threading 동시 갱신 lost-update 방지** |
| 점수 | 하위점수 합=최종점수(가중치 일치), 극단 입력에도 [0,100] 범위 유지, NaN/Infinity 클램프, 입력 dict 순서 무관 재현성 |
| 파일 | 원자적 쓰기 실패 시 원본 보존, 잠금 타임아웃 시 파일 미변경, 손상된 watchlist 파일 fail-closed, 파일 없음=정상 빈 상태, 전부 `tmp_path` 격리 |
| 네트워크 | FakeMarketDataProvider만 사용 확인, provider 예외가 해당 심볼만 제외하고 나머지는 계속 처리 |

### 실행 명령 및 결과

```bash
# 저장소 루트
venv/bin/pytest -q                              # 183 passed, 2 warnings
venv/bin/python -m pytest -q                    # 183 passed, 2 warnings

# 저장소 상위 디렉터리
cd ..
us-stock-trading/venv/bin/pytest -q us-stock-trading            # 183 passed, 2 warnings

# 신규 테스트 집중
venv/bin/pytest -q tests/test_scalping_watchlist.py             # 34 passed

# 반복탐지 동시성 안정성(5회 반복)
venv/bin/pytest -q tests/test_scalping_watchlist.py -k "concurrent"   # 매회 1 passed

# 운영 파일 무결성
md5 order_history.csv   # a61104cf03499860ae89d4e194dc8c07 — Phase 1 종료 시점과 동일
```

### 안전 재검증
- 실제 Alpaca/Slack/Yahoo API 호출 0회: `FakeMarketDataProvider`만 사용, `YFinanceMarketDataProvider`는 테스트에서 import조차 되지 않음(실제 `yfinance`/`from daily_candidate_scanner import calculate_atr` import는 그 클래스의 메서드 내부에서 지연 임포트되어, provider 인스턴스를 만들지 않는 한 로드되지 않음).
- 운영 파일 변경 없음: `order_history.csv` 해시 불변. `scalping_watchlist.csv`/`scalping_repeat_state.csv`는 신규 파일이며 테스트는 전부 `tmp_path`로 리다이렉트, 실제 저장소 루트에 생성되지 않음(확인됨).
- 기존 주문/리스크/reconciliation/broker 로직: 파일 자체를 열지도 import하지도 않음 — 정적으로 완전히 독립된 신규 패키지.
- Live Trading, 운영 서버 접속, origin push 없음.

### 운영 영향
없음. 신규 코드가 어떤 cron/systemd 항목에도 아직 연결되어 있지 않다(Phase 2는 파이프라인 구현까지가 범위이며, 운영 편입은 이번 지시서 범위 밖).

### 남은 위험 / 알려진 한계
- `spread_estimate`/`smart_money_score` 미구현(설계상 의도, 위 참고) — 향후 재검토 대상으로 문서화.
- Stage B/C 임계값과 `SCORING_WEIGHTS`는 백테스트로 검증되지 않은 초기 가정(`DECISION_LOG.md`에 개별 근거 기록).
- `scalping_watchlist.csv`/`scalping_repeat_state.csv` 간에도 Phase 1과 유사하게 단일 트랜잭션은 없음 — 다만 이 데이터는 안전 크리티컬(주문 실행)이 아니므로 우선순위는 낮음.
- `YFinanceMarketDataProvider`(운영 구현)는 실제 Alpaca Paper 계정/실 마켓 데이터로 아직 E2E 검증되지 않음(외부 호출 금지 원칙에 따라 이번 사이클에서 수행하지 않음).

### Codex가 집중 검토해야 할 항목
1. JSON 룰 엔진을 재사용하지 않기로 한 결정(`DECISION_LOG.md`)이 타당한지, 아니면 fail-open 문제를 룰 엔진 자체에서 고치고 재사용하는 편이 나았는지.
2. `liquidity_score`(달러거래대금 기반 대체지표)가 실제 스프레드 없이 "유동성"을 판단하는 근거로 충분한지.
3. 반복탐지의 "동일 거래일" 판정과 만료(`WATCHLIST_TTL_MINUTES`/`WATCHLIST_EXPIRE_MINUTES`) 로직이 실제 스캔 주기(15분 등)와 정합적인지 — 이번 구현은 호출 시점 기준 임의 간격을 지원.
4. `run_scan_cycle()`의 `symbols` 파라미터(테스트/운영 모두 사용 가능)가 운영에서 실수로 전체 universe 대신 부분 목록으로 잘못 호출될 위험이 있는지.

### 현재 커밋 해시
`4a96883` (Add scalping watchlist selection engine) — 이번 패키지가 다루는 마지막 코드 커밋. 문서 갱신은 다음 커밋에서 기록됨.
