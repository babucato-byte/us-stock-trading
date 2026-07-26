# FINAL_VALIDATION_PACKAGE — Stage 3~10 + CODEX-023~038 (2026-07-27)

이 문서는 여섯 사이클의 최종 산출물이다: (1) 사용자의 "미국주식 초단타 자동매매 시스템 최종
자율개발 지시서"가 지정한 Stage 3~10 연속 구현, (2) Codex 1차 독립 검증(overall verdict `FAIL`,
CODEX-023~027)에 대한 통합 수정 사이클, (3) Codex 2차 통합 재검증(overall verdict `FAIL`,
CODEX-024/026 PARTIALLY_RESOLVED + 신규 CODEX-028/029/030)에 대한 재수정 사이클, (4) Codex 3차
통합 재검증(overall verdict `FAIL`, CODEX-024/026/028 PARTIALLY_RESOLVED + 신규
CODEX-031/032/033)에 대한 통합 수정 사이클, (5) Codex 4차 통합 재검증(overall verdict `FAIL`,
CODEX-026/031 PARTIALLY_RESOLVED + 신규 CODEX-034 HIGH)에 대한 수정 사이클 — 동시에 고정
30,000원 파일럿 예산을 잔고 비율(`cash_usage_percent`) 모델로 전면 교체, (6) 그 수정에 대한
Codex 5차 통합 재검증(overall verdict `FAIL`, CODEX-034 PARTIALLY_RESOLVED + 신규
CODEX-035/036/037 HIGH, CODEX-038 LOW)에 대한 최종 수정 사이클. 이 문서 자체는 6차 재검증 요청
전 최종 스냅샷이며, **실거래 승인이나 활성화를 의미하지 않는다.**

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

브랜치: `orchestrator/20260725-013740-us-stock-trading`. 여섯 구간으로 나뉜다.

### 1a~1e. Stage 3~10 ~ CODEX-034/잔고 비율 사이징 (커밋 `415c129`~`72bbb6c`)

이전 `FINAL_VALIDATION_PACKAGE.md`(§1a~§1e, 커밋 `72bbb6c` 시점)에서 이미 네 차례 Codex 검증을
거친 44개 커밋. 상세는 이전 버전 참고(git 이력에 보존됨).

### 1f. Codex 5차 독립 검증 + CODEX-035/036/037/038 최종 수정 사이클 (신규, 이번 패키지의 실제 검증 대상)

| # | 커밋 | 내용 |
|---|---|---|
| 40 | `9d294e3` | Record Codex independent review: FAIL, CODEX-034 PARTIALLY_RESOLVED, CODEX-035/036/037 HIGH, CODEX-038 LOW |
| 41 | `40abc58` | CODEX-035/036/037/038: HTTP-status allowlist, authoritative cash/percent ceilings, NaN cap validation, test isolation (최신, `HEAD`) |

`9d294e3`는 Codex 자신의 통합 재검증 결과(`CODEX_REVIEW.md`)를 그대로 기록한 커밋이며, 이
저장소는 그 파일을 손으로 편집한 적이 없다. `40abc58`이 실제 코드/테스트 수정 커밋이다(문서
갱신은 이 문서와 같은 후속 커밋에서 처리).

이 범위 이전(CODEX-001~022 원격 수정 사이클)은 이미 별도로 Codex 최종 독립 검증을 거쳐
`PASS_WITH_CONDITIONS`로 종결됨(`docs/autonomous/CODEX_REVIEW.md`의 해당 이력, 커밋 `d38cb95`).
이번 문서는 §1a~§1f 전체(46개 커밋)를 검증 대상으로 제출하되, 실질적으로 새로 검증이 필요한 것은
§1f(CODEX-035/036/037/038 수정)다 — §1a~§1e는 이미 다섯 차례 Codex의 눈을 거쳤고 그 결과가 바로
이번 수정의 근거이기 때문이다.

## 2. Stage/사이클별 변경 파일 및 테스트 결과

| 범위 | 신규 테스트 | 결과 |
|---|---|---|
| Stage 3~10(21개 커밋) | 251건 | 통과 |
| CODEX-023~027(1차 수정 사이클) | 103건 | 통과 |
| CODEX-024/026/028/029/030(2차 재수정 사이클) | 50건 | 통과 |
| CODEX-024/026/028/031/032/033(3차 통합 수정 사이클) | 13건 | 통과 |
| CODEX-034 + 잔고 비율 사이징(4차 수정 사이클, watchlist affordability 포함) | 108건 | 통과 |
| **CODEX-035**(definitive rejection status code allowlist) | `test_live_order_gateway.py`에 통합 | 통과 |
| **CODEX-036**(authoritative cash/percent 캡핑) | `test_live_order_gateway.py`에 통합 + `test_account_cash.py` 신설(17건) | 통과 |
| **CODEX-037**(optional numeric cap fail-closed 검증) | `test_live_order_gateway.py`에 통합 | 통과 |
| **CODEX-038**(테스트 격리 누락) | `test_performance_analytics.py` 1줄 수정 | 통과 |
| **합계(§1f만)** | **81건 신규**(직전 1,044 → 1,125) | **통과** |

