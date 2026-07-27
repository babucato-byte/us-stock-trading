# FINAL_VALIDATION_PACKAGE — Stage 3~11 + CODEX-023~041 (2026-07-28)

이 문서는 여덟 사이클의 최종 산출물이다: (1) Stage 3~10 연속 구현, (2)~(6) Codex 1~5차 독립
검증(CODEX-023~038, 매 사이클 상세는 이전 버전 참고, git 이력에 보존됨)에 대한 통합 수정, (7)
사용자가 Codex 재검증과 별도로 지시한 **Stage 11 — Account/Risk/Sizing/Execution Engine
계층 분리**(building block으로 추가, 운영 경로 미배선), (8) 그 직후 Codex 6차 통합 재검증
(overall verdict `FAIL`, CODEX-036 PARTIALLY_RESOLVED + 신규 CODEX-039 MEDIUM/CODEX-040
HIGH/CODEX-041 MEDIUM)에 대한 **실제 운영 주문 경로 배선 완료 사이클**. 이 여덟 번째 사이클이
이 문서의 실제 검증 대상이며, Stage 11에서 building block으로만 존재하던 신규 엔진들을
`paper_strategy_order.main()`의 실제 live-mode 주문 경로에 배선해 CODEX-040의 legacy-bypass
문제를 해소했다. 이 문서 자체는 다음 Codex 재검증 요청 전 최종 스냅샷이며, **실거래 승인이나
활성화를 의미하지 않는다.**

## 0. 최종 상태

```
상태: READY_FOR_FINAL_CODEX_REVALIDATION
```

- `approved: false`, `live_enabled: false` 유지([LIVE_APPROVAL_RECORD.md](../live_review/LIVE_APPROVAL_RECORD.md)).
- Live trading: **`DO_NOT_ENABLE`**.
- Limited live review: **`BLOCKED`**(이번 수정이 아직 Codex 재검증을 거치지 않았으므로).
- `main`/`origin`: 어느 것도 건드리지 않음(아래 §8).
- `READY_FOR_30K_KRW_LIMITED_LIVE_REVIEW`/`LIVE_READY`/`LIVE_APPROVED`/`PRODUCTION_READY` 등의
  표현은 이 문서를 포함해 어디에도 사용하지 않았다.

## 1. 검증 대상 커밋

브랜치: `orchestrator/20260725-013740-us-stock-trading`.

### 1a~1g. Stage 3~10 ~ Stage 11 계층 분리 (커밋 `415c129`~`14f7a13`)

이전 `FINAL_VALIDATION_PACKAGE.md`(§1a~§1g, 커밋 `14f7a13` 시점)에서 이미 다섯 차례 Codex 검증을
거친 47개 커밋. 상세는 이전 버전 참고(git 이력에 보존됨).

### 1h. Codex 6차 독립 검증 + CODEX-039/040/041 실제 운영 경로 배선 사이클 (신규, 이번 패키지의 실제 검증 대상)

| # | 커밋 | 내용 |
|---|---|---|
| 43 | `fff8007` | Record Codex independent review: FAIL, CODEX-036 PARTIALLY_RESOLVED, CODEX-039/041 MEDIUM, CODEX-040 HIGH |
| 44 | `ae2b0fd` | CODEX-039/040/041: wire live-mode main() through Account/Risk/Sizing/Affordability/Execution Engine (최신, `HEAD`) |

`fff8007`는 Codex 자신의 통합 재검증 결과(`CODEX_REVIEW.md`)를 그대로 기록한 커밋이며, 이
저장소는 그 파일을 손으로 편집한 적이 없다. `ae2b0fd`이 실제 코드/테스트 수정 커밋이다(문서
갱신은 이 문서와 같은 후속 커밋에서 처리). 이 범위 이전(CODEX-001~022 원격 수정 사이클)은 이미
별도로 Codex 최종 독립 검증을 거쳐 `PASS_WITH_CONDITIONS`로 종결됨(`docs/autonomous/
CODEX_REVIEW.md`의 해당 이력, 커밋 `d38cb95`). 이번 문서는 §1a~§1h 전체(49개 커밋)를 검증
대상으로 제출하되, 실질적으로 새로 검증이 필요한 것은 §1h(CODEX-039/040/041 배선)다.

## 2. Stage/사이클별 변경 파일 및 테스트 결과

