# CODEX_REVIEW

Review target: Stage 3~10 최종 통합 독립 검증

Commits: `415c129` through `64a5551` on `orchestrator/20260725-013740-us-stock-trading`

Validation package: `docs/autonomous/FINAL_VALIDATION_PACKAGE.md`

Validation package SHA-256: `f8ed9093a59b9f7f94af25d206d621ce8054f36bc792e34933b993a10e649109`

Date: 2026-07-26

Overall verdict: **FAIL**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

Stage 3, 5~9의 격리된 도구·분석 구성요소는 대체로 구현 주장과 일치하고 전체 820개 테스트도 통과했다. 그러나 Stage 4 포지션 생명주기에는 broker 주문 접수와 실제 체결을 혼동하는 문제, 응답 유실 후 중복 청산 문제, 손상 저장소를 빈 포지션으로 취급하는 복구 문제가 있다. Stage 10의 30,000원 sizing과 allow-list는 실제 주문 경계에 전혀 연결되지 않아 현재 주문 경로의 금액·종목을 제한하지 못한다. 신규 HIGH Finding 4건이므로 최종 Stage 검증과 제한적 실거래 검토를 승인할 수 없다.

## Finding summary

| Finding | Severity | Status |
|---|---|---|
| CODEX-023 — 주문 접수를 체결로 간주하여 포지션을 조기 종료 | HIGH | UNRESOLVED |
| CODEX-024 — 청산 응답 유실 시 durable intent가 없어 중복 sell 가능 | HIGH | UNRESOLVED |
| CODEX-025 — 손상된 전체 포지션 저장소가 복구 스캔에서 빈 목록으로 변환됨 | HIGH | UNRESOLVED |
| CODEX-026 — 30,000원 sizing/allow-list가 실제 주문 경계에 미배선 | HIGH | UNRESOLVED |
| CODEX-027 — fill 수량·가격의 유효성과 단조성이 검증되지 않음 | MEDIUM | UNRESOLVED |

## New findings

### [CODEX-023] HIGH — broker 주문 접수를 실제 체결로 간주함

Status: **UNRESOLVED**

Evidence:

- `positions/lifecycle.py::_partial_exit_at_target_1()`은 응답 status code가 200/201이면 broker status와 `filled_qty`를 확인하지 않고 즉시 `PARTIAL_EXITED`로 전환하고 `remaining_qty`와 realized PnL을 변경한다.
- `_force_full_exit()`도 같은 조건만으로 즉시 `CLOSED`, `remaining_qty=0`을 기록한다.
- fake broker가 `status_code=200`, `data={"status": "accepted", "filled_qty": "0"}`을 반환하도록 직접 재현한 결과 stop-loss sell 한 건이 단순 접수됐을 뿐인데 로컬 상태는 `CLOSED`, 잔여 수량은 0이 됐다.
- `filled_avg_price`가 없으면 실제 현재가나 broker fill이 아니라 `stop_price`를 체결가로 대입해 realized PnL까지 확정한다.
- 테스트의 기본 `FakeBrokerResponse(status_code=200, data=None)`가 실제 fill 증거 없이 성공 체결로 취급되므로 기존 정상 테스트가 이 결함을 고정하고 있다.

Impact:

- 미체결·부분체결·취소될 수 있는 sell을 완료로 처리하여 실제 계좌에 잔여 포지션이 남아도 시스템은 flat으로 판단한다.
- 부분 청산이 접수만 된 상태에서 수량을 차감하면 이후 전량 청산도 실제 보유량보다 적게 제출되어 잔여 포지션을 방치할 수 있다.

Required behavior:

- submit 응답은 `*_SUBMITTED`까지만 영속화한다.
- broker reconciliation에서 실제 cumulative fill을 확인한 뒤에만 수량·PnL·`PARTIAL_EXITED`/`CLOSED`를 갱신한다.
- rejected/cancelled/expired/partial/unknown/timeout 각각을 명시적으로 처리하고 accepted-but-unfilled 음성 테스트를 추가한다.

