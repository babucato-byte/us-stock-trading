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

## 10. 청산 주문이 EXIT_SUBMITTED에 머물러 있음 (accepted 상태에서 진행 없음)

- **감지**(2026-07-26, CODEX-023 수정 이후): `positions/lifecycle.py`가 broker 응답의
  accepted/new/pending_* 상태를 더 이상 체결로 오판하지 않으므로, 이런 포지션은
  `EXIT_SUBMITTED`/`PARTIAL_EXIT_SUBMITTED`에 정상적으로 머무른다 — 이는 버그가 아니라 "아직
  실제 체결이 확인되지 않았다"는 정확한 상태다. 다만 이 상태가 비정상적으로 오래(예: 정규장
  마감이 임박했는데도) 지속되면 조사가 필요하다.
- **권장 상태**: 즉시 Kill Switch를 활성화할 필요는 없음 — 먼저
  `positions.lifecycle.reconcile_pending_exit(position_id, broker=broker)`를 호출해 broker의
  실제 주문 상태를 재조회한다(재주문하지 않음, 순수 조회).
- **절차**:
  1. `ops_dashboard.cli`로 해당 포지션의 `client_order_id`와 상태 확인.
  2. `reconcile_pending_exit()` 호출 — broker가 `filled`/`partially_filled`를 반환하면 포지션이
     정상적으로 갱신된다.
  3. broker가 해당 주문을 전혀 모른다고 응답하거나(주문 없음) 조회 자체가 실패하면 exit intent가
     `RECONCILIATION_REQUIRED`로 남는다 — 이 경우 자동 재주문하지 않으며, 사람이 broker
     대시보드에서 직접 주문 상태를 확인한 뒤 수동으로 처리 방향을 결정해야 한다.
  4. 장 마감이 임박했는데도 미해소 상태면 `kss.activate(kss.MANUAL_REVIEW, reason="청산 미확인 상태로 장마감 임박", activated_by=...)`.

## 11. Position store 손상 감지 (2026-07-26, CODEX-025 수정 이후 자동 대응)

- **감지**: `positions/store.py::load_all()`이 `PositionStoreCorruptedError`를 발생시키면,
  `positions.lifecycle.recover_on_restart()`가 이를 감지해 **자동으로**
  `kss.activate(kss.MANUAL_REVIEW, reason="position store unavailable on restart: ...", activated_by="system:recover_on_restart")`
  를 호출한다 — 운영자가 수동으로 활성화하지 않아도 이미 `MANUAL_REVIEW` 상태일 수 있다.
- **권장 상태**: `MANUAL_REVIEW`(이미 자동 전환되어 있을 가능성 높음) — `ENTRY_DISABLED`가 아닌
  이유: 손상된 store는 청산해야 할 포지션이 있는지조차 알 수 없으므로, 청산까지 허용하는
  `ENTRY_DISABLED`보다 전면적인 사람 개입이 필요.
- **절차**:
  1. `kill_switch_state.get_current_record()`로 자동 전환 여부와 사유 확인.
  2. **손상된 `POSITION_STORE.json` 파일을 절대 삭제하거나 자동 초기화하지 않는다** — 코드도
     이를 자동으로 하지 않으므로(원본 보존 확인, `positions/store.py::create_position()`도
     이미 손상 파일에 쓰기를 거부), 사람이 파일 내용을 직접 검사(백업이 있다면 비교)해 실제 손상
     범위를 파악한다.
  3. `broker.get_positions()`(또는 broker 대시보드)로 실제 계좌의 미결제 포지션 전체를 조회해
     로컬 기록과 대조한다.
  4. 손상 원인(디스크 오류, 동시 쓰기 충돌, 수동 편집 실수 등) 파악 후, 필요 시 파일을 수동으로
     복구(가장 최근 정상 백업 복원 등)하고 `recover_on_restart()`를 다시 실행해 정상 복구되는지
     확인.
  5. 확인 완료 후 해제 체크리스트를 거쳐 `release()`.

## 12. Live 진입이 CODEX-026 게이트에 의해 반복적으로 차단됨

- **감지**(2026-07-26, CODEX-026 수정 이후): `broker.config.is_live_mode`가 `True`인 상태에서
  `paper_strategy_order.submit_order(side="buy", ...)`가 423 응답과
  `response.data["blocked_reason"]`(예: `"symbol ... is not on the allow-list"`,
  `"no FX rate available"`, `"max daily entries reached"` 등)을 반복적으로 반환.