| 범위 | 신규 테스트 | 결과 |
|---|---|---|
| Stage 3~10(21개 커밋) | 251건 | 통과 |
| CODEX-023~027(1차 수정 사이클) | 103건 | 통과 |
| CODEX-024/026/028/029/030(2차 재수정 사이클) | 50건 | 통과 |
| CODEX-024/026/028/031/032/033(3차 통합 수정 사이클) | 13건 | 통과 |
| CODEX-034 + 잔고 비율 사이징(4차 수정 사이클, watchlist affordability 포함) | 108건 | 통과 |
| CODEX-035/036/037/038(5차 수정 사이클) | 81건 | 통과 |
| Stage 11(엔진 5종 building block + watchlist affordability 확장) | 174건 | 통과 |
| **CODEX-039/040/041**(실제 운영 경로 배선) | `test_trusted_operator_config.py`(+9)/`test_execution_engine.py`(+2)/`test_live_entry_pipeline.py`(신설 11)/`test_main_live_entry_wiring.py`(신설 9) | 통과 |
| **합계(§1h만)** | **32건 신규**(직전 1,299 → 1,331) | **통과** |

## 3. 전체 테스트 결과

```
$ venv/bin/python -m pytest -q
1331 passed, 2 warnings
```

- 이 문서 작성 직전 최종 실행 결과(2026-07-28). 실패 0건.
- Stage 3~10 착수 시점 베이스라인 613 passed → Stage 3~10 완료 시점 820 passed → CODEX-023~027
  수정 완료 시점 923 passed → CODEX-024/026/028/029/030 수정 완료 시점 973 passed →
  CODEX-024/026/028/031/032/033 수정 완료 시점 986 passed → CODEX-034/잔고 비율 사이징 수정 완료
  시점 1,044 passed → CODEX-035/036/037/038 수정 완료 시점 1,125 passed → Stage 11 완료 시점
  1,299 passed → 이번 CODEX-039/040/041 배선 완료 시점 **1,331 passed**.
- 두 warning은 기존 urllib3(LibreSSL) 경고와 `test_scanner.py`의 의도된 unknown-field 경고로,
  이번 범위와 무관한 기존 항목이다.

## 4. 아키텍처 요약 — 실제 운영 경로 (CODEX-040 배선 이후)

```
Market Scanner (daily_candidate_scanner.py / paper_strategy_order.load_watchlist())
    ▼
Strategy Engine (paper_strategy_order.analyze_stock() 점수 산출)
    │  entry_price/score만 산출 — 계좌 잔고·비율·최종 수량은 절대 산출하지 않음
    ▼
[live_readiness/live_entry_pipeline.py::run_live_entry_pipeline() -- side="buy" AND
 broker.config.is_live_mode 인 진입에만 적용. Paper 모드는 아래 경로를 타지 않고
 기존 try_reserve_order()+submit_order() 흐름을 그대로 사용(완전 미변경).]
    │
    ▼ 1. Account Engine (live_readiness/account_engine.py)
    │     broker.get_account() 기반 AccountSnapshot, effective_cash=min(cash, non_margin)
    ▼ 2. 신뢰 가능한 cash_usage_percent (trusted_operator_config.get_cash_usage_percent(),
    │     인자 없음 — caller percent와 절대 결합하지 않음, CODEX-039)
    ▼ 3. Risk Engine (live_readiness/risk_engine.py)
    │     entry/stop price + daily-loss-remaining으로 risk_based_qty 독자 계산
    ▼ 4. Sizing Engine (live_readiness/sizing_engine.py)
    │     actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)
    ▼ 5. Affordability Filter (live_readiness/watchlist_affordability.evaluate_affordability(),
    │     watchlist 후보 선별과 동일한 함수, CODEX-041)
    ▼ 6. Execution Engine (live_readiness/execution_engine.submit_validated_command())
    │     ValidatedOrderCommand 검증(만료/변조/symbol/기존 예약 불일치, 5종 실패 시 broker 0회),
    │     account_cash_snapshot을 broker에 전달(CODEX-036 잔여 위험 해소)
    ▼
Broker (broker/alpaca_client.py::AlpacaBroker.submit_order())
    │  live_entry_context 게이트(order_gateway.py, CODEX-026~037, 이번 사이클 미변경)
    ▼
포지션 생명주기 (positions/) / 운영 관제 (ops_dashboard/)
```

