"""조각 검사: 구현 worktree의 지금 트리가 조각의 증명 계약을 지키는지 Unity batchmode로 확인하고, 통과를 트리 해시로 기록한다.

    python -I slice_check.py [--again]   # task/<루프>-<조각> 브랜치의 worktree에서. 통과 0, 실패 1
    python -I slice_check.py has <커밋>   # 그 커밋의 트리가 통과한 기록이 있으면 요약을 내고 0, 없으면 1 (pr_chain.sh가 머지 전에 확인)
    python -I slice_check.py guard       # PreToolUse 훅(hooks/hooks.json): 통과 기록이 없는 트리의 git push를 종료 코드 2로 막는다

증명 계약은 설계가 조각에 적는다(unit.py의 kind·guards·retire). 검사가 확인하는 것:
- 컴파일되고, 이 조각이 만들거나 고친 테스트와 지킴이 테스트(guards)가 통과한다. 실행 중 새 파일이 생기지 않는다.
- 기존 테스트는 물러나는 테스트(retire)에 적힌 것만 고치거나 지운다. 지킴이는 건드리지 않는다.
- 바꾸는 조각(kind=change)은 구현 전 코드에 테스트만 얹으면 실패하거나 컴파일되지 않는다(빨강에서 초록).
- 새 파일·타입이 모양 규칙(shape.py: 파일·타입·상속·폴더)을 지킨다. 이미 있던 에셋의 GUID를 바꾸지 않는다.
- 같은 조각에서 BUDGET번 실패하면 더 돌리지 않는다. 판정을 받은 뒤 --again으로 다시 연다.

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
import shape  # noqa: E402
import unity_tests  # noqa: E402

TASK_PREFIX = "task/"
# git 뒤에 -C <경로> 같은 옵션 쌍만 두고 바로 push가 오는 명령. 커밋 메시지 안의 push는 잡지 않는다.
PUSH = re.compile(r"\bgit\s+(?:-\S+\s+\S+\s+)*push\b")
BATCHMODE = re.compile(r"\s-batchmode\b", re.I)
CI_BUSY = "이 PC의 CI 실행기가 작업 중이다. 같은 PC에서 Unity를 함께 띄우면 CI와 충돌한다. CI가 끝난 뒤에 다시 한다."
BUDGET = 3  # 근거 없는 첫 값이다. 멈춘 기록이 쌓이면 고친다.
STOP = (f"이 조각의 검사가 {BUDGET}번 실패했다. 더 고치지 말고 멈춘다. 지금까지의 실패를 조정 세션에 보고하고 판정을 받는다. "
        "판정을 받은 뒤에는 --again을 붙여 다시 돌린다.")
ASK = ("테스트를 통과시키려고 코드를 굽히지 않고, 테스트를 조용히 고치지도 않는다. 멈추고 조정 세션에 판정을 요청한다: "
       "이 테스트가 막는 것이 실제 회귀인가, 옛 구조에 묶인 것인가(저장소 CLAUDE.md의 테스트 규칙).")


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
            # 수정 시각까지 복사한다. git은 인덱스 파일의 시각으로 "같은 초에 같은 크기로 고친 파일"을 다시 읽을지 정한다.
            # 시각이 지금으로 바뀌면 그런 파일을 안 바뀐 것으로 보고 옛 트리 해시를 낸다.
            shutil.copy2(real, index)
        env = {**os.environ, "GIT_INDEX_FILE": index}
        git("add", "-A", cwd=root, env=env)
        return git("write-tree", cwd=root, env=env)


def shared_dir(root):
    """worktree들이 함께 보는 루프 폴더(<git 공용 폴더>/agent-loop). 상태 파일과 조각 검사 기록이 여기 있다."""
    return os.path.join(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root), "agent-loop")


def state_path(root, loop):
    return os.path.join(shared_dir(root), "loop", f"{loop}.json")


def record_path(root, tree):
    return os.path.join(shared_dir(root), "slice-check", f"{tree}.json")


def attempts_path(root, branch):
    return os.path.join(shared_dir(root), "slice-check", "attempts", branch.replace("/", "__") + ".json")


def loop_base(root, branch):
    """task/<루프 이름>-<조각 id>가 갈라져 나온 origin/loop/<루프 이름>. 이름에 -가 있을 수 있어 가장 길게 맞는 루프를 고른다."""
    if not branch.startswith(TASK_PREFIX):
        return None
    rest = branch[len(TASK_PREFIX):]
    loops = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/loop/", cwd=root).split()
    return max((ref for ref in loops if rest.startswith(ref.split("loop/", 1)[1] + "-")), key=len, default=None)


def slice_of(root, branch, base):
    """상태 파일에서 이 브랜치의 조각. 상태 파일이나 그 조각이 없으면 빈 계약이다(컴파일과 바뀐 테스트만 본다)."""
    loop = base.split("loop/", 1)[1]
    path = state_path(root, loop)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as file:
        slices = json.load(file)["slices"]
    return next((piece for piece in slices if f"{TASK_PREFIX}{loop}-{piece['id']}" == branch), {})


def changed(root, base, tree):
    """루프 브랜치에서 갈라진 지점과, 거기서 이 트리까지 바뀐 (상태 글자, 경로). 이름 변경은 지움과 더함으로 본다."""
    fork = git("merge-base", "HEAD", base, cwd=root)
    rows = [line.split("\t") for line in git("diff", "--name-status", "--no-renames", fork, tree, cwd=root).splitlines()]
    return fork, [(status[0], path) for status, path in rows]


def restore(root, source, paths):
    for start in range(0, len(paths), 100):  # 명령줄 길이 한도
        git("restore", f"--source={source}", "--worktree", "--", *paths[start:start + 100], cwd=root)


def reset_to(root, tree, remove_new=False):
    """작업 트리의 파일을 tree의 내용으로 되돌린다 → (되돌린 파일, tree에 없는 새 파일). remove_new면 새 파일도 지운다."""
    rows = [line.split("\t") for line in git("diff", "--name-status", "--no-renames", tree, work_tree(root), cwd=root).splitlines()]
    made = [path for status, path in rows if status == "A"]
    touched = [path for status, path in rows if status != "A"]
    restore(root, tree, touched)
    for path in made if remove_new else ():
        os.remove(os.path.join(root, path))
    return touched, made


def contract_errors(root, piece, rows, is_test):
    """Unity를 띄우기 전에 알 수 있는 계약 위반."""
    guards, retire = piece.get("guards", []), piece.get("retire", {})
    errors = []
    hit = [path for _, path in rows if path in guards]
    if hit:
        errors.append(f"지킴이 테스트를 고쳤다: {', '.join(hit)}. {ASK}")
    unplanned = [path for status, path in rows if status != "A" and is_test(path) and path not in retire and path not in guards]
    if "retire" in piece and unplanned:
        errors.append(f"물러나는 테스트(retire)에 없는 기존 테스트를 고치거나 지웠다: {', '.join(unplanned)}. {ASK}")
    missing = [path for path in guards if not os.path.exists(os.path.join(root, path))]
    if missing:
        errors.append(f"지킴이 테스트 파일이 없다: {', '.join(missing)}")
    if piece.get("kind") == "change" and not any(status != "D" and is_test(path) and path.endswith(".cs") for status, path in rows):
        errors.append("바꾸는 조각(kind=change)에는 그 행동을 증명하는 테스트가 필요하다. 구현 전에는 실패하고 구현 뒤에 통과하는 테스트를 쓴다.")
    return errors


def red(root, fork, tree, rows, tests, is_test, runner, out, marker):
    """구현 전 코드(루프 브랜치에서 갈라진 지점)에 이 조각의 테스트만 얹어 돌린다 → (증명됐는가, 보고 줄들).
    거기서 실패하거나 컴파일되지 않아야 테스트가 구현을 증명한다. 끝나면 작업 트리를 tree로 되돌린다."""
    code = [(status, path) for status, path in rows if not is_test(path)]
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as file:  # 중간에 끊기면 다음 검사가 이 트리로 되돌린다
        file.write(tree)
    try:
        for path in (path for status, path in code if status == "A"):
            os.remove(os.path.join(root, path))
        restore(root, fork, [path for status, path in code if status != "A"])
        status, lines = unity_tests.run(root, out, unity_tests.plan(root, tests), runner)
    finally:
        reset_to(root, tree, remove_new=True)
        if work_tree(root) != tree:
            sys.exit(f"작업 트리를 검사 전 상태로 되돌리지 못했다. 손으로 되돌린다: git restore --source={tree} --worktree -- .")
        os.remove(marker)
    if status == unity_tests.COMPILE:
        return True, ["빨강에서 초록: 구현 전 코드에서는 컴파일되지 않고 구현 뒤에 통과한다"]
    if status == unity_tests.FAIL:
        return True, ["빨강에서 초록: 구현 전 코드에서는 실패하고 구현 뒤에 통과한다"]
    if status == unity_tests.PASS:
        return False, ["이 조각의 테스트가 구현 전 코드에서도 통과한다. 구현을 증명하지 않는 테스트다. 구현이 없으면 실패하는 단언으로 고친다."]
    return False, ["구현 전 코드에서의 실행을 판정하지 못했다:"] + lines


def prove(root, piece, fork, tree, rows, is_test, runner, marker):
    """Unity로 계약을 확인한다 → (통과했는가, 보고 줄들)."""
    out = os.path.join(os.path.dirname(marker), "slice-check-run")
    os.makedirs(out, exist_ok=True)
    tests = [path for status, path in rows if status != "D" and is_test(path)]
    status, lines = unity_tests.run(root, out, unity_tests.plan(root, tests + piece.get("guards", [])), runner)
    # Unity는 열기만 해도 추적 파일을 고쳐 쓴다(ProjectSettings의 define 등). 검사한 트리로 되돌려야 커밋이 기록과 맞는다.
    # 새로 생긴 파일은 되돌리지 않는다. 커밋에서 빠진 .meta처럼 조각이 놓친 것이라 실패로 알린다.
    touched, made = reset_to(root, tree)
    if touched:
        lines.append(f"Unity가 실행 중 고친 파일 {len(touched)}개를 검사 전 내용으로 되돌렸다: {', '.join(touched[:5])}")
    if made:
        return False, lines + ["검사 중 새 파일이 생겼다(커밋에서 빠진 .meta 등). 커밋에 넣거나 지우고 다시 돌린다:"] + made[:unity_tests.MAX_LINES]
    if status != unity_tests.PASS:
        return False, lines + [f"로그: {out}"]
    if piece.get("kind") == "change":
        proven, report = red(root, fork, tree, rows, tests, is_test, runner, out, marker)
        lines += report
        if not proven:
            return False, lines
    retired = [f"{path}({piece['retire'][path]})" for _, path in rows if path in piece.get("retire", {})]
    if retired:
        lines.append(f"물러난 테스트: {', '.join(retired)}")
    return True, lines


def changed_guids(root, fork, rows):
    """이미 있던 에셋의 .meta에서 GUID가 바뀐 것. GUID가 바뀌면 그 에셋을 가리키던 참조가 끊긴다."""
    def guid(text):
        found = re.search(r"^guid:\s*(\w+)", text, re.M)
        return found.group(1) if found else None
    changed = []
    for path in (path for status, path in rows if status == "M" and path.endswith(".meta")):
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as file:
            if guid(git("show", f"{fork}:{path}", cwd=root)) != guid(file.read()):
                changed.append(path)
    return changed


def check(root, is_test, runner=subprocess.run, again=False, shape_config=None):
    branch = git("branch", "--show-current", cwd=root)
    base = loop_base(root, branch)
    if not base:
        sys.exit(f"조각 브랜치(task/<루프>-<조각>)가 아니거나 origin/loop/<루프>가 없습니다: {branch or 'detached'}")
    marker = os.path.join(git("rev-parse", "--path-format=absolute", "--git-dir", cwd=root), "agent-loop", "restore-tree")
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as file:
            reset_to(root, file.read().strip(), remove_new=True)
        os.remove(marker)
        print("지난 조각 검사가 구현 전 상태에서 끊겨 작업 트리를 검사 전 내용으로 되돌렸다.")
    tree = work_tree(root)
    path = record_path(root, tree)
    if os.path.exists(path):
        print("이 트리는 이미 조각 검사를 통과했다. 그대로 커밋·푸시한다.")
        return 0
    tries_path = attempts_path(root, branch)
    tries = []
    if os.path.exists(tries_path):
        with open(tries_path, encoding="utf-8") as file:
            tries = json.load(file)
    if len(tries) >= BUDGET and not again:
        print(STOP)
        return 1
    piece = slice_of(root, branch, base)
    fork, rows = changed(root, base, tree)
    lines = contract_errors(root, piece, rows, is_test)
    moved = changed_guids(root, fork, rows)
    if moved:
        lines.append(f"이미 있던 에셋의 GUID가 바뀌었다(참조가 끊긴다). .meta를 되돌린다: {', '.join(moved)}")
    excused = []
    if shape_config:  # 새 파일·타입의 모양 규칙(shape.py). Unity를 띄우기 전에 본다
        broken, excused = shape.slice_report(root, fork, rows, piece, shape_config, is_test)
        if broken:
            lines += [f"모양 규칙 위반 {len(broken)}건. 고친다. 정당한 예외면 멈추고 조정 세션에 조각의 allow에 이유를 더해 달라고 한다:"] + broken
    ok = not lines
    if ok:
        ok, lines = prove(root, piece, fork, tree, rows, is_test, runner, marker)
        if ok and excused:
            lines.append(f"허용된 모양 예외: {', '.join(excused)}")
    print("\n".join(lines))
    if not ok:
        tries.append(lines[:5])
        os.makedirs(os.path.dirname(tries_path), exist_ok=True)
        with open(tries_path, "w", encoding="utf-8") as file:
            json.dump(tries, file, ensure_ascii=False, indent=1)
        print(f"조각 검사 실패 {len(tries)}/{BUDGET}." + (f" {STOP}" if len(tries) >= BUDGET else ""))
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump({"tree": tree, "branch": branch, "summary": lines}, file, ensure_ascii=False, indent=2)
    if os.path.exists(tries_path):
        os.remove(tries_path)
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
    print("조각 검사를 통과한 기록이 없는 트리다. push 전에 이 worktree에서 다음을 돌려 통과시킨다(조각의 증명 계약):\n"
          f'  python -I "{os.path.abspath(__file__)}"\n'
          "통과한 상태 그대로 전부 커밋한 뒤 push한다.", file=sys.stderr)
    return 2


def main(argv):
    if argv[:1] == ["guard"]:
        try:
            return guard(json.load(sys.stdin))
        except ValueError:
            return 0
    from loopconfig import CONFIG, ROOT
    from scope import is_test
    if argv[:1] == ["has"] and len(argv) == 2:
        return has(ROOT, argv[1])
    if argv not in ([], ["--again"]):
        sys.exit("사용법: python -I slice_check.py [--again | has <커밋> | guard]")
    if runner_configured(ROOT) and ensure_runner.local_busy():
        sys.exit(CI_BUSY)
    return check(ROOT, is_test, again=bool(argv), shape_config=CONFIG.get("shape"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
