# CODEX_REVIEW

Review target: CODEX-018 잔여분 및 CODEX-020 수정 독립 재검증

Commits: `66eda8a`, `ed452da`, `cf5601d`, `edc5ad5`

Validation package SHA-256: `173338b830c0b99b329ac7f430480c411f517d7e16e63abb11d7eb77a2154736`

Date: 2026-07-24

Overall verdict: **FAIL**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

public broker 주문의 binary/4-state Kill Switch 적용과 현재 credential 재검증은 해결됐다. 그러나 검증 패키지가 해결했다고 주장한 method+path 백스톱은 구현되지 않았다. `_request("POST", "/v2/orders", order_side=None, ...)`처럼 `None`을 명시하면 Kill Switch 검사를 건너뛰고 ENTRY_DISABLED와 binary halt 상태에서 각각 HTTP가 1회 호출된다. 신규 HIGH Finding이므로 limited live review로 진행할 수 없다.

## Previous findings

### [CODEX-018]

Status: **RESOLVED**

Evidence:

- `_validate_runtime_safety()`가 매 요청 직전 `BrokerConfig.from_env()`로 현재 credentials를 다시 읽는다.
- 현재 API key/secret의 누락, 공백, 교체 및 환경 조회 예외를 fail-closed 처리한다.
- captured credentials와 현재 값은 `hmac.compare_digest()`로 비교하며 예외 메시지에 secret 원문을 포함하지 않는다.
- GET, POST, DELETE 모두 공통 `_request()`를 사용한다.

Direct reproduction:

- broker 생성 후 API key 삭제 상태에서 GET·POST·DELETE 총 session 호출 0회.
- unsafe mode/endpoint 변경 차단 테스트와 기존 reconciliation/cancel 경로 테스트가 통과했다.

Remaining risk:

- credential rotation은 기존 객체를 자동 갱신하지 않고 새 broker 생성을 요구한다. 문서화된 의도와 일치한다.

### [CODEX-020]

Status: **PARTIALLY_RESOLVED**

Evidence:

- public `AlpacaBroker.submit_order()`는 binary halt와 4-state Kill Switch를 network boundary에서 재검사한다.
- ENTRY_DISABLED는 buy를 차단하고 sell liquidation을 허용한다.
- ALL_TRADING_DISABLED/MANUAL_REVIEW 및 손상 state는 buy/sell 모두 차단한다.
- account, positions, reconciliation 및 cancel은 명시된 read/cancel 정책에 따라 계속 허용된다.
- 그러나 `_request()`의 `order_side`는 필수 인자일 뿐 POST path와 의미적으로 결합되지 않는다. 명시적 `None`은 `_check_kill_switch()`에서 즉시 반환한다.

Direct reproduction:

- ENTRY_DISABLED에서 public buy → HTTP 0회, public sell → HTTP 1회(정책과 일치).
- binary halt에서 public buy → HTTP 0회.
- ENTRY_DISABLED에서 `_request("POST", "/v2/orders", order_side=None, ...)` → HTTP 1회.
- binary halt에서 같은 explicit-None direct POST → HTTP 1회.

Remaining risk:

- private 공통 network boundary를 직접 호출하거나 향후 order method가 실수로 `order_side=None`을 넘기면 모든 주문 정지 정책을 우회한다.

## Regression findings

### [CODEX-016]

Status: **RESOLVED**

Evidence: strict buy/sell 전달과 payload 보존 테스트가 통과했고 이번 변경에 회귀가 없다.

### [CODEX-017]

Status: **RESOLVED**

Evidence: notification health 기록·escalation·주문 차단 경로가 집중 회귀에서 통과했다.

### [CODEX-019]

Status: **RESOLVED**

Evidence: 상태 저장소 multiprocessing lost-update 및 lock timeout 테스트가 통과했다.

## New findings

### [CODEX-021] HIGH — order-shaped `_request()`가 explicit `order_side=None`으로 Kill Switch를 우회함

Status: **UNRESOLVED**

Evidence:

- `_request()`는 `order_side` 누락만 Python TypeError로 막는다.
- `_check_kill_switch(None)`은 HTTP method/path/body를 확인하지 않고 반환한다.
- 구현 및 테스트에 보고서가 주장한 method+path 주문 감지 백스톱이 없다.
- ENTRY_DISABLED와 binary halt에서 직접 POST session 호출을 각각 재현했다.

