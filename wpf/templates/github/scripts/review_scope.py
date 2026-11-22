"""변경이 리뷰 대상 경로(.agent-loop/config.json의 review_paths)를 건드리는지 판정한다.

판정 코드의 정본은 플러그인 templates/github/scripts/의 이 파일이다. 플러그인 scope.py는 정본을 가져다 쓰고,
저장소 .github/scripts/의 것은 loop-init이 넣은 복사본이다(저장소 CI가 init.py --check로 정본과 같은지 검사한다).
저장소 CI의 변경 판정 잡은 플러그인 없이 기준 브랜치의 복사본과 .agent-loop/config.json만 받아 돌린다(PR이 판정을 바꾸지 못하게).
    git diff --name-only --no-renames <base>...<head> | python3 .github/scripts/review_scope.py   # true / false
"""
import json
import os
import sys


def load_review_paths(root):
    with open(os.path.join(root, ".agent-loop", "config.json"), encoding="utf-8") as file:
        return tuple(json.load(file)["review_paths"])


def touches_review(paths, review_paths):
    """리뷰 대상 경로를 하나라도 건드리는가. 건드리지 않는 변경(도구·문서·에이전트 설정)은 테스트·리뷰를 건너뛴다."""
    return any(path.strip().startswith(tuple(review_paths)) for path in paths)


if __name__ == "__main__":
    print("true" if touches_review(sys.stdin, load_review_paths(os.getcwd())) else "false")
