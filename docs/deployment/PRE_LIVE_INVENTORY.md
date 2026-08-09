# PRE_LIVE_INVENTORY — 실거래 직전 lifecycle 재고조사

기준 release: `6b87df87d945fe8a297978219e0ed142f6c1b5a0`
작성 시점 posture: OBSERVE (`ENTRY_DISABLED=true`, `KIS_LIVE_ORDER_ENABLED=false`,
`LIVE_ROLLOUT_ENABLED=false`, `LIVE_ROLLOUT_ALLOWED_SYMBOLS` 비어 있음)

이 문서는 **코드를 읽어 작성한 재고조사**다. 설계 의도가 아니라 현재 코드가 실제로
하는 일을 적는다. 빠진 것은 빠진 것으로 적는다.

---

## PHASE 1 — live lifecycle 단계별 실체

| # | stage | production file | function/class | input | authoritative state | failure behavior | Slack event | DB/state mutation |
|---|-------|-----------------|----------------|-------|---------------------|------------------|-------------|-------------------|
| 1 | scanner | `daily_candidate_scanner.py` | `scan()`, `load_scan_universe()` | `universe_tradable.csv` 우선, 없으면 `universe.csv` | CSV 파일 | 파일 없음/파싱 실패 → `universe.csv` 폴백. 0행 tradable 파일은 그대로 존중 | `send_slack_alert` (스캔 요약) | `candidates.csv`, `order_candidates.csv`, `strong_candidates.csv`, `previous_candidates.csv` |
| 2 | signal | `paper_strategy_order.py::analyze_stock` → `domain/signal.py::build_signal` | `build_signal()` | 종목·점수·가격 | in-memory `Signal` | `SignalError` → 후보 차단 | **없음** | 없음 |
| 3 | candidate cash sizing | `brokers/kis_broker.py` | `get_orderable_usd()` (TTTS3007R) | (symbol, exchange, limit price) | KIS 실응답 `output.ord_psbl_frcr_amt` | `KISOrderableCashUnavailableError` → `ORDERABLE_CASH_UNAVAILABLE`, 0으로 격하 안 함 | **없음** | 없음 |
| 3b | whole-share sizing | `domain/cash_sizing.py` | `whole_shares_affordable()` | orderable, limit price | 순수 계산 (Decimal floor) | 사용 불가 입력 → 0주 | **없음** | 없음 |
| 4 | OrderIntent | `domain/order_intent.py` | `OrderIntent` | 수량·지정가 | in-memory | `OrderIntentError` → 차단 | **없음** | 없음 |
| 5 | buy gate | `execution/order_gate.py` | `evaluate_buy_gate()` (19단계) | `BuyGateContext` | 순수 판정 | `OrderGateBlockedError(code=…)` → transport 0 | **없음** | 없음 |
| 6 | entry limits | `execution/entry_limits.py` | `collect()` + `_check_entry_limits()` | KIS positions + `kis_order_idempotency` | KIS 보유 + durable 원장 | 읽기 실패 → `POSITION_LIMIT_STATE_UNKNOWN` / `DAILY_ENTRY_STATE_UNKNOWN`, 0 가정 없음 | **없음** | 없음 (읽기만) |
| 7 | reservation/idempotency | `execution/idempotency.py` | `register()` — transport **이전**, `single_run_lock()` 안 | internal_order_id, signal_id, symbol, side, trading_date(ET) | `kis_order_idempotency` | `DuplicateOrderAttemptError` → 차단 | **없음** | **INSERT** + `order_state_events` 생성 이벤트 |
| 8 | KIS submit | `execution/execution_engine.py::_submit_new_order` → `brokers/kis_broker.py::submit_order` | `submit_order()` | AuthorizedExecution | KIS 응답 | 모호 실패 → `KISAmbiguousResponseError` → **UNKNOWN, 재시도 금지** | **없음** ← 최대 공백 | `orders` 상태 CAS, `order_state_events` |
| 9 | open order | `brokers/kis_broker.py` | `get_open_orders()` (TTTS3018R) | 계좌 | KIS | 실패 → `KISAccountSweepError` | **없음** | 없음 |
| 10 | fill | `kis_position_manager.py` | `sync_kis_fills_and_manage_exits()`, `_find_kis_fill_for_order()` | TTTS3035R 체결 | KIS + `fills` | 읽기 실패 → 이번 tick 중단, 기록 없음 | **없음** | `fills`, `positions` |
| 11 | position | `positions/lifecycle.py`, `positions/store.py` | `record_fill()`, `finalize_stop_and_targets_from_fill()` | 체결 수량·가격 | `positions` (canonical SQLite) | 전이 위반 → 예외 | **없음** | `positions`, `position_events` |
| 12 | reconciliation | `reconciliation/snapshot.py`, `reconciliation/reconciliation_state.py` | `build_snapshot()`, `record_result()` | KIS 포지션·미체결·체결 vs 내부 | `RECONCILIATION_STATE_FILE` (strict schema v1) | 불일치 → gate `RECONCILIATION` 차단 | `scripts/run_reconciliation.py`에서 `alerts.send_alert` | 스냅샷 파일 atomic 갱신 |
| 13 | exit condition | `positions/lifecycle.py` | `classify_exit()`, `decide_exit()` | 가격·보유시간·상태 | `positions` | — | **없음** | 없음 |
| 14 | sell intent | `state_store/exit_intent_ledger.py` | — | 종목·수량·사유 | `exit_intents` | — | **없음** | `exit_intents` |
| 15 | KIS sell | `execution/execution_engine.py::submit_sell_order` → `brokers/kis_broker_adapter.py` | `evaluate_sell_gate()` 통과 후 transport | SellGateContext | KIS 응답 | UNKNOWN 동일 정책 | **없음** | `orders`, `order_state_events` |
| 16 | sell fill | `kis_position_manager.py` | 동일 sync 경로 | TTTS3035R | `fills` | — | **없음** | `fills`, `positions` |
| 17 | position zero | `positions/lifecycle.py` | 상태 전이 → terminal | 잔량 0 | `positions` | — | **없음** | `positions`, `position_events` |
| 18 | realized PnL | `performance_analytics.py`, `positions/store.py` | 집계 | `fills`/`positions` | SQLite | — | **없음** | 없음 |
| 19 | daily summary | `slack_report.py::build_daily_summary` | CSV 기반 집계 | 운영 CSV | CSV | — | `send_slack_message` (**Alpaca/paper 기준 집계**) | 없음 |

