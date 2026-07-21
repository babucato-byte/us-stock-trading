# CODEX_REVIEW

Review target: Phase 2 초단타 관심종목 선별 엔진 독립 재검증

Commits: `4a96883` (구현), `ee079e6` (검증 패키지 및 문서)

Phase: Phase 2 — 관심종목 선별 엔진

Date: 2026-07-22

Overall verdict: **FAIL**

구현 코드 변경 없이 코드 추적, 격리 재현, 전체 회귀 테스트를 수행했다. 보고된 34개 신규 테스트와 전체 183개 테스트는 통과했으나, Phase 2의 명시 원칙인 “불명확하면 포함하지 않는다”를 위반하는 신규 HIGH Finding 2건이 확인됐다. 비유한 수치가 실제 후보로 포함되고, 실제 데이터 제공자는 데이터 최신성을 검증하지 않아 stale gate가 운영 경로에서 작동하지 않는다. 따라서 Phase 2를 `VALIDATED`로 승격하거나 Phase 3으로 진행할 수 없다.

## Previous Phase 1 findings verification

### [CODEX-001~009]

Status: **RESOLVED (NO REGRESSION OBSERVED)**

Evidence:

- Phase 2는 주문, broker, reconciliation 모듈을 변경하지 않았다.
- 전체 기존 회귀 테스트 149건을 포함한 183건이 통과했다.
- `order_history.csv`의 검증 전후 SHA-256이 동일했다.

Remaining risk: Phase 1에서 문서화한 부분체결 생명주기 및 교차 파일 트랜잭션 조건은 그대로 Phase 5 범위에 남는다.

## New findings

### [CODEX-010] HIGH — 비유한 시장 데이터가 eligibility gate를 통과함

Status: **UNRESOLVED**

Evidence:

- `compute_features()`는 `None`과 부호만 검사하고 `math.isfinite()` 또는 동등한 검사를 하지 않는다.
- Python에서 NaN과 임계값의 `<`, `>`, `<=` 비교는 모두 False이므로 `eligibility.py`의 범위 검사를 우회한다.
- `price`, `previous_close`, `average_volume`, `atr` 각각을 `float("nan")`으로 주입한 격리 재현에서 모두 `selected=1, rejected=0`이었다.
- scorer가 NaN/Infinity를 0으로 클램프하는 테스트는 존재하지만, 그보다 앞선 피처/선별 단계의 fail-closed 여부는 테스트하지 않는다. 점수 클램프는 부적격 후보의 포함을 막지 못한다.

Remaining risk: 잘못되거나 불명확한 가격·거래량·변동성 데이터가 ACTIVE 관심종목으로 저장되어 Phase 3 입력으로 전달될 수 있다. 모든 필수 수치와 계산 결과에 대한 finite 검증 및 회귀 테스트가 필요하다.

### [CODEX-011] HIGH — 운영 provider의 stale-data gate가 실질적으로 비활성임

Status: **UNRESOLVED**

Evidence:

- `SymbolSnapshot.data_is_stale` 기본값은 `False`이고 `YFinanceMarketDataProvider.get_symbol_snapshot()`은 이를 계산하거나 설정하지 않는다.
- provider는 daily history의 마지막 행을 무조건 현재 `price`/`current_volume`으로 사용하며 마지막 bar timestamp, 거래일, 세션 적합성을 검증하지 않는다.
- 따라서 휴장일, 공급자 지연, 이전 거래일 데이터도 `STALE_DATA`로 거부할 근거가 없다. stale 차단 테스트는 Fake provider가 직접 `data_is_stale=True`를 넣는 경우만 검증한다.
- 실제 Yahoo 호출은 안전 원칙상 수행하지 않았으나, 이 결함은 운영 코드 경로의 정적 추적으로 확정된다.

Remaining risk: 오래된 일봉을 당일 데이터로 오인해 후보를 생성할 수 있다. provider가 ET 기준 bar timestamp와 기대 거래일/세션을 검증하고, 불명확한 경우 snapshot을 거부하도록 해야 한다.

