# 30,000 KRW 제한 실거래 준비 (Stage 10)

이 문서는 사용자 지시서 Stage 10의 산출물이다. **실거래를 활성화하지 않으며, 이 문서 작성 자체가
실거래 승인을 의미하지 않는다.** `approved: false`, `live_enabled: false`
([LIVE_APPROVAL_RECORD.md](./LIVE_APPROVAL_RECORD.md))는 변경하지 않았다. 이 문서 작성 과정에서
코드(`broker/**`, `order_safety.py`, `risk_config.py`)와 환경변수(`.env`)는 전혀 변경하지 않았다 —
신규 순수 계산 모듈(`live_readiness/sizing.py`, `live_readiness/allowlist.py`)을 추가했다.

**갱신(2026-07-26, CODEX-026)**: 이 모듈들은 최초 작성 시점(Stage 10)에는 실제 주문 제출 경로에
배선되지 않았으나, Codex 독립 검증(CODEX-026 HIGH)에서 이 배선 부재 자체가 결함으로 지적되어
이번 사이클에 신규 `live_readiness/order_gateway.py::validate_and_size_live_entry()`가
`paper_strategy_order.submit_order()`의 **`side="buy" AND broker.config.is_live_mode`인 경우에
한해** 실제로 강제되도록 배선됐다(커밋 `f482e90`). Paper 거래와 모든 청산 주문은 이 게이트의
영향을 받지 않는다 — 근거는 `docs/autonomous/DECISION_LOG.md`의 CODEX-023~027 섹션 결정 3.
**추가 갱신(2026-07-26, CODEX-029)**: `paper_strategy_order.submit_order()`를 우회하는 direct
broker 호출이 이 게이트의 보호를 받지 못한다는 위 잔여 위험은 해소됐다 — `broker/
alpaca_client.py::AlpacaBroker.submit_order()` 자체가 동일한 게이트(allow-list/예산/FX/symbol
동일성)를 실행하도록 배선되어(커밋 `b78e444`), `AlpacaBroker` 인스턴스에 대한 direct 호출도 더
이상 우회할 수 없다. 같은 사이클에서 `LiveEntryContext.symbol`과 실제 제출 symbol이 반드시
일치해야 한다는 검사도 추가됐다(CODEX-029) — 승인된 context로 다른 symbol을 제출하는 경로를
차단한다. 남은 잔여 범위는 `docs/autonomous/DECISION_LOG.md`의 CODEX-024/026/028/029/030
섹션 결정 4 참고(동일 클래스의 향후 신규 메서드는 이 게이트를 자동으로 상속받지 않음).

**추가 갱신(2026-07-26, CODEX-031)**: 위 게이트가 강제하던 30,000원/일일 진입/동시 포지션 한도는
그동안 여전히 `LiveEntryContext`의 caller 입력값이었다 — caller가 300만원짜리 context를 만들면
그대로 승인되는 실제 재현 가능한 결함이었다. 신규 `live_readiness/entry_reservation_ledger.py`
(SQLite)가 모든 live 진입 시도를 broker 호출 전 durable하게 예약하고, 게이트는 이제 caller
입력이 아니라 이 ledger에서 산출한 실제 사용량을 근거로 판단한다. `PILOT_TOTAL_BUDGET_KRW=30_000`
/`MAX_CONCURRENT_LIVE_POSITIONS=1`/`MAX_DAILY_LIVE_ENTRIES=2`는 코드 상수로 고정되어 caller가
완화할 수 없다(커밋 `8a3be50`). 30,000원 총 예산은 파일럿 전체에 걸친 누적 배분으로 취급되어
포지션이 종료돼도 반환되지 않는다 — 아래 §7 "일일 주문 1~2건"/"30,000원 총 예산" 항목의 실제
숫자값(운영자 미기입) 자체는 여전히 TBD_OPERATOR이지만, 이제 그 값이 실제로 강제된다는 점은
코드로 보장된다.

## 1. 마이크로 주문 수량 계산

