# Kill Switch Runbook

대상 모듈: `kill_switch_state.py` (다단계 상태 머신), `kill_switch.py` (바이너리 halt +
`kill_switch_state` re-export), `notification_health.py` (알림 장애 시 자동 상승).

## 1. 상태 정의 (실측: `kill_switch_state.py:41-49`)

다단계 상태는 아래 4개이며, 허용 범위가 넓은 순서대로 나열한다.

| 상태 | 의미 | 신규 진입(entry) 주문 | 청산/종료(exit·liquidation) 주문 |
|---|---|---|---|
| `ACTIVE` | 정상 운영. 아무 제한 없음 | 허용 | 허용 |
| `ENTRY_DISABLED` | 신규 진입만 차단 | 차단 | 허용 |
| `ALL_TRADING_DISABLED` | 신규 진입·청산 모두 차단(조회만 가능) | 차단 | 차단 |
| `MANUAL_REVIEW` | 사고(incident)가 사람 검토 대기 중. 주문 제한은 `ALL_TRADING_DISABLED`와 동일하지만, 재개 전 반드시 사람이 확인해야 함을 신호 | 차단 | 차단 |

판정 함수(실측 근거):
- `is_entry_allowed()` → `get_state() == ACTIVE`일 때만 `True` (`kill_switch_state.py:138-140`)
- `is_liquidation_allowed()` → `get_state() in (ACTIVE, ENTRY_DISABLED)`일 때 `True` (`kill_switch_state.py:143-145`)

Fail-closed 설계: 상태 파일(`KILL_SWITCH_STATE.json`, 경로는 `KILL_SWITCH_STATE_FILE` 환경변수로
override 가능, 기본값은 모듈과 같은 디렉터리)이 없으면 `ACTIVE`(과거 동작과 호환), 있는데 파싱이
실패하거나 구조가 깨졌으면 `ACTIVE`가 아니라 가장 보수적인 `MANUAL_REVIEW`로 처리한다
(`kill_switch_state.py:89-120`, `FAIL_CLOSED_STATE = MANUAL_REVIEW`).

이와 별도로 `kill_switch.py`의 바이너리 halt(`is_trading_halted()`)는 Fail-open 설계다:
`TRADING_HALTED` 환경변수 또는 `KILL_SWITCH` 센티널 파일이 있으면 `True`(halt), 둘 다 없으면
`False`(정상)이며, 이 동작은 다단계 상태 머신 도입과 무관하게 그대로 유지된다(`kill_switch.py:22-26`).

## 2. 활성화 절차 (킬스위치 켜기)

`kill_switch_state.activate(state, reason, activated_by, expires_at=None, incident_id=None)`
(`kill_switch_state.py:148-183`)를 호출한다.

```python
import kill_switch_state as kss

kss.activate(
    kss.ENTRY_DISABLED,          # 또는 ALL_TRADING_DISABLED / MANUAL_REVIEW
    reason="일일 손실 한도 근접 관찰",
    activated_by="TBD(운영자 기입: 실제 활성화한 사람 식별자)",
    incident_id="TBD(운영자 기입: 있는 경우)",
)
```

- `state`는 `ACTIVE`/`ENTRY_DISABLED`/`ALL_TRADING_DISABLED`/`MANUAL_REVIEW` 중 하나여야 하며,
  그 외 값은 `KillSwitchStateError`.
- `reason`과 `activated_by`는 필수(빈 값이면 `KillSwitchStateError`) — 누가, 왜 활성화했는지
  감사 기록 없이는 활성화할 수 없다.
- 이미 같은 상태이면 `activated_at`은 보존되고 `reason`/`expires_at`/`incident_id`만 갱신되며,
  반복 활성화 시도 자체도 `history`에 새 스냅샷으로 남는다(`kill_switch_state.py:166-171`).
- `expires_at`은 참고용 메타데이터일 뿐, 자동 해제에 쓰이지 않는다. 만료 시각이 지나도
  사람이 `release()`를 호출하기 전까지는 계속 활성 상태로 남는다.

