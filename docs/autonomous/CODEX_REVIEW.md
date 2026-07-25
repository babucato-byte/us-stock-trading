# CODEX_REVIEW

Review target: CODEX-022 및 CODEX-021 잔여분 최종 수정 독립 재검증

Commits: `a31290b`, `5aac75b`, `8803252`

Validation package SHA-256: `32726d8fdd3aeeb3a9f96a764dda6fd236fca7b2446da7beb785ad125e57aad6`

Date: 2026-07-25

Overall verdict: **PASS_WITH_CONDITIONS**

Limited live review recommendation: **READY_FOR_LIMITED_LIVE_REVIEW**

Live trading: **DO_NOT_ENABLE**

`_request()`의 공통 network boundary에 `purpose` × `order_side` × payload `side` 중앙 검증이 추가됐다. 이전 CODEX-022의 세 가지 우회 조합은 모두 session 호출 전에 `ValueError`로 차단됐고, 정상 entry/exit 및 read/reconciliation/cancel 경로는 회귀 없이 동작했다. 미해결 CRITICAL/HIGH/MEDIUM Finding은 없다. 다만 실제 계좌, 절대 주문 한도, 허용 종목·시간, reconciliation 결과, 승인자와 롤백 담당자 등 운영자 `TBD`가 남아 있으므로 제한적 실거래의 사람 검토 단계로만 진행할 수 있으며 실거래 활성화는 승인하지 않는다.

## Previous findings verification

### [CODEX-022]

Status: **RESOLVED**

Evidence:

- `_PURPOSE_REQUIRED_SIDE`가 `ENTRY_ORDER → buy`, `EXIT_ORDER → sell`을 단일 매핑으로 정의한다.
- `validate_order_intent()`가 주문 purpose에서 `order_side` 필수, dict payload 필수, `side` 키 필수 및 정확한 소문자 문자열 `buy`/`sell`을 요구한다.
- purpose, `order_side`, payload `side` 중 하나라도 요구 side와 다르면 `_validate_runtime_safety()`, Kill Switch 조회 및 `session.request()`보다 먼저 `ValueError`가 발생한다.
- READ_ONLY/RECONCILIATION/CANCEL_ORDER에 `order_side` 또는 payload `side`가 섞이면 fail-closed 처리한다.
- 기존에 잘못된 buy payload를 양쪽 purpose에 사용하던 테스트가 purpose별 정상 side를 사용하도록 수정됐다.

Direct reproduction under `ENTRY_DISABLED`:

- `EXIT_ORDER + order_side="sell" + payload.side="buy"` → `ValueError`, HTTP 0회.
- `EXIT_ORDER + order_side=None + payload.side="buy"` → `ValueError`, HTTP 0회.
- `EXIT_ORDER + order_side="buy" + payload.side="buy"` → `ValueError`, HTTP 0회.

Normal-path verification:

- ACTIVE에서 `ENTRY_ORDER + buy + buy` 및 `EXIT_ORDER + sell + sell`은 각각 HTTP 1회.
- public `submit_order()`의 정상 buy/sell도 동일 중앙 게이트를 거쳐 각각 HTTP 1회.
- payload 누락, side 키 누락, 비-dict, 대소문자·공백 변형, bool/int side는 모두 HTTP 0회.

Remaining risk:

- 이번 Finding과 관련된 코드 잔여 위험은 확인되지 않았다.

### [CODEX-021]

Status: **RESOLVED**

Evidence:

- `purpose`는 기본값 없는 keyword-only 필수 enum이며 누락·`None`·잘못된 타입은 session 전에 차단된다.
- method-purpose 매트릭스와 신규 3자 중앙 검증이 결합되어 explicit None 및 목적 위장 우회를 모두 차단한다.
- `order_side`가 payload와 실제로 대조되므로 이전의 무효한 2차 방어선 문제가 해소됐다.

Remaining risk:

- 없음.

### [CODEX-020]

Status: **RESOLVED**

Evidence:

- public 및 direct broker 주문 모두 같은 `_request()` 경계에서 binary/4-state Kill Switch를 적용한다.
- ENTRY_DISABLED는 entry를 차단하고 liquidation을 허용하며, purpose를 EXIT로 위장한 buy payload도 신규 중앙 검증이 차단한다.
- ALL_TRADING_DISABLED/MANUAL_REVIEW 및 binary halt에서 주문은 차단되고 read/reconciliation/cancel은 정책대로 허용된다.