`live_readiness/sizing.py::calculate_micro_order_quantity(available_krw, fx_rate_krw_per_usd,
share_price_usd, *, min_order_amount_usd=1.0, fractional_shares_allowed=False)`.

- 정수 주식 단위만 지원(`fractional_shares_allowed=False`가 기본값) —
  `PROJECT_CONSTITUTION.md` v1.0 범위는 Alpaca 소수점 주식 거래를 활성화한 적이 없고, 이 단계도
  그 범위를 바꾸지 않는다. 소수점 거래 활성화는 별도의 명시적 향후 결정 사항.
- **소수점 주식 확인**: `fractional_shares_allowed=True`로 명시적으로 호출하지 않는 한 항상 내림
  정수 수량만 반환.
- **최소 주문 금액 확인**: 계산된 수량의 총 금액이 `min_order_amount_usd` 미만이면
  `BELOW_MINIMUM_ORDER_AMOUNT` 상태로 수량 0을 반환(실제 존재하지 않는 소액 주문을 지어내지 않음).
- 예산이 1주도 살 수 없으면 `INSUFFICIENT_FUNDS` 상태로 수량 0.
- 모든 입력(KRW 예산/환율/주가)이 양수가 아니면 예외(`InvalidSizingInputError`) — 조용히 0을
  반환하지 않음.
- `DEFAULT_MIN_ORDER_AMOUNT_USD = 1.0`은 **ASSUMPTION**(실제 Alpaca 최소 주문 금액 미확인) — 아래
  §7 TBD_OPERATOR 목록 참고.
- 신규 테스트: `tests/test_live_readiness.py` 12건 중 7건(정상 계산/자금 부족/최소금액 미달/
  소수점 비활성 기본값/소수점 활성화/잘못된 입력 예외/예산 USD 항상 보고).

## 2. 종목 허용목록(Symbol Allow-list)

`live_readiness/allowlist.py::is_symbol_allowed(symbol, allow_list)` — **fail-closed**: 빈
allow-list(`[]`/`None`)는 아무 종목도 허용하지 않는다(빈 차단목록과 반대 방향). 대소문자·공백
정규화. 실제 파일럿 허용 종목 목록 자체는 `TBD_OPERATOR`(§7) — 코드는 정책을 강제할 뿐 실제 목록
내용은 운영자가 채워야 한다.

`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md` 항목 #4가 이미 "코드에 이 하드캡을 강제하는
로직이 아직 없다"고 지적한 갭을 이번 단계에서 계산 함수로 채웠다. **2026-07-26 갱신**: 이 함수는
이제 `live_readiness/order_gateway.py`를 통해 live 진입 경계에 실제로 배선되어 있다(CODEX-026,
커밋 `f482e90`) — 남은 것은 실제 파일럿 종목 목록의 기입뿐이다.

## 3. 일일/포지션 한도 (기존 정책과의 관계)

| 정책 | 값 | 이미 코드로 강제됨? | 근거 |
|---|---|---|---|
| 일일 주문 1~2건 | **2건**(ASSUMPTION, 파일럿 초기 최댓값) | 부분적 — `order_safety.MAX_TRADES_PER_DAY`가 이미 일일 거래 횟수를 제한하지만 현재 기본값은 이 파일럿 규모보다 큼. 파일럿 시작 전 `MAX_TRADES_PER_DAY=2`로 하향 조정 필요(운영자 결정) |
| 동시 1포지션 | **1** | 예 — 아키텍처 자체가 전략 1개(`ORDER_GENERATING_STATUSES={ACTIVE}`, 동시 활성 전략 1개)+포지션 생명주기가 1개 포지션 흐름을 전제로 설계됨(`positions/lifecycle.py`). `order_safety.MAX_OPEN_POSITIONS`도 이미 존재 — 파일럿 값(1)으로 하향 조정 필요 |
| 오버나이트 금지 | 예 | 예 — `positions/lifecycle.py`의 EOD 강제 청산(`EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE`)이 이미 이를 구조적으로 강제 |
| 첫 오류 시 `ENTRY_DISABLED` | 정책만 정의, 코드 미배선 | 아니오 — §6 참고 |

