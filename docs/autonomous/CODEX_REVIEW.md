# CODEX_REVIEW — KIS 실거래 전환 최종 재검증

## 1. 검증 대상과 범위

- 저장소: `us-stock-trading`
- 브랜치: `feature/kis-live-broker`
- 검증 HEAD: `6c30690b52a68106f169faeb456dbef855b50fad`
- 검증일: 2026-08-01
- 시작 시 worktree: clean
- `git diff --check`: pass
- 필수 구현 커밋 포함:
  - `cae6e97` — Execution state-machine test coverage + Shadow Mode completeness/locking/rotation
  - `6c30690` — Oracle runbook sync

이번 작업에서는 구현 코드, 테스트 기대값, 커밋, 병합, push, 배포를 변경하지 않았다. 실제
Alpaca/KIS 외부 네트워크 호출도 수행하지 않았다. 이 파일만 검증 결과로 갱신했다.

## 2. 최종 판정

Overall verdict: **BLOCKED**

KIS limited live review: **BLOCKED**

Oracle deployment: **DO_NOT_DEPLOY**

Live trading: **DO_NOT_ENABLE**

전체 테스트 결과는 구현자 보고와 동일하게 재현됐다. 그러나 통과한 테스트가 실제 코드의 다음
차단 사유를 검출하지 않는다.

1. CODEX-044 reconciliation 결과가 KIS open-order/fill 조회 성공 전에 clean으로 기록되어,
   해당 조회가 실패해도 직전 clean timestamp가 갱신된다.
2. CODEX-047 cancel 및 UNKNOWN reconciliation 상태 쓰기가 상태머신과 DB expected-state/CAS를
   우회한다.
3. CODEX-048은 buy 경로 일부만 기록한다. sell 경로 기록, `fsync`, 보관 정책, 손상 보고가 없다.
4. CODEX-049 런북은 존재하지 않는 sell Shadow 경로를 존재한다고 설명하며, 필수 KIS position
   manager service/timer를 배포자가 새로 설계하도록 남긴다.
5. CODEX-050 free-text 마스킹은 Bearer token의 실제 token 값과 Python dict repr 안의
   `CANO`/`access_token` 값을 그대로 남기며, KIS broker가 raw response dict/row를 예외에 넣는다.

상태머신 미강제, Shadow 누락·손상 위험, 민감정보 원문 노출, 실행 불가능한 Oracle 런북 중
하나라도 있으면 `BLOCKED`라는 지시문의 기준을 충족한다.

## 3. 독립 테스트 재현

집중 안전 테스트:

```text
293 passed, 0 failed, 0 skipped, 0 xfailed, 1 warning
```

정방향 전체 실행:

```text
venv/bin/python -m pytest -q
1721 passed, 0 failed, 0 skipped, 0 xfailed, 2 warnings in 57.30s
```

역방향 전체 실행:

```text
venv/bin/python -m pytest -q $(rg --files tests -g 'test_*.py' | sort -r)
1721 passed, 0 failed, 0 skipped, 0 xfailed, 2 warnings in 49.29s
```

주입된 fake/recording session 밖의 외부 네트워크 호출은 0회였다. 정·역순 모두 통과해 현재
테스트가 관찰하는 범위에서는 순서 의존성이 재현되지 않았다.

경고 두 건은 다음과 같다.

- urllib3의 local LibreSSL 호환 경고: 현재 macOS venv의 Python `ssl`이 LibreSSL 2.8.3으로
  빌드된 환경 경고다. 테스트 결과에는 영향을 주지 않았다. 다만 urllib3 v2가 이 TLS backend를
  지원하지 않는다고 명시하므로 Oracle에서 실제 KIS HTTPS read-only 검증 전
  `python3 -c "import ssl; print(ssl.OPENSSL_VERSION)"`로 OpenSSL 1.1.1+인지 확인해야 한다.
- unsupported scanner-field 경고: `test_unknown_field_skips_with_warning`이 의도적으로
  `unknown_metric` 방어 경고를 발생시키며 정상적으로 skip하는 테스트다. Finding으로 승격하지
  않는다.

## 4. Finding별 재검증

### CODEX-042 — Alpaca 주문 완전 차단

Status: **RESOLVED**

- `AlpacaBroker._request()`의 실제 transport 직전 경계가 ENTRY/EXIT/CANCEL 목적에 대해
  `validate_alpaca_order_permitted()`를 실행한다.
