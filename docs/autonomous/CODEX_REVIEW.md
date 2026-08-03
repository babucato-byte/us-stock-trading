# CODEX_REVIEW — KIS Limiter Artifact Validation 최종 독립 재검증

## 검증 대상

이전 판정과 테스트 결과를 재사용하지 않고 다음 정확한 HEAD를 검증했다.

```text
$ git branch --show-current
feature/kis-live-broker

$ git rev-parse HEAD
904b9ed75f45f17bcfd8950f5fe40333eb8a5f8a

$ git status --short
 M docs/autonomous/CODEX_REVIEW.md

$ git diff --check
(no output — pass)

$ git show --stat --oneline HEAD
904b9ed Fail closed on invalid KIS limiter temp artifacts
 brokers/kis_rate_limiter.py               | 427 ++++++++++++++++----
 tests/test_limiter_artifact_validation.py | 634 ++++++++++++++++++++++++++++++
 tests/test_limiter_stale_temp_recovery.py |  71 +++-
 3 files changed, 1042 insertions(+), 90 deletions(-)
```

branch와 exact HEAD가 일치하고 코드·테스트 비커밋 변경은 없었다. 기존 보고서 변경만 허용했다.
독립 수집 결과는 `2636 tests collected in 2.97s`였다.

## 최종 판정

```text
Overall verdict: PASS_WITH_CONDITIONS
신규 CRITICAL/HIGH/MEDIUM: 0
Oracle 배포: 아직 허용하지 않음
Shadow timer: Oracle 재검증 전 비활성
실주문/실계좌 취소: 활성화 금지
```

malformed own-prefix artifact와 symlink/non-regular artifact의 transport 허용 결함은 해결됐다. artifact
validation, crash recovery, atomic limiter lifecycle, 부분체결 및 기존 Finding 회귀에서 차단사항을
발견하지 않았다. 남은 조건은 Oracle 실응답, 실제 shared-state 권한과 Ubuntu systemd 검증이다.

## namespace 판정과 filename fullmatch

state `KIS_API_RATE_LIMIT_STATE.json`의 own namespace는 suffix가 아니라 다음 prefix로 수집한다.

```text
.KIS_API_RATE_LIMIT_STATE.json.
```

후보를 수집한 뒤 정규식 `fullmatch()`로 다음 전체 형식만 허용한다.

```text
.{state_filename}.{numeric_pid}.{32-lowercase-hex-uuid}.tmp
```

독립 production probe 결과:

| artifact | 결과 | transport |
|---|---|---:|
| `.temp` suffix | `KIS_RATE_LIMIT_TEMP_ARTIFACT_INVALID` | 0 |
| nonnumeric PID | artifact-invalid | 0 |
| invalid UUID | artifact-invalid | 0 |
| uppercase UUID | artifact-invalid | 0 |
| `.tmp.extra` | artifact-invalid | 0 |
| `.tmp.` | artifact-invalid | 0 |
| trailing newline/space | artifact-invalid | 0 |

invalid artifact는 삭제하지 않고 operator alert를 발생시키며 limiter instance를 invalidated로 만든다.
같은 instance의 다음 `wait()`는 state scan/sleep/transport 없이
`KIS_RATE_LIMIT_LIMITER_INVALIDATED`로 즉시 실패했다.

## symlink와 non-regular file

artifact type은 PID 검사보다 먼저 `os.lstat()`로 판정하며 target을 follow하지 않는다.

| type | detail/reason | 삭제 | target 변경 | transport |
|---|---|---:|---:|---:|
| valid-name symlink | `symlink` / artifact-invalid | 0 | 0 | 0 |
| broken symlink | `symlink` / artifact-invalid | 0 | n/a | 0 |
| live-PID symlink | `symlink` / artifact-invalid | 0 | 0 | 0 |
| dead-PID symlink | `symlink` / artifact-invalid | 0 | 0 | 0 |
| directory | `non_regular_file` | 0 | n/a | 0 |
| FIFO | `non_regular_file` | 0 | n/a | 0 |
| Unix socket | `non_regular_file` | 0 | n/a | 0 |

독립 broker probe에서 모든 fault의 session call은 0이었다.

## cross-category와 mixed artifacts

각 limiter는 exact state filename namespace만 검사한다. READ validator는 TOKEN/ORDER/CANCEL state temp와
일반 사용자 tmp를 삭제하거나 차단하지 않으며, TOKEN validator는 TOKEN own-prefix malformed artifact에서
독립적으로 차단된다. dotted-prefix state 이름도 완전 match의 state group을 비교해 다른 state 소유로
분리한다.

valid stale temp, malformed artifact와 symlink를 동시에 둔 mixed probe는 전체 scan과 validation을 먼저
끝낸 뒤 invalid에서 차단했다. stale temp의 부분 cleanup은 실행되지 않았고 세 artifact 및 외부 target이
그대로 유지됐다. invalid를 제거한 다음 새 limiter run에서 valid stale만 정상 cleanup했다.

## scan failure

| fault | reason | broker session calls |
|---|---|---:|
| directory iteration PermissionError | `KIS_RATE_LIMIT_ARTIFACT_SCAN_FAILED` | 0 |
| directory iteration OSError | scan failed | 0 |
| lstat PermissionError | scan failed | 0 |
| lstat OSError | scan failed | 0 |

FileNotFoundError로 scan 중 사라진 entry는 실제로 남은 artifact가 없으므로 정상 진행한다. 그 외 scan
실패를 “artifact 없음”으로 간주하는 경로는 발견하지 않았다.

## TOCTOU 방어

