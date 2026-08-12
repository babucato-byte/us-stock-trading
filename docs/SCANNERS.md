# 독립 스캐너 운영 안내 (Scanner Expansion v1.0)

목적이 서로 다른 6개 스캐너를 같은 유니버스·같은 데이터로 독립 실행하고,
한 달 동안 성과를 비교할 수 있는 데이터를 모으는 구조입니다.

**이 작업은 실거래 변경이 아닙니다.** `scanners/` 패키지의 어떤 모듈도
주문 경로(`broker/`, `brokers/`, `execution/`, `live_pilot/`, 주문 후보 저장소)를
import 하지 않습니다. 이는 규칙이 아니라 구조로 보장되며
`tests/test_scanner_trading_isolation.py` 가 소스 트리를 AST로 직접 검사합니다.

금지 목록은 이 저장소의 **모든 브랜치에 존재하지 않는 모듈까지 포함**합니다.
지금 없는 모듈이 나중에 들어와도 그때 새로 규칙을 추가할 필요가 없도록,
이름 기준으로 미리 막아둡니다.

---

## 1. 구조

```
Alpaca Assets → universe_builder → universe.csv   (기존, 변경 없음)
                                        │
                                        ▼
                         scanners/base/market_data_provider.py
                                        │
        ┌───────────────┬───────────────┼───────────────┬──────────────┐
        ▼               ▼               ▼               ▼              ▼
   hma_early_trend  accumulation  breakout_ready  premarket_momentum  gap_pullback / orb
        └───────────────┴───────────────┼───────────────┴──────────────┘
                                        ▼
                        Scanner Analytics Store (logs/scanners/)
                                        │
                          ┌─────────────┴─────────────┐
                          ▼                           ▼
                  Weekly / Monthly 분석      Candidate Decision Layer
                                              (v1.0에서 비활성)
```

심볼 단위 루프입니다. 심볼 하나의 봉을 **한 번** 받아 6개 스캐너에 모두
전달합니다. API 호출이 1/6로 줄고, 더 중요하게는 6개 스캐너가 완전히
동일한 데이터·동일한 시각으로 판단하므로 교집합 분석(§17)이 의미를 가집니다.

---

## 2. 실행 방법

### 스캐너 실행

```bash
cd /home/ubuntu/trading            # 운영 서버 기준
source venv/bin/activate

# 프리마켓 (장 시작 전) — 모멘텀 스캐너
python scripts/run_scanners.py --profile premarket

# 장 시작 직후 (09:45~10:00 ET) — ORB / 갭 눌림
python scripts/run_scanners.py --profile open

# 장 마감 후 — 일봉 기반 3종
python scripts/run_scanners.py --profile daily

# 전체
python scripts/run_scanners.py --profile all

# 개별 지정 / 소량 점검
python scripts/run_scanners.py --scanners orb,gap_pullback --limit 50
python scripts/run_scanners.py --scanners hma_early_trend --symbols NVDA,AMD --no-store
```

`--no-store` 는 저장하지 않고 결과만 출력합니다. 배포 직후 점검용입니다.

미국 증시 휴장일에는 자동으로 아무것도 실행하지 않습니다
(기존 스캐너들과 동일하게 `market_guard.is_us_trading_day` 사용).
백필이 필요하면 `--ignore-market-calendar` 를 명시합니다.

### 성과 추적 (매일 장 마감 후)

```bash
python scripts/run_scanner_performance.py            # 최근 10거래일 재계산
python scripts/run_scanner_performance.py --days 30
```

**매일 실행해야 합니다.** 신호의 3일·5일 수익률은 며칠 뒤에야 존재하므로,
당일만 계산하면 다기간 컬럼이 영원히 비어 있게 됩니다. 기록은 append 되고
읽을 때 `signal_id` 별 최신 것이 이깁니다. 반복 실행은 안전하며 수렴합니다.

또한 장중 1분봉은 약 일주일이 지나면 공급자가 제공하지 않습니다. 매일
돌려두면 그 기간 안에 계산된 정밀한 값이 보존되고, 나중의 백필이 그것을
거친 값으로 덮어쓰지 않습니다 (`includes_signal_day_intraday` 로 구분 기록).

### 리포트

