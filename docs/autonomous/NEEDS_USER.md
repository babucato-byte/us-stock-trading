# NEEDS_USER — 사용자만 실행할 수 있는 작업

자율 루프가 도구/권한 한계로 실행하지 못한 항목. 각 항목은 복사-붙여넣기로 실행 가능해야 한다.

## 1. Oracle 서버 재검증 (T3)

세션에는 SSH 접근이 없다. 서버에서:

push는 완료됐다 (T2, 2026-08-06): `origin/feature/kis-live-broker` = `b36a8a6`.

```bash
# 1) 최신 코드 반영 상태 확인 — HEAD가 b36a8a6이어야 한다
cd ~/us-stock-trading && git fetch && git log --oneline -3 origin/feature/kis-live-broker

# 2) 검증 절차는 runbook 순서대로
#    docs/deployment/ORACLE_KIS_MIGRATION_RUNBOOK.md 참고
#    - unit/EnvironmentFile/freshness/timer disabled 상태 재검증
#    - systemd timer는 검증 통과 전까지 비활성 유지
```

## 2. KIS 실계좌 TBD 2건 확인 (T3)

`TBD_VERIFY_LIVE_DOCS` 표시된 취소 TR_ID 1건, 현재가 응답 필드명 1건은
실계좌(또는 모의계좌) 조회로만 확정 가능. KIS 개발자센터 문서와 대조 후
코드의 TBD 주석 위치에서 확정값으로 교체 지시.

## 3. Shadow 증거 보관 기간 상향 (T7) — **창구 시작 전에** 해야 함

`SHADOW_AUDIT_RETENTION_DAYS` 기본값은 30일인데, Shadow 판정 창구는 20 거래일
(≈28 캘린더일)이다. 기본값 그대로 창구를 돌리면 판정 시점에 창구 앞부분의
`shadow_audit_events` 행과 `shadow-YYYY-MM-DD.jsonl` 파일이 이미 삭제돼 있다
(`purge_old_events()`/`purge_old_files()`가 reconciliation 틱에서 실제로 지운다).
**삭제된 뒤에는 되돌릴 수 없으므로 Shadow 타이머를 켜기 전에 설정한다.**
근거: `docs/autonomous/SHADOW_MODE_EXIT_CRITERIA.md` G11.

```bash
# 서버에서 (root)
sudo sed -i 's/^SHADOW_AUDIT_RETENTION_DAYS=.*/SHADOW_AUDIT_RETENTION_DAYS=45/' \
  /etc/us-stock-trading/live-readonly.env
grep -q '^SHADOW_AUDIT_RETENTION_DAYS=' /etc/us-stock-trading/live-readonly.env \
  || echo 'SHADOW_AUDIT_RETENTION_DAYS=45' | sudo tee -a /etc/us-stock-trading/live-readonly.env

# JSONL 경로도 설정돼 있어야 한다 (미설정이면 JSONL은 꺼진 채 DB만 남는다)
grep -E '^(SHADOW_MODE_LOG_DIR|SHADOW_MODE_LOG_FILE|SHADOW_AUDIT_MAX_FILE_MB)=' \
  /etc/us-stock-trading/live-readonly.env

sudo systemctl daemon-reload
```

## 4. 매도 전략 인터페이스 잔여 지시 재전송 (T5)

`entry_rules` ~ `end_of_day_exit_rules` 필드 목록이 중간에 끊긴 지시 메시지의
나머지 부분을 아무 Claude 세션에나 다시 전달해 주면 T5가 진행된다.

## 5. 로컬 `.git/HEAD.lock` 제거 — 한 줄 (자율 루프 차단 해제)

직전 세션이 남긴 0바이트 스테일 락이 `git commit`/`git update-ref`를 전부 막는다
(실행 중인 git 프로세스는 0으로 확인됨). 세션 샌드박스가 `.git` 내부 파일 삭제를
거부해 자율 루프가 직접 지울 수 없다. 사이클 3은 링크드 워크트리로 우회했지만,
이후 세션이 정상 경로로 커밋하려면 아래 한 줄이 필요하다. 로컬 전용이며 서버·origin
영향 없음.

```bash
cd ~/Projects/us-stock-trading && rm -f .git/HEAD.lock
```