## 4. 첫 오류 시 ENTRY_DISABLED — 운영 절차 (코드 미배선, 의도적 결정)

`kill_switch_state.py`는 이미 `ENTRY_DISABLED` 상태를 지원한다(신규 진입 주문만 차단, 청산은
허용 — `is_entry_allowed()`/`is_liquidation_allowed()`). Stage 10에서는 이 상태로의 **자동 전이를
`paper_strategy_order.py`/`positions/lifecycle.py`의 실제 주문 제출 경로에 배선하지 않기로
결정했다.** 이유:

- 그 경로는 CODEX-016~022 원격 수정 사이클과 Codex `PASS_WITH_CONDITIONS` 최종 판정을 거친
  안전 크리티컬 네트워크 경계다. 이번 Stage 3~10 연속 구현은 사용자 지시에 따라 Codex 중간 검증
  없이 진행 중이므로, 이 시점에 그 경로 자체를 다시 수정하면 이미 검증된 안전장치를 재검증 없이
  건드리는 것과 같다.
- 대신 **운영 절차**로 문서화한다: 파일럿 주문이 실패(브로커 거부, 타임아웃, 예상 밖 응답)하면
  운영자가 즉시 `kill_switch_state.activate("ENTRY_DISABLED", reason=..., activated_by=...)`를
  수동 실행한다(`KILL_SWITCH_RUNBOOK.md` 참고). 이 자동화 배선 여부는 실제 제한적 실거래 검토
  시점에 Codex 검증을 포함해 별도로 결정한다(`NEEDS_USER_DECISION`으로 아래 §7에 기록).

## 5. 롤백 계획

파일럿 규모(30,000 KRW)에 특화된 추가 사항 — 기존 [ROLLBACK_PLAN.md](./ROLLBACK_PLAN.md)의 코드/
브랜치 롤백 절차에 다음을 더한다:

1. **포지션 청산 우선**: 코드를 롤백하기 전에 반드시 브로커 계정에 미청산 포지션이 없는지 확인
   (`ops_dashboard.cli`의 Positions 섹션 또는 `paper_strategy_order.reconcile_pending_orders()`).
   포지션이 있으면 먼저 `kill_switch_state.activate("ALL_TRADING_DISABLED", ...)`로 전체 거래를
   막고 수동 청산 후 롤백을 진행한다.
2. **자본 회수 확인**: 파일럿 자본(30,000 KRW 상당)이 브로커 계좌에 실제로 남아있는지, 손실이
   예상 범위(§7의 실제 주문 금액 한도) 내인지 확인.
3. **코드/브랜치 롤백**: `ROLLBACK_PLAN.md`의 절차를 그대로 따른다(현재까지 `main`은 전혀
   변경되지 않았으므로, orchestrator 브랜치를 폐기하는 것만으로 충분).
4. **`LIVE_APPROVAL_RECORD.md` 갱신**: `approved: false`로 되돌리고(또는 이미 false였다면 유지),
   롤백 사유와 시각을 기록.

## 6. 일일 운영 플레이북

1. **개장 전(프리마켓)**: Kill Switch 상태 확인(`ops_dashboard.cli` 또는
   `kill_switch_state.get_current_record()`) — `ACTIVE`가 아니면 진행하지 않는다.
2. **개장 전**: `notification_health.get_status()` 확인, Slack 알림 채널 정상 여부 확인(정상이
   아니어도 대시보드는 로컬에서 계속 확인 가능 — Stage 9).
3. **정규장 중**: `ops_dashboard.cli`로 활성 전략/시장상태/관심종목/포지션/일일 주문 수를 주기적
   확인. 일일 주문 수가 §3의 한도(2건)에 도달하면 그날은 추가 진입을 보류(운영자 확인 또는
   `order_safety.MAX_TRADES_PER_DAY` 설정값에 위임).
4. **주문 실패 발생 시**: §4의 절차대로 `ENTRY_DISABLED` 수동 전환, `INCIDENT_RESPONSE_RUNBOOK.md`
   착수.