- 현재 mode의 명시적 order flag, `execution_broker == "alpaca"`, 알려진 trading mode가 모두
  필요하다. `ALPACA_ORDER_ENABLED=true`, `EXECUTION_BROKER=kis` 조합도 차단된다.
- direct submit/cancel, legacy wrapper, dynamic import alias 테스트에서 recording session 호출은
  모두 0회다.
- `replace_order`, `close_position`, `close_all_positions` public method는 현재 구현에 존재하지
  않아 직접 호출 자체가 불가능하며 HTTP에도 도달하지 않는다.

필수 결과: **운영 기본 설정 Alpaca HTTP 호출 수 = 0**.

### CODEX-043 — KIS direct submit/cancel 우회 차단

Status: **RESOLVED**

- `KISBroker.submit_order()`와 `cancel_order()`는 network 전에 single-use
  `AuthorizedExecution`을 consume한다. 토큰 없음, 수동 생성, 재사용, intent/action 불일치는
  transport 전에 차단된다.
- 신규 buy/sell authorization은 HALT를 검사한다. HALT 신규 주문 transport는 0회다.
- cancel은 HALT 중 위험 축소를 위해 허용하되 실제 open order/account/중복 cancel gate를
  통과해야 한다. 정책 없는 direct cancel transport는 0회다.
- 중앙 성공 경로는 broker transport를 한 번만 호출한다.

필수 결과:

```text
direct submit -> transport 0회
direct cancel -> 정책 없는 transport 0회
HALT 신규 주문 -> transport 0회
```

### CODEX-044 — 실제 reconciliation 및 UNKNOWN 조회

Status: **UNRESOLVED**

개선된 부분:

- 운영 주문 경로에서 `reconciliation_ok=True`, `has_unknown_orders=False` 안전 상수 주입은
  발견되지 않았다(주석만 존재).
- buy/sell gate는 `reconciliation_state.is_current_and_clean()`과 SQLite UNKNOWN query를
  읽으며 missing/stale/dirty state 자체는 fail-closed다.
- buy/sell의 KIS position/open-order read 실패는 broker call 전에 차단된다.

남은 결함:

- `kis_position_manager._reconcile_account_and_orders()`는 position mismatch만 계산한 뒤
  `record_result(clean=...)`을 먼저 호출하고, 그 다음 KIS open-orders/fills를 조회한다
  (`kis_position_manager.py:185-195`). 두 번째 조회가 실패하면 함수는 return하지만 이미 새 clean
  timestamp가 기록되어 “실제 조회 실패 -> stale/주문 0회” 계약을 위반할 수 있다.
- UNKNOWN query는 `(symbol, side)` 기준이다(`execution/idempotency.py:141-152`). 같은 계좌·종목의
  반대 side UNKNOWN은 차단하지 않는다.
- UNKNOWN reconciler는 요청수량을 받지 않고 첫 fill row의 양이 0보다 크기만 하면 `FILLED`로
  해소한다(`reconciliation/order_reconciler.py:39-56`). partial fill을 UNKNOWN에서 완전체결로
  잘못 확정해 차단을 해제할 수 있다.

따라서 실제 read failure 및 UNKNOWN 상태에서 항상 신규 주문 0회라는 필수 결과를 보장하지
못한다.

### CODEX-045 — KIS 매도 부분체결

Status: **RESOLVED (normal sell lifecycle)**

- sell adapter는 idempotency row의 `requested_quantity`와 모든 matching `ft_ccld_qty` row의
  누적합을 비교한다.
- 2주 주문 기준 0주는 open/accepted, 1주는 partially_filled, 2주는 filled로 분류된다.
- 3주 누적은 `data_integrity_error`를 반환하고 HALT를 설정한다.
- weighted average price와 cumulative quantity가 lifecycle에 전달되어 잔여 1주만 관리되며,
  1주 체결 뒤 원래 2주 전체를 다시 매도하지 않는다.

단, UNKNOWN reconciliation의 별도 부분체결 오분류는 CODEX-044/047의 미해결 사유로 기록했다.

### CODEX-046 — 초기 고위험 매도 기능 비활성화

Status: **RESOLVED**

- KIS live path의 partial profit, trailing stop, time stop, EOD exit 네 플래그는 각각 독립적이며
  환경변수 미설정 기본값이 모두 false다.
