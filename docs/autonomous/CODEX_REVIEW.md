# CODEX_REVIEW

Review target: CODEX-016·018 최종 독립 재검증

Commits: `47ee8d6`, `03962d3`, `cf4ada9`

Validation package SHA-256: `27a36e62daad8aea3f32e82eb614e7e69fe98dcf917ad97669162f62d7aa8330`

Date: 2026-07-23

Overall verdict: **FAIL**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

side의 strict 전달과 GET·POST·DELETE 공통 `_request()` 통합은 실제 코드와 payload 재현으로 확인됐다. 그러나 CODEX-018의 필수 runtime 안전 항목 중 현재 Kill Switch와 현재 credentials가 공통 broker gate에 포함되지 않는다. direct `AlpacaBroker.submit_order()`는 binary halt 및 `ENTRY_DISABLED` 상태에서도 HTTP를 각각 1회 호출했다. 운영 안전성과 직접 관련된 미해결 항목이므로 limited live review로 진행할 수 없다.

## Previous findings

### [CODEX-016]

Status: **RESOLVED**

Evidence:

- `paper_strategy_order.submit_order(..., *, side)`와 `AlpacaBroker.submit_order(..., *, side)` 모두 side가 keyword-only 필수값이며 암묵적인 buy 기본값이 없다.
- wrapper는 `broker.submit_order(..., side=side)`를 명시적으로 호출한다.
- broker는 정확한 문자열 `buy`, `sell`만 허용한다.
- 최종 POST JSON의 `side`가 wrapper 입력과 동일함을 fake session으로 확인했다.
- main의 현재 신규 진입 호출은 `side="buy"`를 명시한다.

Direct reproduction:

- `side="buy"` → captured payload `side == "buy"`.
- `side="sell"` → captured payload `side == "sell"`.
- None, 빈 문자열, `BUY`, `SELL`, 선행·후행 공백, `short`, `close`, 오타, bool, 숫자는 모두 HTTP delta 0이었다.
- side 누락은 Python signature 단계에서 TypeError로 차단되고 HTTP 호출은 없다.

Remaining risk:

- 현재 저장소에는 실제 포지션 청산 caller가 구현돼 있지 않아 “기존 포지션 청산 E2E”는 실행 검증할 경로가 없다.
- reconciliation은 기존 주문 상태를 조회할 뿐 자동 재주문하지 않으므로 side를 재구성하지 않는다. 향후 sell retry/청산 기능을 추가할 때 side를 ledger/reconciliation identity에 포함할지 별도 설계가 필요하다.

### [CODEX-018]

Status: **PARTIALLY_RESOLVED**

Evidence:

- account, positions, recent orders, assets, client-order-id 조회, order POST, cancel DELETE가 모두 단일 `_request()`를 사용한다.
- `_request()`는 captured `self.config`와 현재 환경의 mode/endpoint를 HTTP 직전에 검사한다.
- safe Paper 생성 후 unsafe Live 환경 변경 시 GET·POST·DELETE는 모두 HTTP delta 0이었다.
- config 교체, invalid mode 및 endpoint 변조를 차단하는 테스트가 존재한다.
- 그러나 `_validate_runtime_safety()`는 binary `is_trading_halted()`, 다단계 `is_entry_allowed()/is_liquidation_allowed()`, 승인 레코드를 검사하지 않는다.
- 현재 credentials도 다시 검증하지 않는다. `self.config.validate_for_request()`는 생성 시점에 캡처된 key만 검사하고 `validate_order_allowed_now()`는 credentials를 검사하지 않는다.

Direct reproduction:

- `KILL_SWITCH_STATE=ENTRY_DISABLED`에서 direct `AlpacaBroker.submit_order(side="buy")` → session request 1회.
- binary `KILL_SWITCH` 파일 활성 상태에서 direct broker submit → session request 1회.
- broker 생성 후 현재 환경에서 `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` 삭제 → stale captured credentials로 session request 1회.
- unsafe Live 전환 후 POST, reconciliation GET, DELETE는 각각 session 호출 0회로 정상 차단됐다.

Remaining risk:

- wrapper를 우회하는 direct broker 주문 경로가 운영 정지 상태를 무시한다. 안전 제어는 최하위 네트워크 경계에서도 강제돼야 한다.
- current credential 제거·회전도 장수명 broker 객체에 즉시 반영되지 않는다.
- 승인 파일은 runtime broker gate에 연결되지 않았지만 현재 live mode 자체가 항상 차단되므로 이번 커밋만으로 live가 활성화되지는 않는다.

## Regression findings

### [CODEX-017]

Status: **RESOLVED**

Evidence:

- 운영 Slack wrapper가 notification health tracker를 실제로 경유한다.
- 성공·실패 기록, 연속 실패 escalation, ENTRY_DISABLED 이후 wrapper buy 차단 테스트가 통과했다.
- 관련 집중 회귀 테스트에 회귀가 없었다.

### [CODEX-019]

Status: **RESOLVED**

Evidence:

- kill-switch와 notification state read-modify-write는 `fcntl.flock` 보호를 유지한다.
- multiprocessing lost-update, lock timeout 원본 보존, 손상 파일 안전 테스트가 통과했다.

## New findings

### [CODEX-020] HIGH — direct broker network boundary가 Kill Switch를 우회함

Status: **UNRESOLVED**

Evidence:

- `broker/alpaca_client.py`는 kill-switch 모듈을 import하거나 검사하지 않는다.
- `paper_strategy_order.submit_order()`에는 binary/4-state gate가 있지만 `AlpacaBroker.submit_order()`를 직접 호출하면 이 계층을 우회한다.
- binary halt와 `ENTRY_DISABLED` 각각에서 fake session request 1회를 직접 재현했다.

Required behavior:

- 모든 주문 POST 직전 broker network boundary에서 binary halt와 side별 4-state 정책을 재검사해야 한다.
- buy는 `is_entry_allowed()`, sell은 `is_liquidation_allowed()`를 사용하고 손상 상태는 HTTP 호출 전에 fail-closed 처리해야 한다.
- account/reconciliation 같은 read-only 조회를 허용할지 정책을 명시적으로 분리해야 한다.
- direct broker 호출 및 wrapper 호출 모두에서 HTTP call count 0 회귀 테스트가 필요하다.

## Executed tests

- 집중 안전 테스트 → **188 passed, 1 warning**
- CODEX-017/019 회귀 집중 → **50 passed, 1 warning**
- 저장소 루트 `venv/bin/pytest -q` → **443 passed, 2 warnings**
- 저장소 루트 `venv/bin/python -m pytest -q` → **443 passed, 2 warnings**
- 저장소 상위 `venv/bin/pytest us-stock-trading -q` → **443 passed, 2 warnings**
- 저장소 상위 `venv/bin/python -m pytest us-stock-trading -q` → **443 passed, 2 warnings**
- side 및 runtime gate 수동 격리 재현

Warnings:

- urllib3 `NotOpenSSLWarning`: macOS LibreSSL 환경 경고이며 이번 Finding 원인이 아니다.
- scanner unknown-field `RuntimeWarning`: 의도된 기존 테스트 경고다.
- 신규 안전 관련 warning은 없다.

## Order-side verification

- wrapper → broker kwargs → POST payload에서 buy/sell이 정확히 유지된다.
- strict side validation은 HTTP 이전에 수행된다.
- 테스트 fake broker도 side를 필수로 받아 기본값으로 오류를 숨기지 않는다.
- 실제 청산 workflow는 아직 구현돼 있지 않아 unverified area로 남겼다.

## Runtime HTTP safety verification

- Alpaca 관련 session 호출은 `broker/alpaca_client.py::_request()` 한 곳에만 존재한다.
- GET·POST·DELETE가 공통 mode/endpoint runtime gate를 사용한다.
- unsafe Live 및 config/endpoint 변조는 session 호출 전에 차단된다.
- Kill Switch와 현재 credential 상태는 공통 gate에 포함되지 않아 FAIL 판정 원인이 됐다.

## Approval and kill-switch verification

- `LIVE_APPROVAL_RECORD.md`는 `approved: false`, `live_enabled: false`, 상태 `BLOCKED`로 불변이다.
- live mode는 현재 `BrokerConfig.validate_order_allowed()`에서 항상 차단된다.
- 검증 중 Kill Switch 파일을 저장소에 생성하거나 해제하지 않았다.
- 임시 경로의 손상·활성 state는 wrapper에는 fail-closed지만 direct broker에는 적용되지 않는다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 호출은 수행하지 않았다.
- 모든 HTTP 검증은 recording fake session을 사용했다.
- 공식 테스트에서 실제 외부 socket 연결 증거는 없었다.

## Operational file safety

- `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966` 불변.
- `universe.csv`: SHA-256 `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`, 833518 bytes, mtime `1784558966` 불변.
- `order_reconciliation.csv`, `scalping_watchlist.csv`, Kill Switch/notification runtime 파일은 검증 전후 모두 존재하지 않았다.
- `LIVE_APPROVAL_RECORD.md`: SHA-256 `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`, 2704 bytes, mtime `1784815607` 불변.
- 기존 전체 테스트가 `strategy_performance.csv` mtime만 갱신했으나 내용·크기는 불변이었고 검증 전 mtime으로 복원했다.

## Document consistency

- 검증 패키지 SHA-256은 보고값과 정확히 일치하며 이전 패키지와 다른 새 패키지다.
- 188 focused 및 443 전체 테스트 수는 실제 결과와 일치한다.
- `READY_FOR_CODEX_REVALIDATION`, limited review `BLOCKED`, `approved: false`, `live_enabled: false`는 정확하다.
- 패키지의 “모든 HTTP runtime gate” 주장은 mode/endpoint 범위에는 맞지만 Kill Switch와 current credentials까지 포함한다는 검증 요청 기준에는 불완전하다.

## Unverified areas

- 실제 Alpaca Paper/Live, Slack, Yahoo E2E
- 실제 포지션 청산 및 sell retry/reconciliation side identity
- 승인 레코드의 runtime machine-readable enforcement
- API credential rotation/removal의 장수명 프로세스 동작
- Ubuntu 운영 환경의 flock 및 실제 스케줄러

## Required next action

1. CODEX-020을 해결해 direct broker POST가 binary/4-state Kill Switch를 반드시 준수하도록 한다.
2. CODEX-018의 current credential 재검증 정책을 구현하고 누락·빈 값·회전 테스트를 추가한다.
3. read-only broker 조회와 주문/취소의 Kill Switch 정책을 명시적으로 분리한다.
4. 독립 재검증 전 limited live review는 `BLOCKED`, 실거래는 `DO_NOT_ENABLE`을 유지한다.
