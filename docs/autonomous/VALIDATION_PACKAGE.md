# VALIDATION_PACKAGE

외부 검증자(ChatGPT/Codex)가 `CODEX_REVIEW.md`를 작성하기 위해 필요한 정보 패키지. Phase 완료 시마다 갱신한다.

---

## 이번 패키지: Phase 1 추가 수정 사이클 — CODEX-007~009 (2026-07-21)

### 검증 대상
- 이전 리뷰: `CODEX_REVIEW.md`, 대상 커밋 `9688a13`/`b93a08a`/`22a6651`/`962eb69`/`1cc784b`, **overall verdict FAIL**. CODEX-003/004/005 RESOLVED 확인, CODEX-001/002/006 PARTIALLY_RESOLVED로 되돌림, 신규 CODEX-007(HIGH)/008(HIGH)/009(MEDIUM) 제기.
- 이번 패키지가 다루는 수정 커밋: `05757fe`(CODEX-007), `0c2dab4`(CODEX-008), `16a1ee4`(CODEX-009).
- Phase 상태: `IN_PROGRESS` (부분 체결의 "포지션 상태" 완전 반영은 Phase 5 선행 필요 — CODEX Finding 아님).

### Finding 처리 결과

| ID | 심각도 | 이전 판정 | 이번 처리 | 커밋 |
|---|---|---|---|---|
| CODEX-001 | HIGH | PARTIALLY_RESOLVED | RESOLVED (CODEX-009로 흡수) | `16a1ee4` |
| CODEX-002 | HIGH | PARTIALLY_RESOLVED | RESOLVED (CODEX-007로 흡수) | `05757fe` |
| CODEX-003 | HIGH | RESOLVED | RESOLVED (변경 없음) | `b93a08a` |
| CODEX-004 | MEDIUM | RESOLVED | RESOLVED (변경 없음) | `962eb69` |
| CODEX-005 | HIGH | RESOLVED | RESOLVED (변경 없음) | `962eb69` |
| CODEX-006 | HIGH | PARTIALLY_RESOLVED | RESOLVED (CODEX-008로 흡수) | `0c2dab4` |
| CODEX-007 | HIGH (신규) | — | RESOLVED | `05757fe` |
| CODEX-008 | HIGH (신규) | — | RESOLVED | `0c2dab4` |
| CODEX-009 | MEDIUM (신규) | — | RESOLVED | `16a1ee4` |

CRITICAL 0건, 미해결 HIGH 0건, 미해결 MEDIUM 0건. 상세는 `REMEDIATION_PLAN.md` 참고.

### 변경 파일

- `paper_strategy_order.py` — `validate_order_date_str()`, `diagnose_order_history_dates()`, `load_order_history()`의 엄격한 날짜 검증(CODEX-007); `_file_lock()` 일반화, `ReconciliationUnavailable`, `merge_reconciliation_state()`, `_status_should_apply()`, `_update_reconciliation_row()`, `_reconciliation_lock`, `reconcile_pending_orders()`/`_update_reconciliation_from_response()` 재작성(CODEX-008)
- `broker/alpaca_client.py` — `get_assets()` 추가(CODEX-009)
- `universe_builder.py` — `AlpacaBroker.get_assets()` 기반으로 전면 재작성, `if __name__ == "__main__":` 가드(CODEX-009)
- `tests/test_paper_order_execution.py` — CODEX-007 테스트 8건, CODEX-008 테스트 15건 추가
- `tests/test_universe_builder.py` (신규) — CODEX-009 테스트 15건
- `docs/autonomous/{REMEDIATION_PLAN,VALIDATION_REPORT,CURRENT_STATUS,SCALPING_V1_ROADMAP,DECISION_LOG}.md`, `CODEX_REVIEW.md`(리뷰 원문 보존, 삭제 없음)

### 핵심 diff (요약)

```python
# paper_strategy_order.py::load_order_history (CODEX-007)
- pd.to_datetime(df["order_date"], errors="raise")   # 파싱만 확인, 정규 형식 강제 없음
+ for row_index, raw_value in df["order_date"].items():
+     validate_order_date_str(raw_value)  # 정규식 + 실제 날짜 + 원본 왕복 일치

# paper_strategy_order.py::merge_reconciliation_state (CODEX-008, 신규)
def merge_reconciliation_state(existing, incoming):
    if _status_should_apply(existing.get("local_status"), incoming.get("local_status")):
        merged["local_status"] = incoming["local_status"]  # 후퇴/UNKNOWN의 FILLED 덮어쓰기 차단
    merged["filled_qty"] = max(existing_filled, incoming_filled)  # 비감소
    if incoming.get("average_fill_price") not in (None, ""):
        merged["average_fill_price"] = incoming["average_fill_price"]  # 비소거

# universe_builder.py (CODEX-009)
- BASE_URL = os.getenv("ALPACA_PAPER_BASE_URL") or os.getenv("ALPACA_BASE_URL") or "..."
- response = requests.get(f"{BASE_URL}/v2/assets", headers=headers, timeout=20)  # 안전검사 없음
+ broker = broker or AlpacaBroker()
+ assets = broker.get_assets()  # _request()의 validate_order_allowed() 게이트 재사용
```

전체 diff는 `git show 05757fe`, `git show 0c2dab4`, `git show 16a1ee4`로 확인 가능.

### 실행 명령 및 결과

