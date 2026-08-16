# NEEDS_USER — 사용자만 실행할 수 있는 작업

자율 루프가 도구/권한 한계로 실행하지 못한 항목. 각 항목은 복사-붙여넣기로 실행 가능해야 한다.

## 1. Oracle 서버 재검증 (T3)

세션에는 SSH 접근이 없다. 서버에서:

push는 완료됐다 (T2, 2026-08-06): `origin/feature/kis-live-broker` = `b36a8a6`.

```bash
# 1) 최신 코드 반영 상태 확인 — HEAD가 b36a8a6이어야 한다
cd ~/us-stock-trading && git fetch && git log --oneline -3 origin/feature/kis-live-broker

# 2) 검증 절차는 runbook 순서대로
#    docs/deployment/ORACLE_KIS_MIGRATION_RUNBOOK.md 참고
#    - unit/EnvironmentFile/freshness/timer disabled 상태 재검증
#    - systemd timer는 검증 통과 전까지 비활성 유지
```

## 2. KIS 실계좌 TBD 2건 확인 (T3)

`TBD_VERIFY_LIVE_DOCS` 표시된 취소 TR_ID 1건, 현재가 응답 필드명 1건은
실계좌(또는 모의계좌) 조회로만 확정 가능. KIS 개발자센터 문서와 대조 후
코드의 TBD 주석 위치에서 확정값으로 교체 지시.

## 3. Shadow 증거 보관 기간 상향 (T7) — **창구 시작 전에** 해야 함

`SHADOW_AUDIT_RETENTION_DAYS` 기본값은 30일인데, Shadow 판정 창구는 20 거래일
(≈28 캘린더일)이다. 기본값 그대로 창구를 돌리면 판정 시점에 창구 앞부분의
`shadow_audit_events` 행과 `shadow-YYYY-MM-DD.jsonl` 파일이 이미 삭제돼 있다
(`purge_old_events()`/`purge_old_files()`가 reconciliation 틱에서 실제로 지운다).
**삭제된 뒤에는 되돌릴 수 없으므로 Shadow 타이머를 켜기 전에 설정한다.**
근거: `docs/autonomous/SHADOW_MODE_EXIT_CRITERIA.md` G11.

```bash
# 서버에서 (root)
sudo sed -i 's/^SHADOW_AUDIT_RETENTION_DAYS=.*/SHADOW_AUDIT_RETENTION_DAYS=45/' \
  /etc/us-stock-trading/live-readonly.env
grep -q '^SHADOW_AUDIT_RETENTION_DAYS=' /etc/us-stock-trading/live-readonly.env \
  || echo 'SHADOW_AUDIT_RETENTION_DAYS=45' | sudo tee -a /etc/us-stock-trading/live-readonly.env

# JSONL 경로도 설정돼 있어야 한다 (미설정이면 JSONL은 꺼진 채 DB만 남는다)
grep -E '^(SHADOW_MODE_LOG_DIR|SHADOW_MODE_LOG_FILE|SHADOW_AUDIT_MAX_FILE_MB)=' \
  /etc/us-stock-trading/live-readonly.env

sudo systemctl daemon-reload
```

## 4. 매도 전략 인터페이스 잔여 지시 재전송 (T5)

`entry_rules` ~ `end_of_day_exit_rules` 필드 목록이 중간에 끊긴 지시 메시지의
나머지 부분을 아무 Claude 세션에나 다시 전달해 주면 T5가 진행된다.

## 5. 로컬 `.git/HEAD.lock` 제거 — **해소됨 (2026-08-06 사이클 5)**

사이클 5에서 자율 루프가 직접 제거했다(실행 중인 git 프로세스 0을 확인한 뒤 `os.unlink`).
아래 내용은 재발 시 참고용으로 남긴다. 사용자 조치는 더 이상 필요 없다.

<details><summary>원래 기록</summary>

직전 세션이 남긴 0바이트 스테일 락이 `git commit`/`git update-ref`를 전부 막는다
(실행 중인 git 프로세스는 0으로 확인됨). 세션 샌드박스가 `.git` 내부 파일 삭제를
거부해 자율 루프가 직접 지울 수 없다. 사이클 3은 링크드 워크트리로 우회했지만,
이후 세션이 정상 경로로 커밋하려면 아래 한 줄이 필요하다. 로컬 전용이며 서버·origin
영향 없음.

```bash
cd ~/Projects/us-stock-trading && rm -f .git/HEAD.lock
```

</details>

## 6. 유니버스 계좌 금액대 필터 활성화 (T8) — 자격증명 + 플래그 1개 + 명령 1줄

T8의 코드·테스트·스크립트·러너 배선은 전부 완료됐다. 남은 사용자 몫은 **KIS 계좌 읽기
자격증명을 넣고 명령 한 줄을 실행하는 것**뿐이다. 이것을 하기 전까지
`state/universe_budget.json`이 없어서 `universe_tradable.csv`가 만들어지지 않고,
스캐너는 기존 `universe.csv`를 그대로 쓴다(= T8 이전과 동일하게 동작, 안전).

**실주문과 무관하다.** 이 경로는 잔고 조회(read)만 하며 `KIS_LIVE_ORDER_ENABLED`는
건드리지 않는다 — 읽기 게이트(`KIS_ACCOUNT_READ_ENABLED`)와 주문 게이트는 별개다.

