# Limited Live Review Checklist

이 문서는 "제한적 실거래(limited live)" 전환 여부를 사람이 판단하기 위한 체크리스트다.
이 문서의 존재 또는 완성 자체가 실거래 승인을 의미하지 않는다. 최종 승인 여부는
[LIVE_APPROVAL_RECORD.md](./LIVE_APPROVAL_RECORD.md)에 별도로 명시적으로 기록된다.

## 0. 문서 메타 정보

| 항목 | 값 |
|---|---|
| 검토 대상 커밋 | `c34fde1a664641799e4a37a02372f5d41a9e72ae` |
| 커밋 일시 | 2026-07-23 00:30:21 +0900 |
| 검토 대상 브랜치 | `orchestrator/20260722-235153-us-stock-trading` |
| 문서 작성일 | 2026-07-23 |
| 검토 일시(실제 사람 검토 수행 시각) | `TBD(운영자 기입)` |

## 1. 테스트 결과 (실측)

실행 명령:

```
venv/bin/python -m pytest -q
```

실측 출력(요약):

```
384 passed, 2 warnings in 30.90s
```

- collected: **384** (`venv/bin/python -m pytest -q --collect-only` → `384 tests collected`, collected 수와 passed 수 일치)
- passed: **384**
- failed: **0**
- warnings: **2건**
  - `urllib3` `NotOpenSSLWarning` (macOS 로컬 LibreSSL 관련, 코드 문제 아님)
  - `tests/test_scanner.py::test_unknown_field_skips_with_warning`가 의도적으로 유발하는 `RuntimeWarning`

이 수치는 `docs/autonomous/PAPER_TRADING_READINESS_REPORT.md`의 t0~t7 시점 수치(336 passed)와 다르다.
그 문서 이후 t8~t11 커밋에서 테스트가 추가되었기 때문이며, 본 체크리스트는 위 커밋 해시 기준
최신 실측값을 우선한다.

## 2. Broker 설정 (실측: `broker/broker_config.py`, `risk_config.py`)

| 항목 | 값 | 근거 |
|---|---|---|
| Broker 계정 종류 (`BrokerConfig().trading_mode` 기본값) | `paper` | `risk_config.py:23` (`TRADING_MODE = "paper"`), `broker/broker_config.py:30` |
| 실제 연결된 Alpaca 계정(paper/live 실키가 어느 계정인지) | `TBD(운영자 기입)` | 본 저장소 체크아웃에는 `.env` 파일이 없고 `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` 환경변수도 설정되어 있지 않음(확인 명령: `ls .env`, `env \| grep ALPACA` → 둘 다 없음) |
| Paper endpoint | `https://paper-api.alpaca.markets` | `broker/broker_config.py:11` (`PAPER_BASE_URL`) |
| Live endpoint | `https://api.alpaca.markets` | `broker/broker_config.py:12` (`LIVE_BASE_URL`) |
| `ENABLE_REAL_TRADING` 기본값 | `False` | `risk_config.py:24` |
| `LIVE_DRY_RUN` 기본값 | `True` | `risk_config.py:26` |
| `status_label` (현재 환경 기준) | `PAPER` | `broker/broker_config.py:103-110`, 환경변수 미설정 시 `is_live_mode=False` → `"PAPER"` |

## 3. 리스크 한도 (실측: `risk_config.py`)

| 항목 | 값 | 근거 |
|---|---|---|
| 일일 손실 한도 | `MAX_DAILY_LOSS_RATE = -0.02` (계좌 자본 대비 -2%) | `risk_config.py:2` |
| 총 낙폭 한도 | `MAX_TOTAL_DRAWDOWN = -0.10` | `risk_config.py:3` |
| 포지션당 최대 비중 | `MAX_POSITION_RATE = 0.10` (계좌 자본의 10%) | `risk_config.py:6` |
| 주문당 최대 금액(절대 금액) | `TBD(운영자 기입)` | `risk_config.py`에 절대 금액(달러) 상한 설정 없음. 비중 기반 한도(`MAX_POSITION_RATE`)만 존재 |
| 최대 동시 포지션 수 | `MAX_OPEN_POSITIONS = 5` | `risk_config.py:11` |
| 최대 일일 주문 수 | `MAX_TRADES_PER_DAY = 3` | `risk_config.py:10` |
| 계좌 총 익스포저 상한 | `MAX_TOTAL_EXPOSURE_RATE = 0.5` (계좌 자본의 50%) | `risk_config.py:15` |
| 허용 종목 범위(심볼 allow-list) | `TBD(운영자 기입)` | `risk_config.py`, `account_risk.py`에 종목 allow-list/블랙리스트 설정 없음 |
| 허용 거래 시간대 | `TBD(운영자 기입)` | `risk_config.py`에 거래 시간 창(time window) 설정 없음 |

