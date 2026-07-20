# CODEX_REVIEW

Review target: Phase 1 safety remediation 독립 재검증

Commits: `fe2988c`, `dc9bff9`

Phase: Phase 1 — 주문 안전성과 실행 경로 검증

Date: 2026-07-21

Overall verdict: **FAIL**

기존 테스트 70건은 저장소 루트에서 모두 통과하지만, CODEX-002와 CODEX-003의 HIGH 위험이 실제로 남아 있고 상위 디렉터리 테스트 실행에서 네트워크성 스크립트가 수집되는 신규 HIGH Finding이 확인됐다.

## Previous findings verification

### [CODEX-001]

Status: **PARTIALLY_RESOLVED**

Evidence:

- `BrokerConfig.is_paper_mode`는 `trading_mode == "paper"`만 허용한다.
- `paper`, `PAPER`, 앞뒤 공백, 오타, 빈 문자열, `None`을 직접 주입해 확인했다. 정확한 소문자 `paper`만 주문 전송 단계까지 도달하고 나머지는 모두 차단됐다. 환경변수 기본값 경로는 import 시 `.strip().lower()`가 적용된다.
- `ALPACA_PAPER_BASE_URL=https://api.alpaca.markets` 환경변수 주입 상태에서 `submit_order()`는 `RuntimeError`로 차단됐다.
- 설정 객체는 frozen dataclass이므로 일반 대입 변경은 `FrozenInstanceError`로 차단된다.
- 기본 endpoint는 `https://paper-api.alpaca.markets`이며 `submit_order()` 66행에서 최종 주문 허용 검사를 다시 수행한다.
- Live 주문 POST 우회는 재현되지 않았다. Live URL 문자열은 `broker/broker_config.py`, 테스트 및 문서에 계속 존재하지만 주문 POST 경로는 차단된다.

Remaining risk:

- `AlpacaBroker._request()`는 endpoint 안전 검사를 하지 않고 credential 존재만 확인한다. `paper_strategy_order.main()`은 주문 안전 검사보다 먼저 `get_account()`와 `get_positions()`를 호출하므로, Paper 모드에 Live endpoint가 주입되면 주문 POST는 막혀도 Live endpoint GET 접근은 먼저 발생할 수 있다. “Live endpoint 사용 차단” 주장은 전체 broker 경로에는 성립하지 않는다.
- 알 수 없는 mode의 `status_label`이 `PAPER`로 표시되는 진단상 모호성도 남지만 주문은 차단된다.

### [CODEX-002]

Status: **PARTIALLY_RESOLVED**

Evidence:

- 정상 CSV의 당일 행은 `count_orders_for_date()`로 복구되며 기존 회귀 테스트가 한도 도달 후 주문 차단을 확인한다.
- 집계는 상태나 주문 ID를 구분하지 않고 해당 날짜의 모든 행을 센다. 따라서 제출 성공뿐 아니라 `PENDING_SUBMISSION`, rejected, timeout 행도 보수적으로 한도를 소비한다.
- 누락 파일을 사용한 독립 재현에서 `load_order_history()`가 빈 DataFrame을 반환했고 집계 결과는 0이었다.
- `order_date` 열이 없는 손상 CSV에서도 집계 결과는 0이었다.
- 날짜는 `datetime.now()`로 계산한다. `TZ=Asia/Seoul` 재현 시 서버 날짜는 `2026-07-21`, 미국 뉴욕 날짜는 `2026-07-20`으로 달랐다.
- 두 실행이 같은 빈 이력을 읽은 뒤 동시에 예약하도록 재현했을 때 양쪽 모두 성공을 반환했지만 최종 CSV에는 1행만 남았다.

Remaining risk:

- 이력 읽기 실패·파일 누락·필수 열 누락을 0건으로 간주하는 fail-open 동작이 남아 있다. **HIGH**.
- 미국 동부시간이 아닌 서버 로컬 날짜를 사용하므로 거래일 경계에서 일일 한도를 우회할 수 있다. **HIGH**.
- 파일 잠금이나 트랜잭션이 없어 여러 프로세스가 동일한 기존 count를 보고 동시에 주문할 수 있다. **HIGH**.
- 실제 broker order ID가 없어 동일 주문 identity 단위의 중복 제거 또는 중복 집계 방지가 불가능하다.

### [CODEX-003]

Status: **PARTIALLY_RESOLVED**

Evidence:

- 단일 프로세스 정상 경로는 후보 선택 → `PENDING_SUBMISSION` 저장 → broker 호출 → `SUBMITTED`/`REJECTED`/`SUBMISSION_FAILED` 저장 → Slack 알림 순서다.
- 예약 저장 함수가 `False`를 반환하면 `RuntimeError`가 발생하고 broker 호출은 0회다.
- timeout은 `SUBMISSION_FAILED`, HTTP rejected는 `REJECTED`로 남으며 성공으로 기록되지 않는다.
- broker 호출 직후 프로세스가 종료되면 디스크의 `PENDING_SUBMISSION` 행이 symbol/date 중복 검사에 걸리므로 같은 symbol의 재주문은 차단된다.
- 오래된 PENDING을 자동 재주문하는 경로는 없고, reconciliation도 없어 영구 잔류할 수 있다.
- 현재 경로는 매수 진입 전용이라 청산 주문을 직접 차단하지는 않지만, 중복 키는 실제 주문 ID가 아닌 symbol/date뿐이다.

Remaining risk:

- `to_csv()`가 대상 파일에 직접 덮어쓰며 임시 파일+rename, fsync, 파일 잠금이 없다. 쓰기 중 장애 시 기존 예약까지 손상될 수 있다. **HIGH**.
- 동시 예약 재현에서 두 호출이 모두 성공했지만 한 행이 유실됐다. 이 race는 중복 주문과 일일 한도 우회를 허용한다. **HIGH**.
- `update_order_status()`의 저장 실패 반환값을 호출자가 확인하지 않는다. 기존 PENDING이 온전히 남으면 보수적으로 차단되지만, 비원자적 쓰기 도중 실패하면 이 보장은 없다.
- 주문 identity와 broker order ID가 없어서 동일 symbol의 독립 주문 구분, broker reconciliation, 상태 전이 검증이 불가능하다.

### [CODEX-004]

Status: **PARTIALLY_RESOLVED**

Evidence:

- 저장소 루트에서 `venv/bin/pytest -q`와 `venv/bin/python -m pytest -q`는 각각 70 passed로 성공했다.
- `bash --noprofile --norc` 환경에서도 두 명령 모두 70 passed로 성공했다.
- 저장소 상위 디렉터리에서 `us-stock-trading/venv/bin/pytest -q`는 프로젝트 `pytest.ini`를 적용하지 않고 다른 저장소와 루트 스크립트까지 수집하여 98개 collection error를 냈다.
- 상위 디렉터리에서 대상 디렉터리를 명시한 `us-stock-trading/venv/bin/pytest -q us-stock-trading`도 프로젝트 루트의 네트워크성 `test_*.py`를 수집해 3개 collection error를 냈다.
- 저장소 루트에서 import된 운영 모듈은 현재 저장소 파일이었으며 다른 동명 모듈을 가져온 증거는 없었다.

Remaining risk:

- `pythonpath = .`은 저장소 루트에서의 import 문제만 해결한다. 실행 위치 독립성은 확보되지 않았다.
- 상위 위치에서 잘못 실행하면 `test_alpaca_account.py`, `test_paper_order.py`, `slack_test.py`, yfinance 스크립트가 수집될 수 있다.

## New findings

### [CODEX-005] HIGH — 테스트 진입점이 저장소 외부 실행에서 실제 네트워크성 스크립트를 수집함

Status: **PARTIALLY_RESOLVED**

Evidence:

- 상위 디렉터리 실행에서 `indicator_test.py`, `ma_test.py`, `test_stock.py`가 Yahoo endpoint 연결을 실제로 시도했고 DNS 오류가 발생했다.
- `slack_test.py`, `test_alpaca_account.py`, `test_paper_order.py`도 import 시 요청 코드를 실행했다. 이번 환경에서는 URL이 `None`이라 전송 전에 실패했지만, 운영 환경변수가 있으면 실제 Slack/Alpaca 요청 가능성이 있다.
- 이는 “테스트에서 실제 Alpaca 또는 Slack API를 호출하지 않는다”는 Project Constitution과 충돌한다.

Remaining risk:

- 운영 credential/URL이 설정된 shell에서 상위 디렉터리 pytest 실행 시 외부 호출 또는 Paper 주문 시도가 발생할 수 있다. **HIGH**.

### [CODEX-006] HIGH — 부분 체결 및 broker reconciliation 미구현

Status: **PARTIALLY_RESOLVED**

Evidence:

- `paper_strategy_order.py`는 HTTP 200/201을 `SUBMITTED`로만 기록하며 broker order ID, 실제 fill status, filled quantity를 저장하지 않는다.
- `order_monitor.py`와 주문 이력/포지션 생명주기 사이의 연결이 없다.
- `SCALPING_V1_ROADMAP.md`와 `VALIDATION_PACKAGE.md`에는 미구현 사실이 기록되어 있다.

