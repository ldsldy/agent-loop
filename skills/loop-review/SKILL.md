---
name: loop-review
description: 루프의 품질 리뷰 절차. 조각 PR의 최소 리뷰, 루프 끝 상세 리뷰, 리뷰 판정을 다시 내거나 결과 파일을 읽을 때 쓴다.
---

# 루프 품질 리뷰

리뷰는 두 단계다. 항목·배점·게이트·통과 점수·개선 한도는 저장소의 `.agent-loop/rubric.json`, 리뷰어와 모델은 `.agent-loop/models.json`이 정한다.
채점 공식과 판정은 `${CLAUDE_PLUGIN_ROOT}/scripts/quality_gate.py`, 채점 규칙은 `${CLAUDE_PLUGIN_ROOT}/references/scoring.md`다(실행기가 리뷰 자료에 싣는다).
아래 `$L`은 `${CLAUDE_PLUGIN_ROOT}/scripts`다. 평가표는 `python -I "$L/quality_gate.py" --rubric`으로 본다.

| 단계 | 언제 | 누가 | 통과 |
| --- | --- | --- | --- |
| **최소 리뷰** | 조각 PR마다(CI 없음) | `models.json`의 `review.minimal` 리뷰어가 `rubric.json`의 `minimal_skill`로 | 최소 리뷰 항목 모두 pass, 증거 명령 `hard_fails` 없음 |
| **상세 리뷰** | 루프의 조각이 전부 머지된 뒤 | `review.detailed` 리뷰어 전원 | 리뷰어마다 그룹별 `pass_score` 이상, 치명적 결함 0건, 게이트 통과 |

## 건너뛰는 변경

리뷰 대상 경로(`config.review_paths`)를 건드리지 않는 변경은 리뷰하지 않는다. `review.py`가 리뷰어를 부르지 않고 `verdict.json`에 `skipped`를 남긴다. 그런 PR은 사용자 검토로 머지한다.
테스트 파일(`config.test_globs`)만 바뀐 조각 PR은 최소 리뷰 없이 루프 브랜치에 머지한다. 그 근거는 루프 끝 CI다. `pr_chain.sh`가 판단한다.

## 최소 리뷰

```bash
python -I "$L/review.py" --level minimal --base origin/loop/<이름> --pr <번호>
```

PR 브랜치 최신 커밋을 체크아웃하고 돌린다. 보통은 `pr_chain.sh`가 전용 worktree에서 돌린다(`loop` 스킬 "세션 운영").
fail이 하나라도 있으면 실패, unknown이 있으면 미완료다. 실패면 지적을 고친 커밋으로 다시 돌린다. 결과(통과/실패와 지적)는 PR 코멘트로 남긴다.

## 상세 리뷰

```bash
gh pr create --base <기준 브랜치> --head loop/<이름> ...                 # CI가 돈다
gh run list --branch loop/<이름> -L 1 --json databaseId,headSha,conclusion   # 그 PR 커밋의 run id
git switch --detach origin/loop/<이름>
python -I "$L/review.py" --level detailed --base origin/<기준 브랜치> --ci-run <run id> --spec <spec 이슈 번호...>
```

- `--spec`에는 루프가 다룬 spec 이슈를 전부 준다. 본문이 리뷰 자료에 실려 동작이 spec대로인지의 근거가 된다.
- 리뷰 자료의 diff는 코드와 `config.full_diff_dirs` 본문만 싣고, `config.stats_only_globs`·테스트는 통계만 싣는다. 테스트 근거는 CI 아티팩트(`config.ci`)다. 리뷰는 테스트를 다시 돌리지 않는다.
- `config.evidence` 명령의 출력도 자료에 실린다. 프로젝트 전용 검사(의존 그래프, 패키지 보안 등)가 여기로 들어온다.
- 미달이면 리뷰어 issues로 개선 조각을 만든다(`loop` 스킬 7). 한도를 넘기면 멈추고 점수와 남은 지적을 사용자에게 보고한다. 통과하면 사용자에게 알린다. 기준 브랜치 머지는 사용자가 한다.
- 리뷰 자료가 한도(`CONTEXT_LIMIT`)를 넘으면 실행기가 멈춘다. 그때는 나눠 리뷰하는 방법을 사용자와 정한다.

## 결과 파일

`ci-results/quality/<sha 7자리>-<minimal|detailed>/`에 리뷰어별 JSON(schema `agent-loop-quality-v1`), 로그, `verdict.json`이 남는다.
상세 리뷰 판정만 다시 내려면 `python -I "$L/quality_gate.py" claude.json codex.json --sha <커밋>`.
`git_sha`가 대상 커밋과 다른 리뷰는 무효다. 오탐은 근거를 남기고 정정할 수 있다.

## 적을 것

조각 PR 본문: 맡은 이슈, 범위, 수용 기준, 조각 테스트 명령과 결과. 최소 리뷰 뒤 코멘트: 통과/실패, 실패 항목, 지적.
상세 리뷰 보고: 최종 판정, 리뷰어별 그룹 점수(항목별), 의견 갈림, 게이트별 상태와 근거, 치명적 결함 수, 개선 횟수, 남은 지적.
