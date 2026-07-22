# CODEX_REVIEW

Review target: 제한적 실거래 검토 안전 사이클 독립 재검증

Commits: `337ba16`, `fd11fb0`, `c34fde1`, `b9c4019`, `fc325b3`, `b6f4924`

Branch: `orchestrator/20260722-235153-us-stock-trading`

Date: 2026-07-23

Overall verdict: **FAIL**

보고된 384개 테스트는 모두 통과했고 실거래 설정 및 운영 데이터 파일은 변경되지 않았다. 그러나 새 다단계 kill switch와 Slack health monitor가 실제 주문·알림 경로에 연결되지 않았다. 모듈 단위 테스트는 통과하지만 운영 주문 진입점은 기존 binary kill switch만 검사한다. 신규 HIGH Finding 2건이 남으므로 `READY_FOR_LIMITED_LIVE_REVIEW`가 아니라 `BLOCKED`로 판정한다.

## Previous findings verification

### [CODEX-010]

Status: **RESOLVED**

Evidence: `a7736d5`에서 필수 시장 데이터와 계산 결과의 NaN/Infinity를 차단했고 관련 회귀 테스트가 통과했다.

Remaining risk: 없음.

### [CODEX-011]

Status: **RESOLVED**

Evidence: `427958a`에서 provider timestamp·거래일·세션 freshness gate를 추가했고 stale/세션 경계 테스트가 통과했다.

Remaining risk: 실제 Yahoo E2E는 외부 호출 금지로 실행하지 않았다.

### [CODEX-012]

Status: **RESOLVED**

Evidence: `044df60`에서 거래일과 허용 세션을 pipeline 진입 전에 차단했다.

Remaining risk: 없음.

### [CODEX-013]

Status: **RESOLVED**

Evidence: `ac2b4b3`에서 watchlist persistence 실패가 호출자에게 명시적으로 전파되도록 수정됐다.

Remaining risk: 없음.

### [CODEX-014]

Status: **RESOLVED**

Evidence: `7ab8db7`에서 lifecycle과 timestamp 검증을 정렬하고 손상 상태를 fail-closed 처리했다.

Remaining risk: 없음.

### [CODEX-015]

Status: **RESOLVED**

Evidence: `4f1f89d`에서 평균거래량 창과 04:00~09:30 ET premarket 경계를 수정했다.

Remaining risk: 실제 provider 데이터의 timezone 형태는 외부 E2E에서 재확인이 필요하다.

## New findings

### [CODEX-016] HIGH — 다단계 kill switch가 실제 주문 경로를 차단하지 않음

Status: **UNRESOLVED**

Evidence:

- `paper_strategy_order.py`는 `kill_switch.is_trading_halted()`만 import하고 `submit_order()` 및 `main()`에서 이 binary gate만 검사한다.
- 저장소 전체 운영 코드에서 `kill_switch_state.is_entry_allowed()`와 `is_liquidation_allowed()` 호출은 없다. 해당 함수는 테스트와 re-export에만 존재한다.
- 임시 상태 파일을 `ENTRY_DISABLED`로 활성화하고 binary halt를 해제한 격리 재현에서 `paper_strategy_order.submit_order()`가 broker를 **1회 호출**하고 HTTP 200 결과를 반환했다.
- `ALL_TRADING_DISABLED`와 `MANUAL_REVIEW` 역시 주문 종류를 구분해 적용되는 실제 호출부가 없다.
- 신규 테스트는 상태 함수의 반환값만 검증하며 실제 주문 진입점이 각 상태를 준수하는지 검증하지 않는다.

Remaining risk: 운영자가 `ENTRY_DISABLED`, `ALL_TRADING_DISABLED`, `MANUAL_REVIEW`를 설정해도 신규 주문이 계속 제출될 수 있다. entry/exit identity를 실제 주문 진입점까지 전달하고, 모든 direct/alternate submit 경로에서 상태별 정책을 fail-closed로 검사해야 한다.

### [CODEX-017] HIGH — Slack health monitor가 운영 알림 경로에 연결되지 않음

Status: **UNRESOLVED**

Evidence:

- `paper_strategy_order._safe_send_slack_alert()`는 여전히 `send_slack_alert()`를 직접 호출한다.
- 운영 코드에서 `notification_health.send_with_health_tracking()`, `record_success()`, `record_failure()` 호출은 없다.
- 테스트는 `pso.send_slack_alert`를 health wrapper lambda로 monkeypatch해 통합을 인위적으로 만든다. 실제 production wiring을 검증하지 않는다.
- 운영 wrapper가 `False`를 반환하도록 한 격리 재현 후 notification status는 `UNKNOWN`이었고 state 파일도 생성되지 않았다.
- 따라서 Slack 연속 실패가 기록되지 않으며 `ENTRY_DISABLED` 자동 상승도 발생하지 않는다. 설령 상태가 상승해도 CODEX-016 때문에 실제 주문을 차단하지 못한다.

Remaining risk: Slack 채널이 장기간 실패해도 시스템이 이를 탐지하거나 신규 진입을 중단하지 않는다. 실제 모든 알림 경로를 health wrapper에 연결하고 production-call-path 회귀 테스트가 필요하다.