각 단계 실패는 `LiveEntryPipelineError`로 즉시 전파되며 broker 호출은 0회다 — 런타임 통합
테스트(`tests/test_main_live_entry_wiring.py`)가 정상 경로에서 4개 엔진 + affordability가
각각 정확히 1회 호출됨과, 각 단계 실패 시 broker 호출 0회를 확인한다.

## 5. 각 구성요소 상세

### 5.1~5.9

이전 `FINAL_VALIDATION_PACKAGE.md`(커밋 `14f7a13`) §5.1~§5.9와 동일, 이번 사이클에서 미변경
(Stage 11에서 만든 5개 엔진 모듈 자체의 내부 로직은 건드리지 않음 — 이번 사이클은 "배선"만
추가했다).

### 5.10 CODEX-039/040/041 — 실제 운영 경로 배선 (신규)

- **CODEX-039**: `trusted_operator_config.get_cash_usage_percent()` 신설(인자 없음, 트러스트
  값을 그대로 반환). `get_cash_usage_percent_ceiling()`(레거시 `order_gateway.py` 전용,
  `min(caller, trusted)` 계약)과 이름/문서를 명시적으로 분리, 값 자체(50%)는 변경 없음.
- **CODEX-040**: 신규 `live_readiness/live_entry_pipeline.py::run_live_entry_pipeline()`이
  Account → Risk → Sizing → Affordability → Execution Engine을 실제로 orchestrate.
  `paper_strategy_order.main()`이 `side="buy" AND broker.config.is_live_mode`인 진입에 대해
  이 파이프라인을 호출하도록 배선 — Paper 모드는 코드 한 줄도 바뀌지 않음(기존 400건 이상 테스트
  그대로 통과). `LIVE_FX_RATE_KRW_PER_USD`/`LIVE_ENTRY_ALLOW_LIST` 환경변수를 fail-closed로
  읽는 헬퍼 2개 추가(TBD_OPERATOR: 실제 FX provider/allow-list 연동은 여전히 미구현).
  `execution_engine.submit_validated_command()`가 신규 optional `account_cash_snapshot`을
  `broker.submit_order()`로 전달(공급 시에만, 구형 테스트 더블 호환 유지).
- **CODEX-041**: `live_entry_pipeline.py`가 Sizing Engine 직후·Execution Engine 직전에
  `evaluate_affordability()`를 재실행(watchlist 후보 선별과 동일 함수) — non-affordable이면
  broker 호출 0회로 차단. watchlist 단계의 사전 일괄 필터링(scanner 전체 대상, 효율성 목적)은
  이번 사이클에서 배선하지 않음(`DECISION_LOG.md` 결정 4 참고 — `main()` 구조상 일괄 필터 단계가
  애초에 없고, 실행 직전 재검증이 실제 안전 반례를 정확히 차단한다).
- **런타임 통합 테스트**: `tests/test_main_live_entry_wiring.py` — live 모드 정상 경로에서
  Account/Risk/Sizing/Execution Engine + affordability가 각각 정확히 1회 호출, 레거시
  `submit_order()` wrapper 미호출, Account Engine 실패/FX rate 미설정/allow-list 불일치/잔고
  부족/예약 충돌이 각각 broker 호출 0회로 차단, Paper 모드는 신규 엔진 호출 0회로 완전히
  미변경임을 확인(9건). `tests/test_live_entry_pipeline.py`(11건)는 파이프라인 자체의 순수
  유닛/통합 테스트.

## 6. 외부 API 호출 현황

여덟 사이클 전체 구현·테스트 과정에서 실제 Alpaca API, 실제 Slack Webhook, 실제 Yahoo/기타 외부
데이터 API를 호출한 적이 **0회**다. 모든 테스트는 fake/sequenced broker, 실제 `AlpacaBroker` +
네트워크 호출 시 예외를 던지는 세션 더블, tmp_path 격리 파일로만 동작한다.

## 7. 운영 파일 변경 현황

`order_history.csv`, `universe.csv`, `strategy_performance.csv`는 여덟 사이클 내내 **바이트
단위 및 mtime까지 불변**(md5/mtime 동일, §12 참고). 신규 테스트는 `STATE_STORE_DB_FILE`/
`POSITION_STORE_FILE`/`ORDER_HISTORY_FILE`/`NOTIFICATION_HEALTH_STATE_FILE` 등을 `tmp_path`로
격리하고 `entry_reservation_ledger._LOCK_FILE`을 monkeypatch — 전체 회귀 실행 전후 실제 저장소
루트 `TRADING_STATE.db*`/`LIVE_ENTRY_RESERVATION.lock`/`NOTIFICATION_HEALTH_STATE.json`이
생성되지 않음을 확인했다.