### 확인된 exit 사유 (production `positions/lifecycle.py`)

```text
STOP_LOSS · EOD_FORCED_CLOSE · TIME_STOP · TRAILING_BREAKEVEN
TARGET_1 계열 (TARGET_1_ACTIVE / PARTIAL_EXITED / TRAILING 상태 전이)
RISK_REDUCTION_REASONS = {STOP_LOSS, EOD_FORCED_CLOSE}
```

`TIME_STOP`과 `TRAILING`은 `config/live_exit_flags.py`의
`LIVE_ENABLE_TIME_STOP` / `LIVE_ENABLE_TRAILING_STOP`으로 기본 비활성이다.

### 상태 저장소

```text
SQLite (state_store/schema.py) — orders · fills · positions · position_events
                                 strategy_runs · risk_events · kill_switch_events
                                 exit_intents · live_entry_reservations
                                 kis_order_idempotency · order_state_events
                                 shadow_audit_events
파일  — RECONCILIATION_STATE_FILE (strict schema v1, atomic + inode 바인딩)
        KILL_SWITCH / OPERATIONS_HALT 상태 파일
        shared/state/kis-readonly-preflight.lock (inter-process single-run flock)
CSV   — universe.csv · universe_tradable.csv · candidates 계열 · order_history.csv
        strategy_performance.csv
```

---

## PHASE 2 — Slack lifecycle 감사

### 현재 존재하는 것

| 구성요소 | 파일 | 용도 |
|---|---|---|
| 기본 전송 | `slack_utils.py` | `send_slack_message()` (일반), `send_slack_alert()` (경고). 각각 `SLACK_WEBHOOK_URL` / `SLACK_ALERT_WEBHOOK_URL` |
| 건전성 추적 | `notification_health.py` | `send_with_health_tracking()` — 전송 실패를 기록하고 거래 로직에 전파하지 않음 |
| 운영 알림 | `operations/alerts.py` | `send_alert()` + `format_order_blocked_message` / `format_reconciliation_mismatch_message` / `format_unknown_order_message` (**포매터는 있으나 KIS 경로에서 호출되지 않음**) |
| 일일 요약 | `slack_report.py` | `build_daily_summary()` — **Alpaca/paper CSV 기준** |
| 스캐너 | `daily_candidate_scanner.py` | 스캔 결과 요약 |
| 기존 paper 경로 | `paper_strategy_order.py` | order blocked / filled / rejected (**Alpaca 경로 전용**) |