레거시 바이너리 halt(즉시 전면 차단이 필요한 응급 상황)는 별도로 다음 중 하나로 켠다:
- `KILL_SWITCH` 센티널 파일 생성: `touch KILL_SWITCH` (저장소 루트, `kill_switch.py:14`
  `BASE_DIR / "KILL_SWITCH"`, `KILL_SWITCH_FILE` 환경변수로 경로 override 가능)
- `TRADING_HALTED=true` 환경변수 설정

## 3. 해제 절차 (킬스위치 끄기, `ACTIVE`로 복귀)

`kill_switch_state.release(released_by, reason=None)` (`kill_switch_state.py:186-213`)를
호출한다. `released_by`는 필수(빈 값이면 `KillSwitchStateError`) — 해제는 언제나 사람의
명시적 승인으로만 이루어지며 자동으로 일어나지 않는다.

```python
import kill_switch_state as kss

kss.release(
    released_by="TBD(운영자 기입: 실제 해제 승인자)",
    reason="TBD(운영자 기입: 원인 조치 내용)",
)
```

이미 `ACTIVE`이면 아무 변화 없이 현재 레코드를 그대로 반환한다.

바이너리 halt를 켠 경우에는 해제도 별도로 필요하다: `KILL_SWITCH` 파일을 삭제하거나
`TRADING_HALTED` 환경변수를 해제(unset)한다.

### 3.1 해제 전 체크리스트 (모두 확인 후에만 해제)

1. **원인 확인** — 활성화 당시 기록된 `reason`/`incident_id`(`kss.get_current_record()`로 조회,
   `kill_switch_state.py:128-130`)에 명시된 근본 원인이 실제로 조치되었는지 확인.
2. **미체결 주문 확인** — broker 계정의 open orders를 조회해 예상치 못한 미체결 주문이
   없는지 확인. (`TBD(운영자 기입)`: 확인에 사용한 broker API 응답/스크린샷 근거)
3. **현재 포지션 확인** — broker 계정의 현재 포지션이 예상 범위 내인지 확인.
   (`TBD(운영자 기입)`: 확인 결과)
4. **broker-로컬 reconciliation** — `paper_strategy_order.py:548`의
   `reconcile_pending_orders(broker)`를 실행해 로컬 기록(`order_history.csv`,
   `order_intent_ledger.py`)과 broker 실제 상태가 일치하는지 확인.
   (`TBD(운영자 기입)`: 실행 결과)
5. **데이터 최신성 확인** — 시세/계좌 데이터가 지연 없이 최신인지 확인.
   (`TBD(운영자 기입)`)
6. **Slack/대체 알림 채널 확인** — `notification_health.get_status()`
   (`notification_health.py:204-214`)가 `HEALTHY` 또는 `UNKNOWN`(아직 전송 이력 없음)인지 확인.
   `DEGRADED` 또는 `FAILED`이면, 해제 전에 대체 알림 경로를 확보하거나 그 사실을 인지한 채로
   진행할지 명시적으로 결정한다. `FAILED` 상태는 이미 `notification_health.py:185-201`의
   `_escalate_kill_switch()`에 의해 자동으로 `ENTRY_DISABLED`가 걸려 있을 수 있으므로,
   해제 후 즉시 재상승하지 않도록 알림 채널 자체를 먼저 복구한다.
7. **운영자 명시 승인** — 위 1~6 확인 결과를 `release()`의 `reason` 인자에 기록하고,
   `released_by`에 승인자 식별자를 명시한 뒤에만 호출한다.

## 4. 조회

- `kss.get_state()` — 현재 상태 문자열만 조회.
- `kss.get_current_record()` — 현재 상태의 전체 레코드(사유, 활성화 시각/주체, 만료 시각,
  incident_id 등) 조회.
- `kss.get_history()` — 전체 감사 이력(시간순) 조회.

## 5. 관련 문서

- 활성화가 필요한 사고 유형별 대응은 [INCIDENT_RESPONSE_RUNBOOK.md](./INCIDENT_RESPONSE_RUNBOOK.md) 참조.
- 현재 실측 상태는 [LIMITED_LIVE_REVIEW_CHECKLIST.md](./LIMITED_LIVE_REVIEW_CHECKLIST.md) 4절 참조.
