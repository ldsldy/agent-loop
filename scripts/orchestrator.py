"""오케스트레이터: 큰 단위(마일스톤)를 고르고 정리한다. 절차는 loop 스킬. 저장소 루트에서 돌린다.

    python -I orchestrator.py status     # 마일스톤별 이슈와 막힘
    python -I orchestrator.py pick       # 다음에 가져갈 큰 단위
    python -I orchestrator.py cleanup    # 머지된 task/* 브랜치 정리, 그 브랜치의 worktree는 detach(--dry-run으로 보기만)
    python -I orchestrator.py start --milestone 5 --branch loop/<이름>   # 승인받아 만든 루프 브랜치로 큰 단위 시작
    python -I orchestrator.py plan --unit <이름> --slices slices.json [--improvement]
    python -I orchestrator.py next --unit <이름>                     # 지금 할 일(JSON)
    python -I orchestrator.py record --unit <이름> --detailed passed|failed --verdict <verdict.json>

판단(복잡도·품질)은 하지 않는다. 무엇을 먼저 할지·다음 행동·정리만 한다. 상태 규칙은 unit.py. 설계·구현은 조정 세션이 Desktop 세션으로 연다.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unit as units  # noqa: E402
from loopconfig import ROOT  # noqa: E402
from quality_gate import MAX_IMPROVEMENTS  # noqa: E402

READY_LABEL = "ready-for-agent"
HUMAN_LABEL = "human"
# 이 브랜치들만 정리 대상이다. loop/*·기준 브랜치·main·다른 세션의 claude/*는 건드리지 않는다.
TASK_PREFIX = units.TASK_PREFIX
BLOCKED_BY = re.compile(r"Blocked by:?([^\n]*)", re.IGNORECASE)  # 그 줄의 #번호 전부가 막는 이슈


def run(args, check=True):
    done = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if check and done.returncode != 0:
        sys.exit(f"{' '.join(args)} 실패: {done.stderr.strip()[:300]}")
    return done.stdout


def gh_json(args):
    return json.loads(run(["gh", *args]) or "null")


# ---- 판정(GitHub를 부르지 않는다. 테스트 대상) ----

def blockers(body):
    """본문의 'Blocked by: #N' 줄에서 막는 이슈 번호들."""
    found = []
    for match in BLOCKED_BY.finditer(body or ""):
        found += [int(n) for n in re.findall(r"#(\d+)", match.group(1))]
    return found


def issue_state(issue, open_numbers, milestone_numbers):
    """이슈 하나가 지금 가져갈 수 있는지: ready · human · not-ready(라벨) · blocked(#N)."""
    labels = {label["name"] for label in issue["labels"]}
    if HUMAN_LABEL in labels:
        return "human", []
    outside = [n for n in blockers(issue.get("body")) if n in open_numbers and n not in milestone_numbers]
    if outside:
        return "blocked", outside
    if READY_LABEL not in labels:
        return "not-ready", []
    return "ready", []


def milestone_report(milestone, issues, open_numbers):
    """마일스톤 하나의 이슈별 상태와 가져갈 수 있는지. 열린 이슈가 전부 ready여야 시작할 수 있다."""
    numbers = {issue["number"] for issue in issues}
    rows = []
    for issue in issues:
        state, by = issue_state(issue, open_numbers, numbers)
        rows.append({"number": issue["number"], "title": issue["title"], "state": state, "blocked_by": by})
    return {"number": milestone["number"], "title": milestone["title"], "issues": rows,
            "startable": bool(rows) and all(row["state"] == "ready" for row in rows)}


def pick(reports):
    """시작할 수 있는 마일스톤 중 번호가 가장 작은 것(먼저 만든 큰 단위가 먼저). 없으면 None."""
    startable = [report for report in reports if report["startable"]]
    return min(startable, key=lambda report: report["number"]) if startable else None


def removable(branches, merged):
    """정리할 task 브랜치: task/로 시작하고 PR이 머지된 것만."""
    return sorted(branch for branch in branches if branch.startswith(TASK_PREFIX) and branch in merged)


# ---- GitHub·git 읽기와 실행 ----

def load_reports():
    milestones = gh_json(["api", "repos/{owner}/{repo}/milestones?state=open&per_page=100"]) or []
    issues = gh_json(["issue", "list", "--state", "open", "--limit", "500",
                      "--json", "number,title,labels,body,milestone"]) or []
    open_numbers = {issue["number"] for issue in issues}
    reports = []
    for milestone in milestones:
        mine = [issue for issue in issues if (issue.get("milestone") or {}).get("number") == milestone["number"]]
        reports.append(milestone_report(milestone, mine, open_numbers))
    return reports


def print_status(reports):
    for report in reports:
        mark = "시작 가능" if report["startable"] else "막힘"
        print(f"## {report['title']} (#{report['number']}) — {mark}")
        for row in report["issues"]:
            extra = f" ← #{', #'.join(map(str, row['blocked_by']))}" if row["blocked_by"] else ""
            print(f"  - #{row['number']} [{row['state']}]{extra} {row['title']}")


def worktrees():
    """브랜치 → worktree 경로."""
    result, path = {}, None
    for line in run(["git", "worktree", "list", "--porcelain"]).splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            result[line[len("branch refs/heads/"):]] = path
    return result


def cleanup(dry_run):
    run(["git", "fetch", "-q", "--prune", "origin"])
    local = run(["git", "branch", "--format=%(refname:short)"]).split()
    remote = [b[len("origin/"):] for b in run(["git", "branch", "-r", "--format=%(refname:short)"]).split()
              if b.startswith("origin/")]
    candidates = sorted({b for b in local + remote if b.startswith(TASK_PREFIX)})
    merged = {b for b in candidates
              if (gh_json(["pr", "list", "--head", b, "--state", "merged", "--json", "number"]) or [])}
    targets = removable(candidates, merged)
    trees = worktrees()
    for branch in targets:
        print(f"{'(보기만) ' if dry_run else ''}정리: {branch}" + (f" · worktree {trees[branch]}" if branch in trees else ""))
        if dry_run:
            continue
        if branch in trees:
            # worktree는 구현 세션이 다음 조각에도 쓰므로 지우지 않고 브랜치에서 떼어 낸다(세션을 보관할 때 앱이 지운다).
            run(["git", "-C", trees[branch], "switch", "-q", "--detach"])
        if branch in local:
            run(["git", "branch", "-D", branch])
        if branch in remote:
            run(["git", "push", "-q", "origin", "--delete", branch])
    kept = [b for b in candidates if b not in merged]
    if kept:
        print(f"남김(머지 안 됨): {', '.join(kept)}")
    return targets


def unit_path(name):
    return os.path.join(ROOT, "ci-results", "loop", f"{name}.json")


def load_unit(name):
    path = unit_path(name)
    if not os.path.exists(path):
        sys.exit(f"큰 단위 상태가 없습니다: {path} (start 먼저)")
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def save_unit(state):
    path = unit_path(units.loop_name(state))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    return path


def task_branches():
    local = run(["git", "branch", "--format=%(refname:short)"]).split()
    remote = [b[len("origin/"):] for b in run(["git", "branch", "-r", "--format=%(refname:short)"]).split()
              if b.startswith("origin/")]
    return {b for b in local + remote if b.startswith(TASK_PREFIX)}


def pr_states(state):
    """조각 브랜치별 가장 최근 PR의 상태."""
    result = {}
    for piece in state["slices"]:
        branch = units.task_branch(state, piece)
        listed = gh_json(["pr", "list", "--head", branch, "--state", "all", "--json", "state", "--limit", "1"]) or []
        if listed:
            result[branch] = listed[0]["state"]
    return result


def main(argv):
    parser = argparse.ArgumentParser(description="오케스트레이터")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("pick")
    clean = sub.add_parser("cleanup")
    clean.add_argument("--dry-run", action="store_true")
    start = sub.add_parser("start")
    start.add_argument("--milestone", type=int, required=True)
    start.add_argument("--branch", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--unit", required=True)
    plan.add_argument("--slices", required=True, help="설계 세션이 쓴 조각 목록 JSON(배열)")
    plan.add_argument("--improvement", action="store_true", help="상세 리뷰 미달 뒤의 개선 조각")
    nxt = sub.add_parser("next")
    nxt.add_argument("--unit", required=True)
    record = sub.add_parser("record")
    record.add_argument("--unit", required=True)
    record.add_argument("--detailed", choices=units.DETAILED, required=True)
    record.add_argument("--verdict", required=True)
    args = parser.parse_args(argv)
    if args.command == "cleanup":
        cleanup(args.dry_run)
        return 0
    if args.command == "start":
        if not run(["git", "ls-remote", "--heads", "origin", args.branch]).strip():
            sys.exit(f"원격에 {args.branch}가 없습니다. 사용자 승인을 받아 먼저 만드세요(loop 스킬 \"흐름\" 2).")
        milestone = gh_json(["api", f"repos/{{owner}}/{{repo}}/milestones/{args.milestone}"])
        state = units.new_unit({"number": milestone["number"], "title": milestone["title"]}, args.branch)
        if os.path.exists(unit_path(units.loop_name(state))):
            sys.exit(f"이미 시작한 큰 단위입니다: {unit_path(units.loop_name(state))}")
        print(f"시작: {save_unit(state)}")
        return 0
    if args.command == "plan":
        state = load_unit(args.unit)
        with open(args.slices, encoding="utf-8") as file:
            try:
                units.add_slices(state, json.load(file), args.improvement)
            except ValueError as error:
                sys.exit(f"조각 목록 문제: {error}")
        print(f"조각 {len(state['slices'])}개 저장: {save_unit(state)}")
        return 0
    if args.command == "record":
        state = units.record_detailed(load_unit(args.unit), args.detailed, args.verdict)
        print(f"상세 리뷰 {args.detailed} 기록: {save_unit(state)}")
        return 0
    if args.command == "next":
        state = load_unit(args.unit)
        actions = units.next_actions(state, pr_states(state), task_branches(), MAX_IMPROVEMENTS)
        print(json.dumps(actions, ensure_ascii=False, indent=2))
        return 0
    reports = load_reports()
    if args.command == "status":
        print_status(reports)
        return 0
    chosen = pick(reports)
    print(json.dumps(chosen, ensure_ascii=False, indent=2) if chosen else "시작할 수 있는 큰 단위가 없습니다(status로 막힘 확인).")
    return 0 if chosen else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