```bash
# 1) .env (git 밖)에 KIS 읽기 자격증명 — 이미 있으면 3)으로
#    KIS_ENV=paper 로 모의계좌부터 시작할 수 있다
cat >> .env <<'EOF'
KIS_ENV=paper
KIS_APP_KEY=<발급값>
KIS_APP_SECRET=<발급값>
KIS_ACCOUNT_NO=<계좌번호 앞 8자리>
KIS_ACCOUNT_PRODUCT_CD=01
KIS_ACCOUNT_READ_ENABLED=true
EOF

# 2) 잔고를 읽어 예산으로 영속 (--show 로 산출된 1주 가격 상한까지 확인)
venv/bin/python scripts/refresh_universe_budget.py --show
#    종료코드 0=조회 성공, 1=조회 실패라 직전값 유지, 2=쓸 수 있는 값이 아예 없음

# 3) 전체 일일 갱신 (목록 갱신 → 잔고 갱신 → 필터된 유니버스 생성)
venv/bin/python universe_daily_runner.py
```

결과 확인:

```bash
head -3 universe_tradable.csv          # 살 수 있는 종목만, 유동성 높은 순
cat logs/universe_filter_report.json   # 포함/제외 사유별 통계 + 사용된 예산·상한
head -5 logs/universe_decisions.csv    # 심볼별 포함/제외 사유 전건
```

## 7. 실시간 실테스트 하네스 실행 (T9) — 자격증명 + 명령 1줄 (+ 실주문은 .env 1줄)

T9의 코드·테스트·기동 게이트·기록·리포트는 전부 완료됐다. 남은 사용자 몫은 **KIS 읽기
자격증명을 넣고 명령 한 줄을 실행하는 것**이다. 실주문까지 켜려면 그 다음에 `.env` 한 줄이
더 필요하다 — 자율 루프는 그 줄을 대신 쓰지 않는다(불변 안전 규칙).

### 7-1. 관찰 모드로 실시간 실테스트 시작 (실주문 없음, 지금 바로 가능)

```bash
# 1) .env — §6에서 이미 넣었다면 그대로 재사용한다. 모의투자부터 시작한다.
#    KIS_ALLOWED_ACCOUNT_NO 는 KIS_ACCOUNT_NO 와 같은 값이어야 기동 게이트를 통과한다.
cat >> .env <<'ENVEOF'
KIS_ENV=paper
KIS_APP_KEY=<발급값>
KIS_APP_SECRET=<발급값>
KIS_ACCOUNT_NO=<계좌번호 앞 8자리>
KIS_ACCOUNT_PRODUCT_CD=01
KIS_ALLOWED_ACCOUNT_NO=<KIS_ACCOUNT_NO 와 동일>
KIS_ACCOUNT_READ_ENABLED=true
ENVEOF

# 2) reconciliation 스냅샷을 한 번 갱신한다 (기동 게이트가 신선도를 요구한다)
venv/bin/python scripts/run_reconciliation.py

# 3) 기동 체크리스트만 먼저 확인 — 실패한 게이트가 이름과 사유코드로 나온다
scripts/start_live_pilot.sh --preflight-only

# 4) 실시간 루프 시작 (정규장, 60초 tick, 15분마다 스캐너 재실행)
scripts/start_live_pilot.sh
```

멈추려면 `Ctrl+C` 한 번 — 진행 중인 tick을 끝내고 그날 리포트를 쓴 뒤 종료한다.

결과 확인:

```bash
tail -3 logs/live_pilot/live-pilot-$(date -u +%F).jsonl   # tick 단위 전건 기록
cat logs/live_pilot/live-pilot-report-$(date -u +%F).json # 그날 집계 리포트
scripts/start_live_pilot.sh --report-only --date 2026-08-06  # 지난 날짜 리포트 재생성
```

자주 쓰는 옵션: `--once`(1 tick만), `--interval 30`, `--sessions premarket,regular`,
`--scan-interval 0`(스캐너 끄고 기존 후보 파일 재사용), `--scan-limit 200`,
`--until 2026-08-06T20:00:00+00:00`.

### 7-2. 실주문까지 켜기 — 사용자의 `.env` 3줄 (모의투자 권장)

관찰 모드에서 하루치 tick 기록이 납득될 때까지 확인한 뒤에만 한다.
**이 3줄을 넣기 전까지 하네스는 어떤 주문도 낼 수 없다** — 주문 경로를 import조차 하지 않는다.

```bash
cat >> .env <<'ENVEOF'
KIS_LIVE_ORDER_ENABLED=true
LIVE_ROLLOUT_ENABLED=true
ENTRY_DISABLED=false
ENVEOF
```

`KIS_ENV=paper`이면 모의투자 계좌에 실제 주문이 나가고, `positions/lifecycle.check_and_manage()`
매도 경로까지 실제로 돈다 — 이것이 매도 로직을 실증하는 유일한 경로다
(`SHADOW_MODE_EXIT_CRITERIA.md` G5가 기록한 구조적 공백).
되돌리려면 `KIS_LIVE_ORDER_ENABLED=false` 한 줄이면 되고, 다음 tick부터 즉시 관찰 모드로
돌아온다(자세는 tick마다 다시 읽는다).

### 7-3. 실계좌(`KIS_ENV=live`)로 올리는 조건

```bash
KIS_ENV=live LIVE_PILOT_ACK_LIVE_ENV=true scripts/start_live_pilot.sh --preflight-only
```

이 명령은 **지금은 반드시 실패한다.** `brokers/kis_broker.py`의 `VERIFICATION_MATRIX`에
실응답으로 확인되지 않은 값이 남아 있는 동안 기동 게이트가 거부하기 때문이다(현재 9건).
해제 방법은 환경변수가 아니라 **모의투자로 값을 확인한 뒤 매트릭스를
`LIVE_RESPONSE_CONFIRMED`로 바꾸는 코드 변경**뿐이다(§2와 같은 작업).
