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
않는다. 두 개의 TBD_VERIFY_LIVE_DOCS 항목(`brokers/kis_broker.py`의 일반 취소 TR_ID, 현재가
응답의 정확한 필드명)을 이 시점에 실제 응답으로 재확인하고, 필요하면 코드를 수정 후 다시
Codex 검증을 받는다.

## 13. KIS 잔고·미체결 대조 (계정 전체 reconciliation, CODEX-044)

`reconciliation_state.is_current_and_clean()`이 buy/sell Order Gate 모두가 읽는 실제
값이다(더 이상 상수가 아니다) -- 이 값은 `kis_position_manager.sync_kis_fills_and_
manage_exits()`가 매 tick마다 계정 전체 포지션을 대조하고 그 결과를 기록해야만 "신선한"
상태로 유지된다(기본 유효기간 `reconciliation_state.DEFAULT_MAX_AGE_SECONDS` = 300초).
이 서비스가 §14에서 계속 실행되지 않으면 300초 후 buy/sell 모두 "reconciliation stale"로
자동 차단된다(안전 방향으로 fail-closed -- 오작동이 아니다).

```bash
python3 -c "
from datetime import datetime, timezone
from brokers.kis_broker import KISBroker
from reconciliation.position_reconciler import reconcile_positions
from reconciliation import reconciliation_state
b = KISBroker()
kis_positions = b.get_positions()
# internal_positions: 이 시점에는 KIS 실거래 이력이 없으므로 빈 리스트가 정상
mismatches = reconcile_positions([], kis_positions)
print('mismatches:', mismatches)
now = datetime.now(timezone.utc)
reconciliation_state.record_result(clean=not mismatches, mismatch_count=len(mismatches), now=now)
print('reconciliation_ok:', reconciliation_state.is_current_and_clean(
    max_age_seconds=reconciliation_state.DEFAULT_MAX_AGE_SECONDS, now=now))
"
```

`mismatches`가 빈 리스트가 아니면(즉 KIS에 이미 알 수 없는 포지션이 있으면) 원인을 파악하기
전까지 중단한다. `RECONCILIATION_STATE_FILE` 환경변수로 이 상태 파일의 경로를 지정할 수
있다(미지정 시 저장소 루트의 `RECONCILIATION_STATE.json`).

## 14. Shadow Mode 실행 (spec §26)

Shadow Mode는 완전히 구현되어 있다 (`shadow_mode.py`) -- buy 경로(`kis_live_trading.py`)와
sell 경로(`brokers/kis_broker_adapter.py`) 모두, config 차단/HALT/signal 만료/symbol
차단/가격 편차/잔고 부족/reconciliation 실패/UNKNOWN 주문 존재/중복 주문/Order Gate
거부/승인까지 모든 결과 범주를 구조화된 JSONL 레코드로 기록한다. 계좌번호·비밀정보는
`execution/secret_redaction.py`가 기록 전에 마스킹한다. 기본 경로는 달력일 단위로 회전한다
(`shadow-YYYY-MM-DD.jsonl`, `SHADOW_MODE_LOG_FILE` 환경변수로 고정 경로 지정 시 회전 없이
그 경로만 사용).

```bash
KIS_ACCOUNT_READ_ENABLED=true KIS_LIVE_ORDER_ENABLED=false LIVE_ROLLOUT_ENABLED=true \
  LIVE_ROLLOUT_ALLOWED_SYMBOLS=AAPL \
  python3 -c "
from brokers.kis_broker import KISBroker
import kis_live_trading as klt
import shadow_mode
b = KISBroker()
results = klt.run_live_buy_entry_cycle(broker=b)
print(results)
print('shadow mode records this run:', len(shadow_mode.read_all()))
"
```

`KIS_LIVE_ORDER_ENABLED=false`이므로 `order_gate.evaluate_buy_gate()`가 `live_order_enabled`
확인에서 매 후보를 차단하고 `results['blocked']`에 이유가 기록된다 — broker.submit_order()는
호출되지 않는다(`execution_engine.py`가 gate 실패 시 broker를 호출하지 않음을 보장). 동시에
같은 실행에서 각 차단마다 Shadow Mode 레코드가 하나씩 남아야 한다 -- `read_all()`의 개수가
0이면 Shadow Mode 기록 경로 자체가 깨진 것이므로 원인을 파악하기 전까지 다음 단계로
진행하지 않는다.

## 15. 서비스 경로 전환

기존 `order-monitor`/`dashboard` systemd 유닛을 새 `~/trading-release` 경로를 가리키도록
갱신하되(`WorkingDirectory=`, `ExecStart=`), **이 시점에도 `ENTRY_DISABLED=true`/
`KIS_LIVE_ORDER_ENABLED=false`/`LIVE_ROLLOUT_ENABLED=false`/네 `LIVE_ENABLE_*` 플래그를
유지한 채** 전환한다.

```bash
sudo systemctl daemon-reload
sudo systemctl restart order-monitor dashboard
systemctl status order-monitor dashboard
```

**신규 서비스 (CODEX-044 이후 필수)**: `kis_position_manager.sync_kis_fills_and_manage_exits()`
를 주기적으로(예: 30~60초 간격) 실행하는 별도 systemd 타이머/서비스가 배포되어야 한다 --
이 tick이 (1) KIS 체결을 내부 포지션에 반영하고, (2) 손절·익절·부분익절·트레일링·시간
청산·EOD 청산을 관리하며, (3) §13의 계정 전체 reconciliation 결과를 갱신해 buy/sell Order
Gate가 "stale"로 자동 차단되지 않도록 유지한다. 이 서비스 파일은 아직 이 저장소에 커밋된
`systemd/` 유닛으로 존재하지 않는다 -- 배포자가 `systemd/` 디렉터리의 기존 유닛 파일 형식을
참고해 새로 작성하고, `WorkingDirectory`/`ExecStart`/`EnvironmentFile`을 `~/trading-release`
와 동일한 `.env`로 지정해야 한다. 이 서비스가 존재/실행되지 않으면 5분(300초) 후부터 모든
신규 매수·매도가 "reconciliation stale"로 자동 차단된다(§13 참고, fail-closed이므로 안전하지만
의도된 정상 운영 상태는 아니다).

## 16. 실주문 비활성 상태 최종 확인

```bash
grep -E "ENTRY_DISABLED|KIS_LIVE_ORDER_ENABLED|LIVE_ROLLOUT_ENABLED|ALPACA_ORDER_ENABLED|ALPACA_PAPER_ORDER_ENABLED|LIVE_ENABLE_PARTIAL_PROFIT|LIVE_ENABLE_TRAILING_STOP|LIVE_ENABLE_TIME_STOP|LIVE_ENABLE_EOD_EXIT" ~/trading-release/.env
```

아홉 값 모두 `false`(또는 `ENTRY_DISABLED=true`)인지 육안으로 재확인한다.

## 롤백 절차

1. `sudo systemctl stop order-monitor dashboard`, 그리고 §15에서 배포했다면 KIS position-
   manager 타이머/서비스도 함께 중지한다.
2. systemd 유닛의 `WorkingDirectory`/`ExecStart`를 `~/trading`(기존 운영본)으로 되돌린다.
3. `sudo systemctl daemon-reload && sudo systemctl start order-monitor dashboard`
4. `~/trading-release`는 삭제하지 않고 보존한다(원인 분석용).
5. 문제가 KIS API 자체(인증/네트워크)라면 `KIS_ACCOUNT_READ_ENABLED=false`로 즉시 KIS
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
