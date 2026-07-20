# Phase 1: Development Baseline Cleanup

정리 일자: 2026-07-21. 목적: MacBook Dev 환경과 Oracle Cloud Prod 환경이 같은 저장소를 경로/환경변수/의존성 문제 없이 공유하도록 기준선을 정리. 전략 점수 계산, 후보 필터링 기준, 주문 실행 로직, 리스크 한도, Slack 실발송 동작, systemd/cron/nginx, 운영 서버 설정은 변경하지 않았습니다.

## 1. 작업 전 상태 (기록)

- `git status`: clean, `main` 브랜치, origin과 동기화됨.
- 최근 커밋: `de76965 Use unrealized PnL in health check report` 외.
- 로컬 Python: `3.9.6`.
- 기존 `requirements.txt`: `pandas_market_calendars` 누락.
- 재현: `pip install -r requirements.txt` 후 `pytest` → `tests/test_scanner.py`, `tests/test_premarket_momentum_score.py` 컬렉션 단계에서 `ModuleNotFoundError: pandas_market_calendars`.
- 추가 발견: 최신 `pandas_market_calendars==5.2.4`는 Python 3.9에서 `X | None` 타입 힌트 문법(3.10+ 필요)으로 인해 import 자체가 실패. `4.4.0`으로 고정 시 3.9.6에서 정상 동작.
- 위 문제 해결 후 테스트 기준선: **47 passed, 0 failed**.
- `/home/ubuntu/trading` 하드코딩 위치: `premarket_scan_runner.py:9`, `universe_daily_runner.py:9`, `run_premarket.py:19,30,31,33`, `backtest_report_slack.py:14,17`, `systemd/dashboard.service`, `systemd/order-monitor.service`.
- `.env.example`에 없던 실참조 변수: `TRADING_BASE_DIR`, `TRADING_PYTHON` (기존 `daily_pipeline.py`가 이미 사용 중이었음에도 문서화 누락).
- 레거시 `ALPACA_BASE_URL`을 실제로 참조하는 운영 파이프라인 파일: `universe_builder.py` (`universe_daily_runner.py`가 cron으로 호출).

## 2. 변경 사항

### requirements.txt
- `pandas_market_calendars==4.4.0` 추가 (Python 3.9 호환 확인된 버전으로 고정).

### 신규 `config/paths.py`
- `get_project_root()`: `TRADING_PROJECT_ROOT` 환경변수가 있으면 그 값을, 없으면 저장소 루트를 자동 인식.
- 기존 `daily_pipeline.py`의 `TRADING_BASE_DIR` 패턴과는 별개로 유지(강제 통합하지 않음 — 이미 배포된 systemd/cron이 참조할 수 있는 이름을 임의로 바꾸지 않기 위함).

### 경로 하드코딩 제거 (로직 변경 없음, 경로 소스만 교체)
- `premarket_scan_runner.py`, `universe_daily_runner.py`, `run_premarket.py`, `backtest_report_slack.py`: `BASE_DIR = "/home/ubuntu/trading"` → `BASE_DIR = str(get_project_root())`.

### `universe_builder.py` — 레거시 환경변수 하위호환
- 기존: `BASE_URL = os.getenv("ALPACA_BASE_URL")`
- 변경: `ALPACA_PAPER_BASE_URL` → `ALPACA_BASE_URL` → `"https://paper-api.alpaca.markets"` 순서로 fallback.
- 운영 서버에 이미 `ALPACA_BASE_URL`이 설정되어 있다면 동작 동일(2순위로 계속 사용됨). 아무것도 설정되지 않은 새 환경(MacBook)에서는 Paper 엔드포인트로 안전하게 기본 동작 — **Live URL이 기본값이 되는 경우는 없음**.

### `.env.example`
- `TRADING_PROJECT_ROOT`, `TRADING_BASE_DIR`, `TRADING_PYTHON` 항목과 설명 주석 추가 (모두 빈 값, 실제 키/시크릿 없음).
- `FMP_API_KEY`는 미사용 확인되어 추가하지 않음(아래 5번 참고).

### 문서
- `docs/DEVELOPMENT_SETUP_MAC.md` 신규: macOS 개발환경 구축 절차.
- `docs/ENVIRONMENT_VARIABLES.md` 신규: 전체 환경변수 목록/용도/현행-레거시 구분.
- `README.md`: Setup 섹션에 `TRADING_PROJECT_ROOT` 설명과 위 두 문서 링크 추가 (기존 내용 유지).

## 3. 정리 후보 파일 분류 (삭제/이동 없음, 분류만)

