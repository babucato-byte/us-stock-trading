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

### 2026-07-25 — CODEX-022(HIGH)를 `_request()` 단일 중앙 집중식 3자 일치 검증으로 해결, CODEX-021 잔여 위험도 동일 지점에서 종결
- 상황: Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ae3ca`/`c133e01`/`cc740a5`, overall
  verdict `FAIL`)이 `RequestPurpose` 재설계(CODEX-021/CODEX-020 잔여분 대응, 커밋 `c133e01`) 이후에도
  `_request()`가 주문 POST의 payload `side`와 `order_side`, `purpose`를 서로 대조하지 않는다고
  지적했다(CODEX-022, HIGH) — `purpose=EXIT_ORDER`를 선언한 채 매수 payload(`json={"side": "buy"}`)를
  보내면 `ENTRY_DISABLED` 상태에서도 HTTP가 나갔다. CODEX-021도 이 잔여 위험 때문에
  `PARTIALLY_RESOLVED`로 남았다. 검증 패키지가 검증 대상으로 제시한 신규 테스트
  `test_post_allows_entry_and_exit_purpose` 자체도 두 purpose 모두 동일한 buy payload를 사용해
  이 불일치를 실제로 검증하지 않았다는 지적도 포함됐다.
- 결정: `order_side`나 `purpose`에 추가 특수 케이스를 덧붙이는 방식이 아니라, `purpose` ×
  `order_side` × payload `side`의 3자 일치를 한 곳에서 강제하는 신규 `validate_order_intent()`
  함수를 만들어 `_request()`가 `self.session.request()`에 도달하기 전, 그리고 `_check_kill_switch()`
  보다도 먼저 호출하도록 배선했다.
- 근거: Codex가 지적한 근본 원인은 "주문의 실제 의미는 HTTP method가 아니라 payload side로
  결정되는데 현재 매트릭스는 POST의 ENTRY/EXIT 선언만 신뢰한다"는 것이었다 — 즉 신뢰할 단일
  진실 공급원이 세 값(purpose 선언, order_side, 실제 payload) 중 어느 하나가 아니라 세 값의 일치
  자체여야 한다는 의미로 해석했다. 검증 로직을 `_check_kill_switch()` 내부에 흩어 넣지 않고 별도
  함수로 분리한 이유는, 이 비교가 kill switch 정책 조회와 무관한 순수 무결성 검사이며 향후 재사용
  ·단위 테스트가 쉬워야 하기 때문이다.
- 구현: `broker/alpaca_client.py`에 `_PURPOSE_REQUIRED_SIDE`(`ENTRY_ORDER→"buy"`,
  `EXIT_ORDER→"sell"`) 매핑과 `validate_order_intent(purpose, order_side, payload)`를 추가했다.
  `ENTRY_ORDER`/`EXIT_ORDER`는 `order_side`와 payload의 `side`가 모두 존재하고, 정확히 요구되는
  문자열과 대소문자/공백/타입까지 일치해야 한다(`isinstance(..., str)`이 `bool`/`int`도 함께
  거부). `READ_ONLY`/`RECONCILIATION`/`CANCEL_ORDER`는 반대로 `order_side`와 payload의 `side`가
  둘 다 부재해야 한다. `_request()`는 다른 어떤 안전장치(런타임 재검증, kill switch 조회)보다도
  먼저 이 함수를 호출해, 불일치 시 세션 호출이 0회임을 보장한다.
- 대안: (a) `_check_kill_switch()` 내부에서 검사 — kill switch 정책과 무결성 검증을 한 함수에
  섞으면 향후 두 관심사가 서로 다른 속도로 바뀔 때(예: kill switch 상태 종류 추가) 무결성 검사가
  실수로 함께 손상될 위험이 있어 기각. (b) `submit_order()`의 기존 defense-in-depth 재검증을
  강화하는 방식 — CODEX-022 원문이 지적한 것과 동일하게 wrapper를 거치지 않는 direct `_request()`
  호출은 여전히 무방비로 남으므로 기각.
- 승인 필요 여부: 아니오(CODEX Finding에 대한 직접 수정, 자율 진행 범위). 이번 run 최종 상태는
  `READY_FOR_CODEX_REVALIDATION`이며, Codex 독립 재검증(`PROCEED`/`FAIL` 여부) 전까지
  **Limited live review: BLOCKED**, **Live trading: DO_NOT_ENABLE**을 유지한다.

### 2026-07-25 — Stage 3: 전략 플랫폼(`strategy/`) 인터페이스·레지스트리·`VWAP_MICRO_PULLBACK_MOMENTUM_V1` 초기 설정값 근거
- 상황: SCALPING_V1_ROADMAP.md Phase 4("VWAP 마이크로 풀백 전략 엔진")와 "유튜브 전략 정보 연결"
  트랙의 `COLLECTED→...→ACTIVE` 상태 저장 구조를 구현. `strategy/interface.py`(TradingStrategy
  ABC), `strategy/status.py`(상태 상수), `strategy/registry.py`(StrategyRegistry),
  `strategy/plugins/vwap_micro_pullback_v1.py`(1차 전략 구현) 신규 작성.
- 결정 1 — ACTIVE는 최대 1개, 두 번째 ACTIVE 시도는 명시적으로 거부(자동 비활성화 없음): 지시서가
  "명시적, 결정적 정책 하나를 선택해 테스트하라"고 요구했다. `register()`/`activate()` 모두, 이미
  다른 strategy_id가 ACTIVE인 상태에서 두 번째를 ACTIVE로 만들려는 시도를 `StrategyRegistrationError`로
  거부하고 첫 번째는 그대로 둔다. 대안(암묵적으로 첫 번째를 PAUSED로 내리고 두 번째를 승격)은
  `kill_switch_state.py`의 activate()/release() 분리 원칙(안전 영향이 있는 전이는 항상 명시적 별도
  호출이어야 한다)과 배치되어 기각.
- 결정 2 — `require_active()`/`select_strategy_for_order()`는 PAPER_APPROVED/LIMITED_LIVE_APPROVED도
  차단: 로드맵 원문 "ACTIVE 이전 전략은 주문 엔진에 연결하지 않는다"를 문자 그대로 적용했다. "거의
  다 됐다"는 인상을 주는 상태라도 주문 생성 가능 여부는 정확히 ACTIVE 하나로만 판정한다
  (`strategy/status.py`의 `ORDER_GENERATING_STATUSES = {ACTIVE}`).
- 결정 3 — `paper_strategy_order.py`에 가짜 연결점을 만들지 않음: 현재 `submit_order()`에는
  `strategy_id` 개념 자체가 없다(단일 하드코딩된 `analyze_stock` 스코어링만 존재). "전략이 주문을
  생성하기로 결정하는" 실제 지점은 Stage 4(Phase 5, 포지션 생명주기)가 만든다. 지시서가 명시적으로
  "실제 연결점이 없으면 가짜 연결점을 만들지 말라"고 요구했으므로, `require_active()`/
  `select_strategy_for_order()`는 `strategy/registry.py`의 독립 함수로만 구현하고 직접 테스트했다
  (`tests/test_strategy_platform.py::test_only_active_registered_vwap_strategy_may_produce_an_order`).
  Stage 4가 실제 주문 트리거 경로에서 이 함수를 호출할 것.
- 결정 4(ASSUMPTION, `config/scalping_strategy_v1_config.py`) — PROJECT_CONSTITUTION.md/로드맵
  원문에 정확한 수치가 없는 임계값들을 다음과 같이 초기값으로 고정하고 코드에 `# ASSUMPTION` 주석을
  남겼다(전부 Phase 6 백테스트 이전까지 잠정값):
  - `RALLY_MIN_PERCENT=0.5`(초기 rally의 최소 크기 — 노이즈와 구분하기 위한 최소값).
  - `MIN_PULLBACK_DEPTH_PERCENT=0.1` / `MAX_PULLBACK_DEPTH_PERCENT=3.0`("얕은 pullback"의 범위 —
    상한을 두지 않으면 "micro" pullback이라는 표현과 모순되는 깊은 조정까지 통과시키게 됨).
  - `TARGET_2_R_MULTIPLE=2.0` — 문서에 명시된 것은 "1R 도달 시 50% 분할 익절"(`TARGET_1_R_MULTIPLE=1.0`,
    `PARTIAL_EXIT_FRACTION_AT_TARGET_1=0.5`)뿐이고, 분할 익절 후 잔여 포지션의 목표(target_2)
    R-배수는 어디에도 명시되어 있지 않다. 검증되지 않은 값을 임의로 지어내는 대신, 일반적으로
    쓰이는 보수적인 러너 목표값 2R을 잠정값으로 사용했다.
  - `generate_entry()`의 진입가 규칙(ASSUMPTION, 코드 주석) — 진입가는 돌파 레벨+오프셋이 아니라
    돌파 봉의 실제 종가를 사용한다. 실제 체결되지 않은 가격을 지어내지 않기 위함.
