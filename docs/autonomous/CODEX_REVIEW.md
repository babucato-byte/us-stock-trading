# CODEX_REVIEW — KIS 실거래 전환 재검증

## 0. 검증자 고지 (중요)

이 라운드의 검증은 **구현자와 동일한 에이전트가 수행했다.** 지시문이 요구하는 독립
검증자(Codex)가 아니다. 아래 모든 확인은 자기 코드에 대한 자기 검증이므로, 다음 한계를
명시한다.

- 구현자가 놓친 전제를 검증자도 동일하게 놓칠 수 있다. 본 문서의 PASS 판정은
  "독립 검증 통과"가 아니라 "구현자 자체 재현 결과"로 읽어야 한다.
- 이를 보정하기 위해 저장소의 기존 테스트에 의존하지 않는 **독립 probe 스크립트**를
  저장소 밖(`/private/tmp`)에서 작성해 실행했다. 각 probe는 먼저 "패치하지 않은 대조군이
  실제로 주문에 도달하는가"를 확인한 뒤 결함을 주입하는 구조로, 통과가 우연이 아님을
  스스로 증명하도록 만들었다.
- 실거래 활성화 판단 전에는 **반드시 별도 독립 검증자의 재검증이 필요하다.**

## 1. 검증 대상

- 저장소: `us-stock-trading`
- 브랜치: `feature/kis-live-broker`
- 검증 HEAD: `6beb1c917afb57dae4f81c4dc478e58d791ce6a8`
- 지시된 대상 커밋과 일치 (TARGET_COMMIT_MISMATCH 아님)
- 검증 시작 시 working tree: clean
- `git diff --check`: pass

주요 대상: CODEX-048, CODEX-049, CODEX-051
회귀 대상: CODEX-042, 043, 044, 045, 046, 047, 050

검증 중 코드·테스트·설정·서비스·스크립트는 수정하지 않았다. 커밋, push, merge, 배포,
실주문, 플래그 변경, 테스트 완화/skip/xfail 추가도 하지 않았다. 변경한 파일은 이 문서뿐이다.

## 2. 최종 판정

Verdict: **PASS_WITH_CONDITIONS**

Oracle deployment: **ALLOWED_FOR_READ_ONLY_SHADOW_STAGE** (아래 조건부)

Live trading: **DO_NOT_ENABLE**

CODEX-048, CODEX-049, CODEX-051은 모두 해결됐다. 기존 해결 Finding의 회귀는 없다.
신규 CRITICAL 0건, 신규 HIGH 0건이다.

조건:

1. 이 판정은 **자기 검증**이다(§0). 실주문 활성화 전 독립 검증자의 재검증이 선행되어야 한다.
2. Oracle read-only 단계에서 KIS 실응답으로 현재가 field(`output.last`)와 일반 취소 TR_ID
   (`TTTT1004U`/`VTTT1004U`)를 반드시 확인한다(§9의 잔여 MEDIUM).
3. 신규 LOW 2건(CODEX-052, CODEX-053)은 Oracle read-only 단계 진행을 막지 않으나,
   실주문 활성화 전에 정리한다.

## 3. 테스트 재현

구현자 주장과 동일한 수치를 독립적으로 재현했다.

### 수집 개수

```text
venv/bin/python -m pytest --collect-only -q
2053 tests collected
```

### 정방향 전체 (외부 네트워크 차단 plugin 적용)

```text
PYTHONPATH=/private/tmp/.../netguard venv/bin/python -m pytest -q -p netguard
2053 passed, 0 failed, 0 skipped, 0 xfailed, 2 warnings in 62.11s
NETGUARD: 0 outbound connection attempt(s): []
```

### 역방향 전체 (동일 plugin 적용)

```text
venv/bin/python -m pytest -q -p netguard $(ls tests/test_*.py | sort -r)
2053 passed, 0 failed, 0 skipped, 0 xfailed, 2 warnings in 62.14s
NETGUARD: 0 outbound connection attempt(s): []
```

