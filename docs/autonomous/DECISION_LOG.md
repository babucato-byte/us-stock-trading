# DECISION_LOG

시간 역순이 아니라 발생 순서대로 기록한다. 각 항목: 날짜, 결정, 근거, 대안, 승인 필요 여부.

---

### 2026-07-21 — 자율개발 지시서 v1.0 수령, Phase 0 착수
- 결정: `docs/autonomous/` 8종 문서를 신규 생성하고 Phase 0(기준선 확정)부터 착수.
- 근거: 지시서 14절 "첫 실행 지시" 1~6단계.
- 대안: 없음(명시적 지시).
- 승인 필요 여부: 아니오(자율 진행 범위).

### 2026-07-21 — Phase 1을 VALIDATED로 승격하지 않음
- 결정: 기존 16개 주문 안전성 테스트(`946caea`)가 지시서의 Phase 1 필수 테스트 대부분을 충족하지만, "부분 체결 처리"는 현재 아키텍처(포지션 생명주기 상태 머신 부재)에서 의미 있게 구현/테스트할 수 없어 Phase 1 상태를 `IN_PROGRESS`로 유지.
- 근거: 지시서 12절 "완료 판정" 원칙 — 성과를 좋게 보이기 위해 기준을 완화하지 않는다. 부분 체결을 형식적으로만 테스트하면 실제로 검증되지 않은 것을 검증됐다고 기록하는 셈.
- 대안: Phase 5를 앞당겨 최소 상태 머신만 구현 후 부분 체결 테스트 추가 — 그러나 이는 "한 번에 대규모 리팩터링 금지" 원칙과 충돌하므로 기각.
- 승인 필요 여부: 아니오(품질 기준 준수, 자율 진행).

### 2026-07-21 — `position_rate` 하드코딩 버그 수정 (0.01 → 실계산)
- 결정: `paper_strategy_order.py`의 `run_order_safety_check(position_rate=0.01, ...)` 하드코딩을 `(qty * price) / equity` 실계산으로 교체. 기존 `risk_config.MAX_POSITION_RATE`(0.10) 값은 변경하지 않음.
- 근거: Phase 2(이전 세션, "Add paper order execution safety tests")에서 이미 발견한 갭(Finding E). 지시서 Phase 1 필수 테스트 "비정상 수량 및 금액 차단"을 의미 있게 통과시키려면 이 연결이 필요. "기존 버그 수정"은 자율 진행 허용 범위(6절)에 명시됨.
- 대안: 갭을 문서에만 남기고 코드는 그대로 둔다 — 그러나 이 경우 필수 테스트 항목을 사실상 통과시킬 수 없어 기각.
- 승인 필요 여부: 아니오. 임계값 자체는 불변, 기존 안전장치를 실제로 연결하는 버그 수정.

### 2026-07-21 — CODEX-007/008/009 처리 순서 및 스코프 확정
- 결정: 지시서가 지정한 우선순위(007→008→009)를 그대로 따르고, Phase 2 관련 코드는 이번 사이클에서 일절 수정하지 않음.
- 근거: CODEX-007(일일 한도 우회)과 CODEX-008(reconciliation 상태 유실)은 안전 크리티컬 경로(중복/한도 판단, 체결 상태 추적)에 직접 영향을 주는 HIGH이므로 먼저 처리. CODEX-009는 주문 경로 자체는 아니지만("주문 요청은 아니지만" — 리뷰 원문) 동일한 안전 정책 일관성 문제라 마지막으로 처리.
- 대안: 세 항목을 하나의 커밋으로 묶는 방안 — "각 커밋은 하나의 논리적 변경만 포함" 원칙과 충돌하여 기각. 지시서가 예시로 제시한 4개 커밋 분리(Enforce canonical ET order dates / Make reconciliation updates atomic and monotonic / Gate universe collection / Update docs)를 그대로 따름.
- 승인 필요 여부: 아니오(자율 진행 범위, Phase 1 갭 수정).

