# Oracle KIS Migration Runbook

**이 문서는 절차서다. Claude Code는 이 저장소가 실행되는 로컬/개발 환경에서 Oracle Cloud
서버로 SSH 접속할 수 있는 도구를 갖고 있지 않으므로, 아래 모든 단계는 실제 서버 접근 권한이
있는 운영자(또는 그런 도구를 가진 별도 세션)가 직접 수행해야 한다. Claude Code는 이 문서를
작성했을 뿐, 어떤 단계도 실행하지 않았다.**

## 0. 사전 조건

- 이 저장소의 `feature/kis-live-broker` 브랜치가 Codex 독립 검증을 통과했고(`PASS` 또는
  승인 가능한 `PASS_WITH_CONDITIONS`), `main`에 병합되어 origin에 push된 상태여야 한다.
  검증 전 커밋을 배포하지 않는다.
- `KIS_APP_KEY`/`KIS_APP_SECRET`/`KIS_ACCOUNT_NO` 등 KIS 실계좌 자격증명을 Git 저장소에
  절대 넣지 않는다. Oracle 서버의 별도 환경파일(`~/trading-release/.env` 또는 systemd
  `EnvironmentFile=`) 또는 비밀정보 관리 도구를 사용한다.
- 현재 운영 중인 `~/trading`은 이 배포 절차 도중 변경하지 않는다.

## 1. 서버 현재 상태 점검 (읽기 전용, 변경 없음)

```bash
ssh <oracle-server>
cd ~/trading
git log --oneline -5
git status --short
git diff --stat
systemctl status order-monitor dashboard 2>&1 | head -40
crontab -l
cat /etc/os-release
python3 --version
df -h
free -h
```

기록할 것: 서버 현재 커밋, 서버 로컬 변경사항(있다면), Git에 없는 운영 파일(`order_history.csv`,
`universe.csv`, `TRADING_STATE.db`, `KILL_SWITCH_STATE.json` 등), 현재 환경변수, 가상환경
경로, 설치된 패키지 버전(`pip freeze`), systemd 유닛 파일 내용, cron 항목, 로그 경로.

**운영 데이터를 삭제하거나 덮어쓰지 않는다.**

## 2. Swap 확인/추가 (메모리 약 1GB, 필요 시)

```bash
free -h  # Swap 없음 확인
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h  # 2GB Swap 반영 확인
```

## 3. 신규 매수 차단 (배포 전 안전 확인)

현재 운영 중인 `~/trading`이 아직 Alpaca 기반이라면, 이 프로젝트의 기존 Kill Switch로
신규 매수를 차단한다(기존 절차, `docs/live_review/KILL_SWITCH_RUNBOOK.md` 참고). 미체결
주문이 있는지 확인한다.

```bash
cd ~/trading
source venv/bin/activate
python3 -c "import kill_switch_state as ks; print(ks.get_state())"
```

## 4. 백업

```bash
cp -r ~/trading ~/trading-backup-$(date +%Y%m%d-%H%M%S)
```

CSV/DB/주문 원장(`order_history.csv`, `universe.csv`, `strategy_performance.csv`,
`TRADING_STATE.db*`, `KILL_SWITCH_STATE.json`, `LIVE_ENTRY_RESERVATION.lock` 등)이 백업에
포함됐는지 확인한다.

## 5. 신규 릴리스 디렉터리 배포

```bash
cd ~
git clone https://github.com/babucato-byte/us-stock-trading.git trading-release
cd trading-release
git checkout main   # Codex 검증 통과 후 병합된 커밋이어야 함
git log --oneline -1  # 검증된 커밋 해시와 반드시 일치해야 함
```

**서버에서 직접 코드를 수정하지 않는다.** `~/trading-release`가 서버 로컬에서 수정된 코드를
포함하고 있다면 그 배포는 무효로 간주한다 — origin에 push되고 검증된 버전만 배포한다.

## 6. 별도 가상환경 준비

```bash
cd ~/trading-release
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 7. 환경변수 설정 (Oracle 서버 전용 secrets 경로)

`~/trading-release/.env` (Git에 커밋되지 않음, `.gitignore` 확인):

```env
MARKET_DATA_PROVIDER=alpaca
EXECUTION_BROKER=kis