- **권장 상태**: Kill Switch 활성화 불필요 — 이 게이트 자체가 이미 해당 진입을 안전하게 차단하고
  있음(broker에 세션 호출이 전혀 가지 않음). 다만 "왜 계속 차단되는가"는 조사가 필요.
- **절차**:
  1. `blocked_reason` 문자열로 어떤 검사에서 막혔는지 확인(`live_readiness/order_gateway.py`의
     `LiveOrderBlockedError` 메시지와 1:1 대응).
  2. allow-list/FX rate/예산 등 `LiveEntryContext`를 조립하는 호출부의 설정값을 점검 — 이 게이트
     자체를 우회하거나 완화하는 코드 변경은 하지 않는다(기존 리스크 한도 완화 금지 원칙).
  3. 실제로 설정이 잘못됐다면(예: allow-list에 거래하려는 심볼이 누락) 운영자가 명시적으로 값을
     수정 — 코드가 자동으로 "일단 통과시키는" 방향으로 완화되어서는 안 됨.
  4. 이 프로젝트는 현재 `ENABLE_REAL_TRADING=False`/`live_dry_run`이 기본값이므로, 이 시나리오는
     실제 실거래 파일럿 준비/검토 단계에서만 실질적으로 발생한다.
  5. **갱신(2026-07-26, CODEX-029)**: `blocked_reason`이 `"order symbol ... does not match live
     entry context symbol ..."`이면, 승인된 context와 실제 제출된 symbol이 서로 다르다는 뜻이다
     — 이는 정상적인 fail-closed 차단이며, 코드가 두 값을 자동으로 맞추려 시도해서는 안 된다.
     호출부(전략 신호 → sizing → context 조립 → 실제 주문 제출)의 어느 단계에서 symbol이
     바뀌었는지 추적한다. 이 차단은 이제 `paper_strategy_order.submit_order()` 경로뿐 아니라
     `AlpacaBroker.submit_order()`를 직접 호출하는 경로에서도 동일하게 발생한다.

## 13. 청산 경로에서 SQLite와 JSON position 상태 불일치 의심 (2026-07-26, CODEX-028 수정 이후)

- **감지**: `ops_dashboard`나 `POSITION_STORE.json`을 직접 읽은 값이 실제와 다르게 보이거나,
  `positions.projection_status`가 `FAILED`로 기록된 포지션이 있음.
- **원인**: CODEX-028 수정 이후 SQLite(`positions`/`position_events` 테이블)가 유일한 canonical
  저장소이고, `POSITION_STORE.json`은 그 SQLite 커밋이 성공한 뒤에만 쓰는 best-effort projection
  이다 — projection 쓰기 자체가 실패해도(디스크 공간 부족 등) 거래 상태(SQLite)는 정상이며 절대
  롤백되지 않는다.
- **절차**:
  1. **JSON 파일이 아니라 SQLite(또는 `store.load_position()`/`store.load_all()`이 반환하는 값)를
     항상 신뢰한다.** `POSITION_STORE.json`은 참고용 스냅샷일 뿐이다.
  2. `positions.projection_status`/`positions.projection_updated_at` 컬럼을 조회해 마지막
     projection 쓰기 성공/실패 시각을 확인한다.
  3. projection이 오래됐거나 손상됐다고 의심되면 `positions.store.regenerate_projection()`을
     실행해 SQLite에서 전체 JSON을 다시 생성한다 — 이 함수는 읽기 전용으로 SQLite를 조회할 뿐,
     거래 상태 자체를 변경하지 않는다.
  4. SQLite 데이터베이스 파일 자체가 손상된 경우는 이 시나리오가 아니라 시나리오 11(Position
     store 손상 감지)을 따른다 — `check_store_health()`로 구분한다.

## 14. 청산 주문이 broker에 거부됐는데 position이 EXIT_SUBMITTED에 계속 남아 있음 (2026-07-26, CODEX-032 수정 이후)

- **감지**: broker가 청산 주문을 명시적으로 거부(4xx/5xx)했는데도 position이 `MANUAL_REVIEW`로
  전이하지 않고 `EXIT_SUBMITTED`에 머물러 있음.
- **원인(수정 전 과거 결함, 참고용)**: CODEX-032 수정 이전에는 `eil.mark_aborted()`가 position의
  `MANUAL_REVIEW` 전이와 별도 트랜잭션으로 커밋되어, 두 번째 write가 실패하면 exit intent만
  terminal `ABORTED`로 남고 position은 영구히 `EXIT_SUBMITTED`에 갇힐 수 있었다. 수정 이후
  (커밋 `55f3806`)에는 두 전이가 하나의 SQLite 트랜잭션으로 원자적으로 커밋되므로 이 특정
  불일치는 재발하지 않아야 한다.
