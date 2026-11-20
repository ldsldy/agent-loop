"""극저위험 자동 머지 가드. 정책은 기준 브랜치의 .github/auto-merge-policy.json만 쓴다(PR 작성자가 못 바꾼다).
허용 목록에 없는 변경, 금지 경로, 기존 테스트 수정·삭제, 분석 불가는 모두 차단이다."""
import json, os, re, subprocess, sys

MARKER = "<!-- auto-merge-guard -->"


def glob_re(g):
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i): out += "(?:.*/)?"; i += 3
        elif g.startswith("**", i): out += ".*"; i += 2
        elif g[i] == "*": out += "[^/]*"; i += 1
        else: out += re.escape(g[i]); i += 1
    return re.compile("^" + out + "$")


def matches(path, globs):
    return any(glob_re(g).match(path) for g in globs)


def evaluate(files, policy):
    """files: GitHub PR 파일 목록(filename, status, previous_filename, patch). 반환: 차단 사유 목록(비면 통과)."""
    reasons, allowed = [], {}
    for f in files:
        name, status = f["filename"], f["status"]
        old = f.get("previous_filename")
        if old: reasons.append(f"`{old}` → `{name}`: 이름 변경·이동은 자동 머지하지 않는다")
        if status not in ("added", "modified", "removed", "renamed"):
            reasons.append(f"`{name}`: 알 수 없는 변경 종류 `{status}` (분석 불가)")
        elif matches(name, policy["deny"]):
            reasons.append(f"`{name}`: 금지 경로")
        elif name.endswith(".meta"):
            continue  # 아래에서 짝을 확인한다
        elif matches(name, [policy["test_glob"]]) and status in ("modified", "removed", "renamed"):
            reasons.append(f"`{name}`: 기존 테스트 {status}")
        else:
            rule = next((r for r in policy["allow"] if matches(name, [r["glob"]])), None)
            if rule is None: reasons.append(f"`{name}`: 허용 목록에 없음")
            elif status not in rule["status"]: reasons.append(f"`{name}`: `{rule['glob']}`에서 `{status}`는 허용되지 않음")
            else:
                allowed[name] = status
                if matches(name, [policy["test_glob"]]):
                    patch = f.get("patch")
                    if patch is None: reasons.append(f"`{name}`: 패치를 볼 수 없어 분석 불가")
                    elif any(m in patch for m in policy["test_disable_markers"]):
                        reasons.append(f"`{name}`: 테스트 비활성화 표식 포함")
    for f in files:
        name = f["filename"]
        if name.endswith(".meta") and not matches(name, policy["deny"]):
            if f["status"] != "added" or allowed.get(name[:-5]) != "added":
                reasons.append(f"`{name}`: 새로 추가한 허용 파일의 짝이 아닌 .meta")
    if not files: reasons.append("변경 파일이 없거나 읽지 못함 (분석 불가)")
    return reasons


def gh(*args, inp=None):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=inp).stdout


def required_ok(repo, base, needed):
    rules = json.loads(gh("api", f"repos/{repo}/rules/branches/{base}"))
    have = {c["context"] for r in rules if r["type"] == "required_status_checks"
            for c in r["parameters"]["required_status_checks"]}
    return [c for c in needed if c not in have]


def report(repo, pr, text):
    cs = json.loads(gh("api", f"repos/{repo}/issues/{pr}/comments", "--paginate"))
    mine = next((c for c in cs if MARKER in c["body"]), None)
    body = json.dumps({"body": f"{MARKER}\n{text}"})
    if mine: gh("api", "-X", "PATCH", f"repos/{repo}/issues/comments/{mine['id']}", "--input", "-", inp=body)
    else: gh("api", f"repos/{repo}/issues/{pr}/comments", "--input", "-", inp=body)


def main():
    repo, pr, base = os.environ["REPO"], os.environ["PR_NUMBER"], os.environ["BASE_REF"]
    labels = json.loads(os.environ["LABELS"])
    policy = json.load(open(".github/auto-merge-policy.json", encoding="utf-8"))
    if policy["label"] not in labels:
        print("low-risk 라벨 없음: 자동 머지 대상 아님 (통과)"); return 0
    reasons = []
    if not base.startswith("loop/"): reasons.append(f"대상 `{base}`는 loop/** 가 아니다")
    try:
        files = json.loads(gh("api", f"repos/{repo}/pulls/{pr}/files", "--paginate"))
        reasons += evaluate(files, policy)
        missing = required_ok(repo, base, policy["required_checks"])
        if missing: reasons.append(f"`{base}`에 필수 검사 규칙이 없다: {', '.join(missing)}")
    except Exception as e:  # 읽지 못하면 차단이다
        reasons.append(f"검사 실패, 분석 불가: {e}")
    if reasons:
        report(repo, pr, "### 자동 머지 차단\n`low-risk` 라벨이 있지만 아래 이유로 자동 머지하지 않는다. 직접 검토해 머지하거나 라벨을 떼라.\n\n" + "\n".join(f"- {r}" for r in reasons))
        print("\n".join(reasons)); return 1
    gh("pr", "merge", pr, "--repo", repo, "--auto", "--squash", "--delete-branch")
    report(repo, pr, f"### 자동 머지 가드 통과\n허용 목록 안의 변경이다. 필수 검사({', '.join(policy['required_checks'])})가 통과하면 GitHub가 squash로 머지한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
