# Manual Watchlist (MANUAL_ONLY)

사람이 직접 읽고 직접 판단하기 위한 목록이다. **자동 주문 경로와 연결되어 있지 않고,
연결될 수 없다.** 산출물은 파일 하나와 Slack 메시지 하나뿐이다.

```
Scanner Signals ──> Watchlist Builder ──> Ranking ──> File ──> Slack ──> 사람
```

연결 금지 대상 (구조적으로 차단, `tests/test_watchlist_isolation.py`):

| | |
|---|---|
| Candidate Decision / Candidate Store | import 0 (양방향) |
| Risk / Sizing / Execution / Broker / KIS | import 0 |
| order / paper order | import 0 |
| 파일 쓰기 | `logs/watchlist/` 외 0 |

격리는 두 방향 모두 검증한다. `watchlist → 주문` 만 막는 것으로는 부족한데,
바깥에서 `import watchlist` 하는 순간 이 목록은 그대로 주문 입력이 되기
때문이다. 세 가지 변조(주문 모듈 import / candidate store import / 주문
모듈이 watchlist import)를 실제로 넣어 테스트가 잡는 것을 확인했다.

## 2단계 구조

```
[D일 장마감 후]  S1·S2·S3 Daily 완료
                    ↓
                 Tomorrow Watchlist   →  파일만 저장, Slack 없음
                                          logs/watchlist/<D+1>.tomorrow.json/.md

[D+1일 프리마켓]  S4 Premarket 완료
                    ↓
                 Tomorrow Watchlist 재평가 (S4 확인)
                    ↓
                 Today Watchlist      →  파일 + Slack 1회
                                          logs/watchlist/<D+1>.today.json/.md
```

파일은 **목록이 대상으로 하는 날짜**로 저장된다. 월요일 저녁에 화요일용으로
만든 목록은 화요일 파일이므로, 아침 패스는 "어느 세션이 만들었는지" 역산할
필요 없이 `<오늘>.tomorrow.json` 을 읽으면 된다.

**저녁 패스가 조용한 이유**: ET 18:45 메시지는 아무도 그날 행동하지 않고,
아침이 되면 프리마켓 스캔이 이미 그림을 바꿔 놓는다. 그래서 저녁은 근거를
적어 두고, 말하는 쪽은 아침이다.

**아침 패스는 종목을 추가하지 않는다.** S4가 잡았지만 전날 아무 데일리
스캐너도 잡지 않은 종목은 오버나이트 근거가 없고 오늘 아침의 갭뿐이다.
그런 종목까지 넣으면 큐레이션된 목록이 다시 원시 신호 피드가 된다.

| 스캐너 | 역할 |
|---|---|
| S1 `hma_early_trend` | 저녁 목록의 소스 |
| S2 `accumulation` | 저녁 목록의 소스 |
| S3 `breakout_ready` | 저녁 목록의 소스 |
| S4 `premarket_momentum` | 아침 **확인만** (추가 금지) |
| S5 `gap_pullback` / S6 `orb` | 장중 행동 관측·성과 분석 전용, **랭킹 미반영** |

## manual_watch_score

Scanner score / threshold 는 **변경하지 않는다.** 이 점수는 스캐너가 이미
PASS/REJECT 를 끝낸 뒤에, 저장된 필드만으로 계산하는 **정렬용 후처리**다.

```
스캐너가 정하는 것 :  PASS / REJECT      ← Month 1 동안 고정
이 계층이 정하는 것 :  1위 / 2위 / 3위     ← PASS 된 것들 사이에서만
```

구성 (`watchlist/config.py`, `manual_watch_v1`):

| 항목 | 가중치 |
|---|---|
| 데일리 스캐너 교차 수 | 30 |
| 최고 scanner_score | 25 |
| S1 early trend | 15 |
| S2 accumulation | 10 |
| S3 breakout ready | 10 |
| S4 프리마켓 확인 | 10 |
| 과열 페널티 | −20 |

