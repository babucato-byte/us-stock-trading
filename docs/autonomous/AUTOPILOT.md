# AUTOPILOT — 자율 개발 루프 계약서

이 문서를 읽는 Claude 세션(Claude Code, Cowork 공통)은 아래 계약에 따라
사용자 개입 없이 작업을 진행한다. **별도의 외부 지시문(ChatGPT 등)을 기다리지 않는다.**
이 문서와 `BACKLOG.md`가 지시문이다.

## 역할 통합

기존 워크플로(ChatGPT 지시 → Claude Code 구현 → Codex 검증)의 세 역할을 한 세션이 수행한다.

1. **Planner**: BACKLOG 최상위 항목을 읽고 설계를 먼저 작성한다.
2. **Implementer**: 설계대로 구현한다.
3. **Verifier**: 구현자와 다른 관점으로(가능하면 서브에이전트로) 해제 조건을 재검증한다.

## 작업 루프 (1 사이클)

1. `docs/autonomous/BACKLOG.md`를 읽고 `status: ready`인 최상위 항목 1개를 선택한다.
2. 항목의 해제 조건(acceptance)을 확인하고, 설계 요약을 작업 로그에 먼저 기록한다.
3. 구현한다. 이 저장소의 기존 원칙(아래 '불변 안전 규칙')을 절대 위반하지 않는다.
4. 검증한다: `venv/bin/python -m pytest` 전체 회귀 + 항목별 probe. 실패 시 수정 후 재실행.
5. 결과를 기록한다:
   - `docs/autonomous/CURRENT_STATUS.md` 상단에 사이클 결과 추가
   - `docs/autonomous/BACKLOG.md`에서 해당 항목 status 갱신 (`done` / `blocked:<사유>`)
6. 변경을 현재 feature 브랜치에 커밋한다 (컨벤션: 기존 커밋 메시지 스타일 유지).
7. 다음 항목으로 계속한다. 사용자 확인을 기다리지 않는다.

## 막혔을 때 (질문 대신 기록)

사용자에게 질문하지 말고 다음을 수행한다:

- 실행에 사용자 자격증명/서버 접근/실계좌가 필요한 항목 →
  `docs/autonomous/NEEDS_USER.md`에 **실행할 정확한 명령어와 함께** 기록하고
  해당 항목을 `blocked:needs-user`로 표시한 뒤 **다음 항목으로 넘어간다**.
- 안전 크리티컬 판단(실주문 활성화, 손절/익절 정책 변경)이 필요한 항목 →
  구현하지 말고 설계 문서만 작성 후 `blocked:needs-user-decision`으로 표시한다.

## 불변 안전 규칙 (이 계약으로도 해제되지 않음)

- `KIS_LIVE_ORDER_ENABLED` / `LIVE_ROLLOUT_ENABLED` / `approved` / `live_enabled` 활성화 금지
- Alpaca 주문 경로 활성화 금지 (데이터 전용)
- main 브랜치 직접 push 금지
- 운영 서버(Oracle) 설정 변경 금지
- 불일치 자동 보정 금지 (reconciliation은 차단만)
- 테스트 삭제·완화로 통과시키기 금지
- 시크릿을 코드/커밋에 포함 금지

## 이 계약이 사전 승인하는 것

사용자는 다음을 사전 승인했다 (매번 물어보지 않는다):

- feature 브랜치에서의 코드 수정·리팩터·테스트 추가
- 문서(docs/) 생성·갱신
- feature 브랜치 커밋 및 origin push (main 제외)
- 전체 테스트 실행