ALPACA_DATA_ENABLED=true
ALPACA_ORDER_ENABLED=false
ALPACA_PAPER_ORDER_ENABLED=false
ALPACA_API_KEY=<alpaca data-only key, if still needed for data>
ALPACA_SECRET_KEY=<...>

KIS_ENV=live
KIS_APP_KEY=<실제 KIS App Key>
KIS_APP_SECRET=<실제 KIS App Secret>
KIS_ACCOUNT_NO=<실제 KIS 계좌번호>
KIS_ACCOUNT_PRODUCT_CD=01
KIS_ALLOWED_ACCOUNT_NO=<위와 동일한 계좌번호 -- order_gate의 계좌 일치 검증용>
KIS_ACCOUNT_READ_ENABLED=true
KIS_LIVE_ORDER_ENABLED=false

ENTRY_DISABLED=true

LIVE_ROLLOUT_ENABLED=false
LIVE_ROLLOUT_ALLOWED_SYMBOLS=
LIVE_ROLLOUT_MAX_QUANTITY=1
LIVE_ROLLOUT_MAX_POSITIONS=1
LIVE_ROLLOUT_MAX_DAILY_ENTRIES=1
REGULAR_SESSION_ONLY=true
MARKET_ORDER_ENABLED=false
EXTENDED_HOURS_ENABLED=false
MAX_PRICE_DEVIATION_PERCENT=0.30

# CODEX-046: independent kill-switches for the KIS position-management
# tick's "extra" exit behaviors. All four MUST stay false at initial
# rollout -- only stop-loss and full take-profit (target_2) are active
# with all four off, which is the intended narrowest starting posture.
# Enabling any of these later is itself a deliberate, reviewed config
# change, not a code deploy.
LIVE_ENABLE_PARTIAL_PROFIT=false
LIVE_ENABLE_TRAILING_STOP=false
LIVE_ENABLE_TIME_STOP=false
LIVE_ENABLE_EOD_EXIT=false

