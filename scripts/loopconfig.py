"""저장소의 .agent-loop/ 설정을 읽는다. 플러그인 스크립트는 저장소마다 다른 값을 모두 여기서 받는다.

- config.json: 기준 브랜치, 리뷰 대상 경로, 테스트 경로, diff 처리, 규칙 문서, CI 결과, 증거 명령, 조각 테스트 지시, 실행기
- models.json: 역할별 모델·노력 정도
- rubric.json: 최소 리뷰 항목, 상세 리뷰 평가표·게이트, 통과 점수, 개선 한도

저장소 루트는 AGENT_LOOP_ROOT(테스트용), 없으면 현재 디렉터리의 git 루트다.
"""
import json
import os
import subprocess
import sys


def _root():
    if os.environ.get("AGENT_LOOP_ROOT"):
        return os.path.abspath(os.environ["AGENT_LOOP_ROOT"])
    done = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        sys.exit("git 저장소 안에서 실행하세요.")
    return os.path.normpath(done.stdout.strip())


ROOT = _root()
DIR = os.path.join(ROOT, ".agent-loop")


def _load(name):
    path = os.path.join(DIR, name)
    if not os.path.exists(path):
        sys.exit(f"{path}가 없습니다. 이 저장소에서 loop-init 스킬로 설정을 먼저 만드세요.")
    with open(path, encoding="utf-8") as file:
        return json.load(file)


CONFIG = _load("config.json")
MODELS = _load("models.json")
RUBRIC = _load("rubric.json")


def effort(section):
    """역할 설정(dict)의 노력 정도. 적지 않았으면 default_effort."""
    return section.get("effort", MODELS["default_effort"])
