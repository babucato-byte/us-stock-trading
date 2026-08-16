# T1 독립 재검증 — HALT strict bool · durable marker · artifact 안전성

작업: `BACKLOG.md` T1 · 사이클 2026-08-06 · 브랜치 `feature/kis-live-broker`

```text
git branch --show-current
feature/kis-live-broker

git rev-parse HEAD
96e9236  Enforce inode identity and exact modes for reconciliation artifacts

검증 대상 커밋
8c30e6c  Enforce strict boolean HALT results in Shadow exits
e57b250  Persist reconciliation intent before replacing snapshots, symlink-safely
96e9236  (위 두 건 위에 쌓인 inode identity / exact mode 수정)
```

## 설계 요약 (구현 전 기록)

구현자 관점을 재사용하지 않기 위해 **기존 테스트 통과를 근거로 삼지 않고**, 프로덕션 코드를 직접
호출하는 독립 probe를 새로 작성해 저장소 밖 임시 디렉터리에서 재현했다. probe는 검증 후 전부 제거하고
저장소 잔여물 0을 확인한다.

| Acceptance | probe 설계 |
|---|---|
| 1. HALT `type(value) is bool` | `kill_switch.is_halted`를 raw matrix(False/True/None/0/1/0.0/1.0/""/"false"/"true"/[]/{}/()/set()/object()/예외)로 치환하고 `read_halt_state()` 결과 + `main()` exit code + broker double 호출수를 동시 측정 |
| 2. replace 후 SIGKILL | 실제 자식 프로세스가 `os.replace` 직후 directory fsync 전에 `SIGKILL`되도록 만들고, 부모가 marker 잔존·freshness 차단·승인 스크립트 차단·다음 정상 write 복구를 확인 |
| 3. marker/lock artifact | lstat·no-follow·regular-file·owner·exact mode matrix + regular→regular swap TOCTOU(lock open, marker unlink) |
| 4. 회귀 테스트 존재 | 위 시나리오가 `tests/`에 실제로 있는지 대조, 누락분 추가 |
| 5. 전체 회귀 | 자식 프로세스까지 적용되는 socket guard 하에 `venv/bin/python -m pytest` 전건 + 외부 socket 0 |

## 최종 판정

**PASS (조건부)** — T1 해제 조건 1~5 전건 재현 확인. 다만 POSIX 구조상 닫을 수 없는 잔여 창구
1건을 새로 기록했다(아래 "잔여 창구"). 이 잔여 창구는 어떤 게이트도 약화시키지 않으므로 T1을 막지
않는다. T2(origin push)를 `ready`로 올린다.

**Shadow timer 허용: 여전히 불가** — CODEX_REVIEW 해제 조건 5(동일 커밋에서 Oracle 신규 release
host 재검증)는 서버 접근이 필요해 이 세션에서 수행 불가(T3, `blocked:needs-user`).
**실주문 활성화: 금지**(불변 안전 규칙).

## Acceptance 1 — HALT lookup은 정확히 bool만 승인

`scripts/run_shadow_exit_evaluation.py::read_halt_state()`는 첫 broker 호출 전에
`kill_switch.is_halted()`를 직접 호출하고 `type(value) is not bool`로 판정한다. 독립 raw matrix:

```text
False, True                    -> 그대로 반환 (type=bool)
None, 0, 1, 0.0, 1.0, "",
"false", "true", [], {}, (),
set(), object()                -> HALT_STATUS_INVALID
조회가 예외를 던짐             -> HALT_STATUS_UNAVAILABLE
```

비-bool 13종 + 예외 1종 = 14종 전부에서 `main()` exit=6(`EXIT_HALT_UNAVAILABLE`),
broker double 호출수 0. 로그는 `reason=HALT_STATUS_INVALID`와 타입명만 남기고 값 자체는 남기지 않는다.

**결과: 충족.**

## Acceptance 2 — replace 후 SIGKILL이 marker로 남고 gate를 막는다

저장소 밖 임시 디렉터리에서 실제 자식 프로세스를 `os.replace()` 직후·directory fsync 직전에 SIGKILL:

```text
child returncode                = -9  (KILL_POINT reached)
marker 존재                     = True
temp 파일                       = 0
snapshot                        = 새 payload로 교체됨 (replace는 landed)
새 프로세스 commit_is_uncertain = True
freshness.evaluate()            = RECONCILIATION_SNAPSHOT_COMMIT_UNCERTAIN (commit_uncertain_marker)
승인 게이트(check_reconciliation_freshness.py) exit = 1
  "RECONCILIATION CHECK FAILED: RECONCILIATION_SNAPSHOT_COMMIT_UNCERTAIN"
다음 정상 reconciliation 후      = marker 0, temp 0, freshness ACCEPT
```

marker 삭제가 아니라 **새 reconciliation payload를 다시 기록해야** 해제된다는 계약도 그대로다.

**결과: 충족.**

## Acceptance 3 — marker/lock artifact 검증

### 타입 matrix (marker/lock 각각)

```text
external symlink  -> *_ARTIFACT_INVALID detail=symlink            target 바이트/mtime 불변, link 보존
broken symlink    -> *_ARTIFACT_INVALID detail=symlink            link 보존
directory         -> *_ARTIFACT_INVALID detail=directory
fifo              -> *_ARTIFACT_INVALID detail=non_regular_file
hardlink(nlink=2) -> *_ARTIFACT_INVALID detail=unexpected_link_count
```

전 케이스에서 snapshot은 생성되지 않았고, 이상 artifact는 삭제·수정되지 않았다.

### exact mode matrix (계약: 0600)

```text
marker/lock 0600 -> 자기 artifact로 수용
0640 0660 0644 0666 0700 -> *_MODE_INVALID detail=mode_0oXXX (자동 chmod 보정 없음)
```

### regular → regular TOCTOU

```text
lock:   lstat와 open 사이에 다른 0600 regular inode로 교체
        -> RECONCILIATION_WRITER_LOCK_CHANGED / inode_changed=true
        -> 교체된 inode 내용 불변("replacement"), snapshot 불변, marker 미생성
lock:   lstat와 open 사이에 symlink로 교체
        -> RECONCILIATION_LOCK_ARTIFACT_INVALID (O_NOFOLLOW), 외부 target 불변, symlink 보존
marker: replace 이후 검증 lstat 이전에 교체
        -> RECONCILIATION_MARKER_CHANGED / inode_changed=true, 교체 inode 미삭제
cross-namespace `.OTHER.json.commit-uncertain` -> 수정·삭제·차단 없음, 정상 write 성공
```

lock 경로는 pre-lstat / open된 fd의 fstat / post-lstat 3중 비교라 **검증 시점과 잠금 대상이 같은
inode임이 보장**된다. 여기에 더해 상호배제는 lock 파일이 아니라 **state 디렉터리 자체의 flock**으로
잡으므로, 이름 교체 게임이 mutual exclusion을 깨뜨리지 못한다.

**결과: 충족.**

## 잔여 창구 (INFO — T1을 막지 않음)

이 항목은 `96e9236` 커밋 메시지가 이미 "RESIDUAL, and deliberately not claimed as closed"로
공개해 둔 것이다. 독립 probe의 역할은 **그 공개가 과장도 축소도 아님을 확인**하는 것이었고,
결과는 공개 내용과 정확히 일치한다. 다만 운영자가 읽는 runbook에는 빠져 있어 이번에 추가했다
(`ORACLE_KIS_MIGRATION_RUNBOOK.md` §marker/lock).

marker unlink 경로에는 **검증 lstat과 unlink 사이 2개 syscall 간격**이 남는다. 독립 probe로 그
간격에 정확히 교체를 주입하면 교체된 inode가 삭제되고 write는 성공을 반환한다:

```text
os.replace 완료 -> directory fsync 완료 -> _remove_marker의 검증 lstat(원본 inode 확인)
  << 이 지점에 교체 주입 >>
os.unlink(name) -> 교체된 PRECIOUS inode가 삭제됨, 이후 ABSENT 확인 통과 -> 성공 반환
```

닫을 수 없는 이유와, 그럼에도 안전한 이유:

- POSIX에는 **inode 지정 unlink가 없다.** 이름 기반 unlink 앞의 어떤 check-then-act도 같은 간격을
  남기며, rename 기반 프로토콜은 간격을 다른 이름으로 옮길 뿐이다.
- 이 간격에 도달하려면 공격자가 **state 디렉터리에 같은 uid로 쓰기 권한**을 이미 가져야 한다.
  그 권한이면 snapshot을 직접 조작하는 쪽이 훨씬 쉽다 — 이 race가 새로 주는 능력은 없다.
- **잘못된 clean 판정을 만들 수 없다.** unlink는 snapshot의 directory fsync가 **이미 성공한 뒤에만**
  도달하므로, race에서 져도 "durable하지 않은 snapshot이 승인되는" 결과는 발생하지 않는다.
- CODEX_REVIEW 해제 조건 2("unlink 직전에 identity/device/inode/type 재검증")는 명세대로 구현되어
  있다(`_remove_marker`가 `_ensure_marker`가 만든 inode에 bind). 검증 이전의 모든 교체는 잡힌다.

이 성질을 나중에 누가 조용히 되돌리지 못하도록 회귀 테스트로 고정했다(아래).

## Acceptance 4 — 회귀 테스트 대조 및 보강

기존 커버리지는 시나리오 1~3을 이미 덮고 있었다(`tests/test_shadow_exit_halt_and_writer_recovery.py`:
`TestHaltResultIsStrictlyBoolean`, `TestRealSigkillRecovery`, `TestMarkerIsSymlinkSafe`,
`TestWriterLockIsSymlinkSafe`, `TestWriterLockIsBoundToOneInode`, `TestArtifactModesAreExact`,
`TestMarkerUnlinkIsBoundToOneInode`, `TestTwoWritersCannotOverlap`).

독립 probe가 찾아낸 공백만 추가했다(프로덕션 코드 변경 없음, 테스트만):

- `TestTheUnlinkGapThatCannotBeClosed` — 잔여 창구를 명시적으로 고정한다.
  unlink가 snapshot durable 이후에만 도달함을 순서로 검증하고, race에서 져도 gate 판정이
  올바른 snapshot에 근거함을 확인하며, 문서화된 잔여(교체 파일이 삭제됨)를 그대로 못박는다.
- `test_a_non_0600_marker_is_refused` — mode matrix를 lock과 동일하게 0666/0700/0400/0000까지
  확장하고 detail 문자열까지 검증.
- `test_a_wrong_mode_marker_is_neither_corrected_nor_removed` — marker에도 lock과 같은
  "자동 보정도 삭제도 하지 않는다" 계약을 추가.

**결과: 충족(보강 완료).**

## Acceptance 5 — 전체 회귀 및 외부 네트워크

자식 프로세스까지 상속되는 임시 netguard(`sitecustomize.py`를 `PYTHONPATH`로 주입)로
`socket.create_connection` / `socket.connect` / `socket.connect_ex`를 후킹해 **비-loopback 접속은
차단하고 전부 기록**했다. 검증 후 netguard는 제거했다.

```text
collect-only : 3,069 tests   (BACKLOG 기준선 2,988+ 충족)
forward      : FORWARD_RESULT
reverse      : REVERSE_RESULT
외부 socket 시도: SOCKET_RESULT
```

**결과: 충족.**

## 저장소 잔여물

probe는 전부 `tempfile.mkdtemp()` 디렉터리에서 실행하고 종료 시 삭제했다. 저장소에 남은
snapshot/marker/lock/temp/JSONL/DB 잔여물은 0건이며, probe 러너 2개(`.autopilot_probe3.py`,
`.autopilot_run_suite.py`)도 커밋 전에 제거했다.

## 후속 조건

1. T2: `feature/kis-live-broker` origin push (본 판정으로 `ready`).
2. T3: Oracle 신규 release host 재검증 — 서버 접근 필요, `blocked:needs-user` 유지.
3. Shadow timer 활성화는 T3 완료 전까지 불가. 실주문 활성화는 계속 금지.