VALIDATED_COMMIT=<Codex가 검증한 정확한 커밋 해시>
DEPLOYED_COMMIT=<위와 동일해야 함 -- order_gate.py가 이 둘의 일치를 강제로 검증한다>
```

**`KIS_LIVE_ORDER_ENABLED=false`와 `ENTRY_DISABLED=true`, `LIVE_ROLLOUT_ENABLED=false`, 그리고
네 개의 `LIVE_ENABLE_*` 플래그는 이 단계 이후에도 계속 `false`로 유지한다.** 이 중 하나라도
켜는 것은 spec §29의 "최초 KIS 실주문 기능 활성화"(또는 그에 준하는 실거래 동작 확장)에
해당하며 운영자의 별도 명시적 승인이 필요하다.

**주의(실제 배포에서 발견된 문제)**: 과거 배포된 Oracle `.env`에는 위 이름과 다른
`KIS_LIVE_APP_KEY`/`KIS_LIVE_APP_SECRET`/`KIS_LIVE_ACCOUNT_NO`/`KIS_LIVE_ACCOUNT_PRODUCT_CODE`
같은 변수명이 남아 있을 수 있다. `brokers/kis_config.py`가 실제로 읽는 이름은 위 코드
블록에 적힌 것(`KIS_APP_KEY`/`KIS_APP_SECRET`/`KIS_ACCOUNT_NO`/`KIS_ACCOUNT_PRODUCT_CD`)뿐이다
-- 8단계(전체 테스트)와 9단계(설정 검증)를 실행하기 전에, 실제 `.env` 파일의 변수명이
이 코드 블록과 정확히 일치하는지 `grep -oE '^[A-Z_][A-Z0-9_]*=' .env`로 반드시 재확인한다.
이름이 다르면 `KISConfig.from_env()`가 `app_key=None` 등으로 조용히 읽어 이후 모든 KIS
호출이 인증 오류로 실패한다 (비밀값 자체는 여전히 출력/로그에 남기지 않는다 -- 이름만
확인한다).

## 8. 스키마 마이그레이션 적용 (읽기 전용이 아님 -- 상태 DB 스키마 변경)

```bash
source venv/bin/activate
python3 -c "
from state_store import db
conn = db.open_db()
from state_store.migrations import CURRENT_SCHEMA_VERSION
print('applied schema version:', db.get_schema_version(conn))
assert db.get_schema_version(conn) == CURRENT_SCHEMA_VERSION
"
```

`db.open_db()`가 `kis_order_idempotency` 테이블(마이그레이션 6)과 그 `requested_quantity`
컬럼(마이그레이션 7, CODEX-045 -- 부분체결 오분류 수정에 필요)까지 자동으로 적용한다.
이 명령이 실패하거나 버전이 `CURRENT_SCHEMA_VERSION`과 다르면 다음 단계로 진행하지 않는다.

## 9. 전체 테스트

```bash
pytest -q
```

`docs/autonomous/FINAL_VALIDATION_PACKAGE.md`(또는 이 사이클의 등가 문서)에 기록된 테스트
수·결과와 정확히 일치해야 한다. 하나라도 실패하면 배포를 중단한다.

## 10. 설정 검증

```bash
python3 -c "
from config.live_rollout_config import LiveRolloutConfig
from config.live_exit_flags import LiveExitFlags
from brokers.kis_config import KISConfig
cfg = LiveRolloutConfig.from_env()
cfg.validate()
print('live_rollout OK:', cfg)
kis = KISConfig.from_env()
print('kis_env:', kis.kis_env, 'account_read_enabled:', kis.account_read_enabled, 'live_order_enabled:', kis.live_order_enabled)
flags = LiveExitFlags.from_env()
print('exit flags (all must be False at initial rollout):', flags)
"
```

`live_order_enabled`가 `False`인지, `LiveRolloutConfig.validate()`가 예외 없이 통과하는지,
`LiveExitFlags`의 네 필드가 모두 `False`인지 확인한다.

## 11. Alpaca 데이터 조회 확인 (읽기 전용)

```bash
python3 -c "
from market_data.alpaca_provider import AlpacaMarketDataProvider
p = AlpacaMarketDataProvider()
q = p.get_price_quote('AAPL')
print(q)
"
```

## 12. KIS 실계좌 조회 (읽기 전용, 이 단계에서 최초로 실제 KIS API 호출 발생)

```bash
python3 -c "
from brokers.kis_broker import KISBroker
b = KISBroker()
snap = b.get_account_snapshot()
print(snap)
positions = b.get_positions()
print('positions:', positions)
open_orders = b.get_open_orders()
print('open_orders:', open_orders)
"
```

**이 단계가 이 프로젝트 전체에서 처음으로 실제 KIS API에 연결하는 지점이다.** 실패하면
(인증 오류, 네트워크 오류, 계좌번호 불일치 등) 원인을 해결할 때까지 다음 단계로 진행하지
않는다.

### 12.1 wire-format 값 실응답 확인 (CODEX-052)

`brokers/kis_broker.py`의 모든 TR_ID·endpoint·응답 field는 KIS 공식 예제/reference
repository 기준으로는 확인됐지만(`REFERENCE_VERIFIED`), **실제 KIS 응답으로는 아직
확인되지 않았다**(`LIVE_RESPONSE_PENDING`). 두 상태는 서로 다른 축이며, 코드의
`VERIFICATION_MATRIX`가 그 유일한 기준이다.

이 단계에서 확인해야 할 항목 전체를 코드에서 직접 뽑아 쓴다.

```bash
python3 -c "
from brokers.kis_broker import VERIFICATION_MATRIX, LIVE_RESPONSE_PENDING
for e in VERIFICATION_MATRIX:
    if e.live_status == LIVE_RESPONSE_PENDING:
        print(f'{e.name:28} {e.value:45} <- {e.source}')
