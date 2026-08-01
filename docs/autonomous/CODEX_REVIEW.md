# CODEX_REVIEW — `fcfc6f26` 독립 재검증

## 0. 검증 시작 명령과 원문 결과

검증 시작 시 다음 명령을 새로 실행했다. 이전 검증의 테스트 결과나 판정을 재사용하지 않았다.

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git show --stat --oneline HEAD
```

원문 결과:

```text
# git status --short
(no output — clean)

# git branch --show-current
feature/kis-live-broker

# git rev-parse HEAD
fcfc6f26ec194b8e16c1b3dc7ab85b8a5b52b2bb

# git show --stat --oneline HEAD
fcfc6f2 CODEX-049: ship an actually-deployable Oracle service package
 deploy/systemd/us-stock-trading-live.service      |  34 +++
 deploy/systemd/us-stock-trading-reconcile.service |  29 ++
 deploy/systemd/us-stock-trading-reconcile.timer   |  13 +
 deploy/systemd/us-stock-trading-shadow.service    |  35 +++
 deploy/systemd/us-stock-trading-shadow.timer      |  13 +
 docs/deployment/ORACLE_KIS_MIGRATION_RUNBOOK.md   | 233 +++++++++++-----
 scripts/install_oracle_services.sh                | 110 ++++++++
 scripts/preflight_kis_live.py                     | 294 ++++++++++++++++++++
 scripts/run_live_buy_entry.py                     |  88 ++++++
 scripts/run_reconciliation.py                     | 152 ++++++++++++
 scripts/run_shadow_mode.py                        | 313 ++++++++++++++++++++++
 tests/test_oracle_deploy_package.py               | 290 ++++++++++++++++++++
 12 files changed, 1534 insertions(+), 70 deletions(-)
