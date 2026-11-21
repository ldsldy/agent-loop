---
name: loop
description: 마일스톤 하나를 루프 브랜치로 열어 조각 PR로 나눠 끝까지 돌리는 오케스트레이터 절차. "루프", "다음 큰 단위", "마일스톤 시작", 루프의 조각·리뷰·머지·cleanup 다음 행동을 정할 때 쓴다.
---

# 루프 오케스트레이터

큰 단위(GitHub 마일스톤 하나 = 루프 브랜치 하나)를 고르고, 조각으로 나눠 구현 세션에 차례로 넘기고, 검사와 리뷰를 돌리고, 끝나면 정리한다.
조정 세션은 순서와 정리만 정한다. 통과·실패는 기계 검사와 리뷰가, 제품 판단은 사용자가 정한다. Unity 저장소 전용이다.

저장소 값은 `.agent-loop/`에 있다(`config.json`, `models.json`, `rubric.json`). 없으면 `loop-init` 스킬로 먼저 만든다.
프로젝트 규칙(설계 원칙, 테스트 규칙)은 저장소 `CLAUDE.md`가 정본이다. 명령은 저장소 루트에서 돌린다. 아래 `$L`은 `${CLAUDE_PLUGIN_ROOT}/scripts`다.

## 흐름

```
pick → (사용자 승인) loop/<이름> → 설계: 조각 목록
     → 조각마다 차례로: 구현 → 조각 검사 → PR(→loop, CI 없음) → 기계 판정·최소 리뷰 → 머지
     → 전부 머지되면 루프 → 기준 브랜치 PR(유일한 CI) → 상세 리뷰(미달이면 개선 조각 → 개선분 재리뷰) → 사용자가 머지
```

1. **고르기**: `python -I "$L/orchestrator.py" status`로 마일스톤별 막힘을 보고 `pick`으로 고른다.
   마일스톤의 열린 이슈가 전부 `ready-for-agent`이고 마일스톤 밖의 열린 이슈에 막히지 않았을 때(본문 `Blocked by: #N`)만 시작할 수 있다.
   `human` 라벨 이슈는 가져가지 않는다. 시작할 것이 없으면 막힌 이유를 보고하고 멈춘다.
2. **시작**: 사용자 승인을 받아 기준 브랜치에서 `loop/<이름>`을 만든다(`config.loop_branch_ruleset`이 있으면 잠깐 끄고 만든 뒤 다시 켠다).
   `orchestrator.py start --milestone <번호> --branch loop/<이름>`이 상태 파일(`ci-results/loop/<이름>.json`)을 만든다.
   그 뒤로는 `orchestrator.py next --unit <이름>`이 지금 할 일과 모델을 JSON으로 알려 준다. 조각의 진행은 저장하지 않고 PR에서 읽는다.
3. **나누기**: 설계 세션이 조각 목록(JSON 배열: `id`·`title`·`issues`·`acceptance`·`files`·`complexity`(simple|medium|complex)·`depends_on`)을 쓰고
   `orchestrator.py plan --unit <이름> --slices <파일>`로 넣는다. 조각은 따로 PR·리뷰할 수 있을 만큼 작게 나눈다.
   `files`(파일이나 폴더)는 범위의 기계 판정에, `complexity`는 구현 모델과 리뷰 생략 여부에 쓰이므로 실제대로 적는다.
4. **구현**: 구현 세션에 조각을 하나씩 SendMessage로 맡긴다. 세션은 `origin/loop/<이름>`에서 `task/<루프 이름>-<조각 id>`를 다시 파고(`git switch -C`) 구현한다.
   push 전에 그 worktree에서 `python -I "$L/slice_check.py"`(조각 검사)를 통과시키고, 통과한 상태 그대로 전부 커밋해 PR을 올린다. PR 본문에 수용 기준을 적는다.
   통과 기록이 없는 트리는 훅이 `git push`를 막는다. 제품 동작을 정해야 하는 선택이 나오면 그 조각은 멈추고 사용자에게 묻는다.
