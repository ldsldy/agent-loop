"""품질 리뷰의 채점 공식과 판정. 절차는 loop-review 스킬. 항목·배점·게이트·한도는 저장소의 .agent-loop/rubric.json이다.

리뷰는 두 단계다.
- 최소 리뷰(PR마다): 기계가 통과로 확정하지 못한 MINIMAL_CHECKS만 MINIMAL_REVIEWERS가 통과/실패로 본다. 증거 명령의 hard_fails 없음은 기계가 본다.
- 상세 리뷰(큰 단위 = 루프 브랜치가 끝났을 때): REQUIRED_REVIEWERS가 평가표 그룹(A 구조 × B 구현·검증 등)을 채점한다.

    python -I quality_gate.py --rubric                  # 상세 리뷰 항목·배점·게이트
    python -I quality_gate.py claude.json codex.json    # 상세 리뷰 판정(리뷰 실행기가 부른다)

리뷰 JSON(schema agent-loop-quality-v1)은 리뷰어 한 명의 채점이다:
ratings.<id> = {quant: 0~4, qual: 0~4, evidence}, gates.<id> = {status: pass|fail|na|unchecked, evidence},
penalty·penaltyReason(첫 그룹 과잉 설계 감점), critical·criticalEvidence, issues, notes. 변경 시나리오 증거는 gates에 적는다.
REQUIRED_REVIEWERS 모두가 대상 커밋(git_sha)을 채점하고 각자 통과해야 승인 가능이다. 평균 내지 않는다.
채점 공식과 판정 규칙은 이 파일 한 곳에, 리뷰어와 모델은 .agent-loop/models.json에 둔다.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loopconfig import MODELS, RUBRIC as _RUBRIC  # noqa: E402

SCHEMA = "agent-loop-quality-v1"
PASS_SCORE = _RUBRIC["pass_score"]
MAX_IMPROVEMENTS = _RUBRIC["max_improvements"]  # 미달 뒤 개선·재채점 반복 한도
MAX_PENALTY = _RUBRIC["max_penalty"]  # 첫 그룹(구조) 과잉 설계 감점 상한
REQUIRED_REVIEWERS = tuple(MODELS["review"]["detailed"]["reviewers"])
MINIMAL_REVIEWERS = tuple(MODELS["review"]["minimal"]["reviewers"])
# (id, 이름, 설명, 기계 판정 이름 또는 None). 기계 판정(review.py의 AUTO)은 통과만 확정한다. 확정 못 하면 리뷰어가 본다.
MINIMAL_CHECKS = [(c["id"], c["name"], c["hint"], c.get("auto")) for c in _RUBRIC["minimal_checks"]]
# 그룹 → [(id, 이름, 배점, 설명)]. 첫 그룹이 감점(penalty)을 받는다.
RUBRIC = {group: [(i["id"], i["name"], i["weight"], i["hint"]) for i in body["items"]] for group, body in _RUBRIC["groups"].items()}
GROUP_TITLES = {group: body["title"] for group, body in _RUBRIC["groups"].items()}
PENALTY_GROUP = next(iter(RUBRIC))
# (id, 이름, 설명, 비대상 허용)
GATES = [(g["id"], g["name"], g["hint"], g["allow_na"]) for g in _RUBRIC["gates"]]

_bad_weights = {group: total for group, items in RUBRIC.items() if (total := sum(item[2] for item in items)) != 100}
if _bad_weights:
    sys.exit(f".agent-loop/rubric.json: 그룹별 배점 합은 100이어야 합니다: {_bad_weights}")


def item_score(weight, rating):
    """배점 × (정량/4 × 0.6 + 정성/4 × 0.4). 등급이 하나라도 없으면 None(미평가: 0점도 통과도 아님)."""
    grades = [rating.get("quant"), rating.get("qual")]
    if not all(isinstance(g, int) and not isinstance(g, bool) and 0 <= g <= 4 for g in grades):
        return None
    return weight * (grades[0] * 0.6 + grades[1] * 0.4) / 4


def group_score(review, group):
    ratings = review.get("ratings", {})
    scores = {item: item_score(weight, ratings.get(item, {})) for item, _, weight, _ in RUBRIC[group]}
    missing = [item for item, s in scores.items() if s is None]
    unevidenced = [item for item, *_ in RUBRIC[group] if not str(ratings.get(item, {}).get("evidence", "")).strip()]
    total = sum(s for s in scores.values() if s is not None)
    if group == PENALTY_GROUP:
        total = max(0.0, total - max(0, min(MAX_PENALTY, review.get("penalty", 0) or 0)))
    return {"score": None if missing else round(total, 1), "missing": missing, "unevidenced": unevidenced}


def gate_results(review):
    results = []
    for gate, _, _, allow_na in GATES:
        entry = review.get("gates", {}).get(gate, {})
        status, evidence = entry.get("status", "unchecked"), str(entry.get("evidence", "")).strip()
        ok = (status == "pass" or (allow_na and status == "na")) and bool(evidence)
        results.append({"id": gate, "status": status, "ok": ok})
    return results


def judge(review):
    """상세 리뷰 한 명의 판정: approved(승인 가능) · blocked(차단) · revise(수정 후 재평가) · incomplete(평가·검증 미완료)."""
    if review.get("schema") != SCHEMA:
        raise ValueError(f"schema가 {SCHEMA}가 아님: {review.get('schema')!r}")
    result = {group: group_score(review, group) for group in RUBRIC}
    result["gates"] = gate_results(review)

    def verdict(name, reasons):
        return {"verdict": name, **result, "reasons": reasons}

    critical = review.get("critical")
    critical_known = isinstance(critical, int) and not isinstance(critical, bool) and critical >= 0
    if critical_known and critical > 0:
        return verdict("blocked", [f"치명적 결함 {critical}건"])
    failed = [g["id"] for g in result["gates"] if g["status"] == "fail"]
    if failed:
        return verdict("blocked", [f"게이트 실패 {failed}"])

    reasons = []
    if (review.get("penalty", 0) or 0) and not str(review.get("penaltyReason", "")).strip():
        reasons.append("과잉 설계 감점에 사유가 없음")
    groups = [(key.upper(), result[key]) for key in RUBRIC]
    for name, group in groups:
        if group["missing"]:
            reasons.append(f"{name} 미평가 항목 {group['missing']}")
        if group["unevidenced"]:
            reasons.append(f"{name} 근거 없음 {group['unevidenced']}")
    if not critical_known or not str(review.get("criticalEvidence", "")).strip():
        reasons.append("치명적 결함 검토(critical·criticalEvidence) 없음")
    unchecked = [g["id"] for g in result["gates"] if not g["ok"]]
    if unchecked:
        reasons.append(f"미검증 게이트 {unchecked}")
    if reasons:
        return verdict("incomplete", reasons)
    low = [f"{name} {group['score']} < {PASS_SCORE}" for name, group in groups if group["score"] < PASS_SCORE]
    return verdict("revise" if low else "approved", low)


def judge_all(reviews, sha, ci="success"):
    """리뷰어별로 따로 판정하고 모두가 같은 커밋에서 통과해야 승인 가능. 가장 나쁜 판정을 따른다.
    ci는 실행기가 확인한 그 커밋의 CI 결론이다. 성공이 아니면 점수와 관계없이 차단이다."""
    per_reviewer, reasons = {}, []
    for review in reviews:
        name = review.get("reviewer")
        if name in per_reviewer:
            reasons.append(f"리뷰어 {name}의 결과가 둘 이상")
        per_reviewer[name] = judge(review)
        if review.get("git_sha") != sha:
            reasons.append(f"{name} 리뷰의 git_sha {review.get('git_sha')}가 대상 커밋 {sha}와 다름(무효)")
    missing = [name for name in REQUIRED_REVIEWERS if name not in per_reviewer]
    if missing:
        reasons.append(f"리뷰어 없음 {missing}")
    verdicts = {name: result["verdict"] for name, result in per_reviewer.items()}
    if ci != "success":
        reasons.append(f"CI 결론 {ci!r}")
    if ci != "success" or "blocked" in verdicts.values():
        overall = "blocked"
    elif reasons or "incomplete" in verdicts.values():
        overall = "incomplete"
    elif "revise" in verdicts.values():
        overall = "revise"
    else:
        overall = "approved"
    # 의견이 갈리면 설계 에이전트가 근거를 보고 갈린 항목만 재리뷰한다. 근거 없이 미달을 덮어쓰지 않는다.
    return {"verdict": overall, "git_sha": sha, "level": "detailed", "disagreement": len(set(verdicts.values())) > 1,
            "reasons": reasons, "reviewers": per_reviewer}


def _grade():
    return {"type": ["integer", "null"], "minimum": 0, "maximum": 4}


def _strict(properties):
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


def _issue():
    return _strict({"severity": {"type": "string", "enum": ["critical", "major", "minor"]}, "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]}, "principle": {"type": "string"},
                    "evidence": {"type": "string"}, "impact": {"type": "string"}, "fix": {"type": "string"}})


def open_checks(machine):
    """리뷰어가 봐야 하는 최소 리뷰 항목 id. 기계가 통과로 확정한 것(passed)과 상세 리뷰로 미룬 것(deferred)은 뺀다."""
    settled = set(machine.get("passed", {})) | set(machine.get("deferred", []))
    return [item for item, *_ in MINIMAL_CHECKS if item not in settled]


def minimal_schema(items):
    check = _strict({"status": {"type": "string", "enum": ["pass", "fail", "unknown"]}, "evidence": {"type": "string"}})
    return _strict({"checks": _strict({item: check for item in items}),
                    "issues": {"type": "array", "items": _issue()}, "notes": {"type": "string"}})


def judge_minimal(reviews, sha, machine):
    """최소 리뷰 판정: pass · fail · incomplete. machine은 실행기가 잰 사실이다:
    {"hard_fails": [...], "passed": {항목 id: 근거}, "deferred": [상세 리뷰로 미룬 항목 id]}. 남은 항목만 리뷰어 결과를 요구한다.
    조각 PR은 CI를 돌리지 않으므로 CI 결론은 보지 않는다(2026-11-19 사용자 결정). 테스트 근거는 조각 검사와 develop PR의 CI가 본다."""
    reasons, failed = [], []
    if machine.get("hard_fails"):
        failed.append(f"증거 명령 hard_fails {machine['hard_fails']}")
    remaining = open_checks(machine)
    titles = {item: title for item, title, *_ in MINIMAL_CHECKS}
    names = [review.get("reviewer") for review in reviews]
    for name in MINIMAL_REVIEWERS if remaining else ():
        if name not in names:
            reasons.append(f"리뷰어 없음 {name}")
    for review in reviews:
        name = review.get("reviewer")
        if review.get("schema") != SCHEMA or review.get("git_sha") != sha:
            reasons.append(f"{name} 리뷰의 schema·git_sha가 대상 커밋 {sha}와 맞지 않음(무효)")
            continue
        for item in remaining:
            status = review.get("checks", {}).get(item, {}).get("status", "unknown")
            if status == "fail":
                failed.append(f"{name} {item} {titles[item]}")
            elif status != "pass":
                reasons.append(f"{name} {item} {titles[item]} 확인 못 함")
    verdict = "fail" if failed else "incomplete" if reasons else "pass"
    return {"verdict": verdict, "git_sha": sha, "level": "minimal", "failed": failed, "reasons": reasons, "machine": machine}


def minimal_text(items):
    lines = ["## 최소 리뷰 항목 (각각 pass·fail·unknown과 근거)"]
    lines += [f"- {item} {name}: {hint}" for item, name, hint, _ in MINIMAL_CHECKS if item in items]
    lines.append("하나라도 fail이면 실패, 확인하지 못한 것(unknown)이 있으면 미완료다. 점수는 매기지 않는다.")
    return "\n".join(lines)


def review_schema():
    """리뷰어(claude -p --json-schema, codex exec --output-schema)가 내야 할 출력. reviewer·git_sha는 실행기가 붙인다."""
    rating = _strict({"quant": _grade(), "qual": _grade(), "evidence": {"type": "string"}})
    properties = {
        "ratings": _strict({item: rating for items in RUBRIC.values() for item, *_ in items}),
        "penalty": {"type": "integer", "minimum": 0, "maximum": MAX_PENALTY}, "penaltyReason": {"type": "string"},
        "critical": {"type": "integer", "minimum": 0}, "criticalEvidence": {"type": "string"},
        "issues": {"type": "array", "items": _issue()}, "notes": {"type": "string"},
    }
    gate = _strict({"status": {"type": "string", "enum": ["pass", "fail", "na", "unchecked"]}, "evidence": {"type": "string"}})
    properties["gates"] = _strict({g: gate for g, *_ in GATES})
    return _strict(properties)


def rubric_text():
    lines = []
    for group, items in RUBRIC.items():
        lines.append(f"## {GROUP_TITLES[group]} (100점, {PASS_SCORE}점 이상 통과)")
        lines += [f"- {item} {name} [{weight}] {hint}" for item, name, weight, hint in items]
    lines.append("## 필수 게이트 (status: pass|fail|unchecked, 비대상 na는 표시된 것만)")
    lines += [f"- {gate} {name}{' [na 허용]' if allow_na else ''}: {hint}" for gate, name, hint, allow_na in GATES]
    lines.append("항목 점수 = 배점 × (정량/4 × 0.6 + 정성/4 × 0.4). 등급 0~4. 미평가는 0점도 통과도 아니다.")
    lines.append(f"리뷰어 {', '.join(REQUIRED_REVIEWERS)} 모두가 각자 통과해야 한다. 미달 뒤 개선은 최대 {MAX_IMPROVEMENTS}회.")
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(description="상세 리뷰 판정")
    parser.add_argument("reviews", nargs="*", help="리뷰어별 채점 결과 JSON")
    parser.add_argument("--sha", help="대상 커밋. 없으면 현재 HEAD")
    parser.add_argument("--rubric", action="store_true", help="채점 항목·배점·게이트 출력")
    args = parser.parse_args(argv)
    if args.rubric:
        print(rubric_text())
        return 0
    if not args.reviews:
        parser.error("리뷰 JSON이 필요합니다")
    sha = args.sha or subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    reviews = []
    for path in args.reviews:
        with open(path, encoding="utf-8") as file:
            reviews.append(json.load(file))
    try:
        result = judge_all(reviews, sha)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verdict"] == "approved" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