### [CODEX-018] MEDIUM — 주문 직전 환경 재검증 함수가 선언만 되고 사용되지 않음

Status: **PARTIALLY_RESOLVED**

Evidence:

- `BrokerConfig.from_env()`와 dataclass `default_factory`는 import-time 고정 문제를 해결한다.
- 그러나 `validate_order_allowed_now()`는 `broker_config.py`와 테스트에서만 참조되며 `AlpacaBroker._request()`, `get_order_by_client_order_id()`, `submit_order()`는 생성 당시의 `self.config`만 검증한다.
- 함수 docstring의 “immediately before order submission” 보장은 실제 배선과 일치하지 않는다.

Remaining risk: 이미 생성된 장수명 broker 객체는 프로세스 내부에서 환경이 변경돼도 즉시 재검증하지 않는다. 실거래 활성화 전에 의도한 runtime policy를 실제 broker 진입점에 적용할지 결정해야 한다.

### [CODEX-019] MEDIUM — 신규 상태 저장소의 동시 갱신 lost-update 가능성

Status: **UNRESOLVED**

Evidence:

- `kill_switch_state.activate()/release()`와 `notification_health.record_success()/record_failure()`는 read-modify-write 전체에 파일 잠금을 사용하지 않는다.
- 각 write는 temp+`os.replace`로 단일 파일 원자성은 확보하지만, 두 프로세스가 동시에 읽고 쓰면 audit history 또는 consecutive failure 증가가 유실될 수 있다.
- concurrency 회귀 테스트가 없다.

Remaining risk: 동시에 발생한 운영자 전환과 자동 escalation 또는 병렬 Slack 실패가 마지막 writer 값으로 덮일 수 있다. lock 안에서 최신 파일을 다시 읽고 병합하는 multiprocessing 테스트가 필요하다.

## Executed tests

- 집중 테스트(`test_broker_config_env.py`, broker/kill-switch/notification 관련) → **78 passed, 1 warning**
- 전체 `venv/bin/python -m pytest -q` → **384 passed, 0 failed, 2 warnings**
- 격리 통합 재현:
  - `ENTRY_DISABLED` 상태에서 `paper_strategy_order.submit_order()` → broker 호출 **1회**
  - 운영 Slack wrapper 실패 → health status `UNKNOWN`, state 파일 미생성

테스트 수 주장은 재현됐지만 신규 테스트는 production wiring 누락을 검출하지 못한다.

## Warnings review

- urllib3 `NotOpenSSLWarning`: 로컬 LibreSSL 환경 경고이며 이번 Finding 원인이 아니다.
- scanner unknown-field `RuntimeWarning`: 기존 의도된 테스트 경고다.

안전 관련 신규 warning은 없다.

## Network safety

- 실제 Alpaca, Slack, Yahoo 호출은 수행하지 않았다.
- 모든 직접 재현은 fake broker, monkeypatch, 임시 경로를 사용했다.

## Operational file safety

- `order_history.csv` SHA-256: `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` (불변)
- `universe.csv` SHA-256: `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`
- `strategy_performance.csv` SHA-256: `ca012439cb2ba6a8f285b3f95493f9b17d22abb5b01a924ef2bd4cfe96f66da8`
- 저장소 루트에 kill-switch/notification state, log 또는 lock 파일이 생성되지 않았다.
- `.env`, 실거래 flag, `broker/alpaca_client.py`, `broker/__init__.py`, `order_safety.py`, `config/scanner_presets.json`은 이번 커밋 범위에서 변경되지 않았다.

## Document consistency

- 384 passed, 2 warnings 및 운영 파일 무변경 주장은 실제 결과와 일치한다.
- `BrokerConfig.from_env()` 구현 주장은 사실이나 주문 직전 재검증 배선 주장은 미완료다.
- Kill Switch 4단계 정책과 Slack 자동 escalation은 모듈 내부에서만 구현됐고 실제 주문·알림 경로에는 적용되지 않았다.
- `LIMITED_LIVE_REVIEW_CHECKLIST.md`와 `LIVE_APPROVAL_RECORD.md`의 `READY_FOR_LIMITED_LIVE_REVIEW` 판정은 신규 HIGH Finding과 양립하지 않는다.
- `approved: false`, `live_enabled: false`는 정확하며 유지해야 한다.

## Unverified areas

- 실제 Alpaca Paper/Live 계정, Slack webhook, Yahoo provider E2E
- 다중 프로세스 kill-switch/notification 상태 경쟁
- 실제 포지션과 open order reconciliation
- 운영자 승인, 주문당 절대 금액, symbol allow-list, 허용 거래 시간
- 자동 청산 주문 identity와 상태별 차단 정책

## Limited live review decision

**BLOCKED**

모듈 단위 안전 기능이 실제 주문 경로에 적용되지 않은 HIGH Finding이 2건 남아 있다. 문서의 `READY_FOR_LIMITED_LIVE_REVIEW` 상태로 승인할 수 없다.

## Live trading recommendation

**DO_NOT_ENABLE**

`approved: false`, `live_enabled: false`를 유지한다. CODEX-016과 CODEX-017을 production-call-path 테스트로 해결하고 CODEX-018~019를 재검증한 뒤에만 제한적 실거래 사람 검토를 다시 시작할 수 있다.