### [CODEX-024] HIGH — 청산 주문에 durable reservation/reconciliation이 없어 timeout 후 중복 제출됨

Status: **UNRESOLVED**

Evidence:

- `_submit_exit_order()`는 임의 `client_order_id`를 만들지만 entry의 `try_reserve_order()`/intent ledger와 같은 제출 전 영속 예약을 하지 않고 client ID도 position record에 저장하지 않는다.
- `_force_full_exit()`와 `_partial_exit_at_target_1()`은 lock 안에서 메모리 상태를 `*_SUBMITTED`로 바꾼 후 broker를 호출한다.
- broker가 주문을 접수한 뒤 응답 유실/timeout 예외를 내면 `locked_position()` context manager가 변경을 쓰지 않으므로 디스크에는 기존 `STOP_ACTIVE` 상태가 남는다.
- 같은 stop-loss 호출을 두 번 실행한 직접 재현에서 두 번 모두 `TimeoutError`, 저장 상태는 계속 `STOP_ACTIVE`, sell 제출 시도는 2회였다.
- position restart recovery는 entry `client_order_id`만 조회하며 생성된 exit client ID는 추적할 수 없다.

Impact:

- 첫 sell이 broker에 접수됐는지 불명확한 상태에서 재실행하면 같은 수량의 sell이 중복 제출될 수 있다.
- 첫 주문이 실제 체결됐다면 두 번째 sell이 초과 청산 또는 의도하지 않은 short exposure를 만들 수 있다.

Required behavior:

- broker 호출 전에 exit identity와 `EXIT_SUBMISSION_PENDING`을 원자적으로 영속화한다.
- timeout/예외 이후 자동 재제출하지 않고 같은 client ID로 broker reconciliation을 수행한다.
- full/partial exit 모두 durable idempotency ledger를 사용하고 restart recovery가 exit order identity를 조회해야 한다.

### [CODEX-025] HIGH — 손상된 position store가 restart recovery에서 “포지션 없음”으로 보임

Status: **UNRESOLVED**

Evidence:

- `positions/store.py::_read_raw()`은 파일 파싱 실패를 `{"positions": {}, "_file_corrupted": True}`로 나타낸다.
- `load_position(id)`는 이 sentinel을 `RECOVERY_REQUIRED`로 변환하지만 `load_all()`은 동일 상태에서 빈 dict `{}`를 반환한다.
- `load_non_terminal()`과 `recover_on_restart()`는 `load_all()`에 의존한다.
- 손상 JSON을 직접 재현했을 때 `load_position("possibly-live")`는 `RECOVERY_REQUIRED`였지만 `load_all() == {}`, `load_non_terminal() == {}`, `recover_on_restart() == []`였다.

Impact:

- 실제 live position이 저장돼 있던 파일 전체가 손상되면 시작 복구는 이를 “열린 포지션 없음”으로 해석해 broker reconciliation과 운영자 escalation을 수행하지 않는다.
- 모듈 설명의 fail-closed 주장과 반대로 fail-open 복구 결과다.

Required behavior:

- 전체 파일 손상은 빈 목록이 아니라 명시적 store-unavailable/fail-closed 상태로 전파한다.
- 새 진입과 정상 운용을 차단하고 broker 전체 포지션 조회 및 운영자 수동 검토를 요구한다.
- malformed/truncated/permission failure 각각의 restart recovery 테스트를 추가한다.

### [CODEX-026] HIGH — 30,000원과 allow-list가 실제 주문을 제한하지 않음

Status: **UNRESOLVED**

Evidence:

- 코드 검색 결과 `calculate_micro_order_quantity()`와 `is_symbol_allowed()`는 `live_readiness/` 자체와 테스트에서만 사용된다.
- `paper_strategy_order.py`, `positions/lifecycle.py`, `broker/alpaca_client.py`는 `live_readiness`를 import하거나 호출하지 않는다.
- 실제 주문 경계는 caller가 제공한 `symbol`과 `qty`를 그대로 전달한다.
- 빈 allow-list가 helper 내부에서는 fail-closed여도 helper가 호출되지 않으므로 주문 경로에는 아무 효과가 없다.
- sizing 최소 주문 금액도 placeholder이며 실제 FX, broker 최소 주문 및 계좌 특성을 검증하지 않았다.