```bash
python scripts/run_scanner_report.py weekly                    # 이번 주
python scripts/run_scanner_report.py weekly --week-of 2026-08-10
python scripts/run_scanner_report.py monthly                   # 이번 달
python scripts/run_scanner_report.py monthly --month 2026-08
python scripts/run_scanner_report.py intersections             # 최근 30일
python scripts/run_scanner_report.py intersections --days 7
python scripts/run_scanner_report.py intersections --start 2026-08-01 --end 2026-08-31
python scripts/run_scanner_report.py export --start 2026-08-01 --end 2026-08-31
```

`export` 는 §22용 CSV/JSON을 `logs/scanners/exports/` 에 생성합니다.

### 기간 지정 규칙

`weekly` / `monthly` / `intersections` 는 인자 없이 실행할 수 있습니다.
`intersections` 의 기본값은 **최근 30일**이며 `--days` 로 조정합니다.
`--start` / `--end` 를 하나만 줘도 됩니다 — 나머지 한쪽은 같은 창(window)으로
채워집니다.

| 지정 | 결과 |
|---|---|
| 둘 다 | 그대로 사용 |
| 둘 다 생략 | `[오늘 - 30일, 오늘]` |
| `--start` 만 | `[start, 오늘]` |
| `--end` 만 | `[end - 30일, end]` |

`export` 는 예외적으로 **양쪽 모두 필수**입니다. 파일명에 기간이 들어가는
산출물이라, 호출자가 지정하지 않은 기간의 데이터셋을 그럴듯한 이름으로
만들어내면 안 되기 때문입니다.

**"오늘"의 기준**: 로컬 시스템 날짜(`date.today()`)가 아니라 신호에 찍히는 것과
같은 **미 동부 거래일**(`us_trading_day()`)입니다. 서버가 UTC이므로 ET 19~20시
이후에는 로컬 날짜가 하루 앞서갑니다. 일요일 저녁 cron이 `date.today()` 기준으로
돌면 다음 주를 조회해 빈 리포트를 내놓고, 출력만 봐서는 이유를 알 수 없습니다.

### 종료 코드

| 코드 | 의미 |
|---|---|
| `0` | 리포트 정상 출력 (데이터가 없는 기간도 정상) |
| `1` | 리포트를 만들지 못함 |
| `2` | **호출이 잘못됨** — 날짜 형식 오류, 역순 기간, 필수 인자 누락 |

`2`를 `1`과 분리한 이유는 crontab 오타를 실제 장애와 구분하기 위해서입니다.

날짜는 `YYYY-MM-DD` 로 정규화됩니다. 저장소가 날짜 키를 **문자열로 비교**하므로
`2026-8-1` 같은 미패딩 날짜는 정렬이 어긋납니다(`"2026-8-1" > "2026-08-05"`).
CLI가 정규화하지만, 직접 API를 호출할 때는 주의하세요.

### cron 예시

```cron
# 프리마켓 스캔 (09:20 ET)
20 9  * * 1-5  cd /home/ubuntu/trading && venv/bin/python scripts/run_scanners.py --profile premarket >> logs/scanners/cron_premarket.log 2>&1

# 장 시작 후 ORB / 갭 눌림 (09:50 ET)
50 9  * * 1-5  cd /home/ubuntu/trading && venv/bin/python scripts/run_scanners.py --profile open >> logs/scanners/cron_open.log 2>&1

# 일봉 기반 3종 (16:30 ET)
30 16 * * 1-5  cd /home/ubuntu/trading && venv/bin/python scripts/run_scanners.py --profile daily >> logs/scanners/cron_daily.log 2>&1

# 성과 추적 (17:30 ET)
30 17 * * 1-5  cd /home/ubuntu/trading && venv/bin/python scripts/run_scanner_performance.py >> logs/scanners/cron_perf.log 2>&1

# 주간 리포트 (금 18:00 ET)
0  18 * * 5    cd /home/ubuntu/trading && venv/bin/python scripts/run_scanner_report.py weekly >> logs/scanners/cron_weekly.log 2>&1
```

시간대는 서버 시간 기준으로 환산해서 넣으세요.

---

## 2.5 데이터 계보 (v1.1)

모든 신호에 다음이 기록됩니다. 한 달 뒤 "이 행은 어디서 온 데이터로, 언제,
어느 실행에서 판단된 것인가"를 저장된 행만 보고 답할 수 있어야 합니다.

