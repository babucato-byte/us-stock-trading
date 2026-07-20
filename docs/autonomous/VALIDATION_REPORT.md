# VALIDATION_REPORT

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
