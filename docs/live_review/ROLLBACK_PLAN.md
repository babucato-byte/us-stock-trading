# Rollback Plan

## 1. 대상 범위

- 이번 run(문서 작성 시점 최신 커밋): `c34fde1a664641799e4a37a02372f5d41a9e72ae`,
  브랜치 `orchestrator/20260722-235153-us-stock-trading`.
- 이전 run 브랜치: `orchestrator/20260722-021713-us-stock-trading`
  (최신 커밋 `3ffa0ec` — t0~t8).

실측 확인 결과, `3ffa0ec`(이전 run 브랜치의 tip)는 현재 브랜치(`orchestrator/20260722-235153-...`)
히스토리의 조상 커밋이다(`git merge-base --is-ancestor 3ffa0ec HEAD` → true). 즉 이번 run은
독립적인 별개 작업이 아니라 이전 run의 연장선(t0~t8 위에 t9~t11을 추가)이며, 두 브랜치가
가리키는 커밋 집합은 사실상 하나의 연속된 작업 이력이다.

## 2. main 브랜치 영향 확인

```
git log -1 --format='%H' main
# 158671ede0320c4c22179b75cb76c4e9eb8ae1fa

git diff main HEAD --stat
# 22 files changed, 3400 insertions(+), 27 deletions(-)
```

`main`은 두 브랜치가 분기한 지점(`158671ede0320c4c22179b75cb76c4e9eb8ae1fa`)에서 전혀 이동하지
않았다. 즉 **`main`에는 이번 run과 이전 run의 어떤 커밋도 병합되거나 반영되지 않았다.**
`git diff main HEAD` 위 22개 변경 파일은 모두 두 orchestrator 브랜치 안에만 존재하며, `main`을
체크아웃하면 이 변경들은 보이지 않는다.

## 3. 롤백 절차

**결론: `main`이 전혀 변경되지 않았으므로, 두 orchestrator 브랜치(현재 브랜치와
`orchestrator/20260722-021713-us-stock-trading`)를 폐기하는 것만으로 롤백은 충분하다.
`git revert`나 `main`에 대한 어떤 조치도 필요 없다.**

1. **작업 중인 변경사항 보존 확인**: 롤백 실행 전 `git status --porcelain`으로 두 브랜치 모두
   커밋되지 않은 변경이 없는지 확인한다(이번 run은 clean 상태에서 시작했으며,
   `docs/live_review/*.md` 추가만 발생).
2. **`main`으로 전환**: `git checkout main` (또는 `git switch main`). `main`은 위 2절 확인대로
   이번 작업의 영향을 받지 않았으므로 이 시점에 이미 롤백 완료 상태와 동일하다.
3. **브랜치 폐기(운영자 승인 후 실행)**:
   ```
   git branch -D orchestrator/20260722-235153-us-stock-trading
   git branch -D orchestrator/20260722-021713-us-stock-trading
   ```
   원격에도 두 브랜치가 push되어 있다면:
   ```
   git push origin --delete orchestrator/20260722-235153-us-stock-trading
   git push origin --delete orchestrator/20260722-021713-us-stock-trading
   ```
   (원격 저장소 존재 여부 및 실제 삭제 실행은 `TBD(운영자 기입)` — 브랜치 삭제는 되돌리기
   어려운 작업이므로 반드시 운영자가 직접 확인 후 실행한다.)
4. **작업 산출물(런타임 상태 파일) 정리**: 이번 run은 `KILL_SWITCH_STATE.json`,
   `NOTIFICATION_HEALTH_STATE.json`, `KILL_SWITCH` 센티널 파일을 생성하지 않았다(문서 작성
   시점 기준 세 파일 모두 저장소에 존재하지 않음 확인). 만약 롤백 시점에 이 파일들이 존재한다면,
   그것은 이번 run이 아니라 그 이후 실제 운영 중 발생한 상태이므로 삭제 전 반드시 내용을 확인하고
   운영자가 판단한다.

## 4. 롤백 후 확인

- `git log -1 --format='%H'`가 `158671ede0320c4c22179b75cb76c4e9eb8ae1fa`(또는 그 이후 `main`에
  정상적으로 병합된 커밋)를 가리키는지 확인.
- `venv/bin/python -m pytest -q`를 재실행해 롤백된 상태에서도 회귀가 없는지 확인
  (`TBD(운영자 기입)`: 롤백 시점 실측 결과 기입).

## 5. 롤백 담당자

`TBD(운영자 기입)`
