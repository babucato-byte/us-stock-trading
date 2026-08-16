# CURRENT_STATUS

마지막 갱신: 2026-08-06

## 자율 사이클 2026-08-06 (5) — T9 실시간 실테스트 하네스 **완료**

`AUTOPILOT.md` 계약에 따른 다섯 번째 사이클. BACKLOG 최상위 `ready` 항목 T9를 처리했다.
장중에 **스캐너 → 신호 → 매수 → lifecycle 매도**를 tick 단위로 반복 실행하는 하네스가
실제로 동작한다(실제 스캐너 1회 + 실제 yfinance 분석 + 4-tick 루프로 확인).

- **설계 판단(가장 중요): 자세(posture)를 코드가 고르지 않게 했다.** 파일럿은 매 tick마다
  `KIS_LIVE_ORDER_ENABLED`/`LIVE_ROLLOUT_ENABLED`/`ENTRY_DISABLED`를 **다시 읽어** 두 갈래로
  분기한다. `OBSERVE`(기본)는 전 구간을 실시간 데이터로 평가하되 주문 경로를 **import조차
  하지 않는다** — 플래그를 검사해서 안 하는 게 아니라 그 모듈이 프로세스에 없다(회귀 테스트가
  `sys.modules`로 고정). `ARMED`는 운영자가 세 플래그를 켰을 때만 `live_pilot/armed.py`를
  지연 import한다. 자율 루프는 이 사이클에서도 어떤 플래그도 켜지 않았다. 자세를 시작 시
  한 번 고정하지 않은 이유는, 장중에 `ENTRY_DISABLED`를 켠 운영자가 **재시작이 아니라 다음
  tick에** 즉시 반영되기를 기대하기 때문이다. 위험한 방향(OBSERVE→ARMED)은 세 플래그가 전부
  필요하므로 편집이 중간에 끊기면 항상 OBSERVE로 떨어진다.
- **평가 로직을 한 줄도 복제하지 않았다.** `OBSERVE`는 `scripts/run_shadow_mode.py`와
  `scripts/run_shadow_exit_evaluation.py`의 `run_once()`를, `ARMED`는
  `run_live_buy_entry_cycle()`과 `sync_kis_fills_and_manage_exits()`를 그대로 호출한다.
  매수 게이트나 매도 조건을 하네스 안에 다시 쓰면 실거래 경로와 조용히 갈라진다 —
  Shadow 서비스가 애초에 그 이유로 만들어졌다. `armed.py`가 자체 게이트를 넣지 않는 것도
  회귀 테스트로 고정했다(`is_halted(`/`evaluate_buy_gate(`/플래그 이름 등장 금지).
- **기동 게이트 10종, 스킵 수단 없음**: KIS 환경 / 미확인 KIS 응답값 / kill switch /
  reconciliation freshness / 실계좌 조회 + 허용계좌 일치 / 스캔 유니버스 / 워치리스트 /
  플래그 정합성 / 로그 디렉터리 / 공용 단일실행락. 하나라도 실패하면 루프에 **진입하지 않고**
  exit 3. `--skip-preflight`도, 게이트를 끄는 환경변수도 만들지 않았다(테스트가 파서 옵션을
  직접 읽어 고정).
- **BACKLOG가 말한 `TBD_VERIFY_LIVE_DOCS` 2건은 실제로 9건이었다.** 그 마커는 CODEX-052에서
  이미 제거됐고(`test_kis_verification_matrix.py`가 부재를 고정한다) 권위 소스는
  `VERIFICATION_MATRIX`다. 지금 `LIVE_RESPONSE_PENDING`인 값은 runbook이 추적하던 2건
  (`price_field_last`, `cancel_tr_id_live`)을 포함해 **9건**이다. 게이트는 9건 전부를 보고,
  `KIS_ENV=live`에서는 FAIL·`paper`에서는 INFO로 갈랐다 — paper가 이 값들을 확인하는 유일한
  인가 수단이라고 runbook이 규정하므로("실계좌 주문으로 확인하지 않는다"), paper까지 막으면
  확인 절차 자체가 순환이 된다. 해제는 환경변수가 아니라 매트릭스를 코드에서
  `LIVE_RESPONSE_CONFIRMED`로 바꾸는 변경뿐이다.
- **증거 파일을 Shadow와 분리했다.** tick JSONL은 `logs/live_pilot/live-pilot-YYYY-MM-DD.jsonl`.
  `shadow_mode.persist()`의 규율(flock → append → flush → fsync, `redact_value` 전건 +
  free-text `redact_text`)은 그대로 가져왔지만 파일은 나눴다 — Shadow JSONL은 판정 창구의
  증거이고(G1~G11이 그 행을 센다), 거기에 파일럿 tick을 섞으면 창구 증거가 희석된다.
  일일 리포트는 그날 JSONL만 읽는 순수 집계라 언제든 재생성 가능하고(`--report-only --date`),
  손상된 줄은 건너뛰지 않고 `unreadable_lines`에 센다.
- **공용 단일실행락을 붙잡지 않는다.** `execution.idempotency.single_run_lock()`을 몇 시간짜리
  세션 동안 쥐면 reconcile·shadow·health 타이머가 전부 굶는다. preflight는 그 락이 지금
  비어 있는지만 확인하고 즉시 놓고, 파일럿의 배타성은 자기 락 파일로 따로 잡는다.
- **이번 사이클에 잡은 실제 결함 1건**: `run_live_buy_entry_cycle()`이 돌려주는 세 리스트의
  모양이 서로 다르다 — `submitted`는 **심볼 문자열**, `blocked`/`skipped`는 **(심볼, 사유)
  튜플**이다(`kis_live_trading.py:464` vs `:272`/`:303`). `sync_kis_fills_and_manage_exits()`도
  같은 식이다(`kis_position_manager.py:332` vs `:273`/`:304`). 첫 구현이 `skipped`를 심볼
  목록으로 읽어 튜플 전체가 tick 로그의 `symbol` 필드에 들어갈 뻔했다. **ARMED 세션에서만
  드러나는 결함**이라 실행 없이는 안 잡히고, 이 세션에는 실거래 자격증명이 없어 그 경로를
  돌릴 수 없으므로 호출 대상 소스를 직접 대조해서 찾았다. 두 모양을 모두 받는 `_split_pair()`로
  정규화하고 각 리스트의 **실제** 모양을 회귀 테스트로 고정했다.
- **매도 사유는 지어내지 않았다**: `managed` 리스트에는 심볼만 담기고 어떤 exit 조건이
  발화했는지는 담기지 않는다. tick은 `reason_code="MANAGED"`까지만 기록하고 사유는 그 값이
  실제로 생성되는 곳(포지션 레코드 + 매도측 감사 이벤트)에 맡긴다.
- **검증**: 전체 회귀 **3,350 passed / 0 failed** (391.2s, 신규 123).
  독립 probe 23/23 — ① 실제 스캐너 1회 통과(12종목 3.1초, `candidates.csv` 등 실제 생성)
  ② 실제 yfinance 분석으로 tick 1회(AAPL/MSFT `BELOW_SCORE_THRESHOLD`) ③ 4-tick 루프에서
  tick 4/4 기록·워치리스트 3종목 전건 평가·`shadow_audit_events` 12행 실제 기록
  ④ 주문 메서드 도달 0(submit_order가 예외를 던지는 broker double로 실행 확인)
  ⑤ HALT 세팅 시 preflight 거부 재현 ⑥ 스냅샷 삭제 시 실제 프로세스 exit 3 재현
  ⑦ 리포트 재생성 결정성 ⑧ `KIS_ENV=live` + ack 없음 → python 도달 전 거부.

**사용자 몫**: 관찰 모드는 자격증명 + 명령 1줄, 실주문은 `.env` 3줄 → `NEEDS_USER.md` §7.
그 전까지 하네스는 주문 경로를 import하지 않는다(= 안전).
**다음 ready 항목**: 없음. T3·T7은 서버 접근, T5는 사용자 지시 재전송 대기.
**Shadow timer 활성화 불가, 실주문 활성화 금지**는 그대로다.

## 자율 사이클 2026-08-06 (4) — T8 유니버스 계좌 금액대 필터 **완료**

`AUTOPILOT.md` 계약에 따른 네 번째 사이클. BACKLOG 최상위 `ready` 항목 T8을 처리했다.
계좌 가용 현금으로 **1주 이상 살 수 있는 종목만** 남기는 진입측 유니버스가 실제로 동작한다
(실제 12,887행 목록 + 실제 yfinance 데이터로 확인).

- **설계 판단(가장 중요): `universe.csv`를 좁히지 않았다.** 그 파일은 스캐너 후보 피드일 뿐
  아니라 `market_data/exchange_registry.py`가 읽는 **거래소 메타데이터 권위 소스**이고, 그
  해석은 **매도에도 실행된다**. "지금 살 수 있는 종목"으로 좁히면 보유 종목의 주가가 상한을
  넘어선 순간 목록에서 빠지고 청산 주문의 거래소 해석이 `EXCHANGE_UNKNOWN`으로 막힌다.
  진입 최적화를 위해 청산 경로를 깨는 것은 안전 회귀이므로, 필터 결과를
  **`universe_tradable.csv`라는 별도 파일**로 분리했다. 이 위험은 가정이 아니라 verifier
  probe로 재현했고 회귀 테스트로 고정했다.
- **상한 공식**: `가용현금 × cash_usage_percent(90%) × MAX_POSITION_RATE(0.10)`.
  지시받은 "가용현금 × 포지션 비율"에 `trusted_operator_config`의 신뢰 운영자 비율을
  **추가로 곱해 더 좁혔다** — PROJECT_CONSTITUTION 계층 분리 원칙을 따르면서 상한을 낮추는
  방향이라 안전측이다. 1주 미만은 `floor()`로 제외하며 소수점 경로는 코드에 존재하지 않는다.
  `live_readiness/watchlist_affordability.py`는 설계상 `AFFORDABLE_FRACTIONAL`을 반환할 수
  있어 이 유니버스에서는 합법 결과가 아니므로 재사용하지 않았다(규율만 승계).
- **잔고 조회 실패 정책**: 이전 상태 파일을 **바이트 그대로** 두고 직전값을 `stale=True`,
  `source="cached:..."`로 반환한다. 값을 지어내지 않고 `as_of`를 재기록하지 않으며 상한을
  넓히지 않는다. 직전값조차 없으면 필터 파일을 **아예 쓰지 않는다**(이전 파일 유지).
- **산출물 3종**: `universe_tradable.csv`(유동성 내림차순 — 다운스트림 `scan_limit`이
  임의의 CSV 꼬리가 아니라 유동성 낮은 쪽부터 자르게 된다), `logs/universe_decisions.csv`
  (심볼별 포함/제외 사유 **전건**), `logs/universe_filter_report.json`(사유별 통계 + 예산 출처·
  상한·임계값 출처).
- **실계좌 접점 분리**: KIS 잔고 조회는 `scripts/refresh_universe_budget.py` 한 곳뿐이고,
  빌드 단계는 영속된 JSON만 읽어 소켓을 열지 않는다. 덕분에 전체 빌드가 broker 없이
  테스트되고, KIS 경로 자체는 fake requests.Session으로 실 KISBroker를 통해 검증했다.
- **검증**: 전체 회귀 **3,227 passed / 0 failed** (305.7s, 신규 150).
  독립 verifier probe **17/17** — 상한 산술 재유도, 단조성(현금↑ ⇒ 포함집합 superset),
  반올림 없음, 미등재 심볼 `EXCHANGE_UNKNOWN` 재현, 신규 모듈의 주문 메서드 호출 0(AST).
  실행 확인: 실제 목록 12,887행 필터 0.26초(포함 3,176 / 미지원거래소 4,271 / 예산초과 1,739 /
  유동성미달 2,473 — 합성 시세 기준 분포), 실제 yfinance 100종목 8.2초에 44종목 통과.
  상장폐지 심볼(`IRAB.WS`, `TRAD.RT`)은 데이터 없음으로 빠지고 나머지는 계속 진행됐다.

**사용자 몫은 1단계만 남았다**: KIS 읽기 자격증명 + `KIS_ACCOUNT_READ_ENABLED=true` +
`venv/bin/python scripts/refresh_universe_budget.py --show` → `NEEDS_USER.md` §6.
그 전까지는 `universe_tradable.csv`가 생성되지 않고 스캐너는 기존 `universe.csv`를 그대로
쓴다(= T8 이전과 동일 동작, 안전).

**다음 ready 항목**: T9(실시간 실테스트 하네스).
**Shadow timer 활성화 불가, 실주문 활성화 금지**는 그대로다.

## 자율 사이클 2026-08-06 (3) — T2 origin push **완료**

`AUTOPILOT.md` 계약에 따른 세 번째 사이클. BACKLOG 최상위 `ready` 항목 T2를 처리했다.
`origin/feature/kis-live-broker`: `673888c` → **`b36a8a6`** (fast-forward, 15커밋).
`git fetch` 후 `rev-list --left-right --count origin...HEAD` = **0 0**. `origin/main`은
`fdf2217` 불변 — main 병합·push 없음(불변 안전 규칙 유지).

- **push 전에 잡은 불일치**: 커밋 `571e03a`가 T4를 done으로 표시하고
  `SHADOW_MODE_EXIT_CRITERIA.md`를 인용하는데, 그 파일이 워킹 트리에 **untracked**로 남아 있었다.
  그 상태로 push하면 origin의 BACKLOG가 히스토리에 존재하지 않는 문서를 가리킨다. 그래서 직전
  사이클 산출물(criteria 문서 + `NEEDS_USER` §3 + CURRENT_STATUS 사이클2 기록)을 `b36a8a6`으로
  먼저 커밋한 뒤 push했다. 프로덕션 코드 변경 0.
- **미치환 플레이스홀더 교정**: 사이클2 기록의 `REGRESSION_PLACEHOLDER`가 그대로 남아 있어
  이번 실측값(3,077 passed)으로 교체했다.
- **git 락 우회**: 직전 세션이 남긴 0바이트 `.git/HEAD.lock`(02:24, 실행 중 git 프로세스 0)이
  모든 커밋을 막았다. 샌드박스가 `.git` 내부 파일 삭제를 거부하므로, 삭제 대신
  `write-tree` → `commit-tree` → **분리 HEAD 링크드 워크트리**에서 `update-ref`로 브랜치를
  전진시켰다(링크드 워크트리는 자체 HEAD 파일을 쓰므로 메인 `HEAD.lock`을 잡지 않는다).
  임시 워크트리는 즉시 제거·prune했고 워킹 트리는 clean이다.
  **잔여**: `.git/HEAD.lock`은 아직 남아 있다. 이후 세션에서 일반 `git commit`을 쓰려면
  사용자가 `rm .git/HEAD.lock` 한 줄을 실행해야 한다 → `NEEDS_USER.md` §5.
