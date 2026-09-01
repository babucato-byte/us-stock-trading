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

### T8. 테스트가 아직 라이브 candidate store를 만진다 — `status: open` (2026-09-01)

`03de626d1` 배포와 **분리된** 후속 건이다. 이번 배포는 `TestCliGate` 한 클래스만 격리했다.

`runner.main()`을 호출하면서 candidate store를 격리하지 않는 테스트가 3개 남아 있다:
`tests/test_scanner_edge_cases.py`(222, 257, 274), `tests/test_scanner_notify.py`(286),
`tests/test_scanner_provenance.py`(300). 호스트 게이트는 `TRADING_PROJECT_ROOT`가 릴리스를
가리키는 채로 돌기 때문에 `candidate_dir()`가 **운영 공용 store**로 해석되고, 이 테스트들은
실제 cycle lock을 잡는다.

이들이 지금까지 red가 아니었던 이유는 통과했기 때문이 아니라 **전부 `== 0`을 단언**하기
때문이다. 겹침 거부(refused overlap)의 반환값도 0이라서, 스캔이 실제로 돌았는지 겹침으로
스킵됐는지를 구분하지 못한다. 즉 조용히 무감각해진 상태이고, 동시에 게이트 실행이 라이브 S6
스캔을 stand down 시킬 수 있는 경로가 열려 있다(이쪽이 더 위험한 방향).

수정은 `TestCliGate.isolated_candidate_store`와 동일한 fixture면 된다. 다만 conftest 전역
autouse로 올리면 `tests/test_candidate_handoff.py`의 misconfiguration 거부 테스트(env가
unset이어야 성립)가 깨지므로, 파일별 적용이거나 opt-out 있는 형태여야 한다. 별도 커밋 필요.

근거: `03de626d1` 커밋 메시지, `tests/test_scanner_runner.py::TestCliGateIsolationFromLiveScans`.

### T9. `scanner_profile.sh daily`가 6시간 넘게 락을 쥔 채 실행 — `status: open` (2026-09-01)

2026-08-31 20:17:00Z에 시작된 daily profile 스캔(pid 3395053, 릴리스 `cdad78cc7`)이
2026-09-01 03:0x 시점까지 **6시간 49분** 실행 중이었고, 그동안 shared store의
`hma_early_trend` cycle lock을 계속 점유했다.

거래 영향은 없다 — `hma_early_trend`는 `DISCOVERY_ONLY`이고 S6(`orb`) 락과는 별개다.
다만 (a) 그 시간 동안 `hma_early_trend` 후보 발행이 전부 거부되고, (b) 위 T8의 테스트들이
그 락에 걸린다. 정상 소요 시간과 대조해 hang인지 정당한 장시간 작업인지 먼저 확인할 것.

관측은 read-only였고 프로세스에 손대지 않았다. 확인:
`ps -o pid,lstart,etime -p <pid>`, `logs/cron/scanner_daily.log`.

### T10. Slack 채널 역할 정리 — rename 대기 — `status: blocked` (2026-09-01)

채널 rename은 **이 시스템 권한으로 불가능**하다. 프로덕션에는 webhook URL 5개만 있고
Slack API token이 0개다. `conversations.rename`은 bot token + `channels:manage`가 필요하고
incoming webhook으로는 rename도, channel ID 조회도 할 수 없다.

현재 매핑(채널명이 아니라 webhook 기준):

| env key | 역할 | producer |
|---|---|---|
| `KIS_LIVE_SLACK_WEBHOOK_URL` | LIVE_TRADING (routine) | `operations/live_notifications.py` |
| `KIS_LIVE_SLACK_ALERT_WEBHOOK_URL` | LIVE_TRADING (urgent) | 동일 |
| `SCANNER_MONITOR_SLACK_WEBHOOK_URL` | SCANNER | `scanners/notify/monitor.py` |
| `SLACK_ALERT_WEBHOOK_URL` | 혼재 → SYSTEM_HEALTH 목표 | `ops_dashboard/snapshot.py` 외 |
| `SLACK_WEBHOOK_URL` | 혼재 → PAPER_RESEARCH 목표 | `backtest_report_slack.py` 외 |

4개 역할 중 3개는 이미 분리돼 있고 fallback 금지 규칙까지 명시돼 있다. 남은 간극은
SYSTEM_HEALTH와 PAPER_RESEARCH가 legacy webhook 2개를 공유한다는 점이다.

**라우팅 변경은 일부러 하지 않았다.** rename 전에 health 메시지를 paper alert 이름의
채널로 보내면 지금보다 더 헷갈린다. §E 라우팅 / §F 메시지 감축 / §G 포맷 표준화는 전부
rename 이후 작업이다.

