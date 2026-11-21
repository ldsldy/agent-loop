"""조각 검사: 구현 worktree의 지금 트리로 컴파일과 바뀐 테스트를 Unity batchmode로 돌리고, 통과를 트리 해시로 기록한다.

    python -I slice_check.py             # task/<루프>-<조각> 브랜치의 worktree에서. 통과 0, 실패 1
    python -I slice_check.py has <커밋>   # 그 커밋의 트리가 통과한 기록이 있으면 요약을 내고 0, 없으면 1 (pr_chain.sh가 머지 전에 확인)
    python -I slice_check.py guard       # PreToolUse 훅(hooks/hooks.json): 통과 기록이 없는 트리의 git push를 종료 코드 2로 막는다

기록은 <git 공용 폴더>/agent-loop/slice-check/<트리 해시>.json 이다(worktree들이 함께 본다).
트리 해시는 커밋하지 않은 변경과 추적 안 된 파일까지 담은 작업 트리의 것이다. 검사한 그대로 커밋해야 PR 커밋의 트리와 맞는다.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ensure_runner  # noqa: E402
import unity_tests  # noqa: E402

TASK_PREFIX = "task/"
# git 뒤에 -C <경로> 같은 옵션 쌍만 두고 바로 push가 오는 명령. 커밋 메시지 안의 push는 잡지 않는다.
PUSH = re.compile(r"\bgit\s+(?:-\S+\s+\S+\s+)*push\b")
BATCHMODE = re.compile(r"\s-batchmode\b", re.I)
CI_BUSY = "이 PC의 CI 실행기가 작업 중이다. 같은 PC에서 Unity를 함께 띄우면 CI와 충돌한다. CI가 끝난 뒤에 다시 한다."


def git(*args, cwd, env=None):
    done = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        sys.exit(f"git {' '.join(args)} 실패: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def work_tree(root):
    """작업 트리(추적 안 된 파일 포함, 무시 파일 제외)의 트리 해시. 임시 인덱스에 담아 실제 인덱스는 건드리지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        index = os.path.join(tmp, "index")
        real = git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=root)
        if os.path.exists(real):
            shutil.copy(real, index)
        env = {**os.environ, "GIT_INDEX_FILE": index}
        git("add", "-A", cwd=root, env=env)
        return git("write-tree", cwd=root, env=env)


def record_path(root, tree):
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root)
    return os.path.join(common, "agent-loop", "slice-check", f"{tree}.json")


def loop_base(root, branch):
    """task/<루프 이름>-<조각 id>가 갈라져 나온 origin/loop/<루프 이름>. 이름에 -가 있을 수 있어 가장 길게 맞는 루프를 고른다."""
    if not branch.startswith(TASK_PREFIX):
        return None
    rest = branch[len(TASK_PREFIX):]
    loops = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/loop/", cwd=root).split()
    return max((ref for ref in loops if rest.startswith(ref.split("loop/", 1)[1] + "-")), key=len, default=None)


def changed_tests(root, base, tree, is_test):
    """루프 브랜치에서 갈라진 뒤 이 트리까지 만들거나 고친 테스트 파일. 지운 파일은 빼고 이름을 바꾼 파일은 새 경로다."""
    rows = [line.split("\t") for line in git("diff", "--name-status", "-M", git("merge-base", "HEAD", base, cwd=root), tree, cwd=root).splitlines()]
    return [row[-1] for row in rows if not row[0].startswith("D") and is_test(row[-1])]


