"""품질 리뷰 실행기. 리뷰어는 같은 커밋·같은 자료를 받고, 서로의 결과를 보지 않고 동시에 채점한다.

    # 최소 리뷰: 조각 PR마다. CI 없이(조각 PR은 CI를 돌리지 않는다) 리뷰어가 rubric.json의 minimal_skill로 보고 통과/실패 항목으로 옮긴다.
    python -I review.py --level minimal --base origin/loop/<이름> --pr <번호>
    # 상세 리뷰: 큰 단위(루프 브랜치)가 끝나 루프 → 기준 브랜치 PR을 열었을 때. 그 PR 커밋을 체크아웃하고 그 PR의 CI로.
    python -I review.py --level detailed --base origin/<기준 브랜치> --ci-run <run id> --spec <spec 이슈 번호...>

커밋하지 않은 변경이 있으면 거부한다(리뷰는 커밋 하나에 묶인다). 리뷰 대상 경로(config.review_paths)를 건드리지 않는 변경은 건너뛴다.
결과는 ci-results/quality/<sha 7자리>-<단계>/에 리뷰어별 JSON·로그와 verdict.json으로 남는다. 판정은 quality_gate.py가 한다.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality_gate as qg  # noqa: E402
from loopconfig import CONFIG, MODELS, ROOT, effort  # noqa: E402
from scope import REVIEW_PATHS, touches_review  # noqa: E402

TIMEOUT_S = 1800
CONTEXT_FILES = tuple(CONFIG["context_files"])
CONTEXT_LIMIT = 400_000  # 글자 수. 넘으면 변경을 나누라고 하고 멈춘다.
# 단계별 리뷰어 모델·노력 정도는 .agent-loop/models.json의 review 절이 정한다.
LEVELS = MODELS["review"]
# 최소 리뷰 스킬의 두 축을 맡을 서브에이전트. 모델·노력 정도를 고정한다(부모를 따르지 않게).
AXIS_AGENT = "review-axis"


def changed_paths(base, sha):
    """바뀐 파일 경로. 이름 변경은 옛 경로와 새 경로를 모두 넣는다(CI changes 잡의 previous_filename과 같게)."""
    return [path for line in git("diff", "--name-status", "-M", f"{base}...{sha}").splitlines() for path in line.split("\t")[1:]]


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


def claude_command(level, schema, schema_path, tmp):
    section = LEVELS[level]
    tools = ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]
    extra = []
    if level == "minimal":  # 최소 리뷰 스킬은 두 축을 서브에이전트로 돌리고 이슈·PR을 읽는다
        tools += ["Skill", "Agent", "Bash(git rev-parse:*)", "Bash(gh issue view:*)", "Bash(gh pr view:*)"]
        axis = {"description": "코드 리뷰의 한 축(Standards 또는 Spec)을 맡아 보고한다", "prompt": "맡은 축만 본다. 한국어로 쓴다.",
                "model": section["subagent"]["model"], "effort": effort(section["subagent"])}
        extra = ["--agents", json.dumps({AXIS_AGENT: axis}, ensure_ascii=False)]
    me = section["reviewers"]["claude"]
    return ["claude", "-p", "--model", me["model"], "--effort", effort(me), *extra,
            "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False), "--allowedTools", *tools]


def read_claude(tmp):
    with open(os.path.join(tmp, "claude.stdout"), encoding="utf-8") as file:
        return json.load(file)["structured_output"]


def codex_command(level, schema, schema_path, tmp):
    # 사용자 설정의 기본 모델이 ChatGPT 계정에서 거부되므로 모델을 고정하고 사용자 설정(MCP 등)을 읽지 않는다.
    # 이 PC에서는 Codex 샌드박스가 명령을 못 띄우므로(read-only: 정책 거부, elevated: CryptUnprotectData 실패) 자료는 프롬프트로 준다.
    me = LEVELS[level]["reviewers"]["codex"]
    return ["codex", "exec", "-m", me["model"], "-c", f'model_reasoning_effort="{effort(me)}"', "-s", "read-only", "--ephemeral", "--ignore-user-config", "--color", "never",
            "-C", ROOT, "--output-schema", schema_path, "-o", os.path.join(tmp, "codex.out"), "-"]


def read_codex(tmp):
    with open(os.path.join(tmp, "codex.out"), encoding="utf-8") as file:
        return json.load(file)


# 리뷰어 이름 → (명령, 출력 읽기). 둘 다 읽기만 한다. 단계별로 누가 필수인지는 quality_gate가 정한다.
REVIEWERS = {"claude": (claude_command, read_claude), "codex": (codex_command, read_codex)}
LEVEL_REVIEWERS = {level: tuple(section["reviewers"]) for level, section in LEVELS.items()}
assert all(set(names) <= set(REVIEWERS) for names in LEVEL_REVIEWERS.values()), "명령이 없는 리뷰어가 있습니다"


def gh(args, tries=3):
    """gh 호출. GitHub API가 순간적으로 실패하는 일이 있어 몇 번 다시 시도한다."""
    for attempt in range(tries):
        done = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        if done.returncode == 0:
            return done.stdout
        time.sleep(3 * (attempt + 1))
    sys.exit(f"gh {' '.join(args)} 실패: {done.stderr.strip()[:300]}")


RESULTS_ARTIFACT = CONFIG["ci"]["results_artifact"]
RESULT_FILES = tuple(CONFIG["ci"]["result_files"])  # 아티팩트 안의 NUnit3 XML


def latest_artifact(artifacts, name):
    """같은 이름의 아티팩트 중 가장 늦게 만든 것. 재실행한 CI는 시도마다 같은 이름으로 올려 옛 시도의 것이 함께 남는다. 없으면 None."""
    matches = [a for a in artifacts if a["name"] == name and not a.get("expired")]
    return max(matches, key=lambda a: a["created_at"], default=None)


def ci_run(run_id, sha):
    """CI 실행이 이 커밋의 것인지 확인하고 (결론, 테스트 결과 요약)을 돌려준다."""
    run = json.loads(gh(["run", "view", str(run_id), "--json", "headSha,conclusion,url"]))
    if run["headSha"] != sha:
        sys.exit(f"CI 실행 {run_id}은 커밋 {run['headSha'][:7]}의 것입니다(대상 {sha[:7]}).")
    lines = [f"CI 실행 {run_id} ({run['url']}): {run['conclusion']}"]
    artifacts = json.loads(gh(["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/artifacts?per_page=100"]))["artifacts"]
    artifact = latest_artifact(artifacts, RESULTS_ARTIFACT)
    with tempfile.TemporaryDirectory() as tmp:
        downloaded = artifact is not None and subprocess.run(
            ["gh", "api", artifact["archive_download_url"]], cwd=ROOT, capture_output=True)
        if downloaded and downloaded.returncode == 0:
            with zipfile.ZipFile(io.BytesIO(downloaded.stdout)) as archive:
                archive.extractall(tmp)
        for name in RESULT_FILES:
            path = os.path.join(tmp, name)
            if downloaded and downloaded.returncode == 0 and os.path.exists(path):
                lines.append(f"{name}: {test_summary(ET.parse(path).getroot())}")
            else:
                lines.append(f"{name}: 없음(테스트 잡이 건너뛰어졌거나 결과를 받지 못함)")
    return run["conclusion"], "\n".join(lines)


def counts(node):
    return f"통과 {node.get('passed')}/{node.get('total')}, 실패 {node.get('failed')}, 건너뜀 {node.get('skipped')}"


def test_summary(root):
    """NUnit 결과의 전체·어셈블리·fixture별 통과 수. 리뷰어가 어떤 테스트가 돌았는지 이름으로 확인하게 한다."""
    lines = [f"{root.get('result')} {counts(root)}"]
    for assembly in root.iter("test-suite"):
        if assembly.get("type") != "Assembly":
            continue
        lines.append(f"- {assembly.get('name')}: {counts(assembly)}")
        lines += [f"  - {fixture.get('name')}: {counts(fixture)}"
                  for fixture in assembly.iter("test-suite") if fixture.get("type") == "TestFixture"]
    return "\n".join(lines)


def spec_part(numbers):
    """spec 이슈 본문. 리뷰어가 동작이 spec대로인지 판단하는 근거다(Codex는 gh를 못 쓴다)."""
    parts = []
    for number in numbers:
        issue = json.loads(gh(["issue", "view", str(number), "--json", "number,title,body"]))
        parts.append(f"### spec #{issue['number']} {issue['title']}\n{issue['body']}")
    return parts


def evidence_part(base, sha):
    """config.evidence 명령마다 출력을 리뷰 자료로 싣고 hard_fails를 모은다. 반환: (자료 조각들, hard_fails).
    명령은 저장소 루트에서 돌고 인자의 {base}·{sha}·{tree}를 바꿔 받는다. {tree}는 대상 커밋 그대로(추적 안 된 파일 없이) 푼 폴더다.
    출력이 JSON 객체면 그 hard_fails가 최소 리뷰를 실패시킨다(치명적 결함). 0이 아닌 종료는 리뷰를 멈춘다."""
    commands = CONFIG.get("evidence", [])
    parts, hard_fails = [], []
    if not commands:
        return parts, hard_fails
    with tempfile.TemporaryDirectory() as tree:
        archive = subprocess.run(["git", "archive", "--format=tar", sha], cwd=ROOT, capture_output=True, check=True).stdout
        tarfile.open(fileobj=io.BytesIO(archive)).extractall(tree, filter="data")
        for item in commands:
            cmd = [arg.replace("{base}", base).replace("{sha}", sha).replace("{tree}", tree) for arg in item["cmd"]]
            if cmd[0] == "python":
                cmd[0] = sys.executable
            done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            if done.returncode != 0:
                sys.exit(f"증거 명령 {item['title']} 실패(exit {done.returncode}): {done.stderr.strip()[:300]}")
            out = done.stdout.strip()
            try:
                data = json.loads(out)
            except ValueError:
                data = None
            if isinstance(data, dict):
                hard_fails += [f"{item['title']}: {fail}" for fail in data.get("hard_fails", [])]
                out = json.dumps(data, ensure_ascii=False, indent=2)
            parts.append(f"### {item['title']}\n{out}")
    return parts, hard_fails


def rule_files(sha):
    """규칙 문서 전문. 프로젝트 전체 리뷰(project_review.py)도 쓴다."""
    return [f"### {path} (커밋 {sha[:7]})\n{git('show', f'{sha}:{path}')}" for path in CONTEXT_FILES]


# 상세 리뷰 diff에서 본문 대신 통계만 싣는 파일(데이터·에셋·도구·문서와 테스트). 큰 데이터 파일은 자료 한도를 넘긴다.
# 테스트 결과는 CI 아티팩트가 근거다. 나머지 코드 본문은 그대로 싣는다.
DATA_GLOBS = (*CONFIG["stats_only_globs"], *CONFIG["test_globs"])


# 확장자와 상관없이 본문을 싣는 폴더(의존성·빌드 설정). 통계로는 판단할 수 없다.
FULL_DIFF_DIRS = tuple(CONFIG["full_diff_dirs"])


def diff_part(base, sha):
    commits = f"{base}...{sha}"
    full = [f":(exclude){folder}" for folder in FULL_DIFF_DIRS]
    excludes = [f":(exclude){glob}" for glob in DATA_GLOBS]
    parts = [f"### git diff {commits} (데이터·에셋 파일과 {'·'.join(FULL_DIFF_DIRS)} 제외)\n{git('diff', commits, '--', '.', *full, *excludes)}"]
    settings = git("diff", commits, "--", *FULL_DIFF_DIRS)
    if settings:
        parts.append(f"### git diff {commits} -- {' '.join(FULL_DIFF_DIRS)} (본문 전체)\n{settings}")
    stat = git("diff", "--stat=200", commits, "--", *DATA_GLOBS, *full)
    if stat:
        parts.append(f"### 데이터·에셋 파일 변경 통계(본문 생략: {', '.join(DATA_GLOBS)})\n{stat}")
    return parts


def context(base, sha, evidence, pr_text, with_diff, specs=()):
    """리뷰어 모두에게 똑같이 주는 자료. Codex는 이 PC의 샌드박스에서 명령을 못 돌리므로 필요한 것을 싣는다."""
    parts = rule_files(sha)
    if pr_text:
        parts.append(f"### PR 본문(이 조각의 범위와 수용 기준)\n{pr_text}")
    parts += spec_part(specs)
    parts += evidence
    parts.append(f"### git log {base}..{sha}\n{git('log', '--format=%h %s', f'{base}..{sha}')}")
    if with_diff:  # 최소 리뷰는 스킬의 두 축 서브에이전트가 diff를 직접 읽게 싣지 않는다
        parts += diff_part(base, sha)
    text = "\n\n".join(parts)
    if len(text) > CONTEXT_LIMIT:
        sys.exit(f"리뷰 자료가 {len(text)}자로 한도 {CONTEXT_LIMIT}자를 넘습니다. 변경을 나누세요.")
    return text


# 최소 리뷰 프롬프트의 "CI 결과" 자리. 조각 PR은 CI를 돌리지 않는다(2026-11-19 사용자 결정).
MINIMAL_EVIDENCE = ("조각 PR은 CI를 돌리지 않는다. 대신 구현 세션이 이 조각이 만들거나 고친 테스트만 로컬로 돌려 PR 본문에 명령과 결과를 적는다. "
                    "테스트를 만들거나 고쳤는데 그 기록이 없거나 실패가 있으면 m2를 fail로 둔다. 나머지는 코드·spec·범위로 판단한다. "
                    "전체 테스트 근거는 루프 끝 기준 브랜치 PR의 CI가 본다.")


def prompt(level, sha, evidence, material, base):
    if level == "minimal":
        task = (f"조각 PR 하나의 최소 리뷰다. 먼저 Skill 도구로 `{qg.MINIMAL_SKILL}`을 고정점 `{base}`로 실행한다. "
                "Spec 출처는 리뷰 자료의 PR 본문(수용 기준)과 그 본문이 가리키는 이슈다. Standards 출처는 CLAUDE.md다. "
                f"스킬 절차대로 Standards와 Spec을 Agent 도구(subagent_type `{AXIS_AGENT}`)로 각각 서브에이전트에 맡긴다(두 축이 섞이지 않게, 직접 대신 보지 않는다). "
                "사람에게 묻지 않는다(비대화형). 스킬이 묻는 것은 여기 주어진 것으로 정한다.\n"
                f"스킬이 끝나면 그 보고서 전문을 report에 넣고, 결과를 아래 항목으로 옮긴다.\n\n{qg.minimal_text()}\n"
                "확인하지 못한 항목은 unknown으로 둔다.")
    else:
        task = (f"{qg.rubric_text()}\n\n큰 단위(루프 브랜치) 전체의 상세 리뷰다. A 구조와 B 구현·검증, 필수 게이트를 모두 채점한다. "
                "B와 게이트의 테스트 근거는 아래 CI 결과다. 동작이 요구대로인지(특히 state 게이트)는 리뷰 자료의 spec 이슈 본문으로 판단한다. 채점 규칙은 리뷰 자료의 규칙 문서 \"채점\"·\"게이트\" 절이다. "
                "확인하지 못한 항목은 quant·qual을 null로 둔다.")
    return compose(sha, task, evidence, material)


def compose(sha, task, evidence, material, subject="변경분"):
    """리뷰어 프롬프트의 공통 틀. 프로젝트 전체 리뷰(project_review.py)도 쓴다."""
    return f"""당신은 이 저장소의 독립 리뷰어다. 파일을 고치지 않는다. 다른 리뷰어의 결과를 찾거나 읽지 않는다(ci-results/를 열지 않는다).