5. **장 마감 전**: 포지션 생명주기의 EOD 강제 청산이 정상 작동했는지 확인(오버나이트 포지션이
   없어야 함 — 이미 코드로 강제되지만 파일럿 초기에는 수동 재확인 권장).
6. **장 마감 후**: 당일 realized PnL, 체결/거부/타임아웃 건수, kill switch 이벤트 로그를
   `docs/live_review/`에 별도 일지로 기록(양식은 운영자 재량, 이 문서는 강제하지 않음).

## 7. TBD_OPERATOR — 코드가 대신 채울 수 없는 항목

아래 항목은 실제 값이 존재하기 전까지 이 문서와 `TBD_REVIEW_RECOMMENDATIONS.md`에 `TBD_OPERATOR`로
남는다. 이 문서 작성 과정에서 어떤 항목도 추정하여 확정하지 않았다.

| 항목 | 상태 |
|---|---|
| 실제 연결 계좌(Paper/Live 어느 계정) | `TBD_OPERATOR` |
| 실제 KRW→USD 환율(파일럿 실행 시점 실측) | `TBD_OPERATOR`(이 문서/테스트의 1,350은 예시값일 뿐 실제 환율 아님) |
| Live API Key 발급 여부 및 위치 | `TBD_OPERATOR` |
| 실제 주문 금액 한도(주문당 절대 USD/KRW 상한) | `TBD_OPERATOR`(`TBD_REVIEW_RECOMMENDATIONS.md` #3 참고, $500~$1,000 초안 존재하나 미확정) |
| 실제 승인자 | `TBD_OPERATOR`(`LIVE_APPROVAL_RECORD.md`) |
| 배포 시각 | `TBD_OPERATOR` |
| 롤백 담당자 | `TBD_OPERATOR`(`ROLLBACK_PLAN.md`/`LIVE_APPROVAL_RECORD.md`) |
| Alpaca 실제 최소 주문 금액(`DEFAULT_MIN_ORDER_AMOUNT_USD=1.0`이 실제 값과 일치하는지) | `TBD_OPERATOR` |
| 실제 파일럿 종목 allow-list 내용 | `TBD_OPERATOR`(§2) |
| §4의 `ENTRY_DISABLED` 자동 배선 여부 | `NEEDS_USER_DECISION`(코드 변경 필요, Codex 검증 권장) |

## 8. 최종 체크리스트 (Stage 10 완료 기준)

- [x] 마이크로 주문 수량 계산 구현 및 테스트(소수점 확인, 최소 주문 금액 확인 포함)
- [x] 종목 allow-list fail-closed 검사 구현 및 테스트
- [x] 일일 1~2건, 동시 1포지션, 오버나이트 금지 정책을 기존 코드 강제 여부와 함께 문서화
- [x] 첫 오류 시 `ENTRY_DISABLED` — 자동 배선하지 않기로 한 결정과 근거, 운영 절차 대안 기록
- [x] 롤백 계획(파일럿 특화 추가 사항)
- [x] 일일 운영 플레이북
- [x] 사고 대응은 기존 `INCIDENT_RESPONSE_RUNBOOK.md`/`KILL_SWITCH_RUNBOOK.md` 재사용 확인(중복
      작성하지 않음)
- [x] TBD_OPERATOR 목록 전부 나열, 어떤 항목도 추정으로 확정하지 않음
- [x] `tests/test_live_readiness.py` 12건 + 전체 회귀 통과
- [x] `approved`/`live_enabled` 미변경, 코드의 실제 주문 경로 미변경 확인

**상태: 문서화 및 계산 모듈 완료. 실거래 준비 완료가 아님 — §7의 모든 `TBD_OPERATOR` 항목이
운영자에 의해 채워지고, §4의 `NEEDS_USER_DECISION`이 해결되고, Codex 최종 통합 검증
(`docs/autonomous/FINAL_VALIDATION_PACKAGE.md`)이 완료되기 전까지 Live trading은 계속
`DO_NOT_ENABLE`.**
