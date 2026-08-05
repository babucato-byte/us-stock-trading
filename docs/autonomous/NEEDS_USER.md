# NEEDS_USER — 사용자만 실행할 수 있는 작업

자율 루프가 도구/권한 한계로 실행하지 못한 항목. 각 항목은 복사-붙여넣기로 실행 가능해야 한다.

## 1. Oracle 서버 재검증 (T3)

세션에는 SSH 접근이 없다. 서버에서:

```bash
# 1) 최신 코드 반영 상태 확인 (T1 PASS + push 이후)
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

## 3. 매도 전략 인터페이스 잔여 지시 재전송 (T5)

`entry_rules` ~ `end_of_day_exit_rules` 필드 목록이 중간에 끊긴 지시 메시지의
나머지 부분을 아무 Claude 세션에나 다시 전달해 주면 T5가 진행된다.
