# CODEX_REVIEW

Review target: CODEX-023~027 통합 수정 최종 독립 재검증

Commits: `f2afb4e`, `0f60ec9`, `c5c56c4`, `ee6dae2`, `f482e90`, `4de0714`, `e49753f`

Validation package SHA-256: `471ca57dc94713d7b2f26fe60eeb49e68764c473d21c41641198f79559a088a1`

Date: 2026-07-26

Overall verdict: **FAIL**

Limited live review: **BLOCKED**

Live trading: **DO_NOT_ENABLE**

accepted-but-unfilled 분리, timeout 후 동일 sell 재제출 차단, 손상 store의 명시적 복구 실패, fill 입력 검증은 각각 개선됐다. 그러나 exit fill 진행량을 SQLite에 먼저 커밋하고 JSON position을 나중에 저장하여 중간 실패 시 두 저장소가 영구 불일치할 수 있다. 또한 live gateway가 context의 symbol과 실제 주문 symbol을 결합하지 않아 허용된 context로 금지 종목을 제출할 수 있고 direct broker 호출은 전체 30K 게이트를 우회한다. 전체 suite도 장 마감 시각에 따라 4건 실패해 `923 passed`가 재현되지 않았다. 신규 HIGH Finding이 남으므로 최종 승인을 내릴 수 없다.

## Finding summary

| Finding | Previous severity | Status |
|---|---:|---|
| CODEX-023 | HIGH | RESOLVED |
| CODEX-024 | HIGH | PARTIALLY_RESOLVED |
| CODEX-025 | HIGH | RESOLVED |
| CODEX-026 | HIGH | PARTIALLY_RESOLVED |
| CODEX-027 | MEDIUM | RESOLVED |
| CODEX-028 — exit SQLite/JSON commit 순서가 fill 진행량을 유실 | HIGH | UNRESOLVED |
| CODEX-029 — live context symbol과 실제 주문 symbol 불일치 허용 | HIGH | UNRESOLVED |
| CODEX-030 — lifecycle 테스트가 실제 장 마감 시각에 의존 | MEDIUM | UNRESOLVED |

## Previous findings verification

### [CODEX-023]

Status: **RESOLVED**

Evidence:

- `positions/order_status.py`가 accepted/new/pending 계열을 `NOT_FILLED`, partial/filled만 실제 fill로 분류한다.
- HTTP 200 + `status="accepted"`, `filled_qty=0` 재현에서 position은 `EXIT_SUBMITTED`, remaining quantity와 PnL은 불변이었다.
- partial fill은 확인 수량만 반영하고 filled 상태만 정상 저장 경로에서 `CLOSED`로 전환한다.
- unknown broker status는 `MANUAL_REVIEW` 및 reconciliation-required로 fail-closed 처리한다.

Remaining risk:

- 정상 저장 경로의 accepted-vs-filled 문제는 해결됐다. 다만 fill 반영의 cross-store 원자성은 CODEX-028로 별도 등록한다.

### [CODEX-024]

Status: **PARTIALLY_RESOLVED**

Evidence:

- broker 호출 전에 SQLite `exit_intents` reservation과 JSON `*_SUBMITTED` 상태를 영속화한다.
- broker timeout 이후 재호출은 기존 intent를 reconcile하며 sell을 다시 제출하지 않는다.
- stale RESERVED, broker lookup failure 및 unknown submission도 자동 재주문하지 않는다.
- concurrent stop/target 요청의 단일 sell 테스트가 통과한다.

Remaining risk:

- SQLite intent와 JSON position은 단일 트랜잭션이 아니다.
- fill 진행량/CONFIRMED를 SQLite에 먼저 기록한 뒤 JSON atomic write가 실패하면 재시도에서 이미 적용된 fill로 간주하여 position 수량 반영을 영구 건너뛴다(CODEX-028).

### [CODEX-025]

Status: **RESOLVED**

Evidence:

- 전체 store 파싱 실패 시 `load_all()`/`load_non_terminal()`이 `PositionStoreCorruptedError`를 발생시키며 빈 dict로 변환하지 않는다.
- `recover_on_restart()`는 `STORE_UNAVAILABLE` typed result를 반환하고 Kill Switch를 `MANUAL_REVIEW`로 전환한다.
- broker가 있으면 full positions 조회를 best-effort로 포함하며 손상 파일을 자동 초기화하지 않는다.
- ops dashboard는 section-level safe wrapper를 통해 오류를 가시화한다.

Remaining risk:

- Kill Switch 저장 자체도 실패할 수 있으나 recovery result와 신규 position 생성 차단이 유지되어 이전 fail-open은 재현되지 않았다.

### [CODEX-026]

Status: **PARTIALLY_RESOLVED**

Evidence:

- `paper_strategy_order.submit_order()`의 live buy 경로에서 context 누락, 빈 allow-list, stale/missing FX, position/daily limits 및 risk cap이 broker 호출 전에 차단된다.
- gateway가 계산한 quantity로 caller qty를 대체하여 wrapper 경로의 notional cap을 적용한다.
- paper buy와 live sell은 해당 entry gate의 적용을 받지 않는다.

Remaining risk:

- gateway는 `live_entry_context.symbol`만 allow-list와 대조하고 실제 `submit_order(symbol)`과 일치시키지 않는다.
- direct `AlpacaBroker.submit_order()`는 gateway를 전혀 실행하지 않는다. public broker network boundary가 우회 가능한 상태이므로 “최종 공통 주문 경계” 보장은 충족하지 못한다.

### [CODEX-027]

Status: **RESOLVED**

Evidence:

- entry cumulative fill에서 numeric/finite/nonnegative/requested 상한/비감소 및 positive fill price를 mutation 전에 검증한다.
- exit path도 delta quantity와 price를 동일 validation module로 검사한다.
- 음수·NaN·Infinity·상한초과·퇴행 fill 재현이 모두 `InvalidFillError`로 차단되고 record는 불변이다.

## New findings

### [CODEX-028] HIGH — SQLite fill progress가 JSON position보다 먼저 커밋되어 수량 반영이 유실됨

Status: **UNRESOLVED**

Evidence:

- `_apply_exit_fill_progress()`는 JSON position record를 메모리에서 변경하는 동시에 `eil.update_progress()` 또는 `eil.mark_confirmed()`를 호출하며, 이 SQLite 함수들은 즉시 commit한다.
- 그 뒤 `locked_position()` context가 종료될 때 JSON `_atomic_write()`가 수행된다.
- SQLite commit 이후 JSON write가 실패하거나 프로세스가 종료되면 intent의 `confirmed_filled_qty`만 앞서간다.

Direct reproduction:

1. 10주 position에 sell partial fill 4주를 반환.
2. Phase C의 SQLite progress commit 뒤 JSON atomic write만 강제로 실패.
3. 재시작 상태: position `EXIT_SUBMITTED`, `remaining_qty=10`; intent `confirmed_filled_qty=4`.
4. broker가 이후 cumulative filled 10을 반환.
5. reconciliation은 delta `10-4=6`만 position에서 차감.
6. 최종 결과: `state=CLOSED`, `remaining_qty=4`, realized PnL도 6주분만 반영.

Impact:

- 실제로 청산된 수량과 로컬 remaining quantity/PnL이 불일치한다.
- `CLOSED`인데 remaining quantity가 양수인 모순 상태가 영속화되어 dashboard와 후속 위험 판단을 오염시킨다.
- immediate full fill에서 SQLite가 terminal CONFIRMED가 된 뒤 JSON write가 실패하면 active intent가 사라지고 `EXIT_SUBMITTED` position을 자동 복구할 근거도 잃을 수 있다.

Required behavior:

- SQLite progress를 position JSON보다 먼저 authoritative하게 전진시키지 않는다.
- 단일 DB 트랜잭션으로 position과 intent를 함께 저장하거나, 재실행 가능한 event/outbox를 사용하여 양쪽 write 순서 어느 지점에서 crash해도 cumulative broker truth로 position을 재구성해야 한다.
- SQLite-before-JSON, JSON-before-SQLite, process crash, fsync/os.replace 실패 fault-injection 테스트를 추가한다.
- `CLOSED` invariant로 `remaining_qty == 0`을 강제한다.