- 대안: 위 값들을 아예 `NOT_EVALUATED`로 두고 신호를 절대 발생시키지 않는 방식도 고려했으나,
  Stage 3의 목적 자체가 "테스트 가능한 실제 로직"을 요구했고(지시서), 값이 잠정적이라는 사실 자체를
  숨기지 않고 명시(코드 주석 + 본 항목)하는 편이 "값이 없어 아무것도 못 만든다"보다 낫다고 판단.
- 승인 필요 여부: 아니오(코드 분석·테스트 추가·최소 리팩터링·mock/fixture 작성 범위, 지시서의 "변경
  승인 기준"에 해당하지 않음). 백테스트(Phase 6) 전까지 실거래 판정 근거로 사용하지 않는다.

## Stage 4(로드맵 Phase 5) — 포지션 생명주기 초기 정책 (2026-07-25)

- 결정 1 — 청산 주문은 `try_reserve_order()`를 거치지 않는다: `try_reserve_order()`/
  `is_duplicate_order()`는 "(symbol, order_date)당 최대 1행"을 강제하는 **진입** 전용 안전장치다
  (동일 종목 당일 중복 매수 방지가 목적). 청산(부분/전체/시간손절/EOD/무효화)을 이 경로로 보내면
  이미 그날 진입 주문이 기록된 종목의 매도 주문이 "중복 주문"으로 오판되어 차단된다. 따라서 진입만
  `try_reserve_order()` + `submit_order(side="buy")`를 그대로 사용하고, 청산은
  `paper_strategy_order.submit_order(side="sell")`를 직접 호출한다(kill switch/credential/
  RequestPurpose 게이트는 동일하게 전부 통과 — 우회하는 것은 order_history.csv의 진입 전용 중복
  방지 로직뿐). 중복 **청산** 방지는 `positions/store.py`의 `locked_position()`(포지션 레코드
  잠금)과 `positions/states.py`의 상태 전이 검증이 대신 맡는다.
- 결정 2 — `positions/store.py`에 `locked_position()` 컨텍스트 매니저 추가: 최초 설계였던
  "락은 저장 시점에만 잡고, 브로커 호출은 락 밖에서 한다" 방식은 두 동시 호출이 모두 같은
  사전-청산 상태를 읽고 둘 다 브로커에 매도 주문을 낼 수 있는 경쟁 조건이 있었다(락은 "누가 먼저
  기록했는지"만 보호하고 "누가 먼저 브로커를 호출했는지"는 보호하지 못함). `locked_position()`은
  읽기→판단→브로커 호출→쓰기 전체 구간 동안 락을 유지해, 두 번째 동시 호출은 첫 번째가 완전히
  끝날 때까지 블록되고 락을 얻은 시점엔 이미 갱신된 상태를 보게 되어 "더 할 일 없음"을 정확히
  판단한다(`tests/test_position_store.py::test_locked_position_serializes_concurrent_callers`로
  스레드 기반 재현).
- 결정 3(ASSUMPTION) — `MAX_POSITION_HOLD_MINUTES=60`: 지시서는 시간 손절을 요구하지만 정확한
  분(分) 값을 명시하지 않는다. PROJECT_CONSTITUTION.md의 "보유 시간: 수분에서 당일"이라는 표현에
  맞춰 60분을 보수적 초기값으로 사용(Phase 6 백테스트 전까지 잠정값).
- 결정 4(ASSUMPTION) — 잔여 수량 청산의 "전략 무효화(invalidate)" 규칙:
  `VWAPMicroPullbackV1.invalidate()`를 "최근 봉 종가가 VWAP 아래로 마감"으로 구현했다. 지시서는
  "2R 목표 또는 VWAP/EMA9 이탈"이라고만 서술하고 정확한 조건(봉중 터치 vs 종가 마감, EMA9도 함께
  요구하는지)은 명시하지 않는다. 진입 조건의 momentum 필터(EMA9>EMA21)를 청산 조건으로 그대로
  재사용하면 문서에 없는 복합 규칙을 지어내는 것이 되므로, VWAP 이탈(종가 기준, 봉중 터치 아님)
  하나만 사용한다.
- 결정 5 — `TradingStrategy.invalidate()`의 실제 시그니처가 Stage 3 스텁 시그니처
  (`evaluation, reason`)와 다르다(`bars, *, symbol`): Stage 3가 만든 자리표시자 시그니처는
  "무엇을 인자로 받을지" 확정 짓지 않은 채 `NotImplementedError`만 던지는 스텁이었고, 실제로
  청산 판단에 필요한 신호는 (진입 시점에 이미 계산된) `EvaluationResult`가 아니라 **최신 봉
  데이터**였다. 시그니처를 억지로 맞추기보다 실제로 필요한 인자로 재정의했다(코드 주석에 명시,
  `tests/test_strategy_platform.py`의 관련 테스트도 이 시그니처 변경을 반영해 갱신 — 기존 테스트를
  약화한 것이 아니라 Stage 3가 "아직 미구현"이라고 표시했던 스텁이 실제로 구현되며 자연히 깨질
  수밖에 없었던 가정을 갱신한 것).
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관).

## Stage 5 — 거래 상태 저장소: CSV vs SQLite 평가 및 전환 (2026-07-25)

- 평가: `order_history.csv` + `order_reconciliation.csv` + `POSITION_STORE.json`은 각자 원자적
  쓰기(fsync+os.replace)와 `fcntl.flock` 락을 갖췄지만, 세 파일에 걸친 단일 트랜잭션은 없다.
  주문/체결/포지션을 함께 갱신해야 하는 경우(예: 청산 주문 제출과 포지션 상태 전이) 파일 하나가
  성공하고 다른 파일이 실패하면 불일치가 발생할 수 있다 — 이는 Phase 1B에서 이미 "부분 체결의
  포지션 상태 완전 반영은 Phase 5 선행 필요"로 문서화된 것과 같은 근본 원인이다. 결론: **CSV는
  다중 파일 트랜잭션·재시작 복구·체결 이력 정규화 요구를 구조적으로 만족할 수 없다** → SQLite로
  전환 검토를 진행한다(사용자 지시서의 Stage 5 평가 조건과 일치).
- 결정 1 — 전환 범위: 이번 단계는 **로컬 SQLite 저장소를 신규 병행 인프라로만 구축**한다.
  `paper_strategy_order.py`/`positions/lifecycle.py`의 실제 운영 경로는 전혀 변경하지 않는다
  (지시서의 "실제 운영 경로 전환 금지" 절대 제약). `order_history.csv`/`order_reconciliation.csv`/
  `POSITION_STORE.json`은 계속 유일한 실제 판단 근거로 남는다. SQLite 저장소를 실제 경로에
  배선하는 것은 **별도의 명시적 사용자 결정**이 필요한 항목으로 남겨둔다(`NEEDS_USER_DECISION`).
- 결정 2 — 스키마: `orders`/`fills`/`positions`/`position_events`/`strategy_runs`/`risk_events`/
  `kill_switch_events` 7개 테이블 + `schema_migrations`(버전 추적). `fills.client_order_id`에는
  `orders`로의 FOREIGN KEY를 걸지 않았다 — `order_history.csv`가 애초에 `client_order_id`를
  저장하지 않는 레코드도 있어(그 값은 `order_intent_ledger.csv`에만 존재), 강제 FK를 걸면 정상적인
  레거시 CSV 가져오기가 실패한다. 기존 CSV들이 이미 (symbol, order_date) 같은 자연 키로만 상관되어
  있는 것과 동일한 느슨한 상관관계를 그대로 유지했다.
- 결정 3 — CSV 가져오기는 **읽기 전용**: `state_store/csv_import.py`는 `pandas.read_csv()`만
  호출하고 원본 CSV를 절대 쓰거나 삭제하지 않는다(테스트로 원본 바이트 불변 확인). 레거시
  `order_history.csv`(구버전 `symbol,order_date` 2컬럼 형식)와 현재
  `REQUIRED_HISTORY_COLUMNS`(5컬럼) 형식을 모두 허용 — 없는 컬럼은 NULL로 채우고 실패시키지
  않는다(감사용 read-only 복사본 생성이 목적이라 fail-closed보다 관용적 처리가 적절).
- 결정 4 — 롤백/내보내기: `state_store/export.py`의 `export_table()`/`export_all()`은 SQLite →
  CSV로만 쓰며, 대상 경로는 항상 호출자가 명시(현재 어떤 호출부도 실제 운영 CSV 경로를 대상으로
  지정하지 않음). `reset_schema()`는 SQLite 데이터베이스 파일 자체만 초기화하며 가져오기 원본 CSV는
  건드리지 않는다(테스트로 확인).
- 신규 테스트: `tests/test_state_store.py` 20건(스키마/마이그레이션 멱등성/트랜잭션/FK/레거시·
  신규 CSV 가져오기/가져오기 멱등성/내보내기 왕복/reset_schema/실제 DB 파일 미생성 확인).
- 전체 회귀: 703 passed, 0 failed(기존 683 + 신규 20). 실제 네트워크 호출 0회, 운영 CSV 변경 0건.
- 커밋: `bf05098`
- 승인 필요 여부: 아니오(로컬 SQLite 신규 구축·CSV 읽기 전용 가져오기·테스트 범위, 운영 경로 전환은
  포함하지 않음. 운영 경로 전환 자체는 향후 별도 사용자 승인 필요 항목으로 기록).

## Stage 7 — 전략 평가 엔진(백테스트/리플레이) 설계 근거 (2026-07-26)

사용자가 Stage 7 착수 직전 명시적으로 제시한 10개 제약을 그대로 구현했다. 이 결정 로그는 그 중
수치/정책 ASSUMPTION만 기록한다 — **백테스트 결과를 보고 나서 조정한 값은 하나도 없다**(제약 1).

- 결정 1 — 비용 가정(`backtest/config.py::BacktestConfig`): `spread_bps=5.0`,
  `slippage_bps=5.0`, `fee_per_share=0.0`(Alpaca 무수수료), `entry_delay_bars=1`,
  `max_fill_fraction_of_bar_volume=0.10`, `nominal_qty=100`. 전부 어떤 전략의 백테스트도 실행하기
  **전에** 고정한 값이며, Phase 6 결과를 이유로 사후 조정하지 않는다. 실제 측정된 스프레드/슬리피지
  수치가 확보되면 이 값을 갱신하되 반드시 새 `DECISION_LOG.md` 항목과 근거(출처)를 남긴다.
- 결정 2 — 동일봉 손절/목표 충돌 정책(제약 2): `SAME_BAR_COLLISION_STOP_FIRST`만 지원. 1분봉
  OHLCV만으로는 봉 내부에서 손절과 목표 중 무엇이 먼저 닿았는지 알 수 없으므로, 항상 손절이 먼저
  닿은 것으로 가정한다 — 전략의 edge를 과대평가할 수 없는 방향으로만 편향된 보수적 선택.
- 결정 3 — 비용 분리 표시(제약 3): `CostBreakdown`(spread_cost/slippage_cost/fee_cost/
  entry_delay_cost) 4개 필드를 거래마다 별도 기록. spread/slippage/entry_delay는 이미 체결가에
  반영된 정보성 수치(체결가 자체가 spread+slippage만큼 불리하게 조정됨)이고, fee만 realized_pnl에서
  실제로 차감된다 — 이 구분을 `models.py`의 `CostBreakdown` 문서화 주석에 명시.
- 결정 4 — 프리마켓/정규장 분리(제약 5): 진입은 `market_hours.get_us_market_session(bar_time) ==
  "regular"`일 때만 허용(`paper_strategy_order.py`의 실제 운영 게이트 `market_session == "regular"`와
  동일 조건 재사용). 프리마켓 봉은 지표 워밍업(VWAP/EMA/ATR)에는 계속 사용되지만 신호→체결의 트리거
  봉이 될 수 없다.
- 결정 5 — 부분체결·거래량 제약(제약 6): 모든 체결(진입·청산)이
  `min(desired_qty, bar_Volume * max_fill_fraction_of_bar_volume)`로 캡핑됨. 한 봉에서 다 체결하지
  못한 잔량은 다음 봉으로 이월되어 동일 조건(손절/목표)이 재평가된다 — 강제로 체결을 지어내지 않음.
  `nominal_qty=100`(ASSUMPTION, 결정 1)으로 1R 50% 분할 익절이 실제 두 개의 구분되는 체결(50주 +
  50주)로 시뮬레이션되도록 함(1주 단위였다면 50% 분할이 항상 전량 청산으로 붕괴되어 목표가 2R 러너
  청산을 검증할 수 없었음).
- 결정 6 — 최대 수익 거래 제거 결과(제약 7): `metrics.compute_metrics_with_best_trade_removed()`가
  `all_trades`와 `best_trade_removed`를 **함께** 반환 — 후자가 전자를 대체하지 않음.
- 결정 7 — 데이터 부족 처리(제약 8): `bars` 개수가 `min_bars_required`(기본 500) 미만이면
  `BacktestResult.status = INSUFFICIENT_DATA`, `trades=[]`를 반환하고 어떤 지표도 계산하지 않음.
  `compare.py`도 이 상태를 그대로 통과시켜 `metrics` 필드 자체를 행에서 생략(플레이스홀더 점수로
  치환하지 않음).
- 결정 8 — YouTube 후보는 비교 대상일 뿐(제약 9): `backtest/compare.py`는
  `strategy.registry`를 import하지 않으며 `activate()`/`register(status=ACTIVE)` 호출이 전혀 없음
  (`tests/test_backtest_engine.py::test_compare_module_never_imports_strategy_registry`로 AST 기반
  검증). 비교 테이블 생성과 전략 활성화는 구조적으로 분리되어 있으며, 활성화는 전적으로 Stage 8의
  책임으로 남겨둔다.
- 결정 9 — Look-ahead 방지(제약 4): `engine.py`의 모든 전략 호출은 `bars.iloc[:i+1]`만 전달 —
  미래 봉을 구조적으로 참조할 수 없음. 데이터가 소진되어 포지션이 청산되지 못한 신호는 거래로
  기록하지 않는다(결과를 지어내지 않음, `_try_enter`의 `fill_index >= n` 처리).
- 승인 필요 여부: 아니오(백테스트 인프라·비용 가정 문서화·테스트 범위, 실거래/승인/main/push와 무관).

## Stage 8 — 전략 선택 엔진 설계 근거 (2026-07-26)

- 결정 1 — "ACTIVE 전략만 평가"의 해석: 지시서 원문이 요구한 "ACTIVE 전략만 평가"를 문자 그대로
  `strategy/status.py`의 `ACTIVE`(등록 시점 최대 1개만 허용되는 레지스트리 상태)로 해석하면
  선택 엔진이 "이미 선택된 전략 단 하나"만 볼 수 있어 애초에 "선택"이라는 개념이 성립하지 않는다.
  대신 **평가 대상 자격**을 `strategy/status.py` 상태 기준으로 다음과 같이 재해석했다: `REJECTED`/
  `PAUSED` → `DISABLED`(운영자가 이미 끔), `COLLECTED`/`STRUCTURED`(아직 검토·백테스트 전) →
  `INSUFFICIENT_DATA`, 그 외(`REVIEWED` 이상 — `BACKTESTED`/`PAPER_APPROVED`/
  `LIMITED_LIVE_APPROVED`/`ACTIVE`)는 실제 점수 계산 대상. 이 해석 근거: 선택 엔진의 목적 자체가
  "여러 후보 중 하나를 뽑는 것"이므로 후보 풀이 복수여야 하고, 상태 진행 단계상 `REVIEWED` 미만은
  백테스트 데이터 자체가 없어 어차피 점수를 매길 수 없다(자연스럽게 `INSUFFICIENT_DATA`로 귀결).
- 결정 2 — 점수 계산 임계값(전부 ASSUMPTION, `strategy_selection/scoring.py`에 상수로 고정,
  실제 백테스트/Paper 결과를 보고 조정하지 않음):
  `MIN_TRADES_FOR_SAMPLE_SIZE_SCORE=30`(표본 충분 기준), `MDD_REFERENCE_DOLLARS=100`(백테스트
  `nominal_qty=100`과 동일 기준), `AVG_R_REFERENCE=2.0`, `PROFIT_FACTOR_REFERENCE=3.0`(각각 이
  값 이상이면 해당 하위 점수 만점). `MIN_TRADES_FOR_SCORING=10`(백테스트 거래 10건 미만이면 아예
  점수 계산 자체를 하지 않고 `INSUFFICIENT_DATA`로 처리 — 향후 실거래 승인 게이트인 "최소 100회
  체결"보다는 낮은, 순수 선택 엔진용 최소 표본 기준).
- 결정 3 — 합성 점수 가중치(`COMPOSITE_WEIGHTS`, 합=1.0): `backtest_performance`/
  `paper_performance` 각 0.20(직접 성과 지표라 가장 큰 비중), `sample_size`/`mdd` 각 0.15,
  `market_state_fit`/`symbol_condition_fit`/`slippage_sensitivity` 각 0.10. 특정 전략의 결과를
  보고 맞춘 값이 아니라 착수 전 고정. 결측 요소는 0으로 취급하지 않고 나머지 요소로 재정규화
  (`compute_composite_score`) — 일부 데이터가 없다고 해서 부당하게 낮은 점수를 받지 않도록 함.
- 결정 4 — 시장상태 적합도 게이트(`PREFERRED_MARKET_STATES`): `TradingStrategy`에 선호 세션
  메타데이터가 없어, 별도의 명시적 테이블로 관리. 현재 `VWAP_MICRO_PULLBACK_MOMENTUM_V1`만
  `{"regular"}`로 등록(`PROJECT_CONSTITUTION.md`의 정규장 전용 정책과 일치). 테이블에 없는
  `strategy_id`는 시장상태 불일치 게이트를 적용하지 않음(신규 전략 추가 시 이 테이블에 문서화된
  값을 채워야 함을 코드 주석에 명시).
- 결정 5 — 동점 처리: 합성 점수가 동일하면 입력 목록에서 먼저 나온 후보가 `SELECTED`(결정론적,
  무작위 아님).
- 결정 6 — 활성화와의 경계: `strategy_selection/engine.py`는 `strategy.registry`를 import하지
  않고 `activate()`/`ACTIVE` 등록을 전혀 호출하지 않음 — Stage 7의 `backtest/compare.py`와 동일한
  경계 원칙. `SELECTED` 판정은 추천일 뿐, 실제로 전략을 `ACTIVE`로 전환하는 것은 여전히 운영자의
  명시적 승인 절차(전략 상태 전이)를 통해서만 이루어진다.
- 신규 테스트: `tests/test_strategy_selection.py` 27건. 전체 회귀 792 passed, 0 failed.
- 승인 필요 여부: 아니오(선택 엔진 인프라·가중치 문서화·테스트 범위, 실거래/승인/main/push와 무관.
  단, 선택 결과를 실제 `ACTIVE` 전환에 사용하려면 별도 운영자 승인 절차가 필요함을 결정 6에 명시).

## Stage 3~10 통합 수정 사이클 — CODEX-023~027 (2026-07-26)

- 결정 1(CODEX-023/024, 아키텍처) — 청산 durable intent를 Stage 5의 SQLite(`state_store/`)에
  신규 `exit_intents` 테이블(migration 2)로 추가하고, 포지션 자체(JSON, `positions/store.py`)는
  마이그레이션하지 않았다. 근거: 포지션 저장소 전체를 SQLite로 옮기는 것은 기존에 광범위하게
  테스트된 JSON 기반 API(`create_position`/`load_position`/`locked_position` 등)를 건드리는
  대규모 변경이며, CODEX-024가 실제로 요구하는 것은 "broker 호출 전 durable 기록"이라는 신규
  기능이지 기존 포지션 저장소의 재작성이 아니다. exit intent만 SQLite로 분리하면 두 저장소 간
  진짜 단일 트랜잭션은 없지만(Phase 1B/Phase 5의 기존 잔여 위험과 동일한 성격), CODEX-024가
  요구한 "broker 호출 전 원자적 영속화"는 `positions.store.locked_position()` 블록이 broker
  호출 **전에** 끝나도록 3단계로 나눈 것으로 실질적으로 충족된다(Phase A가 디스크에 커밋된 뒤에만
  Phase B가 실행됨).
- 결정 2(CODEX-023, 정책) — 청산 시 broker가 반환한 `filled_avg_price`가 없으면(`None`) 손절가
  또는 target_1가로 대체(fallback)한다(기존 Stage 4 정책 유지). 실제 체결가를 모르는 상태에서
  PnL을 아예 계산하지 않는 대안도 검토했으나, 그 경우 CLOSED 포지션의 realized_pnl이 영구히
  `None`으로 남아 운영 대시보드·최종 정산에 사용할 수 없게 되므로, 트리거 가격을 보수적 근사치로
  사용하는 기존 정책을 그대로 유지하기로 했다(ASSUMPTION, 실제 체결가가 나중에 reconciliation으로
  확인되면 후속 개선 과제로 남김).
- 결정 3(CODEX-026, 범위) — 30,000원/allow-list 게이트는 `paper_strategy_order.submit_order()`의
  `side="buy" AND broker.config.is_live_mode` 조합에서만 활성화하고, Paper 거래·청산 주문에는
  전혀 적용하지 않는다. 근거: (a) 이 예산·allow-list 개념 자체가 "실거래 파일럿"에만 의미가 있고
  Paper 거래에는 적용할 논리적 근거가 없다(가짜 자금에 KRW 예산을 강제하는 것은 무의미), (b) 기존
  Paper 경로는 이미 수백 건의 테스트로 검증된 안전 크리티컬 경로이며, 이번 사이클에서 그 경로의
  동작을 하나라도 바꾸면 회귀 위험이 이번 수정 자체의 목적(안전성 강화)과 상충한다, (c) 청산은
  `kill_switch_state`의 `ENTRY_DISABLED`(신규 진입만 차단, 청산은 허용)와 동일한 기존 비대칭
  원칙을 그대로 따른다 — 이미 보유한 포지션은 예산 상태와 무관하게 항상 청산 가능해야 한다.
- 결정 4(CODEX-026, 잔여 범위) — `paper_strategy_order.submit_order()`를 우회해
  `broker.submit_order()`를 직접 호출하는 경로는 이번 게이트의 보호를 받지 못한다. 이 저장소
  전체를 검색해 그런 직접 호출 경로가 현재 존재하지 않음을 확인했으나(모든 진입 경로가 이미
  `submit_order()`를 경유), 이를 원천적으로 막는 것(예: `broker/alpaca_client.py::_request()`
  레벨에 배선)은 이미 CODEX-016~022로 검증 완료된 안전 크리티컬 네트워크 경계를 다시 건드리는
  것이라 이번 사이클 범위에서 제외했다. `docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md`
  §4의 `NEEDS_USER_DECISION`과 함께, 향후 실제 제한적 실거래 검토 시점에 재평가할 항목으로
  기록한다.
- 결정 5(CODEX-025, 복구 정책) — 손상된 store 감지 시 Kill Switch를 `ENTRY_DISABLED`가 아니라
  `MANUAL_REVIEW`로 전환한다. 근거: `ENTRY_DISABLED`는 "신규 진입만 막고 기존 포지션 청산은
  허용"하는 상태인데, 손상된 store는 청산해야 할 포지션이 있는지조차 알 수 없는 상태이므로,
  청산이 계속 허용되는 `ENTRY_DISABLED`보다 사람의 개입을 명시적으로 요구하는 `MANUAL_REVIEW`가
  더 보수적이고 정확하다(`kill_switch_state.py`의 기존 상태 의미론과 일치).
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관). 결정
  3·4에 기록된 "Live 모드 게이트가 direct broker 호출을 막지 못한다"는 잔여 범위는 실제 제한적
  실거래 검토 시점에 사용자 재확인이 필요한 `NEEDS_USER_DECISION`으로 별도 기록.

## Stage 3~10 최종 재수정 사이클 — CODEX-024/026/028/029/030 (2026-07-26)

- 결정 1(CODEX-028, 아키텍처) — `positions/store.py`의 canonical 저장소를 JSON에서 SQLite
  (`positions`/`position_events` 테이블, Stage 5 스키마에 이미 존재했으나 미사용)로 전환하고,
  `POSITION_STORE.json`은 SQLite 커밋 이후에만 쓰는 best-effort projection(재생성 가능,
  `store.regenerate_projection()`)으로 재정의했다. 범위는 `positions`/`position_events`/
  `exit_intents`로 한정하고 `orders`/`fills`(Stage 5 스키마에 있으나 진입 주문 이력은 여전히
  `order_history.csv`/`order_intent_ledger.csv`가 담당)는 이번에도 마이그레이션하지 않았다.
  근거: CODEX-028의 실제 재현(SQLite exit intent가 JSON position보다 먼저 커밋되어 fill 반영이
  유실됨)은 청산 경로의 포지션 상태 드리프트 문제이며, 진입 주문의 CSV 감사 이력과는 무관하다.
  이전 사이클(결정 1, CODEX-023/024)의 "JSON은 유지, exit intent만 SQLite로 분리" 결정을
  뒤집은 것이 아니라 완성한 것 — 그 결정이 명시적으로 인정했던 잔여 위험("두 저장소 간 진짜 단일
  트랜잭션은 없음")이 이번에 `positions.store.locked_position(conn=...)`이 exit intent 커밋과
  동일한 SQLite 트랜잭션을 공유하도록 만들어 해소됐다.
- 결정 2(CODEX-025, 재적용) — CODEX-025의 손상 감지 의미론(구조적으로 corrupted vs
  legitimately empty 구분, `PositionStoreCorruptedError`, `check_store_health()`)은 그대로
  유지하되 진단 대상을 JSON 파일에서 SQLite 파일로 옮겼다. JSON projection 단독 손상은 더 이상
  "store 손상"이 아니다 — SQLite가 살아있는 한 언제든 `regenerate_projection()`으로 다시
  만들 수 있으므로, CODEX-025가 막으려던 "손상을 빈 것으로 오인"하는 실패 모드가 애초에
  JSON에는 더 이상 적용되지 않는다(SQLite 손상에 대해서만 여전히 적용).
- 결정 3(CODEX-029, symbol 식별) — `LiveEntryContext.symbol`과 실제 주문 symbol의 일치 검사는
  `is_symbol_allowed()`(대소문자/공백 정규화, allow-list 매칭용)와 별개의, 완전히 엄격한
  (정규화 없는) 동일성 비교로 구현했다. 근거: allow-list 매칭은 "운영자가 적어둔 표기"와
  "실제 심볼"을 관대하게 대조해야 하지만, context와 실제 주문 사이의 동일성은 오히려
  대소문자/공백이 다르면 그 자체가 이상 징후(버그 또는 변조)이므로 엄격하게 차단하는 것이
  더 안전하다.
- 결정 4(CODEX-026, direct broker 우회 해소) — CODEX-026의 잔여 위험(direct broker 호출이
  게이트를 우회)을 `broker/alpaca_client.py::AlpacaBroker.submit_order()` 자체에 동일한 게이트를
  중복 배치하는 방식으로 닫았다. `broker/alpaca_client.py::_request()` 레벨(CODEX-016~022로
  검증 완료된 kill switch/purpose 게이트)은 건드리지 않고, `submit_order()` 메서드 상단에만
  추가했다 — 기존에 검증된 네트워크 경계 코드를 재작업하는 위험을 피하면서도 "최종 공통 주문
  경계"라는 요구를 충족한다. `paper_strategy_order.submit_order()` 쪽 게이트는 제거하지 않고
  유지했다(FakeBroker 등 AlpacaBroker가 아닌 테스트 더블에 대한 방어 및 이중 방어).
  이 변경으로 `broker.submit_order(side="buy")`를 live 모드에서 직접 호출하는 기존 안전 테스트
  (`test_broker_safety.py`, `test_paper_order_execution.py`)들이 최소한의 유효한
  `LiveEntryContext`를 함께 넘기도록 갱신이 필요했다 — 원래 검증하려던 "실거래는 항상 비활성화"
  라는 주장 자체는 변경 없이 그대로 통과한다(더 앞선 게이트를 하나 통과해야 원래 검사에 도달할
  뿐이다).
- 결정 5(동시성 버그, 발견 및 수정) — CODEX-024 사이클에서 이미 존재했던 `_execute_exit()`의
  `existing_intent` 분기가, lock 없이 읽은 `eil.get_active_intent()` 스냅샷이 실제 lock 획득
  시점에는 이미 CLOSED로 해소된 경우 `CLOSED -> EXIT_SUBMITTED`라는 불법 전이를 시도할 수 있는
  경쟁 조건을 갖고 있었다(전체 회귀 실행 중 1회 관측, `InvalidTransitionError`). lock 아래에서
  다시 읽은 실제 상태만 신뢰하도록 수정하고, 결정적 재현 테스트를 추가했다. 이 finding 목록에
  명시적으로 없었지만, CODEX-024의 "중복 sell 방지"가 실제로 성립하려면 이 경로도 안전해야
  하므로 이번 사이클 범위에 포함했다.
- 결정 6(CODEX-030, 범위) — `clock.py`의 Clock 주입은 `positions/lifecycle.py`의
  `check_and_manage()`/`check_invalidation()`(EOD/시간 의존 판단이 실제로 일어나는 지점)에만
  적용했다. `state_history`/`entry_time` 타임스탬프 기록에 쓰이는 `_now_iso()`(실제 UTC 기록
  용도)는 대상에서 제외했다 — 이 값들은 "포지션 상태가 실제로 언제 바뀌었는가"를 기록하는
  감사 로그이며, 시뮬레이션된 클락으로 대체하면 오히려 실제 발생 시각과 감사 기록이 어긋나는
  새로운 문제를 만든다. CODEX-030의 실제 재현(EOD 근처 실행 시 target/stop/no-action 테스트가
  EOD_FORCED_CLOSE로 바뀜)은 전적으로 `check_and_manage()`의 `now` 인자 누락이 원인이었다.
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관).

## Stage 3~10 최종 통합 수정 사이클 — CODEX-024/026/028/031/032/033 (2026-07-26)

- 결정 1(CODEX-032/024/028, 원자성) — broker rejection 시 `eil.mark_aborted()`와 position의
  `MANUAL_REVIEW` 전이를 별도 트랜잭션이 아니라 `store.locked_position(conn=conn)`의 단일
  트랜잭션(commit=False로 지연된 커밋) 안에서 함께 처리하도록 수정했다. 근거: 이전 설계는
  "broker 호출 밖에서 실행되는 mark_aborted는 position write와 원자적일 필요가 없다"는 가정에
  기반했으나, 실제로는 두 write가 서로 다른 트랜잭션이면 두 번째(position) write 실패 시 intent만
  terminal ABORTED로 남고 position은 영구히 EXIT_SUBMITTED에 갇히는 실제 재현 가능한 결함이었다.
  Phase A의 `eil.reserve(..., commit=False)`와 동일한 패턴을 재사용해 일관성을 유지했다.
- 결정 2(CODEX-031/026, 권위 있는 예산 범위) — `LiveEntryContext`가 caller에게 여전히 허용하는
  필드는 `expected_fill_price_usd`/`stop_price_usd`/`fx_rate_krw_per_usd`/`fx_rate_as_of`/
  `allow_list`/`available_cash_krw`뿐이다. `available_cash_krw`는 실제 broker 계좌 잔고처럼
  이 코드베이스가 로컬에서 독립적으로 재계산할 수 없는 시장/계좌 사실이므로 price/FX와 동일하게
  caller 입력으로 남겼다 — 대신 `min(caller 값, 신뢰 가능한 상한)`으로 caller가 이를 이용해
  상한을 넘길 수 없도록 막았다. `max_order_notional_krw`/`max_daily_loss_krw`/`max_position_count`/
  `max_daily_entries`는 여전히 필드로 존재하지만 이제 "완화" 방향으로만 작동할 수 없고 신뢰
  가능한 코드 상수(`PILOT_TOTAL_BUDGET_KRW=30_000`, `MAX_CONCURRENT_LIVE_POSITIONS=1`,
  `MAX_DAILY_LIVE_ENTRIES=2`)와 `min()`으로 교차한다. `current_open_position_count`/
  `today_entry_count`는 완전히 무시하고 `live_readiness/entry_reservation_ledger.py`의 SQLite
  기록에서만 산출한다.
- 결정 3(CODEX-031, 예산 vs 포지션 수의 서로 다른 시간 범위) — 30,000원 총 예산은 파일럿 전체에
  걸친 누적(lifetime) 배분으로 취급해 포지션이 종료돼도 절대 반환하지 않는다(플레이북의 "30,000원은
  총 테스트 예산" 문구와 일치). 반대로 동시 보유 포지션 수(`MAX_CONCURRENT_LIVE_POSITIONS`)는
  실제로 "지금 열려 있는" 개념이므로, 커밋된 예약이 연결된 position이 canonical SQLite에서
  이미 terminal 상태면 카운트에서 제외한다. 일일 진입 횟수는 당일(미국 동부 거래일 기준)로
  스코프한다. 세 가지 서로 다른 시간 범위를 하나의 캐시된 숫자로 뭉뚱그리지 않고 각각 독립적으로
  `entry_reservation_ledger.build_snapshot()`에서 계산한다.
- 결정 4(CODEX-031, 이중 예약 방지) — `AlpacaBroker.submit_order()`와
  `paper_strategy_order.submit_order()` 양쪽 모두 동일한 게이트를 실행할 수 있는 기존 구조(CODEX-026)
  때문에, 예약(reservation)이라는 부작용이 추가된 이번 사이클에서는 두 계층이 같은 주문에 대해
  중복으로 예산을 예약하는 문제가 생길 수 있었다. `broker`가 실제 `AlpacaBroker` 인스턴스이면
  wrapper가 자신의 게이트 사본을 완전히 건너뛰고 broker 쪽 게이트가 유일한 예약 지점이 되도록
  했다(`isinstance(broker, AlpacaBroker)` 분기). `AlpacaBroker`가 아닌 테스트 더블(FakeBroker 등)에
  대해서는 wrapper가 유일한 보호막이므로 그대로 게이트를 실행하고 예약 commit/release도 직접
  책임진다.
- 결정 5(CODEX-031, position 연결 및 잔여 위험) — 예약을 실제 position_id에 연결하는
  `link_position()`은 `BrokerResponse.data`에 `live_entry_reservation_id`를 실어 보내고
  `positions/lifecycle.py::enter_position()`이 이를 읽어 호출하는 방식으로 구현했다(계층 간
  reservation_id를 명시적으로 전달할 인터페이스가 없어 응답 데이터를 통해 전달) — best-effort이며
  실패해도 fail-closed(예약이 계속 활성으로 집계되어 과소평가가 아니라 과대평가로 남는다).
  broker 호출이 성공했지만(2xx) 로컬에서 예외가 발생해 release가 실행되는 극단적 경쟁 상황은
  Phase 1B의 "다중 파일 트랜잭션 부재" 잔여 위험과 동일한 성격의 미해결 범위로 남긴다(entry
  경로에 대한 crash-safe reconciliation은 이번 사이클 범위 밖).
- 결정 6(CODEX-033, 문서 정합성) — `LIMITED_LIVE_REVIEW_CHECKLIST.md` §8을
  `READY_FOR_LIMITED_LIVE_REVIEW`에서 `BLOCKED`로 되돌리고, 그 근거가 CODEX-016~022의
  `PASS_WITH_CONDITIONS`(여전히 유효)가 아니라 이후 Stage 3~10에서 발견된 별개 Finding들이라는
  점을 명시했다. `FINAL_VALIDATION_PACKAGE.md`를 최신 검증 상태의 단일 진실 공급원으로 문서에
  명시적으로 지정해, 향후 유사한 불일치가 재발하지 않도록 했다.
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관).

## CODEX-034 + 잔고 비율 기반 주문 사이징 사이클 (2026-07-27)

- 결정 1(CODEX-034, ambiguous vs definitive 실패 분류) — `requests.exceptions.HTTPError`이며
  `.response`가 실제로 설정된 경우(broker가 실제로 4xx/5xx를 응답)만 "definitive rejection"으로
  분류해 `RELEASED`, `requests.exceptions.RequestException`(Timeout/ConnectionError 등, `.response`
  없음)은 전부 "ambiguous"로 분류해 `SUBMISSION_UNKNOWN`으로 남긴다. 그 외 예외(kill switch,
  purpose/side 불일치, 자격증명 재검증 실패 등 broker 호출 자체에 도달하지 못한 사전 실패)는
  네트워크에 도달하지 않았음이 코드상 확정적이므로 안전하게 `RELEASED`. 근거: `.response`의
  유무가 "broker가 요청을 실제로 수신했는가"를 구분할 수 있는 유일한 신뢰 가능한 신호이며,
  HTTPError라도 `.response`가 None이면(예: mock/프록시 계층에서 발생) definitive로 오분류할 수
  없으므로 별도로 확인한다.
- 결정 2(CODEX-034, client_order_id 단일 정체성) — `entry_reservation_ledger.reserve()`가
  `client_order_id`를 필수 인자로 요구하도록 변경했다. `validate_and_size_live_entry()`는 caller가
  이미 생성한 `client_order_id`(예: `paper_strategy_order.py`의 `try_reserve_order()`/
  `order_intent_ledger.py` 경로)를 그대로 재사용하고, 없을 때만 자체적으로 채번한다 — 게이트웨이가
  독자적인 두 번째 ID를 채번했다면 예약(reservation)과 실제 broker 주문이 서로 다른 정체성을 갖게
  되어 `order_intent_ledger`의 추적이 깨지는 문제가 있었기 때문이다.
- 결정 3(CODEX-031/034, 누적 vs 현재 상태 시맨틱 재검토) — 이전 CODEX-031 설계는 30,000원 총
  예산을 파일럿 전체에 걸친 누적(lifetime) 배분으로 취급해 포지션 종료 후에도 반환하지 않았다.
  이번 사이클에서 `available_cash_krw`가 매 호출마다 caller가 새로 조회한 실시간 broker 잔고
  값으로 바뀌면서(이미 정산된 지출을 자연히 반영), 원장이 더 이상 lifetime 누적 지출을 추적할
  필요가 없어졌다 — 아직 정산되지 않은 노출(pending/unknown_submission/open position cost)만
  추적하면 충분하다. `entry_reservation_ledger.build_snapshot()`을 이 세 항목으로 재구성했다.
- 결정 4(사용자 지시, 고정 예산 제거) — `PILOT_TOTAL_BUDGET_KRW=30_000` 상수를
  `live_readiness/order_gateway.py`에서 완전히 삭제했다(사용자가 "30,000원은 예시일 뿐 영구
  상한이 아니다"라고 명시적으로 지시). `cash_usage_percent`(1~100, NaN/Infinity/bool/문자열/None
  차단)는 여전히 caller가 매 호출 완화할 수 없는 운영자 설정으로 유지하고,
  `MAX_CONCURRENT_LIVE_POSITIONS`/`MAX_DAILY_LIVE_ENTRIES`(신뢰 가능한 코드 상수, `min()`으로만
  교차)는 이번 사이클에서 변경하지 않았다. margin/leverage는 사용하지 않으며 `available_cash_krw`
  만을 기준으로 계산한다.
- 결정 5(actual_qty = min(잔고, 위험, 전략) 재사이징) — 손절 위험 금액이 잔고 기준 수량보다 더
  타이트한 경우, 이전 설계(risk 초과 시 주문 전체 거부)를 "risk_based_qty만큼 수량을 줄인다"로
  변경했다. 근거: 사용자가 명시한 최종 흐름이 "리스크 기준 수량과 잔고 기준 수량 중 작은 값
  선택"이며, 거부가 아니라 축소가 이 요구사항과 일치한다. `max_risk_per_trade_krw`/
  `strategy_max_quantity`는 신규 optional 필드이며 미지정 시 해당 캡은 사실상 무제한으로
  작동해(제약 없음) 기존 caller의 동작을 바꾸지 않는다 — 실제로 회귀 테스트에서 기존 71건이
  변경 없이 그대로 통과함을 확인했다(재사이징 로직 추가 후에도 무변화).
- 결정 6(watchlist affordability, 배선 범위 제외) — `live_readiness/watchlist_affordability.py`를
  `daily_candidate_scanner.py`/`scalping_watchlist/pipeline.py`에 배선하지 않고 순수 계산 모듈로만
  남겼다. 근거: Stage 10의 `live_readiness/` 자체가 이미 "building block, 아직 실거래 경로에 배선
  안 됨"이라는 선례를 갖고 있고, 이미 광범위하게 검증된 Paper 후보 파이프라인에 실시간 잔고 필터를
  끼워 넣는 것은 별도의 명시적 결정이 필요한 범위 확장이다. 계정 상태(`AccountState`)는 스캔당
  1회만 계산해 모든 후보에 공유하도록 설계했다(candidate마다 SQLite를 재조회하지 않음).
  `fractionable=true`인 종목은 1주 가격이 잔고를 초과해도 최소주문금액을 충족하면 후보로 유지하는
  것을 6개 상태 분류(`AFFORDABLE_WHOLE_SHARE`/`AFFORDABLE_FRACTIONAL`/`INSUFFICIENT_BALANCE`/
  `NOT_FRACTIONABLE`/`BELOW_MINIMUM_ORDER`/`UNKNOWN_ACCOUNT_STATE`)로 명시적으로 구분해, "예산
  없음"과 "이 종목만 분할 불가"를 caller가 구분할 수 있게 했다.
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관).