`netguard`는 저장소 **밖**에 둔 pytest plugin으로, `socket.socket.connect`,
`connect_ex`, `socket.create_connection`을 감싸 loopback 이외 주소로의 연결 시도를 전부
예외로 만들고 그 횟수를 보고한다. 저장소의 conftest나 테스트 fake에 의존하지 않는 독립
증거이며, 정·역방향 모두 시도 0건이었다.

### 집중 안전 테스트

구현자 보고(679)보다 넓은 범위로 실행했다.

```text
30개 파일 (shadow audit/durability/coverage, shadow exit, oracle deploy, redaction,
CAS, reconciliation, order gate, execution engine, KIS broker/adapter/negative,
position lifecycle, exit flags, broker safety, crash recovery, kill-switch gate,
alpaca operational path)

716 passed, 0 failed, 0 skipped, 0 xfailed, 1 warning in 24.71s
```

경고 2건은 기존과 동일하다(LibreSSL urllib3 경고, 의도적 scanner field skip 경고).

## 4. CODEX-048 — Shadow 감사 순서·내구성·Fail-closed

Status: **RESOLVED**

### 4.1 transport 이전 durable commit (핵심 결함)

이전 라운드의 결함은 `GATE_APPROVED`/`EXECUTION_PLANNED`가
`execution_engine.submit_*_order()` **반환 후**에 기록된 것이었다. 현재 두 이벤트는
`execution/execution_engine.py`가 직접, transport 호출 전에 기록한다.

독립 probe: fake broker의 `submit_order()` 내부에서 **별도 sqlite3 connection**을 열어
그 시점에 커밋된 행만 조회했다.

매도 경로 (probe_048.py):

```text
[PASS] SELL: GATE_APPROVED committed before transport
       -- ['GATE_APPROVED', 'EXECUTION_PLANNED']
[PASS] SELL: EXECUTION_PLANNED committed before transport
[PASS] SELL: EXECUTION_PLANNED is last before transport
```

매수 경로는 저장소 테스트(`tests/test_shadow_audit_durability.py`)가 동일 기법으로 검증하며,
`_audit_before_transport()`를 무력화하면 해당 3건이 실패하는 것을 직접 확인했다(테스트가
실제로 이 속성에 결합되어 있음을 증명).

같은 connection의 uncommitted 상태나 반환 후 기록은 인정하지 않았다. probe는 항상 새
connection으로 조회한다.

### 4.2 감사 저장 실패 → transport 0회

probe_048b.py는 케이스마다 **완전히 새로운 임시 상태**를 만들고, 먼저 무패치 대조군이
실제로 transport에 도달하는지 확인한 뒤 결함을 주입한다.

```text
[PASS] 0. unpatched control order reaches transport -- exc=NONE calls=1

[PASS] B. audit COMMIT failure blocks with AUDIT_PERSISTENCE
[PASS] B. audit COMMIT failure -> transport 0 calls
[PASS] C. busy-retry exhausted blocks with AUDIT_PERSISTENCE
[PASS] C. busy-retry exhausted -> transport 0 calls
[PASS] D. missing shadow_audit_events table blocks the order
[PASS] D. missing audit table -> transport 0 calls
```

insert 실패는 저장소 테스트가 별도로 검증한다. commit 실패, SQLITE_BUSY 재시도 소진,
migration 9 누락(테이블 삭제)까지 모두 `ExecutionEngineError(reason_code=
"AUDIT_PERSISTENCE")`로 차단되고 transport 호출은 0회다.

주의: 첫 probe(probe_048.py)의 B/C/D는 직전 케이스가 남긴 주문 때문에 reconciliation이
먼저 차단해 `RECONCILIATION_DIRTY`가 나왔다. probe 자체의 상태 격리 결함이었고, 격리 후
전부 `AUDIT_PERSISTENCE`로 재현됐다. 어느 쪽이든 transport는 0회였다.

