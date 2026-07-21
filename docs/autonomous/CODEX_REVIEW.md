# CODEX_REVIEW

Review target: Phase 1 CODEX-007~009 수정 최종 독립 재검증

Commits: `05757fe`, `0c2dab4`, `16a1ee4`, `56e11be` (이전 검증 기록 `eef3a13` 포함)

Phase: Phase 1 — 주문 안전성과 실행 경로 검증

Date: 2026-07-21

Overall verdict: **PASS_WITH_CONDITIONS**

CODEX-007(HIGH), CODEX-008(HIGH), CODEX-009(MEDIUM)의 재현 경로가 모두 차단됐다. 이전 CODEX-001~006의 해결 상태에도 회귀가 없었다. 미해결 CRITICAL/HIGH/MEDIUM Finding은 없다. 다만 부분체결의 포지션 생명주기 통합과 두 CSV 간 교차 파일 트랜잭션 정책은 Phase 5 전 결정 조건으로 남는다.

## Previous findings

### [CODEX-001]

Status: **RESOLVED**

Evidence:

- account/positions/orders/assets GET과 order POST는 모두 `AlpacaBroker`의 공식 Paper endpoint 검사를 통과해야 한다.
- invalid/live/임의 endpoint 및 client 생성 후 config 변조 시 DummySession 호출 0회를 테스트로 확인했다.
- `universe_builder.py`의 직접 Alpaca GET 경로도 CODEX-009 수정으로 제거됐다.

Remaining risk: Live URL 상수는 부정 테스트와 차단 상태 표현을 위해 남지만 기본/fallback 네트워크 endpoint로 사용되지 않는다.

### [CODEX-002]

Status: **RESOLVED**

Evidence:

- 누락·손상 이력은 fail-closed이며 거래일은 `America/New_York` 기준이다.
- CODEX-007 수정으로 모든 `order_date` 행이 정확한 `YYYY-MM-DD`인지 엄격 검증된다. 파싱 가능한 비정규 값도 더 이상 집계를 우회하지 못한다.
- 잠금 안에서 최신 이력을 다시 읽고 일일한도를 검사한다.

Remaining risk: 없음.

### [CODEX-003]

Status: **RESOLVED**

Evidence:

- `order_history.csv`는 `fcntl.flock` 하에서 최신 상태를 재조회하고 temp write → flush → fsync → `os.replace`로 저장한다.
- 동일 symbol, 서로 다른 symbol, 마지막 한도 슬롯의 threading/multiprocessing 경쟁 테스트가 통과했다.

Remaining risk: `os.replace` 뒤 부모 디렉터리 fsync가 없어 전원 손실 수준의 rename 내구성은 완전 보장되지 않는다. 현재 주문 경쟁 우회는 재현되지 않았으며 LOW 수준이다.

### [CODEX-004]

Status: **RESOLVED**

Evidence: 저장소 루트 및 상위 디렉터리에서 `pytest`와 `python -m pytest` 네 조합이 모두 동일한 149개 테스트를 수집·통과했다.

Remaining risk: 없음.

### [CODEX-005]

Status: **RESOLVED**

Evidence: 상위 디렉터리 실행에서도 root ad-hoc 네트워크 스크립트가 수집되지 않았고 공식 테스트만 실행됐다.

Remaining risk: `collect_ignore`가 명시적 파일 목록이므로 신규 root 스크립트 추가 시 목록 유지보수가 필요하다(LOW).

### [CODEX-006]

Status: **RESOLVED**

Evidence:

- 고유 `client_order_id` 생성·broker 전달·재시작 조회·부분체결 구분·terminal 상태 매핑·자동 재주문 금지가 유지된다.
- CODEX-008 수정으로 reconciliation 저장 실패가 주문 전에 전파되고, 즉시 history/reconciliation 상태가 같은 결과를 사용한다.
- 상태와 filled quantity는 단조 병합되고 동시 갱신 lost update가 제거됐다.

Remaining risk: 부분체결을 손절·익절·강제청산 포지션 상태로 완전히 연결하는 것은 아직 Phase 5 범위다.

