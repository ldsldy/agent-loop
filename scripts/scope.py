"""변경 경로 판정. 리뷰 대상인지(config.review_paths)와 테스트만 바뀌었는지(config.test_globs).

    git diff --name-only --no-renames <base>...<head> | python -I scope.py review       # true / false
    gh pr view <N> --json files -q '.files[].path' | python -I scope.py tests-only     # true / false
"""
import fnmatch
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loopconfig import CONFIG  # noqa: E402

REVIEW_PATHS = tuple(CONFIG["review_paths"])


def touches_review(paths):
    """리뷰 대상 경로를 하나라도 건드리는가. 건드리지 않는 변경(도구·문서·에이전트 설정)은 테스트·리뷰를 건너뛴다."""
    return any(path.strip().startswith(REVIEW_PATHS) for path in paths)


def is_test(path):
    # fnmatch의 *는 /도 맞춘다. git pathspec 글롭과 같은 뜻이라 리뷰 diff의 통계 처리와 같은 목록을 쓴다.
    return any(fnmatch.fnmatchcase(path, glob) for glob in CONFIG["test_globs"])


def tests_only(paths):
    """테스트 파일만 바뀌었는가. 빈 목록은 테스트만으로 보지 않는다(파일 목록을 못 읽은 경우)."""
    paths = [path.strip() for path in paths if path.strip()]
    return bool(paths) and all(is_test(path) for path in paths)


CHECKS = {"review": touches_review, "tests-only": tests_only}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CHECKS:
        sys.exit(f"사용법: python -I scope.py {'|'.join(CHECKS)} < 경로 목록")
    print("true" if CHECKS[sys.argv[1]](sys.stdin.readlines()) else "false")
