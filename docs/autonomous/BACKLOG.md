# BACKLOG — 출시까지 남은 작업 (우선순위순)

작성: 2026-08-05 · 근거: `CODEX_REVIEW.md`(a674112 리뷰), `CURRENT_STATUS.md`(2026-07-31)

목표 정의: **출시 = Oracle 서버에서 Shadow Mode 타이머 활성화 → 무결점 Shadow 운영 기간 통과 → 사용자 최종 승인 후 KIS 제한 실거래.**

---

## T8. 유니버스 확장 + 계좌 금액대 필터 — `status: ready`

현재 universe.csv 약 5,000종목. 사용자 지시: 확장하거나 금액대 기준으로 최적화.
구현 (기존 원칙 유지 — 소수점 금지, 최소 1주 매수 가능):
- `universe_builder.py` 확장: 기존 심볼 소스 경로를 재사용해 후보 풀을 넓히고,
  가격 상한 = 계좌 가용 현금 × 기존 `risk_config` 포지션 비율로 1주 이상 매수 가능한
  종목만 포함. 유동성 하한(평균 거래대금)은 기존 스캐너 기준 재사용.
- 계좌 잔고는 KIS 조회값 기준 일일 갱신 (`universe_daily_runner.py`에 배선, 조회 실패 시 직전값 유지).
- 포함/제외 사유별 통계를 로그·리포트로 남긴다.
- 전체 회귀 + 신규 테스트. 실계좌 조회 부분은 fake session 테스트 + 실행 스크립트 분리.

## T9. 실시간 실테스트 하네스 (스캐너 + 매수·매도 조건) — `status: ready`

사용자 지시: 스캐너와 매수매도 조건을 실시간으로 실테스트.
구현:
- `scripts/start_live_pilot.sh` — 장중 실시간 루프 실행기. `KIS_ENV=paper|live` 전환식.
  모의투자(paper)로 즉시 실테스트 시작 가능, live 전환은 .env 스위치 1줄.
- 시작 전 자동 체크리스트: TBD_VERIFY_LIVE_DOCS 2건 검증 상태, kill switch,
  reconciliation freshness, 계좌/유니버스 로드 — 하나라도 실패 시 기동 거부.
- 스캐너 → 신호 → 매수 → lifecycle 매도(`check_and_manage()`) 전 구간을 실시간
  데이터로 돌리고 tick별 JSONL + 일일 리포트 기록.
- 실주문 플래그 코드 기본값은 안전측 유지. 활성화는 사용자의 .env 1줄 —
  절차를 NEEDS_USER에 3줄로 기록한다.

## T3. Oracle 서버 재검증 + Shadow 타이머 배포 준비 — `status: blocked:needs-user`

서버 SSH는 세션에서 불가. `NEEDS_USER.md`에 절차 기록됨.
`ORACLE_KIS_MIGRATION_RUNBOOK.md` §11의 `TBD_VERIFY_LIVE_DOCS` 2건(취소 TR_ID, 현재가 응답 필드)
실계좌 조회로 확인하는 것 포함.

## T5. 매도 전략 인터페이스 잔여 지시 복원 — `status: blocked:needs-user-decision`

사용자의 전략 인터페이스 지시 메시지가 `entry_rules`~`end_of_day_exit_rules` 필드 목록
도중에 끊겨 수신 못함 (CURRENT_STATUS 2026-07-28 항목). 안전 크리티컬이므로 임의로 채우지 않는다.
→ 사용자가 나머지 지시를 아무 세션에나 다시 전달하면 ready로 전환.

## T6. Shadow 성과 트랙 선택 — `status: done` (2026-08-06 사용자 결정: 실거래 진행)

사용자가 실거래 트랙 진행을 지시했다. 실행 준비는 T8·T9로 구체화. (선택지 상세는 SHADOW_MODE_EXIT_CRITERIA.md §6)

## T7. Oracle 환경파일에 Shadow 증거 보관 설정 확정 — `status: blocked:needs-user`

`SHADOW_MODE_EXIT_CRITERIA.md` G11. `SHADOW_AUDIT_RETENTION_DAYS` 기본값이 30일이고
`purge_old_events()`/`purge_old_files()`가 reconciliation 틱에서 실제로 삭제하므로,
20 거래일(≈28 캘린더일) 창구를 기본값으로 돌리면 판정 시점에 창구 앞부분 증거가 이미
삭제돼 있다. **창구 시작 전에** ≥45일로 올려야 하며 사후 복구 불가.
서버 환경파일 수정이 필요해 `NEEDS_USER.md` §3에 명령 기록.

---

## 완료 기록

### T2. feature/kis-live-broker origin push — `status: done` (2026-08-06)