## 3. 전체 테스트 결과

```
$ venv/bin/python -m pytest -q
1125 passed, 2 warnings
```

- 이 문서 작성 직전 최종 실행 결과(2026-07-27). 실패 0건.
- Stage 3~10 착수 시점 베이스라인 613 passed → Stage 3~10 완료 시점 820 passed → CODEX-023~027
  수정 완료 시점 923 passed → CODEX-024/026/028/029/030 수정 완료 시점 973 passed →
  CODEX-024/026/028/031/032/033 수정 완료 시점 986 passed → CODEX-034/잔고 비율 사이징 수정 완료
  시점 1,044 passed → 이번 CODEX-035/036/037/038 수정 완료 시점 **1,125 passed**.
- 두 warning은 기존 urllib3(LibreSSL) 경고와 `test_scanner.py`의 의도된 unknown-field 경고로,
  이번 범위와 무관한 기존 항목이다.

## 4. 아키텍처 요약

```
사용자 차트/YouTube 자료 (strategy_sources/)
    │  구조화(source/assumption/unknown), 버전 관리, 유사도 분석 — 절대 ACTIVE 자동 승격 없음
    ▼
전략 플러그인 구현 (strategy/interface.py, strategy/plugins/)
    │  TradingStrategy ABC, StrategyRegistry(ACTIVE 최대 1개 구조적 강제)
    ▼
백테스트/리플레이 (backtest/)
    │  1분봉 룩어헤드 방지 리플레이, 비용 분리, 동일봉 충돌 보수적 처리, 세션 분리
    ▼
전략 선택 엔진 (strategy_selection/)
    │  설명가능한 규칙 기반 점수(비-LLM), SELECTED는 추천일 뿐 — registry 미참조
    ▼
(신규 building block, 미배선) 관심종목 잔고 affordability 필터 (live_readiness/watchlist_affordability.py)
    │  fractionable 종목은 1주 가격이 잔고 초과해도 최소주문금액 충족 시 후보 유지
    ▼
포지션 생명주기 (positions/) ──── SQLite canonical (state_store/, positions/position_events)
    │  진입/체결 분리 확인/1R 50% 분할익절/2R·손절 전량청산/시간손절/EOD강제청산
    │  durable exit intent(exit_intents) — 예약·확정·rejection abort 전부 position과
    │  동일 SQLite 트랜잭션에서 커밋(CODEX-024/028/032)
    │  POSITION_STORE.json은 커밋 후 재생성 가능한 best-effort projection일 뿐
    │  fail-closed store corruption 감지(→ Kill Switch MANUAL_REVIEW 자동 전환)
    │  Clock 주입(clock.py) — EOD/시간 판단이 실행 시각이 아닌 명시적 now/clock에 의존
    ▼
Live 진입 게이트 (live_readiness/) ── side="buy" AND is_live_mode에만 적용
    │  allow-list/FX rate/symbol 동일성 fail-closed 검증(order_gateway.py)
    │  예산 = min(available_cash, account_cash_snapshot) × min(cash_usage_percent,
    │  TRUSTED_CASH_USAGE_PERCENT_CEILING) - pending - unknown_submission -
    │  open_position_cost (account_cash.py, entry_reservation_ledger.py 모두 authoritative)
    │  actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)
    │  optional numeric cap 5종 전부 NaN/Infinity/bool fail-closed 검증(CODEX-037)
    │  broker 응답이 definitive rejection allowlist에 없으면 SUBMISSION_UNKNOWN 유지,
    │  RELEASED 금지(CODEX-034/035)
    │  paper_strategy_order.submit_order() + AlpacaBroker.submit_order() 양쪽에 배선,
    │  AlpacaBroker 인스턴스에는 이중 예약 방지를 위해 단일 지점만 예약
    ▼
주문 실행 경계 (paper_strategy_order.py → broker/alpaca_client.py)
    │  RequestPurpose 게이트, kill switch 이중 검사 — CODEX-016~022 기검증, 이번 라운드 미변경
    ▼
운영 관제 (ops_dashboard/)
```