`try/except: pass` 패턴은 `kis_live_trading.py`, `brokers/kis_broker_adapter.py`,
`execution/execution_engine.py`, `shadow_audit.py`에 없다(저장소 AST 테스트 + 육안 확인).
`shadow_audit.handle_audit_failure()`가 SHADOW_ERROR 재시도, `operations/alerts.py` 알림,
`ShadowAuditFailure` 발생으로 평가를 종료한다.

### 4.3 종료 이벤트 정확히 1건

probe_terminal.py가 실제 파이프라인으로 7개 run을 만들었다(매수 승인, 매수 차단,
매수 예외, 사이클 HALT 차단, 매도 승인, 매도 차단, 매도 reconciliation 차단).

```text
[PASS] terminal event exactly once for all 7 runs -- zero=[] multi=[]
[PASS] all three terminal kinds observed
       -- {'SHADOW_COMPLETED': 1, 'SHADOW_BLOCKED': 5, 'SHADOW_ERROR': 1}
[PASS] audit_integrity_report agrees
       -- {'runs_without_terminal_event': [], 'runs_with_multiple_terminal_events': [],
           'total_runs': 7}
[PASS] both sides recorded -- {'buy', 'sell'}
```

`SHADOW_COMPLETED`/`SHADOW_BLOCKED`/`SHADOW_ERROR` 세 종류가 모두 실제로 관측됐고,
0건·2건 이상은 없다. `audit_integrity_report()`가 양쪽 위반을 모두 조회 가능하게 한다.

### 4.4 매수·매도 양쪽

매도 경로가 SQLite 감사 저장소를 우회하지 않는다. probe에서 `side` 값 집합이
`{'buy', 'sell'}`로 확인됐고, 매도 손절/익절/일반매도 판정은 §5의 shadow-exit 서비스가
동일 감사 lifecycle로 기록한다. reconciliation 차단, UNKNOWN 차단, HALT 차단 각각에 대한
전용 이벤트가 저장소 테스트(`tests/test_shadow_audit_coverage.py`)와 probe 양쪽에서
확인됐다.

### 4.5 SQLite 내구성·동시성

`shadow_audit._insert_once()`는 명시적 `BEGIN IMMEDIATE` → INSERT → `rowcount`/`lastrowid`
확인 → `commit()` → **commit 후 재조회**로 durable 여부를 확인한다. SQLITE_BUSY는 지수
backoff로 5회 재시도 후 `ShadowAuditError`를 발생시키며 조용히 버리지 않는다(§4.2 케이스 C).

새 connection 재조회, migration 9 적용, 12개 프로세스 동시 insert(총 72건, 누락 0),
JSONL 12 프로세스 동시 append(총 120줄, 전 줄 파싱 가능)는 저장소 테스트가 검증하며 이번
전체 실행에서 통과했다.

### 4.6 JSONL 보조 경로

`flock` 프로세스 간 잠금, `O_APPEND`(`open(..., "a")`), `flush()`, `os.fsync()`,
잠금 내부 rotation, 크기 기반 rotation, 보관 기간, 손상 라인 탐지, 손상 시 운영 알림
(`verify_log_integrity()`)과 엄격 reader(`read_all_strict()`)가 모두 존재하고 테스트로
검증된다. fsync 호출 자체를 monkeypatch로 관측하는 테스트가 있다.

### 4.7 민감정보

probe_terminal.py가 SQLite 감사 행과 JSONL 파일 전체를 합쳐 검색한 결과 `Bearer `,
`appkey=`, `app_secret`, `CANO':`, `"CANO"` 원문 0건이다. 저장소의
`tests/test_secret_leak_sweep.py`(24건)도 통과한다.

### 4.8 관찰 사항 (차단 아님)

- `submit_buy_order`/`submit_sell_order`의 `audit_run_id` 기본값이 `None`이며, `None`이면
  `_audit_before_transport()`가 조용히 반환한다. 현재 두 호출자는 모두 값을 전달하고
  테스트가 이를 고정하지만, 향후 호출자가 누락하면 승인 감사 없이 주문이 진행된다.
  → CODEX-053 (LOW)로 기록.
