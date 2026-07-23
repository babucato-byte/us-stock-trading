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

실측 출력(요약, 최종 회귀 확인 재실행 결과):

```
443 passed, 2 warnings
```

- collected: **384** (수집된 테스트 수 = 통과 수, 스킵/xfail 없이 전량 실행됨)
- passed: **384**
- failed: **0**
- warnings: **2건**
  - `urllib3` `NotOpenSSLWarning` (macOS 로컬 LibreSSL 관련, 코드 문제 아님)
  - `tests/test_scanner.py::test_unknown_field_skips_with_warning`가 의도적으로 유발하는 `RuntimeWarning`
- exit code: **0**

이 수치는 `docs/autonomous/PAPER_TRADING_READINESS_REPORT.md`의 t0~t7 시점 수치(336 passed)와 다르다.
그 문서 이후 t8~t11 커밋에서 테스트가 추가되었기 때문이며, 본 체크리스트는 위 커밋 해시 기준
최신 실측값을 우선한다.

### 1.1 최종 회귀 확인 (2026-07-23, 코드 수정 없이 확인만 수행)

| 확인 항목 | 결과 | 근거 |
|---|---|---|
| `venv/bin/python -m pytest -q` 전체 실행 | exit code 0, **443 passed, 0 failed, 2 warnings** | CODEX-016·018 최종 보완 커밋 `47ee8d6` 기준 |
| 기존 테스트 삭제/완화/skip/xfail 여부 | 없음 | `grep -rn "skip\|xfail" tests/` 결과 실제 `pytest.mark.skip`/`pytest.mark.xfail` 데코레이터 없음(매치된 문자열은 모두 비즈니스 로직상의 `skip_reason` 값·변수명일 뿐임). `git diff --stat main...HEAD -- tests/`는 10개 파일 전량 신규 추가(`insertions`만 존재, `deletions` 없음) — 기존 테스트 파일 수정/삭제 없음 |
| 금지 파일 변경 여부 (`broker/alpaca_client.py`, `broker/__init__.py`, `order_safety.py`, `config/scanner_presets.json`) | 변경 없음 | `git diff --name-only main...HEAD` 목록에 위 4개 파일 모두 미포함 |
| 운영 데이터 파일 변경 여부 (`order_history.csv`, `strategy_performance.csv`, `universe.csv`) | 변경 없음 | `git diff --name-only main...HEAD -- order_history.csv strategy_performance.csv universe.csv` → 빈 결과 |
| 테스트 실행 부작용(신규/변경 파일) 여부 | 없음 | 테스트 실행 직후 `git status --porcelain` → 빈 출력(본 문서 편집 전 시점 기준). 운영 데이터 파일, lock 파일, 상태 파일(`KILL_SWITCH_STATE.json`, `NOTIFICATION_HEALTH_STATE.json` 등) 생성/변경 없음 |

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

**최종 상태: `BLOCKED`**

근거: CODEX-016·018 최종 보완은 전체 회귀를 통과했으나 Codex 독립 재검증 전이다.
또한 6~7절의 운영자 기입 항목이 남아 있으므로 limited live review 및 실거래 전환을
재개하지 않는다.
