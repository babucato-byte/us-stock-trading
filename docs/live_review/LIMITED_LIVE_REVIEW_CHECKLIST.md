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

### 1.2 CODEX-020·CODEX-018 잔여분 수정 후 재확인 (2026-07-24)

Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ee8d6`/`03962d3`/`cf4ada9`)이 overall verdict
**FAIL**을 내렸다: 신규 CODEX-020(HIGH, direct broker 호출이 kill switch를 우회)과 CODEX-018
잔여분(MEDIUM, 현재 credentials 미재검증)이 지적됐다. 이번 사이클(t1~t2, 커밋 `66eda8a`/`ed452da`)에서
`broker/alpaca_client.py`의 `AlpacaBroker._request()` 공통 경로에 두 항목을 모두 배선했다.
`broker/alpaca_client.py`는 이번 사이클에서 이 두 Finding 범위로만 한시 개방됐다(위 표의 "금지 파일"
목록은 이전 사이클 기준이며, 이번 사이클은 이 파일을 명시적으로 수정 대상에 포함한다).

| 확인 항목 | 결과 | 근거 |
|---|---|---|
| `venv/bin/python -m pytest -q` 전체 실행 | exit code 0, **489 passed, 0 failed, 2 warnings** | CODEX-020·CODEX-018 잔여분 수정 커밋 `66eda8a`/`ed452da` 기준 |
| 집중 안전 테스트 | **208 passed, 1 warning** | `test_broker_kill_switch_gate.py`(신규 25건) + `test_alpaca_client_runtime_revalidation.py`(확장 44건) + `test_broker_safety.py` + `test_universe_builder.py` + `test_paper_strategy_order_kill_switch_state.py` + `test_paper_order_execution.py` |
| 신규 안전 관련 warning 여부 | 없음 | 2건 warning은 기존 urllib3 LibreSSL 경고와 의도된 scanner unknown-field 경고뿐 |
| 운영 데이터 파일 변경 여부 (`order_history.csv`, `universe.csv`) | 변경 없음 | SHA-256이 `CODEX_REVIEW.md` 기록값과 동일 |
| `.env`, kill switch/notification 상태 파일 변경 여부 | 없음 | 이번 사이클에서 생성/수정하지 않음 |

### 1.3 CODEX-021 해결 및 CODEX-020 잔여분 종결 후 재확인 (2026-07-25)

Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `66eda8a`/`ed452da`/`cf5601d`/`edc5ad5`)이 overall
verdict **FAIL**을 내렸다: CODEX-016/017/018/019는 RESOLVED로 재확인됐으나 CODEX-020(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 CODEX-021(HIGH, `order_side=None` 명시로 kill switch 우회)이
제기됐다. 이번 사이클(t1, 커밋 `c133e01`)에서 `AlpacaBroker._request()`를 `RequestPurpose` enum
기반으로 재설계해 두 항목을 함께 닫았다. CODEX-016~019는 코드를 재작업하지 않고 관련 회귀
테스트만 재실행해 확인했다.

| 확인 항목 | 결과 | 근거 |
|---|---|---|
| `venv/bin/python -m pytest -q` 전체 실행 | exit code 0, **536 passed, 0 failed, 2 warnings** | CODEX-021 해결 및 CODEX-020 잔여분 종결 커밋 `c133e01` 기준 |
| 집중 안전 테스트 | **255 passed, 1 warning** | `test_broker_kill_switch_gate.py` + `test_broker_request_purpose.py`(신규) + `test_alpaca_client_runtime_revalidation.py` + `test_broker_safety.py` + `test_universe_builder.py` + `test_paper_strategy_order_kill_switch_state.py` + `test_paper_order_execution.py` |
| CODEX-016~019 회귀 전용 | **36 passed, 1 warning** | `test_paper_strategy_order_kill_switch_state.py` + `test_paper_strategy_order_notification_health.py` + `test_state_store_concurrency.py` — 코드 변경 없이 회귀만 확인 |
| 신규 안전 관련 warning 여부 | 없음 | 2건 warning은 기존 urllib3 LibreSSL 경고와 의도된 scanner unknown-field 경고뿐 |
| 운영 데이터 파일 변경 여부 (`order_history.csv`, `universe.csv`) | 변경 없음 | 이전 사이클 기록값과 동일 |
| `.env`, kill switch/notification 상태 파일 변경 여부 | 없음 | 이번 사이클에서 생성/수정하지 않음 |

### 1.4 CODEX-022 해결 및 CODEX-021 잔여분 종결 후 재확인 (2026-07-25)

Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ae3ca`/`c133e01`/`cc740a5`)이 overall verdict
**FAIL**을 내렸다: CODEX-016/017/018/019는 RESOLVED로 재확인됐으나 CODEX-021(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 CODEX-022(HIGH, `_request()`가 purpose/order_side/payload
side 3자를 서로 대조하지 않아 `EXIT_ORDER` 선언 하에 매수 payload를 보내면 `ENTRY_DISABLED`도
우회됨)가 제기됐다. 이번 사이클(t1, 커밋 `5aac75b`)에서 `AlpacaBroker._request()`에
`validate_order_intent()`를 도입해 세션 호출 전 이 3자 일치를 단일 지점에서 강제해 두 항목을
함께 닫았다. CODEX-016~019는 코드를 재작업하지 않고 관련 회귀 테스트만 재실행해 확인했다.

| 확인 항목 | 결과 | 근거 |
|---|---|---|
| `venv/bin/python -m pytest -q` 전체 실행 | exit code 0, **570 passed, 0 failed, 2 warnings** | CODEX-022 해결 및 CODEX-021 잔여분 종결 커밋 `5aac75b` 기준 |
| 집중 안전 테스트 | **289 passed, 1 warning** | `test_broker_kill_switch_gate.py` + `test_broker_request_purpose.py` + `test_broker_order_intent_gate.py`(신규) + `test_alpaca_client_runtime_revalidation.py` + `test_broker_safety.py` + `test_universe_builder.py` + `test_paper_strategy_order_kill_switch_state.py` + `test_paper_order_execution.py` |
| CODEX-016~019 회귀 전용 | **36 passed, 1 warning** | `test_paper_strategy_order_kill_switch_state.py` + `test_paper_strategy_order_notification_health.py` + `test_state_store_concurrency.py` — 코드 변경 없이 회귀만 확인 |
| 신규 안전 관련 warning 여부 | 없음 | 2건 warning은 기존 urllib3 LibreSSL 경고와 의도된 scanner unknown-field 경고뿐 |
| 운영 데이터 파일 변경 여부 (`order_history.csv`, `universe.csv`) | 변경 없음 | SHA-256이 이전 사이클 기록값과 동일 |
| `.env`, kill switch/notification 상태 파일 변경 여부 | 없음 | 이번 사이클에서 생성/수정하지 않음 |

### 1.5 Stage 3~10 + CODEX-023~027 통합 수정 사이클 후 재확인 (2026-07-26)

Codex 독립 검증(`CODEX_REVIEW.md`, 대상 커밋 `415c129`..`e3b9e9f`, Stage 3~10)이 overall verdict
**FAIL**을 내렸다: CODEX-023(HIGH, accepted 주문을 체결로 오판), CODEX-024(HIGH, 청산 timeout 후
durable intent 부재로 중복 sell 가능), CODEX-025(HIGH, 손상된 position store가 빈 포지션으로
처리됨), CODEX-026(HIGH, 30,000원 sizing/allow-list가 실제 주문 경계에 미배선),
CODEX-027(MEDIUM, 비정상 fill 수량/체결가 허용). 이번 사이클(커밋 `0f60ec9`/`c5c56c4`/`ee6dae2`/
`f482e90`)에서 5건 모두 수정: broker order status 분류(`positions/order_status.py`) +
fill 검증(`positions/fill_validation.py`) + durable exit intent ledger(SQLite,
`state_store/exit_intent_ledger.py`) + 3단계 청산 재설계(`positions/lifecycle.py`) + fail-closed
store corruption 감지(`positions/store.py`) + live 진입 경계 게이트(`live_readiness/
order_gateway.py`, `side="buy" AND is_live_mode`에만 적용).

| 확인 항목 | 결과 | 근거 |
|---|---|---|
| `venv/bin/python -m pytest -q` 전체 실행 | exit code 0, **923 passed, 0 failed, 2 warnings** | CODEX-023~027 수정 완료 커밋 `4de0714` 기준 |
| CODEX-023~027 집중 테스트 | **103 passed** | `test_fill_validation.py` 18 + `test_exit_intent_ledger.py` 13 + `test_exit_reconciliation.py` 20 + `test_live_order_gateway.py` 25 + `test_position_lifecycle.py`/`test_position_store.py`/`test_state_store.py` 신규분 27 |
| 신규 안전 관련 warning 여부 | 없음 | 2건 warning은 기존 urllib3 LibreSSL 경고와 의도된 scanner unknown-field 경고뿐 |
| 운영 데이터 파일 변경 여부 (`order_history.csv`, `universe.csv`, `strategy_performance.csv`) | 변경 없음 | md5가 이전 사이클 기록값과 동일 |
| `.env`, kill switch/notification 상태 파일 변경 여부 | 없음 | 이번 사이클에서 생성/수정하지 않음(단, `recover_on_restart()`가 store 손상 시 Kill Switch를 `MANUAL_REVIEW`로 자동 전환하는 신규 코드 경로 자체는 추가됨 — 실제 상태 파일이 사전에 변경된 것은 아님) |
| 상세 | [FINAL_VALIDATION_PACKAGE.md](../autonomous/FINAL_VALIDATION_PACKAGE.md) | Codex 재검증 대기 중, 상태 `READY_FOR_FINAL_CODEX_REVALIDATION` |

### 1.6 Stage 3~10 최종 재수정 사이클 — CODEX-024/026/028/029/030 후 재확인 (2026-07-26)

Codex 통합 재검증(`CODEX_REVIEW.md`, 대상 커밋 `4de0714`/`e49753f`)이 overall verdict **FAIL**을
내렸다: CODEX-023/025/027은 RESOLVED로 재확인됐으나 CODEX-024/026이 PARTIALLY_RESOLVED로 남았고
신규 CODEX-028(HIGH, exit SQLite/JSON commit 순서가 fill 진행량을 유실), CODEX-029(HIGH, live
context symbol과 실제 주문 symbol 불일치 허용), CODEX-030(MEDIUM, lifecycle 테스트가 wall-clock에
의존)이 제기됐다. 이번 사이클(커밋 `f04a123`/`09b9237`/`b78e444`)에서 5건 모두 수정: Clock 주입
(`clock.py`) + SQLite canonical 전환(`positions/store.py`, `positions`/`position_events`
테이블) + exit intent와 동일 트랜잭션 공유(`state_store/exit_intent_ledger.py`의 `commit=False`)
+ symbol 동일성 검사(`live_readiness/order_gateway.py`) + broker-level 게이트 배선
(`broker/alpaca_client.py::AlpacaBroker.submit_order()`).

| 확인 항목 | 결과 | 근거 |
|---|---|---|
| `venv/bin/python -m pytest -q` / `venv/bin/pytest -q` / 상위 디렉터리 `python -m pytest us-stock-trading -q` | 세 형태 모두 exit code 0, **973 passed, 0 failed, 2 warnings** | 커밋 `b78e444` 기준 |
| 신규 안전 관련 warning 여부 | 없음 | 2건 warning은 기존 urllib3 LibreSSL 경고와 의도된 scanner unknown-field 경고뿐 |
| 운영 데이터 파일 변경 여부 (`order_history.csv`, `universe.csv`, `strategy_performance.csv`) | 변경 없음 | md5가 이전 사이클 기록값과 동일 |
| 실제 저장소 루트 `TRADING_STATE.db*` | 존재하지 않음 | 두 차례 전체 회귀 실행 전후 확인(SQLite canonical 전환 직후 발견한 테스트 격리 누락 버그를 수정한 뒤) |
| `git diff --check` | 통과 | whitespace 오류 없음 |
| `.env`, kill switch/notification 상태 파일 변경 여부 | 없음 | 이번 사이클에서 생성/수정하지 않음 |
| 상세 | [FINAL_VALIDATION_PACKAGE.md](../autonomous/FINAL_VALIDATION_PACKAGE.md) | Codex 재검증 대기 중, 상태 `READY_FOR_FINAL_CODEX_REVALIDATION` |

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
| 주문당 최대 금액(절대 금액) | `TBD(운영자 기입)` | `risk_config.py`에는 여전히 절대 금액 상한이 없으나, `live_readiness/order_gateway.py::validate_and_size_live_entry()`(CODEX-026, 2026-07-26, 커밋 `f482e90`)가 `max_order_notional_krw`를 live 진입 경계에서 실제로 강제한다. 남은 것은 이 값 자체의 실측 기입뿐 |
| 최대 동시 포지션 수 | `MAX_OPEN_POSITIONS = 5` | `risk_config.py:11` |
| 최대 일일 주문 수 | `MAX_TRADES_PER_DAY = 3` | `risk_config.py:10` |
| 계좌 총 익스포저 상한 | `MAX_TOTAL_EXPOSURE_RATE = 0.5` (계좌 자본의 50%) | `risk_config.py:15` |
| 허용 종목 범위(심볼 allow-list) | `TBD(운영자 기입)` | `risk_config.py`, `account_risk.py`에는 여전히 없으나, `live_readiness/allowlist.py::is_symbol_allowed()`(CODEX-026, 2026-07-26, 커밋 `f482e90`)가 live 진입 경계에서 fail-closed로 실제 강제한다(빈 목록은 전부 차단). **갱신(2026-07-26, CODEX-029, 커밋 `b78e444`)**: allow-list와 대조되는 `ctx.symbol`이 실제 제출 symbol과 완전히 일치하는지도 이제 별도로 강제되며(대소문자/공백 변형도 차단), 이 게이트는 `AlpacaBroker.submit_order()` 자체에도 배선되어 direct 호출도 우회할 수 없다. 남은 것은 실제 종목 목록의 기입뿐 |
| 허용 거래 시간대 | `TBD(운영자 기입)` | `risk_config.py`에 거래 시간 창(time window) 설정 없음 |

## 4. Kill Switch 상태 (실측)

| 항목 | 값 | 근거 |
|---|---|---|
| 바이너리 halt (`kill_switch.is_trading_halted()`) | `False` (정지 아님) | `TRADING_HALTED` 환경변수 미설정, `KILL_SWITCH` 센티널 파일 없음(`ls KILL_SWITCH` → No such file or directory) |
| 다단계 상태 (`kill_switch_state.get_state()`) | `ACTIVE` | 상태 파일 `KILL_SWITCH_STATE.json` 없음 → `kill_switch_state.py:105-108`에 의해 기본값 `ACTIVE` |
| 강제 지점(enforcement point) | `paper_strategy_order.submit_order()` wrapper + `broker/alpaca_client.py::AlpacaBroker._request()` 양쪽 | CODEX-022(2026-07-25, 커밋 `5aac75b`) 이후 `_request()`가 `RequestPurpose`(`ENTRY_ORDER`/`EXIT_ORDER`일 때만)별로 binary halt와 4-state 정책을 직접 재조회하기 전에, 신규 `validate_order_intent()`가 `purpose`×`order_side`×payload `side`의 3자 일치를 먼저 강제한다. wrapper를 거치지 않은 direct broker 호출도 동일하게 차단하며, `order_side`는 이제 payload와 purpose 일치를 실제로 대조하는 2차 방어선으로 기능한다. Codex 독립 재검증 대기 중 |
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

**갱신(2026-07-26, CODEX-033)**: 이 절은 이전에 `READY_FOR_LIMITED_LIVE_REVIEW`로 표기되어 있었으나,
그 근거였던 CODEX-016~022 `PASS_WITH_CONDITIONS`(2026-07-25, 커밋 `a31290b`/`5aac75b`/`8803252`
대상)는 제한적 실거래 검토 사이클 자체에 대한 판정일 뿐, 그 이후 착수된 Stage 3~10(전략 플랫폼·
포지션 생명주기·SQLite 저장소·30,000원 게이트 등)에 대한 판정이 아니다. §1.5/§1.6에 기록된 대로
Stage 3~10에 대한 Codex 독립/통합 재검증은 반복적으로 **`FAIL`**을 냈고(CODEX-023~033 다수 Finding),
이 문서의 §8만 그 사실을 반영하지 않은 채 예전 판정을 근거로 `READY_FOR_LIMITED_LIVE_REVIEW`를
유지하고 있었다 — 최신 `docs/autonomous/FINAL_VALIDATION_PACKAGE.md`/`CURRENT_STATUS.md`가 이미
`BLOCKED`/`KEEP_IN_PROGRESS`로 정확히 기록하고 있는 것과 모순되는 상태였다. 운영자가 이 문서만
보고 최신 `FINAL_VALIDATION_PACKAGE.md`를 확인하지 않을 경우, 실제로는 아직 안전하지 않은
상태를 "실거래 검토 준비 완료"로 오판할 위험이 있었다(CODEX-033의 지적).

근거: Stage 3~10에 대한 Codex 최신 통합 재검증(`docs/autonomous/CODEX_REVIEW.md`)이 아직
**`FAIL`**이거나, 그 수정 결과에 대한 재검증을 아직 요청/완료하지 않은 상태다. 이 문서의 최종
상태는 Stage 3~10 관련 모든 Finding이 Codex 재검증에서 **`PASS`** 또는 **`PASS_WITH_CONDITIONS`**로
확정되고, 6~7절의 운영자 기입 항목(실제 계좌, 현재 포지션·미체결 주문·reconciliation, 허용 종목·
거래시간·주문당 절대 한도, 승인자·검토 시각·롤백 담당자)이 전부 채워지기 전까지는
`READY_FOR_LIMITED_LIVE_REVIEW`로 다시 승격하지 않는다. **Live trading: DO_NOT_ENABLE**을 계속
유지하며, `approved: false`/`live_enabled: false`는 변경하지 않았다. 최신 검증 상태의 단일
진실 공급원(source of truth)은 항상 [FINAL_VALIDATION_PACKAGE.md](../autonomous/FINAL_VALIDATION_PACKAGE.md)이며,
이 절은 그 문서가 갱신될 때마다 함께 갱신한다. 각 TBD 항목의 권장값 초안은
[TBD_REVIEW_RECOMMENDATIONS.md](./TBD_REVIEW_RECOMMENDATIONS.md) 참조.

(참고: CODEX-016~022 자체는 `PASS_WITH_CONDITIONS`로 여전히 유효하며 이번 갱신으로 재개된 것은
아니다 — §8이 `BLOCKED`인 이유는 그 이후 Stage 3~10에서 발견된 별개의 Finding들 때문이다.)

**추가 갱신(2026-07-28, Stage 11)**: `live_readiness/`에 Account/Risk/Sizing/Execution Engine
계층(신규 `trusted_operator_config.py`/`account_engine.py`/`risk_engine.py`/`sizing_engine.py`/
`execution_engine.py`)이 추가됐다 — 상세는 `docs/autonomous/PROJECT_CONSTITUTION.md`의 "계층
분리 원칙" 및 `FINAL_VALIDATION_PACKAGE.md` 참고. 이 계층은 순수 building block이며 실제 운영
스캔·주문 파이프라인에는 아직 배선되지 않았다 — 이 절의 `BLOCKED` 상태와 위 승격 조건에는 영향을
주지 않는다.

**추가 갱신(2026-07-28, CODEX-039/040/041, 커밋 `ae2b0fd`)**: Codex 통합 재검증이 위 Stage 11
계층이 실제 `paper_strategy_order.main()` 주문 경로에 배선되지 않았음을 CODEX-040(HIGH)으로
지적했다. 이번 사이클에서 신규 `live_readiness/live_entry_pipeline.py`를 통해 live-mode 진입
(`side="buy" AND broker.config.is_live_mode`)이 Account→Risk→Sizing→Affordability→Execution
Engine 순서로 실제 배선됐다(상세는 `FINAL_VALIDATION_PACKAGE.md` §4/§5.10). Paper 모드 주문
경로는 이번 배선과 무관하게 완전히 미변경이다. 이 배선은 아직 Codex 재검증을 거치지 않았으므로
이 절의 `BLOCKED` 상태와 위 승격 조건에는 여전히 영향을 주지 않는다 — 승격 여부는 다음 Codex
재검증 결과에 따른다.