- cancel 경로에는 shadow_audit run이 없다. 다만 `order_state_events`에 `CANCEL_PENDING`이
  transport **이전**에 durable하게 기록되는 것을 probe로 확인했다
  (`['CREATED','VALIDATING','APPROVED','SUBMITTING','ACCEPTED','CANCEL_PENDING']`).
  지시문의 CODEX-048 대상 목록에 cancel은 없으므로 결함으로 판정하지 않는다.

## 5. CODEX-049 — Oracle Shadow 매도 평가 배포

Status: **RESOLVED**

### 5.1 Shadow / Live 분리와 공통 판단 로직

`positions/lifecycle.py::decide_exit()`가 순수 판단 함수로 존재하고,
`check_and_manage()`가 그 결과로 분기한다. Shadow 전용 별도 매도 판단 구현은 없다.

probe_049.py는 **모든 실주문 플래그를 true로 켠 상태**에서 두 Shadow 서비스를, 주문
메서드가 호출되면 예외를 던지는 broker에 대해 실행했다.

```text
[PASS] shadow ENTRY places no order with ALL live flags on
[PASS] shadow ENTRY still produced a verdict -- hypothetical=WOULD_APPROVE
[PASS] shadow EXIT places no order with ALL live flags on
[PASS] shadow EXIT still reached a FULL_EXIT verdict (stop breached)
       -- decision=full_exit reason=STOP_LOSS
[PASS] shadow EXIT mutated no position state -- STOP_ACTIVE->STOP_ACTIVE
[PASS] the SAME decision does order on the live path (shadow/live agree)
       -- [('AAPL', 10, 'sell')]
```

환경변수만으로는 우회되지 않는다. 주문 불가능성은 플래그가 아니라 구조에서 온다:
두 Shadow 진입점 모두 `execution.execution_engine`, `brokers.kis_broker_adapter`,
`kis_position_manager`를 import하지 않으며 `check_and_manage()`를 호출하지 않는다(AST 및
호출 문자열 스캔 테스트 존재). 마지막 항목은 같은 판단이 live 경로에서는 실제로 주문을
낸다는 것, 즉 Shadow 판정이 무의미한 no-op이 아님을 보인다.

### 5.2 진입점

```text
scripts/install_oracle_services.sh
scripts/preflight_kis_live.py
scripts/run_health_report.py
scripts/run_live_buy_entry.py
scripts/run_migrations.py
scripts/run_reconciliation.py
scripts/run_shadow_exit_evaluation.py
scripts/run_shadow_mode.py
```

7개 Python 진입점 전부 `--help` 성공(exit 0, usage 출력 확인), import 성공, 실행 가능
비트 설정됨.

### 5.3 unit / timer

```text
deploy/systemd/us-stock-trading-migrate.service
deploy/systemd/us-stock-trading-reconcile.service   + .timer
deploy/systemd/us-stock-trading-shadow.service      + .timer
deploy/systemd/us-stock-trading-shadow-exit.service + .timer
deploy/systemd/us-stock-trading-health.service      + .timer
deploy/systemd/us-stock-trading-live.service        (timer 없음)
```

macOS라 `systemd-analyze`를 쓸 수 없어 정적 parser를 직접 작성해 검증했다(섹션 유효성,
키 유효성, key=value 형식). 10개 unit 전부 통과.

각 service unit에서 실제 값 대조 결과 전부 일치:

```text
User=ubuntu                 Group=trading
WorkingDirectory=/home/ubuntu/trading-release
EnvironmentFile=/etc/us-stock-trading/live-readonly.env
Restart=on-failure          RestartSec=10
TimeoutStartSec=300         UMask=0027
NoNewPrivileges=true        PrivateTmp=true
ProtectSystem=full          ProtectHome=false (존재)
ReadWritePaths=/home/ubuntu/trading-release /var/log/us-stock-trading
```