## CODEX-034~038 최종 수정 사이클 (2026-07-27)

- 결정 1(CODEX-035, definitive rejection allowlist) — "HTTPError에 response가 존재하는가"만으로
  definitive/ambiguous를 나누던 기존 판정을 폐기하고, Alpaca가 실제 주문 거절에 사용하는 status
  code의 allowlist(400/401/403/404/409/410/422) + 파싱 가능한 JSON body 조합만 definitive로
  인정하도록 바꿨다. 근거: HTTP 500/502/503/504/408/425/429는 모두 "response는 있지만 주문이
  실제로 처리됐는지 알 수 없는" 상태(upstream/gateway 오류, rate limit, timeout류)이며, response의
  유무만으로는 이 차이를 구분할 수 없다. Codex의 HTTP 500 fault-injection 반례(첫 reservation이
  RELEASED되고 두 번째 27,000원 주문이 실제 session에 도달)가 이 설계 결함을 직접 증명했다.
  allowlist에 없는 status code(예: 418)나 definitive code라도 body가 JSON으로 파싱되지 않는
  경우는 전부 fail-closed 기본값(ambiguous)으로 처리한다 — 새로운/예상치 못한 status code가
  실수로 definitive로 오분류될 수 없다.
- 결정 2(CODEX-036, authoritative cash를 어디서 강제할지) — 처음에는
  `AlpacaBroker.submit_order()`가 `self.get_account()`를 즉시 호출해 매 주문 검증마다 실제 잔고를
  가져오도록 구현했으나, 이 저장소의 `broker_config.py::validate_order_allowed()`가 dry-run 여부와
  무관하게 live 모드의 모든 broker 호출을 이미 차단하고 있다는 사실과 정면으로 충돌했다 — 그
  결과 sizing-only 검증(예: dry-run, 순수 단위 테스트)까지 "Real live trading is disabled"로
  실패하기 시작했다. 즉시 즉시-fetch 설계를 폐기하고, `validate_and_size_live_entry()`가
  이미 만들어진 `AccountCashSnapshot` 객체를 optional 인자로만 받는 방식으로 재설계했다 —
  `fetch_account_cash_snapshot()`으로 실제 fetch를 수행하는 책임은 향후 실거래가 승인되어 live
  네트워크 호출이 실제로 허용되는 시점의 production caller에게 넘긴다. 이 설계는 caller가 스냅샷을
  아예 제공하지 않으면 CODEX-036 이전과 동일하게 동작한다(opt-in 보호) — 실제 배선은 별도의 명시적
  결정(향후 실거래 승인 이후)이 필요하다는 점을 인정하되, 스냅샷을 제공하는 caller에 대해서는 지금
  당장 갭을 완전히 닫는다.