### [CODEX-012] MEDIUM — 거래일 및 허용 세션 gate가 파이프라인에 없음

Status: **UNRESOLVED**

Evidence:

- `run_scan_cycle()`은 `get_us_market_session()` 결과를 CSV 필드로만 사용하며 `market_guard.is_us_trading_day()`를 호출하지 않는다.
- 2026-06-14 일요일 10:00 ET에 정상 fixture를 주입한 격리 재현에서 `selected=1`이었다.
- `VALIDATION_PACKAGE.md`는 `market_guard.is_us_trading_day`를 재사용한다고 기술하지만 실제 import/call은 없다.

Remaining risk: 스케줄 오작동 또는 수동 실행 시 휴장일/부적절한 세션에 잘못된 관심종목 상태를 만들 수 있다. 허용 세션 정책과 휴장일 fail-closed 테스트가 필요하다.

### [CODEX-013] MEDIUM — watchlist 저장 실패가 성공 결과로 반환됨

Status: **UNRESOLVED**

Evidence:

- `repository.save_watchlist_cycle()`은 손상 파일 등에서 `False`를 반환할 수 있으나 `run_scan_cycle()`은 반환값을 확인하지 않는다.
- 저장 함수를 `False`로 고정한 격리 재현에서 `selected=1`이 그대로 반환됐다.
- 호출자는 반환 객체만으로 영속화 성공과 실패를 구분할 수 없다.

Remaining risk: 다음 단계가 메모리상의 selected 결과를 성공으로 취급하거나 운영자가 정상 갱신으로 오인할 수 있다. 저장 실패를 예외 또는 명시적 실패 상태로 전파하는 테스트가 필요하다.

### [CODEX-014] MEDIUM — lifecycle이 문서화된 상태 머신과 일치하지 않고 손상 timestamp를 활성 상태로 보존함

Status: **UNRESOLVED**

Evidence:

- 신규 선택 행은 처음부터 `ACTIVE`로 생성되어 문서의 `NEW→ACTIVE→COOLING→EXPIRED`에서 `NEW` 전이가 구현되지 않았다.
- `_apply_expiry()`는 `detected_at`이 파싱 불가능하거나 timezone-naive이면 상태를 그대로 둔다. 즉, 손상된 ACTIVE 행은 TTL/expire를 우회해 계속 활성 상태로 남을 수 있다.
- `load_watchlist()`는 컬럼 존재만 검사하며 timestamp 및 status 값의 의미 유효성을 검증하지 않는다.

Remaining risk: 오래되거나 손상된 관심종목이 Phase 3 소비 대상에 남을 수 있다. 상태 머신을 실제 요구에 맞게 구현하거나 문서를 정정하고, 손상 lifecycle 필드는 fail-closed 처리해야 한다.

### [CODEX-015] LOW — YFinance 거래량 및 premarket 계산 경계가 부정확함

Status: **UNRESOLVED**

Evidence:

- 평균 거래량은 마지막 행을 포함한 `tail(20).mean()`으로 계산되어 현재 거래일의 부분 거래량이 분모에 섞인다.
- premarket 필터는 `hour < 9`라서 09:00~09:29 ET를 제외하며 04:00 시작 경계도 명시하지 않는다.
- 실제 데이터 source/timezone별 E2E 테스트가 없다.

Remaining risk: 상대거래량과 premarket_volume이 체계적으로 왜곡될 수 있다. Phase 2 임계값 검증 전에 계산 창과 세션 경계를 명시해야 한다.

## Executed tests

- 저장소 루트 `venv/bin/pytest -q tests/test_scalping_watchlist.py` → **34 passed**
- 저장소 루트 `venv/bin/python -m pytest -q tests/test_scalping_watchlist.py` → **34 passed**
- 저장소 루트 `venv/bin/pytest -q` → **183 passed, 2 warnings**
- 저장소 상위 `us-stock-trading/venv/bin/python -m pytest -q us-stock-trading` → **183 passed, 2 warnings**
- 격리 수동 재현: NaN 필수 수치 5종, 주말 실행, 저장 실패 반환 경로

