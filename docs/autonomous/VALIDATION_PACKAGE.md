# VALIDATION_PACKAGE

외부 검증자(ChatGPT/Codex)가 `CODEX_REVIEW.md`를 작성하기 위해 필요한 정보 패키지. Phase 완료 시마다 갱신한다.

---

## 이번 패키지: Phase 1 재수정 사이클 — CODEX-001~006 (2026-07-21)

### 검증 대상
- 이전 리뷰: `CODEX_REVIEW.md`, 대상 커밋 `fe2988c`/`dc9bff9`, **overall verdict FAIL**, **Phase 2 recommendation DO_NOT_PROCEED**.
- 이번 패키지가 다루는 수정 커밋: `9688a13`, `b93a08a`, `22a6651`, `962eb69` (순서대로 CODEX-001 / CODEX-002+003 / CODEX-006 / CODEX-005+004).
- Phase 상태: `IN_PROGRESS` (부분 체결의 "포지션 상태" 완전 반영은 Phase 5 선행 필요 — CODEX Finding이 아니라 Phase 1 자체 승인 기준).

### Finding 처리 결과

| ID | 심각도 | 이전 판정 | 이번 처리 | 커밋 |
|---|---|---|---|---|
| CODEX-001 | HIGH | PARTIALLY_RESOLVED | RESOLVED | `9688a13` |
| CODEX-002 | HIGH | PARTIALLY_RESOLVED | RESOLVED | `b93a08a` |
| CODEX-003 | HIGH | PARTIALLY_RESOLVED | RESOLVED | `b93a08a` |
| CODEX-004 | MEDIUM | PARTIALLY_RESOLVED | RESOLVED | `962eb69` |
| CODEX-005 | HIGH (신규) | PARTIALLY_RESOLVED | RESOLVED | `962eb69` |
| CODEX-006 | HIGH (신규) | PARTIALLY_RESOLVED | RESOLVED | `22a6651` |

CRITICAL 0건, 미해결 HIGH 0건, 미해결 MEDIUM 0건. 상세 원인/수정 방안/테스트는 `REMEDIATION_PLAN.md` 참고.

### 변경 파일

- `broker/alpaca_client.py` — GET 경로 안전검사, `get_order_by_client_order_id`, `submit_order`의 `client_order_id` 파라미터
- `paper_strategy_order.py` — fail-closed 이력 읽기, ET 날짜, 원자적 쓰기, 프로세스 잠금, `try_reserve_order`/`update_order_status` 재설계, `order_reconciliation.csv` 관련 함수 일체, `reconcile_pending_orders`
- `conftest.py` (신규) — `collect_ignore` + `sys.path` 삽입
- `tests/test_broker_safety.py` — CODEX-001 테스트 6건
- `tests/test_paper_order_execution.py` — CODEX-002/003/006 테스트 다수 추가, 기존 테스트를 fail-closed 이력 요구사항에 맞게 재작성
- `docs/autonomous/REMEDIATION_PLAN.md`, `VALIDATION_REPORT.md`, `CURRENT_STATUS.md`, `SCALPING_V1_ROADMAP.md`, `CODEX_REVIEW.md`(리뷰 원문 보존, 삭제 없음)

### 핵심 diff (요약)

```python
# broker/alpaca_client.py::_request
+ self.config.validate_order_allowed()   # GET도 POST와 동일한 게이트를 통과

# paper_strategy_order.py::load_order_history
- except Exception: return pd.DataFrame(columns=[...])   # fail-open
+ if not ORDER_HISTORY_FILE.exists(): raise OrderHistoryUnavailable(...)  # fail-closed
+ (파싱/컬럼/날짜 검증 실패 시에도 동일하게 raise)

# paper_strategy_order.py::main
- today = datetime.now().strftime("%Y-%m-%d")
+ today = eastern_now().strftime("%Y-%m-%d")   # America/New_York

# paper_strategy_order.py::try_reserve_order (신규, 잠금+재조회+client_order_id)
with _order_history_lock(timeout=lock_timeout):
    order_history = load_order_history()          # 최신 재조회
    if is_duplicate_order(...): raise DuplicateOrderError(...)
    check_daily_trade_count(...)                    # 잠금 하 재검증
    ... save_order_history(reserved_history) ...
    client_order_id = f"scalp-{symbol}-{order_date}-{uuid4().hex[:10]}"
    _record_pending_reconciliation(client_order_id, ...)
    return reserved_history, client_order_id
```

전체 diff는 `git show 9688a13`, `git show b93a08a`, `git show 22a6651`, `git show 962eb69`로 확인 가능.

### 실행 명령 및 결과