### [CODEX-029] HIGH — 허용 context로 금지된 실제 symbol을 제출할 수 있음

Status: **UNRESOLVED**

Evidence:

- `validate_and_size_live_entry(ctx)`는 `ctx.symbol`을 allow-list와 비교한다.
- wrapper는 실제 인자 `symbol`과 `ctx.symbol`의 동일성을 확인하지 않는다.
- 직접 재현에서 `ctx.symbol="AAPL"`, `allow_list=["AAPL"]`, 실제 `submit_order("TSLA", qty=999999, side="buy")`를 전달했다.
- 결과는 status 200이고 broker에는 `("TSLA", 2, "buy", ...)`가 전달됐다. 수량은 재산정됐지만 금지 symbol 주문은 차단되지 않았다.

Additional boundary risk:

- `AlpacaBroker.submit_order()`를 직접 호출하면 symbol, budget, FX 및 position/daily limit 전부 검사하지 않는다.
- 저장소의 현재 internal entry가 wrapper를 사용한다는 검색 결과는 향후 caller 또는 public broker API의 우회를 막는 구조적 보장이 아니다.

Required behavior:

- 실제 주문 symbol, context symbol 및 allow-list identity를 최종 network boundary에서 하나의 신뢰 가능한 값으로 결합한다.
- context가 별도 symbol을 보유해야 한다면 exact canonical match를 session 호출 전에 강제한다.
- direct public broker call도 live entry gate를 통과하거나 live entry purpose에는 검증된 immutable authorization artifact를 요구한다.
- context mismatch 및 direct-call 우회에서 session/broker 호출 0회 테스트를 추가한다.

### [CODEX-030] MEDIUM — lifecycle 테스트가 wall-clock EOD 상태에 따라 실패함

Status: **UNRESOLVED**

Evidence:

- `tests/test_position_lifecycle.py`의 target/stop/no-action 테스트가 `check_and_manage(..., now=None)`을 호출한다.
- production 함수는 실제 `eastern_now()`를 사용하고 EOD forced close를 가격 조건보다 먼저 평가한다.
- 미국 동부 장 마감 구간에 재실행한 집중 suite는 **157 passed, 4 failed**, 전체 suite는 **919 passed, 4 failed, 2 warnings**였다.
- 실패 4건은 기대했던 `PARTIAL_EXITED`/`STOP_ACTIVE`/`STOP_LOSS` 대신 `CLOSED`/`EOD_FORCED_CLOSE`가 나온 것이다.

Impact:

- 패키지의 `923 passed` 결과가 실행 시각에 따라 재현되지 않는다.
- 안전 회귀가 실제 코드 결함인지 시장 시각 간섭인지 구분하기 어렵고 CI 신뢰성이 저하된다.

Required behavior:

- EOD 자체 테스트를 제외한 lifecycle 단위 테스트는 timezone-aware `now`를 명시적으로 주입해 정규장 중간 시각으로 고정한다.
- 장 전/정규장/EOD/주말/DST 경계를 별도 parameterized 테스트로 분리한다.

## Known-risk reassessment

1. JSON position + SQLite exit intent 분리: 문서화만으로 충분하지 않으며 CODEX-028 HIGH로 실제 crash inconsistency가 재현됐다.
2. 첫 주문 오류 `ENTRY_DISABLED` 자동 배선: **NEEDS_USER_DECISION**, 일반 주문 장애의 자동 차단은 여전히 미구현.
3. direct broker gate 미적용: CODEX-026을 PARTIALLY_RESOLVED로 유지하는 HIGH residual risk.
4. live FX/최소주문/limit 가정: 실제 값 미확정 상태에서는 계속 live order 차단 조건이어야 한다.
5. 미검증 전략 후보와 dashboard mtime 근사: 주문 경로 비연결로 현재 blocker 아님.
6. Phase 3 live data feed 미착수: 제한적 live readiness와 독립적이지 않으며 실제 운영 검토 전 완료 또는 명시적 수동 공급 설계가 필요하다.

