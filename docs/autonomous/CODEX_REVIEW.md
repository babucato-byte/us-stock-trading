# HALT Strict Type · Durable Marker · Symlink-Safe Lock 최종 독립 재검증

```text
git branch --show-current
feature/kis-live-broker

git rev-parse HEAD
e57b2508dd1e6ca3fa6be0ac479d2c68d4a9f668

git status --short
 M docs/autonomous/CODEX_REVIEW.md

git diff --check
(output 없음, exit 0)

git show --stat --oneline e57b2508dd1e6ca3fa6be0ac479d2c68d4a9f668
e57b250 Persist reconciliation intent before replacing snapshots, symlink-safely
4 files changed, 725 insertions(+), 110 deletions(-)
```

## 최종 판정

**BLOCKED**

HALT exact type, durable intent marker, symlink/broken-symlink 차단, replace 직후 SIGKILL recovery는
해결됐다. 그러나 지시된 regular-file TOCTOU를 production code가 방어하지 않아 신규 HIGH 2건이 독립
probe에서 재현됐다. 기존 writer lock mode도 exact 0600이 아니어도 승인되는 MEDIUM 1건이 있다.
Shadow timer와 실주문 활성화를 허용하지 않는다.

## Findings

### HIGH — writer lock regular→regular TOCTOU가 inode 재검증 없이 승인됨

`reconciliation/reconciliation_state.py::_open_writer_lock()`은 open 전 lstat 분류와 open 후 fstat의
regular/nlink/world-writable 검사를 수행하지만, 지시된 lstat/fstat `st_dev`·`st_ino` 일치 검사가 없다.
`O_NOFOLLOW`는 symlink 교체만 막고 다른 regular inode로의 교체는 막지 못한다.

독립 race probe:

```text
lstat(.R.json.writer.lock) -> 기존 0600 regular inode 반환
open 직전 lock path를 다른 0600 regular inode로 교체
O_NOFOLLOW open -> 성공
writer flock 및 snapshot write -> 성공 반환
교체된 lock content = "replacement"
```

검증한 inode와 실제 flock inode가 다르다. 공격자가 교체 타이밍마다 다른 inode를 제공하면 writer들이
서로 다른 파일을 잠가 mutual exclusion이 깨질 수 있다. 제공 테스트는 regular→symlink 교체만 다뤄
이 race를 놓친다. lstat 결과와 fstat의 device/inode를 비교하고 불일치 시 artifact를 보존한 채
`RECONCILIATION_LOCK_ARTIFACT_INVALID`로 차단해야 한다.

### HIGH — marker unlink regular→regular TOCTOU가 교체 inode를 삭제하고 성공 반환

`_remove_marker()`는 `_classify_file_artifact()`로 marker를 lstat한 뒤 곧바로 dir-fd 상대 unlink한다.
검증한 inode와 unlink 직전 inode/device/type의 동일성을 재확인하지 않는다.

독립 race probe:

```text
marker lstat -> 원래 valid marker
unlink 직전 marker를 다른 0600 regular "PRECIOUS" inode로 교체
os.unlink(marker, dir_fd=...) -> 교체 inode 삭제
directory fsync -> 성공
writer -> 성공 반환
PRECIOUS 존재=false, marker 존재=false
```

지시된 marker unlink TOCTOU 계약은 교체 감지, 삭제 0, fail-closed 유지다. 현재는 검증하지 않은 inode를
삭제하고 uncertainty를 해제한다. unlink 직전 lstat와 초기 검증 inode를 비교하거나 rename-safe한
identity protocol이 필요하다.

### MEDIUM — 기존 writer lock mode를 exact 0600으로 강제하지 않음

artifact 검사는 world-writable bit만 거부한다. 독립 mode matrix:

```text
0600 -> ACCEPT
0640 -> ACCEPT
0660 -> ACCEPT
0644 -> ACCEPT
0666 -> REJECT(world_writable)
```

지시된 lock 계약은 mode 0600이다. group-writable/readable 및 world-readable lock을 승인하면 안 된다.
open 전 lstat와 open 후 fstat 양쪽에서 `stat.S_IMODE(mode) == 0o600`을 확인해야 한다.

## HALT strict type

`read_halt_state()`는 첫 broker/account/positions/open-orders 조회 전에
`kill_switch.is_halted()`를 직접 호출하고 `type(value) is bool`을 적용한다. raw matrix 독립 probe:

```text
False -> False
True -> True
None, 0, 1, 0.0, 1.0, "", "false", "true",
[], {}, (), set(), object() -> HALT_STATUS_INVALID
조회 예외 -> HALT_STATUS_UNAVAILABLE
```

invalid/unavailable은 entrypoint exit 6, broker/order/cancel transport 0이다. HALT=false는 TARGET_2 등
전략 exit 평가를 계속한다. HALT=true에서 TARGET_1/TARGET_2/TIME_STOP/TRAILING_BREAKEVEN은
HALT_ACTIVE로 차단되고 STOP_LOSS/EOD_FORCED_CLOSE만 RISK_REDUCTION으로 계속 평가한다. 신규 reason
기본값은 STRATEGY다. HALT_CHECKED/EXIT_BLOCKED_HALT는 non-terminal이며 run당 terminal은 정확히 1이다.

## Marker 및 lock symlink 안전성

정상 생성은 dir-fd, `O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW`, 0600, file fsync, directory fsync를
사용한다. lock도 dir-fd와 `O_RDWR|O_NOFOLLOW`, 신규 시 `O_CREAT|O_EXCL`, 0600을 사용한다.

