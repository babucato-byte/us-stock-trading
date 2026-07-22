# Paper Trading Readiness Report

작성일: 2026-07-22
대상 브랜치: `orchestrator/20260722-021713-us-stock-trading`
대상 커밋 범위: `a78390b` (t0, baseline) ~ `3511034` (t7)

## (a) 전체 회귀 테스트 실행 결과

실행 명령:

```
venv/bin/python -m pytest -q
```

실측 출력(요약):

```
........................................................................ [ 21%]
........................................................................ [ 42%]
........................................................................ [ 64%]
........................................................................ [ 85%]
................................................                         [100%]
336 passed, 2 warnings in 73.28s (0:01:13)
```

- exit code: **0**
- collected: **336** (`venv/bin/python -m pytest -q --collect-only` → `336 tests collected`, skip/xfail 없음 — collected 수와 passed 수 일치)
- passed: **336**
- failed: **0**
- warnings: 2건, 모두 기존에 알려진 무해한 경고
  - `urllib3` `NotOpenSSLWarning` (macOS 시스템 LibreSSL 관련, 코드 문제 아님)
  - `tests/test_scanner.py::test_unknown_field_skips_with_warning`가 의도적으로 유발하는 `RuntimeWarning`(스캐너의 미지원 필드 스킵 경고 테스트)

## (b) 이번 run(t0~t7)에서 추가/확장된 테스트 파일

| 파일 | 신규/확장 | collect-only 테스트 개수 | 추가된 커밋 |
|---|---|---|---|
| `tests/test_restart_duplicate_order.py` | 신규 파일 | 9 | `291e2fa` (t1) |
| `tests/test_account_exposure_limits.py` | 신규 파일 | 21 | `bf31850` (t2) |
| `tests/test_kill_switch.py` | 신규 파일 | 11 | `396eeec` (t3) |
| `tests/test_api_failure_isolation.py` | 신규 파일 | 6 | `177bd81` (t4) |
| `tests/test_crash_recovery.py` | 신규 파일 | 8 | `5afe500` (t5) |
| `tests/test_broker_safety.py` | 기존 파일 확장(신규 파일 아님) | 19 (전체, 이번 run에서 추가된 케이스 포함) | `ff3d0e4` (t6) |
| `tests/test_order_event_notifications.py` | 신규 파일 | 5 | `3511034` (t7) |

각 개수는 `venv/bin/python -m pytest -q --collect-only <파일>` 실행 결과의 "N tests collected" 값을 그대로 옮긴 것이다. `test_broker_safety.py`는 t6 커밋 이전부터 존재하던 파일이며, 이번 run에서는 코드(`broker/**`) 수정 없이 테스트 케이스만 추가되었다(커밋 메시지: "코드 수정 금지").

## (c) t1~t7 항목별 상태

| 항목 | 심각도 | 상태 | 비고 |
|---|---|---|---|
| t1: 재시작 안전 중복 주문 방지 (reserve→commit 2단계) | CRITICAL | **신규 구현** | `order_intent_ledger.py` 신규 도입, `paper_strategy_order.py`에 배선. 기존 `is_duplicate_order()`가 `SUBMISSION_FAILED` 상태를 걸러내지 않아 abort 후 재시도가 실제 주문 경로까지 도달하지 못하던 gap을 발견해 수정(아래 (d) 참조). |
| t2: 계좌 전체 포지션 수·총 익스포저 상한 | CRITICAL | **신규 구현** | `account_risk.py`, `risk_config.py`, `paper_strategy_order.py` 수정. |
| t3: kill switch (파일/환경변수 기반 신규 주문 차단) | HIGH | **신규 구현** | `kill_switch.py` 신규. Fail-open 설계 — `TRADING_HALTED` 환경변수 또는 `KILL_SWITCH` 센티널 파일이 없으면 기존 동작 그대로 유지. |
| t4: API timeout·부분 응답 시 심볼 단위 격리 실패 처리 | HIGH | **신규 구현** | `paper_strategy_order.py`의 `main()` 루프가 심볼별 결과를 `submitted/failed/blocked/skipped`로 분리 집계하도록 수정, Slack 알림 실패가 나머지 심볼 처리를 막지 않도록 `_safe_send_slack_alert()` 도입. |
| t5: 비정상 종료 잔여 상태 복구 (stale 잠금·임시파일 fail-closed) | HIGH | **신규 구현** | `scalping_watchlist/atomic_io.py` 수정. |
| t6: paper/live 계정 설정 분리 테스트 커버리지 보강 | MEDIUM | **이미 충족됨 + HUMAN_REVIEW** | 코드(`broker/**`) 수정 금지 조건 하에 테스트만 추가. 대부분 경로(누락 환경변수 fallback, 오타 값 fail-closed, 대소문자/공백 정규화, live 모드 안전 플래그 누락 시 dry-run 강제, paper base URL이 live host로 잘못 설정된 경우 차단)는 안전함을 확인. 단, 프로세스 재시작 없이 실행 중 환경변수를 바꿔도 `BrokerConfig`가 반영하지 못하는 이슈를 발견 — 아래 (d) 참조. |
| t7: 주문 제출·체결·거절·예외 이벤트 알림/로그 연결 | MEDIUM | **신규 구현** | `_notify_order_filled()`, `_notify_order_rejected()` 추가, FILLED/PARTIALLY_FILLED/REJECTED 각 이벤트에 대해 Slack 알림 연결. |