해제 조건: `channels:manage` scope를 가진 Slack bot token 제공, 또는 운영자가 직접 rename.
webhook은 rename 후에도 유효하므로 **rotate 하지 말 것**.

### T11. S6 position cap이 선언과 다르게 강제되지 않는다 — `status: open` (2026-09-01)

`config/position_limits`는 `S6_ORB_BREAKOUT_V1: 1`, `ACTIVE = True`라고 선언한다.
실제로는 강제되지 않는다:

- account-scoped cap 3종(`LIVE_ROLLOUT_MAX_POSITIONS`, `..._PER_STRATEGY`,
  `..._MAX_DAILY_ENTRIES`)은 `order_gate._check_entry_limits`에서 OPTIONAL로 문서화돼
  있고, 배포 env에서 전부 비어 있어 실행되지 않는다.
- `position_limits.check_entry`의 호출자는 `s2_live/executor.py` 뿐이다. S6 모듈 어디서도
  참조하지 않는다.
- `s6_live.entry_timeout.entry_is_blocked`는 두 번째 포지션을 막도록 작성돼 있으나
  **호출부가 없다**.

관측: 2026-09-01 S6가 MTCH+PEGA를 동시 보유, 이어서 MTCH+VALE를 동시 보유했다.

여전히 유효한 보호는 always-on 계층 — 종목별 position lock과 당일 재진입 차단 — 이고
익스포저는 orderable cash(약 $126), 정수주, 무레버리지로 제한된다. 즉 위험 폭주는 아니다.

선택지 3가지이며 **결정 사항이지 정리 작업이 아니다**: (a) env cap을 1/1/2로 설정,
(b) `entry_is_blocked`를 entry path에 연결, (c) 동시 보유를 의도된 동작으로 인정하고
`position_limits`가 강제되지 않는 한도를 선언하지 않도록 수정.

### T12. Slack 유지보수 이월 항목 — `status: deferred` (2026-09-01)

2026-09-01 채널 rename(수동) 및 문서 정리 이후 **의도적으로 남긴** 항목들이다.
각각 판단이 필요한 사안이지 정리 누락이 아니다.

**(a) `DAILY_SUMMARY` 목적지 재검토**
현재 `stock-live-trading`(routine)으로 간다. 목표 역할표는 주기적 성과 보고를
`stock-trading-report`에 두지만, live-trading 역할에도 "realized trade result"가
포함돼 있어 지금 위치도 방어 가능하다. "증명된 오배치"가 아니므로 옮기지 않았다.
결정하면 `operations/live_notifications.py`의 `_webhook_for` 분기만 바꾸면 된다.

**(b) cross-cycle Slack dedup (TTL 방식) — 선택**
`_scan_is_duplicate`는 메시지 **내용** digest로 중복을 막지만 in-process다. 스캐너는
cron 주기마다 새 프로세스라 주기 간 반복(0-candidate 연속)은 억제되지 않는다.
이는 버그가 아니라 명시된 설계 결정이다:

> "Persisting across processes would mean a crashed run's state could silence
>  the re-run that replaces it, which is worse than an occasional repeat."

관측 가능성을 로그 줄 수와 맞바꾸는 것이라 임의로 뒤집지 않았다. 진행한다면
`_mark_scan_sent`가 **전송 성공 후에만** 기록한다는 점이 위 위험을 상당히 줄여주므로,
짧은 TTL을 가진 영속 digest가 현실적인 형태다.

**(c) Alpaca/PAPER Slack 라우팅 정리**
`stock-trading-report`(`SLACK_WEBHOOK_URL`)가 일일 리포트와 Alpaca PAPER 체결을
함께 싣고 있다. 이번 작업에서는 **동작 변경 없이 관측만** 했다. 분리하려면 PAPER 전용
목적지 결정이 선행돼야 한다.

**(d) UNKNOWN Alpaca/support 경로**
실행 증거가 없어 분류를 확정하지 못한 것들. 삭제/비활성화하지 않았다.

- `trading_health_check.py` — cron에는 있으나 로그 파일이 한 번도 기록되지 않음, mtime 71일
- `order_monitor.py` — cron 없음, importer 없음
- `daily_pipeline.py` — cron 없음, importer 없음
- `slack_report.py` — cron 없음, importer 없음 (README는 `#stock-trading-report` 발행이라고 기술)

**(e) position cap 결정 — T11 참조**
`config/position_limits`가 선언한 `S6:1`이 강제되지 않는 건은 별도 항목 T11에 있다.
정리 작업이 아니라 결정 사항이므로 여기서 다시 다루지 않는다.