## Executed tests

- CODEX-023~027 집중 7개 파일 → **157 passed, 4 failed, 1 warning**
- 시간 영향을 받는 lifecycle 파일을 제외한 핵심 집중 표본 → **105 passed, 0 failed, 1 warning**
- 저장소 루트 `venv/bin/python -m pytest -q` → **919 passed, 4 failed, 2 warnings**
- accepted-but-unfilled, timeout retry, corrupted store, invalid fill 회귀는 통과.
- live context/actual symbol mismatch 직접 재현 → 금지 TSLA broker 호출 1회.
- Phase C SQLite-before-JSON failure 직접 재현 → 최종 `CLOSED`, `remaining_qty=4`.

## Warnings review

- urllib3 `NotOpenSSLWarning`: macOS LibreSSL 환경 경고다.
- scanner unknown-field `RuntimeWarning`: 의도된 기존 테스트 경고다.
- 이번 실패 4건은 warning이 아니라 실제 test failure다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 또는 기타 외부 API 호출은 수행하지 않았다.
- 모든 직접 재현은 fake broker/session과 임시 JSON/SQLite 파일을 사용했다.
- 테스트 중 실제 외부 socket 연결 증거는 없었다.

## Operational file safety

- `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966` 불변.
- `universe.csv`: SHA-256 `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`, 833518 bytes, mtime `1784558966` 불변.
- `strategy_performance.csv`: SHA-256 `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`, 69 bytes 불변. 테스트가 mtime만 갱신하여 검증 기준 `1785007030`으로 복원했다.
- 실제 저장소 루트 `TRADING_STATE.db*`는 검증 전후 존재하지 않았다.
- `docs/live_review/LIVE_APPROVAL_RECORD.md`: SHA-256 `27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe`, `approved: false`, `live_enabled: false` 불변.
- `.env`, credential, Kill Switch 및 notification 운영 상태 파일을 변경하지 않았다.

## Documentation consistency

- accepted-vs-filled, timeout duplicate suppression, corrupted store escalation 및 fill validation 주장은 정상 경로에서 재현됐다.
- `923 passed` 주장은 현재 실행 시각에서 재현되지 않았다.
- final package는 문서 작성 시점 HEAD를 `4de0714`로 기록하지만 실제 package commit `e49753f`와 현재 HEAD를 검증 대상 표에 반영하지 않았다.
- CODEX-026의 direct broker limitation은 숨기지 않고 기록했으나, 이는 원래 Finding의 최종 network-boundary 요구를 충족하지 못하므로 단순 유지보수 위험으로 하향할 수 없다.

## Unverified areas

- 실제 Alpaca accepted/partial/fill/cancel/reject E2E
- 실제 broker 전체 포지션 및 open-order reconciliation
- 실제 FX provider, Alpaca 최소 주문 및 fractional-share 계좌 정책
- hard process kill/fsync failure를 사용한 두 저장소 crash recovery
- Ubuntu 운영 환경과 실제 scheduler/live data feed

## Final decision

- CODEX-023: **RESOLVED**
- CODEX-024: **PARTIALLY_RESOLVED**
- CODEX-025: **RESOLVED**
- CODEX-026: **PARTIALLY_RESOLVED**
- CODEX-027: **RESOLVED**
- CODEX-028 HIGH: **UNRESOLVED**
- CODEX-029 HIGH: **UNRESOLVED**
- CODEX-030 MEDIUM: **UNRESOLVED**
- Overall: **FAIL**
- Stage 3~10: **KEEP_IN_PROGRESS**
- Limited live review: **BLOCKED**
- Live trading: **DO_NOT_ENABLE**

CRITICAL/HIGH Finding과 전체 테스트 실패가 남아 있으므로 다음 Phase, limited live review, 병합, push 또는 실거래 활성화로 진행하지 않는다.
