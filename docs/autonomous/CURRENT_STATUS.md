# CURRENT_STATUS

마지막 갱신: 2026-07-25

## 현재 Phase
Stage 8 — 전략 선택 엔진(`strategy_selection/`) `IMPLEMENTED`, Claude 자체 테스트 통과, Codex
검증 전 — 사용자 지시에 따라 Stage 3~10을 Codex 중간 검증 없이 연속 구현 중. Stage 7 — 전략 평가
엔진(백테스트/리플레이, `backtest/`) `IMPLEMENTED`, 변경 없음. Stage 6 — 사용자/YouTube 전략 자료
구조화(`strategy_sources/`) `IMPLEMENTED`, 변경 없음. Stage 5 — 거래 상태 저장소(`state_store/`,
SQLite 병행 인프라) `IMPLEMENTED`, 변경 없음. Phase 5 — 포지션 생명주기 및 자동 청산 (Stage 4,
`IMPLEMENTED`, 변경 없음). Phase 4 — VWAP 마이크로 풀백 전략 엔진 (Stage 3, `IMPLEMENTED`, 변경
없음). Phase 2 — 초단타 관심종목 선별 엔진 (`IMPLEMENTED`, CODEX-010~015 수정 완료, Codex 재검증
대기, 변경 없음).

Phase 1 최종 판정(유지): **Phase 1A(주문 진입 안전성) = VALIDATED**, **Phase 1B(부분체결·포지션
생명주기) = Phase 5로 이관 완료, Phase 5 자체는 `IMPLEMENTED`**.

Phase 3(1분봉 실시간 수집/폴링 인프라)은 이번 사이클에서도 착수하지 않음 — Stage 3/4는 전략
플러그인·포지션 생명주기 로직 자체만 구현했고, 구성된 pandas DataFrame과 fake broker를 입력으로
받아 테스트한다. 라이브 1분봉 폴링/실브로커 연동은 여전히 범위 외.

## Stage 8 — 전략 선택 엔진(`strategy_selection/`) 구현 완료 (2026-07-26)
`models.py`(`SelectionState`/`SelectionInput`/`SelectionFactors`/`SelectionResult`),
`scoring.py`(요소별 순수 함수 + `COMPOSITE_WEIGHTS`), `engine.py`(`select_strategy()`).

- 해석 결정(`DECISION_LOG.md` 결정 1): 지시서의 "ACTIVE 전략만 평가"를 문자 그대로
  `strategy.status==ACTIVE`로 해석하면 후보 풀이 항상 최대 1개(레지스트리의 ACTIVE 1개 제약)라
  "선택"이 성립하지 않으므로, "검토 단계(`REVIEWED`) 이상으로 진행된 전략만 평가"로 재해석했다.
  `REJECTED`/`PAUSED` → `DISABLED`, `COLLECTED`/`STRUCTURED`(백테스트 데이터 자체가 없음) →
  `INSUFFICIENT_DATA`로 자연스럽게 귀결.
- 자격 게이트: 백테스트 결과 없음/`INSUFFICIENT_DATA`/거래 10건 미만(`MIN_TRADES_FOR_SCORING`,
  ASSUMPTION) → `INSUFFICIENT_DATA`. 선호 시장상태(`PREFERRED_MARKET_STATES` 테이블, 현재는
  `VWAP_MICRO_PULLBACK_MOMENTUM_V1` → `{"regular"}`만 등록, `PROJECT_CONSTITUTION.md`와 일치)와
  불일치 시 `MARKET_MISMATCH`.
- 점수: `backtest_performance`/`paper_performance`(승률+평균R+PF 평균, `backtest.metrics.
  compute_metrics()`와 동일 shape 재사용)/`sample_size`/`mdd`/`slippage_sensitivity`(
  `backtest.metrics.slippage_sensitivity()` 결과에서 저-고 슬리피지 구간 기대값 유지율)/
  `market_state_fit`/`symbol_condition_fit` 7개 요소, 결측 요소는 0이 아니라 제외 후 재정규화.
  가중치·임계값 전부 결과를 보기 전에 고정(`DECISION_LOG.md` 결정 2/3, Stage 7과 동일한 원칙).
  단 하나만 `SELECTED`, 동점은 입력 순서로 결정론적 처리.
- 경계: `engine.py`는 `strategy.registry`를 import하지 않고 `ACTIVE` 등록을 호출하지 않음(Stage 7
  `backtest/compare.py`와 동일 원칙) — `SELECTED`는 추천일 뿐 실제 활성화는 별도 운영자 승인 필요.
- 신규 테스트: `tests/test_strategy_selection.py` 27건(자격 게이트 5종, 단일/다중 후보 선택,
  비활성/데이터부족 후보 단독일 때도 미선택, 동점 결정론적 처리, 후보 0명 시 미선택, 요소별 순수
  함수 단위 테스트, 가중치 합=1.0 불변식).
- 전체 회귀: **792 passed, 0 failed**(기존 765 + 신규 27). 실제 네트워크 호출 0회, 운영 CSV 변경
  0건.
- 커밋: `2094adf`.
- 잔여 위험: `PREFERRED_MARKET_STATES` 테이블에 신규 전략 추가 시 수동으로 채워야 함(비어있으면
  시장상태 게이트가 적용되지 않음 — 자격이 아니라 "아직 미문서화"로 해석). 가중치·임계값은 전부
  ASSUMPTION, 실제 Paper 성과 데이터 축적 후 재검토 필요.

