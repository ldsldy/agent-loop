"""큰 단위(마일스톤 하나 = 루프 브랜치 하나)의 상태와 다음 행동 규칙. GitHub를 부르지 않는다(테스트 대상).

상태는 <git 공용 폴더>/agent-loop/loop/<루프 이름>.json에 둔다(worktree들이 함께 본다). 조각의 진행(PR 열림·머지)은 저장하지 않고 GitHub의 PR에서 읽는다.
조각 브랜치 이름은 task/<루프 이름>-<조각 id>로 정해져 있어 PR을 이름으로 찾는다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loopconfig import CONFIG, MODELS, effort  # noqa: E402

COMPLEXITY = ("simple", "medium", "complex")
# 조각의 증명 계약(slice_check.py가 확인한다):
# kind    change = 행동을 바꾼다(구현 전에 실패하고 구현 뒤에 통과하는 테스트가 있어야 한다), keep = 행동을 지킨다
# guards  이 조각 뒤에도 고치지 않은 채 통과해야 하는 기존 테스트 파일
# retire  이 조각이 고쳐 쓰거나 지워도 되는 기존 테스트 파일 → 이유(옛 구조에 묶임, spec이 바꾼 동작). 여기 없는 기존 테스트는 못 건드린다
KINDS = ("change", "keep")
SLICE_FIELDS = ("id", "title", "issues", "acceptance", "files", "complexity", "depends_on", "kind", "guards", "retire")
DETAILED = ("pending", "failed", "passed")
LOOP_PREFIX = "loop/"
TASK_PREFIX = "task/"


def loop_name(unit):
    return unit["loop_branch"].split("/", 1)[1]


def task_branch(unit, piece):
    return f"{TASK_PREFIX}{loop_name(unit)}-{piece['id']}"


def new_unit(milestone, loop_branch):
    if not loop_branch.startswith(LOOP_PREFIX) or loop_branch == LOOP_PREFIX:
        raise ValueError(f"루프 브랜치는 loop/<이름>이어야 합니다: {loop_branch}")
    return {"milestone": milestone, "loop_branch": loop_branch, "slices": [], "improvements": 0,
            "detailed": {"state": "pending", "verdict": None}}


def slice_errors(slices, existing=()):
    """조각 목록의 문제. 빈 목록이면 받아들인다. existing은 이미 있는 조각(추가할 때 의존 대상)."""
    errors, known = [], {piece["id"] for piece in existing}
    ids = [piece.get("id") for piece in slices]
    for piece in slices:
        missing = [field for field in SLICE_FIELDS if field not in piece]
        if missing:
            errors.append(f"{piece.get('id')}: 필드 없음 {missing}")
        if piece.get("complexity") not in COMPLEXITY:
            errors.append(f"{piece.get('id')}: complexity는 {COMPLEXITY} 중 하나")
        if piece.get("kind") not in KINDS:
            errors.append(f"{piece.get('id')}: kind는 {KINDS} 중 하나")
        guards, retire = piece.get("guards", []), piece.get("retire", {})
        if not isinstance(guards, list) or not isinstance(retire, dict):
            errors.append(f"{piece.get('id')}: guards는 목록, retire는 {{테스트 파일: 이유}}")
        elif not all(str(why).strip() for why in retire.values()):
            errors.append(f"{piece.get('id')}: retire의 테스트마다 물러나는 이유를 적는다")
        elif set(guards) & set(retire):
            errors.append(f"{piece.get('id')}: 같은 테스트가 guards와 retire에 함께 있다 {sorted(set(guards) & set(retire))}")
    duplicates = {i for i in ids if ids.count(i) > 1} | (set(ids) & known)
    if duplicates:
        errors.append(f"중복 id {sorted(duplicates)}")
    graph = {piece["id"]: piece.get("depends_on", []) for piece in slices if "id" in piece}
    for piece_id, deps in graph.items():
        unknown = [d for d in deps if d not in graph and d not in known]
        if unknown:
            errors.append(f"{piece_id}: 없는 조각에 의존 {unknown}")
    if not errors and _has_cycle(graph):
        errors.append("의존이 순환한다")
    return errors


def _has_cycle(graph):
    state = {}

    def visit(node):
        if state.get(node) == "visiting":
            return True
        if state.get(node) == "done" or node not in graph:
            return False
        state[node] = "visiting"
        found = any(visit(dep) for dep in graph[node])
        state[node] = "done"
        return found

    return any(visit(node) for node in graph)


def implement_model(piece):
    section = MODELS["implement"]
    model = section["by_complexity"].get(piece["complexity"], section["by_complexity"]["default"])
    return {"model": model, "effort": effort(section)}


def next_actions(unit, prs, live_branches, max_improvements):
    """지금 할 일 목록. prs = {브랜치: "OPEN"|"MERGED"|"CLOSED"}, live_branches = 아직 남아 있는 task 브랜치."""
    if not unit["slices"]:
        design = MODELS["design"]
        return [{"action": "design", "why": "조각 목록이 없다", "models": design["by_complexity"], "effort": effort(design)}]
    merged = {piece["id"] for piece in unit["slices"] if prs.get(task_branch(unit, piece)) == "MERGED"}
    actions = []
    for piece in unit["slices"]:
        branch = task_branch(unit, piece)
        state = prs.get(branch)
        if state == "MERGED":
            continue
        if state == "OPEN":
            actions.append({"action": "review", "slice": piece["id"], "branch": branch,
                            "why": "PR이 열려 있다: 최소 리뷰(조각 PR은 CI 없음). 통과하면 묻지 않고 머지·cleanup, 실패면 구현 세션이 고친다"})
        elif all(dep in merged for dep in piece["depends_on"]):
            actions.append({"action": "implement", "slice": piece["id"], "branch": branch, **implement_model(piece),
                            "why": ("의존 조각이 모두 머지됐다" if piece["depends_on"] else "의존 없음")
                            + ". 시작 때 연 구현 세션에 SendMessage로 맡긴다(loop 스킬 \"세션 운영\"). "
                            "push 전에 그 worktree에서 조각 검사(slice_check.py: 컴파일과 바뀐 테스트)를 통과시키고 그 상태 그대로 커밋해 PR을 올린다"})
    gone = [task_branch(unit, p) for p in unit["slices"] if p["id"] in merged and task_branch(unit, p) in live_branches]
    if gone:
        actions.append({"action": "cleanup", "branches": gone, "why": "머지된 조각 브랜치가 남아 있다"})
    if len(merged) < len(unit["slices"]):
        return actions
    detailed = unit["detailed"]["state"]
    if detailed == "pending":
        previous = unit["detailed"].get("previous")
        actions.append({"action": "detailed-review", "base": f"origin/{CONFIG['base_branch']}",
                        "why": f"조각이 모두 머지됐다: 루프 → {CONFIG['base_branch']} PR을 열고 그 PR의 CI(유일한 CI) run으로 상세 리뷰한다"
                        + (". 개선 뒤 재리뷰라 review.py에 --previous를 준다(이전 채점과 그 뒤의 diff만 본다)" if previous else ""),
                        "loop_branch": unit["loop_branch"], **({"previous": previous} if previous else {})})
    elif detailed == "failed" and unit["improvements"] < max_improvements:
        actions.append({"action": "improve", "why": f"상세 리뷰 미달(개선 {unit['improvements']}/{max_improvements}회)",
                        "verdict": unit["detailed"]["verdict"]})
    elif detailed == "failed":
        actions.append({"action": "stop", "why": f"개선 {max_improvements}회 뒤에도 상세 리뷰 미달. 사용자에게 보고한다"})
    else:
        retired = {path: why for piece in unit["slices"] for path, why in piece.get("retire", {}).items()}
        actions.append({"action": "notify-user", "why": f"상세 리뷰 통과. 사용자가 {CONFIG['base_branch']}에 머지한다. 물러난 테스트(retired)를 이유와 함께 보고한다",
                        "retired": retired})
    return actions


def add_slices(unit, slices, improvement):
    """조각을 더한다. 상세 리뷰 뒤 개선 조각이면 improvement=True(개선 횟수 +1, 상세 리뷰는 다시 대기)."""
    errors = slice_errors(slices, unit["slices"])
    if errors:
        raise ValueError("; ".join(errors))
    unit["slices"] += slices
    if improvement:
        unit["improvements"] += 1
        # 미달이던 리뷰를 남겨 둔다. 다음 상세 리뷰는 그 채점과 그 뒤의 diff만 본다(전체를 다시 싣지 않는다).
        unit["detailed"] = {"state": "pending", "verdict": None, "previous": unit["detailed"]["verdict"]}
    return unit


def record_detailed(unit, state, verdict):
    if state not in DETAILED:
        raise ValueError(f"상세 리뷰 상태는 {DETAILED} 중 하나")
    unit["detailed"] = {"state": state, "verdict": verdict}
    return unit