### [CODEX-016], [CODEX-017], [CODEX-018], [CODEX-019]

Status: **RESOLVED**

Evidence: 주문 side 보존과 다단계 Kill Switch, notification health/escalation, 현재 credential·mode·endpoint 재검증, multiprocessing state-store 회귀가 모두 통과했다.

## New findings

신규 CRITICAL/HIGH/MEDIUM/LOW Finding 없음.

## Executed tests

- 주문 intent·RequestPurpose·Kill Switch·runtime credential 집중 4개 파일 → **150 passed, 1 warning**
- 저장소 루트 `venv/bin/pytest -q` → **570 passed, 0 failed, 2 warnings**
- 저장소 루트 `venv/bin/python -m pytest -q` → **570 passed, 0 failed, 2 warnings**
- 저장소 상위 `venv/bin/python -m pytest us-stock-trading -q` → **570 passed, 0 failed, 2 warnings**
- CODEX-022 정확한 우회 조합 3종 및 정상 entry/exit 직접 검증.

## Warnings review

- urllib3 `NotOpenSSLWarning`: macOS LibreSSL 환경 호환성 경고다.
- scanner unknown-field `RuntimeWarning`: 미지원 scanner 필드를 의도적으로 건너뛰는 기존 테스트 경고다.
- 두 경고 모두 주문 안전성과 직접 관련되지 않으며 신규 Finding으로 등록하지 않는다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 호출은 수행하지 않았다.
- HTTP 검증은 recording fake session만 사용했다.
- broker 구현의 `session.request()` 직접 호출은 공통 `_request()` 한 곳에만 있으며 중앙 intent 검증 뒤에 위치한다.
- 테스트 중 실제 외부 socket 연결 증거는 없었다.

## Operational file safety

- `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966` 불변.
- `universe.csv`: SHA-256 `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`, 833518 bytes, mtime `1784558966` 불변.
- `order_reconciliation.csv`, `scalping_watchlist.csv`는 검증 전후 모두 존재하지 않았다.
- `docs/live_review/LIVE_APPROVAL_RECORD.md`: SHA-256 `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`, `approved: false`, `live_enabled: false` 불변.
- 전체 테스트가 `strategy_performance.csv` mtime만 갱신했으나 내용 SHA-256 `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`와 크기는 불변이었고 검증 기준 mtime `1784912492`로 복원했다.
- `.env`, credential, Kill Switch 및 notification 운영 상태 파일을 변경하지 않았다.

## Document consistency

- validation package SHA-256은 보고값 `32726d8f…`와 일치하고 이전 `4eb064d7…` 패키지와 다르다.
- `570 passed, 0 failed, 2 warnings` 주장은 독립 실행에서 재현됐다.
- CODEX-022의 세 가지 우회 차단 및 정상 주문 진행 주장은 코드와 테스트 결과가 일치한다.
- `main`은 `158671e`로 유지되고 검증 브랜치 HEAD는 `8803252`다.
- 현재 governance 문서의 `BLOCKED`는 Codex 재검증 전 상태로 정확했다. 이 PASS_WITH_CONDITIONS 판정 이후 별도 문서 갱신 사이클에서 `READY_FOR_LIMITED_LIVE_REVIEW`로 전환할 수 있다.
- `approved: false`, `live_enabled: false`는 계속 유지해야 한다.

## Remaining conditions and unverified areas

- 실제 연결 계좌 및 paper/live credential 확인.
- 실제 계좌의 현재 포지션·미체결 주문·broker/local reconciliation.
- 허용 종목, 허용 거래시간 및 주문당 절대 최대금액 확정.
- 운영 승인자, 검토 시각, 승인 범위 및 롤백 담당자 기입.
- 실제 Alpaca/Slack/Yahoo E2E와 Ubuntu 운영 환경의 flock·scheduler 검증.

이는 코드 Finding이 아니라 제한적 실거래 사람 검토에서 완료해야 할 운영 조건이다.

## Final decision

- CODEX-016~022: **RESOLVED**
- 신규 CRITICAL/HIGH: **NONE**
- Overall: **PASS_WITH_CONDITIONS**
- Limited live review: **READY_FOR_LIMITED_LIVE_REVIEW**
- Live trading: **DO_NOT_ENABLE**

실거래 활성화는 별도의 명시적 사용자 승인과 모든 `TBD` 완료 전까지 금지한다.