### KIS live lifecycle의 Slack 실태

```text
kis_live_trading.py          0건
kis_position_manager.py      0건
brokers/kis_broker.py        0건
brokers/kis_broker_adapter.py 0건
live_pilot/armed.py          0건
execution/execution_engine.py 3건 (fail-stop 알림 한 종류)
live_pilot/runner.py         1건
```

**결론: KIS 실거래 lifecycle은 사실상 Slack 무음이다.** 주문 제출, 체결, 부분체결,
UNKNOWN, 취소, 매도, 실현손익 중 어느 것도 알림이 없다. `operations/alerts.py`에
UNKNOWN·reconciliation 포매터가 이미 있으나 KIS 경로가 호출하지 않는다.

### 22개 요구 이벤트 대비 구현 현황

| # | 이벤트 | 상태 | 비고 |
|---|--------|------|------|
| 1 | MARKET_START / LIVE_SESSION_READY | **없음** | preflight가 stdout으로만 출력 |
| 2 | BUY_CANDIDATE_SELECTED | **없음** | 스캐너 요약은 있으나 후보별 진입 맥락 없음 |
| 3 | LIVE_ORDER_PREPARED | **없음** | transport 직전 지점은 `_audit_before_transport`로 존재 (audit만) |
| 4 | ORDER_SUBMITTED | **없음** | |
| 5 | ORDER_ACCEPTED / PENDING | **없음** | |
| 6 | PARTIAL_FILL | **없음** | 부분체결 판정 로직(`requested_quantity`)은 존재 |
| 7 | FILL_COMPLETED | **없음** | Alpaca 경로에만 유사 알림 |
| 8 | EXIT_TRIGGERED | **없음** | 사유 분류는 `classify_exit()`에 존재 |
| 9 | SELL_SUBMITTED | **없음** | |
| 10 | SELL_FILLED (realized PnL) | **없음** | |
| 11 | ORDER_REJECTED | **없음** | Alpaca 경로에만 |
| 12 | ORDER_UNKNOWN (+RETRY=BLOCKED) | **없음** | 포매터만 존재, 미호출 |
| 13 | CANCEL_REQUESTED | **없음** | |
| 14 | CANCEL_COMPLETED | **없음** | |
| 15 | CANCEL_FAILED | **없음** | |
| 16 | RECONCILIATION_MISMATCH | **부분** | `scripts/run_reconciliation.py`에서만 |
| 17 | POSITION_MISMATCH | **없음** | |
| 18 | KIS_API_FAILURE | **없음** | |
| 19 | DB_FAILURE | **부분** | fail-stop 시 `execution_engine`에서 |
| 20 | HALT_ACTIVATED | **없음** | |
| 21 | KILL_SWITCH_ACTIVATED | **없음** | |
| 22 | DAILY_SUMMARY | **부분** | 존재하나 Alpaca/paper CSV 기준, KIS 실거래 집계 아님 |

구현 필요: **완전 신규 17건, 보강 3건**(16·19·22), 재사용 가능 2건(전송·건전성 추적).

---

## 외부 의존성 확인 결과

### Slack — 사용 가능

```text
SLACK_WEBHOOK_URL        서버 env에 존재
SLACK_ALERT_WEBHOOK_URL  서버 env에 존재
```

### KIS 모의투자(paper) 자격증명 — **없음**

```text
KIS_PAPER_APP_KEY      absent
KIS_PAPER_APP_SECRET   absent
KIS_PAPER_ACCOUNT_NO   absent
KIS_ENV                live
```

서버 env 44개 키 중 paper 계정 관련 항목이 하나도 없다.

**영향**: paper 계정에서 실제 주문·취소를 넣어 wire 응답을 확인하는 경로
(order_path, cancel_path, cancel_tr_id_paper, cancel_price_field_rule)를 **실행할 수
없다.** 실전 계정으로 대체하는 것은 금지되어 있고, 그렇게 해서도 안 된다.

이 4건이 pending으로 남는 한 ARMED는 13개 중 7개 confirmed에 머문다.

**해결에 필요한 것 (사용자 조치 사항)**: 한국투자증권 모의투자 계좌 개설 및 모의투자용
App Key/Secret 발급. 이는 브로커 계정·본인확인·비밀값이 관여하므로 대신 수행할 수 없다.