```bash
# 저장소 루트
venv/bin/pytest -q                              # 149 passed, 2 warnings
venv/bin/python -m pytest -q                    # 149 passed, 2 warnings

# 저장소 상위 디렉터리, 경로 명시
cd ..
us-stock-trading/venv/bin/pytest -q us-stock-trading            # 149 passed, 2 warnings
us-stock-trading/venv/bin/python -m pytest -q us-stock-trading  # 149 passed, 2 warnings

# 집중 테스트
venv/bin/pytest -q tests/test_broker_safety.py tests/test_paper_order_execution.py tests/test_universe_builder.py
# 106 passed, 1 warning

# 동시성 테스트 안정성(5회 반복, threading + multiprocessing 포함)
venv/bin/pytest -q tests/test_paper_order_execution.py -k "concurrent or lock_acquisition or multiprocessing"
# 매회 6 passed — 5/5 안정

# 정적 검증
git diff --check eef3a13 HEAD   # 통과, 출력 없음
grep -rn "requests.get\|requests.post\|ALPACA_.*_BASE_URL\|api.alpaca.markets" --include="*.py" .
  # broker/ 외 Alpaca 직접 호출은 collect_ignore 대상 스크래치 파일 2개뿐
md5 order_history.csv           # a61104cf03499860ae89d4e194dc8c07 — 사이클 전후 동일
```

warning 2건은 기존 urllib3/LibreSSL 환경 경고와 unknown scanner field 테스트 경고로 이번 변경과 무관.

### 안전 재검증

- 실제 Alpaca/Slack API 호출 없음: 모든 broker 상호작용은 FakeBroker/DummySession/RecordingSession/monkeypatch. `universe_builder` 테스트도 동일 패턴.
- Live Trading 활성화 없음. Live URL은 상수 정의와 "차단되어야 함"을 검증하는 부정 테스트에만 등장.
- 운영 서버 접속/설정 변경 없음. systemd/cron/nginx 미변경.
- `origin/main`에 push하지 않음(로컬 커밋만).
- API Key/Secret/Slack Webhook 신규 노출 없음.
- 기존 리스크 한도/전략 로직 미변경. `order_history.csv` 컬럼 스키마 미변경(신규 추적은 별도 파일).
- 테스트 중 실제 `order_history.csv`/`order_reconciliation.csv`/`universe.csv` 변경 없음(전부 `tmp_path`).

### 운영 영향

- 없음(즉시). 참고 사항:
  - `universe_builder.py`가 이제 `AlpacaBroker`를 통해 인증하므로, 운영 서버 `.env`에 `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`가 이미 설정돼 있다면 그대로 동작한다. 레거시 `ALPACA_BASE_URL` 환경변수는 더 이상 이 스크립트에 영향을 주지 않는다(표준 `ALPACA_PAPER_BASE_URL` 또는 기본값 사용) — 운영 `.env`가 `ALPACA_BASE_URL`만으로 파이프라인을 우회하고 있었다면 이제는 표준 Paper URL로 안전하게 고정된다(동작 개선, 회귀 아님).
  - 기존 `order_history.csv`에 비정규 `order_date` 값이 있다면(수동 편집 등) 다음 실행에서 전체 이력이 `CORRUPTED_HISTORY`로 판정되어 신규 주문이 차단된다. 배포 전 `diagnose_order_history_dates()`로 운영 파일을 점검 권장(자동 마이그레이션 없음, 의도된 동작).

### 남은 위험

- `order_history.csv`와 `order_reconciliation.csv`는 각자 원자적/잠금이지만 두 파일에 걸친 단일 트랜잭션은 없음. 안전 크리티컬 판단(중복/일일한도)은 `order_history.csv`에만 의존하므로 직접적 안전 위협은 아니나, `DECISION_LOG.md`에 SQLite 전환 필요성을 **NEEDS_USER_DECISION**으로 기록했다(Phase 5 착수 전 사용자 판단 필요).
- 부분 체결의 "포지션 상태" 완전 반영은 여전히 Phase 5 범위.
- `run_order_safety_check`/`try_reserve_order` 예외 발생 시 해당 실행의 나머지 후보도 함께 스킵됨(의도된 보수적 동작, 유지).

### Codex가 집중 검토해야 할 항목

1. `validate_order_date_str()`의 round-trip 검증이 `strptime`/`strftime`의 로캘 의존성 없이 항상 결정적인지(테스트 환경 로캘 변경 시에도).
2. `merge_reconciliation_state()`의 상태 랭크 설계(UNKNOWN=SUBMITTED와 동순위)가 실제 Alpaca가 보낼 수 있는 모든 상태 문자열에 대해 안전한 기본값인지.
3. `order_history.csv`/`order_reconciliation.csv` 교차 파일 트랜잭션 부재에 대한 `DECISION_LOG.md`의 위험 평가(NEEDS_USER_DECISION)에 동의하는지, 아니면 이를 실제로 HIGH로 재평가해야 하는지.
4. `universe_builder.py`의 `AlpacaBroker.get_assets()` 전환이 기존 `universe_daily_runner.py`(subprocess로 `python universe_builder.py` 호출)와의 통합에서 `if __name__ == "__main__":` 가드 도입에도 불구하고 동일하게 동작하는지(정적 검토, 실제 cron 환경 미실행).

### 현재 커밋 해시

`16a1ee4` (Gate universe collection behind paper endpoint validation) — 이번 패키지가 다루는 마지막 코드 커밋. 본 문서 자체를 포함한 문서 갱신은 다음 커밋에서 기록됨.
