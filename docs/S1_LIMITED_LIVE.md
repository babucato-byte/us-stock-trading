# S1 Limited Live — PHASE 3 (Candidate Source)

현재 상태:

```
S1 hma_early_trend  = DISCOVERY_ONLY + LIMITED_LIVE_CANDIDATE_SOURCE_READY
S2~S6               = DISCOVERY_ONLY
LIVE_ROLLOUT_ENABLED = false
Candidate Decision   = false
실제 주문             = 0
```

PHASE 3이 만든 것은 **후보 집합을 발행하고 검증하는 경로**뿐이다.
주문을 만드는 것은 아무것도 없다.

## 후보 ≠ 매수

`s1_live_candidates.csv`에 심볼이 올라왔다는 것의 의미는 정확히 하나다:

> "오늘 이 심볼은 **추가 검증 대상이 될 수 있다**."

이 심볼은 아직 다음 중 **어느 것도** 통과하지 않았다 — 전부 하류에 있고, 전부 통과해야 주문이 존재한다.

```
signal freshness · 실시간 현재가 · extension · 계좌 현금 · allocator
max positions · daily loss · drawdown · re-entry cooldown
Kill Switch · reconciliation · duplicate order · Order Gate · Execution Engine
```

이 중 allocator / daily loss / drawdown / cooldown 은 **아직 구현되지 않았다** (PHASE 4~5).

## Scanner Mode

`config/scanner_live_mode.py`:

| scanner | mode |
|---|---|
| `hma_early_trend` | **LIMITED_LIVE** |
| `accumulation` | DISCOVERY_ONLY |
| `breakout_ready` | DISCOVERY_ONLY |
| `premarket_momentum` | DISCOVERY_ONLY |
| `gap_pullback` | DISCOVERY_ONLY |
| `orb` | DISCOVERY_ONLY |

`limited_live_scanner()`는 **정확히 1개**가 아니면 raise한다. 0개도 raise다 —
0개는 안전하지만, 조용히 빈 후보 파일을 쓰는 publisher는 "한산한 날"과
구분되지 않고 그 둘은 운영자 대응이 다르다.

환경변수가 아니라 코드 상수인 이유: env var는 새벽 2시에 서버에서 고쳐진다.
이건 그렇게 바뀌어서는 안 되는 설정이다.

## Publisher

```
logs/scanners/signals/<day>.jsonl   (READ ONLY)
        │  scanner_name == 단일 LIMITED_LIVE scanner
        │  scanner_run_id == 그날 성공한 최신 S1 run
        │  scanner_score 내림차순, 동점은 symbol 오름차순
        │  상위 MAX_S1_LIVE_CANDIDATES (기본 10)
        ▼
shared/state/s1_live_candidates.csv
shared/state/s1_live_candidates.manifest.json
```

**Publisher가 하지 않는 것** — 가격·거래대금·시가총액·산업·추가 기술지표 등
어떤 새 threshold도 넣지 않는다. 넣는 순간 한 달간 측정한 전략과 실제
거래되는 것 사이에 문서화되지 않은 두 번째 전략이 끼어들고, Month 1 기록이
실제로 거래되는 것을 더 이상 설명하지 못하게 된다.

레버리지/인버스/거래불가 종목 차단도 여기 없다 — `domain/instrument.py`와
브로커 게이트가 이미 보장하고, 복제하면 두 곳이 서로 다른 답을 낼 수 있다.

`MAX_S1_LIVE_CANDIDATES`는 **주문 수 상한이 아니다.** 주문 수는
`LIVE_ROLLOUT_MAX_POSITIONS`(=1)와 `LIVE_ROLLOUT_MAX_DAILY_ENTRIES`(=1)가 정한다.

### Provenance

그날 S1이 **실제로 성공한 run manifest**가 있을 때만 발행한다. 신호는
부분 완료되거나 대체된 run에서도 남을 수 있고, 그런 신호로 만든 후보 집합은
갖지 않은 provenance를 주장하게 된다. 대체된 run의 신호는
`scanner_run_id`로 걸러진다.

## Manifest

| 필드 | 용도 |
|---|---|
| `schema_version` | 포맷 변경 감지 |
| `generated_at` | 발행 시각 |
| `trading_day` | **staleness 판정 기준** |
| `source_scanner` | S1 외 소스 차단 |
| `scanner_version` | 전략 버전 고정 |
| `scanner_run_id` | 어느 스냅샷인지 |
| `config_fingerprint` | 파라미터 무변경 증명 |
| `market_data_provider` | 데이터 출처 |
| `candidate_count` | 행 수 교차검증 |
| `payload_sha256` | **CSV 바이트 무결성** |

mtime은 freshness가 아니다 — 복사·복원·릴리스 롤아웃에도 살아남는다.
그래서 manifest가 CSV 바이트의 sha256을 기록하고 `load()`가 재계산한다.
손으로 고친 CSV, 어제 남은 CSV, 크래시로 반만 쓰인 CSV가 전부 같은 검사에 걸린다.

**발행 순서: CSV 먼저, manifest 나중.** 중간 상태를 읽은 소비자는
`새 CSV + 옛 manifest`를 보고 해시·날짜 불일치로 거부한다. 반대 순서였다면
`새 manifest + 옛 CSV`가 되고, 그건 소비자가 **믿어 버리는** 상태다.

## Dynamic Allowlist

