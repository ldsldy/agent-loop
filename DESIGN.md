# agent-loop 설계

ShalomEng 저장소의 루프(마일스톤 → 루프 브랜치 → 조각 PR → 최소 리뷰 → 머지 → 상세 리뷰)를 다른 저장소에서도 쓰게 뺀 Claude Code 플러그인.

## 나누는 기준

루프의 **절차와 판정 로직**은 플러그인 한 곳에 둔다. 저장소마다 다른 **값**은 그 저장소의 `.agent-loop/` 폴더에 둔다.
플러그인 코드는 저장소 값을 모른다. 의존은 저장소 설정 → 플러그인이 읽는 한 방향이다.

| 플러그인 (`agent-loop`) | 저장소 (`.agent-loop/`) |
|---|---|
| 오케스트레이터 상태·다음 행동 규칙 (`orchestrator.py`, `unit.py`) | `config.json`: 기준 브랜치, 리뷰 대상 경로, 테스트 경로, diff 통계 처리 경로, 규칙 문서 목록, CI 아티팩트 이름, 조각 테스트 지시, 실행기 |
| 조각 PR 잇기 (`pr_chain.sh`) | `models.json`: 역할별 모델·노력 (지금 `tools/agent_models.json`) |
| 리뷰 실행기·채점 공식·판정 (`review.py`, `quality_gate.py`) | `rubric.json`: 최소 리뷰 항목, 상세 리뷰 A·B 항목·배점, 게이트, 통과 점수, 개선 한도 |
| 변경 범위 판정 (`scope.py`, 지금 `unity_change.py`) | 증거 명령(`config.json`의 `evidence`): 리뷰 자료에 붙일 프로젝트 전용 출력 |
| 자동 머지 가드 원본, 템플릿 | `.github/` 아래 복사본 (초기화 스킬이 넣음) |
| 스킬: `loop`(절차), `loop-review`(리뷰 절차), `loop-init`(초기화) | `CLAUDE.md`의 프로젝트 규칙 |
| 훅: 세션 시작 때 `AGENT_LOOP_HOME` 내보내기, PR 뒤 실행기 확인(설정에 실행기가 있을 때만) | |

## 계약

- **설정 읽기**: 플러그인 스크립트는 현재 작업 디렉터리의 git 루트에서 `.agent-loop/config.json`을 읽는다. 없으면 `loop-init`을 쓰라고 하고 멈춘다.
  지금처럼 `__file__`로 저장소 루트를 찾지 않는다(스크립트가 저장소 밖에 있으므로).
- **증거 명령**: `{"title": "...", "cmd": [...]}`. 저장소 루트에서 돌고, 인자의 `{base}`·`{sha}`·`{tree}`(대상 커밋을 그대로 푼 폴더)를 바꿔 받는다.
  stdout이 리뷰 자료의 그 제목 아래 들어간다. 출력이 JSON 객체면 그 `hard_fails`가 최소 리뷰를 실패시킨다. 0이 아닌 종료는 리뷰를 멈춘다. ShalomEng의 `design_qa.py`(C# 의존 그래프·중복)와 패키지 보안 절(`Packages/packages-lock.json`)이 여기로 간다.
- **CI 결과**: `config.ci.results_artifact`의 NUnit3 XML을 요약한다(Unity·.NET 공통 형식). 다른 형식은 필요해질 때 더한다.
- **리뷰 대상 경로**: 값은 `config.review_paths` 한 곳, 판정 코드는 `templates/github/scripts/review_scope.py` 한 곳.
  플러그인 `scope.py`가 그 템플릿을 가져다 쓰고, 저장소 CI는 `loop-init`이 복사한 같은 파일을 돌린다.
- **플러그인 관리 파일과 저장소 소유 파일**: `init.py`는 가드 워크플로·스크립트·`review_scope.py`를 플러그인 것으로 맞추고(`--update`),
  `.agent-loop/`와 자동 머지 정책은 처음 한 번만 씨앗을 넣는다.
- **채점 규칙**: `references/scoring.md`. `review.py`가 리뷰 자료 맨 앞에 싣고, 저장소 `context_files`가 뒤따른다.
- **브랜치 이름**: `loop/<이름>`, `task/<루프>-<조각 id>`는 플러그인 규칙으로 고정한다. 기준 브랜치(`develop`)만 설정이다.

## ShalomEng에 남는 것

- `.agent-loop/config.json`·`models.json`·`rubric.json` (지금 값 그대로)
- 증거 명령: `tools/review/design_qa.py`, `tools/review/packages.py` (지금 `review.py`의 `package_part`)
- `project-review` 스킬과 `project_review.py`: C# 선언 파싱과 영역(AREAS)이 이 프로젝트 전용이라 남긴다. 플러그인 모듈은 `AGENT_LOOP_HOME`으로 가져온다.
- `editmode-tests.yml`: changes 잡이 `.agent-loop/config.json`을 읽게 바꾸고, 옮긴 도구 테스트 단계는 뺀다.
- `CLAUDE.md`·`docs/agents/`: 루프 절차는 플러그인 스킬을 가리키고, 프로젝트 규칙만 남긴다.

## 단계

1. 플러그인 뼈대와 핵심 이동. 지금 테스트를 ShalomEng 값의 설정 고정물로 옮겨 그대로 통과시킨다.
2. 스킬·훅·초기화(`loop-init`: 설정 뼈대, 가드 워크플로·정책, 라벨 목록).
3. ShalomEng 전환(루프 브랜치에서 PR). 이 단계 끝에 실제 조각 하나를 플러그인으로 돌려 확인한다.
4. 플러그인 GitHub 저장소(사용자 확인 뒤). 그전까지 ShalomEng의 `extraKnownMarketplaces`는 이 PC의 로컬 경로다.

## 열린 위험

- `CLAUDE_ENV_FILE`로 내보낸 `AGENT_LOOP_HOME`이 Desktop 앱의 Bash와 백그라운드 명령에 실제로 보이는지 1단계에서 확인한다.
  안 보이면 스크립트가 `~/.claude/plugins/installed_plugins.json`에서 플러그인 경로를 찾는다.
- 로컬 경로 마켓플레이스는 이 PC에서만 동작한다. 4단계 전에는 다른 PC에서 ShalomEng 루프를 돌릴 수 없다.