## (d) 남아있는 리스크 및 실거래 전 필요한 사람 검토 항목

1. **[HUMAN_REVIEW] `BrokerConfig` 환경변수가 최초 import 시점에 고정되고 재실행 없이 갱신되지 않음.**
   `docs/autonomous/HUMAN_REVIEW_FINDINGS.md` (t1 커밋에서 함께 추가)에 상세 기록됨. `trading_mode`, `enable_real_trading`, `live_dry_run`, `paper_base_url`, `live_base_url`, `api_key`, `secret_key`가 모듈 최초 import 시 한 번만 평가되는 dataclass 기본값이라, 오래 떠 있는 프로세스(`dashboard/app.py`)에서 실행 중 `TRADING_MODE`를 바꿔도 즉시 반영되지 않는다. `kill_switch.is_trading_halted()`는 매 호출마다 환경/파일을 다시 읽어 즉시 반영되는 것과 대비된다. 이번 run은 `broker/**` 수정이 범위 밖이라 테스트로 현재(미수정) 동작만 고정했으며, 코드 수정 여부는 사람 검토가 필요하다.
2. **[HUMAN_REVIEW] 실거래(`ENABLE_REAL_TRADING`/`TRADING_MODE=live`) 전환은 이번 run의 범위에 포함되지 않음.** t1~t7은 모두 paper 계정 기준 안전장치이며, 실거래 전환 시 위 1번 이슈를 포함해 `broker/broker_config.py`, `.env` 구성을 별도로 사람이 재검토해야 한다.
3. **kill switch 운영 절차 미확정.** `kill_switch.py`는 `KILL_SWITCH` 센티널 파일 또는 `TRADING_HALTED` 환경변수로 동작하지만, 이번 run에서는 실제 운영(누가 언제 어떤 방식으로 생성/해제하는지, 알림 연동 등) 절차는 정의하지 않았다. 실거래 투입 전 운영 런북 필요.
4. **Slack 알림 실패에 대한 관측성 확인 필요.** t4/t7에서 `_safe_send_slack_alert()`로 Slack 전송 실패를 흡수하도록 만들었으나(주문 처리 자체가 막히지 않도록), 이는 동시에 "알림이 조용히 유실될 수 있다"는 의미이기도 하다. 알림 채널 자체의 헬스체크는 이번 range 밖이며, 실거래 전 별도 모니터링(예: Slack 실패율 로그 확인 절차)이 필요할 수 있다.
5. **t6에서 확인된 것은 "회귀 없음"이지 "완전 검증됨"이 아님.** 테스트 커버리지 보강은 현재 동작을 고정(pin)한 것이며, 위 1번 이슈에 대한 수정 여부 결정은 별도 사람 승인이 필요하다.

## (e) 실거래 비활성 상태 확인

- `LIVE_TRADING_ENABLED` (및 관련 `.env`/`broker/**` 설정)는 이번 run에서 **변경되지 않았다.** `git diff main HEAD -- .env .env.* broker/ order_safety.py config/scanner_presets.json`는 빈 결과.
- 저장소 루트에 `KILL_SWITCH` 파일은 **존재하지 않는다** (`ls KILL_SWITCH` → No such file or directory).
- `TRADING_HALTED`, `LIVE_TRADING_ENABLED`, `KILL_SWITCH_FILE` 환경변수는 현재 셸 환경에 설정되어 있지 않다.
- `git status`는 clean이며, 이번 run은 위 (b)의 신규/확장 테스트 파일과 그에 대응하는 프로덕션 코드(`order_intent_ledger.py`, `paper_strategy_order.py`, `account_risk.py`, `risk_config.py`, `kill_switch.py`, `scalping_watchlist/atomic_io.py`) 외에는 수정하지 않았다. 기존 테스트의 삭제·완화·skip 처리는 없다(336 collected == 336 passed).
- 결론: 본 run은 **paper trading 안전장치 강화 및 회귀 테스트 검증**만 수행했으며, 실거래는 여전히 비활성 상태다. 실거래 전환 여부는 (d)의 HUMAN_REVIEW 항목 해소 후 별도로 결정해야 한다.
