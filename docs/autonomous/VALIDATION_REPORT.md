# VALIDATION_REPORT

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
