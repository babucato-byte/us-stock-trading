# VALIDATION_REPORT

## 2026-07-22 — Phase 2 구현 완료 (초단타 관심종목 선별 엔진)

`scalping_watchlist/` 패키지로 Stage A(거래가능성 재검증)~E(설명 가능한 가중합 점수) 파이프라인을 구현했다(커밋 `4a96883`).

- 재사용: `daily_candidate_scanner.calculate_rsi`/`calculate_atr`, `market_hours.eastern_now`/`get_us_market_session`, `market_guard.is_us_trading_day`.
- 재사용하지 않기로 한 것: 기존 JSON 룰 엔진(`evaluate_filter`, 불명확 필드 시 fail-open) — Phase 2 원칙("불명확하면 포함하지 않는다")과 배치되어 전용 함수로 신규 작성. 근거는 `DECISION_LOG.md`.
- 신규 구현(저장소에 대응 로직 없었음, 확인됨): 다중 사이클 반복탐지 스트릭 추적(`repeat_tracker.py`), 유동성 대체지표(`liquidity_score`, `spread_estimate`는 데이터 소스 부재로 항상 `NOT_AVAILABLE`), Stage E 점수 엔진.
- 파일 안전성: `order_history.csv`와 동일한 기법(temp file+fsync+os.replace, `fcntl.flock`)을 `scalping_watchlist/atomic_io.py`에 독립 재구현(Phase 1 파일 미변경 원칙 준수).

테스트: 신규 34건(정상 선별/점수순 정렬/최대 관심종목 수/동점 결정성, 가격·거래량·거래대금·상대거래량·변동성·유동성 부족 차단, 데이터 누락·지연·비정상치 차단, 최초/재등장/타거래일 초기화/ET 경계/재등장 구분/동시성 lost-update 방지, 하위점수-가중치 일치/점수 범위/NaN·Infinity 차단/입력순서 무관성, 원자적쓰기 실패 시 원본 보존/잠금 타임아웃/손상파일 fail-closed, Fake provider only/개별 provider 오류 격리) 전부 통과, 동시성 테스트 5회 반복 안정.

전체 회귀: **183 passed, 0 failed** (기존 149 + 신규 34), 저장소 루트/상위 디렉터리 동일 결과. 실제 Alpaca/Slack/Yahoo 호출 0회. `order_history.csv` 해시 불변 — Phase 1 운영 로직/파일 미변경 확인.

Phase 2 상태: **`IMPLEMENTED`**(Claude 자체 검증). Codex의 `PROCEED` 판정 전까지 `VALIDATED`로 승격하지 않음.

---

## 2026-07-21 — Phase 1 최종 Codex 판정 및 Phase 2 착수

`CODEX_REVIEW.md` 최종 독립 검증(대상 커밋 `05757fe`/`0c2dab4`/`16a1ee4`/`56e11be`) 결과: **overall verdict PASS_WITH_CONDITIONS**. CODEX-001~009 전부 RESOLVED, 신규 Finding 없음, 회귀 없음. 전체 테스트 149 passed, 집중 테스트 106 passed, 동시성 테스트 6 passed×5회, 실제 외부 API 호출 0회, 운영 CSV/runtime 변경 없음.

Phase 판정:
- **Phase 1A(주문 진입 안전성): VALIDATED**
- **Phase 1B(부분체결·포지션 생명주기): DEFERRED_TO_PHASE_5** — Phase 1 자체 판정은 `KEEP_IN_PROGRESS`(Codex 표현), Codex Finding이 아니라 Phase 1 승인 기준 자체의 미충족 항목.
- **Phase 2: PROCEED**

이 결과를 `SCALPING_V1_ROADMAP.md`/`CURRENT_STATUS.md`/`DECISION_LOG.md`에 반영하고 Phase 2(초단타 관심종목 선별 엔진) 착수. Phase 2는 Claude 자체 테스트만으로 `VALIDATED` 처리하지 않고 `IMPLEMENTED`로 표기하며, Codex의 `PROCEED` 판정 후에만 `VALIDATED`로 승격한다.

