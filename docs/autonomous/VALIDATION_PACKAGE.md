# VALIDATION_PACKAGE

외부 검증자(ChatGPT/Codex)가 `CODEX_REVIEW.md`를 작성하기 위해 필요한 정보 패키지. Phase 완료 시마다 갱신한다.

---

## 이번 패키지: Phase 2 구현 완료 — 초단타 관심종목 선별 엔진 (2026-07-22)

### 배경
Phase 1 최종 Codex 판정(verdict `PASS_WITH_CONDITIONS`, CODEX-001~009 전부 RESOLVED): **Phase 1A(주문 진입 안전성) VALIDATED**, **Phase 1B(부분체결·포지션 생명주기) DEFERRED_TO_PHASE_5**, **Phase 2 PROCEED**. 이 판정에 따라 Phase 2를 착수·구현 완료했다. 이번 패키지가 다루는 커밋: `4a96883`.

### 목적
"오늘 어떤 종목을 1분봉으로 집중 감시할 것인가?"에 답하는 결정적 파이프라인. **주문 신호를 생성하지 않는다** — VWAP/EMA 진입 판단은 Phase 3·4 범위.

### 재사용 범위 (근거는 `DECISION_LOG.md`)
- 재사용: `daily_candidate_scanner.calculate_rsi`/`calculate_atr`(순수 함수), `market_hours.eastern_now`/`get_us_market_session`, `market_guard.is_us_trading_day`, `universe_builder.py`의 universe.csv(이미 tradable/active/us_equity로 필터링됨 — Stage A는 방어적 재검증만).
- 의도적으로 재사용하지 않음: 기존 JSON 룰 엔진(`evaluate_filter`)은 미지원 필드/연산자에서 **경고 후 통과**(fail-open)하도록 설계되어 있어, Phase 2의 명시적 원칙("불명확하면 포함하지 않는다")과 정면으로 배치됨. Stage A~E는 이 때문에 전용 명시적 함수로 새로 작성.
- 신규 구현 확인(저장소 전체 검색으로 기존 로직 없음을 확인): 다중 사이클 반복탐지 스트릭 추적, 스프레드/유동성 대체지표.

### 구현 구조
```
scalping_watchlist/
  models.py         WatchlistEntry dataclass (23 필드, UNKNOWN/NOT_AVAILABLE/NOT_EVALUATED 센티널)
  data_provider.py  MarketDataProvider 인터페이스, YFinance(운영)/Fake(테스트) 구현
  features.py       Stage C 피처 계산 + 데이터 품질 게이트
  eligibility.py    Stage A(방어적)/B(가격·유동성)/C(당일 움직임) 명시적 필터
  repeat_tracker.py Stage D 반복탐지(ET 거래일 기준, 잠금 보호)
  scorer.py         Stage E 설명 가능한 가중합 점수([0,100] 클램프)
  repository.py     scalping_watchlist.csv 영속화 + TTL 기반 만료(NEW→ACTIVE→COOLING→EXPIRED)
  atomic_io.py       temp file+fsync+os.replace, fcntl.flock (order_history.csv와 동일 기법, 독립 구현)
  pipeline.py        run_scan_cycle() — Stage A~E 오케스트레이션
config/scalping_watchlist_config.py   임계값/가중치 (risk_config.py와 분리, 대시보드 미노출)
```

### 필수 필드 처리
지시서 22개 필드 전부 구현 + `expires_at` 계산 로직 포함(총 23개, `models.CSV_COLUMNS`). 계산 불가능한 필드는 `UNKNOWN`/`NOT_AVAILABLE`/`NOT_EVALUATED`로 명시(허위 값 없음):
- `spread_estimate`: 항상 `NOT_AVAILABLE` — 실제 호가 데이터 소스가 프로젝트에 없음(확인됨). 대신 `average_dollar_volume` 기반 `liquidity_score`(0-100)를 유동성 게이트로 사용.
- `smart_money_score`: 항상 `NOT_EVALUATED` — 전체 재계산에 daily_candidate_scanner의 MA200/RSI 히스토리가 추가로 필요해 이번 버전에서는 보류(점수 가중치에서 0 기여로 처리, 향후 통합 여지).

### 변경 파일
- 신규: `config/scalping_watchlist_config.py`, `scalping_watchlist/` 전체(9개 파일), `tests/test_scalping_watchlist.py`(34건).
- 수정: 없음(기존 파일 일절 미변경 — Phase 1 주문/리스크/reconciliation/broker/systemd/cron/nginx 전부 그대로).
- 문서: `docs/autonomous/{SCALPING_V1_ROADMAP,CURRENT_STATUS,VALIDATION_REPORT,DECISION_LOG,VALIDATION_PACKAGE}.md`.

### 테스트 목록 (34건, 요구된 6개 범주 전부)