## 8. main/origin 및 approved/live_enabled 현황

- `main`은 여덟 사이클 내내 전혀 이동하지 않았다.
- `origin`으로 push한 적 없음.
- `approved: false`, `live_enabled: false`는 변경하지 않았다.
- Kill Switch 해제, Live API Key 입력, 실제 주문 실행, 테스트 삭제/완화, 기존 리스크 한도 완화
  등 금지된 행위는 수행하지 않았다 — 이번 사이클은 기존 게이트(order_gateway.py/alpaca_client.py
  의 CODEX-026~037 로직)를 대체하지 않고 그 앞단에 Account/Risk/Sizing/Affordability 검증을
  추가했을 뿐이며, Paper 모드 주문 흐름 및 관련 기존 테스트는 단 하나도 수정하지 않았다.

## 9. 남은 TBD_OPERATOR 항목

`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md` + `docs/live_review/LIMITED_LIVE_30K_KRW_
PLAYBOOK.md` §7: 실계좌, 실환율(FX rate provider 연동 자체가 미구현 — `LIVE_FX_RATE_KRW_PER_USD`
환경변수로 임시 소싱), Live API Key, `cash_usage_percent`의 실제 배포값(50%가 최종 승인값인지
운영자 확인 필요), 실 승인자, 배포 시각, 롤백 담당자, 실제 Alpaca 최소 주문 금액, 실제 파일럿
종목 allow-list 내용(`LIVE_ENTRY_ALLOW_LIST` 환경변수로 임시 소싱). 어느 항목도 추정하여 확정하지
않았다.

## 10. 알려진 위험 (Codex 재검증 시 특히 확인 필요)

1. **SQLite canonical 범위가 orders/fills까지 포함하지 않음**: 진입 주문 이력은 여전히 CSV
   기반(`DECISION_LOG.md` 결정 1).
2. **`ENTRY_DISABLED` 자동 배선 미완료**: `NEEDS_USER_DECISION`으로 유지.
3. **entry 경로의 crash-safe reconciliation이 여전히 수동 트리거**: `reconcile_by_client_order_id()`
   는 단위 테스트로만 검증됐고, 재시작/크래시 복구 경로에 자동 배선되지 않았다.
4. **watchlist 사전 일괄 affordability 필터가 scanner에 미배선**(신규 재확인): 실행 직전
   재검증(이번 사이클에서 구현)이 안전 측면에서는 동등하지만, `daily_candidate_scanner.py` 자체의
   효율성 개선(불필요한 analyze_stock 호출 방지)은 여전히 미해결.
5. **실제 FX rate/allow-list provider 연동이 환경변수 임시 소싱 수준**(신규): `LIVE_FX_RATE_
   KRW_PER_USD`/`LIVE_ENTRY_ALLOW_LIST` 둘 다 fail-closed지만 실제 운영급 provider 연동은
   별도 TBD_OPERATOR로 남아있다.
6. **`ValidatedOrderCommand.reservation_id`가 command 생성 시점이 아니라 broker 호출 이후에만
   확정됨**: 이번 사이클에서도 변경하지 않음(Stage 11 결정 2, 단일-예약-지점 아키텍처 유지).
7. **비용/정책 ASSUMPTION 다수**: 백테스트 비용 가정, 선택 엔진 가중치, 사이징 최소 주문 금액 등.
8. **미검증 YouTube 전략 후보 4건**: 어떤 주문 경로와도 연결되어 있지 않다.
9. **Phase 3(1분봉 실시간 수집/폴링 인프라) 미착수**.
10. **동시성 경쟁 조건 발견 이력**: 이전 사이클(CODEX-029/030)에서 1건을 발견·수정했고, 유사한
    패턴이 코드베이스 다른 곳에 더 있는지는 아직 전수 조사하지 않았다.

## 11. 검증 중점 영역 (Codex에게 요청)

1. `paper_strategy_order.main()`의 live-mode 분기가 실제로 Account/Risk/Sizing/Affordability/
   Execution Engine을 정확한 순서로, 정확히 1회씩 호출하는지 (a) 정상 경로 (b) 각 단계 실패
   경로 모두에서 broker 호출 횟수를 계측해 재확인.