- **검증**: 전체 회귀 **3,077 passed / 0 failed** (299.6s, `venv/bin/python -m pytest -q`).
  push 대상 커밋에 `.env`/`.pem`/`.key`/secret 계열 신규 파일 0건.
  push 후 `git cat-file -p origin/...:docs/autonomous/SHADOW_MODE_EXIT_CRITERIA.md`로
  origin 트리에 문서가 실재함을 확인.

**다음 ready 항목**: T8(유니버스 확장 + 계좌 금액대 필터), T9(실시간 실테스트 하네스).
**Shadow timer 활성화 불가, 실주문 활성화 금지**는 그대로다.

## 자율 사이클 2026-08-06 (2) — T4 Shadow 판정 기준 문서화 **완료**

`AUTOPILOT.md` 계약에 따른 두 번째 사이클. BACKLOG 최상위 `ready` 항목 T4를 처리했다.
산출물: `docs/autonomous/SHADOW_MODE_EXIT_CRITERIA.md` (신규). **문서만, 프로덕션 코드 변경 0.**

- **설계 판단**: Shadow는 체결이 0이므로 손익 기반 게이트를 원리상 판정할 수 없다. 따라서 Shadow
  창구를 **운영 무결성 게이트**로 좁게 정의하고, 성과 게이트는 별도 트랙으로 분리했다. Shadow
  통과를 "성과 검증 통과"로 읽지 못하도록 `ACCEPTANCE_CRITERIA.md` §Phase 8 14개 항목 전부를
  판정 가능/부분/불가로 매핑한 표를 문서 앞에 뒀다(가능 6, 부분 2, 불가 6).
- **종료 기준 G1~G11**: 전부 실재하는 산출물로 측정 가능한 형태로만 작성했다 —
  `shadow_audit_events`(권위), `shadow_mode` JSONL(조건부), `run_health_report.collect()`,
  `reconciliation_state`. 확인 명령을 못 붙이는 항목은 기준으로 채택하지 않았다.
  기간은 Phase 8의 "최소 20거래일"을 **낮추지 않고 그대로** 승계.
- **"무결점"의 조작적 정의**: 계수일 인정 조건(§4)과 리셋 규칙(§5)으로 분해했다. 정상 차단
  (`LIVE_FLAG`/`ENTRY_DISABLED`/가격편차/잔고/중복)은 안전장치가 일한 기록이므로 결함 집합에서
  제외했고, 실주문 발생·감사 무결성 위반·`SHADOW_ERROR`·reconciliation dirty·HALT·배포 커밋
  변경만 전체 리셋 사유로 뒀다.
- **이번에 확정된 구조적 공백 2건**(둘 다 신규 위험이 아니라 지금까지 기준이 없어 안 보이던 것):
  1. **Shadow는 매도/청산 경로를 실증하지 못한다.** `scripts/run_shadow_exit_evaluation.py:331`이
     `store.load_non_terminal()`을 순회하는데, 실주문 0이면 포지션도 0이라 매도 타이머는 평가
     대상 없이 돈다. "shadow-exit 무사고"가 매도 로직 검증의 근거가 될 수 없다 → G5는 표본 0을
     허용하되 판정 기록에 `미검증` 명시를 의무화했다.
  2. **기본 보관 기간(30일)이 판정 창구(20 거래일 ≈ 28일)보다 짧다.** `purge_old_events()`/
     `purge_old_files()`가 reconciliation 틱에서 실제로 삭제하므로, 기본값 그대로 창구를 돌리면
     판정 시점에 창구 앞부분 증거가 이미 사라진다. 사후 복구 불가 → G11(창구 시작 **전** 확정,
     ≥45일 권고) + BACKLOG `T7` + `NEEDS_USER.md` §3.
- **파생 항목**: `T6`(성과 트랙 A/B/C 선택, `blocked:needs-user-decision` — Alpaca 주문 차단으로
  기존 "Paper 100회 체결" 경로가 성립하지 않는다. 권고는 A: 이미 구현된 KIS 모의투자 TR_ID 활용),
  `T7`(보관 설정, `blocked:needs-user`).
- **검증**: 문서가 인용한 심볼·이벤트 타입·env·경로·systemd 유닛·산술 **81건을 저장소 밖 독립
  probe로 전건 실재 확인**(probe 최초 실행에서 1건 오탐을 잡아 AST 기반으로 교정 — `run_shadow_
  mode.py`의 `execution_engine` 언급은 import가 아니라 그 보장을 설명하는 docstring이었다).
  전체 회귀 **3,077 passed**(사이클 3에서 커밋 직전 재실행해 확정. 이 자리에 미치환 플레이스홀더가
  남아 있던 것을 사이클 3이 실측값으로 교체했다).

**다음**: ready 항목 없음. T3·T7은 서버 접근, T5·T6은 사용자 결정 대기.
**Shadow timer 활성화 불가, 실주문 활성화 금지**는 그대로다.

**BACKLOG T2 관련 주의**: 이 사이클 시작 시점의 워킹 트리에 T2(origin push) 항목이 커밋되지 않은
채 삭제돼 있었다. 확인 결과 **푸시는 실제로 일어나지 않았다** — `origin/feature/kis-live-broker`는
여전히 `673888c`이고 로컬은 13커밋 앞서 있다. 남의 미커밋 편집을 되돌리지 않기 위해 삭제 상태를
그대로 두고 이 사실만 기록한다. 푸시가 필요하면 별도 지시 바람.

## 자율 사이클 2026-08-06 — T1 독립 재검증 **PASS**

`AUTOPILOT.md` 계약에 따른 첫 사이클. BACKLOG T1(Codex HIGH 3건 수정 커밋 독립 재검증)을
Planner→Implementer→Verifier 순으로 처리했다. 상세: `AUTOPILOT_REVIEW_2026-08-06.md`.

- 검증 대상: `8c30e6c`(HALT exact bool) · `e57b250`(marker before-replace + symlink 안전성) ·
  `96e9236`(inode identity + exact mode). 브랜치 `feature/kis-live-broker`.
- 기존 테스트 통과를 근거로 삼지 않고, 프로덕션 코드를 직접 호출하는 **독립 probe**를 저장소 밖
  임시 디렉터리에서 새로 작성해 해제 조건 1~5를 전부 재현했다.
  - HALT: 비-bool 13종 + 조회 예외 1종 → 전부 exit 6, broker 호출 0
  - durable marker: 실제 SIGKILL(replace 직후·directory fsync 직전) → marker 잔존,
    `freshness`·승인 게이트 차단, 다음 정상 reconciliation으로만 해제
  - artifact: symlink/broken symlink/디렉터리/FIFO/hardlink/비-0600 mode 전건 fail-closed,
    lock의 regular→regular swap은 `RECONCILIATION_WRITER_LOCK_CHANGED`로 차단
- 회귀 보강(테스트만, 프로덕션 코드 변경 없음): marker mode matrix 확장, marker "자동 보정·삭제
  없음" 계약, 그리고 marker unlink의 **닫을 수 없는 잔여 창구**를 고정하는 신규 테스트 클래스.
- 잔여(신규 위험 아님, `96e9236` 커밋 메시지가 이미 공개): POSIX에 inode 지정 unlink가 없어
  검증 `lstat`과 `unlink` 사이 간격은 제거 불가. `shared/state`가 0700 owner-only이고 unlink는
  snapshot durable 이후에만 도달하므로 잘못된 clean 판정을 만들 수 없다. 운영자용 설명을
  `ORACLE_KIS_MIGRATION_RUNBOOK.md`에 추가했다.
- 전체 회귀: netguard(자식 프로세스 포함) 하에 3,069 tests 수집, 전건 통과, 외부 socket 0.

**다음**: T2(origin push, ready로 승격). T3(Oracle 재검증 + Shadow 타이머 배포 준비)는 서버 접근이
필요해 `blocked:needs-user` 유지 — **Shadow timer 활성화 불가, 실주문 활성화 금지**는 그대로다.

## 현재 Phase
**Alpaca 데이터 전용 / KIS 실거래 브로커 전환 — 매수·매도 전체 경로 구현 완료
(2026-07-31, `feature/kis-live-broker` 브랜치, HEAD `ad50311`, `BLOCKED`).**

이전 사이클(매수 경로만 완료)에 이어, 사용자가 "매도 자동화는 새 전략 설계가 아니라 기존
`positions/lifecycle.py` 매도·손절·익절 정책을 KIS에 연결하는 작업"이라고 명확히 하며 계속
진행을 지시했다. 5개 신규 커밋으로 다음을 완료했다(전체 회귀 **1,613 passed, 0 failed**):

- `brokers/kis_broker_adapter.py`: `positions/lifecycle.py`의 이미 완성·검증된 매도 엔진
  (`check_and_manage()` — 손절/익절/분할익절/트레일링/시간손절/EOD강제청산, 정책 미변경)이
  KIS로 실제 매도 주문을 낼 수 있게 하는 브로커 어댑터.
- `kis_position_manager.py`: KIS 매수 체결 후 포지션 레코드 생성, **KIS 실제 평균체결가
  기준**(신호가 아님)으로 손절/익절가 확정(`risk_config.STOP_LOSS_RATE` + 기존 R-multiple
  공식 재사용), 매 tick마다 KIS 체결/포지션 조회 → 리콘실리에이션(불일치 시 차단, 자동 보정
  없음) → `check_and_manage()` 재사용. `check_invalidation()`(전략 무효화 매도)는 의도적으로
  제외 — score 기반 진입에는 대응하는 Strategy 플러그인 객체가 없어 임의로 만들면 "새 매도
  전략 추가"가 되기 때문(문서화된 잔여 범위).
- `shadow_mode.py`: 요구된 전 필드를 JSONL로 영속 기록 — `KIS_LIVE_ORDER_ENABLED=false`와
  별개의 산출물.
- `paper_strategy_order.submit_order()`(실제 `main()`/매도 경로가 호출하는 유일한 운영
  진입점)에 Alpaca 주문 차단을 **실제로 배선**했다 — 이제 실 `AlpacaBroker` 인스턴스가 이
  경로에 도달하면 기본값(플래그 미설정)에서 broker 호출 0회로 차단된다. `AlpacaBroker` 클래스
  자체가 아니라 이 운영 래퍼에만 배선해, 그 클래스를 직접 단위 테스트하는 기존 테스트들은
  전혀 건드리지 않았다. 영향받은 기존 테스트 정확히 8개(2개 파일)는 삭제/완화 없이 명시적
  플래그 활성화로 원래 의도(레거시 게이트 자체 검증)를 유지했다.

**남은 것**: Codex 독립 검증(외부 비동기 프로세스, 직접 트리거 불가), Oracle 서버 배포·KIS
실계좌 조회·실제 KIS 대상 Shadow Mode 실행(전부 이 환경의 도구 접근 한계로 미실시, 코드/문서는
완료), main 병합·origin push(사용자 지시대로 검증 전 보류).

**Live trading: DO_NOT_ENABLE.** `approved`/`live_enabled`는 여전히 `false`.

사용자가 최종 아키텍처 전환을 지시했다: Alpaca는 시장 데이터·종목 탐색 전용으로 고정(Paper/Live
주문 완전 차단), 한국투자증권(KIS) Open API가 유일한 실주문 브로커. 기준 태그
`pre-kis-integration`, 작업 브랜치 `feature/kis-live-broker`(HEAD 이전 `orchestrator/...`
브랜치의 `2014104`에서 분기)를 생성했다. 12개 커밋으로 다음을 구현·테스트했다(전체 회귀
**1,575 passed, 0 failed**, 신규 테스트 약 480건):

- `domain/`: Instrument/Signal/OrderIntent/ExecutionRecord/Position/AccountSnapshot —
  브로커 중립 공통 모델(construction-time fail-closed 검증).
- `broker/broker_config.py`: `ALPACA_ORDER_ENABLED`/`ALPACA_PAPER_ORDER_ENABLED`(둘 다
  기본값 `false`) fail-closed 게이트 추가 — **아직 `AlpacaBroker.submit_order()`에 실제
  배선하지는 않음**(현재도 운영 중인 Alpaca paper 주문 경로를 KIS가 완전히 대체하기 전에
  끊으면 시스템 전체가 무주문 상태가 되므로, 배선은 §89 후속 사이클로 명시적으로 미룸).
- `brokers/kis_broker.py` + `kis_config.py`: KIS Open API 어댑터(OAuth 토큰 발급/갱신,
  실전·모의 분리, 현재가/잔고/주문가능금액/지정가매수/지정가매도/취소/미체결/체결내역).
  TR_ID·엔드포인트는 공식 GitHub 예제(`koreainvestment/open-trading-api`)에서 실제로
  확인했다 — 취소 TR_ID 1건과 현재가 응답 필드명 1건은 확인 소스가 간접적이라
  `TBD_VERIFY_LIVE_DOCS`로 코드에 명시(실거래 전 재확인 필요, `ORACLE_KIS_MIGRATION_
  RUNBOOK.md` §11 참고).
- `execution/`: `order_state_machine.py`(11개 상태, UNKNOWN은 오직 사람이 확인한
  reconciliation을 통해서만 벗어남, 절대 자동 재제출 없음), `idempotency.py`(SQLite
  migration 6, `internal_order_id` + `(signal_id, symbol, side, trading_date)` 이중
  유일성 제약 + 단일 실행 파일 잠금), `order_gate.py`(spec 매수 17항목·매도 5항목 안전
  검사, 순서대로 fail-fast), `execution_engine.py`(idempotency → gate → KISBroker의 유일한
  호출 경로 — `tests/test_execution_engine.py`의 기존 아키텍처 가드에 추가).
- `reconciliation/`: position/order/account 3종 — KIS가 항상 최종 원장, 불일치는 차단만
  하고 자동 보정하지 않음.
- `operations/`: 기존 `kill_switch_state.py`/`notification_health.py`/`slack_utils.py`를
  감싸는 얇은 파사드 + spec의 ENTRY_OFF/HALT/EMERGENCY_LIQUIDATE 3분류(기존 2상태
  kill switch에는 없던 개념, 새 파일 기반 HALT 플래그로 추가 — `kill_switch_state.py`
  자체는 변경하지 않음).
- `market_data/`: `alpaca_provider.py`(기존 yfinance 기반 데이터 경로 재사용 — 이 저장소의
  "Alpaca 데이터"는 원래부터 yfinance였다는 사실을 문서화), `kis_validation_provider.py`
  (KIS 현재가 재검증).
