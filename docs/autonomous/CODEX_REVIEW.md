# CODEX_REVIEW — CODEX-059·060 최종 독립 재검증

## 1. 검증 대상 및 독립성

구현자 완료 보고와 기존 테스트 결과를 재사용하지 않고, 실제 코드·subprocess·다중 프로세스 lock
동작·Git 상태를 새로 확인했다. 저장소 밖 `/private/tmp`에 독립 probe를 작성해 실행하고 검증 후
삭제했다.

검증 시작 시 실행 결과:

```text
$ git status --short
(no output — clean)

$ git branch --show-current
feature/kis-live-broker

$ git rev-parse HEAD
7309a8313adf7801b47a1c6486703ef81fa3efb9

$ git show --stat --oneline HEAD
7309a83 Clean up preflight single-run lock lifecycle
 execution/idempotency.py                | 159 +++++++++++--
 tests/test_oracle_deploy_package.py     |   6 +
 tests/test_single_run_lock_lifecycle.py | 380 ++++++++++++++++++++++++++++++++
 3 files changed, 527 insertions(+), 18 deletions(-)

$ git diff --check
(no output — pass)
```

branch, exact HEAD, clean working tree, diff check 조건이 모두 일치했다.
`TARGET_COMMIT_MISMATCH` 아님.

독립 probe 결과 합계: **78/78 PASS** (섹션별 2:14, 3:13, 4:14, 5:6, 6:12, 7:5, 8:14).
각 fault 시나리오 앞에 무장애 대조군을 두어, 결과가 "코드에 도달하지 못해서 통과한 것"이
아님을 먼저 확인했다.

---

## 2. 최종 판정

```text
PASS_WITH_CONDITIONS
```

코드 Finding 기준으로는 CRITICAL / HIGH / MEDIUM 모두 0이고 CODEX-059·060은 해결됐다. 다만
남은 항목이 **실제 KIS 응답 확인**뿐이므로 §16의 `PASS` 조건 중 "기존 Finding 회귀 없음 · 신규
CRITICAL/HIGH 0"은 충족하나, 9개 wire 값이 `LIVE_RESPONSE_PENDING` 상태로 남아 있어
`PASS_WITH_CONDITIONS`로 판정한다. 상세는 §11.

`BLOCKED` 조건은 하나도 해당하지 않는다.

```text
FatalRepositoryConnectionError 강등            없음
entrypoint exit code 4 우회                    없음
정상/예외 종료 후 lock 잔존                    없음
blocked process가 다른 process lock 삭제       없음
inode 경쟁으로 두 process 동시 진입            없음
stale lock이 다음 실행 차단                    없음
preflight가 저장소 루트 artifact 생성          없음
신규 CRITICAL/HIGH                             없음
```

---

## 3. CODEX-059 회귀 — 없음

fatal 주입이 아니라 **실제 취소 final-state 경로**를 통과시켰다. 별도 child process에서 실제
주문을 넣고 실제 취소를 수행하되, `CANCELLED` 저장 시점부터 commit·rollback·close가 모두
실패하도록 했다.

대조군(무장애, 같은 child):

```text
rc=0  NO_FATAL cycles=1   (실제 취소가 CANCELLED까지 durable하게 완료)
```

fault 주입:

```text
process exit code            = 4          (0 아님)
caller-visible 예외 타입     = FatalRepositoryConnectionError
CancelPostTransportError 강등 = 없음
WRONG_TYPE                   = 없음
후속 cycle                   = 0회 (cycles=1)
HALT                         = True
CRITICAL alert               = 발생
```

프로세스 종료 후 DB write lock 회수:

```text
새 writer의 BEGIN IMMEDIATE + UPDATE + commit = 성공
미커밋 CANCELLED의 durable 여부 = 아님 (status=CANCEL_PENDING 유지)
```

5개 entrypoint 직접 주입 대조:

```text
run_live_buy_entry.main()        = 4
run_reconciliation.main()        = 4
run_shadow_mode.main()           = 4
run_shadow_exit_evaluation.main()= 4
run_health_report.main()         = 4
```

취소 final-state persistence, UNKNOWN fallback, execution engine broad catch, `finally` 안전망,
5개 entrypoint 모두에서 fatal 타입이 보존됐다. CODEX-059 회귀 없음.

---

## 4. CODEX-060 lock 경로 해석

```text
미설정                     → 기존 운영 기본 경로 (<repo>/KIS_ORDER_IDEMPOTENCY.lock)
"" / "   " / "\t"          → 기존 운영 기본 경로
절대 경로                  → 해당 경로 사용
상대 경로                  → 현재 cwd 기준 resolve
부모 디렉터리 없음         → 생성
부모가 디렉터리가 아님     → IdempotencyError (명시적 실패)
```

