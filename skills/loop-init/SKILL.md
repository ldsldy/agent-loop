---
name: loop-init
description: 저장소에 agent-loop 루프를 설치하거나 플러그인 관리 파일을 최신으로 맞춘다.
disable-model-invocation: true
---

# 루프 설치

저장소 하나에 루프를 깐다. 끝나면 `loop` 스킬로 첫 큰 단위를 고를 수 있다. 다시 돌려도 안전하다.

## 1. 파일 넣기

저장소 루트에서:

```bash
python -I "${CLAUDE_PLUGIN_ROOT}/scripts/init.py"            # 없는 파일만 넣는다
python -I "${CLAUDE_PLUGIN_ROOT}/scripts/init.py" --update   # 플러그인 관리 파일이 "다름"이면 덮어쓴다
```

출력의 파일마다 상태가 나온다. `.agent-loop/`와 `.github/auto-merge-policy.json`은 저장소 소유라 처음 한 번만 넣는다.
`.github/workflows/auto-merge-guard.yml`, `.github/scripts/auto_merge_guard.py`, `.github/scripts/review_scope.py`는 플러그인 관리 파일이다. 저장소에서 고치지 않는다.

## 2. 설정 채우기

`.agent-loop/config.json`의 씨앗 값을 이 저장소에 맞게 바꾼다. 코드와 CI 설정을 읽고 정하고, 정할 수 없는 값만 사용자에게 묻는다.

| 키 | 뜻 |
| --- | --- |
| `base_branch` | 루프 브랜치를 파고 루프 끝에 PR을 올리는 브랜치 |
| `review_paths` | 이 경로(접두어)를 건드린 변경만 테스트·리뷰한다. 도구·문서·에이전트 설정은 빼고, CI 워크플로처럼 테스트 결과를 바꾸는 파일은 넣는다 |
| `test_globs` | 테스트 파일 글롭(`*`는 `/`도 맞춘다). 테스트만 바뀐 조각 PR은 리뷰 없이 머지되고, 상세 리뷰 diff는 통계만 싣는다 |
| `stats_only_globs` | 상세 리뷰 diff에서 본문 대신 통계만 싣는 파일(데이터·생성물·문서) |
| `full_diff_dirs` | 확장자와 상관없이 본문을 싣는 폴더(의존성·빌드 설정) |
| `context_files` | 리뷰 자료에 싣는 저장소 규칙 문서(설계 원칙·테스트 규칙이 있는 곳) |
| `ci.results_artifact`, `ci.result_files` | CI가 올리는 테스트 결과 아티팩트 이름과 그 안의 NUnit3 XML 파일 이름 |
| `evidence` | 리뷰 자료에 붙일 프로젝트 전용 검사. `{"title", "cmd"}`, 인자의 `{base}`·`{sha}`·`{tree}`가 바뀐다. JSON 출력의 `hard_fails`는 최소 리뷰를 실패시킨다 |
| `slice_test_hint` | 구현 세션이 PR 전에 조각 테스트를 어떻게 돌리는지(구현 지시에 그대로 들어간다) |
| `loop_branch_ruleset` (선택) | 루프 브랜치 생성을 막는 규칙 세트 `{"name", "id"}`. 시작 때 잠깐 끈다 |
| `runner.task` (선택) | self-hosted 실행기를 켜는 Windows 로그온 작업. 있으면 `gh pr create` 뒤 훅이 실행기를 확인한다 |

`models.json`(역할별 모델), `rubric.json`(평가표)은 씨앗 그대로 써도 된다. 평가표 그룹별 배점 합은 100이어야 한다. 프로젝트 성격에 맞게 항목 설명을 고친다.
`.github/auto-merge-policy.json`의 `deny`·`allow`·`test_glob`을 이 저장소 경로로 맞춘다.

다 바꾸면 `python -I "${CLAUDE_PLUGIN_ROOT}/scripts/quality_gate.py" --rubric`이 평가표를 출력해야 한다(설정을 읽지 못하면 이유를 말하고 멈춘다).

## 3. GitHub 설정

사용자 확인을 받고 한다(외부 서비스 쓰기).

- `base_branch`가 원격에 있다. 머지 방식은 squash만 허용한다.
- 라벨: `ready-for-agent`, `human`, `spec`, `low-risk`(정책의 `label`).
- `loop/**` 규칙 세트: 필수 검사에 정책의 `required_checks`(씨앗은 `auto-merge-guard`). 루프 브랜치 생성까지 막으면 `loop_branch_ruleset`에 적는다.
- 저장소 CI에 `python -I <플러그인>/scripts/init.py --check`를 넣어 플러그인 관리 파일이 플러그인 것과 같은지 검사한다. 플러그인은 커밋 SHA로 고정해 받고(`actions/checkout`의 `ref`), 비밀 토큰으로 받으면 `persist-credentials: false`를 둔다(뒤에 PR 코드가 돈다).
- 테스트 CI는 `base_branch`·`main`으로 가는 PR에서만 돌고, `.github/scripts/review_scope.py`가 true인 PR만 테스트한다. 결과는 `ci.results_artifact` 이름의 아티팩트로 올린다. 판정 잡은 PR 쪽이 아니라 기준 브랜치 커밋의 `review_scope.py`·`config.json`을 읽고, 두 파일과 테스트 워크플로를 `review_paths`에 넣는다(PR이 판정을 바꿔 테스트를 건너뛰지 못하게). 조각 PR(`loop/**` 대상)에서는 돌지 않게 한다.

## 4. 에이전트 규칙

- 저장소 `.claude/settings.json`에 이 플러그인의 마켓플레이스(`extraKnownMarketplaces`)와 `enabledPlugins`를 넣어 세션마다 켜지게 한다.
- 저장소 `CLAUDE.md`에 한 줄을 둔다: 큰 단위 작업은 `loop` 스킬, 리뷰는 `loop-review` 스킬을 따른다. 설계 원칙과 테스트 규칙은 `CLAUDE.md`에 두고 `context_files`에 넣는다.
- 최소 리뷰는 `rubric.json`의 `minimal_skill`(씨앗은 `mattpocock-skills:code-review`)을 쓴다. 그 플러그인이 없으면 설치하거나 쓰는 스킬로 바꾼다.
- 리뷰어 실행에는 `claude` CLI가, `models.json`에 `codex` 리뷰어가 있으면 `codex` CLI가 필요하다. `gh`는 로그인돼 있어야 한다.

## 끝

출력에 "다름"이 없고, `--rubric`이 돌고, `python -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" status`가 마일스톤 목록을 낸다.