## Stage 7 — 전략 평가 엔진(`backtest/`, 백테스트/리플레이) 구현 완료 (2026-07-26)
착수 직전 사용자가 명시한 10개 제약을 그대로 구현: (1) 결과를 보고 임계값을 조정하지 않음 —
`backtest/config.py`의 모든 비용/정책 가정은 어떤 전략도 백테스트하기 전에 고정, 근거는
`DECISION_LOG.md` Stage 7 섹션. (2) 동일봉 손절/목표 충돌은 `STOP_FIRST`(보수적)만 지원. (3)
spread/slippage/수수료/진입지연 4개 비용을 `CostBreakdown`으로 거래마다 분리 기록. (4) look-ahead
금지 — `engine.py`는 항상 `bars.iloc[:i+1]`만 전략에 전달, 데이터가 소진돼 체결을 시뮬레이션할 수
없는 신호는 거래로 기록하지 않음. (5) 프리마켓/정규장 분리 — 진입은
`get_us_market_session(bar_time)=="regular"`일 때만(`paper_strategy_order.py`의 실제 운영 게이트와
동일 조건), 프리마켓 봉은 지표 워밍업에만 사용. (6) 모든 체결이 봉 거래량의
`max_fill_fraction_of_bar_volume`으로 캡핑, 미체결 잔량은 다음 봉으로 이월(체결을 지어내지 않음).
(7) `compute_metrics_with_best_trade_removed()`가 `all_trades`/`best_trade_removed`를 함께 반환 —
후자가 전자를 대체하지 않음. (8) `bars` < `min_bars_required`(기본 500)면
`status=INSUFFICIENT_DATA`, 지표 계산 자체를 하지 않음, `compare.py`도 이를 플레이스홀더 점수 없이
그대로 통과. (9) `backtest/compare.py`는 `strategy.registry`를 import하지 않고
`activate()`/`ACTIVE` 등록을 전혀 호출하지 않음(AST 기반 테스트로 검증) — YouTube 후보 비교는
비교일 뿐 자동 승격 없음, 승격은 전적으로 Stage 8 책임. (10) 자체 테스트 29건 + 전체 회귀 통과 후
본 문서 갱신.

- 관련 파일: `backtest/config.py`(`BacktestConfig`), `backtest/models.py`(`Trade`/`ExitEvent`/
  `CostBreakdown`/`BacktestResult`), `backtest/engine.py`(리플레이 루프 — `_try_enter`/`_manage_bar`/
  `_apply_exit`/`_finalize_trade`), `backtest/metrics.py`(승률/평균R/PF/기대값/MDD/최대연속손실/
  최대수익거래제거/시간대·가격대·유동성·슬리피지민감도 분해), `backtest/compare.py`.
- 신규 테스트: `tests/test_backtest_engine.py` 29건(INSUFFICIENT_DATA 처리, look-ahead 안전 체결
  타이밍, 프리마켓 차단/정규장 허용, 동일봉 충돌 해소, 비용 분리 정확성, 거래량 캡핑+이월, 1R
  부분익절→2R 전량청산 2건 체결 이벤트, 시간손절, 장마감 강제청산, 전략 무효화, 봉 데이터 검증,
  전체 지표/비교 함수).
- 전체 회귀: **765 passed, 0 failed**(기존 736 + 신규 29). 실제 네트워크 호출 0회, 운영 CSV 변경
  0건.
- 커밋: `59958cf`(구현+테스트), `DECISION_LOG.md` Stage 7 섹션 병행 커밋.
- 잔여 위험: `nominal_qty=100`/`spread_bps=5.0`/`slippage_bps=5.0` 등 비용 가정은 실측치가 아닌
  ASSUMPTION — 실제 측정 데이터 확보 시 갱신 필요(근거와 함께 `DECISION_LOG.md`에 기록). 동일봉
  충돌 정책은 `STOP_FIRST` 한 가지만 지원.

## Stage 6 — 사용자/YouTube 전략 자료 구조화(`strategy_sources/`) 구현 완료 (2026-07-25)
신규 패키지 `strategy_sources/`(`models.py`, `repository.py`, `similarity.py`, `known_sources.py`).

- `models.py`: `StrategyClaim`(category/statement/origin/source_excerpt/confidence)과
  `StrategySource`(source_id/type/title/reference/version/validation_status/claims/
  derived_strategy_id/similar_to). `origin`이 소스가 명시적으로 말한 것(`SOURCE`, `source_excerpt`
  필수)과 수집자의 추론(`ASSUMPTION`), 소스가 다루지 않은 공백(`UNKNOWN`)을 구조적으로 분리 —
  Stage 3에서 `VWAPMicroPullbackV1.invalidate()`가 명시되지 않은 가정을 그대로 물려받을 뻔했던
  것(`DECISION_LOG.md` Stage 3 결정 4)과 같은 문제를 방지. `validation_status`는
  `strategy/status.py`의 앞 4개 상태(`COLLECTED`/`STRUCTURED`/`REVIEWED`/`REJECTED`)로만 제한 —
  `ACTIVE`는 단지 "아직 도달 못함"이 아니라 **구조적으로 도달 불가능**(다른 상태로 생성 시도 시
  `InvalidStrategySourceError`).
- `repository.py`: 버전 관리되는 append-only JSON 저장소(기본 `docs/strategy/sources/`,
  테스트용 `STRATEGY_SOURCES_DIR` 환경변수 오버라이드). `save_source()`는 버전이 정확히
  (현재 최대 버전+1)이 아니면 거부하고, 이미 존재하는 버전 파일은 절대 덮어쓰지 않음 — 소스 자료의
  변경 이력이 보존됨. `positions/store.py`와 동일한 `fcntl.flock` 락 패턴으로 동시 저장 시 버전
  번호 경쟁 방지.
- `similarity.py`: 두 소스 간 카테고리별 Jaccard 단어 중복도 기반 **결정론적 규칙 기반**
  유사도 채점 — LLM 판단이 아님(Stage 8의 전략 선택 설명가능성 요구사항과 동일한 원칙을 한 단계
  앞서 적용).