`673888c` → `b36a8a6` fast-forward push 완료. `git fetch` 후 origin/local 양방향 0/0 확인.
`origin/main`은 `fdf2217`로 불변 — main 병합·push 없음.

- push 전 발견한 불일치를 먼저 해소했다: 커밋 `571e03a`가 T4를 done으로 표시하고
  `SHADOW_MODE_EXIT_CRITERIA.md`를 인용하는데 정작 그 파일이 **untracked**였다. 그대로 push하면
  origin의 BACKLOG가 히스토리에 없는 파일을 가리키게 되므로, 직전 사이클 산출물(criteria 문서 +
  NEEDS_USER §3 + CURRENT_STATUS 사이클2 기록)을 `b36a8a6`으로 먼저 커밋했다.
- 같은 커밋에서 CURRENT_STATUS의 미치환 `REGRESSION_PLACEHOLDER`를 실측값으로 교체했다.
- 검증: 전체 회귀 **3,077 passed** (0 failed, 299.6s) — push 직전 실행.
- push 대상 14커밋에 `.env`/`.pem`/`.key`/secret 계열 신규 파일 0건 확인.

### T4. Shadow Mode 운영 기간 판정 기준 문서화 — `status: done` (2026-08-06)

`docs/autonomous/SHADOW_MODE_EXIT_CRITERIA.md` 신규 작성. 문서만, 코드 변경 0.

- Shadow 창구를 **운영 무결성 게이트**로 정의하고 성과 게이트와 명시적으로 분리(§1 매핑표).
- 종료 기준 G1~G11 — 전부 실재하는 산출물(`shadow_audit_events` DB / `shadow_mode` JSONL /
  `run_health_report.collect()` / `reconciliation_state`)로 측정 가능한 형태로만 기술,
  확인 명령 동봉. 기간 20 거래일은 Phase 8 수치를 낮추지 않고 그대로 승계.
- 계수일 인정 조건(§4)과 리셋 규칙(§5) — "무결점"을 셀 수 있는 0-항목 집합으로 조작적 정의하고,
  정상 차단(LIVE_FLAG/ENTRY_DISABLED/가격편차/잔고/중복)은 결함에서 제외.
- 확정된 구조적 공백 2건: ① Shadow 창구는 포지션이 0이라 **매도/청산 경로를 실증하지 못한다**
  (`run_shadow_exit_evaluation.py:331`이 `store.load_non_terminal()`을 순회) → G5는 표본 0을
  허용하되 판정 기록에 미검증 명시 의무. ② 기본 보관 30일이 창구 길이보다 짧다 → G11, T7로 분리.
- 근거 없는 수치는 전부 `ASSUMPTION`으로 표기(§8). 값을 올리는 재검토는 자율 루프가 하고,
  낮추려면 사용자 결정을 요구하도록 규정.
- 검증: 문서가 인용한 심볼/이벤트/env/경로/유닛/산술 **81건 전건 실재 확인**(독립 probe),
  전체 회귀 3,069 passed.

### T1. Codex HIGH 3건 수정 커밋 독립 재검증 — `status: done` (2026-08-06)

커밋 `8c30e6c`(HALT exact bool), `e57b250`(marker before-replace + symlink 안전성),
그 위의 `96e9236`(inode identity + exact mode)을 구현자와 독립된 probe로 재검증했다.

판정 **PASS** — 해제 조건 1~5 전건 재현 확인. 기록:
`docs/autonomous/AUTOPILOT_REVIEW_2026-08-06.md`.

- 1: HALT raw matrix 14종(비-bool 13 + 예외 1) 전부 `main()` exit 6, broker 호출 0
- 2: 실제 SIGKILL 자식 프로세스 — marker 잔존, freshness/승인 게이트 차단, 다음 write로 복구
- 3: marker/lock 타입·exact mode·regular→regular TOCTOU matrix 전건 fail-closed
- 4: 기존 커버리지 대조 후 공백 3건 보강(테스트만 추가, 프로덕션 코드 변경 없음)
- 5: netguard 하 3,069 tests forward/reverse 전건 통과, 외부 socket 0

INFO(잔여, 이미 `96e9236` 커밋 메시지가 공개한 항목): marker의 검증 `lstat`과 `unlink` 사이
간격은 POSIX에 inode 지정 unlink가 없어 닫을 수 없다. `shared/state` 0700 owner-only이며
unlink는 snapshot durable 이후에만 도달하므로 gate 안전성에는 영향이 없다. 운영자용 설명을
`ORACLE_KIS_MIGRATION_RUNBOOK.md`에 추가하고 경계를 회귀 테스트로 고정했다.
