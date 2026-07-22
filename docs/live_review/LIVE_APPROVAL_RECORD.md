# Live Approval Record

```
approved: false
live_enabled: false
```

**중요: 이 파일의 존재는 실거래(live trading) 승인을 의미하지 않는다.**
이 문서는 실거래 승인 여부를 기록하기 위한 빈 양식(템플릿)이며, 위 `approved: false` /
`live_enabled: false`가 실제로 `true`로 변경되고 아래 승인 항목이 모두 운영자에 의해
명시적으로 채워지기 전까지, 코드 상의 `ENABLE_REAL_TRADING`/`TRADING_MODE`
(`risk_config.py:23-24`, 기본값 `TRADING_MODE = "paper"`, `ENABLE_REAL_TRADING = False`)와
무관하게 이 저장소의 어떤 자동화도 실거래 승인을 받은 것으로 간주해서는 안 된다.

## 승인 기록 (모두 미기입 상태)

| 항목 | 값 |
|---|---|
| 승인 여부 | `false` |
| 승인자(성명/식별자) | `TBD(운영자 기입)` |
| 승인 일시 | `TBD(운영자 기입)` |
| 승인 대상 커밋 | `TBD(운영자 기입)` (참고: 이 문서 작성 시점 최신 커밋은 `c34fde1a664641799e4a37a02372f5d41a9e72ae`) |
| 승인 근거([LIMITED_LIVE_REVIEW_CHECKLIST.md](./LIMITED_LIVE_REVIEW_CHECKLIST.md) 최종 상태) | `TBD(운영자 기입)` |
| 실거래 활성화 범위(계좌, 금액 한도, 기간 등) | `TBD(운영자 기입)` |
| 롤백 담당자 | `TBD(운영자 기입)` (참고: [ROLLBACK_PLAN.md](./ROLLBACK_PLAN.md)) |

## 실거래 활성화 전 필수 선행 조건

아래 조건이 모두 충족되고 위 승인 기록이 실제로 채워지기 전까지 `ENABLE_REAL_TRADING`을
`true`로 바꾸거나 `TRADING_MODE`를 `live`로 바꾸는 배포를 진행하지 않는다.

1. [LIMITED_LIVE_REVIEW_CHECKLIST.md](./LIMITED_LIVE_REVIEW_CHECKLIST.md)의 모든 `TBD(운영자 기입)`
   항목이 실제 값으로 채워짐.
2. [KILL_SWITCH_RUNBOOK.md](./KILL_SWITCH_RUNBOOK.md)와 [INCIDENT_RESPONSE_RUNBOOK.md](./INCIDENT_RESPONSE_RUNBOOK.md)의
   절차가 운영팀에 실제로 공유되고 숙지됨.
3. `venv/bin/python -m pytest -q`가 0 failed로 통과함(문서 작성 시점 실측: `384 passed, 2 warnings`).
4. Kill Switch 상태가 `ACTIVE`이고, `notification_health.get_status()`가 `HEALTHY` 또는
   확인 가능한 상태임.

## 최종 상태

이 레코드가 가리키는 현재 상태는 아래 두 값 중 하나로만 표기한다: `READY_FOR_LIMITED_LIVE_REVIEW`
또는 `BLOCKED`. (이 값은 실거래 승인 여부와 별개다 — 위 `approved`/`live_enabled`가 진짜 승인
여부를 나타낸다.)

**현재 상태: `READY_FOR_LIMITED_LIVE_REVIEW`**

근거: 코드/테스트 기준 사람 검토를 시작할 준비는 되어 있으나(체크리스트 1~5절 실측 완료),
승인에 필요한 운영자 기입 항목(체크리스트 6~7절, 본 문서의 승인 기록)이 아직 비어 있어
`approved: false`, `live_enabled: false`를 유지한다.