## 5. 각 구성요소 상세

### 5.1~5.7

이전 `FINAL_VALIDATION_PACKAGE.md`(커밋 `72bbb6c`) §5.1~§5.7과 동일, 이번 사이클에서 미변경.

### 5.8 Live 진입 게이트 — CODEX-035/036/037 (전면 강화)

- **CODEX-035(HTTP 5xx/ambiguous response 재분류)**: `broker/alpaca_client.py::
  _is_ambiguous_broker_failure()`/`paper_strategy_order.py::_is_ambiguous_wrapper_broker_failure()`
  가 "response 유무" 대신 `_DEFINITIVE_REJECTION_STATUS_CODES = {400, 401, 403, 404, 409, 410,
  422}` allowlist + 파싱 가능한 JSON body 조합만 definitive rejection(RELEASED)으로 인정한다.
  408/425/429/모든 5xx/파싱 불가 body/미인식 코드는 전부 ambiguous(SUBMISSION_UNKNOWN) 기본값 —
  새로운 status code도 fail-closed로 처리된다.
- **CODEX-036(authoritative cash/percent)**: 신규 `live_readiness/account_cash.py`.
  `TRUSTED_CASH_USAGE_PERCENT_CEILING=50`(신뢰 가능한 코드 상수, `MAX_CONCURRENT_LIVE_POSITIONS`/
  `MAX_DAILY_LIVE_ENTRIES`와 동일한 min()-only-tightens 패턴)이 `cash_usage_percent`를 항상
  caller-untightenable하게 제한한다. `AccountCashSnapshot`/`fetch_account_cash_snapshot(broker,
  fx_rate)`가 유일한 잔고 스냅샷 생성 경로(broker.get_account() 기반). `validate_and_
  size_live_entry()`가 신규 optional `account_cash_snapshot` 인자로 `min(caller 선언값, 실제
  스냅샷)`을 계산 — 잘못된 타입/stale snapshot은 즉시 차단. `AlpacaBroker.submit_order()` 내부
  자동 fetch는 이 저장소의 pre-live 안전 게이트(dry-run 여부 무관 모든 live 호출 차단)와 충돌해
  폐기됐다 — 실제 fetch 배선은 향후 실거래 승인 이후 production caller의 책임으로 명시적으로
  남겼다(`DECISION_LOG.md` 결정 2).
- **CODEX-037(NaN optional cap fail-closed)**: 신규 `_validate_optional_positive_number()`가
  `max_order_notional_krw`/`max_daily_loss_krw`/`max_risk_per_trade_krw`/`strategy_max_quantity`/
  `stop_price_usd` 5개 전부에 reservation lock 진입 전 적용(bool 제외, finite, 양수 검증). NaN이
  `<=0` 가드와 순서 의존적인 `min()` 비교를 통과해 한도를 무력화하던 결함 수정.
- **CODEX-038(테스트 격리)**: `tests/test_performance_analytics.py::test_summary_csv_generation`
  이 `STRATEGY_PERFORMANCE_FILE`을 격리하지 않아 실제 저장소 루트 `strategy_performance.csv`의
  mtime을 매 실행마다 변경하던 결함 — 누락된 monkeypatch 한 줄 추가로 수정.

## 6. 외부 API 호출 현황

여섯 사이클 전체 구현·테스트 과정에서 실제 Alpaca API, 실제 Slack Webhook, 실제 Yahoo/기타 외부
데이터 API를 호출한 적이 **0회**다. 모든 테스트는 fake/sequenced broker, 실제 `AlpacaBroker` +
네트워크 호출 시 예외를 던지는 세션 더블(`_NetworkForbiddenSession`), tmp_path 격리 파일로만
동작한다.

## 7. 운영 파일 변경 현황

`order_history.csv`, `universe.csv`, `strategy_performance.csv`는 여섯 사이클 내내 **바이트
단위 및 mtime까지 불변**(md5/mtime 동일, §12 참고 — CODEX-038 수정으로 mtime 불변까지 재확인
범위에 포함됐다). 이번 사이클의 모든 신규/수정 테스트 파일은
`STATE_STORE_DB_FILE`/`POSITION_STORE_FILE`/`KILL_SWITCH_FILE`/`KILL_SWITCH_STATE_FILE`을
`tmp_path`로 격리하고 `entry_reservation_ledger._LOCK_FILE`을 monkeypatch — 전체 회귀 실행
전후 실제 저장소 루트 `TRADING_STATE.db*`/`LIVE_ENTRY_RESERVATION.lock`이 생성되지 않음을
확인했다.