def check(root, is_test, runner=subprocess.run):
    branch = git("branch", "--show-current", cwd=root)
    base = loop_base(root, branch)
    if not base:
        sys.exit(f"조각 브랜치(task/<루프>-<조각>)가 아니거나 origin/loop/<루프>가 없습니다: {branch or 'detached'}")
    tree = work_tree(root)
    path = record_path(root, tree)
    if os.path.exists(path):
        print("이 트리는 이미 조각 검사를 통과했다. 그대로 커밋·푸시한다.")
        return 0
    out = os.path.join(git("rev-parse", "--path-format=absolute", "--git-dir", cwd=root), "agent-loop", "slice-check-run")
    os.makedirs(out, exist_ok=True)
    ok, lines = unity_tests.run(root, out, unity_tests.plan(root, changed_tests(root, base, tree, is_test)), runner)
    # Unity는 열기만 해도 추적 파일을 고쳐 쓴다(ProjectSettings의 define 등). 검사한 트리로 되돌려야 커밋이 기록과 맞는다.
    # 새로 생긴 파일은 되돌리지 않는다. 커밋에서 빠진 .meta처럼 조각이 놓친 것이라 실패로 알린다.
    rows = [line.split("\t") for line in git("diff", "--name-status", "--no-renames", tree, work_tree(root), cwd=root).splitlines()]
    made = [path for status, path in rows if status == "A"]
    touched = [path for status, path in rows if status != "A"]
    if touched:
        git("restore", f"--source={tree}", "--worktree", "--", *touched, cwd=root)
        lines.append(f"Unity가 실행 중 고친 파일 {len(touched)}개를 검사 전 내용으로 되돌렸다: {', '.join(touched[:5])}")
    if made:
        ok = False
        lines.append("검사 중 새 파일이 생겼다(커밋에서 빠진 .meta 등). 커밋에 넣거나 지우고 다시 돌린다:")
        lines += made[:unity_tests.MAX_LINES]
    print("\n".join(lines))
    if not ok:
        print(f"조각 검사 실패. 로그: {out}")
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump({"tree": tree, "branch": branch, "summary": lines}, file, ensure_ascii=False, indent=2)
    print("조각 검사 통과. 이 상태 그대로 커밋·푸시한다(파일을 더 바꾸면 다시 돌린다).")
    return 0


def has(root, commit):
    path = record_path(root, git("rev-parse", f"{commit}^{{tree}}", cwd=root))
    if not os.path.exists(path):
        return 1
    with open(path, encoding="utf-8") as file:
        print(", ".join(json.load(file)["summary"]))
    return 0


def runner_configured(root):
    with open(os.path.join(root, ".agent-loop", "config.json"), encoding="utf-8") as file:
        return bool((json.load(file).get("runner") or {}).get("task"))


def guard(data, ci_busy=ensure_runner.local_busy):
    """PreToolUse 훅 입력 → 종료 코드. 루프 저장소에서 막을 명령이면 2(막고 이유를 에이전트에게 준다).
    - 이 PC의 CI 실행기가 작업 중일 때 Unity batchmode를 띄우는 명령
    - 조각 브랜치에서 조각 검사 통과 기록이 없는 트리를 push하는 명령"""
    command = data.get("tool_input", {}).get("command", "")
    push, unity = PUSH.search(command), BATCHMODE.search(command)
    if not push and not unity:
        return 0
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=data.get("cwd") or os.getcwd(), capture_output=True, text=True, encoding="utf-8")
    root = os.path.normpath(top.stdout.strip()) if top.returncode == 0 else None
    if not root or not os.path.exists(os.path.join(root, ".agent-loop", "config.json")):
        return 0
    if unity and runner_configured(root) and ci_busy():
        print(CI_BUSY, file=sys.stderr)
        return 2
    if not push or not git("branch", "--show-current", cwd=root).startswith(TASK_PREFIX):
        return 0
    if os.path.exists(record_path(root, work_tree(root))):
        return 0
    print("조각 검사를 통과한 기록이 없는 트리다. push 전에 이 worktree에서 다음을 돌려 통과시킨다(컴파일과 이 조각이 만들거나 고친 테스트):\n"
          f'  python -I "{os.path.abspath(__file__)}"\n'
          "통과한 상태 그대로 전부 커밋한 뒤 push한다.", file=sys.stderr)
    return 2


def main(argv):
    if argv[:1] == ["guard"]:
        try:
            return guard(json.load(sys.stdin))
        except ValueError:
            return 0
    from loopconfig import ROOT
    from scope import is_test
    if argv[:1] == ["has"] and len(argv) == 2:
        return has(ROOT, argv[1])
    if argv:
        sys.exit("사용법: python -I slice_check.py [has <커밋> | guard]")
    if runner_configured(ROOT) and ensure_runner.local_busy():
        sys.exit(CI_BUSY)
    return check(ROOT, is_test)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
