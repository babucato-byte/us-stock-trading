# VALIDATION_PACKAGE

외부 검증자(ChatGPT/Codex)가 `CODEX_REVIEW.md`를 작성하기 위해 필요한 정보 패키지. Phase 완료 시마다 갱신한다.

---

## 이번 패키지: Phase 0 완료 + Phase 1 갭 수정 (2026-07-21)

### Phase 목적
- Phase 0: 저장소 전체 구조/실행 경로/테스트 기준선을 확정하고 초단타 시스템 v1.0 로드맵을 수립.
- Phase 1(갭 수정): 기존 주문 안전장치 중 실질적으로 비활성 상태였던 포지션 크기 차단을 실제로 연결.

### 변경 파일
- `docs/autonomous/PROJECT_CONSTITUTION.md` (신규)
- `docs/autonomous/SCALPING_V1_ROADMAP.md` (신규)
- `docs/autonomous/CURRENT_STATUS.md` (신규)
- `docs/autonomous/ACCEPTANCE_CRITERIA.md` (신규)
- `docs/autonomous/VALIDATION_REPORT.md` (신규)
- `docs/autonomous/CODEX_REVIEW.md` (신규, 템플릿)
- `docs/autonomous/REMEDIATION_PLAN.md` (신규, 템플릿)
- `docs/autonomous/DECISION_LOG.md` (신규)
- `docs/autonomous/VALIDATION_PACKAGE.md` (신규, 본 파일)
- `paper_strategy_order.py` (수정 — position_rate 실계산)
- `tests/test_paper_order_execution.py` (수정 — 테스트 2건 추가)

### 핵심 diff (요약)
```python
# paper_strategy_order.py (main() 내부)
- account = broker.get_account()
- ...
- run_order_safety_check(position_rate=0.01, ...)
+ account = broker.get_account()
+ equity = float(account["equity"])
+ ...
+ order_qty = 1
+ position_value = order_qty * result["price"]
+ position_rate = (position_value / equity) if equity > 0 else float("inf")
+ run_order_safety_check(position_rate=position_rate, ...)
```
전체 diff는 `git show cd48b6f`로 확인 가능.

### 실행 명령
```bash
source venv/bin/activate
pytest -q
```

### 테스트 결과
`65 passed, 0 failed, 2 warnings` (warning 2건은 기존에도 존재하던 urllib3/LibreSSL 및 scanner 필드 경고로, 이번 변경과 무관).

### 테스트하지 못한 영역
- 부분 체결(partially_filled) 처리 — Phase 5 선행 필요.
- 초단타(1분봉/VWAP/EMA) 로직 전체 — 아직 미착수(Phase 2~4).
- Alpaca 실제 Paper 계정을 통한 end-to-end 수동 검증 — 이번 사이클은 코드/테스트 검증만 수행, 실제 계정 연동 수동 확인은 미실시.

### 안전 관련 변경
- `position_rate` 하드코딩 제거 → 실제 주문가치/equity 비율로 `MAX_POSITION_RATE`(risk_config, 값 불변) 체크가 작동하도록 수정. 리스크를 낮추는 방향의 변경이며, 정책 값 자체는 손대지 않음.

### 운영 영향
없음. 운영 서버 미접속, `.env`/systemd/cron/nginx 미변경, Live 관련 스위치 미변경.

### 남은 위험
- `run_order_safety_check` 실패 시 해당 실행의 나머지 후보가 함께 스킵되는 기존 동작 유지(의도적 보수적 설계로 판단, 변경하지 않음).
- Phase 1은 부분 체결 항목 미해결로 `VALIDATED`가 아닌 `IN_PROGRESS` 상태 유지.

### Codex가 집중 검토해야 할 항목
1. `position_rate` 계산식이 실제 Alpaca 주문 체결가(시장가 주문이므로 `result["price"]`와 체결가가 다를 수 있음)와 괴리가 있을 때의 안전성.
2. `equity <= 0`일 때 `position_rate = inf`로 강제 차단하는 방어 로직이 실제 Alpaca Paper 계정 응답 형식과 맞는지.
3. Phase 0/1 로드맵 문서의 Phase 구분과 완료 조건이 지시서 원문과 일치하는지, 누락된 항목이 있는지.

### 현재 커밋 해시
`cd48b6f` (Fix hardcoded position_rate in paper order safety check) — 본 문서 자체를 포함한 governance 문서 커밋은 다음 커밋에서 별도로 기록됨.