- 결정 3(CODEX-036, cash_usage_percent 트러스트 상수) — `available_cash_krw`와 달리
  `cash_usage_percent`는 시장/계좌 사실이 아니라 순수 운영 정책값이라 broker에 물어볼 대상이 없다.
  `MAX_CONCURRENT_LIVE_POSITIONS`/`MAX_DAILY_LIVE_ENTRIES`와 동일한 "신뢰 가능한 코드 상수 +
  min()으로만 교차" 패턴을 재사용해 `TRUSTED_CASH_USAGE_PERCENT_CEILING=50`(보수적 초기값)을
  도입했다. account snapshot 제공 여부와 무관하게 항상 적용되므로, snapshot 배선이 아직 없는
  현재 상태에서도 이 특정 반례(cash_usage_percent=100 요청)는 즉시 차단된다.
- 결정 4(CODEX-037, 검증 시점) — optional numeric cap 5개(주문/일일손실/거래당위험/전략수량/
  손절가)의 finite/양수 검증을 reservation lock 진입 이전, 다른 caller-input 검증(FX/현금)과 같은
  자리에 배치했다 — risk/strategy 재사이징 로직이 실행되기 전에 실패해야 "NaN이 대소 비교를
  통과해 조용히 무시된다"는 원래 결함의 재발을 구조적으로 막을 수 있기 때문이다.