| 범주 | 테스트 |
|---|---|
| 정상 선별 | 기준 만족 종목 포함, 점수순 정렬, `MAX_WATCHLIST_SIZE` 캡, 동점 결정성(심볼 알파벳순 tiebreak) |
| 기본 차단 | 심볼 형식 오류, 가격 미달/초과, 평균거래량/거래대금/당일거래량 부족(파라미터라이즈드), 상대거래량 부족, 변동성 부족, 유동성 부족(단위테스트), 데이터 없음, 데이터 지연(stale), 비정상 갭(sanity limit), 심볼 중복 |
| 반복 탐지 | 최초 등장, 동일일 재등장 스트릭 증가, 타거래일 초기화, ET 날짜 경계(서울 저녁≠뉴욕), 중간탈락 후 재등장(스트릭 리셋, 총 카운트는 유지), **threading 동시 갱신 lost-update 방지** |
| 점수 | 하위점수 합=최종점수(가중치 일치), 극단 입력에도 [0,100] 범위 유지, NaN/Infinity 클램프, 입력 dict 순서 무관 재현성 |
| 파일 | 원자적 쓰기 실패 시 원본 보존, 잠금 타임아웃 시 파일 미변경, 손상된 watchlist 파일 fail-closed, 파일 없음=정상 빈 상태, 전부 `tmp_path` 격리 |
| 네트워크 | FakeMarketDataProvider만 사용 확인, provider 예외가 해당 심볼만 제외하고 나머지는 계속 처리 |

### 실행 명령 및 결과

```bash
# 저장소 루트
venv/bin/pytest -q                              # 183 passed, 2 warnings
venv/bin/python -m pytest -q                    # 183 passed, 2 warnings

# 저장소 상위 디렉터리
cd ..
us-stock-trading/venv/bin/pytest -q us-stock-trading            # 183 passed, 2 warnings

# 신규 테스트 집중
venv/bin/pytest -q tests/test_scalping_watchlist.py             # 34 passed

# 반복탐지 동시성 안정성(5회 반복)
venv/bin/pytest -q tests/test_scalping_watchlist.py -k "concurrent"   # 매회 1 passed

# 운영 파일 무결성
md5 order_history.csv   # a61104cf03499860ae89d4e194dc8c07 — Phase 1 종료 시점과 동일
```

### 안전 재검증
- 실제 Alpaca/Slack/Yahoo API 호출 0회: `FakeMarketDataProvider`만 사용, `YFinanceMarketDataProvider`는 테스트에서 import조차 되지 않음(실제 `yfinance`/`from daily_candidate_scanner import calculate_atr` import는 그 클래스의 메서드 내부에서 지연 임포트되어, provider 인스턴스를 만들지 않는 한 로드되지 않음).
- 운영 파일 변경 없음: `order_history.csv` 해시 불변. `scalping_watchlist.csv`/`scalping_repeat_state.csv`는 신규 파일이며 테스트는 전부 `tmp_path`로 리다이렉트, 실제 저장소 루트에 생성되지 않음(확인됨).
- 기존 주문/리스크/reconciliation/broker 로직: 파일 자체를 열지도 import하지도 않음 — 정적으로 완전히 독립된 신규 패키지.
- Live Trading, 운영 서버 접속, origin push 없음.

### 운영 영향
없음. 신규 코드가 어떤 cron/systemd 항목에도 아직 연결되어 있지 않다(Phase 2는 파이프라인 구현까지가 범위이며, 운영 편입은 이번 지시서 범위 밖).

### 남은 위험 / 알려진 한계
- `spread_estimate`/`smart_money_score` 미구현(설계상 의도, 위 참고) — 향후 재검토 대상으로 문서화.
- Stage B/C 임계값과 `SCORING_WEIGHTS`는 백테스트로 검증되지 않은 초기 가정(`DECISION_LOG.md`에 개별 근거 기록).
- `scalping_watchlist.csv`/`scalping_repeat_state.csv` 간에도 Phase 1과 유사하게 단일 트랜잭션은 없음 — 다만 이 데이터는 안전 크리티컬(주문 실행)이 아니므로 우선순위는 낮음.
- `YFinanceMarketDataProvider`(운영 구현)는 실제 Alpaca Paper 계정/실 마켓 데이터로 아직 E2E 검증되지 않음(외부 호출 금지 원칙에 따라 이번 사이클에서 수행하지 않음).

### Codex가 집중 검토해야 할 항목
1. JSON 룰 엔진을 재사용하지 않기로 한 결정(`DECISION_LOG.md`)이 타당한지, 아니면 fail-open 문제를 룰 엔진 자체에서 고치고 재사용하는 편이 나았는지.
2. `liquidity_score`(달러거래대금 기반 대체지표)가 실제 스프레드 없이 "유동성"을 판단하는 근거로 충분한지.
3. 반복탐지의 "동일 거래일" 판정과 만료(`WATCHLIST_TTL_MINUTES`/`WATCHLIST_EXPIRE_MINUTES`) 로직이 실제 스캔 주기(15분 등)와 정합적인지 — 이번 구현은 호출 시점 기준 임의 간격을 지원.
4. `run_scan_cycle()`의 `symbols` 파라미터(테스트/운영 모두 사용 가능)가 운영에서 실수로 전체 universe 대신 부분 목록으로 잘못 호출될 위험이 있는지.

### 현재 커밋 해시
`4a96883` (Add scalping watchlist selection engine) — 이번 패키지가 다루는 마지막 코드 커밋. 문서 갱신은 다음 커밋에서 기록됨.
