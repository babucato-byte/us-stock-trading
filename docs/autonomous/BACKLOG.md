# BACKLOG — 출시까지 남은 작업 (우선순위순)

작성: 2026-08-05 · 근거: `CODEX_REVIEW.md`(a674112 리뷰), `CURRENT_STATUS.md`(2026-07-31)

목표 정의: **출시 = Oracle 서버에서 Shadow Mode 타이머 활성화 → 무결점 Shadow 운영 기간 통과 → 사용자 최종 승인 후 KIS 제한 실거래.**

---

## T10. 미푸시 커밋 origin push — `status: ready`

T9 산출물 커밋(live_pilot 하네스) 이후 origin push가 남았다.
`git push origin feature/kis-live-broker` 실행 후 origin과 HEAD 일치 확인, 이 항목을 done으로.

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

### T9. 실시간 실테스트 하네스 (스캐너 + 매수·매도 조건) — `status: done` (2026-08-06)

장중에 스캐너 → 신호 → 매수 → lifecycle 매도까지 tick 단위로 반복 실행하는 하네스가
실제로 동작한다. 신규: `live_pilot/` 패키지(7모듈), `scripts/run_live_pilot.py`,
`scripts/start_live_pilot.sh`. 변경: `tests/test_fatal_connection_propagation.py`(목록 추가만).

- **자세(posture)는 코드가 아니라 환경이 정한다.** 기본은 `OBSERVE` — 전 구간을 실시간
  데이터로 평가하되 주문 경로를 **import조차 하지 않는다**. 운영자가 기존 플래그 3개를
  켜면(`.env` 3줄) 그때 처음 `live_pilot/armed.py`를 지연 import해 이미 검증된
  `run_live_buy_entry_cycle()`/`sync_kis_fills_and_manage_exits()`를 부른다.
  자율 루프는 어떤 플래그도 켜지 않았다.
- **평가 로직 복제 0.** `OBSERVE`는 `scripts/run_shadow_mode.py`와
  `run_shadow_exit_evaluation.py`의 `run_once()`를 그대로 호출한다. 새 매수 규칙도
  새 매도 규칙도 만들지 않았다.
- **기동 게이트 10종** — 하나라도 실패하면 루프에 진입하지 않고 exit 3. 스킵 수단(플래그·
  환경변수·CLI) 없음. BACKLOG가 말한 `TBD_VERIFY_LIVE_DOCS` 건은 그 마커를 대체한
  `VERIFICATION_MATRIX`/`LIVE_RESPONSE_PENDING_ITEMS`로 검사한다 — **실측 결과 미확인 값은
  2건이 아니라 9건**이고, `KIS_ENV=live`에서는 전부 FAIL(해제는 코드 변경뿐), paper에서는
  INFO(모의투자가 그 값들을 확인하는 유일한 인가 수단이라 순환이 된다).
- 증거: tick 단위 JSONL(flock→append→fsync, `redact_value` 전건) + 그날 JSONL만 읽어
  재생성 가능한 일일 리포트. 손상된 줄은 버리지 않고 `unreadable_lines`로 센다.
- 구현 중 실제 결함 1건을 잡았다: `run_live_buy_entry_cycle()`의 `submitted`(심볼)와
  `skipped`/`blocked`(심볼·사유 튜플)가 **서로 다른 모양**이라, 첫 구현이 튜플 전체를
  `symbol` 필드에 넣을 뻔했다. 두 모양을 모두 받도록 정규화하고 실제 모양을 회귀로 고정.
- 검증: 전체 회귀 **3,350 passed / 0 failed**(신규 123). 실행 확인 — 실제 스캐너 1회
  통과(12종목 3.1초), 실제 yfinance 분석으로 tick 1회, 4-tick 루프에서 tick 전건 기록·
  shadow_audit 12행 기록·주문 메서드 도달 0, 게이트 실패 시 exit 3 재현.
- 사용자 몫: 자격증명 + 명령 1줄(관찰), 실주문은 `.env` 3줄 → `NEEDS_USER.md` §7.

### T8. 유니버스 확장 + 계좌 금액대 필터 — `status: done` (2026-08-06)

계좌 가용 현금으로 **1주 이상 살 수 있는 종목만** 남기는 진입측 유니버스를 만들었다.
end-to-end 동작 확인됨(실제 12,887행 목록 + 실제 yfinance 데이터).

신규: `universe_filter.py`(순수 판정), `universe_budget.py`(잔고 영속·폴백),
`universe_metrics.py`(배치 시세), `scripts/refresh_universe_budget.py`(유일한 실계좌 접점).
변경: `universe_builder.py`(+`build_tradable_universe()`), `universe_daily_runner.py`(3단계 배선),
`daily_candidate_scanner.py`(+`load_scan_universe()`).

- **`universe.csv`는 좁히지 않았다.** 그 파일은 `market_data/exchange_registry.py`가 읽는
  거래소 메타데이터 권위 소스이고, 그 해석은 **매도에도 실행된다**. 좁히면 주가가 상한을
  넘어선 보유 종목의 청산 경로가 `EXCHANGE_UNKNOWN`으로 막힌다. 필터 결과는
  `universe_tradable.csv`로 분리했다(DECISION_LOG 2026-08-06 결정 1, 회귀 테스트 고정).
- 상한 = `가용현금 × cash_usage_percent(90%, trusted_operator_config) × MAX_POSITION_RATE(0.10)`.
  지시받은 공식에 신뢰 운영자 비율을 **추가로 곱해 더 좁혔다**(안전측). 1주 미만은
  `floor()`로 제외 — 소수점 경로 없음.
- 유동성·가격 하한은 `config/scanner_rules.json`의 스캐너 기준(`>= $5`, `>= $20M`) 그대로 재사용.
- 잔고 조회 실패 시 이전 파일을 바이트 그대로 두고 직전값을 `stale`로 반환. 값 조작·
  `as_of` 재기록·상한 확대 없음. 직전값도 없으면 필터 파일을 아예 쓰지 않는다.
- 산출물 3종: `universe_tradable.csv`(유동성 내림차순), `logs/universe_decisions.csv`
  (심볼별 포함/제외 사유 전건), `logs/universe_filter_report.json`(사유별 통계 + 예산 출처).
- 검증: 전체 회귀 **3,227 passed / 0 failed**(신규 150). 독립 verifier probe 17/17 —
  상한 산술 재유도, 단조성(현금↑ ⇒ 포함집합 superset), 반올림 없음, 미등재 심볼
  `EXCHANGE_UNKNOWN` 재현, 신규 모듈의 주문 메서드 호출 0(AST).
- 실행 확인: 실제 목록 12,887행 필터 0.26초, 실제 yfinance 100종목 8.2초에 44종목 통과.
- 사용자 몫은 자격증명 + 읽기 플래그 1개 + 명령 1줄 → `NEEDS_USER.md` §6.

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