보고된 테스트 수는 재현됐다. 다만 34개 신규 테스트는 CODEX-010~014의 핵심 실패 경로를 포함하지 않는다.

## Warnings review

- urllib3 `NotOpenSSLWarning`: Python이 LibreSSL 2.8.3으로 빌드된 환경 호환성 경고다. 이번 mock 기반 안전성 실패 원인은 아니다.
- scanner unknown-field `RuntimeWarning`: 기존 fail-open 룰 엔진의 의도된 회귀 테스트 경고이며 Phase 2 전용 eligibility 경로와 직접 연결되지 않는다.

안전성과 직접 관련된 신규 warning은 없었다.

## Network safety

- 신규 테스트는 `FakeMarketDataProvider`만 사용했으며 실제 Yahoo/Alpaca/Slack 호출 증거가 없었다.
- 실제 `YFinanceMarketDataProvider` E2E는 외부 호출 금지 원칙에 따라 실행하지 않았다.
- Phase 2 코드는 cron/systemd에 연결되지 않아 검증 중 운영 실행 경로가 활성화되지 않았다.

## Operational file safety

- `order_history.csv` 검증 전후 SHA-256: `153feb31c2539c19cd60f63e3f90d0d0f734ba7a209ed1800af7c0070a0a91c7` (불변)
- `universe.csv` 검증 후 SHA-256: `9fdaf3ac0ba7d94e24b6276fc603709a0c79c6842cf8143b8a242acdd16188b3`
- 저장소 루트에 `scalping_watchlist.csv`, `scalping_repeat_state.csv`, 관련 lock 파일이 생성되지 않았다.
- 재현 테스트의 모든 쓰기는 임시 디렉터리에 격리했다.
- `.env` 운영 파일은 존재하지 않았고 참조하지 않았다.

## Document consistency

- 34개 신규/183개 전체 테스트 및 2개 warning 주장은 실제 결과와 일치한다.
- `market_guard.is_us_trading_day` 재사용 주장은 실제 코드와 불일치한다.
- lifecycle의 `NEW→ACTIVE` 주장은 신규 행이 즉시 ACTIVE로 생성되는 실제 코드와 불일치한다.
- `YFinanceMarketDataProvider` 미검증 위험은 문서에 기록됐지만, stale 판정 자체가 구현되지 않은 점은 단순 E2E 미검증보다 큰 구조적 결함이다.

## Unverified areas

- 실제 Yahoo market data의 timestamp/timezone/부분 일봉 동작은 네트워크를 사용하지 않아 실행 검증하지 않았다.
- 실제 스케줄러 주기와 장전/정규장/장후 운영 편입은 아직 구현되지 않아 검증하지 않았다.
- 임계값과 가중치의 거래 성과는 백테스트되지 않았다.
- `spread_estimate`와 `smart_money_score`는 각각 `NOT_AVAILABLE`/`NOT_EVALUATED`이며 완료된 기능으로 판정하지 않는다.
- 두 Phase 2 CSV 사이의 crash consistency는 검증하지 않았다.

## Phase 2 decision

**FAIL**

신규 HIGH Finding CODEX-010과 CODEX-011이 남아 있으므로 Phase 2는 `IMPLEMENTED` 상태를 유지해야 하며 `VALIDATED`로 변경할 수 없다.

## Phase 3 recommendation

**DO_NOT_PROCEED**

Phase 3은 Phase 2 관심종목을 1분봉 감시 입력으로 소비하므로 현재 결함과 독립적이지 않다. 최소한 CODEX-010~011을 해결하고 CODEX-012~014의 fail-closed 경로까지 회귀 테스트로 고정한 뒤 재검증해야 한다.