Required behavior:

- POST `/v2/orders` 등 주문 생성 endpoint에서는 `order_side=None`을 HTTP 이전에 거부해야 한다.
- request payload의 `side`와 `order_side`가 정확히 일치하는지 검증해야 한다.
- method/path 기반 분류는 query/trailing slash 등 정상 변형에도 결정적이어야 한다.
- 내부 public method뿐 아니라 공통 network boundary 직접 호출 테스트에서 session 호출 0회를 보장해야 한다.

## Executed tests

- 신규 안전 집중 4개 파일 → **81 passed, 1 warning**
- 저장소 루트 `venv/bin/pytest -q` → **489 passed, 2 warnings**
- 저장소 루트 `venv/bin/python -m pytest -q` → **489 passed, 2 warnings**
- 저장소 상위 `venv/bin/pytest us-stock-trading -q` → **489 passed, 2 warnings**
- 저장소 상위 `venv/bin/python -m pytest us-stock-trading -q` → **489 passed, 2 warnings**
- public/private Kill Switch 및 credential 격리 재현

Warnings:

- urllib3 `NotOpenSSLWarning`: macOS LibreSSL 환경 경고다.
- scanner unknown-field `RuntimeWarning`: 의도된 기존 테스트 경고다.
- 신규 안전 관련 warning은 없다.

## Kill-switch policy verification

- ACTIVE: public buy/sell 허용.
- ENTRY_DISABLED: public buy 차단, public sell 허용.
- ALL_TRADING_DISABLED/MANUAL_REVIEW: public buy/sell 차단.
- binary halt: public buy/sell 차단.
- read-only 조회와 cancel은 Kill Switch와 무관하게 허용하는 문서 정책과 일치한다.
- explicit-None private POST만 정책을 우회한다.

## Credential verification

- 누락·공백·교체·환경 조회 실패는 모든 GET/POST/DELETE 전에 차단된다.
- stale captured credential은 현재 환경과 다르면 사용되지 않는다.
- secret 값은 오류 메시지에 노출되지 않는다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 호출은 수행하지 않았다.
- 모든 HTTP 검증은 recording fake session을 사용했다.
- 공식 테스트에서 실제 외부 socket 연결 증거는 없었다.

## Operational file safety

- `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966` 불변.
- `universe.csv`: SHA-256 `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`, 833518 bytes, mtime `1784558966` 불변.
- `order_reconciliation.csv`, `scalping_watchlist.csv`, Kill Switch/notification runtime 파일은 검증 전후 모두 존재하지 않았다.
- `LIVE_APPROVAL_RECORD.md`: SHA-256 `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`, `approved: false`, `live_enabled: false`, 상태 `BLOCKED`로 불변.
- 전체 테스트가 `strategy_performance.csv` mtime만 갱신했으나 내용·크기는 불변이었고 이번 검증 기준 mtime으로 복원했다.

## Document consistency

- 새 validation package SHA-256은 보고값과 일치하고 이전 `27a36e62...` 패키지와 다르다.
- 489 passed 및 2 warnings 주장은 실제 결과와 일치한다.
- CODEX-018 credential 재검증 완료 주장은 실제 코드와 일치한다.
- “method+path가 주문 관련이면 order_side 생략 시 차단” 주장은 실제 코드·테스트와 불일치한다.
- `READY_FOR_CODEX_REVALIDATION`, limited review `BLOCKED`, `approved: false`, `live_enabled: false`는 정확하다.

## Unverified areas

- 실제 Alpaca/Slack/Yahoo E2E
- 실제 포지션 청산 및 broker reconciliation
- 승인 레코드의 machine-readable runtime enforcement
- Ubuntu 운영 환경의 flock 및 실제 스케줄러

## Required next action

1. CODEX-021을 해결해 order-shaped POST에서 `order_side=None`을 차단한다.
2. payload `side`와 gate `order_side` 불일치도 HTTP 이전에 차단한다.
3. 독립 재검증 전 limited live review는 `BLOCKED`, 실거래는 `DO_NOT_ENABLE`을 유지한다.
