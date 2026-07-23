# CODEX_REVIEW

Review target: CODEX-016~019 remediation 독립 재검증

Commits: `6ad4841`, `79eaa81`, `00b0f68`, `50a097d`, `8fec290`

Branch: `orchestrator/20260723-020935-us-stock-trading`

Date: 2026-07-23

Overall verdict: **FAIL**

전체 417개 테스트와 집중 테스트는 통과했으나 CODEX-016과 CODEX-018의 필수 동작이 실제 HTTP/order call path 전체에 적용되지 않았다. sell 주문은 상태 정책만 sell로 검사한 뒤 broker에는 side를 전달하지 않아 기본 buy로 제출되며, 환경 런타임 재검증은 `_request()` GET 경로에만 추가되어 `submit_order()`와 reconciliation 조회가 우회한다. HIGH Finding이 부분 해결 상태로 남아 제한적 실거래 검토를 재개할 수 없다.

## Previous findings verification

### [CODEX-016] HIGH — 다단계 kill switch production wiring

Status: **PARTIALLY_RESOLVED**

Evidence:

- `paper_strategy_order.submit_order()`는 매 호출마다 binary halt와 다단계 상태를 다시 읽는다.
- ACTIVE/ENTRY_DISABLED/ALL_TRADING_DISABLED/MANUAL_REVIEW의 buy 차단과 손상 상태 fail-closed는 fake broker 기준으로 확인됐다.
- `main()`의 신규 진입은 `side="buy"`로 명시되어 ENTRY_DISABLED에서 broker 호출 전에 차단된다.
- 그러나 wrapper는 `side="sell"`로 `is_liquidation_allowed()`를 검사한 뒤 `broker.submit_order()` 호출에는 `side`를 전달하지 않는다.
- 격리 재현에서 `paper_strategy_order.submit_order(..., side="sell")`의 broker kwargs는 `qty`, `client_order_id`뿐이었고 `side`가 없었다. 실제 `AlpacaBroker.submit_order()` 기본값은 `side="buy"`다.
- 신규 FakeBroker 역시 side 인자를 받지 않아 이 오류를 테스트하지 못한다.

Remaining risk: ENTRY_DISABLED에서 허용된 청산 요청이 실제 broker에는 buy 주문으로 제출될 수 있다. side를 end-to-end 전달하고 실제 broker signature를 반영한 회귀 테스트가 필요하다. 자동 청산 경로가 아직 없다면 sell 지원 완료를 주장하지 말고 명시적으로 미구현 처리해야 한다.

### [CODEX-017] HIGH — Slack health monitor production wiring

Status: **RESOLVED**

Evidence:

- `_safe_send_slack_alert()`가 `notification_health.send_with_health_tracking(send_slack_alert, message)`를 실제 운영 경로에서 호출한다.
- 성공/실패 기록, 임계값 도달 시 ENTRY_DISABLED 상승, 이후 실제 buy 주문 차단을 통합 테스트가 검증한다.
- Slack 예외는 주문 결과를 변경하거나 다음 symbol 처리를 중단하지 않는다.

Remaining risk: fallback local log와 health state 자체에 대한 운영 모니터링·보존 정책은 별도 운영 항목이다.

### [CODEX-018] MEDIUM — 주문 직전 환경 재검증

Status: **PARTIALLY_RESOLVED**

Evidence:

- `AlpacaBroker._request()`는 session.request 직전에 `validate_order_allowed_now()`를 호출해 get_account/get_positions/get_recent_orders/get_assets 경로를 재검증한다.
- 그러나 `AlpacaBroker.submit_order()`는 `_request()`를 사용하지 않고 `session.post()`를 직접 호출하며 runtime 재검증도 호출하지 않는다.
- `get_order_by_client_order_id()` 역시 직접 `session.request()`를 호출하고 runtime 재검증을 하지 않는다.
- 안전한 paper config로 broker를 생성한 뒤 환경을 unsafe live로 변경한 격리 재현에서 `broker.submit_order("AAPL")`가 session.post를 **1회 호출**하고 200을 반환했다.
- 신규 테스트 6건은 get_account/get_positions만 다루며 주문 POST와 reconciliation 조회를 포함하지 않는다.