동점은 심볼 오름차순으로 깬다. 이게 없으면 같은 점수 종목의 순서가 저장소가
내주는 순서에 따라 달라지고, 어제 파일과 오늘 파일을 diff 할 수 없게 된다.

## 과열 표시

`extension_hma200_pct`, `extension_hma89_pct`, `price_change_pct`,
`distance_52w_high` 를 사용해 `overextended` 를 **표시**한다.

**필터가 아니다.** 스캐너가 PASS 라고 했으면 목록에 남고, 순위만 내려가고
표시가 붙는다. "이미 많이 갔다"는 코드가 대신 결정할 것이 아니라 읽는 사람이
봐야 할 정보다.

52주 고점 근접 하나만으로는 과열이 아니다. 이동평균 근처에서 신고가를 만드는
것은 돌파이고, 그건 이 스캐너들 여럿이 찾도록 만들어진 바로 그 형태다.

## 실행

```bash
# 저녁 패스 (파일만)
scripts/run_manual_watchlist.py tomorrow

# 아침 패스 (파일 + Slack TOP 5)
scripts/run_manual_watchlist.py today --slack

# TOP 10 (상한)
scripts/run_manual_watchlist.py today --slack --top 10

# 저장 없이 확인만
scripts/run_manual_watchlist.py today --no-write
```

| 출력 | 개수 |
|---|---|
| 저장 파일 | 상위 20 표시, 최대 200 보관 |
| Slack | 기본 5, 최대 10 |

모든 출력에 `[Manual Watchlist] 수동 검토용 / 자동주문 아님` 배너가 붙는다.
장식이 아니다. 이 목록은 매수 목록과 똑같이 생겼고, 언젠가 실제로 주문을
만들 수도 있는 같은 스캐너가 만들었으며, 주문 알림 바로 옆 채널에 도착한다.
구분해 주는 것은 매번 그렇게 적혀 있다는 사실뿐이다.

### 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 워치리스트 생성 (비어 있어도 정상) |
| 1 | 생성 실패 (파일 쓰기 등) |
| 2 | 잘못된 호출 (날짜 형식, 알 수 없는 stage) |

### cron

```
# Tomorrow Watchlist — ET 18:45, daily 스캔과 성과추적 이후
45 22,23 * * 1-5 [ "$(TZ=America/New_York date +\%H)" = "18" ] && cd /home/ubuntu/trading && \
  flock -n logs/cron/watchlist_tomorrow.lock env TRADING_PROJECT_ROOT=/home/ubuntu/trading \
  venv/bin/python scripts/run_manual_watchlist.py tomorrow >> logs/cron/watchlist.log 2>&1

# Today Watchlist — ET 09:35, premarket 스캔 이후
35 13,14 * * 1-5 [ "$(TZ=America/New_York date +\%H)" = "09" ] && cd /home/ubuntu/trading && \
  flock -n logs/cron/watchlist_today.lock env TRADING_PROJECT_ROOT=/home/ubuntu/trading \
  venv/bin/python scripts/run_manual_watchlist.py today --slack >> logs/cron/watchlist.log 2>&1
```

두 줄 모두 기존 스캐너 cron 과 같은 패턴이다: UTC 두 시각에 발화시키고
ET 시(hour) 가드로 하나만 통과시킨다 (Ubuntu cron 은 `CRON_TZ` 미지원).
ET 09:35 / 18:45 는 모두 ET 18:59 이하이므로 EDT·EST 어느 쪽에서도 UTC
날짜와 요일이 밀리지 않는다.

## 환경 변수

| 변수 | 기본값 | 용도 |
|---|---|---|
| `MANUAL_WATCHLIST_DIR` | `<root>/logs/watchlist` | 저장 위치 |
| `SCANNER_SLACK_ENABLED` | (미설정=활성) | `false` 면 Scanner 계열 발송 전체 중단 |
| `SLACK_WEBHOOK_URL` | — | 기존 리포트 웹훅 재사용 (신규 생성 없음) |