2. Paper 모드 `main()`이 이번 사이클로 인해 단 하나도 달라지지 않았는지 — 기존 400건 이상의
   Paper 테스트가 문자 그대로 동일한 assertion으로 계속 통과하는지, 신규 엔진 모듈에 대한
   import/호출이 Paper 경로에 전혀 없는지 재확인.
3. CODEX-039의 `get_cash_usage_percent()`가 실제로 어떤 경로로도 caller-declared 값과 결합되지
   않는지 — `live_entry_pipeline.py`의 함수 시그니처 자체에 percent 파라미터가 없음을 코드로
   재확인.
4. CODEX-041의 affordability 재검증이 Sizing Engine의 `actual_qty`와 독립적으로 계산되어 두
   결과가 실제로 일치하는지, 불일치 시 어느 쪽이 최종 결정권을 갖는지 명확한지 평가.
5. `LIVE_FX_RATE_KRW_PER_USD`/`LIVE_ENTRY_ALLOW_LIST` 환경변수 fail-closed 처리(미설정/잘못된
   값 시 반드시 차단, 절대 기본값을 조작해내지 않음)를 fault-injection으로 재확인.
6. 전체 테스트(1,331건)가 실제 네트워크/운영 파일 변경 없이 격리되어 있는지 임의 표본 재확인.

## 12. SHA-256 (주요 안전 크리티컬 파일, 여덟 사이클 내내 미변경 확인용)

```
40014f7979ee9b3b25387303a9c3c2e782b5656a3d55972b448675f3369571bf  docs/autonomous/CODEX_REVIEW.md
27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe  docs/live_review/LIVE_APPROVAL_RECORD.md
043a30a5dc9751c062a36a82d4d75fdbb47903a040fb7b9ab86509f047843d84  risk_config.py
34411cf9ff530e850e8de5700a19c47aa71369528f6f541c8edd0e553b9df65e  broker/broker_config.py
408e94af606ce9045b46c0b3c8aeb07f4c9ee140a595f8bab5d198397700f389  kill_switch_state.py
d06ef475cc0fa721faedd986f1eaaab6b4ac0e0192ed4bedd3b0d4d009e6c991  order_intent_ledger.py
1194ccc44ebd2fafd98f1fb07d56f5823fadbdaad29a0ef9e2e8aadd63b7e1e3  broker/alpaca_client.py
```

(`CODEX_REVIEW.md`의 SHA-256은 이번 사이클에서 Codex 자신의 최신 통합 재검증 결과가 그 파일에
기록됐기 때문에 변경됨 — 파일 손상이나 수동 편집이 아니다. 나머지 6개 파일은 이전 패키지와
SHA-256이 완전히 동일 — 여덟 사이클 내내 전혀 건드리지 않았음을 재확인한다. `paper_strategy_
order.py`, `live_readiness/execution_engine.py`, `live_readiness/trusted_operator_config.py`는
이번 사이클에서 변경됐고 신규 `live_readiness/live_entry_pipeline.py`가 추가됐으나, 이 목록의
"안전 크리티컬 파일"에는 원래 포함되지 않았다 — 변경 내용은 §5.10에 상세 기술.)

운영 파일(md5, §7 근거):
```
a61104cf03499860ae89d4e194dc8c07  order_history.csv
09c77d24f6f392a49100d13d90d61aad  universe.csv
9054d0158cf10c47d0e01e8394daaeca  strategy_performance.csv
```

이 문서 작성 시점 `HEAD`: `ae2b0fd61f0032611fefabe57b8c38c630a80532`

## 13. 다음 단계

1. 사용자에게 이 문서 완성을 보고.
2. Codex 통합 재검증 요청(§11의 검증 중점 영역 전달).
3. 판정 결과에 따라:
   - `PASS`/`PASS_WITH_CONDITIONS`: `CODEX_REVIEW.md`에 기록, §10 잔여 위험에 대한 후속 조치
     여부를 사용자와 논의(코드 변경이 필요하면 새로운 별도 사이클).
   - `FAIL`: 지적된 CRITICAL/HIGH를 동일한 패턴으로 수정하고 재검증 요청.
4. 어떤 결과든 `approved`/`live_enabled`/`main`/`origin`/실거래 활성화는 사용자의 명시적 승인
   없이는 건드리지 않는다.
