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

---

향후 CODEX_REVIEW.md 지적사항에 대한 ACCEPTED/REJECTED_WITH_REASON 결정도 이 로그에 이어서 기록한다.