상대 경로가 cwd에 종속됨을 직접 확인했다. 동일 값 `rel/run.lock`으로 cwd를 바꿔 해석시켰다.

```text
cwd=<repo>  → <repo>/rel/run.lock
cwd=<tmp>   → <tmp>/rel/run.lock
두 값 서로 다름 = 확인
```

경로 해석이 **호출 시점**에 일어나는 것도 확인했다. 같은 프로세스에서 환경변수를 바꾸면
`get_single_run_lock_file()` 반환값이 즉시 달라진다. import 시점 고정 아님.

secret 노출: 이 lock 코드가 읽는 환경변수는 `TRADING_SINGLE_RUN_LOCK_FILE` 하나뿐이고
(`os.environ` 참조 1회), 값은 파일 경로로만 사용된다. credential, 계좌번호, payload는 관여하지
않는다.

cwd 종속성은 `execution/idempotency.py`의 `get_single_run_lock_file()` docstring에 명시돼 있다
(§3의 "런북 또는 코드 문서" 조건 충족). 다만 런북에는 이 환경변수가 전혀 언급돼 있지 않다 —
§10의 LOW-2 참조.

---

## 5. 정상·예외 종료 cleanup

각 경로를 실제로 실행했다.

```text
                    보유 중 파일 존재   종료 후 파일   예외 타입   메시지
정상 종료                  O                없음          -          -
RuntimeError               O                없음        보존       보존
KeyboardInterrupt          O                없음        보존       보존
SystemExit                 O                없음        보존       보존
preflight 성공             O                없음          -          -
preflight 실패             O                없음          -          -
```

예외는 다른 타입으로 교체되지 않았고 원문 메시지도 그대로였다. 대조군으로 "보유 중에는 파일이
실제로 존재한다"를 먼저 확인했으므로, cleanup 통과가 "애초에 파일이 안 만들어져서"가 아니다.

저장소 루트 artifact: 모든 경우 0건.

---

## 6. Lock 소유권과 inode 검증

획득 실패 process의 삭제 금지:

```text
process A가 flock 보유
process B 획득 시도 (timeout 0.2s)
→ B = BLOCKED
→ B가 lock path 삭제하지 않음
→ A의 inode 유지 (변경 없음)
```

획득한 process의 identity 조건부 삭제:

```text
보유 중 path가 다른 inode로 교체됨
→ 삭제 0회
→ 교체된 파일 보존 (해당 inode 그대로)
→ 경고 발생:
  "single-run lock at ... was replaced by a different file while held
   (expected inode N, found M) -- leaving it alone"
```

코드상으로도 `_unlink_owned_lock()`이 `os.stat(path)`의 (dev, inode)와 획득 시
`os.fstat(handle)`로 잡은 identity가 일치할 때만 `os.unlink`한다. 실패 획득 경로는
`_acquire_owned_lock()` 안에서 예외가 나가므로 unlink가 있는 `finally`에 도달하지 않는다.

---

## 7. unlink 순서와 경쟁 조건

구현 순서는 보고된 대로다.

```text
flock 보유 → path unlink → flock 해제 → descriptor close
```

그리고 `_acquire_owned_lock()`은 flock 획득 **후** `fstat` identity와 `stat(path)` identity를
재비교해, 불일치면 기존 descriptor를 버리고 처음부터 다시 획득한다.

지시서 §3이 제시한 순서(해제 → close → unlink)와 다르다. 검증자 판단으로는 §3 순서가 §4가
막으라고 한 경쟁을 오히려 만든다.

```text
A 해제 → B가 같은 inode 획득하고 임계구역 진입
→ A가 뒤늦게 unlink → C가 새 파일 생성·획득 → B와 C 동시 진입
```

현재 구현은 unlink를 보유 중에 하므로, 해당 inode에서 깨어난 waiter는 identity 재확인에 실패해
재시작한다. **채택된 순서가 더 안전하다고 판정한다.**

이 주장을 단위 테스트가 아니라 실제 다중 프로세스로 검증했다.

대조군 및 기본 배타성:

```text
무경합 taker 1개          → ACQUIRED, 종료 후 파일 없음
A 보유 중 B·C·D·E 동시    → 4개 전부 BLOCKED
                          → 누구도 A의 파일 삭제하지 않음
                          → A의 inode 유지
A 해제                    → A가 자기 파일 제거
이후 taker                → ACQUIRED (영구 lock 없음)
```

동시 진입 탐지 stress (핵심):