"
```

특히 다음 두 가지는 실주문 활성화 전에 반드시 실응답으로 확인한다.

```text
price_field_last     현재가 응답의 실제 가격 field 이름 (output.last)
cancel_tr_id_live    일반 주문 취소 TR_ID (TTTT1004U / VTTT1004U) 및 요청 field
```

현재가 field는 §12의 읽기 전용 조회 응답으로 바로 확인할 수 있다. 취소 TR_ID는 읽기
전용으로 확인할 수 없으므로 **모의투자(paper) 환경에서 주문 후 취소**로 확인한다. 실계좌
주문으로 확인하지 않는다.

확인된 항목은 `VERIFICATION_MATRIX`의 `live_status`를 `LIVE_RESPONSE_CONFIRMED`로 갱신하고,
값이 실제와 다르면 코드를 수정한 뒤 다시 Codex 검증을 받는다.

## 13. KIS 잔고·미체결 대조 (계정 전체 reconciliation, CODEX-044)

reconciliation은 두 층으로 동작한다.

1. **주문 경로 내부 (CODEX-044, 강제)**: `execution/execution_engine.py`가 매 주문 직전에
   `reconciliation/snapshot.py::build_snapshot()`으로 KIS 실제 잔고·미체결·체결과 내부
   포지션·내부 열린 주문·UNKNOWN 주문을 직접 조회해 불변 스냅샷을 만들고,
   `verify_snapshot()`으로 검증한다. 조회 실패, 계좌/종목 불일치, TTL 초과
   (`RECONCILIATION_MAX_AGE_SECONDS`, 기본 30초), 포지션·미체결·체결 불일치, UNKNOWN 주문
   존재 중 하나라도 있으면 주문은 transport 호출 0회로 차단된다. 매수와 매도에 동일하게
   적용된다.
2. **주기 서비스 (§15의 `us-stock-trading-reconcile`)**: 같은 대조를 주기적으로 수행하고
   그 결과를 `RECONCILIATION_STATE.json`에 기록하며, UNKNOWN 주문을 KIS 체결 이력과 대조해
   해소하고, Shadow 감사 보관 정책을 적용한다. KIS 조회가 실패하면 **아무것도 기록하지
   않는다** -- 실패한 조회가 clean timestamp를 갱신할 수 없다.

수동 확인(읽기 전용, 저장소의 실제 진입점을 그대로 사용한다):

```bash
cd ~/trading-release
source venv/bin/activate
python3 scripts/run_reconciliation.py --log-level INFO
echo "exit=$?"   # 0=대조 완료, 2=KIS 조회 불가(아무것도 기록 안 함), 1=오류
cat RECONCILIATION_STATE.json
```

`mismatch`가 보고되면(즉 KIS에 내부가 모르는 포지션/주문이 있으면) 원인을 파악하기 전까지
중단한다. `RECONCILIATION_STATE_FILE` 환경변수로 상태 파일 경로를 지정할 수 있다(미지정 시
저장소 루트의 `RECONCILIATION_STATE.json`).

## 14. Shadow Mode 실행 (spec §26)

Shadow Mode 진입점은 `scripts/run_shadow_mode.py`다. 이 스크립트는
`execution.execution_engine`을 **import조차 하지 않으며** `submit_order()`를 호출할 수 있는
경로가 없다 -- 주문 불가가 플래그가 아니라 구조로 보장된다.

한 후보당 두 번의 게이트 평가를 기록한다.

- **실제 평가**: 현재 배포 플래그 그대로. 초기 자세에서는 `live order flag is not enabled` /
  `ENTRY_DISABLED`에서 차단되며, 그것이 지금 시스템이 실제로 할 행동의 정직한 기록이다.
- **가정 평가**: 위 두 config 플래그만 뒤집은 경우. 가격 편차·잔고·중복 주문·allow-list·
  종목 적격성·reconciliation·UNKNOWN 등 **나머지 모든 안전 검사는 실제 KIS 조회 결과로**
  평가된다. 운영자가 활성화 전에 실제로 알아야 하는 질문("다른 검사는 통과했겠는가")에
  답하는 것이 이 기록이다.

기록 대상은 두 곳이다.

- 구조화 레코드: `shadow_mode.py` JSONL (`shadow-YYYY-MM-DD.jsonl`, 일자 회전 +
  `SHADOW_AUDIT_MAX_FILE_MB` 크기 회전 + `SHADOW_AUDIT_RETENTION_DAYS` 보관, append마다
  `fsync`).
- 감사 이벤트: `shadow_audit.py` SQLite `shadow_audit_events` 테이블. 매수 경로와 **매도
  경로 모두** 기록하며, 모든 run은 `SHADOW_COMPLETED` 또는 `SHADOW_ERROR`로 반드시
  종료된다. 계좌번호·비밀정보는 `execution/secret_redaction.py`가 기록 직전에 마스킹한다.

```bash
cd ~/trading-release
source venv/bin/activate
python3 scripts/run_shadow_mode.py --log-level INFO