- 결정 5(CODEX-038, 근본 원인) — `write_performance_files()` 자체는 수정하지 않았다(정책/동작
  변경 없음). 문제는 순수하게 테스트 격리 누락이었으므로, 테스트에 누락된 `monkeypatch.setattr`
  한 줄만 추가하는 최소 수정으로 해결했다.
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관).

## Stage 11: Account/Risk/Sizing/Execution Engine 계층 분리 (2026-07-28)

- 결정 1(reservation 이중화 회피) — Execution Engine이 broker 호출 전 자체적으로
  `entry_reservation_ledger.reserve()`를 먼저 실행하는 설계를 처음 검토했으나, `AlpacaBroker.
  submit_order()` 자체가 이미 이 저장소의 유일한 예약 지점(CODEX-026/031 결정 4)이라 두 곳에서
  각각 예약하면 동일 주문의 노출이 이중 계상된다. 대신 Execution Engine은 `client_order_id`로
  기존 예약을 조회해 대조만 하고(불일치 시 broker 호출 0회로 차단), 실제 예약 생성은 여전히
  `broker.submit_order()` 내부의 `validate_and_size_live_entry()`가 담당하도록 설계했다 —
  "broker 호출 전에 SQLite에 예약을 저장한다"는 요구사항은 이미 그 경로에서 충족되고 있었으므로,
  새 계층이 같은 일을 다시 하지 않도록 한 것이다.