---

## 2026-07-21 — Phase 1 추가 수정 사이클 (CODEX-007~009)

독립 재검증(대상 커밋 `9688a13`/`b93a08a`/`22a6651`/`962eb69`/`1cc784b`, verdict FAIL)이 CODEX-003/004/005는 RESOLVED로 최종 확인했지만, CODEX-001/002/006을 PARTIALLY_RESOLVED로 되돌리고 신규 CODEX-007(HIGH)/008(HIGH)/009(MEDIUM)를 제기했다. 지시서 우선순위(007→008→009)대로 처리했다.

- **CODEX-007**: `load_order_history()`가 날짜 파싱 성공 여부만 확인하던 것을 `validate_order_date_str()`(정규식+실제 달력 유효성+원본 왕복 일치)로 교체. 단 하나의 비정규 `order_date`도 전체 이력을 `CORRUPTED_HISTORY`로 판정해 신규 주문을 차단한다(자동 마이그레이션 없음, 진단 전용 `diagnose_order_history_dates()` 별도 제공). 이로써 CODEX-002의 잔여 위험이 해소되어 CODEX-002도 RESOLVED로 승격. (`05757fe`)
- **CODEX-008**: `order_reconciliation.csv` 전용 `fcntl.flock` 잠금 도입(`order_history`용 잠금 로직을 `_file_lock()`으로 일반화해 재사용). `merge_reconciliation_state()`가 상태 후퇴 금지·`filled_qty` 비감소·가격 비소거를 강제하는 단조 병합을 수행하며, 손상된 reconciliation 파일은 `ReconciliationUnavailable`로 fail-closed(자동 재초기화 금지). reconciliation 저장 실패는 이제 주문 예약 자체를 차단하도록 전파되고, `main()`의 즉시 상태 갱신과 reconciliation 스냅샷이 동일한 함수 결과를 공유해 두 파일이 서로 다른 즉시 상태를 기록하는 문제도 제거. **실제 `multiprocessing.Process` 2건**으로 동시 갱신 시 최종 상태가 후퇴하지 않고 lost update가 없음을 재현 검증. 이로써 CODEX-006의 잔여 위험도 해소되어 RESOLVED로 승격. (`0c2dab4`)
- **CODEX-009**: `universe_builder.py`가 공통 broker 안전검사를 우회해 환경변수 기반 URL로 직접 GET하던 것을, `AlpacaBroker.get_assets()`(기존 `_request()` 게이트 재사용)로 교체. 8종 endpoint 변조 시나리오(스킴 다운그레이드·유사 호스트명·비표준 포트·경로/쿼리 조작·userinfo·빈값/공백)를 파라미터라이즈드 테스트로 검증. 저장소 전체 grep으로 다른 Alpaca 직접 호출 경로가 없음(스크래치 파일 2개 제외, 이미 collect_ignore 대상)을 확인. 이로써 CODEX-001의 잔여 위험도 해소되어 RESOLVED로 승격. (`16a1ee4`)

검증: 저장소 루트/상위 디렉터리 4가지 pytest 조합 모두 **149 passed, 0 failed**. 집중 테스트(broker_safety + paper_order_execution + universe_builder) **106 passed**. 동시성 관련 테스트(threading + multiprocessing) 5회 반복 모두 **6 passed**로 안정. `git diff --check` 통과. `order_history.csv` 해시/크기/mtime 사이클 전후 불변.

**잔여 판단(NEEDS_USER_DECISION)**: `order_history.csv`와 `order_reconciliation.csv`는 각각 자체 잠금과 원자적 쓰기를 갖지만, 두 파일에 걸친 단일 트랜잭션은 없다. 안전 크리티컬 판단(중복/일일한도)은 전적으로 `order_history.csv`에만 의존하므로 이 잔여 위험이 실거래 안전성 자체를 위협하지는 않지만, 프로세스가 두 파일에 대한 쓰기 사이에 강제 종료되면 다음 `reconcile_pending_orders()` 실행 전까지 두 파일이 일시적으로 불일치할 수 있다. SQLite 전환 여부는 `DECISION_LOG.md`에 사용자 판단 대기 항목으로 기록했다(임의 전환하지 않음).