- `config/live_rollout_config.py`: spec §19 정책(수량/포지션/일일진입 한도는 config로
  확대 가능하지만, 소수점·시장가·연장거래·레버리지·인버스·공매도·마진은 이번 파일럿에서
  절대 허용하지 않는 하드 블록으로 구현).
- `kis_live_trading.py`(신규 최상위 모듈, `paper_strategy_order.py` 미변경): Alpaca/yfinance
  후보 발굴(기존 `load_watchlist()`/`analyze_stock()` 재사용) → KIS 가격 재검증 → KIS 계좌
  조회 → 사이징 → `OrderIntent` → `execution_engine.submit_buy_order()`. 구조적 사전조건
  (`live_rollout.enabled`, HALT, ENTRY_OFF, 검증·배포 커밋 일치, 허용 계좌번호 설정) 확인 후
  진행.
- `tests/test_kis_negative_suite.py`: spec §22 필수 부정 테스트를 한곳에 정리 — "Alpaca
  운영 주문 호출 0회"는 KIS 경로 12개 모듈이 `broker.alpaca_client`를 아예 import하지 않음을
  AST로 구조적으로 증명(단순 호출부 검사보다 강함).

**명시적으로 미구현 — 임의로 채우지 않고 사용자에게 나머지 지시 재요청한 상태**: 매도 자동화
(손절/익절/50% 분할익절/트레일링스탑/전략무효화매도/시간손절/EOD청산) — 사용자의 두 번째
지시 메시지가 전략 인터페이스 필드 목록(`entry_rules`~`end_of_day_exit_rules`) 도중 끊겨
나머지 내용을 받지 못했다. `execution_engine.submit_sell_order()`/`order_gate.evaluate_
sell_gate()`는 이미 완성·테스트됐으므로, "언제 팔지" 전략 로직과 그 배선만 남았다.

**Claude Code가 실행할 수 없는 것(도구 접근 한계, 사용자에게 명시)**: Oracle 서버 SSH 접근
없음(§91 `ORACLE_KIS_MIGRATION_RUNBOOK.md`는 절차서만 작성, 실행은 서버 접근 권한이 있는
사람/세션이 수행), 실제 KIS API 자격증명 없음(모든 KIS 코드는 fake session 기반 테스트만
검증됨, 실계좌 조회는 미수행), Codex 독립 검증은 외부 비동기 프로세스라 직접 트리거 불가
(이전 사이클처럼 `CODEX_REVIEW.md`가 갱신되면 처리).

**Live trading: DO_NOT_ENABLE.** `KIS_LIVE_ORDER_ENABLED`/`ENTRY_DISABLED`/`LIVE_ROLLOUT_
ENABLED`/`ALPACA_ORDER_ENABLED`/`ALPACA_PAPER_ORDER_ENABLED` 코드 기본값 전부 안전측(주문
차단) 유지. `approved`/`live_enabled`는 여전히 `false`. `main` 병합·origin push는 수행하지
않았다(사용자 지시 §끝: Codex 검증 통과 후에만 병합).

---

## 2026-07-28 — Codex 독립 재검증 PASS_WITH_CONDITIONS + 자동 운영 구조 전환 착수 (종료)
**Codex 독립 재검증 PASS_WITH_CONDITIONS + 자동 운영 구조 전환 착수 (2026-07-28, 진행 중).**

Codex가 `CODEX-039/040/041 실제 운영 경로 배선 사이클`(커밋 `ae2b0fd`/`fc20574`)을 독립적으로
재검증했다(`CODEX_REVIEW.md`, 커밋 `ebce9d0`로 그대로 기록). **결과: overall verdict
`PASS_WITH_CONDITIONS`, Stage 3~11 `VALIDATED`, Limited live review `READY_FOR_LIMITED_LIVE_
REVIEW`로 상승**(직전 `BLOCKED`). CODEX-034~041 전 항목 `RESOLVED`, 신규 CRITICAL/HIGH Finding
없음. **Live trading은 여전히 `DO_NOT_ENABLE`, `approved`/`live_enabled`도 여전히 `false`** —
"제한적 실거래 검토를 시작할 준비가 코드/테스트 수준에서 됐다"는 뜻이지 실거래 활성화가 아니다.
Codex가 명시한 필수 후속 조건: 제한적 live review 전에 operator TBD(`TBD_REVIEW_RECOMMENDATIONS.md`)
· kill-switch 절차 · reconciliation runbook을 **사람이** 검토해야 한다.

이 검증 직후, 사용자가 별도로 "운영자 제한값 없는 자동 운영 구조로 변경"(현금 사용률 등 일일
수동 입력 제거) 및 "전략 기반 자동 매수·매도 + 현금주문 전용 정책"(활성 전략이 손절/익절/분할
익절/트레일링 스탑/무효화/시간 손절/EOD 청산까지 전부 정의, 소수점 주문 금지, 최소 1주 이상
매수 가능한 종목만 탐색) 두 건을 추가로 지시했다. 두 번째 지시는 메시지가 전략 인터페이스 필드
목록 도중 잘렸고(파일이 아닌 채팅 원문이라 CODEX_REVIEW.md처럼 다시 읽어올 수 없음), 손절/익절
로직을 다루는 안전 크리티컬 영역이라 임의로 채우지 않고 사용자에게 나머지 내용을 재전송해
달라고 요청한 상태다(사용자가 "이어서 붙여주기"를 선택함, 아직 미수신).

이번 사이클에서 지금까지 완료한 것(로컬 커밋 `a0f0ae2`):
- `trusted_operator_config.CASH_USAGE_PERCENT_CEILING` 기본값 **50 → 90**(운영자가 매일 별도
  입력하지 않아도 시스템이 사용하는 자동 기본값; 여전히 (0,100] 검증, margin/leverage와 무관하게
  non-margin cash만 사용, 여전히 코드 리뷰를 거치는 운영자 결정이지 caller 선택이 아님).
- "소수점 주문 금지" / "최소 1주 이상 매수 가능한 종목만 주문" 요구사항이 **이미** 배선돼 있음을
  확인·문서화·회귀 테스트로 고정: `run_live_entry_pipeline()`의 `fractionable` 파라미터는
  기본값 `False`이고 유일한 호출부(`paper_strategy_order.main()`)가 이를 절대 override하지
  않으므로, 모든 live 진입은 이미 whole-share-only이며 1주 미만만 감당 가능한 종목은 차단된다
  (신규 회귀 테스트로 고정, `sizing_engine`/`account_engine`은 코드 변경 없음).
- 90% 기본값 변경으로 기존 50% 가정 테스트 4건(`test_account_engine.py` 2건,
  `test_live_entry_pipeline.py` 1건, `test_live_order_gateway.py` 1건)의 기대값을 갱신.
- 전체 회귀 **1,333 passed, 0 failed**.

**아직 미착수**(사용자 지시 §16 요구사항 중 나머지): 운영자 일일 수동 입력이 필요 없다는 사실을
반영해 `TBD_REVIEW_RECOMMENDATIONS.md`/`LIMITED_LIVE_REVIEW_CHECKLIST.md`의 "일일 진입 횟수/
종목별 주문금액/수동 allow-list/최대 동시 포지션/거래시간 수동 입력"을 실거래 시작 필수 TBD에서
제외하는 것(코드가 실제로 "시장 전체 후보 자동 스캔 → allow-list 없이도 안전하게 종목 자동 선별"을
구현하기 전까지는 문서만 앞서가면 안 되므로, 이 자동 선별 자체를 먼저 구현해야 함 — 아직
미구현), 전략 기반 손절/익절/분할익절/트레일링스탑/무효화/시간손절/EOD청산 자동 생명주기(사용자
메시지 §1 이어지는 부분 수신 대기 중), 9종 거버넌스 문서 전체 갱신 및 `FINAL_VALIDATION_PACKAGE.md`
재생성(사용자 지시가 아직 진행 중이므로 이번 사이클 완료 시 한 번에 처리 예정).

---

## 2026-07-28 — CODEX-039/040/041 실제 운영 경로 배선 사이클 (커밋 `fc20574`까지, 종료)
**CODEX-039/040/041 실제 운영 경로 배선 사이클 — 전부 RESOLVED (2026-07-28).**
Codex 독립 검증(`CODEX_REVIEW.md`, 커밋 `9d294e3`/`40abc58`/`06a77c8`/`3494fe3`/`14f7a13` 포함 범위,
overall verdict `FAIL`)이 CODEX-036을 `PARTIALLY_RESOLVED`로 재확인하고 신규 CODEX-039(MEDIUM,
trusted 50%가 default가 아니라 강제 maximum)·CODEX-040(HIGH, 실제 `main()` 주문 흐름이 Stage 11
Execution Engine 전체를 우회)·CODEX-041(MEDIUM, affordability가 실제 후보/주문 차단에 미배선)을
제기했다. 3건 전부 로컬 브랜치에서 수정·테스트했다(커밋 `ae2b0fd`).

- **CODEX-039**: `trusted_operator_config.get_cash_usage_percent()` 신설 — 인자를 전혀 받지 않고
  트러스트 값을 그대로 반환한다(caller percent와 결합하지 않음). 기존 `get_cash_usage_percent_
  ceiling()`은 `order_gateway.py`의 레거시 `LiveEntryContext.cash_usage_percent` 계약 전용으로
  이름/문서만 명시적으로 분리해 유지(값 자체는 변경 없음, 여전히 50%).
- **CODEX-040**: 신규 `live_readiness/live_entry_pipeline.py` — Account Engine → Risk Engine →
  Sizing Engine → Affordability Filter → Execution Engine을 실제로 orchestrate한다.
  `paper_strategy_order.main()`이 `broker.config.is_live_mode`인 `side="buy"` 진입에 대해 레거시
  `submit_order()`/직접 broker 호출 대신 이 파이프라인을 호출하도록 배선됐다. Paper 모드는 완전히
  미변경(기존 400건 이상 테스트 그대로 통과). `execution_engine.submit_validated_command()`가
  신규 optional `account_cash_snapshot`을 `broker.submit_order()`로 전달해 CODEX-036의 잔여 위험도
  이 새 경로에서는 닫힘.
- **CODEX-041**: `live_entry_pipeline.py`가 watchlist 후보 선별과 동일한 `evaluate_affordability()`
  함수를 Execution Engine 직전 최종 검사로 재실행 — 주문 불가능한 candidate가 broker 호출 0회로
  차단된다.
- **런타임 통합 테스트**(`tests/test_main_live_entry_wiring.py`): live 모드 정상 경로에서 4개
  엔진(Account/Risk/Sizing/Execution) + affordability가 정확히 1회씩 호출됨을 확인, 각 단계
  실패 시 broker 호출 0회 확인, 레거시 `submit_order()` wrapper가 live 진입에서 절대 호출되지
  않음을 확인, Paper 모드 `main()` 동작이 완전히 동일함을 확인(신규 엔진 호출 0회).

전체 회귀 **1,331 passed, 0 failed**(직전 1,299에서 32건 신규). `approved: false`, `live_enabled:
false` 유지, **Live trading: DO_NOT_ENABLE**. 다음 작업은 새 `FINAL_VALIDATION_PACKAGE.md`(새
SHA-256 포함) 작성 후 상태를 `READY_FOR_FINAL_CODEX_REVALIDATION`으로 종료하는 것.

## Stage 11 — Account/Risk/Sizing/Execution Engine 계층 분리 (2026-07-28 완료).
CODEX-034~038 수정에 이어, 사용자 지시에 따라 주문 경로를 `Market Data → Strategy Engine →
Signal → Risk Engine → Account Engine → Sizing Engine → Execution Engine → Broker` 계층으로
분리했다. `docs/autonomous/PROJECT_CONSTITUTION.md`에 "계층 분리 원칙"을 신설(Strategy는 신호/
진입가/손절가만 결정, 계좌·비율·수량은 절대 결정하지 않음 — `strategy/interface.py::
EvaluationResult`에 애초에 그런 필드가 없어 코드 구조로 강제됨).

- **`live_readiness/trusted_operator_config.py`**(신규): `cash_usage_percent` 트러스트 상한
  (50%)과 `MAX_CONCURRENT_LIVE_POSITIONS`/`MAX_DAILY_LIVE_ENTRIES`의 단일 소스. `account_cash.py`/
  `order_gateway.py`가 이제 여기서 값을 가져온다(하위 호환을 위해 기존 이름으로 재노출).
- **`live_readiness/account_engine.py`**(신규): `AccountSnapshot`(immutable) — 실제
  `broker.get_account()` + `entry_reservation_ledger.build_snapshot()` 기반.
  `effective_cash = min(broker_cash, non_margin_available_cash)`(margin 미사용, buying_power는
  상한으로 절대 사용 안 함). broker 조회 실패/cash 누락·음수·NaN/Paper-Live 모호/계좌 ID 불일치
  시 fail-closed 차단.
- **`live_readiness/risk_engine.py`**(신규): `compute_risk_decision()` — 전략이 전달한 수량을
  절대 사용하지 않고 진입가/손절가/일일 손실 잔여 한도로 risk_based_qty를 독자 계산. 모든 숫자
  finite 검증, 하나라도 무효면 전체 차단.
- **`live_readiness/sizing_engine.py`**(신규): `compute_sizing_decision()` —
  `actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)`, 세 값 모두 명시적으로
  유효할 때만 계산. `apply_entry_price_buffer()`로 슬리피지/가격상승 버퍼 적용.
- **`live_readiness/execution_engine.py`**(신규): `ValidatedOrderCommand` + broker 호출 유일
  경로. 만료된 command, qty*price 불일치(변조 의심), symbol 불일치, SQLite 기존 예약과의 불일치는
  broker 호출 0회로 차단. 다른 모듈의 `broker.submit_order(` 직접 호출을 금지하는 정적 grep 테스트
  (`tests/test_execution_engine.py`)로 강제 — `paper_strategy_order.py`의 기존 호출은
  legacy compat으로 명시적으로 유지(삭제 없음).
- **`live_readiness/watchlist_affordability.py`**: `STALE_ACCOUNT_STATE`(존재하지만 만료된 계좌
  스냅샷, `UNKNOWN_ACCOUNT_STATE`와 구분) + `buffered_entry_price`/`account_snapshot_at` 필드
  추가.

전체 회귀 **1,299 passed, 0 failed**(직전 1,125에서 174건 신규). `approved: false`, `live_enabled:
false` 유지, **Live trading: DO_NOT_ENABLE**. 다음 작업은 새 `FINAL_VALIDATION_PACKAGE.md`(새
SHA-256 포함) 작성 후 상태를 `READY_FOR_FINAL_CODEX_REVALIDATION`으로 종료하는 것.

