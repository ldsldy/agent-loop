---
name: loop
description: 마일스톤 하나를 루프 브랜치로 열어 조각 PR로 나눠 끝까지 돌리는 오케스트레이터 절차. "루프", "다음 큰 단위", "마일스톤 시작", 루프의 조각·리뷰·머지·cleanup 다음 행동을 정할 때 쓴다.
---

# 루프 오케스트레이터

큰 단위(GitHub 마일스톤 하나 = 루프 브랜치 하나)를 고르고, 조각으로 나눠 설계·구현 세션에 넘기고, 리뷰 두 단계를 돌리고, 끝나면 정리한다.
오케스트레이터는 무엇을 먼저 할지와 정리만 정한다. 품질 판단은 리뷰가, 제품 판단은 사용자가 한다.

저장소 값은 `.agent-loop/`에 있다. `config.json`(기준 브랜치 `base_branch`, 조각 테스트 지시 `slice_test_hint` 등), `models.json`(역할별 모델), `rubric.json`(리뷰 평가표·개선 한도).
없으면 `loop-init` 스킬로 먼저 만든다. 프로젝트 규칙(설계 원칙, 테스트 규칙, 실행 제약)은 저장소 `CLAUDE.md`가 정본이다.

명령은 저장소 루트에서 돌린다. 아래 `$L`은 `${CLAUDE_PLUGIN_ROOT}/scripts`다.

## 흐름

```
pick → (사용자 승인) loop/<이름> 생성 → 설계: 조각 목록 → 조각마다 구현(바꾼 테스트만 로컬) → PR(→loop, CI 없음) → 최소 리뷰 → 머지 → cleanup
     → 조각이 전부 머지되면 루프 → 기준 브랜치 PR(유일한 CI) → 상세 리뷰(미달이면 개선 조각) → 사용자에게 알림 → 사용자가 머지
```

1. **고르기**: `python -I "$L/orchestrator.py" status`로 마일스톤별 막힘을 보고, `pick`으로 다음 큰 단위를 고른다.
   마일스톤의 열린 이슈가 전부 `ready-for-agent`이고, 마일스톤 밖의 열린 이슈에 막히지 않았을 때(본문 `Blocked by: #N`)만 시작할 수 있다.
   `human` 라벨 이슈는 가져가지 않는다. 시작할 것이 없으면 막힌 이유를 사용자에게 보고하고 멈춘다.
2. **시작**: 큰 단위 시작은 사용자 결정이다. 승인을 받으면 기준 브랜치에서 `loop/<이름>`을 만든다.
   `config.loop_branch_ruleset`이 있으면 그 규칙 세트가 루프 브랜치 생성을 막으므로, 잠깐 끄고 만든 뒤 다시 켠다.
   그다음 `orchestrator.py start --milestone <번호> --branch loop/<이름>`으로 상태 파일(`ci-results/loop/<이름>.json`)을 만든다.
   이후 단계마다 `orchestrator.py next --unit <이름>`이 지금 할 일(JSON: design·implement·review·cleanup·detailed-review·improve·stop·notify-user)과 모델을 알려 준다.
   조각의 진행(PR 열림·머지)은 저장하지 않고 PR에서 읽는다. 조각 브랜치 이름은 `task/<루프 이름>-<조각 id>`로 고정이다.
3. **나누기**: 설계 세션(모델은 `models.json`의 `design`, 복잡도로 고름)이 조각 목록을 쓴다. 조각마다 제목, 맡은 이슈, 수용 기준, 바뀔 파일, 복잡도, 의존 조각.
   조각은 독립적으로 PR·리뷰할 수 있을 만큼 작게, 파일이 겹치는 조각은 차례로 둔다.
   목록(JSON 배열: `id`·`title`·`issues`·`acceptance`·`files`·`complexity`(simple|medium|complex)·`depends_on`)을 `orchestrator.py plan --unit <이름> --slices <파일>`로 넣는다. 중복·없는 의존·순환은 거부된다.
4. **구현**: 시작 때 연 구현 세션(아래 "세션 운영")에 조각을 하나씩 SendMessage로 맡긴다. 세션은 `origin/loop/<이름>`에서 `task/<루프 이름>-<조각 id>`를 다시 파고(`git switch -C`), PR 본문에 수용 기준을 적는다.
   PR 전에 그 worktree에서 `config.slice_test_hint`대로 이 조각이 만들거나 고친 테스트만 돌린다. 오류가 없어야 PR을 올리고, 명령과 결과를 PR 본문에 적는다(조각의 테스트 게이트, 최소 리뷰가 확인).
   제품 동작을 정해야 하는 선택이 나오면 그 조각은 멈추고 사용자에게 묻는다. 제품 동작이 아닌 판단(테스트 근거·범위)은 조정 세션이 `CLAUDE.md`로 답한다.