- 결정 2(ValidatedOrderCommand의 reservation_id/entry_intent_id) — 사용자 지시서는 이 두 필드를
  command의 필수 필드로 요구했으나, 위 결정 1의 구조상 reservation_id는 broker 호출이 실제로
  성사된 "이후"에야 존재한다 — command 생성 시점에는 알 수 없다. `ValidatedOrderCommand`는 그
  대신 사전에 정할 수 있는 `client_order_id`(CODEX-034 방식과 동일)로 정체성을 유지하고,
  `reservation_id`는 `ExecutionResult`(broker 호출 결과)에 실어 반환하도록 재설계했다. 이 저장소가
  entry 주문에 대해 별도 `entry_intents` 테이블을 두지 않고(청산측 `exit_intents`와 달리)
  `live_entry_reservations` 자체가 entry intent 역할을 겸하고 있다는 기존 설계와도 일치한다.
- 결정 3(Execution Engine이 broker를 직접 만들지 않음) — `ExecutionEngine.submit_validated_
  command()`는 `broker`와 `live_entry_context`를 인자로 받고 스스로 구성하지 않는다. Account
  Engine의 snapshot으로부터 `LiveEntryContext`를 조립하는 로직을 Execution Engine 안에 중복
  구현하는 대신, 이미 order_gateway.py가 정의한 `LiveEntryContext` 계약을 caller가 채워서
  넘기도록 했다 — 계층 간 경계를 "누가 무엇을 검증하는가"로 유지하고, "누가 어떤 객체를
  생성하는가"를 중복시키지 않기 위함이다.
