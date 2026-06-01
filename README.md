# US Stock Trading Automation

미국 주식 자동매매를 Paper Trading 기준으로 운영하는 저장소입니다. 현재 구조는 실거래 직전 점검을 위한 환경이며, 기본값에서는 Live 주문이 실행되지 않습니다.

## Safety Defaults

- `TRADING_MODE=paper`
- `ENABLE_REAL_TRADING=False`
- `LIVE_DRY_RUN=True`
- API Key는 `.env` 또는 서버 환경변수에만 둡니다.
- `.env`는 `.gitignore`에 포함되어 GitHub에 올라가지 않습니다.
- Dashboard에서는 Live 관련 값을 수정할 수 없습니다.
- 이 PR에서는 Live 주문이 Broker 계층에서도 실행되지 않습니다. `TRADING_MODE=live`, `ENABLE_REAL_TRADING=True`, `LIVE_DRY_RUN=False`가 모두 들어와도 실거래 주문은 별도 전환 PR 전까지 차단됩니다.

## Structure

- `broker/`: Alpaca Paper, Live Dry-run, Live guardrail 분리
- `config/scanner_rules.json`: 현재 스캐너 조건
- `config/scanner_presets.json`: `conservative_trend`, `smart_money`, `momentum`, `paper_safe`, `live_dry_run`
- `daily_candidate_scanner.py`: 후보 스캔 및 CSV 생성
- `paper_strategy_order.py`: Paper 주문 검토 및 중복 주문 차단
- `order_monitor.py`: Alpaca 주문 상태 모니터링
- `gpt_analysis.py`: 후보 상위 종목 분석 보조
- `slack_report.py`: `#value-report` 일일 요약
- `slack_utils.py`: Slack webhook 전송
- `dashboard/`: Flask 운영 대시보드
- `tests/`: 안전장치와 핵심 로직 테스트
- `systemd/`, `nginx/`: 배포 예시 파일

## Candidate Files

- `candidates.csv`: 전체 후보
- `strong_candidates.csv`: `smart_money_score` 기준 강한 후보
- `order_candidates.csv`: 실제 주문 검토 대상
- `gpt_candidate_analysis.csv`: GPT 또는 fallback 분석 결과
- `order_history.csv`: 당일 중복 주문 방지 기록

주문 엔진은 `order_candidates.csv`를 우선 사용하고, 파일이 없거나 비어 있으면 `candidates.csv`를 fallback으로 사용합니다.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env`에 Alpaca Paper Key와 Slack Webhook을 설정합니다. Live Key를 코드에 하드코딩하지 마세요.

## Run

```bash
python daily_candidate_scanner.py
python gpt_analysis.py
python slack_report.py
python daily_pipeline.py
python paper_strategy_order.py
python order_monitor.py
```

`paper_strategy_order.py`는 정규장(`regular`)에서만 주문을 검토합니다. 프리마켓과 애프터마켓에서는 알림만 보내고 주문하지 않습니다.

## Slack Channels

- `SLACK_ALERT_WEBHOOK_URL`: `#realtime-alerts`
  - 전체 후보 수
  - 수급 강한 후보 수
  - 거래량 2배 이상 후보 수
  - TOP 후보
  - 신규/반복 후보
  - smart-money 상위 후보
  - 정규장 주문 가능 여부
- `SLACK_WEBHOOK_URL`: `#value-report`
  - 시장 상태
  - 후보 요약
  - 백테스트 요약
  - GPT 요약
  - 리스크 상태

## Scanner Presets

`config/scanner_rules.json`의 `active_preset`을 바꾸거나 Dashboard에서 preset을 선택합니다.

- `conservative_trend`: 유동성 높고 추세 확인이 강한 후보
- `smart_money`: 거래량과 smart-money score 우선
- `momentum`: 모멘텀 조건을 넓게 탐색
- `paper_safe`: 기본 Paper 운영값
- `live_dry_run`: 실거래 전 리허설용 엄격 조건, Live 주문 활성화 아님