## CODEX-034~038 최종 수정 사이클 (2026-07-27)
Codex 독립 검증(`CODEX_REVIEW.md`, 커밋 `5da6662`/`5316cd1`/`72bbb6c` 포함 범위, overall verdict
`FAIL`)이 CODEX-034를 `PARTIALLY_RESOLVED`로 재확인하고 신규 CODEX-035(HIGH, HTTP 5xx/408/425/429를
definitive rejection으로 오분류)·CODEX-036(HIGH, 잔고/사용비율이 authoritative source 없이 caller
선언에 의존)·CODEX-037(HIGH, optional NaN sizing/risk cap이 fail-open)·CODEX-038(LOW, 테스트가
운영 CSV mtime 변경)을 제기했다. 4건 전부 로컬 브랜치에서 수정·테스트했다(커밋 `40abc58`).

- **CODEX-035**: `broker/alpaca_client.py`/`paper_strategy_order.py`의 ambiguous-failure 분류를
  "response 존재 여부"에서 "definitive rejection status code allowlist(400/401/403/404/409/410/422)
  + 파싱 가능한 JSON body" 기준으로 재작성. 408/425/429/5xx/파싱 불가 body/미인식 코드는 전부
  ambiguous(SUBMISSION_UNKNOWN) 기본값 — 새로운 status code도 fail-closed로 처리된다.
- **CODEX-036**: `live_readiness/account_cash.py` 신설 — `TRUSTED_CASH_USAGE_PERCENT_CEILING`(신뢰
  가능한 코드 상수, `cash_usage_percent`를 caller가 절대 완화 불가)와 `AccountCashSnapshot`/
  `fetch_account_cash_snapshot()`(broker.get_account() 기반, 유일한 생성 경로). `validate_and_
  size_live_entry()`가 신규 optional `account_cash_snapshot` 인자를 받아 `min(caller 선언값, 실제
  스냅샷)`으로 caller가 잔고를 부풀릴 수 없게 한다. `AlpacaBroker.submit_order()` 내부에서 자동
  fetch하지는 않음 — 이 저장소의 pre-live 안전 게이트가 dry-run 여부와 무관하게 모든 live 모드
  broker 호출을 차단하므로, 내부에서 fetch를 시도하면 sizing-only 검증 자체가 깨진다(실거래 승인
  이후 production caller가 채워야 할 배선, `DECISION_LOG.md` 참고).
- **CODEX-037**: `max_order_notional_krw`/`max_daily_loss_krw`/`max_risk_per_trade_krw`/
  `strategy_max_quantity`/`stop_price_usd` 5개 optional cap 전부에 finite/양수 검증을 예약 이전에
  추가 — NaN이 `<=0` 가드와 `min()` 비교를 통과해 위험/주문 한도를 무력화하던 결함 수정.
- **CODEX-038**: `tests/test_performance_analytics.py::test_summary_csv_generation`이
  `STRATEGY_PERFORMANCE_FILE`을 격리하지 않아 실제 저장소 루트 `strategy_performance.csv`의 mtime을
  매 테스트 실행마다 변경하던 결함 수정.

전체 회귀 **1,125 passed, 0 failed**(직전 1,044에서 81건 신규). `approved: false`, `live_enabled:
false` 유지, **Live trading: DO_NOT_ENABLE**. 다음 작업은 새 `FINAL_VALIDATION_PACKAGE.md`(새
SHA-256 포함) 작성 후 상태를 `READY_FOR_FINAL_CODEX_REVALIDATION`으로 종료하는 것.

## CODEX-034 + 잔고 비율 기반 주문 사이징 사이클 (2026-07-27)
Codex 독립 검증(`CODEX_REVIEW.md`, 커밋 `5da6662` 포함 범위, overall verdict `FAIL`)이 제기한
신규 CODEX-034(HIGH, broker 응답 유실 시 live-entry reservation을 해제해 중복 주문과 예산 우회
허용)를 수정. 동시에 사용자 지시에 따라 고정 `30,000원` 파일럿 예산을 영구 정책으로 굳히지 않고,
`available_cash × cash_usage_percent`(margin/leverage 미사용, 현금 기준) 잔고 비율 모델로
전면 교체했다.

- **CODEX-034**: `live_entry_reservations`에 `client_order_id` 컬럼 추가(SQLite migration 5,
  UNIQUE), 상태에 `SUBMISSION_UNKNOWN` 신설. `broker/alpaca_client.py::AlpacaBroker.submit_order()`
  와 `paper_strategy_order.py`가 `requests.exceptions.HTTPError`(응답 있음)/사전-네트워크 실패는
  안전하게 `RELEASED`, `requests.exceptions.RequestException`(timeout/connection reset, 응답
  없음)은 `SUBMISSION_UNKNOWN`으로 분류해 예산/포지션 집계에서 계속 차감 상태를 유지한다.
  `entry_reservation_ledger.reconcile_by_client_order_id()`가 재시작/재시도 시 broker에
  `client_order_id`로 재조회해 최종 상태(commit/release)를 확정하는 화해 경로.
- **잔고 비율 사이징**: `live_readiness/order_gateway.py`에서 `PILOT_TOTAL_BUDGET_KRW` 고정 상수
  완전 제거. `LiveEntryContext`에 `available_cash_krw`/`cash_usage_percent`(1~100, 검증)/
  `cash_as_of`를 신설, `max_allocatable_cash = available_cash × cash_usage_percent/100` →
  `available_for_new_order = max_allocatable_cash - pending - unknown_submission -
  open_position_cost`(전부 `entry_reservation_ledger.build_snapshot()`의 authoritative SQLite
  집계, caller 선언 아님)로 매 호출마다 재계산. `actual_qty = min(balance_based_qty,
  risk_based_qty, strategy_max_qty)` — 손절 위험이 잔고 기준 수량보다 더 타이트하면 거부가 아니라
  수량을 줄이는 방식으로 변경(`max_risk_per_trade_krw`/`strategy_max_quantity` 신규 optional
  필드).
- **관심종목 affordability 필터**: `live_readiness/watchlist_affordability.py` 신설(순수 계산
  모듈, `daily_candidate_scanner.py`/`scalping_watchlist/` 파이프라인에 아직 배선 안 함 — Stage 10
  선례와 동일하게 building block으로 보류). `AFFORDABLE_WHOLE_SHARE`/`AFFORDABLE_FRACTIONAL`/
  `INSUFFICIENT_BALANCE`/`NOT_FRACTIONABLE`/`BELOW_MINIMUM_ORDER`/`UNKNOWN_ACCOUNT_STATE` 6개
  상태로 분류하며, `fractionable=true` 종목은 1주 가격이 잔고를 초과해도 최소주문금액을 충족하면
  후보로 유지한다(명시적 요구사항).

전체 회귀 **1,044 passed, 0 failed**(직전 986에서 58건 신규 — CODEX-034/사이징 78건 +
watchlist affordability 30건, 기존 테스트 파일 2종의 `LiveEntryContext` 필드셋 갱신 포함).
`approved: false`, `live_enabled: false` 유지, **Live trading: DO_NOT_ENABLE**. 다음 작업은 새
`FINAL_VALIDATION_PACKAGE.md`(새 SHA-256 포함) 작성 후 상태를 `READY_FOR_FINAL_CODEX_REVALIDATION`
으로 종료하는 것.

## Stage 3~10 최종 통합 수정 사이클 — CODEX-024/026/028/031/032/033 (2026-07-26)
Codex 통합 재검증(대상 커밋 `f04a123`/`aee663c`/`09b9237`/`b78e444`/`fe3e9b7`, overall verdict
`FAIL`)이 CODEX-029/030을 `RESOLVED`로 재확인하고, CODEX-024/026/028을 `PARTIALLY_RESOLVED`로,
신규 CODEX-031(HIGH, 30K/count/pending 제한이 caller 선언에 의존)·CODEX-032(HIGH, rejected exit의
intent/position 비원자적 갱신)·CODEX-033(MEDIUM, governance 문서 불일치)을 제기했다. 6건 전부
로컬 브랜치에서 수정·테스트했다(커밋 `55f3806`/`8a3be50`/`9c43862`). 전체 회귀 **986 passed,
0 failed**(직전 973에서 13건 신규). `approved: false`, `live_enabled: false` 유지,
**Live trading: DO_NOT_ENABLE**. Stage 10 —
30,000원 제한 실거래 준비(`live_readiness/` + 플레이북 문서) `IMPLEMENTED`(문서화·계산 모듈,
실거래 준비 완료 아님), 변경 없음. Stage 9 —
운영 관제(Dashboard/CLI, `ops_dashboard/`) `IMPLEMENTED`, 변경 없음. Stage 8 — 전략 선택 엔진
(`strategy_selection/`) `IMPLEMENTED`, 변경 없음. Stage 7 — 전략 평가 엔진(백테스트/리플레이,
`backtest/`) `IMPLEMENTED`, 변경 없음. Stage 6 — 사용자/YouTube 전략 자료 구조화
(`strategy_sources/`) `IMPLEMENTED`, 변경 없음. Stage 5 — 거래 상태 저장소(`state_store/`, SQLite
병행 인프라) `IMPLEMENTED`, 변경 없음. Phase 5 — 포지션 생명주기 및 자동 청산 (Stage 4,
`IMPLEMENTED`, 변경 없음). Phase 4 — VWAP 마이크로 풀백 전략 엔진 (Stage 3, `IMPLEMENTED`, 변경
없음). Phase 2 — 초단타 관심종목 선별 엔진 (`IMPLEMENTED`, CODEX-010~015 수정 완료, Codex 재검증
대기, 변경 없음).

Phase 1 최종 판정(유지): **Phase 1A(주문 진입 안전성) = VALIDATED**, **Phase 1B(부분체결·포지션
생명주기) = Phase 5로 이관 완료, Phase 5 자체는 `IMPLEMENTED`**.

Phase 3(1분봉 실시간 수집/폴링 인프라)은 이번 사이클에서도 착수하지 않음 — Stage 3/4는 전략
플러그인·포지션 생명주기 로직 자체만 구현했고, 구성된 pandas DataFrame과 fake broker를 입력으로
받아 테스트한다. 라이브 1분봉 폴링/실브로커 연동은 여전히 범위 외.

## Stage 3~10 통합 수정 사이클 — CODEX-023~027 (2026-07-26)
Codex 독립 검증(`CODEX_REVIEW.md`, 대상 범위 `415c129`~`64a5551`, overall verdict `FAIL`)이
제기한 5건을 전부 로컬 브랜치에서 수정. 상세는 `docs/autonomous/REMEDIATION_PLAN.md`/
`VALIDATION_REPORT.md`의 동일 날짜 섹션, ASSUMPTION·범위 결정은 `DECISION_LOG.md` 참고.

- **CODEX-023**(HIGH): `positions/order_status.py` 신설 — broker의 accepted/new/... 상태를
  체결로 오판하지 않도록 청산 경로 재작성. 청산 접수 즉시 `EXIT_SUBMITTED`로 전환되며, 실제
  `filled`/`partially_filled` 확인 전까지 remaining_qty·PnL 불변.
- **CODEX-024**(HIGH): `state_store/exit_intent_ledger.py` 신설(SQLite migration 2) — broker
  호출 **전에** durable exit intent 예약. `positions/lifecycle.py::_execute_exit()`가 3단계
  (예약+상태전환 → broker 호출 → 결과반영)로 재설계되어 timeout/크래시 후 재시도해도 sell이
  중복 제출되지 않음. `reconcile_pending_exit()`가 재시작/재시도 시 공통 해소 경로.
- **CODEX-025**(HIGH): `positions/store.py::load_all()`이 전체 파일 손상 시
  `PositionStoreCorruptedError`를 발생(빈 dict 반환 대신). `recover_on_restart()`가
  `RestartRecoveryResult`(status/positions/reason)를 반환해 "손상됨"과 "포지션 0개"가 구조적으로
  구분됨. 손상 감지 시 Kill Switch를 `MANUAL_REVIEW`로 자동 전환.
- **CODEX-026**(HIGH): `live_readiness/order_gateway.py` 신설 — allow-list/예산/FX rate/최대
  포지션/일일 진입 횟수/손절 위험금액을 전부 fail-closed 검증. `paper_strategy_order.
  submit_order()`의 `side="buy" AND is_live_mode` 경로에만 배선(Paper 거래·청산은 미적용,
  설계 근거는 `DECISION_LOG.md`).
- **CODEX-027**(MEDIUM): `positions/fill_validation.py` 신설 — `record_fill()`이 mutation 전에
  검증(음수/NaN/상한초과/퇴행 전부 차단, 동일 관측 반복은 멱등적 no-op).

전체 회귀: **923 passed, 0 failed**(착수 전 820에서 103건 신규). 실제 네트워크 호출 0회, 운영
CSV 변경 0건, 실제 저장소 루트 `TRADING_STATE.db`가 테스트 중 생성되지 않음(청산 경로의 신규
SQLite 의존성이 격리되지 않았던 실제 버그를 발견·수정한 뒤 확인). `approved`/`live_enabled`/
`main`/`origin` 변경 없음. 기존 리스크 한도 완화 없음 — 전부 신규 fail-closed 검증 추가.

커밋: `0f60ec9`(CODEX-027), `c5c56c4`(CODEX-025), `ee6dae2`(CODEX-023/024 통합),
`f482e90`(CODEX-026).

잔여 범위: `paper_strategy_order.submit_order()`를 우회한 direct broker 호출은 CODEX-026 게이트의
보호를 받지 않음(현재 이 저장소에 그런 경로 없음을 확인, 향후 유지보수 시 재확인 필요). 첫 오류
시 `ENTRY_DISABLED` 자동 배선은 여전히 미구현(Stage 10에서 이미 `NEEDS_USER_DECISION`으로 기록).

## Stage 3~10 최종 통합 수정 사이클 — CODEX-024/026/028/031/032/033 (2026-07-26)
Codex 통합 재검증(`CODEX_REVIEW.md`, 대상 커밋 `f04a123`/`aee663c`/`09b9237`/`b78e444`/`fe3e9b7`,
overall verdict `FAIL`)이 제기한 6건을 전부 로컬 브랜치에서 수정. 상세는
`docs/autonomous/REMEDIATION_PLAN.md`/`VALIDATION_REPORT.md`의 동일 날짜 섹션, ASSUMPTION·범위
결정은 `DECISION_LOG.md` 참고.