- 결정 4(정적 grep 테스트로 아키텍처 경계 강제) — "Strategy에서 직접 Broker 호출 불가"를
  런타임에 강제하는 방법(예: 호출 스택 검사, 별도 프로세스 격리)은 이번 사이클 범위에서 과도한
  복잡성으로 판단해 채택하지 않았다. 대신 `broker\.submit_order\(` 패턴의 실제 호출부(클래스명이
  아닌 변수명 기준, 즉 `AlpacaBroker.submit_order()`라는 docstring 언급은 제외)를 전체 저장소에서
  검색해 허용 목록(execution_engine.py, broker/alpaca_client.py 자기 자신, paper_strategy_order.py
  legacy compat) 밖의 호출부가 있으면 실패하는 테스트를 추가했다 — 코드 리뷰 없이 새 직접 호출이
  추가되면 CI에서 즉시 발견된다.
- 결정 5(TRUSTED_CASH_USAGE_PERCENT_CEILING 값 이전) — `trusted_operator_config.py` 신설 시 값
  자체(50%)는 CODEX-036에서 이미 결정된 것을 그대로 옮겼다(재논의하지 않음) — 이번 사이클은 "어디서
  읽는가"를 하나로 통합하는 리팩터링이지, 정책 값 자체를 바꾸는 사이클이 아니다.
- 결정 6(기존 경로 삭제하지 않음) — 사용자가 명시적으로 "기존 기능을 한 번에 삭제하지 말고 호환
  계층을 두라"고 지시함에 따라, `paper_strategy_order.py`의 `submit_order()`는 동작을 전혀
  바꾸지 않고 모듈 docstring에 "legacy compat" 지위만 명시했다. 실제 Paper 모드 주문 흐름과 관련
  기존 테스트 전부(400건 이상)는 이번 사이클에서 단 하나도 수정하지 않았다.
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관).

## CODEX-039/040/041 실제 운영 경로 배선 사이클 (2026-07-28)

- 결정 1(CODEX-040, Paper 모드는 새 파이프라인을 통과하지 않는다) — Codex의 요구사항 문구
  ("operational main()의 모든 buy entry를 Account Engine → ... → Execution Engine으로 배선")를
  문자 그대로 해석하면 Paper 모드도 포함되지만, 실제로 그렇게 하려면 Paper 주문 경로(USD/equity
  비율 기반 sizing, `order_qty=1` 고정, `risk_config.MAX_POSITION_RATE` 등)를 KRW 기반 잔고 비율
  파일럿 모델로 강제 변환해야 한다 — 두 모델은 통화 단위부터 다르고, Paper 경로는 이미 400건
  이상의 테스트로 광범위하게 검증된 안전 크리티컬 경로다. CODEX-026부터 CODEX-037까지 이
  저장소의 모든 live-entry 게이트가 예외 없이 "`side=='buy' AND is_live_mode`"로 스코프된 기존
  선례와 일치하도록, 새 파이프라인도 동일한 스코프로 제한했다. Paper 모드에 KRW 파이프라인을
  강제하는 것은 이번 사이클의 안전 목표(운영 경로 배선 완료)보다 훨씬 위험도가 높은 별개의
  변경이라고 판단했다.