CRITICAL 0건, HIGH 전부(001/002/003/005/006/007/008) RESOLVED, MEDIUM 전부(004/009) RESOLVED. Phase 1은 부분 체결의 "포지션 상태" 완전 반영이 Phase 5 범위라 여전히 `IN_PROGRESS`.

---

## 2026-07-21 — Phase 1 재수정 사이클 (CODEX-001~006)

`CODEX_REVIEW.md`(대상 커밋 `fe2988c`/`dc9bff9`, verdict FAIL, Phase 2 DO_NOT_PROCEED)의 지시서 우선순위(001→002→003→006→005→004)대로 재수정했다.

- **CODEX-001**: `AlpacaBroker._request()`(GET 경로)가 `submit_order()`와 동일한 안전검사를 거치지 않던 문제 수정. 모든 broker 호출이 매번 `self.config`를 재검증하도록 통일. (`9688a13`)
- **CODEX-002**: `load_order_history()`를 fail-closed로 전환(`MISSING_HISTORY`/`CORRUPTED_HISTORY` 구분), 거래일 판정을 서버 로컬 시간에서 `market_hours.eastern_now()`(America/New_York) 기준으로 변경. (`b93a08a`)
- **CODEX-003**: `order_history.csv` 쓰기를 임시파일+fsync+`os.replace()` 원자적 방식으로 전환, `fcntl.flock` 기반 프로세스 잠금 도입. `try_reserve_order()`가 잠금 하에 이력을 다시 읽고 중복/일일한도를 재검사한 뒤에만 기록. `threading` 기반 실제 동시성 재현 테스트로 lost update 없음을 확인. (`b93a08a`)
- **CODEX-006**: 스키마 동결 원칙을 지키며 별도 파일 `order_reconciliation.csv`로 `client_order_id`/체결 상태 추적을 추가. 매 실행 시작 시 비종결 상태를 broker와 대조(`reconcile_pending_orders`), partially_filled≠filled 유지, 미인식 주문은 `MANUAL_REVIEW`(재주문 없음). (`22a6651`)
- **CODEX-005**: 저장소 루트 `conftest.py`에 `collect_ignore` 추가 — 상위 디렉터리에서 경로를 명시해 pytest를 실행해도(이 경우 `testpaths`가 무시됨) 루트 스크래치 스크립트가 수집되지 않도록 함. (`962eb69`)
- **CODEX-004**: 동일 `conftest.py`가 수집 시점에 저장소 루트를 `sys.path`에 직접 삽입 — 실행 위치/ini 해석 여부와 무관하게 import가 안정적으로 동작. (`962eb69`)

검증: 저장소 루트(`pytest -q`, `python -m pytest -q`)와 저장소 상위 디렉터리에서 경로 명시(`pytest us-stock-trading -q`, `python -m pytest us-stock-trading -q`) 4가지 조합 모두 **97 passed, 0 failed**. 동시성 테스트 5회 반복 재실행으로 플레이키니스 없음 확인. `git diff --check` 통과. Live URL이 코드 어디에서도 기본값/폴백으로 쓰이지 않음을 grep으로 재확인. `order_history.csv` 해시가 이번 사이클 전후로 불변(`a61104cf...`) — 실제 운영 파일 미변경.

CRITICAL 0건, HIGH 5건(001/002/003/006/005) 전부 RESOLVED, MEDIUM 1건(004) RESOLVED. Phase 1은 부분 체결의 "포지션 상태 반영"이 Phase 5 범위라 여전히 `IN_PROGRESS`(정책적으로 `VALIDATED`로 올리지 않음 — 상세는 `CURRENT_STATUS.md`/`SCALPING_V1_ROADMAP.md`).