전체 namespace scan 후 stale cleanup은 해당 directory fd를 열고 각 entry를
`os.lstat(name, dir_fd=...)`로 다시 확인한다. 최초 scan과 비교해 type, `st_dev`, `st_ino` 중 하나라도
변하면 `detail=type_changed`, artifact-invalid, limiter invalidation과 transport 0으로 끝난다.

regular stale temp를 cleanup 직전에 symlink 또는 다른 inode의 regular file로 교체한 fault에서 교체된
entry는 unlink되지 않았고 외부 target과 replacement 내용이 보존됐다. 실제 unlink도 pathname 전체가
아니라 `os.unlink(name, dir_fd=dir_fd)`를 사용한다.

## broker 실제 경로

깨끗한 state directory의 production broker read 대조군은 다음 세 session 호출까지 도달했다.

```text
OVRS_EXCG_CD = NASD, NYSE, AMEX
session calls = 3
```

동일 경로에 malformed, symlink, non-regular 또는 scan failure를 주입하면 limiter 예외가 첫 transport
전에 전파되어 session calls는 0이었다. 단순 limiter 단위 결과가 아니라 실제 3거래소 broker 호출부를
통해 확인했다.

## 정상 stale cleanup과 실제 SIGKILL

실제 subprocess가 temp JSON write, flush, file fsync 후 `os.replace` 직전에 SIGKILL되도록 했다.

```text
child return = -9
committed state = byte-for-byte unchanged, valid JSON
crash 직후 valid stale temp = 1
다음 정상 limiter 실행 후 stale temp = 0
state/reservation = valid
```

정상 stale artifact 강화가 valid dead-PID recovery를 차단하지 않는다. 기본 Policy B(age 0)는 즉시
삭제하고 parent directory를 fsync한다. 양수 Policy A는 wall-clock `wall_now - st_mtime`으로 age 미달을
보존하고 age 초과를 삭제한다. monotonic clock은 file age 계산에 사용하지 않는다.

valid regular temp의 실제 live PID는 stale로 오인해 삭제하지 않고
`KIS_RATE_LIMIT_TEMP_ARTIFACT_LIVE`로 현재 cycle을 차단한다. transient live verdict는 limiter를 영구
invalidated하지 않아 owner가 사라지거나 artifact가 제거된 뒤 같은 instance가 재시도할 수 있다.

## atomic lifecycle와 다중 process pacing

다음 기존 limiter 보장을 재검증했다.

```text
unlock/close failure → transport 0, invalidation
persistence failure → transport 0
same-directory temp write → flush → file fsync → chmod 0600
os.replace → parent directory fsync
replace 전 failure → old state byte preserved
replace 후 fsync failure → complete new JSON, transport 0
atomic reader/writer → empty/truncated/partial/wrong-type observations 0
```

실제 subprocess 4개의 1초 shared pacing은 모두 성공하고 총 3초 이상 직렬화됐다. state JSON은 완전했고
temp collision/leak, deadlock과 permanent lock은 0이었다.

## partial fill 및 기존 Finding 회귀

```text
same odno fills 2+3 → 2 rows, filled 5
ordered 10 → remaining 5, PARTIALLY_FILLED
weighted average → 11.2
exact execution duplicate → 1 row
cross-venue same odno → separate
overfill → no clamp, data_integrity_error/HALT
```

NASD/NYSE/AMEX sweep, partial exchange failure snapshot block, cash non-duplication, invalid/future/permission
limiter fail-closed, ORDER/CANCEL EGW00201 UNKNOWN 및 retry 0, future token rejection과 multi-process token
single issuance도 통과했다.

CODEX-042~060의 exit quantity safety, UNKNOWN account-wide block, reconciliation failure gate, terminal audit
exactly once, fatal repository exit 4, single-run lock cleanup과 secret redaction의 회귀는 발견하지 않았다.

## 테스트와 네트워크

저장소 밖 netguard로 `socket.create_connection`, `socket.connect`, `connect_ex`를 차단했다.

```text
집중 안전: 1201 passed, 0 failed/skipped/xfailed, socket attempts 0
정방향:    2636 passed, 0 failed/skipped/xfailed, socket attempts 0
역방향:    2636 passed, 0 failed/skipped/xfailed, socket attempts 0
```

집중 범위는 요구된 601개 이상이고 전체 수는 기대값과 일치한다. 순서 의존성은 없었다.

## 운영 파일과 artifact

검증 전후 값은 동일했다.

| 파일 | SHA-256 | size | mtime |
|---|---|---:|---:|
| order_history.csv | `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` | 31 | 1784558966 |
| universe.csv | `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3` | 833518 | 1784558966 |
| strategy_performance.csv | `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8` | 69 | 1785083284 |

숨김 파일을 포함한 `*.tmp`, `.*.tmp`, `*.temp`, `.*.temp`, lock, DB/sidecar, shadow JSONL, runtime
rate-limit/token test state와 임시 env는 0건이다. 저장소 밖 probe, socket guard와 logs도 삭제했다.

## Oracle 재검증 조건과 허용 여부

코드 CRITICAL/HIGH/MEDIUM과 기존 Finding 회귀는 발견하지 않았다. 남은 조건은 Oracle host에서 실제
shared-state directory 소유권/권한과 process 경쟁, 실제 KIS read 응답 및 Ubuntu systemd unit 검증이다.

```text
Shadow timer: Oracle 재검증 전 비활성
Oracle 배포: 아직 허용하지 않음
실주문 활성화: 금지
실계좌 취소: 금지
```

## 종료 상태

```text
$ git status --short
 M docs/autonomous/CODEX_REVIEW.md

$ git diff --check
(no output — pass)

최종 변경 파일:
docs/autonomous/CODEX_REVIEW.md
```