- 결정 2(CODEX-039, 두 함수를 분리 유지한 이유) — `get_cash_usage_percent()`(신규, 인자 없음)와
  `get_cash_usage_percent_ceiling()`(기존, `order_gateway.py` 전용)을 하나로 합치지 않았다 —
  `order_gateway.py`의 `LiveEntryContext.cash_usage_percent`는 여전히 존재하는 필드이고, 그
  필드를 없애거나 그 필드가 있는 `LiveEntryContext`의 계약을 바꾸면 `test_live_order_gateway.py`
  137건과 관련 legacy caller들에 영향을 준다. 새 파이프라인은 애초에 그 필드를 채울 caller
  percent가 없으므로(strategy_id/signal_id만 있고 percent는 없음), 새 함수를 별도로 만들어
  "여기엔 결합할 caller 값이 없다"는 계약 자체를 이름으로 표현하는 쪽을 택했다.
- 결정 3(CODEX-040, FX rate/allow-list를 env var로 임시 소싱) — 이 저장소에는 아직 실시간 FX rate
  provider가 연동돼 있지 않다(TBD_OPERATOR). 파이프라인 배선을 완료하려면 어떤 형태로든 FX rate가
  필요했으므로, `LIVE_FX_RATE_KRW_PER_USD`/`LIVE_ENTRY_ALLOW_LIST` 환경변수를 fail-closed로 읽는
  최소 헬퍼 두 개를 `paper_strategy_order.py`에 추가했다 — 값을 조작해내지 않고, 미설정 시 그냥
  차단한다(FX rate 없으면 entry 자체를 시도하지 않고, allow-list 비어 있으면
  `is_symbol_allowed()`가 이미 fail-closed로 전부 차단). 실제 FX 제공자 연동은 여전히 별도
  TBD_OPERATOR 항목으로 남긴다.
- 결정 4(CODEX-041, watchlist 사전 필터가 아니라 실행 직전 재검증) — Codex의 요구사항은 "실제
  scanner/watchlist/main 후보 흐름에서 non-affordable result를 제거"와 "Execution Engine
  직전에도 affordability/sizing 결과를 재검증" 두 가지를 모두 언급했다. `paper_strategy_order.
  main()`은 종목을 하나씩 순회하며 `analyze_stock()`을 호출하는 구조라, 별도의 "watchlist
  일괄 필터링" 단계 자체가 존재하지 않는다(Stage 10/CODEX-034 당시 이미 확인된 구조). 그래서
  이번 사이클은 "Execution Engine 직전 재검증"만 구현했다 — Codex의 실제 반례("50,000원
  non-fractionable candidate가 30,000원 계좌에서도 broker까지 제출됨")를 정확히 재현·차단하는
  지점이며, watchlist 단계의 "사전" 필터링(효율성 목적, 안전 목적 아님)은 여전히 별도
  building block(`watchlist_affordability.py`)으로 남겨둔다 — daily_candidate_scanner.py에
  실제로 배선하는 것은 이전 사이클들과 동일하게 범위 밖으로 명시적으로 남긴다.
- 결정 5(reservation_id 잔여 위험 재확인) — Stage 11에서 이미 기록한 `ValidatedOrderCommand.
  reservation_id`가 command 생성 시점에 존재하지 않는다는 설계상 제약은 이번 사이클에서도
  변경하지 않았다 — `execution_engine.submit_validated_command()`는 여전히 유일한 예약 지점인
  `broker.submit_order()`를 그대로 사용하고, `live_entry_pipeline.py`도 별도의 선-예약을 만들지
  않는다(이중 예약 방지, Stage 11 결정 1과 동일한 근거).
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관).

## 자동 운영 구조 전환 착수 (2026-07-28, 진행 중)

- 결정 1(Codex `PASS_WITH_CONDITIONS` 기록) — `CODEX_REVIEW.md`가 작업 디렉터리에 이미 갱신된
  상태로 나타났다(외부 독립 검증 프로세스 산출물로 판단 — 실제 커밋 해시 `ae2b0fd`/`fc20574`를
  정확히 참조하고 이 저장소 테스트 스위트와 일치하는 방법론을 서술함, 지시성/권한 주장 문구
  없음). 이 프로젝트의 확립된 관례(CODEX_REVIEW.md는 손으로 편집하지 않고 그대로 커밋)에 따라
  내용을 검토하지 않고 그대로 커밋했다(`ebce9d0`). Limited live review 상태가 `BLOCKED`에서
  `READY_FOR_LIMITED_LIVE_REVIEW`로 상승했지만, Live trading은 여전히 `DO_NOT_ENABLE`이고
  `approved`/`live_enabled`도 변경하지 않았다 — 리뷰 자체가 "제한적 live review를 사람이 진행할
  준비가 코드 수준에서 됐다"고만 말할 뿐, 활성화 승인이 아니기 때문이다.
- 결정 2(cash_usage_percent 기본값 90 채택, 검증 로직은 그대로) — 사용자가 "운영자 입력이 없으면
  90 사용"을 명시적으로 요구했다. 기존 CODEX-036/039 설계(단일 신뢰 소스, `min()`이 아니라
  덮어쓰기 없이 그대로 반영, 코드 리뷰를 거치는 상수)를 그대로 유지하면서 상수값만 50→90으로
  바꿨다 — 검증 범위(0,100]나 caller 우회 불가 계약은 전혀 바꾸지 않았다. "1~100"이라는 사용자
  표현과 기존 "(0,100]" 검증 범위의 미세한 차이(0.x 같은 소수 percent 허용 여부)는 실무적으로
  무의미하다고 판단해 검증 로직 자체는 건드리지 않았다 — 필요 시 별도로 정수 강제를 추가할 수
  있으나 안전성에 영향이 없어 이번 사이클 범위에서 제외했다.
- 결정 3(소수점 주문 금지 요구사항 — 신규 구현 없이 회귀 테스트만 추가) — 사용자 지시서(§2)가
  "소수점 주문 금지"/"최소 1주 이상 매수 가능한 종목만 탐색·주문"을 요구했으나, 코드를 확인한
  결과 `live_entry_pipeline.run_live_entry_pipeline()`의 `fractionable` 파라미터가 이미 기본값
  `False`이고 유일한 호출부가 이를 절대 override하지 않으므로 요구사항이 이미 충족돼 있었다.
  새 로직을 추가하는 대신 이 불변량을 문서(모듈 docstring)와 회귀 테스트로 명시적으로 고정해
  향후 실수로 `fractionable=True`가 전달되는 회귀를 막았다.
- 결정 4(수동 allow-list/일일 진입 횟수 TBD 제거 보류) — 사용자는 이 항목들을 실거래 시작 필수
  TBD 조건에서 제외하라고 지시했으나, 현재 코드는 "시장 전체 후보에서 자동으로 종목을 선별"하는
  기능이 없다(candidates.csv/watchlist 파일에 의존, `LIVE_ENTRY_ALLOW_LIST` 환경변수가 없으면
  fail-closed로 전부 차단). 문서만 먼저 "자동 선별됨"으로 바꾸면 실제 동작과 어긋나므로, 이
  자동 선별 기능을 실제로 구현하기 전까지는 `TBD_REVIEW_RECOMMENDATIONS.md`/`LIMITED_LIVE_
  REVIEW_CHECKLIST.md`의 해당 항목을 편집하지 않기로 했다 — 이 프로젝트 전체의 "문서가 코드보다
  앞서가지 않는다"는 원칙(RUNBOOK/CODEX 사이클 전반에 걸쳐 반복된 패턴)을 그대로 적용.
- 결정 5(전략 lifecycle 자동화 지시의 나머지 부분을 구현하지 않고 사용자에게 재확인 요청) —
  두 번째 사용자 지시(전략 기반 자동 매수·매도)가 `entry_rules`/`stop_loss_rules`/...
  `end_of_day_exit_rules` 목록 코드 블록 도중 끊겼다. 이전 CODEX_REVIEW.md 절단 사례와 달리
  이번엔 저장소 파일이 아니라 사용자의 채팅 원문이라 다시 읽어올 방법이 없다. 손절가/익절/
  트레일링스탑/무효화 조건은 실거래 자금 손실과 직결되는 안전 크리티컬 로직이므로, 누락된 필드
  스펙을 추정해 구현하기보다 사용자에게 나머지 내용을 재전송해 달라고 요청했다(AskUserQuestion,
  사용자가 "이어서 붙여주기" 선택) — 아직 수신 대기 중이며, 그동안 확실하게 특정 가능한 §2의
  하위 항목(소수점 금지 등)만 결정 3에서 별도로 처리했다.
- 승인 필요 여부: 아니오(코드 구현·테스트 추가·문서화 범위, 실거래/승인/main/push와 무관).