| 필드 | 의미 | 예시 |
|---|---|---|
| `market_data_provider` | 봉을 제공한 vendor | `yfinance` |
| `market_data_feed` | vendor가 feed를 식별하는 경우에만. **추정 금지** | `null` |
| `data_timestamp` | 판단에 사용한 **최신 봉**의 timestamp (offset 포함) | `2026-08-12T09:49:00-04:00` |
| `feature_timestamp` | Feature 계산이 **끝난** 시각 (UTC) | `2026-08-12T13:49:03+00:00` |
| `scanner_run_id` | 한 번의 runner 실행 식별자 | `20260812_OPEN_9b13de` |
| `source_timeframe` | 그 스캐너의 **판단이 근거한** 봉 간격 | `1d` 또는 `1m` |

`data_timestamp`와 `feature_timestamp`의 간격이 stale data 실행을 드러냅니다.
09:50에 실행했는데 최신 봉이 09:31이면 그 사실이 데이터에 남습니다.

`source_timeframe`은 "그 스캐너가 읽은 모든 간격"이 아니라 **판단의 근거**입니다.
장중 스캐너도 HMA200·52주 고점은 일봉에서 읽지만, 판정은 장중이므로 `1m`입니다.

### Provider 이름 정리

실제 데이터 소스는 **Yahoo Finance**입니다. 구현 클래스는
`YahooFinanceMarketDataProvider`이고, `provider_name = "yfinance"`가 모든 신호에
기록됩니다.

이전 이름 `AlpacaMarketDataProvider`는 **deprecated alias**로만 남아 있습니다
(기존 import 호환용). 이 이름을 써도 저장되는 vendor는 `yfinance`입니다.
신규 코드에서는 사용하지 않으며, 테스트가 스캐너 패키지 내 실제 호출을 금지합니다.

`market_data_feed`는 Yahoo Finance가 feed를 알려주지 않으므로 `null`입니다.
확인하지 않은 feed명을 추정해서 넣지 않습니다.

> 참고: 저장소에 이전부터 있던 `market_data/alpaca_provider.py`(주문 경로 인접,
> 기존 테스트 4건·runbook 1건이 참조)는 이번 작업 범위에서 건드리지 않았습니다.
> 같은 명칭 불일치가 남아 있으나, 실주문 경로에 인접한 기존 모듈이므로 §1에 따라
> 그대로 두었습니다.

---

## 3. 저장 위치

| 내용 | 경로 | 환경변수 |
|---|---|---|
| 신호 (append-only) | `logs/scanners/signals/<날짜>.jsonl` | `SCANNER_ANALYTICS_DIR` |
| 성과 | `logs/scanners/performance/<날짜>.jsonl` | 〃 |
| 실행 기록 | `logs/scanners/runs/<날짜>.jsonl` | 〃 |
| 리포트 | `logs/scanners/reports/` | 〃 |
| CSV/JSON export | `logs/scanners/exports/` | 〃 |
| 스캐너별 로그 | `logs/scanners/<스캐너>.log` | `SCANNER_LOG_DIR` |

`SCANNER_ANALYTICS_DIR` 는 주문 후보 저장 위치와 **의도적으로 별개**입니다 (§10).
주문 후보는 저장소 루트의 `order_candidates.csv`(`daily_candidate_scanner.py` 가
생성, 주문 경로가 소비)이고, 공유 저장소를 쓰는 브랜치에서는 `KIS_CANDIDATE_DIR`
입니다. 어느 쪽이든 스캐너 분석 저장소와 같은 곳을 가리키게 설정하면 안 됩니다.

스캐너는 `order_candidates.csv` 를 **어떤 경로에도 쓰지 않습니다** — 분석 저장소는
`.jsonl` 만 씁니다. 테스트가 파일명과 디렉터리 양쪽을 검증합니다.

신호 파일은 append-only이며 하루가 끝나면 불변입니다. 성과는 별도 파일에
쌓이고, `signal_price` 는 절대 수정되지 않습니다. 모든 수익률·MFE·MAE가
그 값을 기준으로 계산되기 때문입니다.

---

## 4. v1.0 조건 (§11 — 한 달간 고정)