### 2026-07-21 — `order_history.csv`/`order_reconciliation.csv` 교차 파일 트랜잭션: SQLite 전환은 지금 하지 않음, 사용자 판단 대기 (NEEDS_USER_DECISION)
- 상황: CODEX-008을 고치면서 각 파일은 자체 `fcntl.flock` 잠금과 원자적 쓰기(temp+fsync+os.replace)를 갖게 됐지만, **두 파일에 걸친 단일 트랜잭션은 여전히 없다.** 예: `try_reserve_order()`가 `order_history.csv`에 `PENDING_SUBMISSION`을 성공적으로 기록한 직후, `order_reconciliation.csv` 기록이 실패하면 예외를 던져 주문 자체를 막지만(이번 사이클에서 새로 강제한 동작), 그 사이에 프로세스가 SIGKILL 등으로 강제 종료되면 `order_history.csv`에는 예약이 남고 `order_reconciliation.csv`에는 대응 행이 없는 상태가 이론적으로 가능하다. 마찬가지로 `main()`의 즉시 상태 갱신도 두 파일에 대해 순차적인 두 번의 잠금-쓰기이지 하나의 원자적 연산이 아니다.
- 평가: 안전 크리티컬 판단(중복 주문 차단, 일일 거래 한도)은 전적으로 `order_history.csv`만 사용하므로, 이 잔여 위험이 실거래 안전성(이중 주문, 한도 우회)에 직접적인 영향을 주지는 않는다. 다음 실행의 `reconcile_pending_orders()`가 broker의 실제 상태와 대조해 자가 치유(self-healing)하도록 설계되어 있어, 불일치는 일시적이다. 다만 Phase 5(포지션 생명주기)가 이 reconciliation 데이터를 손절/익절/강제청산 판단의 입력으로 삼기 시작하면, 이 잔여 위험의 실질적 영향이 커질 수 있다.
- 결정: 이번 사이클에서 SQLite(혹은 다른 트랜잭셔널 저장소)로 임의 전환하지 않는다. 지시서 원칙("단, 이번 작업에서 임의로 SQLite로 전환하지는 않습니다")을 그대로 따름.
- **NEEDS_USER_DECISION**: Phase 5 착수 전에 다음을 사용자가 판단해야 한다 — (a) 현재의 "각 파일 자체 원자성 + 자가 치유 reconciliation" 수준이 Phase 5 포지션 생명주기의 입력 데이터로 충분한지, 아니면 (b) SQLite 등으로 `order_history`/`order_reconciliation`/향후 포지션 상태를 단일 트랜잭션 저장소로 통합해야 하는지. 후자를 선택할 경우 이는 "대규모 리팩터링"에 해당하므로 별도 Phase로 명시적으로 계획되어야 한다.
- 대안: 지금 바로 SQLite로 전환 — 이번 사이클의 스코프(Phase 1 HIGH/MEDIUM 해결)를 넘어서고 "한 번에 대규모 리팩터링 금지" 원칙과 충돌하여 기각.
- 승인 필요 여부: **예** — 위 NEEDS_USER_DECISION 항목.

### 2026-07-21 — Phase 1 최종 Codex 판정 반영, Phase 2 착수
- 결정: Codex 최종 독립 검증(verdict `PASS_WITH_CONDITIONS`, CODEX-001~009 전부 RESOLVED, Phase 2 판정 `PROCEED`)을 그대로 반영해 Phase 1을 `Phase 1A: VALIDATED` / `Phase 1B: DEFERRED_TO_PHASE_5`로 기록하고 Phase 2(초단타 관심종목 선별 엔진)를 `IN_PROGRESS`로 착수.
- 근거: 지시서가 명시적으로 이 판정을 문서에 반영한 뒤 Phase 2를 자율 진행하도록 지시.
- 위 SQLite `NEEDS_USER_DECISION` 항목은 **Phase 2에서는 요구되지 않음**(Codex도 "Phase 2 관심종목 선별과 독립적"이라고 명시) — Phase 5 착수 전에만 재확인 필요. Phase 2 구현에서 이 결정을 선결 조건으로 취급하지 않는다.
- 승인 필요 여부: 아니오(Codex 판정 반영 및 자율 진행 범위).

---

향후 CODEX_REVIEW.md 지적사항에 대한 ACCEPTED/REJECTED_WITH_REASON 결정도 이 로그에 이어서 기록한다.