## 8. main/origin 및 approved/live_enabled 현황

- `main`은 여섯 사이클 내내 전혀 이동하지 않았다.
- `origin`으로 push한 적 없음.
- `approved: false`, `live_enabled: false`는 변경하지 않았다.
- Kill Switch 해제, Live API Key 입력, 실제 주문 실행, 테스트 삭제/완화, 기존 리스크 한도 완화
  등 금지된 행위는 수행하지 않았다 — 이번 사이클은 기존 한도를 오히려 더 엄격하게(HTTP 상태
  코드 오분류 축소, 트러스트 상한 신설, NaN cap 차단) 만들었을 뿐이다.

## 9. 남은 TBD_OPERATOR 항목

`docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md` + `docs/live_review/LIMITED_LIVE_30K_KRW_
PLAYBOOK.md` §7: 실계좌, 실환율(FX rate provider 연동 자체가 미구현), Live API Key,
`cash_usage_percent`/`TRUSTED_CASH_USAGE_PERCENT_CEILING`의 실제 배포값(50%가 최종 승인값인지
운영자 확인 필요), 실 승인자, 배포 시각, 롤백 담당자, 실제 Alpaca 최소 주문 금액, 실제 파일럿
종목 allow-list 내용. 어느 항목도 추정하여 확정하지 않았다.

## 10. 알려진 위험 (Codex 재검증 시 특히 확인 필요)

1. **SQLite canonical 범위가 orders/fills까지 포함하지 않음**: 진입 주문 이력은 여전히 CSV
   기반(`DECISION_LOG.md` 결정 1).
2. **`ENTRY_DISABLED` 자동 배선 미완료**: `NEEDS_USER_DECISION`으로 유지.
3. **CODEX-026/029 게이트가 `AlpacaBroker`의 향후 신규 메서드를 자동으로 보호하지 않음**:
   `submit_order()`에만 배선(`DECISION_LOG.md` 결정 4).
4. **entry 경로의 crash-safe reconciliation이 여전히 수동 트리거**: `reconcile_by_client_order_id()`
   는 단위 테스트로만 검증됐고, `positions/lifecycle.py`의 재시작/크래시 복구 경로에 자동으로
   배선되지 않았다 — 운영자가 수동으로 실행해야 한다(`INCIDENT_RESPONSE_RUNBOOK.md` 시나리오 16).
5. **`account_cash_snapshot` 전달이 opt-in이며 production 배선이 아직 없음**(신규, CODEX-036):
   실제 broker 잔고를 조회해 `LiveEntryContext`에 채워 넣는 production caller가 아직 존재하지
   않는다 — 스냅샷을 전달하지 않는 caller는 여전히 자체 선언 잔고만으로 판단된다(단,
   `cash_usage_percent`의 트러스트 상한은 스냅샷 여부와 무관하게 항상 적용된다).
6. **watchlist affordability가 실제 스캔 파이프라인에 미배선**: `live_readiness/
   watchlist_affordability.py`는 순수 계산 모듈 단위 테스트만 존재 — 실제
   `daily_candidate_scanner.py`/`scalping_watchlist/pipeline.py`와의 통합은 이번 사이클 범위
   밖.
7. **비용/정책 ASSUMPTION 다수**: 백테스트 비용 가정, 선택 엔진 가중치, 사이징 최소 주문 금액 등.
8. **미검증 YouTube 전략 후보 4건**: 어떤 주문 경로와도 연결되어 있지 않다.
9. **"마지막 성공 실행 시각"이 근사치**(`ops_dashboard/`).
10. **Phase 3(1분봉 실시간 수집/폴링 인프라) 미착수**.
11. **동시성 경쟁 조건 발견 이력**: 이전 사이클(CODEX-029/030)에서 `_execute_exit()`의 lock 없는
    읽기로 인한 경쟁 조건 1건을 발견·수정했고, 유사한 패턴이 코드베이스 다른 곳에 더 있는지는
    아직 전수 조사하지 않았다.

## 11. 검증 중점 영역 (Codex에게 요청)

1. CODEX-035의 definitive rejection allowlist가 실제 Alpaca API의 모든 거절 시나리오(잘못된
   심볼, 부족한 buying power, 유효하지 않은 파라미터 등)를 올바르게 definitive로 분류하는지,
   반대로 allowlist에 없는 status code(예: 5xx)는 항상 ambiguous로 남는지 재현 검증. 특히
   allowlist 자체가 실제 Alpaca API 문서의 rejection status code 전체를 포함하는지 (누락된
   definitive 코드가 있으면 정당한 rejection도 계속 SUBMISSION_UNKNOWN으로 남아 예산을
   불필요하게 묶어둘 수 있음, MEDIUM 이하의 availability 위험으로 평가 요청).
