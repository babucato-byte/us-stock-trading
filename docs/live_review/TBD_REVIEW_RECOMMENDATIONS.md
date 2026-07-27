# TBD Review Recommendations

`docs/live_review/LIMITED_LIVE_REVIEW_CHECKLIST.md`에 남아 있는 `TBD(운영자 기입)` 항목을 전부
추출하고, 각 항목에 대한 권장값 초안·근거·위험·승인 필요 여부를 정리한 운영자 검토용 문서다.

**이 문서는 어떤 값도 확정하지 않는다.** 아래 "권장값"은 코드/문서/설정에서 근거를 찾을 수 있거나
이 프로젝트의 명시된 초기 범위(Paper 전용, 단일 전략, v1.0)에서 합리적으로 도출한 **초안**일 뿐이며,
실제 체크리스트·`LIVE_APPROVAL_RECORD.md`에 반영하려면 운영자가 검토 후 직접 기입해야 한다. 이
문서 작성 과정에서 코드, 환경변수, 브랜치, 승인 플래그(`approved`, `live_enabled`)는 전혀 변경하지
않았다.

**상태: `DO_NOT_ENABLE`** — 아래 10개 항목이 전부 운영자에 의해 확정되기 전까지 유지한다. 이 값들이
채워지는 것과 실거래 활성화 여부(운영자 승인)는 별개다 — 항목이 채워졌다고 자동으로
`READY_FOR_LIMITED_LIVE_REVIEW`나 `LIVE_READY`/`LIVE_APPROVED`로 승격되지 않는다.

## TBD 항목 목록 및 권장값