# JSONL 레코드 수와 감사 이벤트 확인
python3 -c "
import shadow_audit, shadow_mode
records, corruption = shadow_mode.read_all_with_integrity()
print('shadow records:', len(records), 'corrupt lines:', corruption)
print('audit events:', len(shadow_audit.read_events()))
print('runs without a terminal event:', shadow_audit.runs_without_terminal_event())
"
```

`corrupt lines`가 비어 있지 않거나 `runs without a terminal event`가 비어 있지 않으면 감사
기록 경로 자체가 깨진 것이므로 원인을 파악하기 전까지 다음 단계로 진행하지 않는다.

## 15. 서비스 설치 및 경로 전환

### 15.1 유닛 설치

저장소에 실제 systemd 유닛과 설치 스크립트가 포함되어 있다.

```text
deploy/systemd/us-stock-trading-migrate.service        # 스키마 마이그레이션
deploy/systemd/us-stock-trading-reconcile.service      # 읽기 전용 대조
deploy/systemd/us-stock-trading-reconcile.timer        # 2분 주기
deploy/systemd/us-stock-trading-shadow.service         # 매수 평가, 주문 없음
deploy/systemd/us-stock-trading-shadow.timer           # 5분 주기
deploy/systemd/us-stock-trading-shadow-exit.service    # 매도/청산 조건 평가, 주문 없음
deploy/systemd/us-stock-trading-shadow-exit.timer      # 2분 주기
deploy/systemd/us-stock-trading-health.service         # 상태 점검
deploy/systemd/us-stock-trading-health.timer           # 15분 주기
deploy/systemd/us-stock-trading-live.service           # 설치만, enable 하지 않음

