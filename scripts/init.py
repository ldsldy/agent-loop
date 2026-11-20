"""저장소에 agent-loop 파일을 넣는다. 여러 번 돌려도 같은 결과다. 저장소 루트에서:

    python -I init.py            # 없는 파일만 넣고, 플러그인 관리 파일이 옛것이면 알린다
    python -I init.py --update   # 플러그인 관리 파일을 플러그인 것으로 덮어쓴다

- 플러그인 관리 파일(가드 워크플로·스크립트, 리뷰 범위 판정): 플러그인이 정본이다. 저장소 기준 브랜치에서 돌아야 해서 복사본을 둔다.
- 저장소 소유 파일(.agent-loop/ 설정, 자동 머지 정책): 처음 한 번 씨앗을 넣고 그 뒤로는 덮어쓰지 않는다.
"""
import os
import shutil
import subprocess
import sys

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
PLUGIN, REPO = "plugin", "repo"
# (템플릿 경로, 저장소 경로, 소유)
FILES = [
    ("agent-loop/config.json", ".agent-loop/config.json", REPO),
    ("agent-loop/models.json", ".agent-loop/models.json", REPO),
    ("agent-loop/rubric.json", ".agent-loop/rubric.json", REPO),
    ("github/auto-merge-policy.json", ".github/auto-merge-policy.json", REPO),
    ("github/workflows/auto-merge-guard.yml", ".github/workflows/auto-merge-guard.yml", PLUGIN),
    ("github/scripts/auto_merge_guard.py", ".github/scripts/auto_merge_guard.py", PLUGIN),
    ("github/scripts/review_scope.py", ".github/scripts/review_scope.py", PLUGIN),
]


def same_text(a, b):
    """줄 끝만 다른 파일은 같다. Windows 체크아웃(core.autocrlf)은 저장소 복사본을 CRLF로 바꿔 둔다."""
    with open(a, "rb") as first, open(b, "rb") as second:
        return first.read().replace(b"\r\n", b"\n") == second.read().replace(b"\r\n", b"\n")


def plan(root, update):
    """파일마다 (저장소 경로, 할 일): copy · same · keep(저장소 소유라 그대로) · stale(플러그인 것과 다름) · overwrite."""
    actions = []
    for src, dst, owner in FILES:
        source, target = os.path.join(TEMPLATES, src), os.path.join(root, dst)
        if not os.path.exists(target):
            actions.append((dst, "copy"))
        elif owner == REPO:
            actions.append((dst, "keep"))
        elif same_text(source, target):
            actions.append((dst, "same"))
        else:
            actions.append((dst, "overwrite" if update else "stale"))
    return actions


def apply(root, actions):
    sources = {dst: src for src, dst, _ in FILES}
    for dst, action in actions:
        if action in ("copy", "overwrite"):
            target = os.path.join(root, dst)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(TEMPLATES, sources[dst]), target)


LABELS = {"copy": "넣음", "same": "최신", "keep": "저장소 것 유지", "stale": "플러그인 것과 다름(--update로 덮어쓰기)", "overwrite": "덮어씀"}


def main(argv):
    done = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        sys.exit("git 저장소 안에서 실행하세요.")
    root = os.path.normpath(done.stdout.strip())
    actions = plan(root, "--update" in argv)
    apply(root, actions)
    for dst, action in actions:
        print(f"{dst}: {LABELS[action]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