| 파일 | import 참조 | 운영 사용 여부 | 분류 |
|---|---|---|---|
| `multi_scanner.py` | 없음 | 없음 (README 미언급) | 제거 후보 |
| `test.py` | 없음 | 없음 | 이동/제거 후보 (스크래치) |
| `ma_test.py` | 없음 | 없음 | 이동/제거 후보 (스크래치) |
| `indicator_test.py` | 없음 | 없음 | 이동/제거 후보 (스크래치) |
| `slack_test.py` | 없음 | 없음 | 이동/제거 후보 (스크래치) |
| `backtest_basic.py` | 없음 | 없음 | `backtest_multi.py`와 로직 중복 — 통합 후보 |
| `backtest_multi.py` | `backtest_report_slack.py`가 subprocess 호출 | **운영 사용 중** | 유지 |
| `health_check.py` | 없음 | 불명확(`watchlist.csv` 참조, README 미언급, `trading_health_check.py`와 기능 중복 의심) | 조사 후 제거 후보 |
| `trading_health_check.py` | 없음(엔트리포인트) | Slack 연동 있음, 최근 커밋 이력 활발 | 유지 (사실상 현행 버전) |

이번 단계에서는 위 파일들을 삭제/이동하지 않았습니다. Phase 2 이후 별도 작업으로 진행 권장.

## 4. 검증 결과

- `pip install -r requirements.txt`: 성공.
- `pytest -q`: **47 passed**, 0 failed (기준선과 동일).
- import 스모크 테스트: `daily_candidate_scanner`, `market_guard`, `order_monitor`, `paper_strategy_order`, `health_check`, `trading_health_check`, `dashboard.app` 모두 정상 import (모듈 최상단에서 네트워크/Slack 호출 없음을 코드로 사전 확인 후 실행).
- `universe_builder.py`, `premarket_scan_runner.py`는 모듈 최상단에서 즉시 네트워크 호출/subprocess 실행을 하는 스크립트라 일반 import 스모크 테스트에서 제외. 대신 `get_project_root()` 단위 동작과 `universe_builder.py`의 fallback 로직(`ast.parse` 문법 확인 + 코드 리딩)으로 검증.
- `get_project_root()`: `TRADING_PROJECT_ROOT` 미설정 시 저장소 루트 자동 인식, 설정 시 해당 값 사용 — 양쪽 모두 확인됨.
- 비밀값/실주문/Slack 실발송 없음.

## 5. 남아있는 하드코딩 경로

- `systemd/dashboard.service`, `systemd/order-monitor.service`: `WorkingDirectory`, `ExecStart`에 `/home/ubuntu/trading` 하드코딩. **이번 단계에서 미수정.**

## 6. 운영 서버에 나중에 반영 권장 (선택, 즉시 필요 없음)

- systemd 유닛 파일에 `Environment=TRADING_PROJECT_ROOT=/home/ubuntu/trading` 추가를 고려할 수 있으나, 현재 두 서비스(`dashboard.service`, `order-monitor.service`)는 `WorkingDirectory`가 이미 `/home/ubuntu/trading`으로 고정되어 있고 `dashboard/app.py`는 `Path(__file__)` 기반으로 루트를 스스로 찾으므로 **필수는 아님**. `order_monitor.py`도 경로 하드코딩이 없어 영향 없음.
- cron으로 실행되는 `premarket_scan_runner.py`, `universe_daily_runner.py`, `run_premarket.py`, `backtest_report_slack.py`는 이번 변경으로 `TRADING_PROJECT_ROOT` 미설정 시 스크립트 자신의 파일 위치 기준으로 루트를 자동 인식하므로, 운영 서버의 `.env`나 crontab을 당장 바꾸지 않아도 기존과 동일하게 `/home/ubuntu/trading` 하위에서 정상 동작합니다.
- `universe_builder.py`가 참조하는 레거시 `ALPACA_BASE_URL`이 운영 `.env`에 실제로 설정되어 있는지 다음 배포 시 확인 권장. 설정되어 있지 않다면 이번 fallback으로 인해 새롭게 `ALPACA_PAPER_BASE_URL`을 사용하게 되므로(이전엔 `BASE_URL=None`으로 조용히 깨졌을 가능성) 오히려 이전보다 안정적으로 동작할 것으로 예상됨 — 그래도 배포 후 첫 실행 로그는 확인 권장.
- `.env.example`에 추가된 `TRADING_PROJECT_ROOT`는 운영 서버 `.env`에도 명시적으로 채워두면(`/home/ubuntu/trading`) 향후 실행 위치가 달라지는 경우에도 안전.
