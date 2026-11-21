# agent-loop 설계

Unity 저장소의 개발 루프(마일스톤 → 루프 브랜치 → 조각 PR → 조각 검사 → 최소 리뷰 → 머지 → 상세 리뷰)를 돌리는 Claude Code 플러그인.
Unity 전용이다(2026-11-21 사용자 결정). 절차는 `skills/loop`, 리뷰는 `skills/loop-review`, 설치는 `skills/loop-init`이 정본이다.

## 원칙

- **사람이 빠진 자리는 기계가 맡는다.** 통과·실패로 떨어지는 것(컴파일, 테스트, 범위, 테스트 보존)은 스크립트와 훅이 정한다.
  LLM 리뷰는 판단이 필요한 항목에만 쓴다. 기계 검사는 토큰이 들지 않는다.
- **반드시 지켜야 하는 것은 글이 아니라 하네스에 둔다.** 조각 검사 없는 push, CI 중의 Unity 실행은 훅이 막는다.
- **절차와 판정 로직은 플러그인에, 저장소마다 다른 값은 그 저장소의 `.agent-loop/`에 둔다.** 플러그인 코드는 저장소 값을 모른다.

## 어디에 무엇이 있나

| 플러그인 | 저장소 (`.agent-loop/`) |
|---|---|
| 상태와 다음 행동 규칙 (`orchestrator.py`, `unit.py`) | `config.json`: 기준 브랜치, 리뷰 대상 경로, 테스트 경로, diff 통계 처리 경로, 규칙 문서 목록, CI 아티팩트 이름, 증거 명령, 실행기 |
| 조각 검사 (`slice_check.py`, `unity_tests.py`) | `models.json`: 역할별 모델·노력 |
| 조각 PR 잇기 (`pr_chain.sh`) | `rubric.json`: 최소 리뷰 항목(기계 판정 `auto`), 상세 리뷰 항목·배점, 게이트, 통과 점수, 개선 한도 |
| 리뷰 실행기·채점 공식·판정 (`review.py`, `quality_gate.py`) | `CLAUDE.md`의 프로젝트 규칙 |
| 변경 범위 판정 (`scope.py`), 자동 머지 가드 원본·템플릿 | `.github/` 아래 복사본(`loop-init`이 넣음) |
| 훅: `AGENT_LOOP_HOME` 내보내기, push·Unity 실행 가드, PR 뒤 실행기 확인 | |

## 계약

- **설정 읽기**: 스크립트는 현재 작업 디렉터리의 git 루트에서 `.agent-loop/config.json`을 읽는다. 없으면 `loop-init`을 쓰라고 하고 멈춘다.
- **조각 검사 기록**: `<git 공용 폴더>/agent-loop/slice-check/<트리 해시>.json`. 트리 해시는 커밋하지 않은 변경과 추적 안 된 파일까지 담은 작업 트리의 것이다.
  PR 커밋의 트리와 같아야 머지된다. Unity가 실행 중 고친 추적 파일은 되돌리고, 새로 생긴 파일은 실패다.
- **최소 리뷰의 기계 판정**: `rubric.json` 항목의 `auto`가 `review.py`의 `AUTO`에서 판정을 고른다. 기계는 통과만 확정한다. 확정하지 못하면 리뷰어가 본다.
- **증거 명령**: `{"title": "...", "cmd": [...]}`. 저장소 루트에서 돌고, 인자의 `{base}`·`{sha}`·`{tree}`(대상 커밋을 그대로 푼 폴더)를 바꿔 받는다.
  출력이 JSON 객체면 그 `hard_fails`가 최소 리뷰를 실패시킨다. 0이 아닌 종료는 리뷰를 멈춘다.
- **CI 결과**: `config.ci.results_artifact`의 NUnit3 XML을 요약한다.
- **리뷰 대상 경로**: 값은 `config.review_paths` 한 곳, 판정 코드는 `templates/github/scripts/review_scope.py` 한 곳.
  플러그인 `scope.py`가 그 템플릿을 가져다 쓰고, 저장소 CI는 `loop-init`이 복사한 같은 파일을 돌린다.
- **플러그인 관리 파일과 저장소 소유 파일**: `init.py`는 가드 워크플로·스크립트·`review_scope.py`를 플러그인 것으로 맞추고(`--update`),
  `.agent-loop/`와 자동 머지 정책은 처음 한 번만 씨앗을 넣는다.
- **브랜치 이름**: `loop/<이름>`, `task/<루프>-<조각 id>`는 플러그인 규칙으로 고정한다. 기준 브랜치만 설정이다.

## 열린 위험

- 조각 검사 기록은 에이전트가 직접 만들 수 있다. 막으려는 것은 잊어버리는 것이지 속이는 것이 아니다. 전체 테스트는 루프 끝 CI가 본다.
- Unity 실행 파일은 Unity Hub 기본 설치 경로(Windows)에서만 찾는다.
- 로컬 경로 마켓플레이스는 이 PC에서만 동작한다.
- `AGENT_LOOP_HOME`은 `claude -p --plugin-dir` 세션의 Bash에서 보이는 것만 확인했다(2026-11-20). push 가드 훅도 같은 방식으로 확인했다(2026-11-21). Desktop 앱 세션에서는 확인하지 않았다.