```text
6개 독립 process × 25회 반복 = 150회 임계구역 진입
각 process가 임계구역 진입/이탈 시 공유 journal에 O_APPEND 기록
기록 총량 = 300줄 (대조군: stress가 실제로 수행됨)

중첩 진입(E-E 연속) 탐지 = 0건
mismatched exit 탐지      = 0건
종료 후 lock 파일         = 없음
```

임계구역 동시 진입 최대 1 process, lock 파일 오삭제 0건, 영구 lock 0건.

---

## 8. Stale lock 복구 (실제 SIGKILL)

```text
child A: flock 획득 → "HELD" → SIGKILL
  (finally 미실행, LOCK_UN 없음, unlink 없음)

A 생존 중 획득 시도  = BLOCKED          (대조군: 살아 있는 보유자는 실제로 막는다)
A 사망 후 파일 상태  = 존재 (stale)
다음 실행            = 획득 성공, stale inode를 그 자리에서 재사용
종료 후              = 파일 정리됨
```

파일 존재 자체는 실행을 차단하지 않으며, 실제 배타성 기준은 OS flock이다. stale path 자동 회수,
종료 후 artifact 0건.

---

## 9. Preflight 실제 subprocess 재현

`scripts/preflight_kis_live.py`를 cwd=저장소 루트로 실제 child process 실행했다.

격리 환경변수 있음:

```text
성공: exit 0, "single_run_lock: no other instance holds the single-run lock" 출력(대조군)
      tmp lock 종료 후 없음, 저장소 루트 lock 없음
실패: exit 1, "PREFLIGHT FAILED"
      tmp lock 종료 후 없음, 저장소 루트 lock 없음
```

격리 환경변수 **없음** (기존 결함의 원래 조건):

```text
성공 경로: single_run_lock 검사 실제 수행 → 저장소 루트 lock 없음
실패 경로: PREFLIGHT FAILED → 저장소 루트 lock 없음
```

즉 테스트 cwd 변경이나 환경변수 격리로 결함을 숨긴 것이 아니라, 기본 운영 경로를 쓰는
원래 조건에서도 lock 파일이 남지 않는다. 수정의 본체가 lock lifecycle 자체임을 확인했다.

원 결함 경로 전체 재현:

```text
tests/test_oracle_deploy_package.py 단독 실행 = 155 passed
저장소 루트 lock artifact = 0건
```

---

## 10. Lock API 정적 검증 및 다른 flock context

정적 확인 결과:

```text
import 시점 환경변수 고정   없음 (DEFAULT_LOCK_FILE만 상수, 해석은 호출 시)
호출 시점 경로 해석         있음 (single_run_lock 진입 시 get_single_run_lock_file())
획득 실패 process cleanup   없음 (예외가 finally 진입 전에 나감)
inode identity 검증         있음 (fstat vs stat, (st_dev, st_ino))
waiter 획득 후 identity 재확인 있음 (불일치 시 재획득 루프)
```

`single_run_lock` 의미(프로세스 단일 실행 잠금)를 갖는 것은 `execution/idempotency.py` 하나뿐이다.

### LOW-1 — 다른 9개 flock context (범위 밖, 회귀 아님)

```text
scalping_watchlist/atomic_io.py      positions/store.py
kill_switch_state.py                 notification_health.py
order_intent_ledger.py               paper_strategy_order.py
strategy_sources/repository.py       shadow_mode.py
live_readiness/entry_reservation_ledger.py
```

확인 결과:

```text
모두 fcntl.flock(LOCK_EX | LOCK_NB) 사용 — 실제 배타성 기준은 OS lock
.lock 파일을 남기는 것이 설계상 의도이며 문서화돼 있음
  (예: positions/store.py "a stale .lock file left by a crashed process never blocks
   the next", kill_switch_state.py, scalping_watchlist/atomic_io.py 동일 취지)
파일 존재만으로 stale 실행을 차단하지 않음
전체 정방향·역방향 실행에서 저장소 stray artifact 0건
단일 실행(single-run) semantics를 주장하지 않음 — 각각 특정 state 파일의
  read-modify-write 보호용이며 CODEX-060과 혼동될 여지 없음
```

신규 HIGH 아님. **LOW / 별도 정리 후보**로 기록한다.

### LOW-2 — 런북에 신규 환경변수 미기재

`TRADING_SINGLE_RUN_LOCK_FILE`이 `docs/`와 `deploy/` 어디에도 없다. 코드 docstring에는 cwd
종속성까지 명시돼 있어 §3 조건 자체는 충족하지만, 운영자가 Oracle 배포 시 이 knob의 존재와
상대 경로 함정을 런북에서 알 수 없다. 기능 결함 아님. **LOW / 문서 보완 후보**.

---

## 11. 코드 Finding과 Oracle 외부 조건 구분

이 둘은 분리해서 판정한다.