모든 `ExecStart`/`ExecStartPre`의 `.py` 대상이 `scripts/`에 실재한다.

의존 순서는 런북 설명이 아니라 unit 지시자로 강제된다.

```text
migrate 이외 전 unit: Requires/After = us-stock-trading-migrate.service
shadow, shadow-exit, live: Requires/After 에 us-stock-trading-reconcile.service 추가
migrate/health 이외 전 unit: ExecStartPre = preflight_kis_live.py
```

`Requires=`가 곧 "선행 unit 실패 시 시작 안 함"이므로 reconciliation 실패는 shadow 시작을
차단한다. `ConditionPathExists`가 인터프리터, 환경파일, 자기 진입점을 각각 가드한다.
timer 4개는 각각 실재하는 service를 가리키고, live.service를 가리키는 timer는 없다.

preflight 예외 2건은 의도적이며 타당하다: migrate는 preflight가 검사하는 스키마 자체를
만들므로 순환이고, health는 문제가 있을 때야말로 실행돼야 한다.

### 5.4 설치 스크립트

가짜 release 트리에 `DRY_RUN=1`로 실행해 확인했다.

```text
KIS_LIVE_ORDER_ENABLED=true  -> ERROR ... only deploys the read-only posture (설치 거부)
LIVE_ROLLOUT_ENABLED=true    -> ERROR ... only deploys the read-only posture (설치 거부)
ENTRY_DISABLED=false         -> ERROR ... does not set ENTRY_DISABLED=true (설치 거부)
```

정상 read-only 환경파일에서의 실행 계획:

```text
systemctl daemon-reload
run_migrations.py
preflight_kis_live.py
systemctl enable us-stock-trading-migrate.service
systemctl enable --now us-stock-trading-reconcile.timer
systemctl enable --now us-stock-trading-shadow.timer
systemctl enable --now us-stock-trading-shadow-exit.timer
systemctl enable --now us-stock-trading-health.timer
systemctl disable us-stock-trading-live.service
systemctl stop    us-stock-trading-live.service
```

live service의 enable/start는 0건이다. migration과 preflight가 enable보다 먼저 실행된다.
스크립트 어디에도 `KIS_LIVE_ORDER_ENABLED=true` / `LIVE_ROLLOUT_ENABLED=true` /
`ENTRY_DISABLED=false` 대입이 없다.

### 5.5 런북 정합성

런북이 언급하는 `scripts/`·`deploy/` 경로 18개 전부 실재하고, 언급된 unit 10개 전부
파일이 존재한다. 존재하지 않는 모듈·unit·timer·명령은 없다.

단계 순서(백업 → release → venv → 환경파일 → migration → preflight → reconciliation →
shadow entry → shadow exit → systemd 설치 → timer 확인 → 로그 확인 → live disabled 확인 →
롤백)가 문서에 순서대로 존재한다.

## 6. CODEX-051 — Full SHA exact match

Status: **RESOLVED**

직접 실행한 negative/positive 케이스 전부 기대와 일치했다.

```text
[PASS] 1-char prefix                        -> rejected   (이전 라운드에서 통과하던 값)
[PASS] 7-char short SHA                     -> rejected
[PASS] 39 chars                             -> rejected
[PASS] 41 chars                             -> rejected
[PASS] uppercase SHA                        -> rejected
[PASS] leading whitespace                   -> rejected
[PASS] trailing whitespace                  -> rejected
[PASS] HEAD literal                         -> rejected
[PASS] refs/heads/main                      -> rejected
[PASS] empty string                         -> rejected
[PASS] None                                 -> rejected
[PASS] nonexistent well-formed SHA          -> rejected
[PASS] validated==deployed but != real HEAD -> rejected
[PASS] validated != deployed                -> rejected
[PASS] full correct SHA                     -> ACCEPTED
```