2. CODEX-036의 `account_cash_snapshot`/`TRUSTED_CASH_USAGE_PERCENT_CEILING` capping이 실제로
   caller가 제공하는 어떤 조합으로도 우회할 수 없는지, 특히 snapshot을 아예 제공하지 않는
   caller(opt-in 미사용)에 대해서는 CODEX-036의 보호가 적용되지 않는다는 잔여 위험(§10.5)이
   HIGH로 재분류되어야 하는지 판단 요청.
3. CODEX-037의 5개 optional cap 검증이 whole-share/fractional 양쪽 경로 모두에서 reservation
   호출 이전에 확실히 차단하는지, 다른 신규/기존 optional 필드에 유사한 NaN 취약점이 남아있지
   않은지 전수 조사.
4. CODEX-038 수정 이후 전체 테스트 실행 전후 `order_history.csv`/`universe.csv`/
   `strategy_performance.csv`의 mtime까지 완전히 불변인지 재확인.
5. 전체 테스트(1,125건)가 실제 네트워크/운영 파일 변경 없이 격리되어 있는지 임의 표본 재확인.
6. 이전 사이클에서 RESOLVED/PARTIALLY_RESOLVED였던 항목들이 이번 사이클의 코드 변경(ambiguous
   분류 강화, 트러스트 상한 신설, optional cap 검증 추가)으로 인해 실질적으로 재발/회귀하지
   않았는지.

## 12. SHA-256 (주요 안전 크리티컬 파일, 여섯 사이클 내내 미변경 확인용)

```
8b8c358fe87634ef74bed0699a6ba0f8e1ebe345cd58aa238c795fd88b179514  docs/autonomous/CODEX_REVIEW.md
27e640537c41334859eb8ad89eb3d013b17b0c95b8abf7b5385e2b76adbd5bfe  docs/live_review/LIVE_APPROVAL_RECORD.md
043a30a5dc9751c062a36a82d4d75fdbb47903a040fb7b9ab86509f047843d84  risk_config.py
34411cf9ff530e850e8de5700a19c47aa71369528f6f541c8edd0e553b9df65e  broker/broker_config.py
408e94af606ce9045b46c0b3c8aeb07f4c9ee140a595f8bab5d198397700f389  kill_switch_state.py
d06ef475cc0fa721faedd986f1eaaab6b4ac0e0192ed4bedd3b0d4d009e6c991  order_intent_ledger.py
```

(`CODEX_REVIEW.md`의 SHA-256은 이전 패키지 대비 변경됨 — Codex 자신의 최신 통합 재검증 결과가
그 파일에 기록됐기 때문이며, 파일 손상이나 수동 편집이 아니다. 나머지 5개 안전 크리티컬 파일은
이전 패키지와 SHA-256이 완전히 동일 — 여섯 사이클 내내 전혀 건드리지 않았음을 재확인한다.
`broker/alpaca_client.py`, `live_readiness/order_gateway.py`, `live_readiness/account_cash.py`,
`paper_strategy_order.py`는 이번 사이클에서 변경됐으나 이 목록의 "안전 크리티컬 파일"에는 원래
포함되지 않았다 — 변경 내용은 §5.8에 상세 기술.)

운영 파일(md5, §7 근거):
```
a61104cf03499860ae89d4e194dc8c07  order_history.csv
09c77d24f6f392a49100d13d90d61aad  universe.csv
9054d0158cf10c47d0e01e8394daaeca  strategy_performance.csv
```

이 문서 작성 시점 `HEAD`: `40abc58974659ae146535cb81c0d6db090c94831`

## 13. 다음 단계

1. 사용자에게 이 문서 완성을 보고.
2. Codex 통합 재검증 요청(§11의 검증 중점 영역 전달).
3. 판정 결과에 따라:
   - `PASS`/`PASS_WITH_CONDITIONS`: `CODEX_REVIEW.md`에 기록, §10 잔여 위험에 대한 후속 조치
     여부를 사용자와 논의(코드 변경이 필요하면 새로운 별도 사이클).
   - `FAIL`: 지적된 CRITICAL/HIGH를 동일한 패턴으로 수정하고 재검증 요청.
4. 어떤 결과든 `approved`/`live_enabled`/`main`/`origin`/실거래 활성화는 사용자의 명시적 승인
   없이는 건드리지 않는다.
