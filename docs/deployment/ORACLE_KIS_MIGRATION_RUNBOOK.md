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

VALIDATED_COMMIT=<Codex가 검증한 정확한 커밋 해시>
DEPLOYED_COMMIT=<위와 동일해야 함 -- order_gate.py가 이 둘의 일치를 강제로 검증한다>
```

**`KIS_LIVE_ORDER_ENABLED=false`와 `ENTRY_DISABLED=true`, `LIVE_ROLLOUT_ENABLED=false`는
이 단계 이후에도 계속 유지한다.** 이 셋 중 하나라도 켜는 것은 spec §29의 "최초 KIS 실주문
기능 활성화"에 해당하며 운영자의 별도 명시적 승인이 필요하다.

## 8. 전체 테스트

```bash
source venv/bin/activate
pytest -q
```

`docs/autonomous/FINAL_VALIDATION_PACKAGE.md`(또는 이 사이클의 등가 문서)에 기록된 테스트
수·결과와 정확히 일치해야 한다. 하나라도 실패하면 배포를 중단한다.

## 9. 설정 검증

```bash
python3 -c "
from config.live_rollout_config import LiveRolloutConfig
from brokers.kis_config import KISConfig
cfg = LiveRolloutConfig.from_env()
cfg.validate()
print('live_rollout OK:', cfg)
kis = KISConfig.from_env()
print('kis_env:', kis.kis_env, 'account_read_enabled:', kis.account_read_enabled, 'live_order_enabled:', kis.live_order_enabled)
"
```

`live_order_enabled`가 `False`인지, `LiveRolloutConfig.validate()`가 예외 없이 통과하는지
확인한다.

## 10. Alpaca 데이터 조회 확인 (읽기 전용)

```bash
python3 -c "
from market_data.alpaca_provider import AlpacaMarketDataProvider
p = AlpacaMarketDataProvider()
q = p.get_price_quote('AAPL')
print(q)
"
```

## 11. KIS 실계좌 조회 (읽기 전용, 이 단계에서 최초로 실제 KIS API 호출 발생)

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

## 12. KIS 잔고·미체결 대조

```bash
python3 -c "
from brokers.kis_broker import KISBroker
from reconciliation.position_reconciler import reconcile_positions
b = KISBroker()
kis_positions = b.get_positions()
# internal_positions: 이 시점에는 KIS 실거래 이력이 없으므로 빈 리스트가 정상
mismatches = reconcile_positions([], kis_positions)
print('mismatches:', mismatches)
"
```

빈 리스트가 아니면(즉 KIS에 이미 알 수 없는 포지션이 있으면) 원인을 파악하기 전까지 중단한다.

## 13. Shadow Mode 실행 (spec §26)

**Shadow Mode 자체 구현은 이번 사이클 범위에 포함되지 않았다** — `kis_live_trading.py`의
buy-entry pipeline은 실제로 `KIS_LIVE_ORDER_ENABLED=false`일 때 `order_gate`/`execution_
engine`이 자연히 매 후보를 차단하므로(주문 거부, broker 호출 0회) 사실상 Shadow Mode와
동일한 안전성을 제공하지만, spec §26이 요구하는 별도 기록 항목(signal_id, alpaca_signal_
price, kis_validation_price, price_difference_percent, planned_quantity, planned_limit_
price, rejection_reason 등을 구조화된 로그/CSV로 남기는 것)은 아직 별도 구현되지 않았다.
이 단계에서는 대신 다음으로 대체 검증한다:

```bash
KIS_ACCOUNT_READ_ENABLED=true KIS_LIVE_ORDER_ENABLED=false LIVE_ROLLOUT_ENABLED=true \
  LIVE_ROLLOUT_ALLOWED_SYMBOLS=AAPL \
  python3 -c "
from brokers.kis_broker import KISBroker
import kis_live_trading as klt
b = KISBroker()
results = klt.run_live_buy_entry_cycle(broker=b)
print(results)
"
```

`KIS_LIVE_ORDER_ENABLED=false`이므로 `order_gate.evaluate_buy_gate()`가 `live_order_enabled`
확인에서 매 후보를 차단하고 `results['blocked']`에 이유가 기록된다 — broker.submit_order()는
호출되지 않는다(§9 참고: `execution_engine.py`가 gate 실패 시 broker를 호출하지 않음을
보장). 실제 별도 Shadow Mode 기록 기능은 후속 사이클의 남은 작업이다.

## 14. 서비스 경로 전환

기존 `order-monitor`/`dashboard` systemd 유닛을 새 `~/trading-release` 경로를 가리키도록
갱신하되(`WorkingDirectory=`, `ExecStart=`), **이 시점에도 `ENTRY_DISABLED=true`/
`KIS_LIVE_ORDER_ENABLED=false`/`LIVE_ROLLOUT_ENABLED=false`를 유지한 채** 전환한다.

```bash
sudo systemctl daemon-reload
sudo systemctl restart order-monitor dashboard
systemctl status order-monitor dashboard
```

## 15. 실주문 비활성 상태 최종 확인

```bash
grep -E "ENTRY_DISABLED|KIS_LIVE_ORDER_ENABLED|LIVE_ROLLOUT_ENABLED|ALPACA_ORDER_ENABLED|ALPACA_PAPER_ORDER_ENABLED" ~/trading-release/.env
```

다섯 값 모두 `false`(또는 `ENTRY_DISABLED=true`)인지 육안으로 재확인한다.

## 롤백 절차

1. `sudo systemctl stop order-monitor dashboard`
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
