# REMEDIATION_PLAN

검증 기준: `CODEX_REVIEW.md` (2026-07-21, 기준 커밋 `4a98f41`).

### CODEX-001
- 심각도: HIGH
- 재현 여부: 재현됨
- 원인: `is_paper_mode`가 `not is_live_mode`였고 주문 허용 검사에서 endpoint를 검증하지 않았다.
- 수정 방안: `TRADING_MODE == "paper"`만 허용하고 공식 Paper endpoint 외 URL은 fail-closed 처리한다.
- 수정 파일: `broker/broker_config.py`, `order_safety.py`
- 테스트: 오타 모드 차단, Paper 모드의 Live endpoint 차단
- 처리 상태: RESOLVED

### CODEX-002
- 심각도: HIGH
- 재현 여부: 재현됨
- 원인: 실행 시 당일 주문 횟수를 항상 0으로 초기화했다.
- 수정 방안: 영속 주문 이력에서 당일 주문 시도 수를 복구해 기존 한도 검사에 전달한다.
- 수정 파일: `paper_strategy_order.py`
- 테스트: 당일 이력이 한도에 도달한 상태에서 재시작 후 주문 차단
- 처리 상태: RESOLVED

### CODEX-003
- 심각도: HIGH
- 재현 여부: 재현됨
- 원인: broker 제출 성공 뒤에만 주문 이력을 기록했다.
- 수정 방안: 제출 전 `PENDING_SUBMISSION`을 영속화하고, 실패 시 제출 자체를 차단하며, 응답 후 상태를 `SUBMITTED`, `DRY_RUN`, `REJECTED`, `SUBMISSION_FAILED`로 갱신한다.
- 수정 파일: `paper_strategy_order.py`
- 테스트: 예약 저장 실패 시 broker 미호출, 재시작 시 pending 예약의 중복 차단, timeout/rejected 상태 기록
- 처리 상태: RESOLVED

### CODEX-004
- 심각도: MEDIUM
- 재현 여부: 재현됨
- 원인: pytest console entrypoint 실행 시 저장소 루트가 import path에 포함된다는 환경 의존 가정이 있었다.
- 수정 방안: `pytest.ini`에 `pythonpath = .`을 명시한다.
- 수정 파일: `pytest.ini`
- 테스트: 문서화된 `venv/bin/pytest -q` 명령으로 전체 수집 및 실행
- 처리 상태: RESOLVED

CRITICAL/HIGH 미해결 Finding은 없다. 다만 Phase 1의 별도 승인 기준인 부분 체결 처리가 남아 있어 다음 Phase 자동 진행 조건은 아직 충족하지 않는다.