```

시작 조건 판정:

- 브랜치 `feature/kis-live-broker`: **일치**
- HEAD `fcfc6f26ec194b8e16c1b3dc7ab85b8a5b52b2bb`: **일치**
- working tree: **clean**
- `TARGET_COMMIT_MISMATCH`: **해당 없음**

## 1. 변경 범위와 테스트 수집 확인

```text
git diff 6c30690..fcfc6f26 --stat
42 files changed, 5738 insertions(+), 1001 deletions(-)
```

대상 구간 커밋:

```text
fcfc6f2 CODEX-049: ship an actually-deployable Oracle service package
2933550 CODEX-044/047/048: self-collected reconciliation, CAS state writes, durable audit
20731b5 CODEX-050: centralize secret redaction and close the reproduced leaks
2599d73 tests: stamp the affordability account snapshot per test, not at import
f4407a5 Record Codex final independent revalidation: BLOCKED (044/047/048/049/050)
```

새 collect 결과:

```text
venv/bin/python -m pytest --collect-only -q
1911 tests collected in 2.84s
```

구현자 보고의 1,911건과 일치한다. 신규 주요 테스트 파일도 실제 collection에 포함됐다.

## 2. 최종 판정

Overall verdict: **BLOCKED**

Oracle deployment: **DO_NOT_DEPLOY**

Live trading: **DO_NOT_ENABLE**

CODEX-044, CODEX-047, CODEX-050의 이전 재현 결함은 최신 코드에서 해결됐다. 그러나 다음 두
필수 조건은 여전히 충족되지 않는다.

1. CODEX-048의 `GATE_APPROVED`와 `EXECUTION_PLANNED` audit event가 broker transport 호출 전에
   기록되지 않고, `execution_engine.submit_buy_order()`/`submit_sell_order()`가 반환된 뒤 기록된다.
   broker 호출 중 process crash가 발생하면 주문 전 승인·실행예정 audit가 없다.
2. CODEX-049 preflight는 검증 커밋을 정확히 고정한다고 설명하지만 실제 구현은 임의 길이 prefix를
   허용한다. 단 한 글자 `f`도 현재 HEAD와 일치한다고 통과하는 것을 직접 재현했다.

Shadow 누락·감사 순서 위험은 기존 최종 기준의 명시적 `BLOCKED` 사유다.

## 3. 새 테스트 실행 결과

### 집중 안전 테스트

저장소에 494건 집중 실행 명령이 문서화되어 있지 않아, 기존 broker/order/lifecycle 안전 파일과
이번 변경의 reconciliation/CAS/Shadow/redaction/Oracle package 파일 및 operational dashboard
검증을 명시적으로 묶었다.

```text
494 passed, 0 failed, 0 skipped, 0 xfailed, 1 warning in 14.69s
```

### 정방향 전체 회귀

```text
venv/bin/python -m pytest -q
1911 passed, 0 failed, 0 skipped, 0 xfailed, 2 warnings in 55.86s
```

### 역방향 전체 회귀

```text
venv/bin/python -m pytest -q $(rg --files tests -g 'test_*.py' | sort -r)
1911 passed, 0 failed, 0 skipped, 0 xfailed, 2 warnings in 60.16s
```

세 실행 모두 이번 검증에서 새로 수행했다. 외부 Alpaca/KIS network 호출은 없었으며 테스트는
fake/recording broker와 격리된 임시 상태를 사용했다.

경고는 기존 두 종류뿐이다.

- local Python의 LibreSSL 2.8.3에 대한 urllib3 v2 경고
- unsupported scanner field를 의도적으로 경고하고 skip하는 방어 테스트

테스트 결과에는 영향을 주지 않았다. Oracle KIS HTTPS read-only 실행 전 Oracle Python이
OpenSSL 1.1.1+인지 확인하는 조건은 유지한다.

## 4. CODEX-044 — 실제 reconciliation 및 UNKNOWN

Status: **RESOLVED**

- `ReconciliationSnapshot`은 caller가 boolean을 주입하는 구조가 아니라 Execution Engine이 KIS
  positions/open orders/fills와 내부 positions/orders/UNKNOWN을 직접 조회해 생성한다.
- buy/sell은 동일한 `_submit_new_order()`에서 transport 직전 snapshot을 만들고 검증한다.
- snapshot 없음, 다른 account/symbol, stale/future timestamp, read failure, position/open-order/fill
  mismatch, account-wide UNKNOWN 모두 broker 호출 전에 차단된다.
- 운영 경로의 `reconciliation_ok=True`, `has_unknown_orders=False` 주입은 발견되지 않았다.
  검색 결과는 과거 구현을 설명하는 주석뿐이다.
- UNKNOWN은 더 이상 `(symbol, side)` 한정이 아니라 account-wide query다.
- UNKNOWN reconciliation은 matching fill row를 누적하고 `requested_quantity`와 비교한다.
  partial은 `PARTIALLY_FILLED`, equal은 `FILLED`, overfill 또는 요청수량 없음은 unresolved다.
- periodic reconciliation은 required KIS read가 모두 성공한 뒤에만 clean result를 기록한다.
  실패한 read는 clean timestamp를 갱신하지 않는다.

필수 결과인 read failure/stale/UNKNOWN 시 신규 transport 0회가 신규 negative tests에서 확인됐다.

## 5. CODEX-047 — Execution Engine 상태머신/CAS

Status: **RESOLVED**

- migration 8은 `kis_order_idempotency.version`과 append-only `order_state_events`를 추가한다.
- `idempotency.update_status()`는 삭제됐다. non-test source에서 bare status-write API 호출은 없다.
- `order_repository.compare_and_set_state()`는 state machine transition을 검증하고
  `(internal_order_id, expected_state, expected_version)` CAS를 수행한다.
- 상태 row와 event row는 하나의 `BEGIN IMMEDIATE` transaction에서 함께 commit/rollback된다.
- rowcount가 정확히 1이 아니면 conflict로 중단하고 broker를 재호출하지 않는다.
- cancel은 transport 전에 `CANCEL_PENDING`을 durable하게 기록하며 ambiguous/rejected cancel은
  상태를 추측하지 않고 UNKNOWN으로 보낸다.
- UNKNOWN resolution도 `via_reconciliation=True`와 expected UNKNOWN/version CAS를 사용한다.
- UNKNOWN->SUBMITTING 자동 재제출 경로는 없다.

신규 CAS, illegal transition, concurrent writer, atomic rollback, cancel race 및 reconciliation
테스트가 실제 DB 경로를 실행한다.

## 6. CODEX-048 — Shadow Mode 완전성

Status: **UNRESOLVED**

해결된 부분:

- SQLite `shadow_audit_events`가 buy와 sell 양쪽의 config/signal/instrument/price/cash/
  reconciliation/UNKNOWN/duplicate/HALT/gate/terminal 결과를 기록한다.
- SQLite transaction으로 process 간 atomic append와 crash-safe persistence를 제공한다.
- 모든 run은 `SHADOW_COMPLETED` 또는 `SHADOW_ERROR` terminal event를 갖도록 구성되며 누락 run을
  query할 수 있다.
- JSONL도 flock 안에서 size rotation, flush, fsync를 수행한다. retention과 corruption reporting이
  추가됐다.
- 테스트 파일은 임시 경로/DB로 격리된다.

남은 결함:

- buy 경로는 `execution_engine.submit_buy_order()`가 반환된 뒤
  `GATE_APPROVED`/`EXECUTION_PLANNED`를 기록한다(`kis_live_trading.py:409-425`).
- sell 경로도 `execution_engine.submit_sell_order()`가 반환된 뒤 두 event를 기록한다
  (`brokers/kis_broker_adapter.py:229-280`).
- 즉 event 이름과 달리 `EXECUTION_PLANNED`는 실제로 `SUBMITTED` 이후의 사후 기록이다. 테스트는
  event 존재만 검사하며 broker call보다 먼저 기록됐는지 검사하지 않는다.
- broker transport 시 process가 종료되면 실제 주문은 KIS에 도달했지만 승인/실행예정 audit가
  없는 run이 남을 수 있다. 이는 이전 finding의 “주문 직전 전체 경로 기록” 요구를 충족하지
  못한다.

따라서 CODEX-048은 완전히 해결되지 않았다.

## 7. CODEX-049 — Oracle 런북/서비스 패키지

Status: **UNRESOLVED**

해결된 부분:

- repository-tracked Shadow, reconciliation, disabled-live service/timer unit이 존재한다.
- executable preflight, Shadow, reconciliation, live-entry, installer scripts가 존재한다.
- installer는 read-only timer 두 개만 enable하고 live service는 명시적으로 disable한다.
- EnvironmentFile permissions, systemd hardening, migration, read-only posture, reconciliation,
  rollback 및 journal 확인 절차가 실제 파일/명령과 연결된다.
- Shadow service는 execution engine을 import하지 않고 KIS read-only method만 사용한다.

남은 결함 직접 재현:

```text
input environment:
VALIDATED_COMMIT=f
DEPLOYED_COMMIT=f
actual HEAD=fcfc6f26ec194b8e16c1b3dc7ab85b8a5b52b2bb

