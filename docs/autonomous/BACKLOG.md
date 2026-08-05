# BACKLOG — 출시까지 남은 작업 (우선순위순)

작성: 2026-08-05 · 근거: `CODEX_REVIEW.md`(a674112 리뷰), `CURRENT_STATUS.md`(2026-07-31)

목표 정의: **출시 = Oracle 서버에서 Shadow Mode 타이머 활성화 → 무결점 Shadow 운영 기간 통과 → 사용자 최종 승인 후 KIS 제한 실거래.**

---

## T2. feature/kis-live-broker origin push + 로컬 미푸시 커밋 정리 — `status: ready`

로컬 HEAD가 origin(673888c)보다 앞서 있다. T1 PASS 후 origin push.
(main 병합은 하지 않는다 — 사용자 규칙: 검증 통과 후에만 병합, 병합 자체는 사용자 결정.)

## T3. Oracle 서버 재검증 + Shadow 타이머 배포 준비 — `status: blocked:needs-user`

서버 SSH는 세션에서 불가. `NEEDS_USER.md`에 절차 기록됨.
`ORACLE_KIS_MIGRATION_RUNBOOK.md` §11의 `TBD_VERIFY_LIVE_DOCS` 2건(취소 TR_ID, 현재가 응답 필드)
실계좌 조회로 확인하는 것 포함.

## T4. Shadow Mode 운영 기간 판정 기준 문서화 — `status: ready`

Shadow 운영을 며칠/몇 건 이상 무결점 통과하면 제한 실거래 검토로 넘어가는지의
객관 기준이 없다. `SCALPING_V1_ROADMAP.md`와 기존 문서 기반으로 기준안 작성
(구현 아님, 문서만 — 실거래 활성화 결정 자체는 사용자 몫).

## T5. 매도 전략 인터페이스 잔여 지시 복원 — `status: blocked:needs-user-decision`

사용자의 전략 인터페이스 지시 메시지가 `entry_rules`~`end_of_day_exit_rules` 필드 목록
도중에 끊겨 수신 못함 (CURRENT_STATUS 2026-07-28 항목). 안전 크리티컬이므로 임의로 채우지 않는다.
→ 사용자가 나머지 지시를 아무 세션에나 다시 전달하면 ready로 전환.

---

## 완료 기록

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