Impact:

- 현재 코드는 “30,000원 제한 실거래”의 금액 또는 종목 범위를 기술적으로 보장하지 않는다.
- 운영자 실수나 다른 caller는 임의 symbol/qty 주문을 기존 broker 경계로 제출할 수 있다.

Required behavior:

- live 주문의 최종 공통 경계에서 허용 종목, KRW/USD budget, 주문당·일일 누적 금액 및 계좌 모드를 fail-closed 검증한다.
- 실제 환율/최소 주문/소수점 가능 여부를 확정하지 못한 상태는 주문 차단이어야 한다.
- public 및 direct broker 우회 테스트에서 session 호출 0회를 보장한다.

### [CODEX-027] MEDIUM — record_fill이 비정상·퇴행 fill을 허용함

Status: **UNRESOLVED**

Evidence:

- `record_fill()`은 `filled_qty`와 `average_fill_price`에 대해 숫자 타입, finite, 양수, requested quantity 상한 및 기존 cumulative fill 이상 여부를 검사하지 않는다.
- 직접 재현에서 `filled_qty=-3`, `filled_qty=NaN`, `average_fill_price=-5`가 모두 저장됐다.
- PARTIALLY_FILLED self-loop에서 더 작은 cumulative quantity를 전달해도 기존 fill을 감소시키고 remaining quantity를 덮어쓸 수 있다.

Impact:

- 청산 수량과 PnL 계산이 음수·NaN 또는 실제보다 작은 값으로 오염될 수 있으며 잔여 포지션을 잘못 판단할 수 있다.

Required behavior:

- finite positive price, `0 < cumulative_filled_qty <= requested_qty`, 기존 cumulative fill 비감소를 강제한다.
- 동일 cumulative fill의 중복 관측과 합법적인 partial→full 진행만 허용하는 테스트를 추가한다.

## Known-risk assessment

1. SQLite 병행 인프라 미배선: **DEFERRED / NOT A NEW BLOCKER**. 기존 CSV/JSON 경로를 자동 전환하지 않은 점은 안전한 선택이지만 다중 파일 트랜잭션 위험은 Phase 5 결정 사항으로 유지한다.
2. 첫 오류 시 `ENTRY_DISABLED` 자동 배선 부재: **NEEDS_USER_DECISION**, 제한적 실거래 전 보완 권고. 수동 절차는 무인 자동화의 동등한 안전장치가 아니다.
3. allow-list/sizing 미배선: **HIGH BLOCKER**, CODEX-026으로 등록.
4. 비용·점수·최소금액 가정: **PARTIALLY_ACCEPTED**. backtest/selection에는 명시된 가정으로 허용 가능하지만 live sizing의 placeholder는 CODEX-026 해결 전 사용할 수 없다.
5. 미검증 전략 후보: **ACCEPTED**. 주문 경로와 registry ACTIVE 승격에 연결되지 않아 현재 안전 blocker가 아니다.
6. dashboard 마지막 성공 시각 근사치: **LOW RESIDUAL RISK**. 운영 표시 정확도 문제이며 주문 안전장치는 아니다.
7. Phase 3 실제 실시간 데이터 미착수: **STRUCTURAL INCOMPLETENESS**. 구성 데이터 기반 도구 검증에는 독립적이나 live readiness를 주장할 수 없는 조건이다.

## Architecture-boundary verification

- `backtest/` 및 `strategy_selection/` production code는 `strategy.registry`를 import하지 않아 평가 결과가 자동 ACTIVE 승격되지 않는다.
- `StrategyRegistry`는 ACTIVE 최대 한 개를 구조적으로 강제한다.
- strategy source 모델은 ACTIVE 상태를 허용하지 않는다.
- SQLite state store는 실제 주문 판단 경로에 연결되지 않았다.
- position lifecycle 자체도 현재 기존 runtime CLI/scheduler에 연결되지 않았으므로 CODEX-023~025가 기존 주문 경로를 즉시 변경하지는 않는다. 그러나 완료된 Stage 4로 승인하거나 향후 live path에 연결하기에는 안전하지 않다.

