# Development Setup (macOS)

이 문서는 새 MacBook에서 이 저장소를 clone한 뒤 개발 가능한 상태로 만드는 절차입니다. Oracle Cloud 운영 서버 설정은 변경하지 않습니다.

## 1. Clone

```bash
git clone https://github.com/babucato-byte/us-stock-trading.git
cd us-stock-trading
```

## 2. Python 버전

운영 서버 및 이 저장소의 `pandas_market_calendars==4.4.0` 고정은 Python 3.9 기준으로 검증되었습니다. `python3 --version`으로 3.9 계열인지 확인하세요. 3.10 이상을 쓰는 경우에도 `requirements.txt`의 `pandas_market_calendars==4.4.0` 고정 버전은 그대로 동작합니다(더 최신 버전은 3.10+에서만 동작하므로 임의로 올리지 마세요).

## 3. 가상환경 및 의존성

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. 환경변수

```bash
cp .env.example .env
```

`.env`에 최소한 다음을 채워야 스캐너/대시보드가 정상 동작합니다 (Paper 기준):

- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`
- `SLACK_WEBHOOK_URL`, `SLACK_ALERT_WEBHOOK_URL` (없어도 스캐너 자체는 동작하지만 알림이 전송되지 않음)

`TRADING_PROJECT_ROOT`, `TRADING_BASE_DIR`, `TRADING_PYTHON`은 비워두세요. 저장소 루트와 현재 Python 인터프리터를 자동으로 인식합니다.

## 5. 검증

```bash
pytest -q
```

47개 테스트가 모두 통과해야 합니다(Phase 1 기준선). 실패가 있다면 `pandas_market_calendars` 버전이 `requirements.txt`와 다르게 설치되지 않았는지 먼저 확인하세요.

주요 모듈 import 확인 (네트워크 호출 없이 문법/의존성만 검증):

```bash
python -c "import daily_candidate_scanner, market_guard, order_monitor, paper_strategy_order, health_check, trading_health_check, dashboard.app"
```

## 6. 실행 시 주의사항

- `universe_builder.py`, `premarket_scan_runner.py`, `run_premarket.py` 등은 모듈 최상단에서 즉시 실행되는 스크립트입니다. `.env`에 실제 Alpaca 키가 없으면 import만으로도 API 호출을 시도해 401 에러가 날 수 있습니다 — 이는 정상이며 주문을 발생시키지 않습니다.
- Paper Trading이라도 `paper_strategy_order.py`, `order_monitor.py`를 직접 실행하면 실제 Alpaca Paper 계정에 주문이 나갈 수 있습니다. 개발 중 임의 실행에 주의하세요.
- Slack Webhook을 `.env`에 설정한 상태로 `slack_report.py`, `trading_health_check.py` 등을 실행하면 실제 채널에 메시지가 발송됩니다.

## 7. 운영 서버와의 차이

| 항목 | MacBook (Dev) | Oracle Cloud (Prod) |
|---|---|---|
| `TRADING_PROJECT_ROOT` | 비움 (자동 인식) | `/home/ubuntu/trading` (systemd/cron에 명시 필요 시) |
| 실행 방식 | 수동 실행 / IDE | systemd 2개 서비스 + cron |
| `.env` | 로컬 파일, Paper 키만 | 서버 환경변수 또는 `.env`, Paper 키만 (현재 기준) |

자세한 환경변수 목록은 [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md), 이번 정리 작업의 상세 배경은 [PHASE1_BASELINE_CLEANUP.md](PHASE1_BASELINE_CLEANUP.md)를 참고하세요.