## 4. Kill Switch 상태 (실측)

| 항목 | 값 | 근거 |
|---|---|---|
| 바이너리 halt (`kill_switch.is_trading_halted()`) | `False` (정지 아님) | `TRADING_HALTED` 환경변수 미설정, `KILL_SWITCH` 센티널 파일 없음(`ls KILL_SWITCH` → No such file or directory) |
| 다단계 상태 (`kill_switch_state.get_state()`) | `ACTIVE` | 상태 파일 `KILL_SWITCH_STATE.json` 없음 → `kill_switch_state.py:105-108`에 의해 기본값 `ACTIVE` |
| 상세 절차 | [KILL_SWITCH_RUNBOOK.md](./KILL_SWITCH_RUNBOOK.md) 참조 | |

## 5. Slack / 알림 상태 (실측: `notification_health.py`)

| 항목 | 값 | 근거 |
|---|---|---|
| 알림 헬스 상태 (`notification_health.get_status()`) | `UNKNOWN` (기록된 전송 이력 없음) | 상태 파일 `NOTIFICATION_HEALTH_STATE.json` 없음 → `notification_health.py:204-214`에 의해 `existed=False` → `UNKNOWN` |
| 연속 실패 임계값 | `5` (기본값, `NOTIFICATION_HEALTH_FAILURE_THRESHOLD` 미설정 시) | `notification_health.py:57` (`DEFAULT_FAILURE_THRESHOLD`) |
| 임계값 도달 시 자동 조치 | `ENTRY_DISABLED`로 킬스위치 자동 상승(ACTIVE 상태일 때만) | `notification_health.py:185-201` (`_escalate_kill_switch`) |

## 6. 계좌/주문 상태 (운영자가 실거래 검토 시점에 직접 확인 필요)

| 항목 | 값 |
|---|---|
| 상태 reconciliation 결과 (broker-로컬 대사) | `TBD(운영자 기입)` — `paper_strategy_order.py:548`의 `reconcile_pending_orders()` 실행 결과를 검토 시점에 기입 |
| 미체결 주문(open orders) | `TBD(운영자 기입)` |
| 현재 포지션 | `TBD(운영자 기입)` |

## 7. 승인 및 롤백

| 항목 | 값 |
|---|---|
| 운영자 승인 | `TBD(운영자 기입)` — [LIVE_APPROVAL_RECORD.md](./LIVE_APPROVAL_RECORD.md) 참조 |
| 롤백 담당자 | `TBD(운영자 기입)` |
| 롤백 절차 | [ROLLBACK_PLAN.md](./ROLLBACK_PLAN.md) 참조 |

## 8. 최종 상태

이 문서의 최종 상태는 아래 두 값 중 하나로만 표기한다: `READY_FOR_LIMITED_LIVE_REVIEW` 또는 `BLOCKED`.

**최종 상태: `READY_FOR_LIMITED_LIVE_REVIEW`**

근거: 위 1절의 회귀 테스트가 0 failed로 통과하고, 2~5절의 실측값이 모두 코드에서 확인 가능하며,
Kill Switch가 `ACTIVE`(정상)로 확인됨. 단, 6~7절의 다수 항목이 `TBD(운영자 기입)`로 남아 있으므로
이 값들이 운영자에 의해 채워지고 명시적으로 승인되기 전까지는 실거래 전환이 허용되지 않는다.
