# BACKLOG — 출시까지 남은 작업 (우선순위순)

작성: 2026-08-05 · 근거: `CODEX_REVIEW.md`(a674112 리뷰), `CURRENT_STATUS.md`(2026-07-31)

목표 정의: **출시 = Oracle 서버에서 Shadow Mode 타이머 활성화 → 무결점 Shadow 운영 기간 통과 → 사용자 최종 승인 후 KIS 제한 실거래.**

---

## T1. Codex HIGH 3건 수정 커밋 독립 재검증 — `status: ready`

커밋 `8c30e6c`(HALT exact bool)와 `e57b250`(marker before-replace + symlink 안전성)이
`CODEX_REVIEW.md` 해제 조건 1~4를 실제로 충족하는지 **구현자와 독립된 관점으로** 재검증한다.

Acceptance (CODEX_REVIEW.md 해제 조건 그대로):
1. HALT lookup 결과 `type(value) is bool` 검증 — None/0/[]/{}/"false" 전부 exit 6, broker 0 probe 재현
2. replace 후 SIGKILL probe: marker가 남아 재시작 freshness/approval 차단 확인
3. marker/lock lstat·no-follow·regular-file·owner/mode 검증 probe (broken symlink, external target, lock symlink)
4. 위 시나리오가 회귀 테스트로 추가되어 있는지 확인 (없으면 추가)
5. 전체 회귀: `venv/bin/python -m pytest` 2,988+ 전건 통과, 외부 socket 0

결과는 `CODEX_REVIEW.md` 스타일로 `docs/autonomous/AUTOPILOT_REVIEW_<date>.md`에 기록.
판정이 PASS면 T2를 ready로 올린다.

## T2. feature/kis-live-broker origin push + 로컬 미푸시 커밋 정리 — `status: blocked:after-T1`

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

(사이클 완료 시 여기로 이동)