대상: 커밋 {sha}. 규칙 문서와 {subject}은 아래 "리뷰 자료"에 있다. 도구로 저장소를 더 읽을 수 있으면 읽어도 된다.
도구 실행이 실패하면 그 내용을 추측하지 않는다. 근거는 리뷰 자료나 실제로 성공한 명령 결과에서만 가져온다.
문제는 issues에 파일·줄, 어긴 원칙, 증거, 영향, 개선 방안으로 적는다. 글은 한국어로 쓴다. 출력은 주어진 JSON 스키마 그대로다.

{task}

CI 결과:
{evidence}

## 리뷰 자료
{material}"""


def run_reviewers(level, schema, text, tmp):
    schema_path = os.path.join(tmp, "schema.json")
    with open(schema_path, "w", encoding="utf-8") as file:
        json.dump(schema, file, ensure_ascii=False)
    processes = {}
    for name in LEVEL_REVIEWERS[level]:  # 동시에 띄워 서로의 결과가 생기기 전에 시작한다
        files = [open(os.path.join(tmp, f"{name}.{kind}"), "w", encoding="utf-8") for kind in ("stdout", "log")]
        process = subprocess.Popen(REVIEWERS[name][0](level, schema, schema_path, tmp), cwd=ROOT, stdin=subprocess.PIPE,
                                   stdout=files[0], stderr=files[1], text=True, encoding="utf-8")
        process.stdin.write(text)
        process.stdin.close()
        processes[name] = (process, files)
    outputs = {}
    for name, (process, files) in processes.items():
        try:
            process.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for file in files:
            file.close()
        try:
            outputs[name] = REVIEWERS[name][1](tmp)
        except (OSError, ValueError, KeyError, TypeError):
            outputs[name] = None  # 못 읽은 리뷰어는 없는 것으로 판정돼 미완료가 된다
    return outputs


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def main(argv):
    parser = argparse.ArgumentParser(description="품질 리뷰(최소·상세)")
    parser.add_argument("--level", choices=("minimal", "detailed"), required=True)
    parser.add_argument("--base", required=True, help="비교 기준. 최소: origin/loop/<이름>, 상세: origin/<기준 브랜치>")
    parser.add_argument("--ci-run", help="상세 리뷰: 루프 → 기준 브랜치 PR 커밋의 CI 실행 id(필수). 최소 리뷰는 쓰지 않는다")
    parser.add_argument("--pr", help="최소 리뷰 대상 PR 번호(본문의 수용 기준을 리뷰어에게 준다)")
    parser.add_argument("--spec", type=int, nargs="+", default=[], help="변경이 따르는 spec 이슈 번호(본문을 리뷰 자료에 싣는다). 상세 리뷰에는 필수, 루프가 다룬 spec 전부")
    args = parser.parse_args(argv)
    if args.level == "minimal" and not args.pr:
        parser.error("최소 리뷰에는 --pr이 필요합니다")
    if args.level == "detailed" and not args.spec:  # spec 없이는 동작이 요구대로인지(state 게이트) 판단할 근거가 없다(#203)
        parser.error("상세 리뷰에는 --spec <spec 이슈 번호...>가 필요합니다")
    if git("status", "--porcelain", "--untracked-files=no"):
        sys.exit("커밋하지 않은 변경이 있습니다. 리뷰는 커밋 하나를 대상으로 합니다.")
    sha = git("rev-parse", "HEAD")
    out_dir = os.path.join(ROOT, "ci-results", "quality", f"{sha[:7]}-{args.level}")
    os.makedirs(out_dir, exist_ok=True)

    changed = changed_paths(args.base, sha)
    if not touches_review(changed):
        # 리뷰 대상 경로를 건드리지 않는 변경(도구·문서·에이전트 설정)은 리뷰하지 않는다.
        verdict = {"verdict": "skipped", "git_sha": sha, "level": args.level,
                   "reasons": [f"리뷰 대상 경로({', '.join(REVIEW_PATHS)}) 변경 없음"], "changed": changed}
        write_json(os.path.join(out_dir, "verdict.json"), verdict)
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0

    if args.level == "detailed":
        if not args.ci_run:
            parser.error("상세 리뷰에는 루프 → 기준 브랜치 PR의 CI 실행 id(--ci-run)가 필요합니다")
        conclusion, evidence = ci_run(args.ci_run, sha)
    else:
        conclusion, evidence = None, MINIMAL_EVIDENCE
    evidence_parts, hard_fails = evidence_part(args.base, sha)
    pr_text = ""
    if args.pr:
        pr = json.loads(gh(["pr", "view", args.pr, "--json", "title,body"]))
        pr_text = f"{pr['title']}\n\n{pr['body']}"
    schema = qg.minimal_schema() if args.level == "minimal" else qg.review_schema()
    material = context(args.base, sha, evidence_parts, pr_text, with_diff=args.level == "detailed", specs=args.spec)
    text = prompt(args.level, sha, evidence, material, args.base)

    reviews = []
    # 리뷰 중에는 결과를 저장소 밖 임시 폴더에 둔다(리뷰어가 서로의 결과를 읽지 못하게).
    with tempfile.TemporaryDirectory() as tmp:
        for name, data in run_reviewers(args.level, schema, text, tmp).items():
            for kind in ("stdout", "log"):
                shutil.copy(os.path.join(tmp, f"{name}.{kind}"), out_dir)
            if data is None:
                print(f"{name}: 출력을 읽지 못함 (로그 {os.path.join(out_dir, name + '.log')})")
                continue
            review = {"schema": qg.SCHEMA, "reviewer": name, "git_sha": sha, "level": args.level, **data}
            write_json(os.path.join(out_dir, f"{name}.json"), review)
            reviews.append(review)

    if args.level == "minimal":
        verdict = qg.judge_minimal(reviews, sha, {"hard_fails": hard_fails})
        summary = {key: verdict[key] for key in ("verdict", "failed", "reasons")}
        passed = verdict["verdict"] == "pass"
    else:
        verdict = qg.judge_all(reviews, sha, conclusion)
        summary = {"verdict": verdict["verdict"], "reasons": verdict["reasons"],
                   "reviewers": {name: {"verdict": r["verdict"], "A": r["a"]["score"], "B": r["b"]["score"]}
                                 for name, r in verdict["reviewers"].items()}}
        passed = verdict["verdict"] == "approved"
    write_json(os.path.join(out_dir, "verdict.json"), verdict)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"결과: {out_dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