- **절차**:
  1. `state_store/exit_intent_ledger.py`의 `get_active_intent(conn, position_id)`로 이 position에
     대한 active exit intent가 있는지 확인한다.
  2. active intent가 있으면 `positions.lifecycle.reconcile_pending_exit(position_id, broker=...)`
     을 실행해 broker의 실제 최신 상태로 재조정한다 — 절대 수동으로 position 상태를 직접
     덮어쓰지 않는다.
  3. active intent가 없는데도 position이 `EXIT_SUBMITTED`에 남아 있다면(CODEX-032 이전 버전에서
     발생한 과거 데이터이거나 예기치 않은 새로운 결함), 자동 복구 경로가 없으므로 운영자가
     broker의 실제 포지션/주문 상태를 직접 조회해 수동으로 `MANUAL_REVIEW`로 전이시키고 후속
     청산을 수동 처리한다.
  4. 이 시나리오가 CODEX-032 수정 이후에도 재현되면(즉 수정 자체가 실패했다는 뜻이므로) 코드
     수정 없이 즉시 Kill Switch를 `MANUAL_REVIEW`로 활성화하고 사용자에게 보고한다 — 리스크
     한도를 완화하거나 재조정 로직을 우회하는 방향으로 임시 수정하지 않는다.

## 15. Live 진입 예산이 실제보다 적게 남은 것으로 보이거나(예약이 반환되지 않음), 반대로 예상보다 쉽게 승인됨 (2026-07-26, CODEX-031 수정 이후)

- **감지**: `live_readiness/entry_reservation_ledger.py`의 `build_snapshot()`이 보고하는
  `active_notional_krw`/`active_position_count`/`today_entry_count`가 실제 broker 계좌 상태와
  맞지 않아 보임.
- **원인**: 30,000원 총 예산은 **파일럿 전체에 걸친 누적 배분**으로 설계되어 있어 포지션이
  종료돼도 절대 반환되지 않는다(의도된 동작, `docs/autonomous/DECISION_LOG.md`의 CODEX-024/026/
  028/031/032/033 섹션 결정 3 참고) — "예산이 줄어들지 않는다"는 관측은 대부분 버그가 아니다.
  반대로 동시 포지션 수(`active_position_count`)는 연결된 position이 종료되면 자동으로
  감소한다.
- **절차**:
  1. `live_entry_reservations` 테이블을 직접 조회해 각 예약의 `state`(RESERVED/COMMITTED/
     RELEASED)와 `position_id` 연결 여부를 확인한다.
  2. broker 호출이 성공했는데도 예약이 `RESERVED`에 머물러 있다면(commit이 실패한 경우),
     `positions/lifecycle.py::enter_position()`이 응답을 어떻게 처리했는지 확인 — 이 상태 자체가
     예산을 과소 사용으로 잘못 보고하지는 않는다(RESERVED도 활성으로 집계됨, fail-closed).
  3. broker 호출이 실패/거부됐는데도 예약이 `RELEASED`로 전환되지 않았다면, 해당 예약은
     계속 예산을 점유한다 — 이는 의도된 fail-closed 동작(release 실패 시 더 보수적으로 남음)이며
     코드를 우회해 수동으로 `RELEASED`로 바꾸는 것은 리스크 한도 완화에 해당하므로 하지 않는다.
     실제로 그 예약이 잘못된 것으로 확인되면(예: 실제로 broker가 절대 받지 않은 주문), 사용자
     승인 하에만 수동 정정한다.
  4. `PILOT_TOTAL_BUDGET_KRW`/`MAX_CONCURRENT_LIVE_POSITIONS`/`MAX_DAILY_LIVE_ENTRIES` 값 자체를
     완화하는 코드 변경은 이 런북의 범위가 아니다 — 사용자의 명시적 지시 없이는 수행하지 않는다.

## 공통 유의사항

- 모든 활성화/해제는 `kill_switch_state.py`의 감사 이력(`get_history()`)에 남으므로,
  사고 대응 종료 후 반드시 `get_history()`로 전체 타임라인을 재확인하고 사고 보고서에 첨부한다.
- `expires_at`은 참고용일 뿐 자동 해제를 유발하지 않는다(`kill_switch_state.py:26-29`) — 위
  모든 시나리오에서 "시간이 지나면 자동으로 풀리겠지"라는 가정을 하지 않는다.
