# SHADOW_MODE_EXIT_CRITERIA — Shadow 운영 종료(제한 실거래 검토 진입) 판정 기준

작성: 2026-08-06 · BACKLOG `T4` · 브랜치 `feature/kis-live-broker`

근거: `ACCEPTANCE_CRITERIA.md` §Phase 8, `SCALPING_V1_ROADMAP.md`,
`docs/deployment/ORACLE_KIS_MIGRATION_RUNBOOK.md` §13~15, 그리고 실제 구현
(`shadow_audit.py`, `shadow_mode.py`, `scripts/run_shadow_mode.py`,
`scripts/run_shadow_exit_evaluation.py`, `scripts/run_health_report.py`,
`reconciliation/reconciliation_state.py`).

---

## 0. 이 문서의 지위와 설계 원칙

**지위**

- 이 문서는 **판정 기준**이지 승인이 아니다. 전건 충족의 의미는 오직
  "제한 실거래 **검토**를 시작해도 된다"이며, 실거래 활성화 결정 자체는 사용자만 내린다
  (`docs/live_review/LIVE_APPROVAL_RECORD.md`).
- `AUTOPILOT.md`의 불변 안전 규칙은 이 문서로 해제되지 않는다.
  `KIS_LIVE_ORDER_ENABLED` / `LIVE_ROLLOUT_ENABLED` / `approved` / `live_enabled`는
  이 기준을 전부 통과해도 자동으로 켜지지 않는다.
- 이 문서는 코드를 변경하지 않는다. 문서만이다(BACKLOG T4의 범위).

**설계 원칙 (기준을 이렇게 만든 이유)**

1. **측정 가능한 것만 기준으로 쓴다.** 모든 항목은 저장소에 실제로 존재하는 산출물
   — `shadow_audit_events` 테이블, `shadow-YYYY-MM-DD.jsonl`,
   `run_health_report.collect()`의 반환값, `RECONCILIATION_STATE.json` — 로 판정된다.
   확인 명령을 함께 적지 못하는 항목은 기준으로 채택하지 않았다.
2. **Shadow가 관측할 수 없는 것을 관측한 척하지 않는다.** Shadow는 체결이 0이다.
   따라서 손익 기반 게이트는 여기서 판정 **불가**이며, §6에서 별도 트랙으로 분리했다.
   Shadow 창구 통과를 "성과 검증 통과"로 읽으면 안 된다.