| # | 위치 | 항목 | 권장값(초안) | 근거 | 위험 | 운영자 승인 필요? |
|---|---|---|---|---|---|---|
| 1 | §0 문서 메타 정보 | 검토 일시(실제 사람 검토 수행 시각) | *(값 없음 — 사람이 실제로 검토를 수행한 시각을 그때 기입)* | 이 항목은 코드나 설정에서 도출할 수 없는, 검토 행위 자체의 기록이다. 미리 채우면 검토가 실제로 일어났다는 거짓 증거가 된다. | **높음** — 값을 미리 채우면 "사람이 검토했다"는 사실 자체가 조작된다. 이 프로젝트의 fail-closed 원칙과 정면으로 배치. | **예** — 운영자 본인이 검토를 마친 시점에 직접 기입해야 하며, 대신 채워질 수 없음 |
| 2 | §2 Broker 설정 | 실제 연결된 Alpaca 계정(paper/live 실키가 어느 계정인지) | **Paper 계정만 연결**(Live 키는 아예 발급/설정하지 않거나, 발급하더라도 이번 v1.0 범위에서는 `.env`에 넣지 않음) | `PROJECT_CONSTITUTION.md`: "초기 v1.0의 거래 계정은 Alpaca **Paper Trading 전용**"; `risk_config.py`의 `TRADING_MODE="paper"`, `ENABLE_REAL_TRADING=False`, `LIVE_DRY_RUN=True` 기본값과 일치 | **중간** — Live API 키를 어떤 형태로든 환경에 존재시키는 것 자체가 향후 오설정(`TRADING_MODE` 오타, `.env` 실수 등) 시 실주문으로 이어질 수 있는 잠재 위험. `docs/autonomous/HUMAN_REVIEW_FINDINGS.md`에 기록된 `BrokerConfig` import-time 문제도 이와 연관 | **예** — 운영자가 실제로 어떤 API 키를 어느 환경에 설정했는지는 본인만 확인 가능. 코드는 "Paper 전용" 정책을 강제할 뿐, 실제 키 발급/배치 여부는 알 수 없음 |
| 3 | §3 리스크 한도 | 주문당 최대 금액(절대 금액, USD) | **$500 ~ $1,000** (제한적 파일럿 규모 제안) | 현재 `risk_config.py`에는 비중 기반 한도(`MAX_POSITION_RATE=0.10`)만 있고 절대 금액 상한이 없음. "제한적 소액 실거래"라는 이번 사이클의 목표(사용자 지시서 반복 언급)에 맞춰, 계좌 자본과 무관하게 단일 주문이 넘지 못할 절대 상한을 두는 것이 안전. 예: 계좌가 예상보다 커도 1건 주문이 과도해지지 않도록 하는 하드캡 | **중간(2026-07-26 하향 조정)** — CODEX-026 수정으로 `live_readiness/order_gateway.py`가 이제 `max_order_notional_krw` 절대 상한을 live 진입 경계에서 실제로 강제한다(`paper_strategy_order.submit_order()`에 배선, 커밋 `f482e90`). 즉 "코드에 하드캡 로직이 없다"는 이전 위험은 해소됨. **추가 갱신(2026-07-26, CODEX-031, 커밋 `8a3be50`)**: 이 하드캡 자체도 더 이상 caller가 완화할 수 없다 — `live_readiness/entry_reservation_ledger.py`가 durable하게 예산 사용량을 추적하고, `PILOT_TOTAL_BUDGET_KRW=30_000`이라는 신뢰 가능한 코드 상수가 `max_order_notional_krw` caller 값과 `min()`으로 교차해 실제 상한이 된다. **추가 갱신(2026-07-27, 잔고 비율 사이징)**: `PILOT_TOTAL_BUDGET_KRW=30_000` 고정 상수는 사용자 지시에 따라 완전히 제거됐다 — 이제 상한은 `available_cash_krw × cash_usage_percent/100`(caller가 매 호출 조회한 실제 broker 잔고 기준, margin/leverage 미사용)이며 `cash_usage_percent`(1~100)만 caller가 완화할 수 없는 운영자 설정으로 남는다. **추가 갱신(2026-07-27, CODEX-036, 커밋 `40abc58`)**: `cash_usage_percent`를 caller가 완화할 수 없다는 서술이 이 시점까지도 실제로 강제되지 않고 있던 것을 Codex가 지적했다 — `live_readiness/account_cash.py::TRUSTED_CASH_USAGE_PERCENT_CEILING=50`이 신설되어 이제 실제로 100%를 요청해도 50%로 상한이 걸린다. `available_cash_krw`도 `AccountCashSnapshot`(broker.get_account() 기반)을 optional로 전달하면 `min()`으로 caller 선언값을 초과할 수 없다. **남은 위험은 `TRUSTED_CASH_USAGE_PERCENT_CEILING`의 실제 배포값(50%가 최종 승인값인지 운영자 확인 필요)과, `account_cash_snapshot` 전달 자체가 아직 opt-in이라 이를 실제로 채워 넣는 production 배선이 없다는 점뿐** **추가 갱신(2026-07-28, Stage 11)**: `TRUSTED_CASH_USAGE_PERCENT_CEILING`의 실제 소스가 `live_readiness/trusted_operator_config.py`로 이전됐다(값 자체는 50%로 불변, 단일 소스로 통합만 함) — `account_cash.py`/`order_gateway.py`는 이제 그 모듈을 통해 값을 읽는다. 남은 위험은 동일하게 유지된다. | **예** — 코드는 이제 임의의 값을 강제할 수 있지만, 실제 배분 가능한 자본 규모·리스크 감내 수준·`cash_usage_percent`/`TRUSTED_CASH_USAGE_PERCENT_CEILING` 값은 운영자만 알고 있어 운영자가 지정해야 함 |
| 4 | §3 리스크 한도 | 허용 종목 범위(심볼 allow-list) | **초기 파일럿은 고정된 소규모 allow-list(예: 상위 유동성 대형주 5~10종목)로 시작**, 이후 안정성 확인되면 기존 동적 스캐너(`scalping_watchlist`, `MIN_PRICE=5`/`MAX_PRICE=500`/`MIN_AVERAGE_DOLLAR_VOLUME=2000만`/`MIN_LIQUIDITY_SCORE=20` 기준)로 확대 | `risk_config.py`/`account_risk.py`에는 심볼 allow-list/블랙리스트가 없었음(원래 지적). **2026-07-26 CODEX-026 수정으로 `live_readiness/allowlist.py::is_symbol_allowed()`가 신설되어 `live_readiness/order_gateway.py`를 통해 live 진입 경계에서 실제로 강제된다**(빈 목록은 아무것도 허용하지 않는 fail-closed 기본값, 커밋 `f482e90`) — "실거래를 허용할 종목을 좁히는 장치가 없다"는 코드 갭 자체는 해소됨. **추가 갱신(2026-07-26, CODEX-029, 커밋 `b78e444`)**: allow-list 대조 대상(`ctx.symbol`)이 실제 제출 symbol과 반드시 일치하도록 강제 — 승인된 context로 다른 symbol을 제출하는 경로도 차단되며, `AlpacaBroker.submit_order()` 직접 호출도 동일하게 차단된다 | **낮음(2026-07-26 하향 조정)** — 코드 강제는 이제 존재하고 direct-call 우회도 닫힘. **남은 위험은 순수하게 allow-list의 실제 종목 구성**(운영자 미기입)뿐. **추가 갱신(2026-07-28, CODEX-040, 커밋 `ae2b0fd`)**: `paper_strategy_order.main()`의 실제 live 진입 경로가 이제 `LIVE_ENTRY_ALLOW_LIST` 환경변수(쉼표 구분 심볼 목록)를 읽어 이 allow-list를 실제로 배선한다 — 미설정 시 빈 목록(fail-closed)이 기본값이다. 실제 파일럿 종목 구성 자체는 여전히 운영자 미기입 | **예** — 코드는 이제 강제할 수 있지만, 어떤 종목을 파일럿 대상으로 신뢰할지는 여전히 운영 판단이며 규제/세금/유동성 이해도에 따라 운영자별로 다를 수 있음 |
| 5 | §3 리스크 한도 | 허용 거래 시간대 | **정규장 개장 후 60분(09:30–10:30 ET) + 프리마켓(04:00–09:30 ET)** — 현재 스캐너 게이트와 동일하게 유지 | `config/scalping_watchlist_config.py`: `ALLOWED_SESSIONS=("premarket","regular")`, `REGULAR_OPEN_WINDOW_MINUTES=60` — `scalping_watchlist/calendar_guard.py`가 이미 이 창 밖에서는 파이프라인 자체를 `SKIPPED` 처리함. 즉 이 값은 이미 코드에 존재하며, 체크리스트가 이를 아직 "운영자가 명시적으로 재확인/승인"하지 않았을 뿐 | **낮음** — 이미 코드로 강제되고 있어 새로 도입하는 위험은 아님. 다만 체크리스트에 명시적으로 옮겨 적어 "코드 설정"과 "운영자가 승인한 정책"이 같은 값임을 문서로 고정해두지 않으면, 향후 `SCALPING_REGULAR_OPEN_WINDOW_MINUTES` 환경변수가 조용히 바뀌어도 아무도 알아채지 못할 수 있음 | **권장하지만 필수는 아님** — 코드 기본값을 그대로 채택할지, 더 좁힐지(예: 정규장 첫 30분만)는 운영자 선택 |
| 6 | §6 계좌/주문 상태 | 상태 reconciliation 결과 (broker-로컬 대사) | *(값 없음 — 실거래 검토 시점에 `paper_strategy_order.reconcile_pending_orders()`를 실제로 실행한 결과를 기입)* | `paper_strategy_order.py:548`. 이 값은 코드가 지금 산출할 수 없다 — "검토 시점의" broker 계좌와 로컬 기록이 일치하는지는 그 순간에만 확정 가능 | **높음** — 미리 채우거나 생략하면, 실거래 전환 시점에 broker와 로컬 상태가 실제로 어긋나 있어도 아무도 확인하지 않은 채 넘어갈 수 있음 | **예** — 실행과 결과 판독 모두 운영자(또는 운영자가 지정한 검증 절차)만 수행 가능 |
| 7 | §6 계좌/주문 상태 | 미체결 주문(open orders) | *(값 없음 — 검토 시점 실측)* | 동일 — 이 저장소에는 아직 실제 Alpaca 계정이 연결되지 않아 코드로 산출할 데이터 자체가 없음 | **높음** — 미체결 주문이 있는 상태로 kill switch 정책을 바꾸거나 실거래를 켜면 예기치 않은 체결이 발생할 수 있음(`docs/live_review/KILL_SWITCH_RUNBOOK.md`의 해제 전 체크리스트와 동일한 이유) | **예** |
| 8 | §6 계좌/주문 상태 | 현재 포지션 | *(값 없음 — 검토 시점 실측)* | 동일 | **높음** — 기존 포지션을 모른 채 신규 정책(예: 노출 한도)을 적용하면 이미 한도를 넘긴 상태를 인지하지 못할 수 있음 | **예** |
| 9 | §7 승인 및 롤백 | 운영자 승인 | *(값 없음 — `LIVE_APPROVAL_RECORD.md`에 `approved: true`로 전환하는 것과 동일한 무게의 결정)* | `LIVE_APPROVAL_RECORD.md`는 이미 `approved: false`/`live_enabled: false`로 시작하도록 강제되어 있고, 이 문서 작성 과정에서도 변경하지 않았다(사용자 지시 준수) | **최고** — 이 프로젝트 전체에서 유일하게 "실거래 전환"을 의미하는 필드. 코드나 문서가 대신 채울 수 없고 채워서도 안 됨 | **예 — 이 항목 자체가 승인 행위** |
| 10 | §7 승인 및 롤백 | 롤백 담당자 | *(값 없음 — 조직 내 실제 담당자 성명/식별자를 운영자가 지정)* | `docs/live_review/ROLLBACK_PLAN.md` §5에 절차는 정의되어 있으나 "누가" 실행할지는 비어 있음 | **중간** — 담당자가 지정되지 않은 채 사고가 발생하면 롤백 절차가 문서로만 존재하고 실제 실행자가 불명확해질 수 있음 | **예** — 조직 내 인력 배정은 운영자 권한 |