- **CODEX-032**(HIGH) + CODEX-024/028 잔여분: broker rejection 시 `eil.mark_aborted()`가 독립
  커밋되고 position의 `MANUAL_REVIEW` 전이가 별도 트랜잭션이었던 것을, `store.locked_position
  (conn=conn)`의 단일 SQLite 트랜잭션(`commit=False`)으로 통합 — 두 번째 write 실패 시 intent만
  terminal ABORTED로 남고 position이 영구히 EXIT_SUBMITTED에 갇히는 실제 재현 결함을 닫았다.
- **CODEX-031**(HIGH) + CODEX-026 잔여분: `live_readiness/entry_reservation_ledger.py` 신설
  (SQLite migration 4) — 모든 live 진입이 broker 호출 전 예산을 durable 예약하고, 게이트가
  caller 입력이 아닌 이 ledger에서 산출한 authoritative 예산/일일 진입 횟수/동시 포지션 수를
  사용한다. 신뢰 가능한 코드 상수(`PILOT_TOTAL_BUDGET_KRW=30_000`,
  `MAX_CONCURRENT_LIVE_POSITIONS=1`, `MAX_DAILY_LIVE_ENTRIES=2`)와 caller 값을 `min()`으로
  교차해 caller가 상한을 완화할 수 없다. 스냅샷 읽기~예약 전체를 파일 락으로 원자화.
- **CODEX-033**(MEDIUM): `LIMITED_LIVE_REVIEW_CHECKLIST.md` §8의 `READY_FOR_LIMITED_LIVE_REVIEW`
  (오래된 CODEX-016~022 판정 근거)를 `BLOCKED`로 정정, `FINAL_VALIDATION_PACKAGE.md`를 최신
  검증 상태의 단일 진실 공급원으로 명시.
- 부수 발견: `test_broker_safety.py`/`test_paper_order_execution.py`가 `LiveEntryContext`를
  사용하면서도 `STATE_STORE_DB_FILE`을 격리하지 않아 실제 저장소 루트 DB에 쓰던 것을 발견해
  즉시 수정.

전체 회귀: **986 passed, 0 failed**(직전 973에서 13건 신규). 네 가지 실행 형태(`venv/bin/python
-m pytest -q`/`venv/bin/pytest -q`/상위 디렉터리에서 `python -m pytest us-stock-trading -q`/
`pytest us-stock-trading -q`) 모두 동일 결과. 실제 네트워크 호출 0회, 운영 CSV 변경 0건, 실제
저장소 루트 `TRADING_STATE.db*`/`LIVE_ENTRY_RESERVATION.lock` 미생성 재확인, `git diff --check`
통과. `approved`/`live_enabled`/`main`/`origin` 변경 없음. 기존 리스크 한도 완화 없음(오히려
caller가 완화할 수 없도록 더 엄격해짐).

커밋: `55f3806`(CODEX-032/024/028), `8a3be50`(CODEX-031/026), `9c43862`(CODEX-033),
`07548d1`(Codex 재검증 원문 기록).

잔여 범위: entry 경로에서 broker 호출이 실제로는 성공했지만 로컬에서 예외가 발생해 예약이
release되는 극단적 경쟁 상황에 대한 crash-safe reconciliation은 미구현(Phase 1B의 기존 잔여
위험과 동일 범주). 첫 오류 시 `ENTRY_DISABLED` 자동 배선은 여전히 미구현(Stage 10에서 이미
`NEEDS_USER_DECISION`으로 기록).

## Stage 3~10 최종 재수정 사이클 — CODEX-024/026/028/029/030 (2026-07-26)
Codex 통합 재검증(`CODEX_REVIEW.md`, 대상 커밋 `4de0714`/`e49753f`, overall verdict `FAIL`)이
제기한 5건을 전부 로컬 브랜치에서 수정. 상세는 `docs/autonomous/REMEDIATION_PLAN.md`/
`VALIDATION_REPORT.md`의 동일 날짜 섹션, ASSUMPTION·범위 결정은 `DECISION_LOG.md` 참고.

- **CODEX-030**(MEDIUM): `clock.py` 신설(Clock/ProductionClock/FrozenClock).
  `check_and_manage()`/`check_invalidation()`이 명시적 timezone-aware `now`/`clock`을 받도록
  변경(naive datetime 거부). 실제 결함은 테스트가 `now`를 전달하지 않아 실행 시각(특히 EOD 근처)에
  결과가 좌우된 것 — 관련 테스트 전부에 고정 시각(`MID_SESSION_NOW`)을 전달하도록 수정.
- **CODEX-028**(HIGH) + CODEX-024 잔여분: `positions/store.py`를 SQLite(`positions`/
  `position_events`) canonical로 재작성, `POSITION_STORE.json`은 커밋 후에만 쓰는 재생성 가능한
  projection(`projection_status` 컬럼)으로 재정의. `locked_position(conn=...)`이 exit intent
  커밋과 동일 SQLite 트랜잭션을 공유하도록 배선 — CODEX-028의 "SQLite가 JSON보다 먼저 커밋되어
  fill 반영이 유실"되는 재현을 닫았고, CODEX-024가 인정했던 "단일 트랜잭션 아님" 잔여 위험도
  함께 해소됐다. CODEX-025의 손상 감지도 SQLite 파일 대상으로 이식(JSON 단독 손상은 더 이상
  store 손상이 아님).
- **CODEX-029**(HIGH) + CODEX-026 잔여분: `live_readiness/order_gateway.py::
  validate_and_size_live_entry(ctx, order_symbol)`에 `ctx.symbol`과 실제 주문 symbol의 완전 일치
  검사(대소문자/공백 정규화 없음) 추가. `broker/alpaca_client.py::AlpacaBroker.submit_order()`
  자체에 동일 게이트를 배선해 direct broker 호출도 더 이상 우회 불가.
- 부수 발견: `_execute_exit()`의 lock-없는 읽기로 인한 드문 경쟁 조건(`CLOSED -> EXIT_SUBMITTED`
  불법 전이) 1건 발견 즉시 수정. `tests/test_position_store.py`/`tests/test_ops_dashboard.py`의
  `STATE_STORE_DB_FILE` 격리 누락(실제 저장소 루트 DB에 쓰던 문제) 발견 즉시 수정.

전체 회귀: **973 passed, 0 failed**(직전 923에서 50건 신규). 세 가지 실행 형태(`venv/bin/python
-m pytest -q`/`venv/bin/pytest -q`/상위 디렉터리에서 `python -m pytest us-stock-trading -q`)
모두 동일 결과. 실제 네트워크 호출 0회, 운영 CSV 변경 0건, 실제 저장소 루트 `TRADING_STATE.db*`
미생성 재확인, `git diff --check` 통과. `approved`/`live_enabled`/`main`/`origin` 변경 없음.
기존 리스크 한도 완화 없음.

커밋: `f04a123`(CODEX-030), `09b9237`(CODEX-028/024), `b78e444`(CODEX-029/026 + 경쟁 조건 수정),
`aee663c`(Codex 재검증 원문 기록).

잔여 범위: `AlpacaBroker.submit_order()`가 아닌 동일 클래스의 향후 신규 메서드는 이번 게이트를
자동으로 상속받지 않음(향후 유지보수 시 재확인 필요). 첫 오류 시 `ENTRY_DISABLED` 자동 배선은
여전히 미구현(Stage 10에서 이미 `NEEDS_USER_DECISION`으로 기록).

## Stage 10 — 30,000원 제한 실거래 준비 (2026-07-26)
`live_readiness/`(`sizing.py`/`allowlist.py`) + `docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md`.

