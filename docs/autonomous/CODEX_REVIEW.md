# CODEX_REVIEW

Review target: CODEX-021 및 CODEX-020 잔여분 최종 수정 독립 재검증

Commits: `47ae3ca`, `c133e01`, `cc740a5`

Validation package SHA-256: `4eb064d74ac0471788d63a0f9990e6bb81250c052c6890928fee5cda90c5f63d`

Date: 2026-07-25

Overall verdict: **FAIL**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

`RequestPurpose` 도입으로 `purpose` 누락·`None`·잘못된 method 조합은 HTTP 전에 차단되고 public `submit_order()`도 안전하다. 그러나 공통 HTTP 경계 `_request()`는 주문 payload의 `side`, `order_side`, `purpose`가 서로 일치하는지 검증하지 않는다. 따라서 `ENTRY_DISABLED` 상태에서 `purpose=EXIT_ORDER`와 매수 payload를 전달하면 HTTP가 호출된다. 이전 HIGH 우회가 다른 인자 조합으로 여전히 가능하므로 최종 승인 조건을 충족하지 못한다.

## Previous findings verification

### [CODEX-021]

Status: **PARTIALLY_RESOLVED**

Evidence:

- `_request()`의 `purpose`는 기본값 없는 keyword-only 필수 인자다.
- `purpose=None` 및 enum이 아닌 값은 runtime `isinstance` 검사에서 `ValueError`가 발생하며 session 호출은 0회다.
- 옛 `order_side`만 전달하고 `purpose`를 생략하면 `TypeError`, 잘못된 method-purpose 조합은 `ValueError`이며 모두 session 호출 전이다.
- public `submit_order(side="buy")`는 `ENTRY_ORDER`, sell은 `EXIT_ORDER`를 선택하고 local payload도 재검증한다.
- 하지만 `_request()`는 POST 주문 payload의 `side`와 `purpose`를 결합하지 않는다. `order_side`도 선택 인자이며 payload와 일치 검사가 없다.

Direct reproduction under `ENTRY_DISABLED`:

- `purpose=EXIT_ORDER`, `order_side="sell"`, JSON `side="buy"` → HTTP 1회.
- `purpose=EXIT_ORDER`, `order_side=None`, JSON `side="buy"` → HTTP 1회.
- `purpose=EXIT_ORDER`, `order_side="buy"`, JSON `side="buy"` → HTTP 1회.
- 세 호출 모두 recording fake session에 매수 payload가 전달됐다.

Remaining risk:

- private 메서드라도 모든 broker HTTP가 통과하는 공통 network boundary다. 향후 주문 경로가 잘못된 purpose를 지정하거나 내부 호출이 추가되면 ENTRY_DISABLED를 우회해 매수 주문을 전송할 수 있다.

### [CODEX-020]

Status: **PARTIALLY_RESOLVED**

Evidence:

- public buy/sell, binary halt, 4-state Kill Switch 정책과 read/reconciliation/cancel 예외는 집중 회귀에서 통과했다.
- `RequestPurpose`의 method 매트릭스는 GET/POST/DELETE 용도 오분류를 차단한다.
- 그러나 주문의 실제 의미는 HTTP method가 아니라 payload side로 결정되며, 현재 매트릭스는 POST의 ENTRY/EXIT 선언만 신뢰한다.

Remaining risk:

- 선언된 `EXIT_ORDER`와 실제 buy payload 불일치가 network boundary에서 허용된다.

### [CODEX-018]

Status: **RESOLVED**

Evidence: 모든 요청 직전 현재 credential 및 mode/endpoint를 재검증하는 기존 회귀가 통과했다.

### [CODEX-016], [CODEX-017], [CODEX-019]

Status: **RESOLVED**

Evidence: 주문 side/payload 보존, notification health/escalation, multiprocessing state-store 회귀가 집중 테스트에서 통과했다.

## New findings

### [CODEX-022] HIGH — EXIT_ORDER 선언으로 ENTRY_DISABLED 매수 payload를 전송할 수 있음

Status: **UNRESOLVED**

Evidence:

- `_METHOD_PURPOSES["POST"]`는 ENTRY_ORDER와 EXIT_ORDER를 모두 허용하지만 body를 검사하지 않는다.
- `_check_kill_switch()`는 `purpose`만 사용하고 `order_side` 및 JSON payload를 목적과 대조하지 않는다.
- 신규 테스트 `test_post_allows_entry_and_exit_purpose`도 두 purpose 모두에 동일한 buy payload를 사용하여 불일치를 허용한다.
- 위 세 가지 직접 재현에서 session 호출과 buy payload 전달을 확인했다.