scripts/preflight_kis_live.py
scripts/run_migrations.py
scripts/run_reconciliation.py
scripts/run_shadow_mode.py
scripts/run_shadow_exit_evaluation.py
scripts/run_health_report.py
scripts/run_live_buy_entry.py
scripts/install_oracle_services.sh
```

서비스 기동 순서는 유닛의 `After=`/`Requires=`로 강제된다.

```text
migrate  →  (각 유닛의 ExecStartPre) preflight  →  reconcile  →  shadow / shadow-exit
```

`shadow`와 `shadow-exit`는 `Requires=us-stock-trading-reconcile.service`이므로
reconciliation이 실패하면 시작되지 않는다.

환경파일을 먼저 만든다(§7의 내용을 그대로 사용, **root:trading 0640**).

```bash
sudo install -d -m 0750 -o root -g trading /etc/us-stock-trading
sudo cp ~/trading-release/.env /etc/us-stock-trading/live-readonly.env
sudo chown root:trading /etc/us-stock-trading/live-readonly.env
sudo chmod 0640 /etc/us-stock-trading/live-readonly.env
```

설치:

```bash
cd ~/trading-release
sudo RELEASE_DIR=/home/ubuntu/trading-release scripts/install_oracle_services.sh
```

이 스크립트는 순서대로 다음을 수행한다.

```text
모든 entrypoint/unit 파일 존재 확인
trading 그룹·로그 디렉터리 생성
환경파일 권한 root:trading 0640 적용
환경파일의 실주문 플래그가 켜져 있으면 설치 거부
unit 파일 복사 + daemon-reload
scripts/run_migrations.py 실행
scripts/preflight_kis_live.py 실행
reconcile/shadow/shadow-exit/health timer만 enable
us-stock-trading-live.service disable + stop
```

`us-stock-trading-live.service`는 **절대 enable/start 하지 않는다**.

### 15.2 사전 검증 (수동 실행 가능)

```bash
cd ~/trading-release
source venv/bin/activate
python3 scripts/run_migrations.py
python3 scripts/preflight_kis_live.py
echo "exit=$?"    # 0이 아니면 서비스가 시작되지 않는다
```

preflight는 필수 환경변수, 계좌 alias, Alpaca/KIS 주문 비활성, `LIVE_ROLLOUT_ENABLED`
비활성, `ENTRY_DISABLED=true`, 플래그 상호 정합성, DB 스키마 버전, reconciliation 실행 가능
여부, 로그 디렉터리 쓰기 권한, 모든 entrypoint/unit 파일 존재, 단일 실행 lock, 그리고
**검증 커밋·배포 커밋·실제 체크아웃 커밋이 모두 동일한 40자리 소문자 hex SHA인지**를
확인한다(CODEX-051 — 짧은 prefix, 대문자, `HEAD` 같은 ref, 존재하지 않는 SHA는 모두 거부).
비밀값은 출력하지 않으며 계좌번호는 마스킹된 형태로만 나타난다.

### 15.3 단독 실행 확인 (서비스 등록 전에 손으로 한 번씩)

```bash
python3 scripts/run_reconciliation.py --log-level INFO          # exit 0 / 2(KIS 조회 불가)
python3 scripts/run_shadow_mode.py --log-level INFO             # 매수 평가, 주문 0회
python3 scripts/run_shadow_exit_evaluation.py --log-level INFO  # 매도 조건 평가, 주문 0회
python3 scripts/run_health_report.py --json                     # exit 0 / 2(문제 있음)
```

`run_shadow_exit_evaluation.py`는 보유 포지션마다 손절·익절·분할익절·시간청산·EOD 청산
조건을 `positions.lifecycle.decide_exit()`(실주문 경로가 사용하는 것과 **동일한** 순수
함수)로 평가하고 결과만 기록한다. `check_and_manage()`를 호출하지 않으므로 청산 주문을
낼 수 없다.

### 15.4 시작·확인

```bash
sudo systemctl start us-stock-trading-reconcile.service
sudo systemctl start us-stock-trading-shadow.service
sudo systemctl start us-stock-trading-shadow-exit.service
sudo systemctl start us-stock-trading-health.service

systemctl list-timers | grep us-stock-trading
journalctl -u us-stock-trading-reconcile.service -n 50 --no-pager
journalctl -u us-stock-trading-shadow.service -n 50 --no-pager
journalctl -u us-stock-trading-shadow-exit.service -n 50 --no-pager
journalctl -u us-stock-trading-health.service -n 50 --no-pager
```

감사 기록 확인:

```bash
python3 -c "
import shadow_audit, shadow_mode
print('audit events:', len(shadow_audit.read_events()))
print('integrity:', shadow_audit.audit_integrity_report())
records, corruption = shadow_mode.read_all_with_integrity()
print('shadow records:', len(records), 'corrupt lines:', corruption)
"
```

`runs_without_terminal_event` 또는 `runs_with_multiple_terminal_events`가 비어 있지 않거나
`corrupt lines`가 비어 있지 않으면 감사 기록 경로가 깨진 것이므로 다음 단계로 진행하지
않는다.

### 15.5 live 서비스가 비활성인지 확인 (필수)

```bash
systemctl is-enabled us-stock-trading-live.service   # disabled 이어야 한다
systemctl is-active  us-stock-trading-live.service   # inactive 이어야 한다
```

`enabled`가 나오면 즉시 `sudo systemctl disable --now us-stock-trading-live.service`를
실행하고 원인을 조사한다. `run_health_report.py`도 매 15분 이 조건을 확인한다.

### 15.6 기존 서비스 경로 전환

기존 `order-monitor`/`dashboard` 유닛을 새 `~/trading-release` 경로로 갱신하되,
**`ENTRY_DISABLED=true`/`KIS_LIVE_ORDER_ENABLED=false`/`LIVE_ROLLOUT_ENABLED=false`/네
`LIVE_ENABLE_*` 플래그를 유지한 채** 전환한다.

```bash
sudo systemctl daemon-reload
sudo systemctl restart order-monitor dashboard
systemctl status order-monitor dashboard
```

### 15.7 정지

```bash
sudo systemctl stop us-stock-trading-shadow.timer us-stock-trading-shadow-exit.timer \
    us-stock-trading-reconcile.timer us-stock-trading-health.timer