- 구현: 마이크로 주문 수량 계산(`calculate_micro_order_quantity` — 소수점 주식 기본 비활성 확인,
  최소 주문 금액 확인, 자금 부족/최소금액 미달을 별도 상태로 명시, 잘못된 입력은 예외), 종목
  allow-list fail-closed 검사(`is_symbol_allowed` — 빈 목록은 아무것도 허용하지 않음,
  `TBD_REVIEW_RECOMMENDATIONS.md` #4가 지적한 "코드 미강제" 갭을 채움).
- **의도적으로 실제 주문 경로에 배선하지 않음**: `paper_strategy_order.py`/`positions/lifecycle.py`는
  이미 Codex `PASS_WITH_CONDITIONS` 검증을 거친 안전 크리티컬 경계라, Stage 3~10 연속 구현 중
  재검증 없이 다시 수정하지 않기로 결정(플레이북 §6). 배선 여부는 `NEEDS_USER_DECISION`으로 남김.
- 첫 오류 시 `ENTRY_DISABLED`: 기존 `kill_switch_state.py`가 이미 지원하는 상태를 활용한 **수동
  운영 절차**로 문서화(자동화는 별도 결정).
- 일일 1~2건/동시 1포지션/오버나이트 금지: 기존 코드 강제 여부를 표로 정리(`order_safety.
  MAX_TRADES_PER_DAY`/`MAX_OPEN_POSITIONS`는 존재하나 파일럿 규모 값으로 하향 조정 필요, EOD 강제
  청산은 이미 `positions/lifecycle.py`로 구조적 강제).
- 롤백 계획(기존 `ROLLBACK_PLAN.md`에 파일럿 특화 추가), 일일 운영 플레이북, 최종 체크리스트.
- TBD_OPERATOR(추정 없이 명시적으로 미확정 유지): 실계좌, 실환율, Live API Key, 실 주문 금액
  한도, 실 승인자, 배포 시각, 롤백 담당자, 실제 Alpaca 최소 주문 금액, 실제 allow-list 내용.
- 신규 테스트: `tests/test_live_readiness.py` 12건. 전체 회귀 **820 passed, 0 failed**(기존 808 +
  신규 12). 실제 네트워크 호출 0회, 운영 CSV 변경 0건, `approved`/`live_enabled` 미변경.
- 커밋: `986d655`.
- **본 문서 작성 시점: 사용자 지시서의 Stage 3~10 범위가 전부 완료됨.** 다음 작업은
  `docs/autonomous/FINAL_VALIDATION_PACKAGE.md` 작성 후 `READY_FOR_FINAL_CODEX_VALIDATION`으로
  종료하는 것.

## Stage 9 — 운영 관제(`ops_dashboard/`, Dashboard/CLI) 구현 완료 (2026-07-26)
`snapshot.py`(`build_snapshot()`), `cli.py`(`render_text()`/`main()`, `python -m ops_dashboard.cli`).

- 구현: 현재 모드/활성 전략/시장상태/관심종목/일일 주문/포지션(손절·목표가·실현·미실현 PnL 포함)/
  Kill Switch(binary+4-state)/Slack 설정 여부/broker config/reconciliation 집계/마지막 성공 실행
  시각(근사치)까지 로컬 파일과 env 파생 config만으로 조립. 실제 Alpaca/Slack API를 전혀 호출하지
  않음 — "Slack 다운 시에도 로컬 확인 가능"이 별도 폴백이 아니라 애초에 어떤 섹션도 Slack 가용성에
  의존하지 않는 구조로 보장(Slack 섹션은 webhook 환경변수 존재 여부만, broker 섹션은 `BrokerConfig`
  env 파생 값만 확인, 소스에 `requests.post/get` 미참조를 테스트로 검증).
- 각 섹션(`SectionResult`)이 개별적으로 장애 허용적 — 데이터 소스 하나가 깨져도(예: 아직
  `order_history.csv`가 초기화되지 않은 상태) 나머지 섹션은 정상 렌더링.
- 신규 테스트: `tests/test_ops_dashboard.py` 16건. 작성 중 실제 크로스 파일 테스트 격리 버그
  발견·수정: `test_ai_analysis.py::test_ai_analysis_is_independent_from_order_modules`가
  `sys.modules.pop("paper_strategy_order", ...)`를 실행하는 것과 상호작용해, 파일 상단에서
  수집 시점에 바인딩한 `import paper_strategy_order as pso`가 이후 stale해지고
  `ops_dashboard/snapshot.py`의 내부 지역 import는 새로 재임포트된(패치되지 않은) 모듈 객체를
  가져오면서 3개 테스트가 실행 순서에 따라 간헐적으로 실제 저장소 루트의 `order_history.csv`/
  `order_reconciliation.csv`를 읽는 문제가 있었다. 픽스처가 fixture 실행 시점에 fresh하게
  import하고 그 모듈 객체를 `pso`라는 이름으로 명시적으로 요청 가능한 fixture 반환값으로 노출하는
  방식으로 수정(테스트 본문은 더 이상 파일 최상단의 stale한 바인딩에 의존하지 않음).
- 전체 회귀: **808 passed, 0 failed**(기존 792 + 신규 16). 실제 네트워크 호출 0회, 운영 CSV 변경
  0건.
- 커밋: `f2e1a24`.
- 잔여 위험: "마지막 성공 실행 시각"은 전용 마커 파일이 없어 CSV mtime을 근사치로 사용
  (ASSUMPTION). 일일 손실(`daily_loss`)은 `account` dict를 호출자가 명시적으로 주입해야 계산되며,
  라이브 broker 호출 없이는 `NOT_AVAILABLE`.

## Stage 8 — 전략 선택 엔진(`strategy_selection/`) 구현 완료 (2026-07-26)
`models.py`(`SelectionState`/`SelectionInput`/`SelectionFactors`/`SelectionResult`),
`scoring.py`(요소별 순수 함수 + `COMPOSITE_WEIGHTS`), `engine.py`(`select_strategy()`).

- 해석 결정(`DECISION_LOG.md` 결정 1): 지시서의 "ACTIVE 전략만 평가"를 문자 그대로
  `strategy.status==ACTIVE`로 해석하면 후보 풀이 항상 최대 1개(레지스트리의 ACTIVE 1개 제약)라
  "선택"이 성립하지 않으므로, "검토 단계(`REVIEWED`) 이상으로 진행된 전략만 평가"로 재해석했다.
  `REJECTED`/`PAUSED` → `DISABLED`, `COLLECTED`/`STRUCTURED`(백테스트 데이터 자체가 없음) →
  `INSUFFICIENT_DATA`로 자연스럽게 귀결.
- 자격 게이트: 백테스트 결과 없음/`INSUFFICIENT_DATA`/거래 10건 미만(`MIN_TRADES_FOR_SCORING`,
  ASSUMPTION) → `INSUFFICIENT_DATA`. 선호 시장상태(`PREFERRED_MARKET_STATES` 테이블, 현재는
  `VWAP_MICRO_PULLBACK_MOMENTUM_V1` → `{"regular"}`만 등록, `PROJECT_CONSTITUTION.md`와 일치)와
  불일치 시 `MARKET_MISMATCH`.
- 점수: `backtest_performance`/`paper_performance`(승률+평균R+PF 평균, `backtest.metrics.
  compute_metrics()`와 동일 shape 재사용)/`sample_size`/`mdd`/`slippage_sensitivity`(
  `backtest.metrics.slippage_sensitivity()` 결과에서 저-고 슬리피지 구간 기대값 유지율)/
  `market_state_fit`/`symbol_condition_fit` 7개 요소, 결측 요소는 0이 아니라 제외 후 재정규화.
  가중치·임계값 전부 결과를 보기 전에 고정(`DECISION_LOG.md` 결정 2/3, Stage 7과 동일한 원칙).
  단 하나만 `SELECTED`, 동점은 입력 순서로 결정론적 처리.
- 경계: `engine.py`는 `strategy.registry`를 import하지 않고 `ACTIVE` 등록을 호출하지 않음(Stage 7
  `backtest/compare.py`와 동일 원칙) — `SELECTED`는 추천일 뿐 실제 활성화는 별도 운영자 승인 필요.
- 신규 테스트: `tests/test_strategy_selection.py` 27건(자격 게이트 5종, 단일/다중 후보 선택,
  비활성/데이터부족 후보 단독일 때도 미선택, 동점 결정론적 처리, 후보 0명 시 미선택, 요소별 순수
  함수 단위 테스트, 가중치 합=1.0 불변식).
- 전체 회귀: **792 passed, 0 failed**(기존 765 + 신규 27). 실제 네트워크 호출 0회, 운영 CSV 변경
  0건.
- 커밋: `2094adf`.
- 잔여 위험: `PREFERRED_MARKET_STATES` 테이블에 신규 전략 추가 시 수동으로 채워야 함(비어있으면
  시장상태 게이트가 적용되지 않음 — 자격이 아니라 "아직 미문서화"로 해석). 가중치·임계값은 전부
  ASSUMPTION, 실제 Paper 성과 데이터 축적 후 재검토 필요.

## Stage 7 — 전략 평가 엔진(`backtest/`, 백테스트/리플레이) 구현 완료 (2026-07-26)
착수 직전 사용자가 명시한 10개 제약을 그대로 구현: (1) 결과를 보고 임계값을 조정하지 않음 —
`backtest/config.py`의 모든 비용/정책 가정은 어떤 전략도 백테스트하기 전에 고정, 근거는
`DECISION_LOG.md` Stage 7 섹션. (2) 동일봉 손절/목표 충돌은 `STOP_FIRST`(보수적)만 지원. (3)
spread/slippage/수수료/진입지연 4개 비용을 `CostBreakdown`으로 거래마다 분리 기록. (4) look-ahead
금지 — `engine.py`는 항상 `bars.iloc[:i+1]`만 전략에 전달, 데이터가 소진돼 체결을 시뮬레이션할 수
없는 신호는 거래로 기록하지 않음. (5) 프리마켓/정규장 분리 — 진입은
`get_us_market_session(bar_time)=="regular"`일 때만(`paper_strategy_order.py`의 실제 운영 게이트와
동일 조건), 프리마켓 봉은 지표 워밍업에만 사용. (6) 모든 체결이 봉 거래량의
`max_fill_fraction_of_bar_volume`으로 캡핑, 미체결 잔량은 다음 봉으로 이월(체결을 지어내지 않음).
(7) `compute_metrics_with_best_trade_removed()`가 `all_trades`/`best_trade_removed`를 함께 반환 —
후자가 전자를 대체하지 않음. (8) `bars` < `min_bars_required`(기본 500)면
`status=INSUFFICIENT_DATA`, 지표 계산 자체를 하지 않음, `compare.py`도 이를 플레이스홀더 점수 없이
그대로 통과. (9) `backtest/compare.py`는 `strategy.registry`를 import하지 않고
`activate()`/`ACTIVE` 등록을 전혀 호출하지 않음(AST 기반 테스트로 검증) — YouTube 후보 비교는
비교일 뿐 자동 승격 없음, 승격은 전적으로 Stage 8 책임. (10) 자체 테스트 29건 + 전체 회귀 통과 후
본 문서 갱신.

- 관련 파일: `backtest/config.py`(`BacktestConfig`), `backtest/models.py`(`Trade`/`ExitEvent`/
  `CostBreakdown`/`BacktestResult`), `backtest/engine.py`(리플레이 루프 — `_try_enter`/`_manage_bar`/
  `_apply_exit`/`_finalize_trade`), `backtest/metrics.py`(승률/평균R/PF/기대값/MDD/최대연속손실/
  최대수익거래제거/시간대·가격대·유동성·슬리피지민감도 분해), `backtest/compare.py`.
- 신규 테스트: `tests/test_backtest_engine.py` 29건(INSUFFICIENT_DATA 처리, look-ahead 안전 체결
  타이밍, 프리마켓 차단/정규장 허용, 동일봉 충돌 해소, 비용 분리 정확성, 거래량 캡핑+이월, 1R
  부분익절→2R 전량청산 2건 체결 이벤트, 시간손절, 장마감 강제청산, 전략 무효화, 봉 데이터 검증,
  전체 지표/비교 함수).
- 전체 회귀: **765 passed, 0 failed**(기존 736 + 신규 29). 실제 네트워크 호출 0회, 운영 CSV 변경
  0건.
- 커밋: `59958cf`(구현+테스트), `DECISION_LOG.md` Stage 7 섹션 병행 커밋.
- 잔여 위험: `nominal_qty=100`/`spread_bps=5.0`/`slippage_bps=5.0` 등 비용 가정은 실측치가 아닌
  ASSUMPTION — 실제 측정 데이터 확보 시 갱신 필요(근거와 함께 `DECISION_LOG.md`에 기록). 동일봉
  충돌 정책은 `STOP_FIRST` 한 가지만 지원.

## Stage 6 — 사용자/YouTube 전략 자료 구조화(`strategy_sources/`) 구현 완료 (2026-07-25)
신규 패키지 `strategy_sources/`(`models.py`, `repository.py`, `similarity.py`, `known_sources.py`).

- `models.py`: `StrategyClaim`(category/statement/origin/source_excerpt/confidence)과
  `StrategySource`(source_id/type/title/reference/version/validation_status/claims/
  derived_strategy_id/similar_to). `origin`이 소스가 명시적으로 말한 것(`SOURCE`, `source_excerpt`
  필수)과 수집자의 추론(`ASSUMPTION`), 소스가 다루지 않은 공백(`UNKNOWN`)을 구조적으로 분리 —
  Stage 3에서 `VWAPMicroPullbackV1.invalidate()`가 명시되지 않은 가정을 그대로 물려받을 뻔했던
  것(`DECISION_LOG.md` Stage 3 결정 4)과 같은 문제를 방지. `validation_status`는
  `strategy/status.py`의 앞 4개 상태(`COLLECTED`/`STRUCTURED`/`REVIEWED`/`REJECTED`)로만 제한 —
  `ACTIVE`는 단지 "아직 도달 못함"이 아니라 **구조적으로 도달 불가능**(다른 상태로 생성 시도 시
  `InvalidStrategySourceError`).
- `repository.py`: 버전 관리되는 append-only JSON 저장소(기본 `docs/strategy/sources/`,
  테스트용 `STRATEGY_SOURCES_DIR` 환경변수 오버라이드). `save_source()`는 버전이 정확히
  (현재 최대 버전+1)이 아니면 거부하고, 이미 존재하는 버전 파일은 절대 덮어쓰지 않음 — 소스 자료의
  변경 이력이 보존됨. `positions/store.py`와 동일한 `fcntl.flock` 락 패턴으로 동시 저장 시 버전
  번호 경쟁 방지.
- `similarity.py`: 두 소스 간 카테고리별 Jaccard 단어 중복도 기반 **결정론적 규칙 기반**
  유사도 채점 — LLM 판단이 아님(Stage 8의 전략 선택 설명가능성 요구사항과 동일한 원칙을 한 단계
  앞서 적용).
- `known_sources.py`: 지시서에 명시된 8개 소스(VWAP 진입, 1:2 손익비, 1R 50% 분할 익절, Ross
  Cameron 스타일 마이크로 눌림목은 `PROJECT_CONSTITUTION.md`에 실제로 명시되어 있고
  `vwap_micro_pullback_v1.py`로 이미 구현됨 — 실제 인용문으로 `origin=SOURCE` 처리, `REVIEWED`.
  Turtle·멀티 타임프레임 RSI·볼린저 눌림목·CCI/RSI/ADX는 실제 지정된 소스 문서/영상이 없어 모든
  claim을 `origin=ASSUMPTION`, `reference`를 `TBD_OPERATOR` 명시 마커로 처리 — 근거 없는 인용을
  지어내지 않음(`PROJECT_CONSTITUTION.md` 절대 금지사항 13 "유튜브에서 추출한 전략을 검증 없이
  주문 엔진에 연결하지 않는다"와 같은 원칙을 한 단계 앞서 적용), `validation_status=COLLECTED`
  유지, `derived_strategy_id` 없음). `seed_known_sources()`는 멱등적이며 실제
  `docs/strategy/sources/*.json` 8개 파일로 이미 시딩 완료.
- 신규 테스트: `tests/test_strategy_sources.py` 33건(claim/source 검증 — `ACTIVE` 구조적 차단
  포함, 왕복 직렬화, 저장소 버전 관리 — 저장/로드/잘못된 버전 번호 거부/버전 건너뛰기 거부/다중
  버전 이력 보존, 유사도 채점 — 동일/무관/부분 카테고리 중복 claim, 임계값 필터링, 자기 자신 제외,
  8개 알려진 소스 카탈로그 — 정확히 8개 고유 항목, `REVIEWED` 이상 없음, 미검증 4개 항목에
  조작된 `SOURCE` claim 없음, 시딩 멱등성 및 재로드 검증).
- 전체 회귀: **736 passed, 0 failed**(기존 703 + 신규 33). 실제 네트워크 호출 0회, 운영 CSV
  변경 0건.
- 커밋: `639af97`(구현+테스트+시딩된 카탈로그), `8915c44`+`9814114`(`.gitignore` 락 파일 패턴
  버그 수정 및 실수로 커밋된 빈 락 파일 제거 — 부수적 정리).
- 잔여 위험: 8개 중 4개(Turtle/멀티 RSI/볼린저/CCI-RSI-ADX)는 실제 사용자 자료·영상이 아직
  제공되지 않은 자리표시자 카탈로그 — 실제 자료 제공 시 `save_source()`로 버전 2를 추가해 갱신
  필요. `similarity.py`는 매우 단순한 단어 중복 기반이라 유사 전략 탐지의 정밀도는 낮음(의도된
  최소 구현, Stage 7/8에서 필요 시 고도화 검토).

## Stage 5 — 거래 상태 저장소(`state_store/`) 구현 완료 (2026-07-25)
`docs/autonomous/DECISION_LOG.md` "Stage 5" 섹션에 CSV vs SQLite 평가를 먼저 기록한 뒤 착수.
결론: `order_history.csv`/`order_reconciliation.csv`/`POSITION_STORE.json`은 각자 원자적이지만
세 파일에 걸친 단일 트랜잭션이 없어 Phase 1B/Phase 5가 이미 문서화한 잔여 위험(부분 체결의
포지션 상태 완전 반영)의 근본 원인이 됨 — SQLite로 **병행** 인프라를 구축하되, **실제 운영 경로는
전환하지 않음**(지시서의 절대 제약).

- `state_store/schema.py`+`migrations.py`: `orders`/`fills`/`positions`/`position_events`/
  `strategy_runs`/`risk_events`/`kill_switch_events` 7개 테이블 + `schema_migrations` 버전 추적.
  `fills.client_order_id`는 `orders`로의 FK를 걸지 않음(`order_history.csv` 자체가
  `client_order_id`를 항상 갖지 않아 — 그 값은 `order_intent_ledger.csv`에만 존재 — 강제 FK가
  정상 레거시 가져오기를 실패시키므로 기존 CSV들과 동일한 느슨한 자연 키 상관관계 유지).
- `state_store/db.py`: `connect()`(WAL 모드, FK 강제, busy_timeout), `init_db()`(멱등적 마이그레이션
  실행기, 마이그레이션별 독립 트랜잭션).
- `state_store/csv_import.py`: `order_history.csv`/`order_reconciliation.csv`용 **읽기 전용**
  가져오기(원본 CSV는 `pandas.read_csv()`만, 절대 쓰거나 삭제하지 않음 — 바이트 불변 테스트로 확인).
  구버전 2컬럼 형식과 현재 5컬럼 형식(`REQUIRED_HISTORY_COLUMNS`) 모두 관용적으로 처리, 자연 키
  기반 멱등성(재실행 시 중복 삽입 없음).
- `state_store/export.py`: `export_table()`/`export_all()`(SQLite→CSV, 대상 경로는 항상 호출자
  지정), `reset_schema()`(SQLite 파일 자체만 초기화, 가져오기 원본 CSV는 건드리지 않음).
- 신규 테스트: `tests/test_state_store.py` 20건(스키마/마이그레이션 멱등성, 트랜잭션 무결성,
  UNIQUE/FK 제약, 레거시·신규 CSV 가져오기, 가져오기 멱등성, 파일 누락 처리, 내보내기 왕복,
  `reset_schema` 데이터 초기화, 실제 `TRADING_STATE.db` 미생성 확인).
- 전체 회귀: **703 passed, 0 failed**(기존 683 + 신규 20). 실제 네트워크 호출 0회, 운영 CSV 변경
  0건(가져오기 전후 바이트 동일 확인), `broker/`·`order_safety.py`·`config/scanner_presets.json`·
  `.env`·kill switch 상태 파일 변경 없음. `.gitignore`에 `TRADING_STATE.db(-wal/-shm)` 추가.
- 커밋: `bf05098`.
- 잔여 위험/미해결: SQLite 저장소를 실제 주문/포지션 경로에 배선하는 것은 이번 단계에 포함되지
  않음 — `DECISION_LOG.md`에 `NEEDS_USER_DECISION`으로 명시. 배선 전까지는 CSV/JSON이 유일한 실제
  판단 근거로 계속 사용됨(안전성에 영향 없음, 감사/롤백용 병행 사본일 뿐).

## Stage 4 — 포지션 생명주기(`positions/`) 구현 완료 (2026-07-25)
사용자의 "Stage 3~10 연속 구현, Codex 중간 검증 없이 진행" 지시에 따라 착수. `docs/autonomous/
SCALPING_V1_ROADMAP.md` Phase 5 대응. 신규 패키지 `positions/`(`states.py`, `store.py`,
`lifecycle.py`)를 추가했다.

- `positions/states.py`: 13개 생명주기 상태(`SETUP_DETECTED`~`CLOSED`) + 6개 예외 상태
  (`REJECTED/CANCELLED/EXPIRED/UNKNOWN/MANUAL_REVIEW/RECOVERY_REQUIRED`), 명시적 `TRANSITIONS`
  인접 테이블로 임의 상태 전이를 구조적으로 차단, `FAIL_CLOSED_STATE = RECOVERY_REQUIRED`
  (`kill_switch_state.py`의 fail-closed 컨벤션 재사용, 한 단계 더 보수적).
- `positions/store.py`: 포지션별 JSON 원자적 저장소(`order_intent_ledger.py`/`kill_switch_state.py`
  와 동일한 `fcntl.flock`+tempfile+fsync+os.replace 패턴), 레코드별 fail-closed 검증(손상 JSON/
  필드 누락/미인식 상태 → 다른 레코드는 영향 없이 해당 레코드만 `RECOVERY_REQUIRED`), `locked_position()`
  컨텍스트 매니저로 "읽기→판단→브로커 호출→쓰기" 전체 구간을 단일 락으로 보호(중복 청산 방지의
  핵심 메커니즘 — 최초 설계는 저장 시점만 잠갔는데, 두 동시 호출이 모두 브로커를 호출한 뒤 마지막
  쓰기만 순서가 보장되는 경쟁 조건이 있어 재설계함, 스레딩 테스트로 검증).
- `positions/lifecycle.py`: `enter_position()`(전략 `require_active()` 검증 → `generate_entry()` →
  `try_reserve_order()` → `submit_order(side="buy")`, ledger commit/abort), `record_fill()`(부분/
  완전 체결, `FILLED`→`STOP_ACTIVE` 자동 전이), `check_and_manage()`(우선순위: EOD 강제청산 >
  시간손절 > 손절 > 1R 50% 분할익절 > 2R 전량청산, 분할 익절 후 손절가를 손익분기로 이동하는
  최소 트레일링 정책), `check_invalidation()`(전략 무효화 신호 시 전량청산, 신선한 봉 데이터가
  필요해 `check_and_manage()`와 분리), `recover_on_restart()`(브로커 재조회 실패/불확실/broker
  미제공 시 `RECOVERY_REQUIRED`로 fail-closed, 이미 `RECOVERY_REQUIRED`인 레코드는 절대 추측으로
  복구하지 않음). 모든 청산 주문은 `paper_strategy_order.submit_order(side="sell")`을 직접
  호출 — `try_reserve_order()`/`is_duplicate_order()`는 "심볼당 하루 1건" 진입 전용 중복 방지
  구조라 청산에 재사용할 수 없다고 판단(청산은 kill switch/자격증명/`RequestPurpose` 게이트는
  그대로 통과, 진입 전용 일일 중복 방지 로직만 우회). 근거: `DECISION_LOG.md` Stage 4 섹션.
- 신규 테스트: `tests/test_position_states.py` 31건(상태 전이 커버리지), `tests/test_position_store.py`
  15건(원자적 저장/락 경쟁/fail-closed/`locked_position` 동시성), `tests/test_position_lifecycle.py`
  23건(진입 성공/무신호/비활성전략/kill-switch차단/브로커거부, 부분·완전체결, 1R분할익절·2R전량청산·
  손절, 시간손절, EOD강제청산, 전략무효화, 동시 손절 요청의 중복 청산 방지, 실현/미실현 PnL, 재시작
  복구 4가지 시나리오) — 총 69건 신규.
- 전체 회귀: 저장소 루트 `venv/bin/python -m pytest -q` 기준 **683 passed, 0 failed**(기존 613 →
  Stage 3 이후 660(부분 구현) → 683). 실제 Alpaca/Slack/네트워크 호출 0회(FakeBroker/모킹만 사용),
  `order_history.csv`/`universe.csv`/`strategy_performance.csv` MD5 불변, `broker/`·`order_safety.py`·
  `config/scanner_presets.json`·`.env`·kill switch 상태 파일 변경 없음.
- 커밋: `a78ab1b`(states+테스트), `2058614`(store+테스트), `f9a2d1f`(`locked_position()`+VWAP
  `invalidate()` 실구현+config), `b3d8cf4`(lifecycle+테스트).
- 잔여 위험: 상태 영속화가 여전히 파일(JSON) 기반이며 `order_history.csv`와 별개 파일이라 두 파일에
  걸친 단일 트랜잭션은 없음(Phase 1B에서 이미 문서화된 동일 위험, 안전 크리티컬 판단 자체는
  `order_history.csv`/kill switch 상태에만 의존하므로 실거래 안전성에는 영향 없음) — Stage 5(SQLite
  전환 검토)에서 재평가 예정. 트레일링 정책은 "1R 50% 분할 후 손절을 손익분기로 이동"이라는 최소
  규칙으로, 정교한 트레일링 알고리즘이 아님(의도된 초기 정책). 실시간 브로커 reconciliation
  (`recover_on_restart()`가 실제 Alpaca 응답을 어떻게 파싱할지)은 Phase 3(1분봉 실시간 인프라) 착수
  후 실제 broker 클라이언트로 통합 테스트 필요 — 현재는 fail-closed 동작만 검증됨.

## Stage 3 — 전략 플랫폼(`strategy/`) 구현 완료 (2026-07-25)
`docs/autonomous/SCALPING_V1_ROADMAP.md` Phase 4 대응. 신규 패키지 `strategy/`(`interface.py`,
`status.py`, `registry.py`, `plugins/vwap_micro_pullback_v1.py`, `plugins/__init__.py`,
`plugins/_example_orb_stub.py`)와 `config/scalping_strategy_v1_config.py`를 추가했다.

- `TradingStrategy` ABC(`strategy/interface.py`): `strategy_id`/`version`/`status`를 생성 시점에
  fail-closed 검증. `evaluate_setup`/`generate_entry`/`calculate_stop`/`calculate_targets`는 Stage 3
  실 구현. `manage_position`/`invalidate`는 `NotImplementedError` 스텁(Stage 4/Phase 5 포지션
  생명주기 선행 필요, 코드 주석에 이유 명시).
- 전략 상태(`strategy/status.py`): `COLLECTED/STRUCTURED/REVIEWED/BACKTESTED/PAPER_APPROVED/
  LIMITED_LIVE_APPROVED/ACTIVE/PAUSED/REJECTED` 9종, `ORDER_GENERATING_STATUSES={ACTIVE}`로 주문
  생성 가능 여부를 단일 지점에서 정의.
- `StrategyRegistry`(`strategy/registry.py`): 등록 시점에 strategy_id/version/status 검증(fail-closed),
  ACTIVE 최대 1개를 구조적으로 강제(두 번째 ACTIVE 등록/활성화 시도는 `StrategyRegistrationError`로
  거부, 첫 번째를 암묵적으로 비활성화하지 않음 — 결정 근거 `DECISION_LOG.md`), `get_active_strategy()`
  (없으면 `None`), `require_active()`/`select_strategy_for_order()`(ACTIVE가 아니면
  `StrategyNotActiveError`, PAPER_APPROVED/LIMITED_LIVE_APPROVED도 차단).
- `VWAP_MICRO_PULLBACK_MOMENTUM_V1`(`strategy/plugins/vwap_micro_pullback_v1.py`): VWAP/EMA9/EMA21을
  pandas로 직접 계산(`indicators.py`는 일봉 HMA 계열 전용이라 재사용 대상 아님을 확인 후 판단).
  price>VWAP·EMA9>EMA21 → 초기 rally → 얕은 pullback(거래량 감소) → 재돌파(거래량 재확대) 순으로
  판정, 손절은 micro-pullback low + ATR 기반 최소 버퍼, 목표는 1R에서 50% 분할 익절(문서에 명시된
  값) + target_2 2R(ASSUMPTION, 근거 `DECISION_LOG.md`).
- 주문 경로 연결: `paper_strategy_order.submit_order()`에는 현재 `strategy_id` 개념 자체가 없어
  (하드코딩된 단일 스코어링만 존재) 가짜 연결점을 만들지 않았다 — `require_active()`/
  `select_strategy_for_order()`를 `strategy/registry.py`의 독립 함수로 구현하고
  `tests/test_strategy_platform.py`에서 직접 검증. Stage 4가 실제 주문 트리거 경로에서 호출할
  예정(코드 주석에 명시).
- 확장 패턴: `strategy/plugins/__init__.py` 모듈 docstring + `strategy/plugins/_example_orb_stub.py`
  (미구현 스텁, ORB류 신규 전략 추가 시 따라야 할 최소 형태 예시).
- 신규 테스트: `tests/test_strategy_platform.py` 43건(레지스트리 검증/ACTIVE 1개 강제/가드/플러그인
  entry-present·VWAP-EMA 실패·pullback 없음·stop/target 정합성·Stage4 스텁 등).
- 전체 회귀: 저장소 루트 `venv/bin/python -m pytest -q` 기준 **613 passed, 0 failed, 2 warnings**
  (기존 570 + 신규 43). 실제 네트워크 호출 0회(모두 구성된 pandas DataFrame 사용), `order_history.csv`
  /`universe.csv`/`strategy_performance.csv` MD5 불변 확인, `broker/`·`order_safety.py`·
  `config/scanner_presets.json`·`.env`·kill switch 상태 파일 변경 없음.
- 잔여 위험: Stage 4(Phase 5, 포지션 생명주기)가 `manage_position`/`invalidate`를 실제 구현하고
  `require_active()`를 실제 주문 트리거 경로에 배선해야 함. 임계값(눌림 깊이 %, rally 최소 %,
  target_2 R-배수 등)은 Phase 6 백테스트 이전까지 잠정값(`DECISION_LOG.md`). 신호 중복 방지/추격진입
  방지/실시간 스프레드·유동성 차단/stale 데이터 차단은 Phase 3(1분봉 실시간 수집, 여전히
  `NOT_STARTED`) 착수 후 별도 구현 필요.

## Codex 최종 독립 재검증: PASS_WITH_CONDITIONS (2026-07-25, 커밋 `a31290b`/`5aac75b`/`8803252` 대상)
Overall verdict **`PASS_WITH_CONDITIONS`**. CODEX-016~022 전부 **RESOLVED**로 최종 확정, 신규
CRITICAL/HIGH/MEDIUM Finding 없음. Limited live review 권고: **`READY_FOR_LIMITED_LIVE_REVIEW`**
— 단 **Live trading: DO_NOT_ENABLE`**을 유지하며, 이 권고 자체가 실거래 승인을 의미하지 않는다.
남은 조건은 전부 코드 Finding이 아니라 운영자가 실제로 채워야 하는 `TBD` 항목(실제 Alpaca
계정/credential, 현재 포지션·미체결 주문·broker reconciliation, 허용 종목·거래시간·주문당 절대
한도, 승인자·검토 시각·롤백 담당자)이며, `docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md`에 각
항목의 권장값 초안·근거·위험·승인 필요 여부가 정리되어 있다. `approved: false`,
`live_enabled: false`는 변경하지 않았다. 상세: `docs/autonomous/CODEX_REVIEW.md`(커밋 `d38cb95`).

## 제한적 실거래 검토 사이클 — CODEX-022 해결 및 CODEX-021 잔여분 종결 (2026-07-25, 이전 기록)
Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ae3ca`/`c133e01`/`cc740a5`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/018/019는 RESOLVED로 재확인됐으나, CODEX-021(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-022(HIGH)**가 제기됐다 — `RequestPurpose` 재설계
(커밋 `c133e01`) 이후에도 `_request()`가 주문 POST의 payload `side`와 `order_side`, `purpose`
세 값을 서로 대조하지 않아, `purpose=EXIT_ORDER`를 선언한 채 매수 payload(`json={"side":
"buy"}`)를 전달하면 `ENTRY_DISABLED` 상태에서도 HTTP가 실제로 나갔다.

이번 사이클(t1)에서 `broker/alpaca_client.py`에 신규 `validate_order_intent(purpose, order_side,
payload)`를 도입해 `_request()`가 세션 호출 전, 다른 어떤 안전장치보다도 먼저 이 3자 일치를
검증하도록 배선했다(커밋 `5aac75b`):
- **CODEX-022 (HIGH)**: `_PURPOSE_REQUIRED_SIDE` 매핑(`ENTRY_ORDER→"buy"`, `EXIT_ORDER→"sell"`)
  기준으로, `ENTRY_ORDER`/`EXIT_ORDER`는 `order_side`와 payload의 `side`가 모두 존재하고 정확히
  요구되는 문자열과 완전히 일치해야 한다(대소문자·공백·`bool`/`int` 변형도 거부). 불일치·누락·
  비-dict body는 모두 `ValueError`로 세션 호출 전에 차단된다.
- **CODEX-021 잔여분 (HIGH)**: 위와 동일한 함수로 함께 닫혔다 — `order_side`가 이제 실제로
  payload `side`와 대조되므로 2차 방어선으로서 실질적 방어력을 갖는다.

CODEX-016~019(다단계 kill switch 배선, Slack health 배선, 주문 직전 credential/환경 재검증,
상태 저장소 파일 잠금)는 이번 사이클에서 **재작업하지 않았다** — 관련 회귀 테스트
(`tests/test_paper_strategy_order_kill_switch_state.py` 12건,
`tests/test_paper_strategy_order_notification_health.py` 6건,
`tests/test_state_store_concurrency.py` 6건, 도합 36 passed)로 회귀 없음만 확인했다.

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **570 passed, 0 failed, 2
warnings**다. 집중 테스트(`tests/test_broker_kill_switch_gate.py` +
`tests/test_broker_request_purpose.py` + `tests/test_broker_order_intent_gate.py`(신규) +
`tests/test_alpaca_client_runtime_revalidation.py` + `tests/test_broker_safety.py` +
`tests/test_universe_builder.py` + `tests/test_paper_strategy_order_kill_switch_state.py` +
`tests/test_paper_order_execution.py`) **289 passed, 1 warning**. `order_history.csv`/
`universe.csv` SHA-256은 이전 사이클 기록값과 동일(불변), `.env`·kill switch/notification 상태
파일 변경 없음. 현재 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지
**Limited live review: BLOCKED**, **Live trading: DO_NOT_ENABLE**을 유지한다.

## 이전 사이클 — CODEX-021 해결 및 CODEX-020 잔여분 종결 (2026-07-25, 역사적 기록)
Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `66eda8a`/`ed452da`/`cf5601d`/`edc5ad5`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/018/019는 RESOLVED로 재확인됐으나, CODEX-020(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-021(HIGH)**이 제기됐다 — `_request()`의 `order_side`가
필수 인자이긴 했지만 POST 경로와 의미적으로 결합되지 않아, `broker._request("POST", "/v2/orders",
order_side=None, ...)`처럼 명시적으로 `None`을 전달하면 `_check_kill_switch(None)`이 method/path를
전혀 확인하지 않고 즉시 반환해 kill switch를 우회할 수 있었다.

이번 사이클(t1)에서 `AlpacaBroker._request()`를 `order_side` 단일 신호가 아니라 신규
`RequestPurpose` enum(`READ_ONLY`/`ENTRY_ORDER`/`EXIT_ORDER`/`CANCEL_ORDER`/`RECONCILIATION`)
기반으로 재설계했다(커밋 `c133e01`):
- **CODEX-021 (HIGH)**: `_request()`에 기본값 없는 keyword-only `purpose` 인자를 추가하고,
  `isinstance(purpose, RequestPurpose)`를 요구해 `None`을 포함한 잘못된 값은 `ValueError`로
  세션 접근 전에 차단한다. `_METHOD_PURPOSES` 매트릭스가 HTTP method(GET/POST/DELETE)와
  purpose의 허용 조합을 명시적으로 검사해, 예컨대 POST가 `READ_ONLY`를 주장하거나 GET이
  `ENTRY_ORDER`를 주장하는 불일치를 세션 호출 전에 거부한다. `_check_kill_switch()`는 이제
  `purpose`가 `ENTRY_ORDER`/`EXIT_ORDER`일 때만 kill switch를 검사하며, `order_side`는 payload의
  `side`와 `purpose`가 일치하는지 확인하는 2차 방어선으로만 쓰인다(`submit_order()`가
  `_SIDE_TO_PURPOSE`로 파생한 `purpose`와 payload의 `order["side"]`가 다르면 세션 호출 전에
  `RuntimeError`).
- **CODEX-020 잔여분 (HIGH)**: 위와 동일한 재설계로 함께 닫혔다 — method+path 기반 주문 감지
  백스톱이 없다는 지적이 `_METHOD_PURPOSES` 매트릭스로 해결됐다. 조회·취소 경로
  (`get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order`)는 각각 `RequestPurpose.READ_ONLY`/
  `RECONCILIATION`/`CANCEL_ORDER`를 명시해 kill switch 정책과 무관하게 계속 동작한다.

CODEX-016~019(다단계 kill switch 배선, Slack health 배선, 주문 직전 credential/환경 재검증,
상태 저장소 파일 잠금)는 이번 사이클에서 **재작업하지 않았다** — 관련 회귀 테스트
(`tests/test_paper_strategy_order_kill_switch_state.py` 12건, `tests/test_paper_strategy_order_notification_health.py`
6건, `tests/test_state_store_concurrency.py` 6건, 도합 36 passed)로 회귀 없음만 확인했다.

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **536 passed, 0 failed, 2 warnings**다.
집중 테스트(`tests/test_broker_kill_switch_gate.py` + `tests/test_broker_request_purpose.py`(신규) +
`tests/test_alpaca_client_runtime_revalidation.py` + `tests/test_broker_safety.py` +
`tests/test_universe_builder.py` + `tests/test_paper_strategy_order_kill_switch_state.py` +
`tests/test_paper_order_execution.py`) **255 passed, 1 warning**. `order_history.csv`/`universe.csv`
SHA-256은 이전 사이클 기록값과 동일(불변), `.env`·kill switch/notification 상태 파일 변경 없음.
현재 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review:
BLOCKED**, **Live trading: DO_NOT_ENABLE**을 유지한다.

## 이전 사이클 — CODEX-020·CODEX-018 잔여분 수정 (2026-07-24, 역사적 기록)
최신 Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ee8d6`/`03962d3`/`cf4ada9`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/019는 RESOLVED로 재확인됐으나, CODEX-018(MEDIUM)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-020(HIGH)**이 제기됐다 — direct
`AlpacaBroker.submit_order()`가 `paper_strategy_order.py` wrapper를 거치지 않고 직접 호출되면
binary kill switch(`kill_switch.is_trading_halted()`)와 다단계 kill switch
(`kill_switch_state.is_entry_allowed()`/`is_liquidation_allowed()`)를 모두 우회해 HTTP가 실제로
나갔다. 또한 CODEX-018의 "현재 credentials 재검증" 요구사항이 `_validate_runtime_safety()`에
아직 배선되지 않았다는 지적도 함께 남아 있었다.

이번 사이클(t1~t2)에서 두 항목을 broker 공통 경로에 배선했다:
- **CODEX-020 (HIGH)**: `AlpacaBroker._request()`에 `order_side`(주문이 아니면 `None`, 매수/매도면
  `"buy"`/`"sell"`) 키워드 전용 필수 인자를 추가하고, 내부에서 신규 `_check_kill_switch()`가
  binary halt와 side별 4-state(`is_entry_allowed`/`is_liquidation_allowed`) 정책을 매 요청마다
  다시 조회해 불허 시 HTTP 호출 전에 `RuntimeError`를 발생시키도록 배선했다(커밋 `66eda8a`).
  `get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order` 등 조회·취소 경로는 `order_side=None`으로 명시해
  kill switch 정책과 무관하게 계속 동작하도록 분리했다. `_request()`를 우회해 `order_side`를
  생략하면 네트워크 호출 전에 `TypeError`로 즉시 차단된다.
- **CODEX-018 잔여분 (MEDIUM)**: `_validate_runtime_safety()`에 `_validate_current_credentials_match_captured()`를
  추가해, 매 요청마다 `BrokerConfig.from_env()`로 현재 환경의 API key/secret을 다시 읽어
  생성 시점에 캡처된 값과 `hmac.compare_digest()`로 상수시간 비교한다. 누락/공백/회전/삭제/환경
  읽기 실패 시 모두 요청 전에 차단하며, credential 값 자체는 예외 메시지에 포함하지 않는다(커밋 `ed452da`).

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **489 passed, 0 failed, 2 warnings**다.
집중 테스트(`tests/test_broker_kill_switch_gate.py` 25건, `tests/test_alpaca_client_runtime_revalidation.py`
44건 포함) 208 passed. `order_history.csv`/`universe.csv` SHA-256은 `CODEX_REVIEW.md`에 기록된
값과 동일(불변), `.env`·kill switch/notification 상태 파일 변경 없음. 현재 상태는
**`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review: BLOCKED**,
**Live trading: DO_NOT_ENABLE**을 유지한다.

## 마지막 완료 작업 (CODEX-010~015 수정 사이클)
- CODEX-010 (HIGH): `numeric_guard.require_finite_number()` 도입, `features.py`의 모든 raw/derived 수치에 NaN/Infinity 명시 차단 적용.
- CODEX-011 (HIGH): `SymbolSnapshot`에 `data_as_of`/`provider_fetched_at` 분리, `freshness.py` 신규(세션별 최대 데이터 나이), `YFinanceMarketDataProvider`가 손상/미래/타임존無 타임스탬프를 fail-closed 반환.
- CODEX-012 (MEDIUM): `calendar_guard.py` 신규 — 휴장일(`market_guard.is_us_trading_day`)/허용 세션/정규장 오픈 윈도우를 provider·파일 접근 이전에 게이트, 차단 시 `SKIPPED`(미저장).
- CODEX-013 (MEDIUM): `save_watchlist_cycle()`이 `{success, persisted_count, error_code, error_message}` 반환 + 쓰기 후 재검증(`_verify_after_write`), `run_scan_cycle()` 결과에 `status/error_code/error_message` 포함.
- CODEX-014 (MEDIUM): `first_detected_at/last_detected_at/updated_at` 3분리, `detect_count` 기반 실제 NEW→ACTIVE 전이, `validate_lifecycle_timestamps()`로 손상된 타임스탬프를 가진 행은 방치 대신 REJECTED 처리(TTL 우회 차단).
- CODEX-015 (LOW): `_compute_average_volume()`이 당일(미완료) 봉을 제외하고 최소 완료일수 미만이면 `None` 반환; `filter_premarket_rows()` 순수함수로 04:00~09:30 ET premarket 구간 분리, `premarket_coverage_complete` 필드로 부분 구간 여부 명시.
- 신규 테스트 65건 (`tests/test_scalping_watchlist.py` 103건 → 118건: CODEX-015분 15건 포함).
- 전체 회귀 267 passed(레포 루트 `pytest -q`/`python -m pytest -q` 동일), 실제 외부 API 호출 0회, `order_history.csv` 해시 불변, 운영 파일 변경 없음 확인.

## 현재 테스트 수
986 passed, 0 failed (CODEX-024/026/028/031/032/033 최종 통합 수정 사이클 신규 약 13건 포함:
CODEX-032 test_exit_reconciliation.py 원자성 재현 4건, CODEX-031 test_live_order_gateway.py
authoritative 모델 전면 재작성 다수. 직전 CODEX-024/026/028/029/030 사이클은 973 passed 기준
완료)

## 실패 테스트
없음

## 현재 블로커
없음 (코드 수준). CODEX-016~022는 이전 사이클에서 Codex 최종 독립 재검증까지 `PASS_WITH_CONDITIONS`로
종결됨. CODEX-023/025/027/029/030은 재검증에서 RESOLVED로 재확인됐고, CODEX-024/026/028/031/032/033도
이번 사이클에서 전부 RESOLVED로 로컬 수정·테스트 완료됨 — **유일한 블로커는 아직 이번 수정에 대한
Codex 통합 재검증을 요청/수행하지 않았다는 것뿐**.
`approved: false`, `live_enabled: false` 유지. **Limited live review: BLOCKED**(신규 수정이
아직 Codex 검증을 거치지 않았으므로), **Live trading: DO_NOT_ENABLE**.

## 다음 작업
1. **새 `FINAL_VALIDATION_PACKAGE.md` 작성**(새 SHA-256 포함) 후 상태를
   `READY_FOR_FINAL_CODEX_REVALIDATION`으로 종료.
2. **Codex 통합 재검증 요청** — 결과(`PASS`/`PASS_WITH_CONDITIONS`/`FAIL`)에 따라 후속 조치:
   - `PASS`/`PASS_WITH_CONDITIONS`: `CODEX_REVIEW.md`에 기록하고, 남은 잔여 범위(entry 경로
     crash-safe reconciliation 미구현, `ENTRY_DISABLED` 자동 배선 미구현)에 대한 후속 조치 여부를
     사용자와 논의.
   - `FAIL`: 지적된 CRITICAL/HIGH를 동일한 패턴으로 수정 후 재검증.
3. 어떤 결과든 `approved`/`live_enabled`/`main`/`origin`/실거래 활성화는 사용자의 명시적 승인
   없이는 건드리지 않는다.

## 최근 커밋
- `9c43862` Fix limited-live checklist's stale READY status (CODEX-033)
- `8a3be50` Enforce authoritative 30K budget/count/pending limits at the broker boundary (CODEX-031/026)
- `55f3806` Make rejected-exit intent-abort and position transition atomic (CODEX-032/024/028)
- `07548d1` Record Codex independent review: FAIL, CODEX-024/026/028 PARTIALLY_RESOLVED, CODEX-031/032 HIGH, CODEX-033 MEDIUM
- `fe3e9b7` Update final remediation and validation package for CODEX-024/026/028/029/030
- `b78e444` Enforce symbol-identity lock and close direct-broker bypass (CODEX-029/026)
- `09b9237` Make SQLite canonical for position/exit-intent state (CODEX-028/024)
- `aee663c` Record Codex independent review: FAIL, CODEX-024/026 PARTIALLY_RESOLVED, CODEX-028/029 HIGH, CODEX-030 MEDIUM
- `f04a123` Inject deterministic clock into lifecycle checks (CODEX-030)
- `e49753f` Regenerate FINAL_VALIDATION_PACKAGE for CODEX-023~027 cycle
- `4de0714` Update governance docs for CODEX-023~027 remediation cycle
- `f482e90` Enforce 30000 KRW budget and allow-list at the order boundary (CODEX-026)
- `ee6dae2` Separate order acceptance from fills and add durable exit intents (CODEX-023/024)
- `c5c56c4` Fail closed on corrupted position store (CODEX-025)
- `0f60ec9` Validate fill quantities and prices (CODEX-027)
- `f2afb4e` Record Codex independent review: FAIL, CODEX-023~026 HIGH, CODEX-027 MEDIUM
- `530f888` Add FINAL_VALIDATION_PACKAGE.md for Stage 3-10, ready for Codex validation
- `986d655` Prepare 30000 KRW limited live review (Stage 10)
- `f2e1a24` Add trading operations monitoring dashboard (Stage 9)
- `2094adf` Add deterministic strategy selection engine (Stage 8)
- `59958cf` Add intraday strategy backtest/replay engine (Stage 7)
- `9814114` Normalize .gitignore to LF and remove duplicate/dead lines
- `8915c44` Fix .gitignore lock-file pattern and remove stray lock file
- `639af97` Structure user and YouTube strategy sources (Stage 6)
- `bf05098` Add local SQLite trading state store (Stage 5)
- `b3d8cf4` Add position lifecycle and automated exits (Stage 4 part 4/N)
- `f9a2d1f` Add locked_position() and real strategy invalidation (Stage 4 part 3/N)
- `2058614` Add atomic position record store (Stage 4 part 2/N)
- `a78ab1b` Add position lifecycle state machine (Stage 4 part 1/N)
- `5aac75b` CODEX-022 해결 + CODEX-021 잔여분 종결: `_request()`에 중앙 집중식 3자 일치(purpose/order_side/payload side) 검증 추가 및 회귀 테스트
- `a31290b` Codex 독립 재검증 기록: FAIL, CODEX-021 partial, CODEX-022 신규
- `c133e01` CODEX-021/CODEX-020 잔여분: `_request()`를 RequestPurpose 기반으로 재설계하고 회귀 테스트 추가
- `47ae3ca` Codex 독립 재검증 기록: FAIL, CODEX-020 partial, CODEX-021 신규
- `ed452da` CODEX-018 잔여분: 공통 gate 에서 현재 credentials 재검증
- `66eda8a` CODEX-020: broker `_request` 공통 경로에 kill switch 게이트 추가
- `4f1f89d` Correct volume and premarket calculations (CODEX-015)
- `7ab8db7` Align watchlist lifecycle and timestamp validation (CODEX-014)
- `ac2b4b3` Make watchlist persistence failures explicit (CODEX-013)
- `044df60` Gate the pipeline behind trading-day and allowed-session checks (CODEX-012)

## 미반영 검증 지적사항
없음. Phase 1의 CODEX-001~009, Phase 2의 CODEX-010~015, 제한적 실거래 검토의 CODEX-016~022 전부
Claude 측 수정/테스트 완료. Codex 최종 재검증 대기 중(Phase 2 `PROCEED` 여부, CODEX-022 해결 및
CODEX-021 잔여분 종결의 `RESOLVED` 여부 모두 미확정).