### [CODEX-007]

Status: **RESOLVED**

Evidence:

- `validate_order_date_str()`는 string 타입, `^\d{4}-\d{2}-\d{2}$`, 실제 달력 유효성, `strptime/strftime` 왕복 일치를 모두 요구한다.
- datetime suffix, compact date, zero-padding 누락, 공백, timezone, 잘못된 날짜, 숫자/None/NaN을 차단한다.
- 한 행이라도 비정규이면 `load_order_history()`가 `CORRUPTED_HISTORY`로 전체 신규 주문을 차단한다.
- 진단 함수는 문제 행을 보고하지만 파일을 수정하지 않는다.
- CODEX-007 관련 신규 테스트와 전체 suite가 통과했다.

Remaining risk: 기존 운영 CSV에 비정규 날짜가 있으면 배포 후 신규 주문이 의도적으로 차단되므로 배포 전 진단이 필요하다. 안전 측 실패다.

### [CODEX-008]

Status: **RESOLVED**

Evidence:

- reconciliation 전용 `fcntl.flock`이 lock → 최신 재조회 → monotonic merge → atomic replace 전체 read-modify-write 구간을 직렬화한다.
- 손상 reconciliation은 `ReconciliationUnavailable`로 fail-closed하며 자동 초기화되지 않는다.
- pending reconciliation 저장 실패는 `RuntimeError`로 전파되어 broker 제출을 차단한다.
- 상태 후퇴, `UNKNOWN`의 FILLED 덮어쓰기, filled quantity 감소, 평균가격 소거가 병합 규칙으로 차단된다.
- 즉시 부분체결 응답에서 history/reconciliation이 모두 `PARTIALLY_FILLED`로 기록된다.
- 실제 multiprocessing에서 동일 주문의 partial/filled 경쟁은 최종 FILLED와 최대 filled quantity를 보존했고, 서로 다른 주문의 동시 갱신도 두 행을 모두 보존했다.
- threading+multiprocessing 선택 테스트 6건을 5회 실행해 모두 통과했다.

Remaining risk:

- `order_history.csv`와 `order_reconciliation.csv`는 각각 원자적이지만 두 파일을 하나의 트랜잭션으로 묶지는 않는다. history 예약 후 reconciliation 기록 전 SIGKILL이면 orphan reservation이 남을 수 있다.
- 이 상태는 중복 주문과 일일한도에 사용되는 history에 예약을 남기므로 fail-open이나 재주문을 만들지 않는다. 현재 Phase 1/2 기준으로 MEDIUM 이하의 운영 정합성 위험이며, Phase 5가 reconciliation을 청산 판단에 사용하기 전 저장소 정책 결정이 필요하다.

### [CODEX-009]

Status: **RESOLVED**

Evidence:

- `universe_builder.py`는 import 부수효과 없이 함수화됐고 `AlpacaBroker.get_assets()`만 사용한다.
- `get_assets()`는 다른 broker GET과 동일한 `_request()` 안전 게이트를 거친다.
- Live, 임의 도메인, HTTP downgrade, 유사 hostname, 비표준 port, path/query/userinfo 조작, 빈 URL, invalid mode 및 생성 후 config 변조에서 session 호출 0회를 확인했다.
- 정상 Paper endpoint만 1회 호출하며 활성·거래가능·미국주식 필터 결과가 유지됐다.
- 운영 진입점은 `if __name__ == "__main__": build_universe()`로 유지돼 runner subprocess 방식과 호환된다.

Remaining risk: 없음.

## New findings

없음.

## Executed tests

- 저장소 루트 `venv/bin/pytest -q` → **149 passed, 2 warnings**
- 저장소 루트 `venv/bin/python -m pytest -q` → **149 passed, 2 warnings**
- 저장소 상위 `pytest us-stock-trading -q` → **149 passed, 2 warnings**
- 저장소 상위 `python -m pytest us-stock-trading -q` → **149 passed, 2 warnings**
- 집중 테스트(`test_broker_safety.py`, `test_paper_order_execution.py`, `test_universe_builder.py`) → **106 passed, 1 warning**
- concurrency 선택 테스트 → **6 passed**, 5회 반복 모두 통과