## Executed tests

- Stage 안전 집중 8개 파일 → **173 passed, 0 failed, 1 warning**
- 저장소 루트 `venv/bin/python -m pytest -q` → **820 passed, 0 failed, 2 warnings**
- accepted-but-unfilled exit 직접 재현 → 로컬 `CLOSED`, remaining 0.
- timeout 이후 stop-loss 2회 직접 재현 → 저장 상태 `STOP_ACTIVE`, sell 시도 2회.
- 손상 position store 직접 재현 → 단건은 `RECOVERY_REQUIRED`, 전체 복구는 빈 목록.
- 음수·NaN 수량 및 음수 평균 체결가 직접 재현 → 모두 저장 허용.

전체 회귀는 통과하지만 위 실패 의미론에 대한 음성 테스트가 없으며 일부 기존 테스트는 HTTP 200을 fill로 간주하는 잘못된 기대를 갖고 있다.

## Warnings review

- urllib3 `NotOpenSSLWarning`: macOS LibreSSL 환경 경고다.
- scanner unknown-field `RuntimeWarning`: 의도된 기존 테스트 경고다.
- 신규 Stage 안전성과 직접 관련된 warning은 없다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 및 기타 외부 API 호출은 수행하지 않았다.
- 직접 재현과 테스트는 fake broker/session 및 임시 파일만 사용했다.
- 외부 socket 연결 증거는 없었다.

## Operational file safety

- `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966` 불변.
- `universe.csv`: SHA-256 `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`, 833518 bytes, mtime `1784558966` 불변.
- `strategy_performance.csv`: SHA-256 `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`, 69 bytes 불변. 테스트가 mtime만 갱신하여 검증 기준 `1784997139`로 복원했다.
- `docs/live_review/LIVE_APPROVAL_RECORD.md`: SHA-256 `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`, `approved: false`, `live_enabled: false` 불변.
- `.env`, credential, Kill Switch 및 notification 운영 상태 파일을 변경하지 않았다.

## Documentation review

- 820 passed 및 2 warnings 주장은 재현됐다.
- 실제 외부 API 0회, 운영 파일 불변, main/origin 미변경 주장은 확인 범위에서 일치한다.
- final package의 “position duplicate-exit prevention is structural” 주장은 broker 예외/응답 유실 경로에서는 사실이 아니다.
- “30,000원 제한 실거래 준비”는 helper와 playbook 준비로는 맞지만 실제 주문 제한이 아니므로 limited-live readiness 근거로 사용할 수 없다.
- package §1은 “20개”라고 쓰면서 표에 21개를 열거하고, 문서 작성 뒤 커밋 `530f888`/`64a5551`을 검증 대상 목록에 포함하지 않는다. 안전 blocker는 아니지만 다음 패키지에서 정확한 target range와 HEAD를 갱신해야 한다.

## Unverified areas

- 실제 Alpaca fill/rejection/cancel/partial-fill E2E
- 실제 broker 전체 포지션 및 open-order reconciliation
- 실제 FX·Alpaca 최소 주문·fractional-share 계좌 정책
- Ubuntu 운영 환경의 flock/SQLite 및 실제 scheduler
- 실제 1분봉 live feed

## Final decision

- Previous CODEX-016~022: **RESOLVED (no observed regression)**
- New HIGH findings: **CODEX-023~026 UNRESOLVED**
- New MEDIUM finding: **CODEX-027 UNRESOLVED**
- Overall: **FAIL**
- Stage 3~10 validation: **KEEP_IN_PROGRESS**
- Limited live review: **BLOCKED**
- Live trading: **DO_NOT_ENABLE**

CRITICAL/HIGH Finding이 남아 있으므로 다음 Phase, limited live review, 병합, push 또는 실거래 활성화로 진행하지 않는다.