| 스캐너 | 버전 | 주요 조건 |
|---|---|---|
| `hma_early_trend` | `hma_early_trend_v1.0` | price > HMA200 / HMA200 상승 / HMA89 > HMA200 / ADX > 20 / ADX 상승 |
| `accumulation` | `accumulation_v1.0` | volume ≥ 1.5배 / price change ≤ +8% / price > HMA200 / HMA200 상승 |
| `breakout_ready` | `breakout_ready_v1.0` | 20일 고점까지 ≤ 5% / ADX > 20 / price > HMA200 / HMA200 상승 / HMA89 > HMA200 |
| `premarket_momentum` | `premarket_momentum_v1.0` | 기존 score_scanner 그대로 (score ≥ 60 / gain ≥ +7% / volume > 2배 / ADX > 25 / 52주 고점의 98% 이상) |
| `gap_pullback` | `gap_pullback_v1.0` | gap **+2% 이상 +8% 이하** / 세션 고가 대비 **1% 이상 60% 이하** 눌림 / 눌림 거래량 < 임펄스 거래량 / VWAP -1% 이내 유지 |
| `orb` | `orb_v1.0` | ORB **15분** / 종가 기준 돌파 후 유지 / price > VWAP / EMA9 > EMA21 / 거래량 **1.2배** 이상 확대 |

> 위 표의 `2~8%`류 표기가 이전 보고서에서 `28%`로 읽히는 문제가 있었습니다.
> **실제 설정값은 `scanners/gap_pullback/config.json`의 `gap_min_pct = 2.0`,
> `gap_max_pct = 8.0`이며 코드는 수정하지 않았습니다.** 경계값은 테스트
> (`test_scanner_session_boundaries.py`)로 고정되어 있습니다.

### 파라미터 이름 정리 (v1.1, 값 변경 없음)

의미가 모호했던 두 개의 키 이름만 바꿨습니다. **값·로직·버전은 그대로**입니다
(§10: 이름 변경은 임계값 변경이 아님).

| 이전 이름 | 새 이름 | 이유 |
|---|---|---|
| `max_pullback_volume_ratio` | `max_pullback_to_impulse_volume_ratio` | 어느 쪽이 분자인지 이름이 말하지 않았음. 실제로는 **눌림 / 임펄스** |
| `near_52w_ratio` | `week52_high_proximity_ratio` | `0.98`만으로는 의미를 알 수 없음. 실제로는 `price >= 52주고점 × 0.98`, 즉 **52주 고점의 2% 이내** |

각 config 파일에 `parameter_meanings` 블록이 있어 모든 파라미터의 계산식이
파일 안에 적혀 있습니다.

`max_pullback_from_high_pct = 60.0`은 **의도적으로 느슨한 backstop**입니다.
실제로는 `vwap_tolerance_pct = 1.0`이 훨씬 먼저 걸러내므로 이 값이 단독으로
거절하는 경우는 사실상 없습니다. Month 1에서 조정하지 않습니다 (§31).

파라미터는 각 `scanners/<이름>/config.json` 에 있습니다.
**값을 바꾸면 같은 파일의 `version` 도 반드시 바꾸세요** (§19).

잊어버려도 흔적은 남습니다. 모든 신호에 파라미터 값의 해시(`config_fingerprint`)가
기록되므로, 버전을 안 바꾸고 값만 고쳐도 주간·월간 리포트가 경고를 띄웁니다.

### 설정 형식이 YAML이 아니라 JSON인 이유

지시문 §19는 예시를 YAML로 들었지만, 실제로는 JSON으로 넣었습니다.
이 환경에 PyYAML이 설치되어 있지 않고 `requirements.txt` 에도 없습니다.
실주문을 내는 시스템에 새 서드파티 의존성을 추가하는 것보다,
이 저장소가 이미 같은 목적으로 쓰고 있는 형식(`config/scanner_rules.json`,
`config/scanner_presets.json`)을 따르는 편이 안전하다고 판단했습니다 (§25).
파라미터 이름·구조·값은 지시문과 동일합니다.

---

## 4.5 지표 정의 (§20 — 기억에 의존하지 않기 위해)

한 달 뒤 "이 컬럼이 정확히 무엇이었나"를 코드를 읽지 않고 알 수 있어야 합니다.
공통 지표 파라미터는 `scanners/base/config.json`에 있습니다
(`common_features_v1.0`).

