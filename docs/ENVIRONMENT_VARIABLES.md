# Environment Variables

코드에서 실제로 참조하는 환경변수 전수 목록 (2026-07-21 기준, `os.environ.get`/`os.getenv` grep으로 조사). 실제 키/시크릿 값은 이 문서에 절대 기록하지 않습니다.

## 경로 / 실행 환경

| 변수 | 필수 여부 | 기본값 | 사용처 | 비고 |
|---|---|---|---|---|
| `TRADING_PROJECT_ROOT` | 선택 | 저장소 루트 자동 인식 | `config/paths.py` → `premarket_scan_runner.py`, `universe_daily_runner.py`, `run_premarket.py`, `backtest_report_slack.py` | Phase 1에서 신규 도입. 운영 서버에서만 명시적으로 설정 권장 |
| `TRADING_BASE_DIR` | 선택 | 파일 위치 자동 인식 | `daily_pipeline.py` | Phase 1 이전부터 존재하던 별도 패턴, 하위호환 위해 유지 |
| `TRADING_PYTHON` | 선택 | 현재 Python 인터프리터 | `daily_pipeline.py` | 위와 동일 |

## 거래 모드 / 세이프티

| 변수 | 필수 여부 | 기본값 | 사용처 |
|---|---|---|---|
| `TRADING_MODE` | 필수 | `paper` | `broker/broker_config.py` 등 |
| `ENABLE_REAL_TRADING` | 필수 | `False` | `broker/broker_config.py` |
| `LIVE_DRY_RUN` | 필수 | `True` | `broker/broker_config.py` |

## Alpaca

| 변수 | 필수 여부 | 사용처 | 비고 |
|---|---|---|---|
| `ALPACA_API_KEY` | 필수 | `broker/alpaca_client.py`, `universe_builder.py` 등 | |
| `ALPACA_SECRET_KEY` | 필수 | 위와 동일 | |
| `ALPACA_PAPER_BASE_URL` | 필수 (표준) | `broker/broker_config.py`, `universe_builder.py` | 현행 표준 변수 |
| `ALPACA_LIVE_BASE_URL` | 필수 (표준) | `broker/broker_config.py` | 현행 표준 변수 |
| `ALPACA_BASE_URL` | 레거시 | `universe_builder.py`, `test_alpaca_account.py`, `test_paper_order.py` | 운영 `.env`에 이미 설정되어 있다면 `universe_builder.py`가 계속 우선 사용(하위호환). 신규 설정 시 `ALPACA_PAPER_BASE_URL`을 사용하세요. `test_*.py`는 루트 스크래치 스크립트로 운영 파이프라인 밖에 있음 |

## Slack

| 변수 | 필수 여부 | 채널 | 사용처 |
|---|---|---|---|
| `SLACK_WEBHOOK_URL` | 필수 | `#stock-trading-report` | `slack_report.py`, `backtest_report_slack.py`, Scanner 주간 요약 (`scripts/run_scanner_report.py weekly --slack`), Manual Watchlist (`scripts/run_manual_watchlist.py --slack`) |
| `SLACK_ALERT_WEBHOOK_URL` | 필수 | `#stock-system-health` | `order_monitor.py`, `trading_health_check.py`, Scanner 실패 알림 (`scanners/notify/slack.py`) |

Scanner 계열은 새 webhook을 만들지 않고 위 두 개를 재사용한다. 실패는
alert 채널, 예정된 요약은 report 채널로 간다. `SCANNER_SLACK_ENABLED=false`
로 Scanner 계열 발송만 전부 끌 수 있다 (기존 Trading 알림에는 영향 없음).

## AI 분석

| 변수 | 필수 여부 | 기본값 | 사용처 |
|---|---|---|---|
| `AI_ANALYSIS_PROVIDER` | 선택 | `auto` | `gpt_analysis.py`, `ai_analysis/provider_config.py` |
| `OPENAI_API_KEY` | 선택 | - | `ai_analysis/openai_analyzer.py` |
| `OPENAI_MODEL` | 선택 | `gpt-4o-mini` | 위와 동일 |
| `GEMINI_API_KEY` | 선택 | - | `ai_analysis/gemini_analyzer.py` |
| `GEMINI_MODEL` | 선택 | `gemini-1.5-flash` | 위와 동일 |
| `AI_ANALYSIS_LIMIT` | 선택 | `10` | `gpt_analysis.py` |
| `GPT_ANALYSIS_LIMIT` | 레거시 | - | `gpt_analysis.py`가 `AI_ANALYSIS_LIMIT` 없을 때 fallback으로 읽음 |

## 스캐너 / 기술적 필터

| 변수 | 필수 여부 | 기본값 |
|---|---|---|
| `USE_TECHNICAL_ENTRY_FILTER` | 선택 | `true` |
| `SCAN_LIMIT` | 선택 | (전체) |
| `TECHNICAL_FILTER_MIN_SCORE` | 선택 | `5` |
| `TECH_FILTER_WEIGHT_PRICE_ABOVE_HMA200` | 선택 | `1` |
| `TECH_FILTER_WEIGHT_HMA200_RISING` | 선택 | `2` |
| `TECH_FILTER_WEIGHT_HMA_MACD_BULLISH` | 선택 | `1` |
| `TECH_FILTER_WEIGHT_MACD_HISTOGRAM_RISING` | 선택 | `2` |
| `TECH_FILTER_WEIGHT_SQZMOM_GREEN` | 선택 | `2` |
| `HMA_LONG_LENGTH` | 선택 | `200` |
| `HMA_MACD_FAST` | 선택 | `12` |
| `HMA_MACD_SLOW` | 선택 | `26` |
| `HMA_MACD_SIGNAL` | 선택 | `9` |
| `SQZMOM_LENGTH` | 선택 | `20` |
| `SQZMOM_BB_MULT` | 선택 | `2.0` |
| `SQZMOM_KC_MULT` | 선택 | `1.5` |

## 미사용 확인

`FMP_API_KEY`는 `.env.example`에 없고, 조사 결과 코드베이스 어디에서도 실제로 읽지 않습니다(죽은 참조 후보 — 문서에만 기록, 제거는 이번 단계 범위 밖).