### 2026-07-21 — Phase 2 관심종목 선별 엔진: 재사용 범위와 초기 설정값 근거
- 재사용: `calculate_rsi`/`calculate_atr`(`daily_candidate_scanner.py`, 순수 함수), `market_hours.eastern_now`/`get_us_market_session`, `market_guard.is_us_trading_day` — 그대로 import해 재사용.
- **재사용하지 않기로 결정**: `daily_candidate_scanner.py`의 JSON 룰 엔진(`evaluate_filter`)은 지원하지 않는 필드/연산자를 만나면 **경고 후 통과(fail-open)**시키도록 설계되어 있다. Phase 2의 명시적 원칙("불명확하면 포함하지 않는다")과 정면으로 배치되므로, Stage A~E 필터는 Phase 2 전용의 명시적 함수로 새로 작성한다(기존 로직을 억지로 재해석하지 않는다는 지시서 2절 원칙과 일치).
- 반복탐지: 저장소에 다중 사이클 "연속 등장 횟수" 추적 로직이 존재하지 않음(확인됨 — `daily_candidate_scanner.py`의 `previous_candidates.csv` 비교는 직전 1회 사이클과의 단순 집합 차이일 뿐 스트릭 카운트가 아님). Phase 2에서 신규 구현.
- 스프레드/유동성: 저장소 전체에 스프레드 관련 로직이 전혀 없음(grep 확인). 실제 호가창 데이터 소스가 없으므로 `spread_estimate`는 이번 버전에서 항상 `NOT_AVAILABLE`로 기록하고 하드 필터로 사용하지 않는다. 대신 `avg_dollar_volume` 기반 `liquidity_score`(실제 계산 가능한 대체 지표)를 유동성 게이트로 사용한다. 이 결정은 허위 값을 만들지 않는다는 원칙을 지키기 위함이며, 실제 호가 데이터 소스 확보 시 재검토한다.
- 파일 잠금/원자적 쓰기: `paper_strategy_order.py`의 기법(temp file + fsync + os.replace, `fcntl.flock`)은 **재사용하되 코드는 재사용하지 않는다** — Phase 1 주문 실행 파일을 이번 Phase에서 변경하지 않는다는 원칙(12절) 때문에, 동일 기법을 Phase 2 전용 소규모 모듈(`scalping_watchlist/atomic_io.py`)로 독립 구현한다.
- 초기 설정값(보수적 가정, 과거 성과로 검증되지 않음 — Phase 6 백테스트 전까지 잠정값):
  - `MIN_PRICE=5`, `MAX_PRICE=500`: 최소값은 기존 `config/scanner_rules.json`의 `price>=5`를 그대로 채택. 최대값은 신규 가정(소액 계좌에서 다루기 어려운 초고가 종목 배제).
  - `MIN_AVERAGE_DOLLAR_VOLUME=20_000_000`: 기존 `scanner_rules.json`과 동일값 그대로 채택(이미 운영 중인 보수적 유동성 기준).
  - `MIN_RELATIVE_VOLUME=3.0`: `score_scanner/premarket_momentum_score.py`가 이미 사용 중인 `volume_multiple > 3` 임계값을 그대로 채택(초단타 맥락에서 검증된 유일한 기존 값).
  - `MIN_GAP_PERCENT=2.0`, `MAX_GAP_PERCENT=50.0`: 신규 가정. 프리마켓 스코어러의 10%는 "이미 강한 신호"용이라 관심종목 선별(더 넓은 후보군)에는 과함 — 낮춰서 채택. 상한은 정지/역병합 등 비정상치 배제용.
  - `MIN_ATR_PERCENT=1.5`: 신규 가정, 기존 코드에 참조값 없음.
  - `MAX_WATCHLIST_SIZE=30`: 신규 가정(1분봉 수작업 감시가 현실적인 상한).
  - `WATCHLIST_TTL_MINUTES=30`: 신규 가정(재탐지 없이 30분 경과 시 COOLING, 60분 경과 시 EXPIRED).
  - `SCORING_WEIGHTS`: liquidity/volume/gap/volatility/repeat/smart_money 6개 요소에 각 0.15~0.25 사이 가중치 균등 분배(합계 1.0), 과거 성과 근거 없음 — 명시적으로 잠정값.
- 승인 필요 여부: 아니오(지시서 4절이 초기값을 "보수적으로 결정하고 근거를 기록"하도록 위임함, 사용자 승인은 Phase 6 백테스트 이후 조정 시점에 재확인).

### 2026-07-24 — CODEX-020(HIGH)·CODEX-018 잔여분(MEDIUM)을 broker 공통 요청 경로에서 처리, wrapper 레벨 수정으로는 대체하지 않음
- 상황: Codex 독립 재검증(`CODEX_REVIEW.md`, overall verdict `FAIL`)이 `paper_strategy_order.py`
  wrapper의 kill switch 게이트를 우회해 `AlpacaBroker.submit_order()`를 직접 호출하면 binary halt와
  4-state kill switch가 모두 무시된 채 HTTP가 실제로 나간다고 지적(CODEX-020). 동시에
  `_validate_runtime_safety()`가 mode/endpoint는 재검증하면서도 현재 process 환경의 credentials
  자체는 재검증하지 않는다고 재지적(CODEX-018 잔여분).
- 결정: 두 항목 모두 `paper_strategy_order.py`가 아니라 `broker/alpaca_client.py`의
  `AlpacaBroker._request()` 공통 경로에서 처리한다. wrapper에 더 강한 검사를 추가하는 방식은
  기각 — wrapper를 거치지 않는 모든 direct 호출(현재와 향후 신규 호출부 포함)이 계속 우회 가능한
  상태로 남기 때문에, Finding의 근본 원인("network boundary 자체가 무방비")을 고치지 못한다.
- 근거: CODEX-020 원문의 Required behavior가 "모든 주문 POST 직전 broker network boundary에서"
  재검사를 요구했고, 조회/취소 경로는 정책을 명시적으로 분리해야 한다고 명시했다. 이는 wrapper
  레벨이 아니라 broker 레벨 개입을 요구하는 것으로 해석했다.
