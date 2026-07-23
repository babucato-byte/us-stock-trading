# Incident Response Runbook

이 문서는 Kill Switch(`kill_switch_state.py`) 활성화가 필요할 수 있는 사고 유형 후보 9종과
각각의 대응 절차를 정리한다. 상태 정의·활성화/해제 절차 자체는
[KILL_SWITCH_RUNBOOK.md](./KILL_SWITCH_RUNBOOK.md)를 따른다.

2026-07-24부터(CODEX-020 수정, 커밋 `66eda8a`) kill switch 판정은 `paper_strategy_order.py`
wrapper뿐 아니라 `broker/alpaca_client.py::AlpacaBroker._request()` 자체에서도 재조회되므로,
아래 절차로 `kss.activate()`를 호출하면 wrapper를 거치지 않은 direct broker 호출(예: 수동
스크립트, 향후 신규 호출부)에도 동일하게 적용된다. Codex 독립 재검증은 아직 완료되지 않았다.

각 시나리오의 "권장 상태"는 후보이며, 실제 판단(어느 상태로 얼마나 활성화할지)은 운영자가
사고 현장의 구체적 정황을 보고 결정한다.

## 1. 일일 손실 한도 초과

- **감지**: `account_risk.check_daily_loss_limit()`가 계좌 `daily_return`이
  `MAX_DAILY_LOSS_RATE = -0.02`(`risk_config.py:2`) 이하로 내려가면 예외를 발생시킨다
  (`account_risk.py:11-26`).
- **권장 상태**: `ALL_TRADING_DISABLED` — 이미 발생한 손실의 원인이 파악되기 전까지는
  신규 진입은 물론 자동 청산 로직도 추가 손실을 만들 수 있으므로 사람이 먼저 포지션을 검토한다.
- **절차**:
  1. `kss.activate(kss.ALL_TRADING_DISABLED, reason="일일 손실 한도(-2%) 초과", activated_by=...)`
  2. 현재 포지션과 미체결 주문을 broker에서 직접 조회.
  3. 손실 원인(단일 심볼 급락, 전체 시장 이벤트, 로직 오류 등) 파악.
  4. 원인 파악 후 필요 시 `MANUAL_REVIEW`로 격상하거나, 안전 확인되면 해제 체크리스트를 거쳐 `release()`.

## 2. 연속 주문 오류(연속 제출 실패/거절)

- **감지**: `paper_strategy_order.py`의 주문 루프에서 `submitted/failed/blocked/skipped` 집계
  중 `failed`가 반복적으로 누적되는 경우. (임계 건수는 `TBD(운영자 기입)` — 현재 코드에
  "연속 N회 실패 시 자동 차단" 같은 카운터/임계값은 구현되어 있지 않다.)
- **권장 상태**: `ENTRY_DISABLED` — 신규 진입만 막고 기존 포지션 청산은 계속 허용.
- **절차**:
  1. 실패 로그/Slack 알림(`_notify_order_rejected()` 등, t7 커밋)에서 오류 유형(거절 사유,
     타임아웃, 인증 오류 등) 확인.
  2. broker API 상태(레이트리밋, 인증 만료, 서비스 장애 등) 점검.
  3. `kss.activate(kss.ENTRY_DISABLED, reason="연속 주문 실패 N건", activated_by=...)`
  4. 원인 조치(예: API 키 갱신) 후 해제 체크리스트를 거쳐 `release()`.

## 3. 데이터 지연(시세/계좌 데이터 staleness)

- **감지**: 시세 또는 계좌 데이터의 최신성 검증 로직. (현재 코드베이스에 전용 staleness
  감지/임계값 모듈은 확인되지 않음 — `TBD(운영자 기입)`: 실제 사용 중인 데이터 지연 감지 방법.)
- **권장 상태**: `ALL_TRADING_DISABLED` — 지연된 데이터 기반의 신규 진입뿐 아니라 청산 판단도
  잘못될 수 있으므로 전면 차단.
- **절차**:
  1. `kss.activate(kss.ALL_TRADING_DISABLED, reason="시세/계좌 데이터 지연", activated_by=...)`
  2. 데이터 소스(스캐너, broker API) 상태 확인.
  3. 데이터 최신성 복구 확인 후 해제 체크리스트를 거쳐 `release()`.