5. **머지**: `bash "$L/pr_chain.sh" <루프 이름> <PR>`이 조각 검사 기록 확인 → 최소 리뷰 → 머지 → 브랜치 정리 → `next` 출력을 잇는다(`loop-review` 스킬).
   통과면 묻지 않고 머지한다. 리뷰 대상 경로 밖의 변경(`skipped`)은 사용자 검토로 머지한다.
6. **정리**: `python -I "$L/orchestrator.py" cleanup`이 PR이 머지된 `task/*` 브랜치(로컬·원격)만 지운다. 그 브랜치를 쓰던 worktree는 detach한다.
   지우기 전에 남길 결과는 PR 코멘트에 둔다.
7. **상세 리뷰**: 조각이 전부 머지되면 루프 → 기준 브랜치 PR을 열고 그 PR의 CI run으로 상세 리뷰를 돌린다(`loop-review` 스킬).
   결과는 `orchestrator.py record --unit <이름> --detailed passed|failed --verdict <verdict.json>`로 남긴다.
   미달이면 리뷰어 issues를 개선 조각으로 만들어 `plan --improvement`로 넣고 4~5를 거친다. 그 뒤의 상세 리뷰는 `next`가 준 `previous`를 `--previous`로 넘긴다.
   개선 횟수가 `rubric.json`의 `max_improvements`에 닿으면 `next`가 stop을 낸다.
8. **끝**: 통과하면 점수와 남은 지적을 사용자에게 알린다. 기준 브랜치 머지는 사용자가 한다.

## 사용자에게 묻고 멈추는 곳

큰 단위 시작, 제품 동작 선택, 리뷰를 건너뛴 PR 머지, 기준 브랜치 머지, 개선 한도를 넘긴 미달.
그 밖에는 멈추지 않는다. 루프 하나는 시작 승인부터 상세 리뷰 보고까지 사용자 턴 없이 돈다.

## 세션 운영

Desktop 앱은 자동 이벤트(다른 세션의 보고, 백그라운드 완료)로 시작된 턴에서 새 세션을 열지 못한다. 그래서 세션은 **사용자 턴인 시작 승인 턴에 연다.**

- **여는 것**: 설계 세션 하나와 구현 세션 하나(`use_worktree`, `source_branch: loop/<이름>`). 구현 세션 브리핑은 "조정 세션의 메시지 전까지 대기"다.
  구현 세션은 하나만 둔다. Unity 프로젝트 하나는 한 번에 한 Unity만 열 수 있고 worktree마다 `Library`를 새로 만들어야 해서, 조각은 차례로 돈다.
- **조각 시작**: `next`가 implement를 내면 그 세션에 조각 id, 상태 파일 위치, 설계 메모,
  `git fetch origin && git switch -C task/<루프 이름>-<조각 id> origin/loop/<이름>`을 SendMessage로 보낸다.
  `next`가 준 모델이 세션의 지금 모델과 다르면 보내기 전에 `set_session_model`로 바꾼다. 바꾸지 못하면 그대로 진행하고 PR 코멘트에 적는다.
- **조각 사이**: 조각 PR이 머지되면 다음 조각을 보내기 전에 구현 세션을 `clear_session`으로 비운다. 조각의 상태는 git과 상태 파일에 있어 잃는 것이 없다.
  거부되면 그대로 진행한다.
- **머지 잇기**: `pr_chain.sh`를 백그라운드로 돌리고, 끝나면 출력을 읽어 같은 턴에 다음 조각까지 보낸다.
  `slice-check=missing`이나 `minimal=fail`이면 PR 코멘트의 이유를 구현 세션에 보내 고치게 하고, 고친 커밋이 올라오면 다시 돌린다(리뷰는 커밋마다 따로다).
  리뷰어가 권한 부족으로 unknown을 내면 조정 세션이 직접 확인해 근거와 함께 정정한다.
- 조정 세션의 체크아웃은 상세 리뷰 때 detached로 바뀐다. 끝나면 `loop/<이름>`으로 돌아와 `git pull --ff-only`를 한다.
- 다 쓴 세션(설계는 plan 뒤, 구현은 루프 끝)은 보관한다.