sudo systemctl stop us-stock-trading-shadow.service us-stock-trading-shadow-exit.service \
    us-stock-trading-reconcile.service us-stock-trading-health.service
```

**주의(범위)**: 실제 청산 주문을 내는
`kis_position_manager.sync_kis_fills_and_manage_exits()` tick은 실주문 경로이므로 초기
배포에 서비스로 포함하지 않는다. 그 조건 평가는 위 `shadow-exit` 서비스가 주문 없이
수행하므로 Oracle에서 매도 로직을 검증하는 것 자체는 가능하다. 실제 실행 활성화는 spec §29의
실주문 활성화 승인 절차에 속한다.

## 16. 실주문 비활성 상태 최종 확인

```bash
sudo grep -E "ENTRY_DISABLED|KIS_LIVE_ORDER_ENABLED|LIVE_ROLLOUT_ENABLED|ALPACA_ORDER_ENABLED|ALPACA_PAPER_ORDER_ENABLED|LIVE_ENABLE_PARTIAL_PROFIT|LIVE_ENABLE_TRAILING_STOP|LIVE_ENABLE_TIME_STOP|LIVE_ENABLE_EOD_EXIT" /etc/us-stock-trading/live-readonly.env
systemctl is-enabled us-stock-trading-live.service
```

아홉 값 모두 `false`(또는 `ENTRY_DISABLED=true`)이고 live 서비스가 `disabled`인지 육안으로
재확인한다.

## 롤백 절차

1. 새 유닛을 먼저 중지·비활성화한다.

   ```bash
   sudo systemctl disable --now us-stock-trading-shadow.timer
   sudo systemctl disable --now us-stock-trading-shadow-exit.timer
   sudo systemctl disable --now us-stock-trading-reconcile.timer
   sudo systemctl disable --now us-stock-trading-health.timer
   sudo systemctl stop us-stock-trading-shadow.service us-stock-trading-shadow-exit.service \
       us-stock-trading-reconcile.service us-stock-trading-health.service
   systemctl is-enabled us-stock-trading-live.service   # disabled 확인
   ```

2. `sudo systemctl stop order-monitor dashboard`
3. systemd 유닛의 `WorkingDirectory`/`ExecStart`를 `~/trading`(기존 운영본)으로 되돌린다.
4. `sudo systemctl daemon-reload && sudo systemctl start order-monitor dashboard`
5. `~/trading-release`와 `/etc/us-stock-trading/live-readonly.env`는 삭제하지 않고
   보존한다(원인 분석용).
6. 문제가 KIS API 자체(인증/네트워크)라면 `KIS_ACCOUNT_READ_ENABLED=false`로 즉시 KIS
   API 호출을 전면 차단할 수 있다 — 코드 변경 없이 환경변수만으로 가능.

## 긴급 대응 절차

- **의심되는 이중 주문/미확인 주문**: `KIS_LIVE_ORDER_ENABLED=false`로 즉시 전환(신규
  주문 전면 차단) 후 `reconciliation/order_reconciler.reconcile_unknown_order()`로 KIS
  주문 이력과 대조. 자동 반대매매/자동 취소를 수행하지 않는다.
- **계좌 불일치 발견**: `operations/commands.halt()`를 호출해 전량 자동 주문 실행을 중지한다
  (기존 포지션 감시는 계속되나 신규 주문/매도 자동 실행도 함께 중단됨 — spec §20의 HALT).
- **긴급 전량 청산 필요**: `operations/commands.request_emergency_liquidation()`으로 승인
  기록을 남기되, 실제 청산 주문은 이 승인만으로 자동 실행되지 않는다 — 사람이 KIS HTS/MTS
  또는 별도 명시적 조작으로 직접 수행한다(spec §29).