## Dashboard

```bash
python dashboard/app.py
```

브라우저에서 `http://SERVER_IP:5000`으로 접속합니다.

기능:

- 후보 CSV 조회
- 주문 내역 조회
- GPT 분석 조회
- 로그 조회
- `scanner_rules.json` 수정
- scanner preset 선택
- 일부 `risk_config.py` 값 수정
- systemd, cron, 미국장 상태 확인
- Paper / Live Dry-run / Live Disabled 상태 표시

수정 금지:

- `ENABLE_REAL_TRADING`
- `TRADING_MODE=live`
- `LIVE_DRY_RUN=False`

## Dashboard Korean UI

Flask Dashboard is presented as a Korean operations console named `자동매매 운영 관제`.

Main labels:

- `Performance`: `성과 분석`
- `Settings`: `설정`
- `Logs`: `로그`
- `Candidates`: `후보 종목`
- `Strong Candidates`: `수급 강한 후보`
- `Order Candidates`: `주문 검토 후보`
- `Orders`: `주문 내역`
- `GPT`: `AI 분석`
- `Broker`: `거래 모드`
- `Market`: `시장 상태`
- `Live Guard`: `실거래 보호`

The dashboard keeps Bootstrap dark mode and mobile responsive cards. Candidate tables show short Korean context messages such as `현재 조건을 통과한 종목 목록입니다.` and `실제 주문 전 최종 검토 대상입니다.` Performance Analytics shows `Paper Trading 성과 검증용 화면입니다.`

Live trading safety remains read-only in the Dashboard. `ENABLE_REAL_TRADING=False` and `LIVE_DRY_RUN=True` must stay unchanged unless a separate live-enablement review is performed.

## Scanner Rule Engine

Scanner conditions are managed in `config/scanner_rules.json` and can also be edited from Dashboard Settings. New scanner filters should be added to the `filters` array so `daily_candidate_scanner.py` does not need code changes for ordinary threshold updates.

Example:

```json
{
  "scan_limit": 1500,
  "top_alert_count": 5,
  "filters": [
    {"field": "price", "operator": ">=", "value": 5},
    {"field": "avg_dollar_volume", "operator": ">=", "value": 20000000},
    {"field": "rsi", "operator": "between", "min": 40, "max": 65},
    {"field": "volume_ratio", "operator": ">=", "value": 1.2},
    {"field": "score", "operator": ">=", "value": 70},
    {"field": "smart_money_score", "operator": ">=", "value": 50}
  ]
}
```

Supported operators: `>=`, `<=`, `>`, `<`, `==`, `!=`, `between`, `in`, `not_in`.

Supported fields include the current scanner metrics `price`, `avg_dollar_volume`, `rsi`, `volume_ratio`, `score`, `smart_money_score`, `above_ma200`, plus extension fields prepared for later use: `atr`, `gap_percent`, `market_cap`, `relative_strength`, `vwap_position`, `dollar_volume`, `float_shares`, `sector`, and `exchange`.

Unknown fields do not stop the scanner. They are logged as warnings and skipped. Presets in `config/scanner_presets.json` use the same rule-engine structure: `conservative_trend`, `smart_money`, `momentum`, `paper_safe`, and `live_dry_run`.

## Performance Analytics

Performance Analytics measures Paper Trading results before any real-trading decision. It reads Alpaca Paper account/orders/positions, combines them with `order_history.csv`, and writes:

- `performance_summary.csv`: account-level metrics such as win rate, profit factor, daily return, open P/L, and open positions
- `performance_trades.csv`: filled-order trade rows with current position P/L where available

Run manually:

```bash
python performance_analytics.py
```

Dashboard:

```bash
python dashboard/app.py
```

Open `http://SERVER_IP:5000/performance` or use the Performance menu. The dashboard falls back to existing CSV data if Alpaca Paper API calls fail, and shows "No performance data yet" when no summary exists.

Suggested Paper Trading validation period before live review:

- Run Paper Trading for at least 2 to 4 weeks.
- Confirm orders are filled only under expected market-session and risk rules.
- Review `performance_summary.csv`, `performance_trades.csv`, order history, Slack alerts, and dashboard metrics.

Minimum live-review checklist:

- Win rate is stable across multiple weeks, not just one day.
- Profit Factor is above 1.0 and preferably above 1.3 after fees/slippage assumptions.
- Average loss is controlled and smaller than the planned risk budget.
- No repeated rejected/canceled order pattern remains unresolved.
- Open P/L and daily return stay inside account-level drawdown limits.
- `ENABLE_REAL_TRADING=False` and `LIVE_DRY_RUN=True` remain unchanged until a separate live-enablement review.

## Cron

예시:

```cron
*/15 20-23 * * 1-5 cd /home/ubuntu/trading && /home/ubuntu/trading/venv/bin/python premarket_scan_runner.py >> logs/premarket_scan_cron.log 2>&1
0 7 * * 1-5 cd /home/ubuntu/trading && /home/ubuntu/trading/venv/bin/python daily_pipeline.py >> logs/daily_pipeline.log 2>&1
```

서버 타임존과 미국장 시간을 반드시 확인하세요.

## systemd

주문 모니터:

```bash
sudo cp systemd/order-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now order-monitor.service
sudo systemctl status order-monitor.service
```

Dashboard:

```bash
sudo cp systemd/dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dashboard.service
sudo systemctl status dashboard.service
```

## Nginx

```bash
sudo cp nginx/trading-dashboard.conf /etc/nginx/sites-available/trading-dashboard.conf
sudo ln -s /etc/nginx/sites-available/trading-dashboard.conf /etc/nginx/sites-enabled/trading-dashboard.conf
sudo nginx -t
sudo systemctl reload nginx
```

## Live Dry-run

Live Dry-run은 실거래 전 점검용입니다.

```env
TRADING_MODE=live
ENABLE_REAL_TRADING=False
LIVE_DRY_RUN=True
```

이 상태에서 주문 함수는 실제 Alpaca 주문 API를 호출하지 않고 dry-run 응답만 반환합니다.

## Real Trading Prohibited Conditions

다음 조건 중 하나라도 해당하면 실거래 전환 금지입니다.

- `.env`가 GitHub에 포함됨
- `ENABLE_REAL_TRADING=False`가 유지되지 않음
- `LIVE_DRY_RUN=True`가 유지되지 않음
- Dashboard에서 Live 값을 바꾸도록 수정됨
- `broker/` 또는 `order_safety.py`의 Live 차단을 제거함
- Paper Trading 최소 2주 검증 전
- 중복 주문 차단, 손실 제한, 포지션 제한 테스트 실패
- Slack 알림 또는 order monitor 장애

## Tests

```bash
pytest
```

포함된 테스트:

- live 기본 차단
- live dry-run 주문 미실행
- paper mode 정상
- scanner rules 로딩
- order candidates 생성
- duplicate order 차단
- market hours

## Incident Response

1. 주문 이상: `systemctl stop order-monitor.service`
2. cron 중지: `crontab -e`에서 자동 실행 주석 처리
3. Alpaca 포지션 확인: Paper 계정 UI와 `order_history.csv` 비교
4. 로그 확인: Dashboard Logs 또는 `logs/*.log`
5. Slack Webhook 장애: `.env` 값과 채널 권한 확인
6. scanner 이상: `config/scanner_rules.json`을 `paper_safe`로 되돌림

## Pull Request Summary

이 변경은 실거래 직전 준비 환경을 위한 PR입니다.

- Broker 계층 분리 및 Live Dry-run guardrail 추가
- 스캐너 규칙/프리셋 JSON화
- 후보 CSV 3단계 분리
- GPT 분석 보조 모듈 추가
- Slack 메시지 정리
- Flask Dashboard 추가
- 테스트와 배포 예시 파일 추가