---

## 2026-07-21 — Codex 독립 검증 수정 사이클

- HIGH 3건과 MEDIUM 1건을 실제 코드/테스트 실행으로 재현하고 모두 수정했다.
- 주문 모드는 정확히 `paper`이고 endpoint는 공식 Alpaca Paper URL인 경우에만 허용한다.
- 주문 이력에서 당일 주문 수를 복구하며, 제출 전에 `PENDING_SUBMISSION` 예약을 저장한다.
- `pytest.ini`의 import 경로를 고정했다.
- 회귀 테스트 5건을 추가/갱신했고 전체 결과는 70 passed, 0 failed, 2 warnings다.
- 실제 Alpaca/Slack 호출, 운영 서버 변경, Live 활성화, 데이터 삭제는 수행하지 않았다.
- Phase 1 부분 체결 승인 기준은 미충족이므로 상태는 `IN_PROGRESS`다.

Claude 자체 검증 결과 기록 (외부 검증자의 `CODEX_REVIEW.md`와는 별개).

---

## 2026-07-21 — Phase 0 + Phase 1 갭 수정 사이클

### 범위
- `docs/autonomous/` 8종 문서 신규 생성
- `paper_strategy_order.py`의 `position_rate` 하드코딩(0.01) 버그 수정
- `tests/test_paper_order_execution.py`에 비정상 주문 금액 차단 테스트 2건 추가

### 실행 명령 및 결과
```
./venv/bin/python -m pytest -q
```
```
65 passed, 2 warnings in 1.68s
```
- 이전 기준선(63) 대비 신규 2건 추가, 기존 63건 전부 유지(회귀 없음).
- 실제 Alpaca/Slack 네트워크 호출: 0회 (전부 `FakeBroker`/`DummySession`/monkeypatch).
- 실제 운영 CSV(`order_history.csv` 등) 변경: 0건 (전부 `tmp_path`).

### 코드 변경 검증
- `position_rate = (order_qty * result["price"]) / equity` (equity<=0이면 `inf`로 안전 측 처리) — `risk_config.MAX_POSITION_RATE` 등 기존 임계값은 미변경, 값을 실제로 연결만 함.
- 기존 happy-path 테스트(등가/가격 비율 0.01)가 그대로 통과함을 확인 — 회귀 없음.
- 신규 테스트로 equity 대비 과도한 주문가치(20%)가 실제로 `run_order_safety_check`에서 차단됨을 확인.

### 테스트하지 못한 영역
- 부분 체결(partially_filled) 처리 — Phase 5(포지션 생명주기) 선행 필요, 현재 아키텍처에 해당 개념이 없어 의미 있는 테스트 불가. `SCALPING_V1_ROADMAP.md` Phase 1/5에 명시.
- `analyze_stock`의 RSI/MA200/거래량 계산 자체의 수치 정확성 — 이번 사이클은 안전장치 경로만 검증, 계산 로직은 monkeypatch로 우회.

### 안전 관련 변경
- `position_rate` 실계산 도입은 기존에 사실상 비활성 상태였던 안전장치를 활성화하는 방향이므로 리스크를 낮추는 변경. 임계값 자체는 무변경.

### 운영 영향
- 없음. 운영 서버 미접속, systemd/cron/nginx 미변경, `.env` 실값 미변경.

### 남은 위험
- `run_order_safety_check` 호출부에 여전히 try/except가 없어, 한 심볼에서 안전장치가 발동하면 해당 실행의 나머지 후보도 함께 스킵됨(의도된 보수적 동작으로 유지, `DECISION_LOG.md` 참고).
- `position_rate` 계산에 사용하는 `equity`는 매 실행 시 1회만 조회되며 루프 중 갱신되지 않음(기존 동작과 동일, 이번 변경으로 새로 생긴 위험은 아님).