검증에 성공한 당일 후보 집합이 그날의 1차 allowlist가 된다.

**다음 중 하나라도 해당하면 allowlist는 빈 집합이다:**

```
파일 없음 · manifest 없음 · payload sha256 불일치 · trading_day 불일치
scanner_run_id 불일치 · provider 불일치 · source_scanner 불일치
schema_version 불일치 · manifest 필수키 누락 · CSV 헤더 변경
malformed row · 중복 심볼 · candidate_count 불일치
LIMITED_LIVE scanner 개수 != 1 · shared store 미해결
```

**부분 성공이 없다.** 일부 행만 파싱된다고 그 행들을 쓰지 않는다 —
행이 깨졌다는 것은 publisher가 쓴 파일이 아니라는 뜻이고, 출처를 알 수 없는
후보 파일에 대한 정직한 응답은 "후보 없음"이다.

빈 allowlist → 전 심볼 거부는 **기존 동작 그대로**다. 그래서 fail-closed
경로에 새 게이트를 만들 필요가 없었다.

### 운영자 override는 조이기만 한다

`LIVE_ROLLOUT_ALLOWED_SYMBOLS`가 설정되어 있으면 S1 집합과 **교집합**을 쓴다.
목록을 적어 둔 운영자는 그걸 의도한 것이고, 이 코드베이스의 관례
(`order_gateway`의 현금 `min()`)는 신뢰 설정이 계산값을 조일 수만 있다는 것이다.
비어 있으면(기본값) S1 집합이 단독으로 쓰인다 — 동적 소스가 동작할 수 있는 유일한 방법.

> ⚠️ 이것은 "사람이 사전 승인한 종목만 거래한다"는 기존 성질을 바꾼다.
> PHASE 3은 그 구조를 만들었을 뿐이고, `S1_LIVE_SOURCE_ENABLED`가 꺼져 있는 한
> 실제로 사용되지 않는다.

## Candidate Source 추상화

```
CandidateSource
  ├─ LegacyWatchlistSource  pso.load_watchlist() / rollout.allowed_symbols
  └─ S1CandidateSource      검증된 당일 후보 / 같은 집합
```

`kis_live_trading.run_live_buy_entry_cycle()`은 **하나뿐이다.** 소스만 교체되고
Order Gate · entry limits · idempotency · Kill Switch · reconciliation ·
가격 재검증 · Execution Engine은 전부 공유된다.

두 번째 파이프라인을 만들지 않는 이유: 파이프라인 두 개는 "무엇이 안전한가"에
대한 생각 두 개이고, 그 둘은 조용히 벌어지며, 벌어진 것은 운영에서 발견된다.

기본값은 legacy다. `S1_LIVE_SOURCE_ENABLED`를 명시적으로 켜지 않으면
기존 경로가 심볼 단위로 동일하게 동작한다 (테스트로 단언).

### 알려진 성질 (PHASE 4에서 재검토)

기존 파이프라인 본문을 바꾸지 않았으므로, S1 후보도 기존
`pso.analyze_stock()` + `SCORE_THRESHOLD=70`을 통과해야 한다. 이는 **추가
제약**(더 보수적)이라 안전하지만, S1 점수와 별개의 옛 스코어링이라
PHASE 4에서 이 계층을 어떻게 다룰지 별도 결정이 필요하다.

## 실행

```bash
scripts/run_s1_publisher.py                       # 오늘
scripts/run_s1_publisher.py --trading-day 2026-08-17
scripts/run_s1_publisher.py --limit 5
scripts/run_s1_publisher.py --dry-run             # 출력만, 발행 없음
```

| 종료 코드 | 의미 |
|---|---|
| 0 | 발행됨 (빈 집합도 정상) |
| 1 | 거부 — 성공한 S1 run 없음 / LIMITED_LIVE 개수 != 1 / store 미해결 |
| 2 | 잘못된 호출 — 날짜 형식, 음수 limit |

## 환경 변수

| 변수 | 기본 | 의미 |
|---|---|---|
| `S1_LIVE_CANDIDATE_DIR` | `<root>/../shared/state` | 후보 파일 위치 |
| `S1_LIVE_SOURCE_ENABLED` | `false` | 후보 **소스** 전환. **주문을 켜지 않는다** |

`S1_LIVE_SOURCE_ENABLED=true`로도 주문은 발생하지 않는다. 주문은 여전히
`KIS_LIVE_ORDER_ENABLED` + `LIVE_ROLLOUT_ENABLED` + `ENTRY_DISABLED` 세 플래그가
정하며, PHASE 3에서 이 값들은 하나도 바뀌지 않았다.

## 아직 구현하지 않은 것 (PHASE 4+)

```
35/30/25 allocation · cash 100% sizing · max positions 4 · daily entries 5
daily loss gate · drawdown gate · re-entry cooldown · fee/net PnL accounting
S1 전용 exit 정책 · 주문 시점 freshness 수치 · live activation
```

특히 **S1 exit 정책**은 미결이다. 현재 배선된 청산 정책은 스캘핑용
(`MAX_POSITION_HOLD_MINUTES=60`, target_2 = 2R = +16% on a −8% stop)이고
일봉 HMA 추세 신호의 holding horizon과 맞지 않는다. Month 1 데이터가 쌓인 뒤
MFE/MAE와 실제 보유 거동으로 도출해야 한다.