독립 probe 결과:

```text
marker external symlink -> MARKER_ARTIFACT_INVALID, target bytes/mtime 불변, link 보존
broken marker symlink + fresh snapshot -> freshness COMMIT_UNCERTAIN, link 보존
lock external symlink -> LOCK_ARTIFACT_INVALID, target bytes/mtime 불변, link 보존
directory/FIFO/socket/world-writable/hardlink marker·lock -> fail-closed
symlink/broken-symlink TOCTOU -> O_NOFOLLOW 차단
```

marker 존재/비정상 artifact 검사는 snapshot JSON/schema/freshness보다 먼저 수행되어 fresh clean snapshot도
승인하지 않는다. cross-namespace `.OTHER.json.*` artifact는 수정·삭제·차단하지 않는다. 단 regular-file
TOCTOU 및 exact lock mode findings는 남아 있다.

## Durable intent 및 SIGKILL

production 순서는 writer lock -> artifact validation/temp cleanup -> marker exclusive create/write/file fsync
-> marker directory fsync -> snapshot temp/write/file fsync -> replace -> snapshot directory fsync -> marker unlink
-> marker directory fsync -> unlock이다. marker는 replace 전에 durable하다.

저장소 밖 실제 subprocess 핵심 결과:

```text
D: replace 직후 snapshot directory fsync 전 SIGKILL
  return=-9, 새 snapshot 노출 가능, marker 존재, temp 0
  새 process freshness -> RECONCILIATION_SNAPSHOT_COMMIT_UNCERTAIN
  다음 정상 reconciliation -> marker 0/temp 0, 새 snapshot 및 freshness 정상

F: marker unlink 후 marker directory fsync 전 SIGKILL
  current view marker 0, snapshot directory fsync는 이미 완료
  fresh-clean 새 process freshness -> ACCEPT
```

F 결과는 marker 삭제가 유지된 경우 snapshot이 이미 durable하므로 허용 정책과 일치한다. marker가 crash
후 다시 나타나는 경우에는 marker 우선 검사로 차단되고 다음 정상 reconciliation이 full lifecycle을
다시 수행한다.

A~E crash 지점에서는 replace 전 기존 snapshot 보존 또는 durable marker 존재로 새 process
freshness/approval/runtime이 차단된다. C의 dead-PID temp는 다음 정상 write가 lock 안에서 삭제하고
directory fsync한다. temp/marker unlink 실패 및 cleanup directory fsync 실패는 성공 반환하지 않으며 gate
차단을 유지한다. recovery는 marker만 지우지 않고 새 reconciliation payload를 다시 기록한다.

## Strict schema·freshness·운영 회귀

strict schema는 clean 문자열/정수, count 문자열/bool, 필수 필드 누락, schema version 누락/문자열/미지원을
계속 차단한다. invalid approval은 enable 0/start 0이고 runtime Shadow body 0이다.

정상 6필드 snapshot에서 30일 stale 차단, TTL 899/900 허용·901 차단, future 29/30 허용·31 차단,
naive timestamp/partial JSON/world-writable snapshot 차단을 유지한다.

installer live static·enable symlink 0, installer enable/start 0, 실패 후 timers disabled/inactive, approval
exact true, rollback, JSONL fallback 0, IXN/ARCA KIS 호출 0, AAPL 정상/hypothetical, terminal exactly once,
limiter artifact/SIGKILL/atomic persistence/4-process pacing, 3거래소 reconciliation, token cache 공유,
partial fill 2+3=5, EGW00201 UNKNOWN, single-run lock cleanup과 redaction은 집중 회귀에서 통과했다.

## 테스트 및 외부 네트워크

자식 프로세스에도 적용되는 임시 netguard로 `socket.create_connection`, `connect`, `connect_ex`를
차단·기록했고 완료 후 제거했다.

```text
collect-only: 3,040 tests
focused: 1,781 passed, failed/skipped/xfailed 0, socket attempts 0
forward: 3,040 passed, failed/skipped/xfailed 0, socket attempts 0
reverse: 3,040 passed, failed/skipped/xfailed 0, socket attempts 0
KIS/Alpaca/Slack external socket: 0
```

## 운영 파일 및 artifact

| 파일 | SHA-256 | size | mtime |
|---|---|---:|---:|
| order_history.csv | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| universe.csv | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| strategy_performance.csv | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

세 파일의 SHA-256/size/mtime는 전후 동일하다. repo의 TRADING_STATE.db, shadow JSONL, snapshot temp,
commit marker, tmp/temp, DB/sidecar와 netguard pyc는 0건이다. 저장소 밖 probe와 socket logs도 제거했다.
writer lock은 probe 임시 디렉터리에만 생성되어 함께 제거됐고 repo 상주 lock은 없다.

## 해제 조건

1. lock open 전 lstat와 open 후 fstat의 `st_dev/st_ino`를 비교하고 regular→regular swap을 차단한다.
2. marker unlink 직전에 identity/device/inode/type를 재검증해 교체 inode를 삭제하지 않는다.
3. 기존 및 신규 lock의 mode를 exact 0600으로 검증한다.
4. regular→regular lock swap, marker unlink inode swap, 0640/0660/0644 lock matrix를 실제 회귀 테스트에
   추가한다.
5. 동일 commit에서 독립 A~F/subprocess/approval/runtime와 집중·정방향·역방향을 재검증한 후 Oracle 신규
   release host 검증을 수행한다.

현재 Shadow timer 허용: **불가**. Oracle 재검증 전 비활성 유지. 실주문 활성화: **금지**.