actual result:
[PASS] commit_match: validated == deployed == HEAD (fcfc6f26ec19)
```

원인은 `check_commit_match()`가 exact equality 대신 다음 양방향 prefix 조건을 사용하기 때문이다.

```python
if not head.startswith(deployed) and not deployed.startswith(head):
```

따라서 빈 값만 아니면 1자리 prefix도 검증된 정확한 배포 커밋으로 오인할 수 있다. 테스트는
40자리 all-zero mismatch만 검사하고 short-prefix false positive를 검사하지 않는다. 검증 대상
HEAD를 정확히 고정해야 한다는 배포 전 안전 조건이므로 exact 40자리 equality가 필요하다.

또한 KIS current-price field 및 cancel TR_ID의 실제 live response 확인은 Oracle read-only 단계의
외부 조건으로 남는다.

## 8. CODEX-050 — 민감정보 마스킹

Status: **RESOLVED**

- `Authorization: Bearer <token>`은 token 값까지 마스킹된다.
- single/double-quoted Python/JSON dict의 CANO/access token/App Key/App Secret가 마스킹된다.
- nested dict/list/tuple/set/dataclass/exception과 `(key, value)` pair를 처리한다.
- configured account number가 key 없이 free text에 있어도 마지막 4자리만 남긴다.
- KIS raw response dict/row는 `safe_repr()`로 structural redaction 후 제한 길이로 출력된다.
- KIS error body와 broker error message도 redaction을 거친다.
- logging filter와 Shadow/order-event persistence boundary가 별도로 재-redact한다.
- account correlation은 alias 또는 keyed HMAC fingerprint를 사용한다.

이전 세 누출 입력을 다시 실행한 결과는 다음과 같다.

```text
Authorization: ***REDACTED***
KIS position row malformed: {'CANO': '***REDACTED***', 'qty': 'bad'}
KIS price response missing: {'access_token': '***REDACTED***'}
```

신규 source leak sweep 및 persistence/logging tests도 통과했다.

## 9. 새로운 Finding

### CODEX-051 — MEDIUM — Oracle preflight가 임의 길이 commit prefix를 exact match로 승인

Status: **OPEN**

CODEX-049의 잔여 범위이면서 독립적으로 재현 가능한 preflight 결함이다. 1자리 prefix도 통과해
운영자가 잘못 축약한 값 또는 잘못 고정된 검증 기준을 발견하지 못한다. full 40-character
`VALIDATED_COMMIT == DEPLOYED_COMMIT == git rev-parse HEAD` 비교와 negative short-prefix test가
필요하다.

새로운 CRITICAL/HIGH finding은 발견하지 않았다.

## 10. 운영 파일과 산출물

테스트 전후 값은 동일하다.

| File | SHA-256 | Size | mtime |
|---|---|---:|---:|
| `order_history.csv` | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| `universe.csv` | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| `strategy_performance.csv` | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

- `.db-journal`, `.db-wal`, `.db-shm`: 없음
- `*shadow*.jsonl`: 없음
- 기타 새 운영 DB/CSV/log: 없음
- 보고서 갱신 전 working tree: clean
- 보고서 갱신 전 `git diff --check`: pass

## 11. 해제 전 필수 조치

1. Execution Engine이 authorization과 gate approval을 완료한 뒤, broker transport를 호출하기
   전에 `GATE_APPROVED`와 `EXECUTION_PLANNED` audit를 durable하게 기록한다.
2. transport 결과 뒤에는 `EXECUTION_SUBMITTED`/`TRANSPORT_RESULT` 등 실제 사후 event를 별도로
   기록해 planned와 submitted 의미를 구분한다.
3. buy/sell 모두에서 audit persistence 실패 시 transport 0회인 fail-closed test와 event-before-
   transport ordering test를 추가한다.
4. Oracle preflight commit 비교를 full exact equality로 바꾸고 empty/1-char/short/overlong prefix
   negative tests를 추가한다.
5. 수정 HEAD에서 494 집중 및 1,911 수준 정·역순 전체 회귀를 다시 독립 검증한다.
6. 그 전까지 KIS/Alpaca order flags와 live rollout을 disabled로 유지하고 live service를 시작하지
   않는다.

현재 HEAD는 Oracle deploy 또는 실거래 활성화 대상으로 승인하지 않는다.