## 4. Broker 상태 불일치(로컬 기록 vs 실제 broker 계정)

- **감지**: `paper_strategy_order.py:548` `reconcile_pending_orders(broker)` 실행 결과,
  로컬 `order_history.csv`/`order_intent_ledger.py` 상태와 broker 실제 주문/포지션 상태가
  불일치.
- **권장 상태**: `MANUAL_REVIEW` — 불일치는 로직 결함이나 이중 처리 가능성을 의미하므로,
  사람이 확인하기 전까지 어떤 자동 거래도 재개하지 않는다는 신호가 필요.
- **절차**:
  1. `kss.activate(kss.MANUAL_REVIEW, reason="broker-로컬 reconciliation 불일치", activated_by=...)`
  2. 불일치 건별로 broker 측 실제 상태를 진실 소스(source of truth)로 놓고 로컬 기록을 정정.
  3. 재발 방지가 필요하면 코드 수정은 별도 태스크로 분리(본 런북은 코드 수정 대상 아님).
  4. 정정 완료 및 재확인 후 해제 체크리스트를 거쳐 `release()`.

## 5. 미확인 부분체결(partial fill)

- **감지**: 주문이 `PARTIALLY_FILLED` 상태로 남아 있고, 잔여 수량 처리 방침이 확인되지 않은 경우
  (`_notify_order_filled()` 등에서 `PARTIALLY_FILLED` 이벤트 알림, t7 커밋 참고).
- **권장 상태**: `ENTRY_DISABLED` — 해당 심볼/계좌의 실제 노출(exposure)이 불확실하므로 신규
  진입을 멈추고, 기존 포지션 정리(청산)는 허용해 노출을 줄일 수 있게 한다.
- **절차**:
  1. broker에서 해당 주문의 실제 체결 수량/잔여 수량 확인.
  2. `kss.activate(kss.ENTRY_DISABLED, reason="미확인 부분체결", activated_by=...)`
  3. 노출 재계산 후 `account_risk.check_account_exposure_limits()` 기준 재검증.
  4. 확인 완료 후 해제 체크리스트를 거쳐 `release()`.

## 6. Slack/관제 알림 장애

- **감지**: `notification_health.get_status()`가 `FAILED`
  (연속 실패 `failure_threshold()`, 기본값 `DEFAULT_FAILURE_THRESHOLD = 5`,
  `notification_health.py:57` 이상 도달).
- **자동 조치**: `notification_health.py:185-201`의 `_escalate_kill_switch()`가 이미
  `ACTIVE` 상태였다면 자동으로 `ENTRY_DISABLED`로 격상한다. 더 restrictive한 상태(`MANUAL_REVIEW`
  등)를 사람이 이미 설정해 두었다면 그 상태를 덮어쓰지 않는다.
- **권장 상태**: 자동 조치 결과인 `ENTRY_DISABLED` 유지, 필요시 운영자가 `ALL_TRADING_DISABLED`로
  추가 격상.
- **절차**:
  1. `notification_health.summarize()`로 `last_error_kind`/`last_status_code` 확인.
  2. Slack webhook/네트워크 상태 점검, 대체 알림 채널(이메일 등, `TBD(운영자 기입)`)로 공지.
  3. 알림 채널 복구 확인(`notification_health.record_success()`가 다시 기록되는지).
  4. 복구 확인 후 해제 체크리스트를 거쳐 `release()` — 알림 채널이 복구되었다는 사실만으로
     자동 복귀되지 않으므로 반드시 사람이 `release()`를 호출해야 한다.

## 7. 상태 복구 실패(상태 파일 손상 등)

- **감지**: `KILL_SWITCH_STATE.json`이 존재하지만 파싱 실패 또는 구조 불량
  (`kill_switch_state.py:97-120`). 이 경우 시스템은 이미 자동으로 `MANUAL_REVIEW`
  (`FAIL_CLOSED_STATE`, `kill_switch_state.py:49`)로 fail-closed 되어 있다.