## 요약

- **즉시 값을 제안할 수 있는 항목**: #3(주문당 최대 금액), #4(허용 종목 범위), #5(허용 거래 시간대) —
  코드/정책 맥락에서 합리적인 초안을 제시했으나 최종 확정은 운영자 몫.
- **코드로 대신 채울 수 없고, 반드시 그 시점에 실측/실행해야 하는 항목**: #1, #6, #7, #8 — 검토
  행위·broker reconciliation·미체결 주문·포지션 조회는 전부 "그 순간의 실제 상태"를 요구하므로
  미리 채우면 오히려 위험(허위 기록).
- **순수 운영 결정 항목(코드가 관여할 수 없음)**: #2(실제 키 배치), #9(운영자 승인), #10(롤백
  담당자) — 전부 사람의 판단·행위 그 자체.

## 변경 사항 확인

이 문서 작성 과정에서 다음을 변경하지 않았다:

- 코드 (`broker/**`, `risk_config.py`, `config/scalping_watchlist_config.py` 등)
- 환경변수(`.env`, `TRADING_MODE`, `ALPACA_*` 등)
- 브랜치(현재 `orchestrator/20260725-013740-us-stock-trading`에서 신규 문서 파일만 추가)
- 승인 플래그(`LIVE_APPROVAL_RECORD.md`의 `approved`/`live_enabled`는 그대로 `false`)
- `docs/live_review/LIMITED_LIVE_REVIEW_CHECKLIST.md` 원본(이 문서는 별도 파일로만 작성 — 체크리스트
  자체의 `TBD` 표기는 운영자가 직접 확정해 옮겨 적을 때까지 그대로 둔다)

실거래는 활성화하지 않았으며, 이 문서 자체도 실거래 승인을 의미하지 않는다.