Remaining risk: 가장 중요한 주문 제출 경로가 생성 시점 config에만 의존한다. 모든 직접 HTTP 경로에서 network call 직전 현재 환경을 검증하고 session 호출 0회를 검증해야 한다.

### [CODEX-019] MEDIUM — 상태 저장소 동시성

Status: **RESOLVED**

Evidence:

- kill-switch 및 notification state 모두 `fcntl.flock`으로 lock → 최신 재조회 → 병합 → atomic write 전체 구간을 보호한다.
- multiprocessing 동시 activate 3건의 audit history와 동시 failure 3건의 카운트가 모두 보존됐다.
- lock timeout에서 kill-switch는 명시적 예외, notification은 비전파 계약을 유지하며 원본 파일을 보존한다.

Remaining risk: 5초 timeout은 운영 관측 후 조정 가능한 LOW 수준 설정 위험이다.

## New findings

별도 신규 ID는 등록하지 않는다. sell side 전달 누락은 CODEX-016의 필수 side별 정책 범위이고, 직접 POST/reconciliation 우회는 CODEX-018의 “실제 요청 직전” 범위에 포함된다.

## Executed tests

- 집중 테스트 4개 파일 → **33 passed, 1 warning**
- 전체 `venv/bin/python -m pytest -q` → **417 passed, 0 failed, 2 warnings**
- 격리 production-path 재현:
  - paper→unsafe live 환경 변경 후 `AlpacaBroker.submit_order()` → session.post **1회**
  - wrapper `side="sell"` 호출 → broker kwargs에 side 없음

보고된 전체 테스트 수는 재현됐다. 보고서의 “집중 테스트 30건”과 실제 4개 파일 수집 결과 33건에는 수치 차이가 있으나 안전 판정의 직접 원인은 아니다.

## Warnings review

- urllib3 `NotOpenSSLWarning`: macOS LibreSSL 환경 경고다.
- scanner unknown-field `RuntimeWarning`: 의도된 기존 테스트 경고다.

신규 안전 관련 warning은 없다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 호출은 없었다.
- HTTP 검증은 recording session, Slack은 monkeypatch/fake를 사용했다.

## Operational file safety

- `order_history.csv` SHA-256: `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` (불변)
- `universe.csv` SHA-256: `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`
- `strategy_performance.csv` SHA-256: `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`
- 저장소 루트에 runtime state/log 파일을 생성하지 않았다.
- `.env`, live flag, 운영 서버, origin은 변경하지 않았다.

## Document consistency

- 417 passed, 2 warnings 및 운영 파일 무변경 주장은 실제 결과와 일치한다.
- CODEX-017/019 RESOLVED 주장은 코드와 테스트 근거에 부합한다.
- CODEX-016의 “매도→is_liquidation_allowed” 검사는 존재하지만 실제 broker 주문 side까지 이어지지 않는다.
- CODEX-018의 “get_account/get_positions/submit_order/get_order_by_client_order_id 전부 자동 적용” 주장은 실제 코드와 불일치한다.
- `READY_FOR_CODEX_REVALIDATION`은 검증 요청 상태로는 정확하지만 `READY_FOR_LIMITED_LIVE_REVIEW`로 승격할 수 없다.

## Unverified areas

- 실제 Alpaca/Slack E2E와 실제 계좌 reconciliation
- 자동 청산 구현 및 sell identity 전달
- macOS 외 운영 환경의 flock 동작
- 전원 손실 시 디렉터리 fsync 내구성
- 체크리스트의 운영자 TBD 항목

## Remediation decision

**KEEP_IN_PROGRESS**

CODEX-016 HIGH가 부분 해결 상태이고 CODEX-018도 핵심 주문 경로에서 미해결이다.

## Limited live review decision

**BLOCKED**

## Live trading recommendation

**DO_NOT_ENABLE**

`approved: false`, `live_enabled: false`를 유지한다. sell side end-to-end 전달과 모든 direct HTTP 경로의 runtime 재검증을 추가한 후 재검증해야 한다.
