"""변경이 리뷰 대상 경로(.agent-loop/config.json의 review_paths)를 건드리는지 판정한다.

agent-loop 플러그인이 관리하는 파일이다(loop-init이 복사). 플러그인의 scope.py와 저장소 CI가 이 판정을 같이 쓴다.
CI는 플러그인이 없으므로 이 복사본과 .agent-loop/config.json만 받아 돌린다.
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