- `kis_position_manager`가 네 값을 final lifecycle decision 경계에 전달한다.
- false인 기능은 주문 의도를 만들지 않고, 하나를 true로 해도 다른 기능은 활성화되지 않는다.
- 기본 stop-loss와 target-2 full take-profit은 네 플래그와 무관하게 유지된다.

### CODEX-047 — Execution Engine 상태머신 강제

Status: **UNRESOLVED**

- new buy/sell 정상 경로는 pure `transition()` graph를 호출하고 SUBMITTING 응답 유실을 UNKNOWN으로
  전환한다. UNKNOWN->SUBMITTING과 illegal jump pure-helper 테스트도 통과한다.
- 그러나 DB persistence API `idempotency.update_status()`는 임의 status 문자열을 그대로
  UPDATE하며 현재 상태 조회, transition 검증, `WHERE status = expected_state`, rowcount 검사가
  전혀 없다(`execution/idempotency.py:166-180`). 따라서 모든 상태 변경이 상태머신을 통과한다는
  보장이 없다.
- `submit_cancel()`은 CANCEL_PENDING 전이도 기록하지 않고 broker를 먼저 호출한 뒤 CANCELLED/
  REJECTED/UNKNOWN을 직접 쓴다(`execution/execution_engine.py:170-200`).
- UNKNOWN reconciliation도 `reconcile_unknown()` 결과를 얻은 후 expected UNKNOWN 조건 없이
  직접 UPDATE한다(`kis_position_manager.py:196-201`). fill/update/cancel 경쟁으로 더 최신 DB
  상태를 덮어쓸 수 있다.
- `test_expected_state_mismatch_rejected`는 DB/CAS를 시험하지 않고 `transition("CREATED",
  "PARTIALLY_FILLED")` pure helper만 시험한다.
- 상태 전이와 별도의 durable event 기록을 한 transaction으로 저장하는 구현도 없다.

상태머신 미강제는 지시문의 명시적 `BLOCKED` 조건이다.

### CODEX-048 — Shadow Mode 완성

Status: **UNRESOLVED**

개선된 부분:

- buy cycle은 config/HALT/signal/symbol/price/cash/open-order/gate 결과 다수를 승인·거절 모두
  기록한다.
- sibling lock file의 process `flock`과 한 번의 line write가 있으며 기본 경로는 날짜별로
  rotation된다. 테스트 경로는 `tmp_path`로 격리된다.
- persist 시 structural/free-text redaction을 시도한다.

남은 결함:

- 저장소 전체에서 `shadow_mode.persist()` 호출은 `kis_live_trading.py` buy cycle에만 있다.
  `brokers/kis_broker_adapter.py`와 `kis_position_manager.py` sell 경로는 승인, 실행 예정, gate
  거절, reconciliation/UNKNOWN/HALT 등을 Shadow JSONL에 기록하지 않는다.
- 승인 레코드 하나만 broker 호출 직전에 기록되지만 별도의 “실행 예정” event/schema는 없다.
- append 후 `flush()`/`os.fsync()`가 없어 process/host crash durability를 보장하지 않는다.
- 날짜 rotation만 있고 size limit이나 보관/삭제 정책이 없다.
- `_read_file()`은 어느 위치의 malformed line이든 조용히 skip한다(`shadow_mode.py:151-164`).
  손상 사실을 audit/alert에 노출하지 않으며 테스트도 이 silent skip을 기대한다.

Shadow 누락·파일 손상 위험은 지시문의 명시적 `BLOCKED` 조건이다.

### CODEX-049 — Oracle 런북 정합성

Status: **UNRESOLVED**

- 환경변수 이름, migration 7, readonly KIS 확인, reconciliation 선행, 안전 플래그 확인, 백업,
  rollback의 큰 순서는 현재 코드와 대체로 일치한다.
- 그러나 §14는 buy와 sell 경로 모두 Shadow 결과를 기록한다고 단정한다. 실제 sell adapter/
  position manager에는 Shadow 호출이 없다.
- §15는 필수 `kis_position_manager.sync_kis_fills_and_manage_exits()` service/timer가 저장소에
  없다고 인정하고 배포자가 unit과 executable entrypoint를 새로 작성하도록 한다. 현재
  `systemd/`에는 `dashboard.service`, `order-monitor.service`만 있다. 운영자가 추가 설계 없이
  실행할 수 없다는 검증 조건을 충족하지 않는다.
- 런북의 §13 수동 reconciliation은 position만 비교하고 open-order/fill read 성공을 clean
  record의 전제조건으로 만들지 않아 CODEX-044 결함과 동일한 gap이 있다.