| 지표 | 정의 | Timeframe |
|---|---|---|
| `hma89` | 종가의 Hull MA, length 89. `indicators.hma` (실주문 기술필터와 동일 함수) | **1d** |
| `hma200` | 종가의 Hull MA, length 200 | **1d** |
| `hma200_slope` | `(HMA200[t] / HMA200[t-5] - 1) × 100`. lookback 5봉, 퍼센트, 부호 있음 | **1d** |
| HMA 최소 봉 수 | `length + floor(sqrt(length)) - 1` (HMA200 → 213봉) + slope lookback 5 = **218봉** | 1d |
| `hma89_cross_hma200_recent` | HMA89가 HMA200 위로 교차한 지 20봉 이내인가 (기록 전용, 필터 아님) | 1d |
| `adx` | 14기간 ADX. `score_scanner`의 기존 `calculate_adx`와 **동일 계산** (테스트로 고정) | **1d** |
| `adx_rising` | `ADX[t] > ADX[t-1]`. 값이 하나뿐이면 `None`이고 **None은 거절**로 처리 | 1d |
| `avg_volume` | 최근 20봉 평균 거래량, **당일 봉 제외** | **1d** |
| `volume_multiple` | `당일 거래량 / avg_volume`. 분모 0이면 `None` (절대 `inf` 아님) | 1d |
| `high_20d` / `high_50d` / `high_52w` | 최근 20 / 50 / 252봉 최고가, **당일 봉 제외** | **1d** |
| `distance_*_high` | `(고점 - 현재가) / 고점 × 100`. **양수 = 고점 아래**, 음수 = 이미 돌파 | 1d |
| `extension_*_pct` | `(price / 기준 - 1) × 100` (§8). 기록·점수 전용, 필터 아님 | 해당 스캐너의 timeframe |
| `ema9` / `ema21` | 장중 종가의 EMA, span 9 / 21 | **1m** (장중 스캐너) |
| `vwap` | **세션별로 초기화**되는 누적 VWAP. typical price `(H+L+C)/3` 가중 | **1m**, 정규장만 |
| `gap_pct` | `(세션 시가 / 직전 거래일 종가 - 1) × 100`. 직전 종가는 세션일보다 **이전** 마지막 일봉 | 1m + 1d |
| `pullback_from_high_pct` | `(세션 고가 - 현재가) / 세션 고가 × 100` | 1m |
| `pullback_volume_ratio` | **눌림 구간** 평균 bar 거래량 / **임펄스 구간** 평균 bar 거래량. 세션 고가 bar는 임펄스에 포함 | 1m |
| Opening Range | 세션 **첫 봉**부터 `orb_minutes`분. ORB15 = 09:30~09:44 라벨 봉 **15개**. 09:45 봉은 range 이후 | 1m |
| `breakout_confirmed` | range high 위에서 **종가 마감**한 봉이 있는가 | 1m |
| `breakout_touched` | range high를 **고가로 뚫은** 봉이 있는가 (wick) | 1m |
| `retest_confirmed` | 돌파 → 이후 봉이 range high 근처(±0.3%)까지 되돌림 → 그 뒤 봉이 다시 위로 마감. **순서대로** | 1m |
| `premarket_gain_pct` | S4는 기존 score_scanner 계산 그대로: `(최신 장중가 / 직전 일봉 종가 - 1) × 100` | 1m + 1d |

**봉 라벨 규칙**: 1분봉은 **시작 시각**으로 라벨됩니다. `09:30` 봉은
09:30:00~09:30:59를 덮습니다. 그래서 09:30~09:45 opening range는
`09:30`~`09:44` 라벨 봉 15개이고, `09:45` 봉은 range 다음 첫 봉입니다.

**MFE / MAE 측정 창**: 신호 시각부터 신호일 이후 N번째 세션 종가까지.
`[신호 시각 ~ 신호일 종가]`는 1분봉에서, `[D+1 ~ D+N]`은 일봉 고가/저가에서
가져와 합칩니다. 신호일 **당일 일봉은 제외**합니다 — 그 봉의 고가에는 스캐너가
말하기 **전**의 움직임이 포함되어 있기 때문입니다.

---

## 5. 지금 하지 않는 것

- **주문 후보 발행**: Candidate Decision Layer(`scanners/candidate_decision.py`)는
  구현되어 있고 테스트도 되어 있지만 `enabled: false` 로 출고됩니다.
  `publish()` 는 이유를 밝히며 거부합니다. 한 달 검토 후 별도로 결정할 일입니다 (§30).
- **교집합을 진입 조건으로 사용**: `confirmation_count` 는 기록·분석만 하며
  랭킹에 쓰지 않습니다 (§17, §18).
- **Extension 하드 필터**: 기록하고 점수에만 반영합니다. 지금 컷을 정하면
  "extension이 높으면 성과가 나쁜가?"라는 §22의 질문에 답할 근거 자체가
  수집되지 않습니다 (§8).