```text
코드 Finding CRITICAL : 0
코드 Finding HIGH     : 0
코드 Finding MEDIUM   : 0
코드 Finding LOW      : 2 (LOW-1 다른 flock context, LOW-2 런북 미기재)
```

```text
Oracle 외부 검증 조건 : 미해소
```

`brokers/kis_broker.VERIFICATION_MATRIX` 실측:

```text
REFERENCE_VERIFIED 아닌 항목 = 0건
LIVE_RESPONSE_PENDING 항목   = 9건
  price_field_last     포함
  cancel_tr_id_live    포함
```

즉 직전 완료 보고의 "남은 MEDIUM 0"은 **코드 Finding 기준**이며, `price_field_last` /
`cancel_tr_id_live`를 포함한 9개 wire 값은 공식 reference 확인은 끝났으나 **실제 KIS 응답으로는
확인되지 않은 상태**로 그대로 남아 있다. 이 항목들은 코드로 해소할 수 없고 Oracle에서
read-only 또는 모의투자 단계의 실제 응답으로만 해소된다.

**실제 KIS 응답 확인 전에는 실주문 활성화를 허용하지 않는다.**

---

## 12. 테스트

집중 범위 (구현자 보고 214건보다 넓게 실행):

```text
tests/test_single_run_lock_lifecycle.py
tests/test_idempotency.py
tests/test_oracle_deploy_package.py
tests/test_fatal_connection_propagation.py
tests/test_repository_read_and_fatal_connection.py
tests/test_persistence_failure_normalization.py
tests/test_cancel_audit_lifecycle.py
tests/test_audit_context_required.py
tests/test_secret_leak_sweep.py
tests/test_shadow_audit_durability.py
tests/test_kis_verification_matrix.py

→ 440 passed / 0 failed / 0 skipped / 0 xfailed
```

전체 정방향:

```text
2273 passed / 0 failed / 0 skipped / 0 xfailed   (78.44s)
```

전체 역방향 (수집 순서 역전):

```text
2273 passed / 0 failed / 0 skipped / 0 xfailed   (77.42s)
```

세 실행 모두 다른 작업과 동시 실행하지 않고 순차로 수행했다.

---

## 13. 외부 네트워크

저장소 밖 socket guard 플러그인(`connect` / `connect_ex` / `create_connection` 후킹, loopback
외 연결 시 즉시 실패)을 세 실행 모두에 적용했다.

```text
집중 : NETGUARD external socket connects: 0   targets: []
정방향: NETGUARD external socket connects: 0   targets: []
역방향: NETGUARD external socket connects: 0   targets: []

Alpaca socket 시도 0
KIS socket 시도 0
Slack socket 시도 0
기타 외부 socket 시도 0
```

---

## 14. 운영 파일 및 artifact

검증 전후 비교 — SHA-256 / size / mtime 3항목 모두 동일:

```text
order_history.csv          153feb31...0a91c7   31 bytes      mtime 1784558966
universe.csv               9fdaf3ac...6188b3   833518 bytes  mtime 1784558966
strategy_performance.csv   ca012439...f66da8   69 bytes      mtime 1785083284
```

artifact — 저장소 루트와 운영 경로 모두 0건:

```text
*.lock            0
*.db              0
*.db-wal          0
*.db-shm          0
*.db-journal      0
shadow-*.jsonl    0
임시 env          0
테스트 logs       0
독립 probe        0   (저장소 밖에서 작성·실행 후 삭제 완료)
```

종료 시 Git 상태:

```text
$ git status --short
 M docs/autonomous/CODEX_REVIEW.md

$ git diff --check
(no output — pass)

$ git rev-parse HEAD
7309a8313adf7801b47a1c6486703ef81fa3efb9
```

변경 파일은 이 문서 하나뿐이다. 코드·테스트 수정, 커밋, push, merge, Oracle 배포, 실주문,
안전 플래그 변경은 수행하지 않았다.

---

## 15. Oracle 진행 허용 범위

```text
허용: Oracle read-only 배포 및 Shadow / 모의투자 단계 진행
      (CODEX-059·060 해결, 코드 CRITICAL/HIGH/MEDIUM 0, 회귀 없음)

금지: 실주문 활성화
```

실주문 활성화 금지 해제 조건:

```text
LIVE_RESPONSE_PENDING 9건이 실제 KIS 응답으로 확인될 것
  (price_field_last, cancel_tr_id_live 포함)
확인 전까지 다음 유지:
  KIS_LIVE_ORDER_ENABLED=false
  LIVE_ROLLOUT_ENABLED=false
  ENTRY_DISABLED=true
```

실거래 활성화 판정은 이 검증의 범위가 아니며 수행하지 않는다.
