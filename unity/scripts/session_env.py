"""세션 시작 훅: 이 플러그인 경로를 AGENT_LOOP_HOME으로 세션의 Bash 환경에 내보낸다.

저장소 쪽 도구(예: 프로젝트 전용 리뷰 스크립트)가 플러그인 모듈을 가져올 때 쓴다. CLAUDE_ENV_FILE이 없으면 아무것도 하지 않는다.
"""
import os
import shlex
import sys

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def export_line(home):
    # Git Bash가 읽는다. 경로의 \ 와 공백이 그대로 남도록 작은따옴표로 감싼다.
    return f"export AGENT_LOOP_HOME={shlex.quote(home.replace(os.sep, '/'))}\n"


if __name__ == "__main__":
    target = os.environ.get("CLAUDE_ENV_FILE")
    if target:
        with open(target, "a", encoding="utf-8") as file:
            file.write(export_line(HOME))
    sys.exit(0)