Required behavior:

- 주문 POST의 공통 session 경계에서 `order_side`와 JSON `side`를 필수로 요구한다.
- `ENTRY_ORDER ↔ buy`, `EXIT_ORDER ↔ sell`, `order_side ↔ payload side`를 모두 정확히 일치시킨다.
- 누락, `None`, 비-dict body, 알 수 없는 side 및 모든 불일치는 session 접근 전에 fail-closed 처리한다.
- mismatch 각각에 대해 session 호출 0회를 보장하는 회귀 테스트를 추가한다.

## Executed tests

- 주문·Kill Switch·runtime credential·notification·state-store 집중 6개 파일 → **152 passed, 1 warning**
- 저장소 루트 `venv/bin/pytest -q` → **536 passed, 2 warnings**
- 저장소 루트 `venv/bin/python -m pytest -q` → **536 passed, 2 warnings**
- 저장소 상위 `venv/bin/python -m pytest us-stock-trading -q` → **536 passed, 2 warnings**
- CODEX-021 기존 exploit 3종 → 모두 session 호출 0회.
- CODEX-022 purpose/side/payload mismatch 3종 → 모두 session 호출 1회.

전체 테스트는 통과하지만 CODEX-022의 음성 테스트가 없으므로 안전성 해결의 근거가 되지 않는다.

## Warnings review

- urllib3 `NotOpenSSLWarning`: macOS LibreSSL 환경 호환성 경고다.
- scanner unknown-field `RuntimeWarning`: 미지원 필드를 의도적으로 건너뛰는 기존 테스트 경고다.
- 두 경고 모두 이번 주문 우회와 직접 관련되지 않는다.

## Kill-switch policy verification

- ACTIVE: public buy/sell 허용.
- ENTRY_DISABLED: public buy 차단, public sell 허용.
- ALL_TRADING_DISABLED/MANUAL_REVIEW: public buy/sell 차단.
- binary halt: public buy/sell 차단.
- READ_ONLY/RECONCILIATION/CANCEL_ORDER는 문서 정책대로 허용.
- 단, ENTRY_DISABLED + direct EXIT_ORDER/buy-payload 조합은 차단되지 않는다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 호출은 수행하지 않았다.
- 직접 재현은 recording fake session만 사용했다.
- 테스트 중 실제 외부 호출 증거는 없었다.

## Operational file safety

- `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966` 불변.
- `universe.csv`: SHA-256 `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`, 833518 bytes, mtime `1784558966` 불변.
- `order_reconciliation.csv`, `scalping_watchlist.csv`는 검증 전후 모두 존재하지 않았다.
- `docs/live_review/LIVE_APPROVAL_RECORD.md`: SHA-256 `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`, `approved: false`, `live_enabled: false`, 상태 `BLOCKED`로 불변.
- 테스트가 `strategy_performance.csv` mtime만 갱신했으며 내용 SHA-256 `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`와 크기는 불변이었다. 검증 기준 mtime `1784906741`로 복원했다.

## Document consistency

- validation package SHA-256은 보고값과 일치하며 이전 패키지와 다르다.
- `536 passed, 0 failed, 2 warnings` 주장은 재현됐다.
- `purpose=None`, 옛 인자-only 및 public buy 우회 차단 주장은 재현됐다.
- “payload의 side와 purpose가 일치하는지도 세션 호출 직전 재검증” 주장은 public `submit_order()`에만 해당하며 공통 `_request()`에는 적용되지 않아 문서가 해결 범위를 과장한다.
- `approved: false`, `live_enabled: false`, `BLOCKED`는 정확하다.

## Unverified areas

- 실제 Alpaca/Slack/Yahoo E2E
- 실제 포지션 청산 및 broker reconciliation
- Ubuntu 운영 환경의 flock 및 실제 스케줄러

## Phase decision

- CODEX-021: **PARTIALLY_RESOLVED**
- CODEX-022 HIGH: **UNRESOLVED**
- Overall: **FAIL**
- Limited live review: **BLOCKED**
- Live trading: **DO_NOT_ENABLE**

## Required next action

1. `_request()`의 주문 POST 경계에서 purpose, `order_side`, payload `side`의 완전한 일치를 강제한다.
2. 누락·None·불일치·비정상 payload 각각의 HTTP 0회 회귀 테스트를 추가한다.
3. 독립 재검증 전 병합·push·limited live review·실거래 활성화를 진행하지 않는다.