- **Trading 성과 계산**: 월간 리포트는 Profit Factor 등을 계산하지 않고
  "해당 없음"과 그 이유를 출력합니다. 신호 수익률로 계산하면 아무도 실행하지
  않은 전략의 성과표가 만들어집니다 (§14, §16).

---

## 6. 문제가 생겼을 때

**스캐너 하나가 실패해도 나머지는 계속 실행됩니다.** 세 겹으로 격리됩니다.

| 실패 | 결과 |
|---|---|
| 심볼 하나의 봉을 못 받음 | 그 심볼만 건너뜀 (모든 스캐너) |
| 심볼 하나에서 예외 | 그 스캐너의 그 심볼만 건너뜀 |
| 한 스캐너가 25개 심볼 연속 실패 | 그 스캐너만 중단 처리, 나머지는 완주 |
| 저장 실패 | 그 스캐너만 실패 표시, 나머지는 정상 저장 |

스캐너가 실패하면 종료 코드가 0이 아니므로 cron/systemd에서 감지됩니다.
단, **다른 스캐너들의 결과를 저장한 뒤**에 종료합니다.

### 후보 0건과 파이프라인 장애는 절대 같지 않습니다 (§14)

`logs/scanners/runs/<날짜>.jsonl`에 실행마다 기록됩니다.

| 상황 | `run_status` | `candidate_count` |
|---|---|---|
| 정상 실행, 조건 맞는 종목 없음 | `SUCCESS` | `0` (실측값) |
| 일부 스캐너만 실패 / 유니버스 절반 이상 fetch 실패 | `PARTIAL` | 실측값 |
| 모든 심볼 fetch 실패 | `FAILED_PROVIDER` | `null` |
| 모든 스캐너 실패 | `FAILED` | `null` |
| 스캐너를 하나도 생성 못함 | `FAILED_NO_SCANNER` | `null` |
| universe.csv 없음/손상 | `FAILED_NO_UNIVERSE` | `null` |
| 미국 증시 휴장 | `SKIPPED_MARKET_CLOSED` | `null` |

**장애 시 `candidate_count`는 `0`이 아니라 `null`입니다.** `0`은 "찾아봤는데
없었다"는 주장이고, 그 주장은 끝까지 실행된 스캔만 할 수 있습니다.

Circuit breaker 상태도 같은 파일에 남습니다:
`provider_error_count`, `consecutive_error_peak`, `circuit_breaker_triggered`,
`circuit_breaker_reason`.

휴장일에도 `SKIPPED_MARKET_CLOSED` 기록을 남깁니다 — 그래야 신호 파일에 없는
날짜가 "휴장"인지 "cron이 안 돌았다"인지 구분됩니다.

### 성과 추적 상태 (§23)

`logs/scanners/performance/<날짜>.jsonl`의 각 horizon에 상태가 붙습니다.

| 상태 | 의미 | 조치 |
|---|---|---|
| `complete` | 측정됨 | 없음 |
| `pending` | 아직 기간이 지나지 않음 | 기다리면 채워짐 |
| `expired` | 1분봉 보존기간(약 7일)이 지나 영구히 계산 불가 | 포기 |
| `data_unavailable` | 봉이 있어야 하는데 못 받음 | 재시도 가능 |

**이미 계산된 값은 재실행이 절대 null로 덮어쓰지 않습니다.** 병합 규칙은
"null → 값"과 "값 → 다른 값"은 허용, "값 → null"은 금지입니다.
(수정 전에는 8일째 재실행이 0일째 계산한 `return_30m`을 null로 지웠습니다.)

로그에서 왜 걸렀는지 보려면:

```bash
grep 'result=FAIL' logs/scanners/breakout_ready.log | tail -50
```

FAIL 로그는 DEBUG 레벨입니다 (800종목 × 6스캐너면 INFO로는 PASS가 묻힙니다).
필요하면 로깅 레벨을 내려서 실행하세요.

---

## 7. 개선 사이클 (§21)

```
Month 1  Discovery    조건 고정, 데이터 수집만
Month 2  Calibration  성과 분석 후 제한적 조정 (§20: 20 / 25, 1.5x / 2x / 3x 같은 의미 있는 단위로만)
Month 3  Validation   조정된 조건을 다시 건드리지 않고 독립 검증
```

AI 분석은 export한 CSV/JSON을 대상으로만 하고, 결과가 실전 설정에 자동
반영되지 않습니다. 반드시 `분석 → 제안 → 백테스트 → 검증 → 승인 → 새 버전`
순서를 따릅니다 (§22).
