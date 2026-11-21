---
name: loop-review
description: 루프의 품질 리뷰 절차. 조각 검사, 조각 PR의 최소 리뷰, 루프 끝 상세 리뷰, 리뷰 판정을 다시 내거나 결과 파일을 읽을 때 쓴다.
---

# 루프 품질 리뷰

기계가 정할 수 있는 것은 기계가 정하고, 판단이 필요한 것만 리뷰어가 본다.
항목·배점·게이트·통과 점수·개선 한도는 저장소의 `.agent-loop/rubric.json`, 리뷰어와 모델은 `.agent-loop/models.json`이 정한다.
아래 `$L`은 `${CLAUDE_PLUGIN_ROOT}/scripts`다. 평가표는 `python -I "$L/quality_gate.py" --rubric`으로 본다.

| 단계 | 언제 | 누가 | 통과 |
| --- | --- | --- | --- |
| **조각 검사** | push 전, 구현 worktree | `slice_check.py`(Unity batchmode) | 조각의 증명 계약: 컴파일, 바뀐 테스트와 지킴이 통과, 물러나는 테스트만 수정, 바꾸는 조각은 빨강에서 초록, 실행 중 새 파일 없음 |
| **최소 리뷰** | 조각 PR마다(CI 없음) | 기계 판정, 남은 항목만 `review.minimal` 리뷰어 하나 | 최소 리뷰 항목 pass, 증거 명령 `hard_fails` 없음 |
| **상세 리뷰** | 조각이 전부 머지된 뒤 | `review.detailed` 리뷰어 전원 | 리뷰어마다 그룹별 `pass_score` 이상, 치명적 결함 0건, 게이트 통과 |

## 조각 검사

```bash
python -I "$L/slice_check.py"      # task/<루프>-<조각> 브랜치의 worktree에서
```

조각의 증명 계약(`loop` 스킬 "증명 계약")을 확인한다. 루프 브랜치에서 갈라진 뒤 바뀐 테스트 파일과 지킴이(`guards`)의 fixture만 골라 EditMode·PlayMode로 돌린다.
돌릴 테스트가 없으면 컴파일만 확인한다. 기존 테스트는 `retire`에 적힌 것만 고치거나 지울 수 있다.
바꾸는 조각(`kind: change`)은 구현 전 코드에 이 조각의 테스트만 얹어 한 번 더 돌린다. 거기서 실패하거나 컴파일되지 않아야 통과다. 끝나면 작업 트리를 되돌린다.
같은 조각에서 3번 실패하면 더 돌지 않는다(`--again`으로 다시 연다). 통과하면 작업 트리 해시로 기록을 남긴다. 검사한 그대로 전부 커밋해야 PR 커밋과 맞는다. 파일을 더 바꾸면 다시 돌린다.
Unity가 실행 중 고친 추적 파일은 검사 전 내용으로 되돌리고, 새로 생긴 파일(커밋에서 빠진 `.meta` 등)은 실패로 알린다.
이 PC의 CI 실행기가 작업 중이면 돌리지 않는다. 기록이 없는 트리는 훅이 `git push`를 막고 `pr_chain.sh`가 리뷰·머지를 하지 않는다.

## 최소 리뷰

```bash
python -I "$L/review.py" --level minimal --base origin/loop/<이름> --pr <번호> --unit <상태 파일>
```

PR 브랜치 최신 커밋을 체크아웃하고 돌린다. 보통은 `pr_chain.sh`가 전용 worktree(`.claude/worktrees/review`)에서 돌린다.

- `rubric.json`에서 `auto`가 붙은 항목은 기계가 먼저 본다. `tests_kept`는 기존 테스트 파일을 고치거나 지우지 않았을 때,
  `declared_files`는 테스트가 아닌 바뀐 파일이 모두 조각의 `files` 안일 때 통과로 확정한다. 확정하지 못한 항목은 리뷰어가 본다.
- `simple` 조각이 기계 판정을 모두 통과하면 리뷰어를 부르지 않는다. 남은 항목은 상세 리뷰가 본다.
- 그 밖에는 리뷰어 하나가 규칙 문서·PR 본문·diff를 받아 남은 항목만 pass·fail·unknown으로 낸다. fail이 있으면 실패, unknown이 있으면 미완료다.
- 리뷰 대상 경로(`config.review_paths`)를 건드리지 않는 변경은 리뷰하지 않고 `skipped`를 남긴다. 그런 PR은 사용자 검토로 머지한다.
- 테스트 파일(`config.test_globs`)만 바뀐 조각 PR은 최소 리뷰 없이 머지한다(조각 검사는 거친다).

## 상세 리뷰

```bash
gh pr create --base <기준 브랜치> --head loop/<이름> ...                 # CI가 돈다
gh run list --branch loop/<이름> -L 1 --json databaseId,headSha,conclusion   # 그 PR 커밋의 run id
git switch --detach origin/loop/<이름>
python -I "$L/review.py" --level detailed --base origin/<기준 브랜치> --ci-run <run id> --spec <spec 이슈 번호...> [--previous <이전 verdict.json>]
```

- `--spec`에는 루프가 다룬 spec 이슈를 전부 준다. 본문이 동작이 spec대로인지의 근거가 된다.
- diff는 코드와 `config.full_diff_dirs` 본문만 싣고, `config.stats_only_globs`·테스트는 통계만 싣는다. 테스트 근거는 CI 아티팩트(`config.ci`)다.
- `config.evidence` 명령의 출력도 자료에 실린다. 프로젝트 전용 검사(의존 그래프, 패키지 보안 등)가 여기로 들어온다.
- 개선 조각 뒤의 재리뷰는 `--previous`를 준다. 리뷰어마다 자기 이전 채점과 그 뒤의 diff만 받아 평가표 전체를 다시 낸다. 전체 diff를 다시 싣지 않는다.
- 미달이면 리뷰어 issues로 개선 조각을 만든다(`loop` 스킬 7). 한도를 넘기면 멈추고 점수와 남은 지적을 사용자에게 보고한다.
- 리뷰 자료가 한도(`CONTEXT_LIMIT`)를 넘으면 실행기가 멈춘다. 그때는 나눠 리뷰하는 방법을 사용자와 정한다.

## 결과 파일

`ci-results/quality/<sha 7자리>-<minimal|detailed>/`에 리뷰어별 JSON(schema `agent-loop-quality-v1`), 로그, `verdict.json`이 남는다.
최소 리뷰의 `verdict.json`은 `machine`에 기계가 통과로 확정한 항목(`passed`)과 상세 리뷰로 미룬 항목(`deferred`)을 담는다.
상세 리뷰 판정만 다시 내려면 `python -I "$L/quality_gate.py" claude.json codex.json --sha <커밋>`.
`git_sha`가 대상 커밋과 다른 리뷰는 무효다. 오탐은 근거를 남기고 정정할 수 있다.

## 적을 것

조각 PR 본문: 맡은 이슈, 범위, 수용 기준. 머지 코멘트(`pr_chain.sh`가 쓴다): 최소 리뷰 결과, 기계 판정, 조각 검사 요약.
상세 리뷰 보고: 최종 판정, 리뷰어별 그룹 점수, 의견 갈림, 게이트별 상태와 근거, 치명적 결함 수, 개선 횟수, 남은 지적.