- 허용 형식은 `^[0-9a-f]{40}$`이며 대문자는 정규화하지 않고 거부한다.
- 비교 대상 3값(`git rev-parse HEAD`, `VALIDATED_COMMIT`, `DEPLOYED_COMMIT`)이 모두 문자열
  동일성으로 대조된다. 환경변수끼리만 비교하지 않는다.
- 실제 commit object 존재를 `git rev-parse --verify --quiet <sha>^{commit}`로 확인한다.
  함수 단위로 직접 호출해 확인했다: 실제 HEAD → True, `0*39+1` → False.
- 실행 코드의 `startswith` 비교는 AST 기준 0건이다(`ast.Attribute.attr == "startswith"`
  탐색). 남은 1건은 결함을 설명하는 주석이다.

## 7. 기존 Finding 회귀

| Finding | 결과 | 근거 |
|---|---|---|
| CODEX-042 | 회귀 없음 | Alpaca direct 주문 차단 테스트 통과 (`test_alpaca_operational_path_disabled`, `test_broker_kill_switch_gate`) |
| CODEX-043 | 회귀 없음 | KIS direct submit/cancel 및 HALT 신규 주문 transport 0회 (`test_kis_broker`, `test_kis_negative_suite`, `test_execution_engine_kis`) |
| CODEX-044 | 회귀 없음 | KIS 조회 실패·mismatch·UNKNOWN account-wide·snapshot TTL/계좌/종목 검증 전부 통과. 운영 코드의 상수 주입 0건(검색 결과는 과거 구현을 설명하는 주석뿐) |
| CODEX-045 | 회귀 없음 | 부분체결 분류 테스트 통과 (`test_kis_broker_adapter`, `test_reconciliation`) |
| CODEX-046 | 회귀 없음 | 네 exit 플래그 기본 false, `test_live_exit_flags` 통과. probe에서 플래그를 켰을 때만 PARTIAL_EXIT 판정이 나오는 것도 확인 |
| CODEX-047 | 회귀 없음 | `UPDATE kis_order_idempotency`는 `execution/order_repository.py`에만 존재(4건, 전부 CAS). `update_status(` 호출 0건(잔존 문자열은 전부 주석/문서). cancel의 CANCEL_PENDING이 transport 이전 durable 기록됨을 probe로 확인 |
| CODEX-050 | 회귀 없음 | `{output!r}`/`{row!r}` 류 raw repr 보간 0건. secret sweep 24건 통과. probe에서 두 저장소 합산 원문 0건 |

비-테스트 코드의 `broker.submit_order(` 호출부는 `paper_strategy_order.py`(legacy 유예),
`live_readiness/execution_engine.py`(Alpaca 엔진), `execution/execution_engine.py`(KIS 엔진)
세 곳뿐으로, 기존 allow-list와 동일하다. 신규 호출부는 없다.

## 8. 신규 Finding

### CODEX-052 — LOW — `brokers/kis_broker.py`의 TBD 주석이 모듈 docstring과 모순

Status: **OPEN**

모듈 docstring(9~35행)은 공식 reference repo와 대조해 다음을 **확인 완료**했다고 기술한다.

- 일반 취소 TR_ID 쌍 = `TTTT1004U` / `VTTT1004U`
- 현재가 field `output.last` (chk_price.py의 필드 주석으로 독립 확인)

그러나 아래 두 주석이 아직 남아 정반대로 기술한다.

```text
brokers/kis_broker.py:63-68
  "TBD_VERIFY_LIVE_DOCS: general cancel path/TR_ID -- only the *daytime*
   variant ... was directly confirmed"

brokers/kis_broker.py:215-217
  "TBD_VERIFY_LIVE_DOCS: response field name -- `last` ... was not directly
   confirmed"
```