Warnings:

- urllib3 `NotOpenSSLWarning`: LibreSSL 2.8.3 환경 호환성 경고이며 이번 mock 안전 검증 실패는 아니다.
- scanner unknown-field `RuntimeWarning`: 의도된 unsupported-field skip 테스트 경고로 주문 안전성과 무관하다.

## Concurrency verification

- history와 reconciliation 각각에 독립 lock이 존재하고, 각 파일 내부 read-modify-write는 잠금 범위에 포함된다.
- 동일 reconciliation row의 부분/완전체결 경쟁은 단조 병합되며, 다른 row의 병렬 갱신도 lost update 없이 보존됐다.
- lock timeout 및 저장 실패는 파일을 보존하고 주문 또는 해당 상태 갱신을 fail-closed 처리한다.

## Reconciliation verification

- client ID 기반 재조회, 상태 매핑, 부분체결 수량, remaining quantity, 평균가격, 멱등성, 상태 후퇴 방지를 테스트로 확인했다.
- unknown 상태는 비종결 상태로 남아 다음 실행에서 다시 조회된다.
- 두 CSV 간 단일 트랜잭션 부재는 남지만 현재 안전 게이트를 우회하지 않는다.

## Network safety

- 공식 테스트에서 실제 Alpaca/Slack/Yahoo 호출 증거는 없었다.
- Alpaca 운영 경로는 broker 공통 Paper endpoint 게이트를 사용한다.
- 저장소 grep상 broker 외 Alpaca 직접 호출은 pytest `collect_ignore` 대상인 root scratch 파일만 남아 있다.

## Operational file safety

- 검증 전후 `order_history.csv`: SHA-256 `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7`, 31 bytes, mtime `1784558966`으로 동일했다.
- 검증 전후 `universe.csv`: SHA-256 `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`, 833518 bytes, mtime `1784558966`으로 동일했다.
- `order_reconciliation.csv`, history/reconciliation lock, `.env`는 검증 전후 모두 존재하지 않았다.
- 모든 테스트 및 동시성 파일은 임시 경로에 격리됐다.

## Document consistency

- 테스트 수 149, 집중 테스트 106, 동시성 반복 5/5와 커밋 해시는 실제 결과와 일치한다.
- Phase 1은 부분체결의 포지션 상태 통합이 남아 있어 `IN_PROGRESS`로 정확히 기록됐다.
- CODEX-001~009 해결 주장과 실제 코드·테스트 결과가 일치한다.
- SQLite/교차 파일 트랜잭션 판단은 `DECISION_LOG.md`에 `NEEDS_USER_DECISION`으로 명시돼 있다.

## Unverified areas

- 실제 Alpaca Paper 계정 및 실제 부분체결 E2E는 외부 호출 금지 원칙에 따라 실행하지 않았다.
- Ubuntu에서의 flock 및 실제 cron 환경은 이 macOS 검증에서 실행하지 않았다.
- SIGKILL/전원 손실 순간의 교차 파일 정합성은 실제 장애 주입하지 않았다.
- Phase 5 포지션 생명주기·자동 청산은 아직 구현되지 않았다.

## Phase 1 decision

**KEEP_IN_PROGRESS**

CODEX Finding 기준으로는 통과했지만 Phase 1 자체 승인 기준인 부분체결의 포지션 상태 반영이 Phase 5 선행 조건으로 남아 있어 `VALIDATE`하지 않는다.

## Phase 2 recommendation

**PROCEED**

미해결 CRITICAL/HIGH/MEDIUM Finding이 없고 남은 조건은 Phase 2 관심종목 선별과 독립적이다. Phase 2 구현은 진행 가능하다. 단, Phase 5 착수 전 두 CSV를 유지할지 SQLite 등 단일 트랜잭션 저장소로 통합할지 사용자 결정을 받아야 한다.