- 구현: `_request()`에 `order_side`(주문 아니면 `None`) 키워드 전용 필수 인자를 추가해 모든 호출부가
  의도를 명시하도록 강제(기본값 없음 → 생략 시 `TypeError`로 즉시 차단). 조회·취소 경로는
  `order_side=None`으로 kill switch 정책에서 명시적으로 제외. credential 재검증은 동일 공통 경로
  (`_validate_runtime_safety()`) 안에 `_validate_current_credentials_match_captured()`로 추가.
- 대안: (a) wrapper의 `submit_order()`만 강화 — 위 이유로 기각. (b) 조회 경로도 포함해 모든 요청을
  kill switch로 차단 — CODEX-020 원문이 "read-only 조회를 허용할지 정책을 명시적으로 분리"하라고
  요구했고, 조회 자체를 막으면 reconciliation/포지션 확인 등 기존 안전 동작이 깨지므로 기각.
- 승인 필요 여부: 아니오(CODEX Finding에 대한 직접 수정, 자율 진행 범위). Codex 독립 재검증
  (`PROCEED`/`FAIL` 여부) 전까지 Limited live review는 `BLOCKED`, Live trading은 `DO_NOT_ENABLE`을
  유지한다.

### 2026-07-25 — CODEX-021(HIGH)을 `order_side` 보강이 아니라 `RequestPurpose` enum 기반 재설계로 해결, CODEX-020 잔여분도 동일 설계로 함께 종결
- 상황: Codex 독립 재검증(`CODEX_REVIEW.md`, overall verdict `FAIL`)이 `_request()`의 `order_side`가
  필수 인자이긴 하지만 POST 경로와 의미적으로 결합돼 있지 않아, 명시적 `order_side=None`이
  `_check_kill_switch(None)`을 즉시 반환시켜 direct POST가 kill switch를 우회한다고 지적했다
  (CODEX-021, HIGH). 이전 사이클이 검증 패키지에서 주장한 "method+path 백스톱"이 실제로는
  구현되지 않았다는 지적이며, CODEX-020(PARTIALLY_RESOLVED)의 잔여 위험과 근본 원인이 동일했다.
- 결정: `order_side`에 추가 방어 분기를 덧붙이는 방식(예: `order_side is None`이면 path를 검사)은
  기각하고, `_request()`의 1차 판단 신호 자체를 `order_side`에서 명시적 `RequestPurpose` enum으로
  교체했다. `order_side`는 payload와 purpose의 일치를 확인하는 2차 방어선으로 격하했다.
- 근거: `order_side`에 분기를 덧붙이는 방식은 "값이 없으면 안전하지 않은 기본 동작"이라는 동일한
  구조적 취약점을 다른 조건문으로 옮길 뿐이었다 — 지시서와 Codex 요구 모두 "method/path 기반
  분류가 정상 변형에도 결정적이어야 한다"고 명시했는데, 이는 별도의 명시적 분류 축(purpose)이
  필요하다는 의미로 해석했다. `purpose`를 기본값 없는 필수 인자로 만들고 `_METHOD_PURPOSES`
  매트릭스로 HTTP method와 조합을 강제하면, 새로운 호출부가 추가되어도 의도를 명시하지 않고는
  세션에 도달할 수 없다.
- 구현: `RequestPurpose` enum(`READ_ONLY`/`ENTRY_ORDER`/`EXIT_ORDER`/`CANCEL_ORDER`/
  `RECONCILIATION`), `_METHOD_PURPOSES` 매트릭스, `_check_kill_switch(purpose, order_side=None)`
  재설계, `submit_order()`의 payload `side` ↔ `purpose` 파생값 일치 재검증. CODEX-016~019는
  이번 사이클 범위 밖이므로 코드를 건드리지 않고 관련 회귀 테스트 재실행으로만 확인했다.
- 대안: (a) `order_side is None and method == "POST"`이면 차단 — 문자열 method 비교에 의존하고
  향후 `purpose` 없는 새 order-shaped 엔드포인트가 추가되면 다시 우회 가능해 기각. (b) `order_side`
  자체의 허용값에서 `None`을 없애고 모든 non-order 호출에 별도 sentinel 문자열(`"NOT_AN_ORDER"`
  등)을 쓰게 하는 방식 — 여전히 단일 문자열 인자에 의미를 과적재하는 구조적 문제가 남아 기각.
- 승인 필요 여부: 아니오(CODEX Finding에 대한 직접 수정, 자율 진행 범위). Codex 독립 재검증
  (`PROCEED`/`FAIL` 여부) 전까지 Limited live review는 `BLOCKED`, Live trading은 `DO_NOT_ENABLE`을
  유지한다.