- **권장 상태**: 이미 자동 적용된 `MANUAL_REVIEW` 유지.
- **절차**:
  1. `kss.get_current_record()`를 호출해 `reason` 필드에 담긴
     `CORRUPTED_STATE_FILE: ...` 상세 오류 메시지 확인.
  2. 손상된 `KILL_SWITCH_STATE.json`의 원인(디스크 오류, 동시 쓰기 충돌 등) 조사.
  3. 상태 파일을 정상 구조로 복구하거나, 파일을 제거해 기본값(`ACTIVE`)으로 재시작할지
     결정. 어느 쪽이든 `TBD(운영자 기입)`: 복구 판단 근거를 기록한 뒤 `release()`
     또는 `activate()`로 원하는 상태를 명시적으로 재설정한다(파일을 그냥 삭제하는 것만으로는
     감사 이력이 끊기므로 지양).

## 8. 운영자 수동 활성화(위 7종 외의 임의 사유)

- **감지**: 사람이 위 1~7 범주에 해당하지 않는 사유(예: 예정된 점검, 외부 이벤트, 의심스러운
  시장 상황)로 선제적으로 거래를 중단하고자 하는 경우.
- **권장 상태**: 사유에 맞게 `ENTRY_DISABLED`/`ALL_TRADING_DISABLED`/`MANUAL_REVIEW` 중 운영자가 선택.
- **절차**:
  1. `kss.activate(state, reason="TBD(운영자 기입: 구체적 사유)", activated_by="TBD(운영자 기입)")`
  2. 사유가 해소되었다고 판단되면 해제 체크리스트([KILL_SWITCH_RUNBOOK.md](./KILL_SWITCH_RUNBOOK.md)
     3.1절)를 모두 확인한 뒤 `release()`.

## 9. API credential 회전/삭제/노출 의심

- **감지**: `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`가 회전, 삭제되거나 유출이 의심되는 경우.
  이미 생성된 `AlpacaBroker` 인스턴스는 생성 시점에 credentials를 캡처하므로, 환경변수만
  바꾼다고 실행 중인 프로세스가 즉시 새 값을 쓰지는 않는다 — CODEX-018 잔여분 수정(커밋
  `ed452da`)에 따라 `_validate_runtime_safety()`가 매 요청마다 현재 환경 credentials를
  다시 읽어 생성 시점 캡처값과 비교하므로, 값이 더 이상 일치하지 않으면(회전/삭제/공백) 그
  프로세스의 이후 모든 요청이 세션 호출 전에 차단된다(`RuntimeError`, credential 값 자체는
  예외 메시지에 노출되지 않음).
- **권장 상태**: `ALL_TRADING_DISABLED` — credential 유출이 의심되면 신규 진입뿐 아니라 청산
  판단도 신뢰할 수 없는 계정 상태에서 이루어질 수 있으므로 전면 차단.
- **절차**:
  1. `kss.activate(kss.ALL_TRADING_DISABLED, reason="API credential 회전/유출 의심", activated_by=...)`
  2. Alpaca 대시보드에서 즉시 기존 키를 폐기(revoke)하고 신규 키를 발급.
  3. `.env`(또는 배포 secret store)를 신규 값으로 갱신. **기존에 이미 실행 중인 프로세스는
     재시작 없이는 새 값을 읽지 않는다** — credential 재검증 gate는 새 값을 자동으로 재캡처하지
     않고 오직 불일치를 차단만 하므로, 프로세스를 재시작해 새 `BrokerConfig`/`AlpacaBroker`를
     생성해야 정상 동작이 재개된다.
  4. 재시작 후 새 credential로 정상 조회(`get_account`)가 되는지 확인.
  5. 확인 완료 후 해제 체크리스트를 거쳐 `release()`.

## 공통 유의사항

- 모든 활성화/해제는 `kill_switch_state.py`의 감사 이력(`get_history()`)에 남으므로,
  사고 대응 종료 후 반드시 `get_history()`로 전체 타임라인을 재확인하고 사고 보고서에 첨부한다.
- `expires_at`은 참고용일 뿐 자동 해제를 유발하지 않는다(`kill_switch_state.py:26-29`) — 위
  모든 시나리오에서 "시간이 지나면 자동으로 풀리겠지"라는 가정을 하지 않는다.