```bash
# 저장소 루트
venv/bin/pytest -q                              # 97 passed, 2 warnings
venv/bin/python -m pytest -q                    # 97 passed, 2 warnings

# 저장소 상위 디렉터리, 경로 명시 (CODEX-005가 요구한 정확한 두 형태)
cd ..
us-stock-trading/venv/bin/pytest -q us-stock-trading            # 97 passed, 2 warnings
us-stock-trading/venv/bin/python -m pytest -q us-stock-trading  # 97 passed, 2 warnings

# 집중 테스트
venv/bin/pytest -q tests/test_broker_safety.py tests/test_paper_order_execution.py
# 54 passed, 1 warning

# 동시성 테스트 안정성(5회 반복)
venv/bin/pytest -q tests/test_paper_order_execution.py -k "concurrent or lock_acquisition"
# 매회 4 passed, 40 deselected — 5/5 안정

# 정적 검증
git diff --check 6ea2c13 HEAD   # 통과, 출력 없음
grep -rn "api.alpaca.markets" --include="*.py" .   # LIVE_BASE_URL 상수 정의/테스트 부정 케이스 외 실사용 없음
md5 order_history.csv           # a61104cf03499860ae89d4e194dc8c07 — 사이클 전후 동일(실제 운영 파일 미변경)
```

warning 2건은 기존에도 존재하던 urllib3/LibreSSL 환경 경고와 unknown scanner field 테스트 경고이며, 이번 변경과 무관하고 실패는 없다.

### 안전 재검증

- 실제 Alpaca/Slack API 호출 없음: 모든 broker 상호작용은 `FakeBroker`/`DummySession`/monkeypatch. 4가지 pytest 호출 형태 모두에서 스크래치 스크립트(실네트워크 코드 포함)가 수집되지 않음을 확인.
- Live Trading 활성화 없음. Live URL은 상수 정의와 "차단되어야 함"을 검증하는 부정 테스트에만 등장하며, 어떤 실행 경로에서도 기본값/폴백으로 쓰이지 않음(grep 재확인).
- 운영 서버 접속/설정 변경 없음. systemd/cron/nginx 미변경.
- `origin/main`에 push하지 않음(로컬 커밋만).
- API Key/Secret/Slack Webhook 신규 노출 없음.
- 기존 리스크 한도(`risk_config.py`) 값 미변경. 전략 로직 미변경. `order_history.csv` 컬럼 스키마 미변경(신규 추적 데이터는 별도 파일 `order_reconciliation.csv`).
- 테스트 중 실제 `order_history.csv`/`order_reconciliation.csv` 변경 없음(전부 `tmp_path`, 매 테스트마다 세 경로 — 이력/잠금/조정 파일 — 모두 명시적으로 리다이렉트).

### 운영 영향

- 없음(즉시). 다만 다음 배포 시 참고할 사항:
  - 운영 서버의 `order_history.csv`가 아직 없다면(또는 스키마가 다르다면) `paper_strategy_order.initialize_order_history()`를 배포 절차에 한 번 포함해야 한다 — 이제 파일이 없으면 신규 주문이 fail-closed로 전부 차단된다(의도된 동작).
  - 신규 파일 `order_reconciliation.csv`/`order_history.lock`이 실행 디렉터리에 생성된다. `.gitignore`의 `*.csv`/`*.lock` 규칙으로 이미 커버됨.

### 남은 위험

- 부분 체결이 broker와 대조(reconciliation)되어 상태로는 기록되지만, 이를 "포지션"으로서 손절/익절/강제청산과 연결하는 것은 Phase 5(포지션 생명주기 상태 머신) 범위다. Phase 1은 이 때문에 `VALIDATED`로 승격하지 않았다.
- `order_reconciliation.csv`는 duplicate/일일한도 판단에 쓰이는 안전 크리티컬 파일이 아니므로 `order_history.csv`와 달리 자체 `fcntl` 잠금이 없다. 동시 실행 시 이 파일에 한정된 낮은 확률의 lost update 가능성이 이론적으로 남아있으나, 안전 게이트(duplicate/일일한도)는 전부 잠금이 걸린 `order_history.csv`에서만 판단하므로 주문 안전성 자체에는 영향이 없다.
- `run_order_safety_check`/`try_reserve_order`에서 예외가 발생하면 해당 실행의 나머지 후보 심볼도 함께 처리되지 않는다(의도된 보수적 동작, `DECISION_LOG.md` 참고).

### Codex가 집중 검토해야 할 항목

1. CODEX-001의 GET 경로 수정이 실제로 모든 broker 진입점(get_account/get_positions/get_recent_orders/submit_order)을 커버하는지, 우회 경로가 남아있지 않은지.
2. CODEX-003의 `threading` 기반 동시성 테스트가 `fcntl.flock`의 실제 프로세스 간 보장을 충분히 대표하는지(같은 프로세스 내 다른 파일 디스크립터 기준 테스트의 한계).
3. CODEX-006에서 `order_reconciliation.csv`에 자체 잠금이 없다는 판단이 타당한지(위 "남은 위험" 참고) — 안전 크리티컬 여부에 대한 재검토 요청.
4. CODEX-005의 `collect_ignore` 방식이 향후 새로운 루트 스크래치 파일이 추가될 경우에도 견고한지, 아니면 더 구조적인 해법(예: 스크래치 파일을 `scratch/` 디렉터리로 이동)이 필요한지.

### 현재 커밋 해시

`962eb69` (Harden pytest collection and project imports) — 이번 패키지가 다루는 마지막 코드 커밋. 본 문서 자체를 포함한 문서 갱신은 다음 커밋에서 기록됨.