Remaining risk:

- Phase 1의 명시적 승인 기준인 부분 체결 테스트를 충족하지 못한다. 구조 구현은 Phase 5로 이관할 수 있고 Phase 2 관심종목 선별 로직과 기술적으로는 독립적이지만, 현재 Phase 1을 완료 처리할 수는 없다.

## Executed tests

- 저장소 루트: `venv/bin/pytest -q` → **70 passed, 2 warnings**
- 저장소 루트: `venv/bin/python -m pytest -q` → **70 passed, 2 warnings**
- 집중 테스트: `venv/bin/pytest -q tests/test_broker_safety.py tests/test_paper_order_execution.py` → **27 passed, 1 warning**
- 깨끗한 bash: `pytest -q` → **70 passed, 2 warnings**
- 깨끗한 bash: `python -m pytest -q` → **70 passed, 2 warnings**
- 저장소 상위: `us-stock-trading/venv/bin/pytest -q` → **collection 실패, 98 errors, 네트워크 시도 확인**
- 저장소 상위+대상 지정: `us-stock-trading/venv/bin/pytest -q us-stock-trading` → **collection 실패, 3 errors, 네트워크 시도 확인**
- 추가 독립 재현: mode/endpoint 행렬, 누락·손상 이력, 서울/뉴욕 날짜 경계, 동시 CSV 예약 lost update.

## Warnings review

- urllib3 `NotOpenSSLWarning`: 현재 Python이 LibreSSL 2.8.3으로 빌드되어 urllib3 v2의 지원 조건과 맞지 않는 환경 경고다. 테스트 mock 경로의 주문 안전성 실패는 아니지만 실제 HTTPS 신뢰성과 향후 호환성 위험이므로 개발환경 개선 대상으로 남긴다.
- scanner unknown field `RuntimeWarning`: 의도된 unsupported field skip 테스트에서 발생하며 주문 안전성과 무관하다.
- 상위 실행의 `PytestCacheWarning`은 상위 디렉터리 쓰기 권한 제한 때문이며 제품 안전 Finding은 아니다.

## Network safety

- 정상적인 저장소 루트 70개 테스트와 27개 집중 테스트는 broker/session double 및 Slack monkeypatch를 사용했고 실제 Alpaca/Slack 호출 증거가 없었다.
- 저장소 상위 실행에서는 Yahoo 네트워크 요청이 실제로 시도됐다.
- Alpaca/Slack 스크립트도 수집됐지만 이 환경에는 URL이 없어 요청 준비 단계에서 실패했다. 따라서 모든 실행 위치에서 네트워크 안전하다는 주장은 거부한다.
- `broker_config.py`는 import 시 `load_dotenv()`를 호출한다. 현재 저장소에는 `.env`가 없었지만 테스트가 운영 환경변수로부터 완전히 격리되어 있지는 않다.

## Operational file safety

- 기존 `order_history.csv`의 검증 전후 SHA-256은 모두 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 크기 31 bytes, mtime 동일로 실제 파일 변경은 없었다.
- 독립 CSV 재현은 임시 디렉터리에서만 수행했다.
- 저장소 `.env`는 존재하지 않았고 운영 서버/설정에는 접근하지 않았다.
- 결과 보고서 외 운영 경로 파일은 생성하거나 수정하지 않았다.

## Unverified areas

- 실제 Alpaca Paper 계정 E2E는 외부 호출 금지 원칙에 따라 수행하지 않았다.
- 프로세스 kill/power-loss 수준의 파일 내구성은 실제 장애 주입 없이 코드 구조와 동시 쓰기 재현으로 판정했다.
- 운영 서버 timezone, cron 중복 실행 가능성, 실제 배포 환경변수 값은 확인하지 않았다.
- 부분 체결, 취소, fill reconciliation은 구현 자체가 없어 검증할 수 없었다.

## Phase 1 decision

**FAIL** — `KEEP_IN_PROGRESS`보다 강한 재수정 필요 상태다. CODEX-002와 CODEX-003의 HIGH 위험 및 신규 HIGH Finding이 남아 있으므로 `VALIDATE`할 수 없다.

## Phase 2 recommendation

**DO_NOT_PROCEED**

부분 체결 기능 자체는 Phase 2 관심종목 선별과 기술적으로 독립적이지만, 기존 HIGH가 실제로 해결되지 않았고 테스트 진입점의 네트워크 위험도 확인됐으므로 현재 게이트 정책상 다음 Phase 진행 조건을 충족하지 않는다.