코드 값 자체는 docstring의 확인 내용과 일치하므로 **동작 결함은 아니다.** 문서 모순이며,
Oracle 단계 운영자가 "무엇을 아직 확인해야 하는가"를 잘못 판단할 수 있다. 안전 방향으로
기운 오류(실제보다 덜 확인된 것으로 읽힘)이므로 LOW로 분류한다. 실주문 활성화 전에
주석을 실제 상태에 맞추거나, 반대로 docstring의 확인 주장을 근거와 함께 재확인해야 한다.

### CODEX-053 — LOW — `audit_run_id` 누락 시 승인 감사가 조용히 생략됨

Status: **OPEN**

`execution_engine.submit_buy_order()`/`submit_sell_order()`의 `audit_run_id` 기본값이
`None`이고, `_audit_before_transport()`는 `None`이면 아무 것도 기록하지 않고 반환한다.
현재 호출자(`kis_live_trading.py`, `brokers/kis_broker_adapter.py`)는 둘 다 값을 전달하며
테스트가 이를 고정하고 있어 현 시점 결함은 아니다. 다만 CODEX-048의 핵심 보호가
"호출자가 인자를 잊지 않는 것"에 의존하는 구조이므로, 향후 신규 호출자가 생길 때
fail-open이 된다. 실주문 활성화 전에 필수 인자로 승격하거나, `None`을 명시적 오류로
바꾸는 것이 바람직하다.

신규 CRITICAL/HIGH는 발견하지 않았다.

## 9. 잔여 MEDIUM

```text
KIS 현재가 응답 field (output.last)
일반 취소 TR_ID (TTTT1004U / VTTT1004U)
```

공식 reference repo 기준으로는 확인됐으나, **KIS 실서버 응답으로는 아직 확인되지 않았다.**
이 저장소에서 네트워크 없이 검증할 수 없으므로 분류는 다음과 같다.

```text
CRITICAL/HIGH 아님
코드 재검증 차단 아님
Oracle live-readonly 단계 필수 조건
실주문 활성화 전 반드시 확인
```

공식 KIS 문서와 현재 구현의 명백한 충돌은 발견하지 않았으므로 HIGH로 승격하지 않는다.

## 10. 운영 파일

검증 전후 SHA-256, size, mtime 모두 동일하다(diff 결과 완전 일치).

| File | SHA-256 | Size | mtime |
|---|---|---:|---:|
| `order_history.csv` | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| `universe.csv` | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| `strategy_performance.csv` | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

## 11. Stray artifact

검증 후 저장소 운영 경로에 다음이 남지 않았다.

```text
*.db  *.db-wal  *.db-shm  *.db-journal
shadow-*.jsonl  SHADOW_MODE_LOG.jsonl  RECONCILIATION_STATE.json
logs/  임시 env  임시 systemd 출력  임시 lock
```

probe 스크립트와 가짜 release 트리는 전부 `/private/tmp` 아래에서만 생성·삭제했다.

## 12. 네트워크

```text
Alpaca HTTP        0
KIS HTTP           0
Slack 실제 호출     0
기타 외부 네트워크   0
```

저장소 밖 `netguard` plugin으로 정·역방향 전체 실행에서 loopback 이외 연결 시도 0건을
독립 확인했다. 모든 broker 상호작용은 주입된 fake/read-only double을 통해서만 이뤄졌다.

## 13. Oracle 단계 허용 여부

read-only Shadow 단계 진행을 **허용**한다. 단, §2의 세 조건을 전제로 한다.

실주문 활성화는 **허용하지 않는다.** 다음 세 가지가 선행되어야 한다.

1. 독립 검증자(본 문서 §0의 자기 검증이 아닌)의 재검증
2. Oracle read-only 단계에서 KIS 실응답으로 현재가 field·취소 TR_ID 확인
3. CODEX-052, CODEX-053 정리

## 14. 최종 상태 확인

```text
git status --short        -> 이 문서 외 변경 없음
git diff --check          -> pass
검증 중 수정한 파일       -> docs/autonomous/CODEX_REVIEW.md 뿐
커밋/push/merge/배포/실주문 -> 수행하지 않음
```