- 일반 취소 TR_ID와 현재가 field를 Oracle live 응답에서 여전히 재확인해야 한다.

따라서 현재 런북만으로 검증된 KIS scheduler/reconciliation-first 운영 구조를 배포할 수 없다.

### CODEX-050 — 민감정보 마스킹

Status: **UNRESOLVED**

개선된 부분:

- buy/cancel account mismatch 오류는 계좌번호 마지막 4자리만 남긴다.
- `redact_value()`는 dict/list 내부의 대소문자 혼합 App Key, App Secret, Authorization,
  access token, account number, CANO, token key를 재귀적으로 마스킹한다.
- Shadow persist가 모든 dataclass field를 structural redaction하고 rejection reason에 free-text
  redaction을 적용한다.

직접 재현된 누출:

```text
input:  Authorization: Bearer secret-token-123
output: Authorization: ***REDACTED*** secret-token-123

input:  KIS position row malformed: {'CANO': '12345678', 'qty': 'bad'}
output: KIS position row malformed: {'CANO': '12345678', 'qty': 'bad'}

input:  KIS price response missing: {'access_token': 'secret-token-123'}
output: KIS price response missing: {'access_token': 'secret-token-123'}
```

- free-text regex가 `Authorization: Bearer <token>`에서 `Bearer`만 치환하고 실제 token을 남긴다.
- single-quoted Python dict repr를 처리하지 못한다.
- `KISBroker.get_current_price()`는 raw `output!r`, `get_positions()`는 raw `row!r`를 예외에
  포함한다(`brokers/kis_broker.py:220-225`, `274-283`). KIS response dict/row가 CANO/token류
  field를 포함하면 상위 error/Shadow 경로로 원문이 전달될 수 있다.

민감정보 원문 노출 가능성은 지시문의 명시적 `BLOCKED` 조건이다.

## 5. 새로운 Finding

별도 신규 ID는 만들지 않았다. 확인된 결함은 기존 CODEX-044, 047, 048, 049, 050의 필수
조치가 완결되지 않은 구체적 잔여 범위다. CODEX-050의 Bearer-token/raw-dict 누출은 HIGH 범주의
기존 finding에 포함한다.

## 6. 운영 파일과 테스트 산출물

테스트 전후 값은 모두 동일하다.

| File | SHA-256 | Size | mtime |
|---|---|---:|---:|
| `order_history.csv` | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| `universe.csv` | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| `strategy_performance.csv` | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

- SQLite `.db-journal`, `.db-wal`, `.db-shm`: 없음
- `*shadow*.jsonl`: 없음
- 기타 새 운영 DB/CSV/log: 없음
- 보고서 수정 전 `git status --short`: clean
- 보고서 수정 전 `git diff --check`: pass

## 7. Oracle 배포 전 남은 필수 조건

1. reconciliation clean record는 position/open-order/fill 조회와 내부 비교가 모두 성공한 뒤에만
   atomic하게 기록하고, 실패 시 dirty/failure를 기록하거나 기존 record를 stale하게 유지한다.
2. UNKNOWN은 같은 계좌·종목 전체를 차단하고 요청수량 대비 누적체결을 사용해 해소한다.
3. DB status API에 transition 검증과 expected-state CAS/rowcount 검사를 넣고 cancel/
   reconciliation을 포함한 모든 상태 변경과 event 기록을 transaction으로 묶는다.
4. sell 경로를 포함한 모든 승인·차단·실행예정 Shadow event를 기록하고 fsync, retention/size
   policy, corruption alert를 구현한다.
5. KIS position-manager executable과 repository-tracked systemd service/timer를 제공하고 런북을
   실제 entrypoint와 일치시킨다.
6. Bearer token, single/double-quoted dict, nested response를 안전하게 마스킹하고 raw KIS response
   dict/row를 예외에 직접 포함하지 않는다.
7. 수정 후 동일 정·역순 전체 회귀와 직접 negative runtime test로 다시 독립 검증한다.
8. 그 전까지 `KIS_LIVE_ORDER_ENABLED=false`, `LIVE_ROLLOUT_ENABLED=false`,
   `ENTRY_DISABLED=true`, 모든 `LIVE_ENABLE_*` flag=false를 유지한다.

현재 HEAD는 merge, push, Oracle deploy 또는 실거래 활성화 대상으로 승인하지 않는다.