5. **최소 리뷰**: 조각 PR은 CI를 돌리지 않는다. 최소 리뷰(`loop-review` 스킬)를 돌려 실패면 구현 세션이 고쳐 다시, 통과면 묻지 않고 머지한다.
   리뷰를 건너뛴 변경(리뷰 대상 경로 밖)은 사용자 검토로 머지한다. 테스트 파일만 바뀐 PR은 최소 리뷰 없이 머지한다. 둘 다 `pr_chain.sh`가 판단한다.
6. **정리**: 머지되면 `python -I "$L/orchestrator.py" cleanup`. PR이 머지된 `task/*` 브랜치(로컬·원격)만 지운다. 그 브랜치를 쓰던 worktree는 지우지 않고 detach한다(구현 세션이 다음 조각에 계속 쓴다).
   `loop/*`·기준 브랜치·`main`·다른 세션의 `claude/*`는 남긴다. 지우기 전에 남길 결과는 PR 코멘트에 둔다(worktree 안 `ci-results/`는 함께 지워진다).
7. **상세 리뷰**: 조각이 전부 머지되면 루프 → 기준 브랜치 PR을 연다(그 PR의 CI가 루프의 유일한 CI). 그 CI run으로 상세 리뷰(`loop-review` 스킬)를 돌리고,
   결과를 `orchestrator.py record --unit <이름> --detailed passed|failed --verdict <verdict.json>`로 남긴다.
   미달이면 리뷰어 issues를 개선 조각으로 만들어 `plan --improvement`로 넣고 4~6을 거친다(개선 횟수가 `rubric.json`의 `max_improvements`에 닿으면 `next`가 stop을 낸다).
8. **끝**: 통과하면 점수와 남은 지적을 사용자에게 알린다. 기준 브랜치 머지는 사용자가 한다. 머지 뒤 저장소의 진행 문서(있으면)를 고친다.

## 사용자에게 묻고 멈추는 곳

큰 단위 시작(루프 브랜치 생성), 제품 동작 선택, 리뷰를 건너뛴 PR 머지, 기준 브랜치 머지, 개선 한도를 넘긴 미달.
그 밖에는 멈추지 않는다. 루프 하나는 시작 승인부터 상세 리뷰 보고까지 사용자 턴 없이 돈다.

## 세션 운영

Desktop 앱은 자동 이벤트(CI 완료 알림·다른 세션의 보고)로 시작된 턴에서 새 세션을 열지 못하게 한다(`start_session` 거부, 카드만 가능). 카드는 `source_branch`를 버려 기준 브랜치도 틀어진다.
설계가 끝나는 때, 앞 조각이 머지되는 때는 모두 자동 턴이다. 그래서 세션은 **사용자 턴인 시작 승인 턴에 한꺼번에 연다.**

- **시작 승인 턴에 여는 것**: 설계 세션 하나, 구현 세션 하나(`use_worktree`, `source_branch: loop/<이름>`, 모델은 `implement.by_complexity.default`). 구현 세션 브리핑은 "조정 세션의 시작 메시지 전까지 코드·브랜치를 건드리지 않고 CLAUDE.md만 읽고 대기"로 쓴다.
  조각이 서로 독립이라 동시에 돌릴 것이 확실하면 그 수만큼 구현 세션을 더 연다(파일이 겹치는 조각은 한 세션에서 차례로).
- **조각 시작**: `next`가 implement를 내면 그 세션에 SendMessage로 조각 id, 수용 기준이 있는 상태 파일 위치, 설계 메모, `git fetch origin && git switch -C task/<루프 이름>-<조각 id> origin/loop/<이름>`을 보낸다. SendMessage는 자동 턴에서도 된다.
  조각이 complex면 보내기 전에 `set_session_model`로 `implement.by_complexity.complex` 모델로 바꾸고, 끝나면 되돌린다. 바꾸지 못하면 지금 모델로 진행하고 PR 코멘트에 적는다.
- **개선 조각**도 같은 구현 세션에 보낸다. 상세 리뷰 결과의 issues를 메시지에 담는다.
- **최소 리뷰 → 머지**는 `bash "$L/pr_chain.sh" <루프 이름> <PR>`을 백그라운드로 돌려 잇는다. 전용 worktree(`.claude/worktrees/review`)에서 최소 리뷰를 돌리고, pass면 코멘트·머지·브랜치 정리를 한 뒤 `next`를 출력한다.
  끝나면 결과를 읽고 다음 조각 SendMessage까지 같은 턴에 한다. 리뷰어가 권한 부족으로 unknown을 내면 조정 세션이 직접 확인해 근거와 함께 정정한다.
- 머지 전에 더할 커밋이 있는 PR은 `pr_chain.sh`를 쓰지 말고 커밋을 다 올린 뒤 돌린다. 리뷰는 커밋마다 따로 한다(`git_sha`가 다르면 무효). 고친 커밋이 올라오면 다시 돌린다.
- 조정 세션의 체크아웃은 리뷰 때 detached로 바뀐다. 리뷰가 끝나면 `loop/<이름>`으로 돌아와 `git pull --ff-only`를 한다.
- 다 쓴 세션(설계는 plan 뒤, 구현 세션은 루프 끝)은 보관한다. 구현 세션은 조각마다 보관하지 않고 다음 조각에 쓴다.