- `known_sources.py`: 지시서에 명시된 8개 소스(VWAP 진입, 1:2 손익비, 1R 50% 분할 익절, Ross
  Cameron 스타일 마이크로 눌림목은 `PROJECT_CONSTITUTION.md`에 실제로 명시되어 있고
  `vwap_micro_pullback_v1.py`로 이미 구현됨 — 실제 인용문으로 `origin=SOURCE` 처리, `REVIEWED`.
  Turtle·멀티 타임프레임 RSI·볼린저 눌림목·CCI/RSI/ADX는 실제 지정된 소스 문서/영상이 없어 모든
  claim을 `origin=ASSUMPTION`, `reference`를 `TBD_OPERATOR` 명시 마커로 처리 — 근거 없는 인용을
  지어내지 않음(`PROJECT_CONSTITUTION.md` 절대 금지사항 13 "유튜브에서 추출한 전략을 검증 없이
  주문 엔진에 연결하지 않는다"와 같은 원칙을 한 단계 앞서 적용), `validation_status=COLLECTED`
  유지, `derived_strategy_id` 없음). `seed_known_sources()`는 멱등적이며 실제
  `docs/strategy/sources/*.json` 8개 파일로 이미 시딩 완료.
- 신규 테스트: `tests/test_strategy_sources.py` 33건(claim/source 검증 — `ACTIVE` 구조적 차단
  포함, 왕복 직렬화, 저장소 버전 관리 — 저장/로드/잘못된 버전 번호 거부/버전 건너뛰기 거부/다중
  버전 이력 보존, 유사도 채점 — 동일/무관/부분 카테고리 중복 claim, 임계값 필터링, 자기 자신 제외,
  8개 알려진 소스 카탈로그 — 정확히 8개 고유 항목, `REVIEWED` 이상 없음, 미검증 4개 항목에
  조작된 `SOURCE` claim 없음, 시딩 멱등성 및 재로드 검증).
- 전체 회귀: **736 passed, 0 failed**(기존 703 + 신규 33). 실제 네트워크 호출 0회, 운영 CSV
  변경 0건.
- 커밋: `639af97`(구현+테스트+시딩된 카탈로그), `8915c44`+`9814114`(`.gitignore` 락 파일 패턴
  버그 수정 및 실수로 커밋된 빈 락 파일 제거 — 부수적 정리).
- 잔여 위험: 8개 중 4개(Turtle/멀티 RSI/볼린저/CCI-RSI-ADX)는 실제 사용자 자료·영상이 아직
  제공되지 않은 자리표시자 카탈로그 — 실제 자료 제공 시 `save_source()`로 버전 2를 추가해 갱신
  필요. `similarity.py`는 매우 단순한 단어 중복 기반이라 유사 전략 탐지의 정밀도는 낮음(의도된
  최소 구현, Stage 7/8에서 필요 시 고도화 검토).

## Stage 5 — 거래 상태 저장소(`state_store/`) 구현 완료 (2026-07-25)
`docs/autonomous/DECISION_LOG.md` "Stage 5" 섹션에 CSV vs SQLite 평가를 먼저 기록한 뒤 착수.
결론: `order_history.csv`/`order_reconciliation.csv`/`POSITION_STORE.json`은 각자 원자적이지만
세 파일에 걸친 단일 트랜잭션이 없어 Phase 1B/Phase 5가 이미 문서화한 잔여 위험(부분 체결의
포지션 상태 완전 반영)의 근본 원인이 됨 — SQLite로 **병행** 인프라를 구축하되, **실제 운영 경로는
전환하지 않음**(지시서의 절대 제약).

- `state_store/schema.py`+`migrations.py`: `orders`/`fills`/`positions`/`position_events`/
  `strategy_runs`/`risk_events`/`kill_switch_events` 7개 테이블 + `schema_migrations` 버전 추적.
  `fills.client_order_id`는 `orders`로의 FK를 걸지 않음(`order_history.csv` 자체가
  `client_order_id`를 항상 갖지 않아 — 그 값은 `order_intent_ledger.csv`에만 존재 — 강제 FK가
  정상 레거시 가져오기를 실패시키므로 기존 CSV들과 동일한 느슨한 자연 키 상관관계 유지).
- `state_store/db.py`: `connect()`(WAL 모드, FK 강제, busy_timeout), `init_db()`(멱등적 마이그레이션
  실행기, 마이그레이션별 독립 트랜잭션).
- `state_store/csv_import.py`: `order_history.csv`/`order_reconciliation.csv`용 **읽기 전용**
  가져오기(원본 CSV는 `pandas.read_csv()`만, 절대 쓰거나 삭제하지 않음 — 바이트 불변 테스트로 확인).
  구버전 2컬럼 형식과 현재 5컬럼 형식(`REQUIRED_HISTORY_COLUMNS`) 모두 관용적으로 처리, 자연 키
  기반 멱등성(재실행 시 중복 삽입 없음).
- `state_store/export.py`: `export_table()`/`export_all()`(SQLite→CSV, 대상 경로는 항상 호출자
  지정), `reset_schema()`(SQLite 파일 자체만 초기화, 가져오기 원본 CSV는 건드리지 않음).
- 신규 테스트: `tests/test_state_store.py` 20건(스키마/마이그레이션 멱등성, 트랜잭션 무결성,
  UNIQUE/FK 제약, 레거시·신규 CSV 가져오기, 가져오기 멱등성, 파일 누락 처리, 내보내기 왕복,
  `reset_schema` 데이터 초기화, 실제 `TRADING_STATE.db` 미생성 확인).
- 전체 회귀: **703 passed, 0 failed**(기존 683 + 신규 20). 실제 네트워크 호출 0회, 운영 CSV 변경
  0건(가져오기 전후 바이트 동일 확인), `broker/`·`order_safety.py`·`config/scanner_presets.json`·
  `.env`·kill switch 상태 파일 변경 없음. `.gitignore`에 `TRADING_STATE.db(-wal/-shm)` 추가.
- 커밋: `bf05098`.
- 잔여 위험/미해결: SQLite 저장소를 실제 주문/포지션 경로에 배선하는 것은 이번 단계에 포함되지
  않음 — `DECISION_LOG.md`에 `NEEDS_USER_DECISION`으로 명시. 배선 전까지는 CSV/JSON이 유일한 실제
  판단 근거로 계속 사용됨(안전성에 영향 없음, 감사/롤백용 병행 사본일 뿐).

## Stage 4 — 포지션 생명주기(`positions/`) 구현 완료 (2026-07-25)
사용자의 "Stage 3~10 연속 구현, Codex 중간 검증 없이 진행" 지시에 따라 착수. `docs/autonomous/
SCALPING_V1_ROADMAP.md` Phase 5 대응. 신규 패키지 `positions/`(`states.py`, `store.py`,
`lifecycle.py`)를 추가했다.

- `positions/states.py`: 13개 생명주기 상태(`SETUP_DETECTED`~`CLOSED`) + 6개 예외 상태
  (`REJECTED/CANCELLED/EXPIRED/UNKNOWN/MANUAL_REVIEW/RECOVERY_REQUIRED`), 명시적 `TRANSITIONS`
  인접 테이블로 임의 상태 전이를 구조적으로 차단, `FAIL_CLOSED_STATE = RECOVERY_REQUIRED`
  (`kill_switch_state.py`의 fail-closed 컨벤션 재사용, 한 단계 더 보수적).
- `positions/store.py`: 포지션별 JSON 원자적 저장소(`order_intent_ledger.py`/`kill_switch_state.py`
  와 동일한 `fcntl.flock`+tempfile+fsync+os.replace 패턴), 레코드별 fail-closed 검증(손상 JSON/
  필드 누락/미인식 상태 → 다른 레코드는 영향 없이 해당 레코드만 `RECOVERY_REQUIRED`), `locked_position()`
  컨텍스트 매니저로 "읽기→판단→브로커 호출→쓰기" 전체 구간을 단일 락으로 보호(중복 청산 방지의
  핵심 메커니즘 — 최초 설계는 저장 시점만 잠갔는데, 두 동시 호출이 모두 브로커를 호출한 뒤 마지막
  쓰기만 순서가 보장되는 경쟁 조건이 있어 재설계함, 스레딩 테스트로 검증).
- `positions/lifecycle.py`: `enter_position()`(전략 `require_active()` 검증 → `generate_entry()` →
  `try_reserve_order()` → `submit_order(side="buy")`, ledger commit/abort), `record_fill()`(부분/
  완전 체결, `FILLED`→`STOP_ACTIVE` 자동 전이), `check_and_manage()`(우선순위: EOD 강제청산 >
  시간손절 > 손절 > 1R 50% 분할익절 > 2R 전량청산, 분할 익절 후 손절가를 손익분기로 이동하는
  최소 트레일링 정책), `check_invalidation()`(전략 무효화 신호 시 전량청산, 신선한 봉 데이터가
  필요해 `check_and_manage()`와 분리), `recover_on_restart()`(브로커 재조회 실패/불확실/broker
  미제공 시 `RECOVERY_REQUIRED`로 fail-closed, 이미 `RECOVERY_REQUIRED`인 레코드는 절대 추측으로
  복구하지 않음). 모든 청산 주문은 `paper_strategy_order.submit_order(side="sell")`을 직접
  호출 — `try_reserve_order()`/`is_duplicate_order()`는 "심볼당 하루 1건" 진입 전용 중복 방지
  구조라 청산에 재사용할 수 없다고 판단(청산은 kill switch/자격증명/`RequestPurpose` 게이트는
  그대로 통과, 진입 전용 일일 중복 방지 로직만 우회). 근거: `DECISION_LOG.md` Stage 4 섹션.
- 신규 테스트: `tests/test_position_states.py` 31건(상태 전이 커버리지), `tests/test_position_store.py`
  15건(원자적 저장/락 경쟁/fail-closed/`locked_position` 동시성), `tests/test_position_lifecycle.py`
  23건(진입 성공/무신호/비활성전략/kill-switch차단/브로커거부, 부분·완전체결, 1R분할익절·2R전량청산·
  손절, 시간손절, EOD강제청산, 전략무효화, 동시 손절 요청의 중복 청산 방지, 실현/미실현 PnL, 재시작
  복구 4가지 시나리오) — 총 69건 신규.
- 전체 회귀: 저장소 루트 `venv/bin/python -m pytest -q` 기준 **683 passed, 0 failed**(기존 613 →
  Stage 3 이후 660(부분 구현) → 683). 실제 Alpaca/Slack/네트워크 호출 0회(FakeBroker/모킹만 사용),
  `order_history.csv`/`universe.csv`/`strategy_performance.csv` MD5 불변, `broker/`·`order_safety.py`·
  `config/scanner_presets.json`·`.env`·kill switch 상태 파일 변경 없음.
- 커밋: `a78ab1b`(states+테스트), `2058614`(store+테스트), `f9a2d1f`(`locked_position()`+VWAP
  `invalidate()` 실구현+config), `b3d8cf4`(lifecycle+테스트).
- 잔여 위험: 상태 영속화가 여전히 파일(JSON) 기반이며 `order_history.csv`와 별개 파일이라 두 파일에
  걸친 단일 트랜잭션은 없음(Phase 1B에서 이미 문서화된 동일 위험, 안전 크리티컬 판단 자체는
  `order_history.csv`/kill switch 상태에만 의존하므로 실거래 안전성에는 영향 없음) — Stage 5(SQLite
  전환 검토)에서 재평가 예정. 트레일링 정책은 "1R 50% 분할 후 손절을 손익분기로 이동"이라는 최소
  규칙으로, 정교한 트레일링 알고리즘이 아님(의도된 초기 정책). 실시간 브로커 reconciliation
  (`recover_on_restart()`가 실제 Alpaca 응답을 어떻게 파싱할지)은 Phase 3(1분봉 실시간 인프라) 착수
  후 실제 broker 클라이언트로 통합 테스트 필요 — 현재는 fail-closed 동작만 검증됨.

## Stage 3 — 전략 플랫폼(`strategy/`) 구현 완료 (2026-07-25)
`docs/autonomous/SCALPING_V1_ROADMAP.md` Phase 4 대응. 신규 패키지 `strategy/`(`interface.py`,
`status.py`, `registry.py`, `plugins/vwap_micro_pullback_v1.py`, `plugins/__init__.py`,
`plugins/_example_orb_stub.py`)와 `config/scalping_strategy_v1_config.py`를 추가했다.

- `TradingStrategy` ABC(`strategy/interface.py`): `strategy_id`/`version`/`status`를 생성 시점에
  fail-closed 검증. `evaluate_setup`/`generate_entry`/`calculate_stop`/`calculate_targets`는 Stage 3
  실 구현. `manage_position`/`invalidate`는 `NotImplementedError` 스텁(Stage 4/Phase 5 포지션
  생명주기 선행 필요, 코드 주석에 이유 명시).
- 전략 상태(`strategy/status.py`): `COLLECTED/STRUCTURED/REVIEWED/BACKTESTED/PAPER_APPROVED/
  LIMITED_LIVE_APPROVED/ACTIVE/PAUSED/REJECTED` 9종, `ORDER_GENERATING_STATUSES={ACTIVE}`로 주문
  생성 가능 여부를 단일 지점에서 정의.
- `StrategyRegistry`(`strategy/registry.py`): 등록 시점에 strategy_id/version/status 검증(fail-closed),
  ACTIVE 최대 1개를 구조적으로 강제(두 번째 ACTIVE 등록/활성화 시도는 `StrategyRegistrationError`로
  거부, 첫 번째를 암묵적으로 비활성화하지 않음 — 결정 근거 `DECISION_LOG.md`), `get_active_strategy()`
  (없으면 `None`), `require_active()`/`select_strategy_for_order()`(ACTIVE가 아니면
  `StrategyNotActiveError`, PAPER_APPROVED/LIMITED_LIVE_APPROVED도 차단).
- `VWAP_MICRO_PULLBACK_MOMENTUM_V1`(`strategy/plugins/vwap_micro_pullback_v1.py`): VWAP/EMA9/EMA21을
  pandas로 직접 계산(`indicators.py`는 일봉 HMA 계열 전용이라 재사용 대상 아님을 확인 후 판단).
  price>VWAP·EMA9>EMA21 → 초기 rally → 얕은 pullback(거래량 감소) → 재돌파(거래량 재확대) 순으로
  판정, 손절은 micro-pullback low + ATR 기반 최소 버퍼, 목표는 1R에서 50% 분할 익절(문서에 명시된
  값) + target_2 2R(ASSUMPTION, 근거 `DECISION_LOG.md`).
- 주문 경로 연결: `paper_strategy_order.submit_order()`에는 현재 `strategy_id` 개념 자체가 없어
  (하드코딩된 단일 스코어링만 존재) 가짜 연결점을 만들지 않았다 — `require_active()`/
  `select_strategy_for_order()`를 `strategy/registry.py`의 독립 함수로 구현하고
  `tests/test_strategy_platform.py`에서 직접 검증. Stage 4가 실제 주문 트리거 경로에서 호출할
  예정(코드 주석에 명시).
- 확장 패턴: `strategy/plugins/__init__.py` 모듈 docstring + `strategy/plugins/_example_orb_stub.py`
  (미구현 스텁, ORB류 신규 전략 추가 시 따라야 할 최소 형태 예시).
- 신규 테스트: `tests/test_strategy_platform.py` 43건(레지스트리 검증/ACTIVE 1개 강제/가드/플러그인
  entry-present·VWAP-EMA 실패·pullback 없음·stop/target 정합성·Stage4 스텁 등).
- 전체 회귀: 저장소 루트 `venv/bin/python -m pytest -q` 기준 **613 passed, 0 failed, 2 warnings**
  (기존 570 + 신규 43). 실제 네트워크 호출 0회(모두 구성된 pandas DataFrame 사용), `order_history.csv`
  /`universe.csv`/`strategy_performance.csv` MD5 불변 확인, `broker/`·`order_safety.py`·
  `config/scanner_presets.json`·`.env`·kill switch 상태 파일 변경 없음.
- 잔여 위험: Stage 4(Phase 5, 포지션 생명주기)가 `manage_position`/`invalidate`를 실제 구현하고
  `require_active()`를 실제 주문 트리거 경로에 배선해야 함. 임계값(눌림 깊이 %, rally 최소 %,
  target_2 R-배수 등)은 Phase 6 백테스트 이전까지 잠정값(`DECISION_LOG.md`). 신호 중복 방지/추격진입
  방지/실시간 스프레드·유동성 차단/stale 데이터 차단은 Phase 3(1분봉 실시간 수집, 여전히
  `NOT_STARTED`) 착수 후 별도 구현 필요.

## Codex 최종 독립 재검증: PASS_WITH_CONDITIONS (2026-07-25, 커밋 `a31290b`/`5aac75b`/`8803252` 대상)
Overall verdict **`PASS_WITH_CONDITIONS`**. CODEX-016~022 전부 **RESOLVED**로 최종 확정, 신규
CRITICAL/HIGH/MEDIUM Finding 없음. Limited live review 권고: **`READY_FOR_LIMITED_LIVE_REVIEW`**
— 단 **Live trading: DO_NOT_ENABLE`**을 유지하며, 이 권고 자체가 실거래 승인을 의미하지 않는다.
남은 조건은 전부 코드 Finding이 아니라 운영자가 실제로 채워야 하는 `TBD` 항목(실제 Alpaca
계정/credential, 현재 포지션·미체결 주문·broker reconciliation, 허용 종목·거래시간·주문당 절대
한도, 승인자·검토 시각·롤백 담당자)이며, `docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md`에 각
항목의 권장값 초안·근거·위험·승인 필요 여부가 정리되어 있다. `approved: false`,
`live_enabled: false`는 변경하지 않았다. 상세: `docs/autonomous/CODEX_REVIEW.md`(커밋 `d38cb95`).

## 제한적 실거래 검토 사이클 — CODEX-022 해결 및 CODEX-021 잔여분 종결 (2026-07-25, 이전 기록)
Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ae3ca`/`c133e01`/`cc740a5`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/018/019는 RESOLVED로 재확인됐으나, CODEX-021(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-022(HIGH)**가 제기됐다 — `RequestPurpose` 재설계
(커밋 `c133e01`) 이후에도 `_request()`가 주문 POST의 payload `side`와 `order_side`, `purpose`
세 값을 서로 대조하지 않아, `purpose=EXIT_ORDER`를 선언한 채 매수 payload(`json={"side":
"buy"}`)를 전달하면 `ENTRY_DISABLED` 상태에서도 HTTP가 실제로 나갔다.

이번 사이클(t1)에서 `broker/alpaca_client.py`에 신규 `validate_order_intent(purpose, order_side,
payload)`를 도입해 `_request()`가 세션 호출 전, 다른 어떤 안전장치보다도 먼저 이 3자 일치를
검증하도록 배선했다(커밋 `5aac75b`):
- **CODEX-022 (HIGH)**: `_PURPOSE_REQUIRED_SIDE` 매핑(`ENTRY_ORDER→"buy"`, `EXIT_ORDER→"sell"`)
  기준으로, `ENTRY_ORDER`/`EXIT_ORDER`는 `order_side`와 payload의 `side`가 모두 존재하고 정확히
  요구되는 문자열과 완전히 일치해야 한다(대소문자·공백·`bool`/`int` 변형도 거부). 불일치·누락·
  비-dict body는 모두 `ValueError`로 세션 호출 전에 차단된다.
- **CODEX-021 잔여분 (HIGH)**: 위와 동일한 함수로 함께 닫혔다 — `order_side`가 이제 실제로
  payload `side`와 대조되므로 2차 방어선으로서 실질적 방어력을 갖는다.

CODEX-016~019(다단계 kill switch 배선, Slack health 배선, 주문 직전 credential/환경 재검증,
상태 저장소 파일 잠금)는 이번 사이클에서 **재작업하지 않았다** — 관련 회귀 테스트
(`tests/test_paper_strategy_order_kill_switch_state.py` 12건,
`tests/test_paper_strategy_order_notification_health.py` 6건,
`tests/test_state_store_concurrency.py` 6건, 도합 36 passed)로 회귀 없음만 확인했다.

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **570 passed, 0 failed, 2
warnings**다. 집중 테스트(`tests/test_broker_kill_switch_gate.py` +
`tests/test_broker_request_purpose.py` + `tests/test_broker_order_intent_gate.py`(신규) +
`tests/test_alpaca_client_runtime_revalidation.py` + `tests/test_broker_safety.py` +
`tests/test_universe_builder.py` + `tests/test_paper_strategy_order_kill_switch_state.py` +
`tests/test_paper_order_execution.py`) **289 passed, 1 warning**. `order_history.csv`/
`universe.csv` SHA-256은 이전 사이클 기록값과 동일(불변), `.env`·kill switch/notification 상태
파일 변경 없음. 현재 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지
**Limited live review: BLOCKED**, **Live trading: DO_NOT_ENABLE**을 유지한다.

## 이전 사이클 — CODEX-021 해결 및 CODEX-020 잔여분 종결 (2026-07-25, 역사적 기록)
Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `66eda8a`/`ed452da`/`cf5601d`/`edc5ad5`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/018/019는 RESOLVED로 재확인됐으나, CODEX-020(HIGH)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-021(HIGH)**이 제기됐다 — `_request()`의 `order_side`가
필수 인자이긴 했지만 POST 경로와 의미적으로 결합되지 않아, `broker._request("POST", "/v2/orders",
order_side=None, ...)`처럼 명시적으로 `None`을 전달하면 `_check_kill_switch(None)`이 method/path를
전혀 확인하지 않고 즉시 반환해 kill switch를 우회할 수 있었다.

이번 사이클(t1)에서 `AlpacaBroker._request()`를 `order_side` 단일 신호가 아니라 신규
`RequestPurpose` enum(`READ_ONLY`/`ENTRY_ORDER`/`EXIT_ORDER`/`CANCEL_ORDER`/`RECONCILIATION`)
기반으로 재설계했다(커밋 `c133e01`):
- **CODEX-021 (HIGH)**: `_request()`에 기본값 없는 keyword-only `purpose` 인자를 추가하고,
  `isinstance(purpose, RequestPurpose)`를 요구해 `None`을 포함한 잘못된 값은 `ValueError`로
  세션 접근 전에 차단한다. `_METHOD_PURPOSES` 매트릭스가 HTTP method(GET/POST/DELETE)와
  purpose의 허용 조합을 명시적으로 검사해, 예컨대 POST가 `READ_ONLY`를 주장하거나 GET이
  `ENTRY_ORDER`를 주장하는 불일치를 세션 호출 전에 거부한다. `_check_kill_switch()`는 이제
  `purpose`가 `ENTRY_ORDER`/`EXIT_ORDER`일 때만 kill switch를 검사하며, `order_side`는 payload의
  `side`와 `purpose`가 일치하는지 확인하는 2차 방어선으로만 쓰인다(`submit_order()`가
  `_SIDE_TO_PURPOSE`로 파생한 `purpose`와 payload의 `order["side"]`가 다르면 세션 호출 전에
  `RuntimeError`).
- **CODEX-020 잔여분 (HIGH)**: 위와 동일한 재설계로 함께 닫혔다 — method+path 기반 주문 감지
  백스톱이 없다는 지적이 `_METHOD_PURPOSES` 매트릭스로 해결됐다. 조회·취소 경로
  (`get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order`)는 각각 `RequestPurpose.READ_ONLY`/
  `RECONCILIATION`/`CANCEL_ORDER`를 명시해 kill switch 정책과 무관하게 계속 동작한다.

CODEX-016~019(다단계 kill switch 배선, Slack health 배선, 주문 직전 credential/환경 재검증,
상태 저장소 파일 잠금)는 이번 사이클에서 **재작업하지 않았다** — 관련 회귀 테스트
(`tests/test_paper_strategy_order_kill_switch_state.py` 12건, `tests/test_paper_strategy_order_notification_health.py`
6건, `tests/test_state_store_concurrency.py` 6건, 도합 36 passed)로 회귀 없음만 확인했다.

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **536 passed, 0 failed, 2 warnings**다.
집중 테스트(`tests/test_broker_kill_switch_gate.py` + `tests/test_broker_request_purpose.py`(신규) +
`tests/test_alpaca_client_runtime_revalidation.py` + `tests/test_broker_safety.py` +
`tests/test_universe_builder.py` + `tests/test_paper_strategy_order_kill_switch_state.py` +
`tests/test_paper_order_execution.py`) **255 passed, 1 warning**. `order_history.csv`/`universe.csv`
SHA-256은 이전 사이클 기록값과 동일(불변), `.env`·kill switch/notification 상태 파일 변경 없음.
현재 상태는 **`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review:
BLOCKED**, **Live trading: DO_NOT_ENABLE**을 유지한다.

## 이전 사이클 — CODEX-020·CODEX-018 잔여분 수정 (2026-07-24, 역사적 기록)
최신 Codex 독립 재검증(`CODEX_REVIEW.md`, 대상 커밋 `47ee8d6`/`03962d3`/`cf4ada9`)의 판정은
**Overall verdict: FAIL**이었다. CODEX-016/017/019는 RESOLVED로 재확인됐으나, CODEX-018(MEDIUM)이
PARTIALLY_RESOLVED로 남았고 신규 **CODEX-020(HIGH)**이 제기됐다 — direct
`AlpacaBroker.submit_order()`가 `paper_strategy_order.py` wrapper를 거치지 않고 직접 호출되면
binary kill switch(`kill_switch.is_trading_halted()`)와 다단계 kill switch
(`kill_switch_state.is_entry_allowed()`/`is_liquidation_allowed()`)를 모두 우회해 HTTP가 실제로
나갔다. 또한 CODEX-018의 "현재 credentials 재검증" 요구사항이 `_validate_runtime_safety()`에
아직 배선되지 않았다는 지적도 함께 남아 있었다.

이번 사이클(t1~t2)에서 두 항목을 broker 공통 경로에 배선했다:
- **CODEX-020 (HIGH)**: `AlpacaBroker._request()`에 `order_side`(주문이 아니면 `None`, 매수/매도면
  `"buy"`/`"sell"`) 키워드 전용 필수 인자를 추가하고, 내부에서 신규 `_check_kill_switch()`가
  binary halt와 side별 4-state(`is_entry_allowed`/`is_liquidation_allowed`) 정책을 매 요청마다
  다시 조회해 불허 시 HTTP 호출 전에 `RuntimeError`를 발생시키도록 배선했다(커밋 `66eda8a`).
  `get_account`/`get_positions`/`get_recent_orders`/`get_assets`/
  `get_order_by_client_order_id`/`cancel_order` 등 조회·취소 경로는 `order_side=None`으로 명시해
  kill switch 정책과 무관하게 계속 동작하도록 분리했다. `_request()`를 우회해 `order_side`를
  생략하면 네트워크 호출 전에 `TypeError`로 즉시 차단된다.
- **CODEX-018 잔여분 (MEDIUM)**: `_validate_runtime_safety()`에 `_validate_current_credentials_match_captured()`를
  추가해, 매 요청마다 `BrokerConfig.from_env()`로 현재 환경의 API key/secret을 다시 읽어
  생성 시점에 캡처된 값과 `hmac.compare_digest()`로 상수시간 비교한다. 누락/공백/회전/삭제/환경
  읽기 실패 시 모두 요청 전에 차단하며, credential 값 자체는 예외 메시지에 포함하지 않는다(커밋 `ed452da`).

전체 회귀는 저장소 루트 `venv/bin/python -m pytest -q` 기준 **489 passed, 0 failed, 2 warnings**다.
집중 테스트(`tests/test_broker_kill_switch_gate.py` 25건, `tests/test_alpaca_client_runtime_revalidation.py`
44건 포함) 208 passed. `order_history.csv`/`universe.csv` SHA-256은 `CODEX_REVIEW.md`에 기록된
값과 동일(불변), `.env`·kill switch/notification 상태 파일 변경 없음. 현재 상태는
**`READY_FOR_CODEX_REVALIDATION`**이며, 독립 재검증 전까지 **Limited live review: BLOCKED**,
**Live trading: DO_NOT_ENABLE**을 유지한다.

## 마지막 완료 작업 (CODEX-010~015 수정 사이클)
- CODEX-010 (HIGH): `numeric_guard.require_finite_number()` 도입, `features.py`의 모든 raw/derived 수치에 NaN/Infinity 명시 차단 적용.
- CODEX-011 (HIGH): `SymbolSnapshot`에 `data_as_of`/`provider_fetched_at` 분리, `freshness.py` 신규(세션별 최대 데이터 나이), `YFinanceMarketDataProvider`가 손상/미래/타임존無 타임스탬프를 fail-closed 반환.
- CODEX-012 (MEDIUM): `calendar_guard.py` 신규 — 휴장일(`market_guard.is_us_trading_day`)/허용 세션/정규장 오픈 윈도우를 provider·파일 접근 이전에 게이트, 차단 시 `SKIPPED`(미저장).
- CODEX-013 (MEDIUM): `save_watchlist_cycle()`이 `{success, persisted_count, error_code, error_message}` 반환 + 쓰기 후 재검증(`_verify_after_write`), `run_scan_cycle()` 결과에 `status/error_code/error_message` 포함.
- CODEX-014 (MEDIUM): `first_detected_at/last_detected_at/updated_at` 3분리, `detect_count` 기반 실제 NEW→ACTIVE 전이, `validate_lifecycle_timestamps()`로 손상된 타임스탬프를 가진 행은 방치 대신 REJECTED 처리(TTL 우회 차단).
- CODEX-015 (LOW): `_compute_average_volume()`이 당일(미완료) 봉을 제외하고 최소 완료일수 미만이면 `None` 반환; `filter_premarket_rows()` 순수함수로 04:00~09:30 ET premarket 구간 분리, `premarket_coverage_complete` 필드로 부분 구간 여부 명시.
- 신규 테스트 65건 (`tests/test_scalping_watchlist.py` 103건 → 118건: CODEX-015분 15건 포함).
- 전체 회귀 267 passed(레포 루트 `pytest -q`/`python -m pytest -q` 동일), 실제 외부 API 호출 0회, `order_history.csv` 해시 불변, 운영 파일 변경 없음 확인.

## 현재 테스트 수
792 passed, 0 failed (Stage 8 전략 선택 엔진 신규 27건 포함, Stage 7 백테스트 엔진 신규 29건,
Stage 6 전략 자료 구조화 신규 33건, Stage 5 거래 상태 저장소 신규 20건, Stage 4 포지션 생명주기
신규 69건: states 31 + store 15 + lifecycle 23)

## 실패 테스트
없음

## 현재 블로커
없음 (코드 수준). CODEX-016~022는 이전 사이클에서 Codex 최종 독립 재검증까지 `PASS_WITH_CONDITIONS`로
종결됨(위 "Codex 최종 독립 재검증" 섹션 참고). 사용자 지시에 따라 Stage 3~10은 Codex 중간 검증 없이
연속 구현 중이며, 전체 완료 후 `FINAL_VALIDATION_PACKAGE.md` 작성과 함께 Codex 통합 검증을 1회
요청할 예정. `approved: false`, `live_enabled: false` 유지. **Limited live review: BLOCKED**(신규
Stage 코드가 아직 Codex 검증을 거치지 않았으므로), **Live trading: DO_NOT_ENABLE**.

## 다음 작업
1. Stage 9(운영 관제 Dashboard/CLI) 착수 — 현재 모드/활성 전략/시장상태/관심종목/신호/주문/
   포지션/손절·목표가/실현·미실현 PnL/일일 주문 수/일일 손실/Kill Switch/Slack 상태/broker 상태/
   reconciliation/마지막 성공 실행 시각을 로컬에서 확인 가능하게 하고, Slack이 다운되어도 계속
   확인 가능해야 한다.
2. Stage 10을 사용자 지시서의 순서대로 계속 진행(자체 테스트 → 전체 회귀 → 로컬 커밋 → 문서 갱신,
   Codex 검증 없이).
3. Stage 10 완료 후 `docs/autonomous/FINAL_VALIDATION_PACKAGE.md` 작성, 상태를
   `READY_FOR_FINAL_CODEX_VALIDATION`으로 종료.

## 최근 커밋
- `2094adf` Add deterministic strategy selection engine (Stage 8)
- `59958cf` Add intraday strategy backtest/replay engine (Stage 7)
- `9814114` Normalize .gitignore to LF and remove duplicate/dead lines
- `8915c44` Fix .gitignore lock-file pattern and remove stray lock file
- `639af97` Structure user and YouTube strategy sources (Stage 6)
- `bf05098` Add local SQLite trading state store (Stage 5)
- `b3d8cf4` Add position lifecycle and automated exits (Stage 4 part 4/N)
- `f9a2d1f` Add locked_position() and real strategy invalidation (Stage 4 part 3/N)
- `2058614` Add atomic position record store (Stage 4 part 2/N)
- `a78ab1b` Add position lifecycle state machine (Stage 4 part 1/N)
- `5aac75b` CODEX-022 해결 + CODEX-021 잔여분 종결: `_request()`에 중앙 집중식 3자 일치(purpose/order_side/payload side) 검증 추가 및 회귀 테스트
- `a31290b` Codex 독립 재검증 기록: FAIL, CODEX-021 partial, CODEX-022 신규
- `c133e01` CODEX-021/CODEX-020 잔여분: `_request()`를 RequestPurpose 기반으로 재설계하고 회귀 테스트 추가
- `47ae3ca` Codex 독립 재검증 기록: FAIL, CODEX-020 partial, CODEX-021 신규
- `ed452da` CODEX-018 잔여분: 공통 gate 에서 현재 credentials 재검증
- `66eda8a` CODEX-020: broker `_request` 공통 경로에 kill switch 게이트 추가
- `4f1f89d` Correct volume and premarket calculations (CODEX-015)
- `7ab8db7` Align watchlist lifecycle and timestamp validation (CODEX-014)
- `ac2b4b3` Make watchlist persistence failures explicit (CODEX-013)
- `044df60` Gate the pipeline behind trading-day and allowed-session checks (CODEX-012)

## 미반영 검증 지적사항
없음. Phase 1의 CODEX-001~009, Phase 2의 CODEX-010~015, 제한적 실거래 검토의 CODEX-016~022 전부
Claude 측 수정/테스트 완료. Codex 최종 재검증 대기 중(Phase 2 `PROCEED` 여부, CODEX-022 해결 및
CODEX-021 잔여분 종결의 `RESOLVED` 여부 모두 미확정).