3. **기준을 완화하지 않는다.** `ACCEPTANCE_CRITERIA.md` 마지막 문장("성과를 좋게 보이기
   위해 완화하지 않는다")을 그대로 승계한다. Phase 8에서 가져온 수치는 값을 낮추지 않고
   그대로 쓰거나, 등가가 아닌 경우 대체하지 않고 §6으로 이관했다.
4. **정상 차단은 결함이 아니다.** 현재 자세에서 Shadow는 `LIVE_FLAG` / `ENTRY_DISABLED`로
   차단되는 것이 정상이며, 가격 편차·잔고 부족·중복 차단도 모두 안전장치가 일한 기록이다.
   §3의 결함 항목 집합에는 이런 정상 차단이 들어 있지 않다.

---

## 1. Shadow 창구가 판정할 수 있는 것 / 없는 것

Shadow Mode는 주문을 내지 않는다. `scripts/run_shadow_mode.py`는
`execution.execution_engine`을 **import조차 하지 않는다**(runbook §14). 따라서 체결·손익·
슬리피지가 구조적으로 존재하지 않으며, `ACCEPTANCE_CRITERIA.md` §Phase 8의 14개 항목 중
상당수는 Shadow 창구로 원리상 판정할 수 없다.

| # | Phase 8 게이트 항목 | Shadow로 판정 | 이유 / 이관처 |
|---|---|---|---|
| 1 | 최소 100회 체결 | **불가** | 체결 0. → §6 성과 트랙 |
| 2 | 최소 20거래일 | 가능 | 기간은 관측 가능 → **G1** |
| 3 | Profit Factor ≥ 1.20 | **불가** | 실현 손익 없음 → §6 |
| 4 | 평균 Expectancy 양수 | **불가** | 동상 → §6 |
| 5 | 비용/슬리피지 반영 후 총손익 양수 | **불가** | 체결가·수수료 없음 → §6 |
| 6 | 최대 낙폭 사전 한도 이내 | **불가** | 포지션 없음 → §6 |
| 7 | 단일 최대 수익 거래 제거 후 총손익 양수 | **불가** | 동상 → §6 |
| 8 | 일일 손실 제한 정상 작동률 100% | **부분** | 손실이 발생하지 않으므로 "발동"을 관측할 수 없다. 게이트가 호출 경로에 있다는 것만 확인 가능 → §6 |
| 9 | 장 마감 후 미청산 포지션 0건 | **부분(자명)** | 포지션이 애초에 0이라 0건은 증거가 아니다 → §6 |
| 10 | 중복 주문 0건 | 가능 | 가정 평가의 `DUPLICATE_BLOCKED` → **G4** |
| 11 | Live 주문 0건 | 가능 | Shadow 창구의 **핵심** 기준 → **G3** |
| 12 | 치명적 주문 상태 불일치 0건 | 가능 | reconciliation / UNKNOWN → **G6** |
| 13 | 재시작 후 상태 복구 성공 | 가능 | 계획된 재시작으로 실증 → **G9** |
| 14 | 데이터 지연 시 신규 진입 차단 | 가능 | KIS 조회 실패 경로 → **G10** |

**결론**: Shadow 창구는 **운영 무결성(operational integrity) 게이트**다. 성과 게이트가
아니다. 두 게이트는 각각 독립적으로 통과해야 하며, Shadow 통과가 성과 게이트를 대체하지
않는다.

### 1.1 Shadow가 매도/청산 경로를 실증하지 못한다는 점 (중요)

`scripts/run_shadow_exit_evaluation.py:331`은 `store.load_non_terminal()`이 반환하는
포지션을 순회한다. 실주문이 0이면 포지션도 0이므로, **Shadow 창구 동안 매도 타이머는
평가 대상이 하나도 없는 상태로 돈다.** shadow-exit 타이머가 "무사고"인 것은 매도 로직이
검증됐다는 뜻이 아니라 아무것도 평가하지 않았다는 뜻일 수 있다.

이를 기준에서 구분하기 위해 **G5**(매도 경로 표본)를 두되, 표본이 0인 상태로 창구를
통과할 수 있게 열어 두고 그 사실을 판정 기록에 **명시적으로 남기도록** 했다. 매도 경로의
실증은 §6 성과 트랙에서만 가능하다.

---

## 2. 용어와 측정 단위

| 용어 | 정의 |
|---|---|
| **창구(window)** | Shadow 타이머가 활성화된 뒤, 연속된 계수일의 구간. G1의 일수를 채워야 판정 대상이 된다. |
| **계수일(counted day)** | §4의 인정 조건을 만족한 미국 정규장 거래일 1일. 인정되지 않은 날은 창구 길이에 포함되지 않는다(리셋과는 다르다). |
| **결함(defect)** | §3의 G3·G6·G7·G8 중 하나라도 위반한 사건. §5의 리셋 규칙을 적용한다. |
| **정상 차단** | `CONFIG_BLOCKED`(`LIVE_FLAG`/`ENTRY_DISABLED`/`BROKER`/`COMMIT`/`ACCOUNT`), `PRICE_DEVIATION_BLOCKED`, `CASH_BLOCKED`, `DUPLICATE_BLOCKED`, `INSTRUMENT_BLOCKED`, `KIS_PIPELINE_EXCLUDED`. 결함이 아니다. |
| **권위 기록** | `shadow_audit_events` SQLite 테이블. JSONL은 보조이며, JSONL 0건이 곧 "후보 없음"이 아니다(runbook §14). |

**증거 소스 3종**

1. `shadow_audit_events` (권위) — 매수/매도 양 경로, 모든 run이
   `SHADOW_COMPLETED`/`SHADOW_BLOCKED`/`SHADOW_ERROR` 중 **정확히 하나**로 종료.
2. `shadow-YYYY-MM-DD.jsonl` (조건부) — Order Gate까지 **도달한** 후보만 기록.
   `code_commit` 필드를 포함하므로 G8(코드 동일성)의 근거가 된다.
3. `scripts/run_health_report.py` (15분 주기) — `healthy` / `problems` /
   `reconciliation` / `unknown_orders` / `shadow_audit` / `shadow_log_corruption` /
   `live_service`.

---

## 3. 종료 기준 G1~G10

전건 충족해야 한다. 하나라도 미충족이면 창구는 계속된다.

### G1. 기간 — 무결점 연속 **20** 미국 정규장 거래일

- **값의 근거**: `ACCEPTANCE_CRITERIA.md` §Phase 8 "최소 20거래일"을 **그대로** 재사용.
  낮추지 않았다.
- **측정**: §4를 만족한 계수일이 리셋 없이 20일 연속.

### G2. 매수 경로 표본 — 게이트 도달 평가 ≥ **100**건, 그중 가정 평가 `GATE_APPROVED` ≥ **30**건

- **측정**: 창구 구간의 `shadow_audit_events`에서 `GATE_APPROVED` + `GATE_REJECTED`
  합계 ≥ 100, `GATE_APPROVED` ≥ 30.
- **값의 근거**: `ASSUMPTION`. Phase 8의 "100회 체결"과 **등가가 아니다** — 체결이 아니라
  게이트 판정 횟수다. 100은 Phase 8 수치를 표본 크기의 하한으로만 차용한 값이고,
  30은 "승인 경로가 한 번도 실행되지 않은 창구는 승인 경로를 검증하지 못했다"는 이유로 둔
  최소값이며 실증 근거가 없다. 실제 후보 유입량이 관측되면 재검토 대상이다
  (**완화 금지**: 재검토는 값을 올릴 수 있고, 내리려면 사용자 결정이 필요하다).
- **주의**: 이 표본이 안 모이면 창구는 20일을 넘겨 계속된다. 그것이 의도된 동작이다.

### G3. 실주문 0 — 창구 전 기간

전부 0이어야 한다. 하나라도 아니면 **즉시 창구 전체 리셋 + 원인 규명 전까지 재시작 금지**.

| 확인 대상 | 요구값 |
|---|---|
| KIS 주문/취소 transport 호출 | 0 |
| `systemctl is-enabled us-stock-trading-live.service` | `static`(절대 `enabled` 아님) |
| `KIS_LIVE_ORDER_ENABLED` / `LIVE_ROLLOUT_ENABLED` | 전 기간 `false` |
| `order_history.csv` 신규 실주문 행 | 0 |
| health report의 `live_service` | `enabled` 관측 0회 |

### G4. 중복 주문 의도 0 — 가정 평가 기준

- **측정**: 가정 평가에서 동일 `signal_id`에 대해 `GATE_APPROVED`가 2회 이상 나온 사례 0건.
  `DUPLICATE_BLOCKED` 이벤트가 발생한 것 자체는 **정상 차단**이며 결함이 아니다.
- **근거**: Phase 8 §10 "중복 주문 0건"의 Shadow 대응물.

### G5. 매도 경로 표본 — 관측된 만큼 기록 (통과 조건 아님, 기록 의무)

- **측정**: 창구 구간의 `EXIT_EVALUATION`(= `SIGNAL_RECEIVED` with
  `reason_code='EXIT_EVALUATION'`) 건수.
- **판정**: 이 값이 **0이어도 G5는 통과**한다(§1.1의 구조적 이유). 다만 판정 기록에
  `매도 경로 실증 표본 = 0 (미검증)`을 **반드시 명시**해야 하며, 이 상태로는 §6의 성과
  트랙 없이 제한 실거래로 넘어갈 수 없다.

### G6. 감사 무결성 — 창구 전 기간 전부 빈 목록

```
shadow_audit.audit_integrity_report()["runs_without_terminal_event"]        == []
shadow_audit.audit_integrity_report()["runs_with_multiple_terminal_events"] == []
shadow_mode.read_all_with_integrity()[1]                                    == []
shadow_audit_events 중 event_type='SHADOW_ERROR'                            == 0건
```

- **근거**: 감사 기록이 깨진 창구는 그 자체로 판정 근거가 없다. runbook §14도 이 두 값이
  비어 있지 않으면 다음 단계로 진행하지 말라고 이미 규정한다.

### G7. 상태 정합 — 창구 전 기간

| 확인 대상 | 요구값 |
|---|---|
| `reconciliation_state.get_last_result().clean` | 전 기간 `True` |
| `mismatch_count` | 0 |
| UNKNOWN 주문 (`idempotency.list_unknown_orders`) | 0 |
| commit-uncertain marker (`reconciliation_state.commit_is_uncertain()`) | `False` |
| freshness 위반 (`scripts/check_reconciliation_freshness.py` 비-0 종료) | 0회 |
| HALT 발생 (`kill_switch.is_halted()` True 관측) | 0회 |

- **근거**: Phase 8 §12 "치명적 주문 상태 불일치 0건". 불일치는 자동 보정하지 않고 차단만
  한다는 저장소 불변 규칙과 정합.

### G8. 코드 동일성 — 창구 전 기간 단일 배포 커밋

- **측정**: 창구 구간 JSONL 레코드의 `code_commit` distinct 값이 정확히 1개.
  (JSONL이 0건인 날이 있을 수 있으므로, 배포 이력과 대조해 보완한다.)
- **근거**: 창구 도중 코드가 바뀌면 앞부분 관측은 바뀐 코드에 대한 증거가 아니다.
  → 코드 변경은 §5에 따라 **전체 리셋**.

### G9. 재시작 복구 실증 — 창구 중 최소 1회

- **측정**: 창구 중 계획된 서비스 재시작(migrate → preflight → reconcile → shadow 순서
  재기동)을 1회 이상 수행하고, 그 직후:
  - `RECOVERY_REQUIRED` 상태 0건
  - 재시작 직후 첫 reconciliation이 `clean=True`
  - 감사 run의 terminal 이벤트 누락 0건
- **근거**: Phase 8 §13.

### G10. 데이터 지연/조회 실패 시 차단 실증 — 창구 중 최소 1회 관측

- **측정**: KIS 조회 실패가 발생한 run에서
  - `scripts/run_reconciliation.py`가 exit 2로 끝났고 **`RECONCILIATION_STATE.json`의
    `checked_at`이 갱신되지 않았음**(실패한 조회가 clean timestamp를 갱신하지 않는다)
  - 해당 구간에 `GATE_APPROVED` 0건
- **근거**: Phase 8 §14. 창구 중 자연 발생하지 않으면, 판정 기록에
  `미관측`으로 남기고 §7의 인위적 재현(운영자 수행)으로 대체한다.

### G11. 증거 보관 설정 — **창구 시작 전에** 확정해야 함

- **요구**: `SHADOW_AUDIT_RETENTION_DAYS` ≥ (창구 캘린더 길이 + 15일).
  20 거래일은 캘린더로 약 28일이므로 **≥ 45일**을 권고한다.
- **왜 기준인가**: `shadow_audit.py`/`shadow_mode.py`의 기본 보관 기간은 **30일**이고,
  `purge_old_events()`/`purge_old_files()`는 reconciliation 틱에서 실제로 삭제를 수행한다.
  기본값 그대로 20 거래일 창구를 돌리면 **판정 시점에 창구 앞부분의 증거가 이미 삭제된
  상태**가 된다. 이 설정은 창구가 시작된 뒤에 고쳐도 삭제된 기록을 되돌리지 못하므로,
  반드시 사전에 확정한다.
- **확인**: `SHADOW_AUDIT_RETENTION_DAYS`, `SHADOW_AUDIT_MAX_FILE_MB`,
  `SHADOW_MODE_LOG_DIR`(또는 `SHADOW_MODE_LOG_FILE`)이 환경파일에 설정되어 있을 것.
  세 값이 모두 미설정이면 JSONL은 아예 꺼져 있고 DB만 남는다(지원되는 구성이지만,
  그 경우 G8의 `code_commit` 근거를 배포 이력으로만 확보해야 한다).

---

## 4. 계수일(counted day) 인정 조건

하루가 창구 길이에 포함되려면 **전부** 만족해야 한다.

1. 그날이 미국 정규장 거래일이다(휴장일·반장일 제외. 반장일은 3번 조건을 비례 적용).
2. 그날 `shadow_audit_events`에 매수 경로 run이 1건 이상 있다.
3. 정규장 구간(09:30~16:00 ET, 5분 주기 → 기대 78회)의 shadow run 성공 횟수가
   기대치의 **90% 이상**(≥ 71회). `ASSUMPTION` — 타이머 누락·재기동으로 인한 소량 결손은
   허용하되 사실상 멈춘 날을 계수하지 않기 위한 값이며 실증 근거는 없다.
4. 그날 수집된 health report(15분 주기 → 기대 96회) 중 `healthy=false`가 0회.
5. §3의 결함이 0건.

인정되지 않은 날은 **미계수**일 뿐 리셋이 아니다. 창구는 그날을 건너뛰고 이어진다.
단, 미계수일이 **연속 3일** 이상이면 운영이 중단된 것으로 보고 §5의 전체 리셋을 적용한다
(`ASSUMPTION`).

---

## 5. 리셋 규칙

| 사건 | 조치 |
|---|---|
| 실주문/취소 transport 1건이라도 발생 (G3 위반) | **전체 리셋** + 원인 규명 전까지 Shadow 재시작 금지 |
| `us-stock-trading-live.service`가 `enabled`로 관측됨 | **전체 리셋** + 즉시 disable, 원인 규명 |
| `SHADOW_ERROR` 발생 | **전체 리셋** |
| 감사 무결성 위반(terminal 0개/2개, JSONL 손상) (G6) | **전체 리셋** |
| reconciliation dirty / mismatch / UNKNOWN 발생 (G7) | **전체 리셋** + 해소까지 창구 정지 |
| HALT 발생 | **전체 리셋** |
| 배포 커밋 변경(코드·설정 변경 포함) (G8) | **전체 리셋** |
| 타이머 정지·호스트 재부팅으로 그날 3번 조건 미달 | 해당일 **미계수** (연속 3일이면 전체 리셋) |
| KIS 조회 불가(exit 2)로 일부 run 실패 | 3번 조건을 만족하면 **계속**. 미달이면 해당일 미계수 |
| 계획된 재시작(G9 실증 목적) | **계속** — 단 그날 3번 조건을 만족해야 계수 |
| 정상 차단(§2 목록) 발생 | **계속** — 결함이 아니다 |

전체 리셋은 계수일 카운터를 0으로 되돌린다. 리셋 사유와 시각은 §7의 판정 기록에 남긴다.

---

## 6. Shadow로 판정 불가한 것 — 후속 성과 트랙 `NEEDS_USER_DECISION`

§1 표에서 **불가/부분**으로 분류된 Phase 8 항목 8개(체결 수, PF, Expectancy, 비용 반영
손익, MDD, 최대수익거래 제거 후 손익, 일일 손실 제한 발동, 미청산 포지션)와 §1.1의 매도
경로 실증은 Shadow 창구로 충족될 수 없다. 이들을 무엇으로 충족할지는 **미결정**이다.

현재 아키텍처의 제약: Alpaca 주문 경로는 데이터 전용으로 차단됐고, KIS가 유일한 주문
브로커다. 따라서 기존 "Alpaca Paper Trading으로 100회 체결"은 더 이상 성립하지 않는다.

선택지:

- **A. KIS 모의투자 계좌 운영** — `brokers/kis_broker.py`에 이미 모의(paper) TR_ID 세트가
  구현되어 있다(`TR_ID_ORDER[("paper","buy")] = VTTT1002U` 등). 실체결·실슬리피지는
  아니지만 체결 이벤트·포지션·매도 경로·손익 계산이 실제로 흐르므로 Phase 8 항목 대부분을
  관측 가능하게 만든다. **권고안.**
- **B. Phase 6 백테스트 결과로 대체** — 실행 경로를 전혀 검증하지 못한다. 성과 게이트의
  취지(실행 포함 검증)를 충족하지 못하므로 단독으로는 부적합.
- **C. 제한 실거래(30,000원)로 직접 진입** — `LIMITED_LIVE_30K_KRW_PLAYBOOK.md` 경로.
  성과 게이트를 실거래에서 채우는 셈이므로 순서가 뒤집힌다. 사용자 결정 사항.

**이 선택은 안전 크리티컬이므로 자율 루프가 결정하지 않는다.** 사용자가 A/B/C 중 하나를
지정하면 해당 트랙의 판정 기준을 별도 문서로 작성한다.

---

## 7. 판정 절차

### 7.1 창구 시작 전 (1회)

1. T3(Oracle 재검증) 완료. Shadow 타이머 활성화는 `scripts/enable_oracle_shadow_timer.sh`
   단계 B로만 수행한다.
2. **G11의 보관 설정을 먼저 확정한다.** 창구 시작 후에는 되돌릴 수 없다.
3. 배포 커밋 해시를 판정 기록에 고정한다(G8의 기준값).

### 7.2 매 계수일 (운영자 또는 일일 자동 수집)

```bash
cd ~/trading-release && source venv/bin/activate

# 1) 감사 무결성 + JSONL 손상 + 종결 이벤트 (G6)
python3 -c "
import json, shadow_audit, shadow_mode
records, corruption = shadow_mode.read_all_with_integrity()
print(json.dumps({
  'jsonl_records': len(records),
  'jsonl_corruption': corruption,
  'audit_integrity': shadow_audit.audit_integrity_report(),
}, indent=2))
"

# 2) 상태 정합 + live unit + health (G3/G7/§4-4)
python3 scripts/run_health_report.py --json
echo "exit=$?"   # 0=healthy, 2=unhealthy

# 3) 게이트 표본과 코드 동일성 (G2/G4/G5/G8)
sqlite3 "$STATE_STORE_DB_FILE" "
  select date(created_at) d, event_type, count(*)
  from shadow_audit_events
  where created_at >= date('now','-1 day')
  group by d, event_type order by d, event_type;
"

# 4) reconciliation freshness (G7)
python3 scripts/check_reconciliation_freshness.py; echo "exit=$?"
```

### 7.3 창구 종료 판정 (1회)

G1~G11을 표로 채운 판정 기록을 남긴다. 기록 위치:

- `docs/autonomous/CURRENT_STATUS.md` — 판정 결과 요약 1개 절
- `docs/live_review/LIVE_APPROVAL_RECORD.md` — 사용자 최종 승인은 여기에만 기록
- 미충족·미관측 항목(특히 G5의 매도 경로 표본, G10 미관측 여부)은 **감추지 않고 명시**

전건 충족이어도 그 결과물은 "제한 실거래 **검토** 진입 가능"이다. 실거래 활성화는
§6의 성과 트랙 결정과 사용자 승인을 별도로 요구한다.

---

## 8. ASSUMPTION / TBD 목록

| 항목 | 값 | 상태 |
|---|---|---|
| G1 창구 길이 | 20 거래일 | 근거 있음 (Phase 8 그대로) |
| G2 게이트 도달 표본 하한 | 100건 | `ASSUMPTION` (Phase 8 수치 차용, 등가 아님) |
| G2 `GATE_APPROVED` 하한 | 30건 | `ASSUMPTION` (실증 근거 없음) |
| §4-3 일일 run 성공률 | 90% (≥71/78) | `ASSUMPTION` |
| §4 연속 미계수 허용 | 3일 | `ASSUMPTION` |
| G11 보관 기간 | ≥ 45일 | 계산 근거 있음 (28일 창구 + 판정 여유) |
| §6 성과 트랙 선택 (A/B/C) | 미정 | `NEEDS_USER_DECISION` |
| 창구 시작일 / 담당 운영자 / 판정 승인자 | 미정 | `TBD_OPERATOR` |

`ASSUMPTION` 표시 값은 실제 관측치가 쌓이면 재검토한다. 재검토로 **값을 올리는 것은 자율
루프가 할 수 있고, 낮추는 것은 사용자 결정을 요구한다**(§0 원칙 3).
