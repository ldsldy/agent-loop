"""품질 리뷰 실행기. 리뷰어는 같은 커밋·같은 자료를 받고, 서로의 결과를 보지 않고 동시에 채점한다.

    # 최소 리뷰: 조각 PR마다. 기계가 통과로 확정하지 못한 항목만 리뷰어 하나가 diff를 보고 통과/실패를 낸다(simple 조각은 기계 판정만).
    python -I review.py --level minimal --base origin/loop/<이름> --pr <번호> [--unit <상태 파일>]
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
import unit as units  # noqa: E402
from loopconfig import CONFIG, MODELS, ROOT, effort  # noqa: E402
from scope import REVIEW_PATHS, is_test, touches_review  # noqa: E402

TIMEOUT_S = 1800
CONTEXT_FILES = tuple(CONFIG["context_files"])
CONTEXT_LIMIT = 400_000  # 글자 수. 넘으면 변경을 나누라고 하고 멈춘다.
# 단계별 리뷰어 모델·노력 정도는 .agent-loop/models.json의 review 절이 정한다.
LEVELS = MODELS["review"]


def changed_rows(base, sha):
    """바뀐 파일의 (상태 글자, 경로들). 이름 변경은 옛 경로와 새 경로를 함께 준다(CI changes 잡의 previous_filename과 같게)."""
    rows = [line.split("\t") for line in git("diff", "--name-status", "-M", f"{base}...{sha}").splitlines()]
    return [(row[0][0], row[1:]) for row in rows]


def changed_paths(base, sha):
    return [path for _, paths in changed_rows(base, sha) for path in paths]


def tests_kept(rows, piece):
    if all(status == "A" for status, paths in rows if any(is_test(path) for path in paths)):
        return "기존 테스트 파일을 고치거나 지우지 않았다"


def declared_files(rows, piece):
    """테스트가 아닌 바뀐 파일이 모두 조각의 files(파일이나 폴더) 안인가. Unity의 .meta는 짝 파일을 따른다."""
    declared = [entry.rstrip("/") for entry in (piece or {}).get("files", [])]
    paths = [path.removesuffix(".meta") for _, row in rows for path in row if not is_test(path)]
    if declared and all(any(path == entry or path.startswith(entry + "/") for entry in declared) for path in paths):
        return "테스트가 아닌 바뀐 파일이 모두 조각의 files 안이다"


# rubric.json 최소 리뷰 항목의 auto 이름 → 판정. 통과를 확정하면 근거를, 못 하면 None을 돌려준다(그 항목은 리뷰어가 본다).
AUTO = {"tests_kept": tests_kept, "declared_files": declared_files}


def machine_facts(rows, piece, hard_fails):
    """최소 리뷰에서 기계가 정하는 것. piece는 이 PR의 조각(상태 파일에서 찾지 못하면 None).
    simple 조각이 기계 판정을 모두 통과하면 남은 항목은 상세 리뷰로 미룬다(리뷰어를 부르지 않는다)."""
    passed = {item: why for item, _, _, auto in qg.MINIMAL_CHECKS if auto and (why := AUTO[auto](rows, piece))}
    autos = [item for item, _, _, auto in qg.MINIMAL_CHECKS if auto]
    simple = bool(piece) and piece["complexity"] == "simple" and bool(autos) and all(item in passed for item in autos)
    deferred = [item for item, *_ in qg.MINIMAL_CHECKS if item not in passed] if simple else []
    return {"hard_fails": hard_fails, "passed": passed, "deferred": deferred}


def slice_of(state_path, branch):
    """상태 파일에서 이 PR 브랜치의 조각을 찾는다. 상태 파일을 주지 않았거나 그 조각이 없으면 None."""
    if not state_path:
        return None
    with open(state_path, encoding="utf-8") as file:
        state = json.load(file)
    return next((piece for piece in state["slices"] if units.task_branch(state, piece) == branch), None)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


def claude_command(level, schema, schema_path, tmp):
    section = LEVELS[level]
    tools = ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]
    if level == "minimal":  # 수용 기준이 가리키는 이슈를 읽는다
        tools.append("Bash(gh issue view:*)")
    me = section["reviewers"]["claude"]
    return ["claude", "-p", "--model", me["model"], "--effort", effort(me),
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


SCORING = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "references", "scoring.md")


def rule_files(sha, scoring=True):
    """플러그인 채점 규칙과 저장소 규칙 문서(config.context_files, 대상 커밋 그대로) 전문. 프로젝트 전용 리뷰 도구도 쓴다.
    최소 리뷰는 점수를 매기지 않으므로 채점 규칙을 싣지 않는다."""
    parts = []
    if scoring:
        with open(SCORING, encoding="utf-8") as file:
            parts.append(f"### 채점 규칙 (agent-loop references/scoring.md)\n{file.read()}")
    return parts + [f"### {path} (커밋 {sha[:7]})\n{git('show', f'{sha}:{path}')}" for path in CONTEXT_FILES]


# 상세 리뷰 diff에서 본문 대신 통계만 싣는 파일(데이터·에셋·도구·문서와 테스트). 큰 데이터 파일은 자료 한도를 넘긴다.
# 테스트 결과는 CI 아티팩트가 근거다. 나머지 코드 본문은 그대로 싣는다.
DATA_GLOBS = (*CONFIG["stats_only_globs"], *CONFIG["test_globs"])


# 확장자와 상관없이 본문을 싣는 폴더(의존성·빌드 설정). 통계로는 판단할 수 없다.
FULL_DIFF_DIRS = tuple(CONFIG["full_diff_dirs"])


def diff_part(base, sha, stats_globs=DATA_GLOBS):
    """리뷰 자료의 diff. stats_globs에 맞는 파일은 본문 대신 통계만 싣는다. 최소 리뷰는 테스트 본문도 싣는다(테스트 보존을 볼 수 있게)."""
    commits = f"{base}...{sha}"
    full = [f":(exclude){folder}" for folder in FULL_DIFF_DIRS]
    excludes = [f":(exclude){glob}" for glob in stats_globs]
    parts = [f"### git diff {commits} (데이터·에셋 파일과 {'·'.join(FULL_DIFF_DIRS)} 제외)\n{git('diff', commits, '--', '.', *full, *excludes)}"]
    settings = git("diff", commits, "--", *FULL_DIFF_DIRS)
    if settings:
        parts.append(f"### git diff {commits} -- {' '.join(FULL_DIFF_DIRS)} (본문 전체)\n{settings}")
    stat = git("diff", "--stat=200", commits, "--", *stats_globs, *full)
    if stat:
        parts.append(f"### 데이터·에셋 파일 변경 통계(본문 생략: {', '.join(stats_globs)})\n{stat}")
    return parts


def previous_reviews(verdict_path):
    """이전 상세 리뷰의 (커밋, {리뷰어: 채점}). 개선 뒤 재리뷰가 리뷰어마다 자기 이전 채점에서 이어 가게 한다."""
    with open(verdict_path, encoding="utf-8") as file:
        sha = json.load(file)["git_sha"]
    reviews = {}
    for name in LEVEL_REVIEWERS["detailed"]:
        path = os.path.join(os.path.dirname(verdict_path), f"{name}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as file:
                reviews[name] = json.load(file)
    return sha, reviews


def context(base, sha, evidence, pr_text, level, specs=(), earlier=None):
    """리뷰어에게 주는 자료. Codex는 이 PC의 샌드박스에서 명령을 못 돌리므로 필요한 것을 싣는다.
    earlier는 (이전 리뷰 커밋, 이 리뷰어의 이전 채점)이다. 있으면 diff는 그 커밋 뒤의 것만 싣는다."""
    minimal = level == "minimal"
    parts = rule_files(sha, scoring=not minimal)
    if pr_text:
        parts.append(f"### PR 본문(이 조각의 범위와 수용 기준)\n{pr_text}")
    parts += spec_part(specs)
    parts += evidence
    if earlier:
        base = earlier[0]
        parts.append(f"### 이전 채점(커밋 {base[:7]}까지의 변경 전체를 보고 낸 결과)\n{json.dumps(earlier[1], ensure_ascii=False, indent=1)}")
    parts.append(f"### git log {base}..{sha}\n{git('log', '--format=%h %s', f'{base}..{sha}')}")
    parts += diff_part(base, sha, tuple(CONFIG["stats_only_globs"]) if minimal else DATA_GLOBS)
    text = "\n\n".join(parts)
    if len(text) > CONTEXT_LIMIT:
        sys.exit(f"리뷰 자료가 {len(text)}자로 한도 {CONTEXT_LIMIT}자를 넘습니다. 변경을 나누세요.")
    return text


# 최소 리뷰 프롬프트의 "CI 결과" 자리. 조각 PR은 CI를 돌리지 않는다(2026-11-19 사용자 결정).
MINIMAL_EVIDENCE = ("조각 PR은 CI를 돌리지 않는다. 대신 조각 검사(slice_check.py)가 이 커밋의 트리로 컴파일과 이 조각이 만들거나 고친 테스트를 돌렸고, "
                    "통과 기록이 없는 커밋은 이 리뷰 전에 막힌다(pr_chain.sh). 테스트가 돌았는지는 따지지 않고 코드·spec·범위로 판단한다. "
                    "전체 테스트 근거는 루프 끝 기준 브랜치 PR의 CI가 본다.")


INCREMENTAL = ("상세 리뷰 미달 뒤 개선분의 재리뷰다. 리뷰 자료의 \"이전 채점\"은 같은 리뷰어 자리에서 이전 커밋까지의 변경 전체를 보고 낸 결과이고, "
               "diff는 그 뒤의 개선분만 있다. 이전 issues가 각각 고쳐졌는지 확인하고, 개선분이 영향을 준 항목의 등급·게이트만 고쳐 평가표 전체를 다시 낸다. "
               "영향이 없는 항목은 이전 값과 근거를 그대로 옮긴다. 고쳐지지 않은 지적과 새로 생긴 문제는 issues에 적는다. ")


def prompt(level, sha, evidence, material, machine=None, incremental=False):
    if level == "minimal":
        settled = "".join(f"\n- {item}: {why}" for item, why in machine["passed"].items())
        task = ("조각 PR 하나의 최소 리뷰다. 아래 항목만 판단한다. 수용 기준의 출처는 리뷰 자료의 PR 본문과 그 본문이 가리키는 이슈, "
                f"규칙의 출처는 리뷰 자료의 저장소 규칙 문서({', '.join(CONTEXT_FILES)})다. 사람에게 묻지 않는다(비대화형).\n\n"
                f"{qg.minimal_text(qg.open_checks(machine))}\n확인하지 못한 항목은 unknown으로 둔다."
                + (f"\n\n기계가 이미 통과로 확정해 다시 보지 않는 항목:{settled}" if settled else ""))
    else:
        task = (f"{qg.rubric_text()}\n\n큰 단위(루프 브랜치) 전체의 상세 리뷰다. 평가표의 모든 그룹과 필수 게이트를 채점한다. "
                + (INCREMENTAL if incremental else "") + "테스트 근거는 아래 CI 결과다. 동작이 요구대로인지는 리뷰 자료의 spec 이슈 본문으로 판단한다. 채점 규칙은 리뷰 자료의 \"채점 규칙\" 문서다. "
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
    """리뷰어를 동시에 돌린다. text는 모두에게 줄 프롬프트 하나거나 {리뷰어: 프롬프트}다."""
    texts = text if isinstance(text, dict) else dict.fromkeys(LEVEL_REVIEWERS[level], text)
    schema_path = os.path.join(tmp, "schema.json")
    with open(schema_path, "w", encoding="utf-8") as file:
        json.dump(schema, file, ensure_ascii=False)
    processes = {}
    for name in LEVEL_REVIEWERS[level]:  # 동시에 띄워 서로의 결과가 생기기 전에 시작한다
        files = [open(os.path.join(tmp, f"{name}.{kind}"), "w", encoding="utf-8") for kind in ("stdout", "log")]
        process = subprocess.Popen(REVIEWERS[name][0](level, schema, schema_path, tmp), cwd=ROOT, stdin=subprocess.PIPE,
                                   stdout=files[0], stderr=files[1], text=True, encoding="utf-8")
        process.stdin.write(texts[name])
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
    parser.add_argument("--unit", help="최소 리뷰: 큰 단위 상태 파일. 이 PR 조각의 files·complexity로 기계 판정을 한다(없으면 리뷰어가 모든 항목을 본다)")
    parser.add_argument("--previous", help="상세 리뷰: 개선 뒤 재리뷰일 때 이전 상세 리뷰의 verdict.json. 리뷰어마다 자기 이전 채점과 그 뒤의 diff만 받는다")
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
    pr_text, machine = "", None
    if args.pr:
        pr = json.loads(gh(["pr", "view", args.pr, "--json", "title,body,headRefName"]))
        pr_text = f"{pr['title']}\n\n{pr['body']}"
        machine = machine_facts(changed_rows(args.base, sha), slice_of(args.unit, pr["headRefName"]), hard_fails)
    minimal = args.level == "minimal"
    # 기계가 모든 항목을 정했거나(통과 확정·미룸) 증거 명령이 이미 실패시켰으면 리뷰어를 부르지 않는다.
    ask = not minimal or (qg.open_checks(machine) and not hard_fails)
    reviews = []
    # 리뷰 중에는 결과를 저장소 밖 임시 폴더에 둔다(리뷰어가 서로의 결과를 읽지 못하게).
    with tempfile.TemporaryDirectory() as tmp:
        outputs = {}
        if ask:
            schema = qg.minimal_schema(qg.open_checks(machine)) if minimal else qg.review_schema()
            before, earlier = previous_reviews(args.previous) if args.previous else (None, {})
            texts = {}
            for name in LEVEL_REVIEWERS[args.level]:  # 이전 채점이 없는 리뷰어는 전체 diff를 받는다
                mine = (before, earlier[name]) if name in earlier else None
                material = context(args.base, sha, evidence_parts, pr_text, args.level, specs=args.spec, earlier=mine)
                texts[name] = prompt(args.level, sha, evidence, material, machine, incremental=bool(mine))
            outputs = run_reviewers(args.level, schema, texts, tmp)
        for name, data in outputs.items():
            for kind in ("stdout", "log"):
                shutil.copy(os.path.join(tmp, f"{name}.{kind}"), out_dir)
            if data is None:
                print(f"{name}: 출력을 읽지 못함 (로그 {os.path.join(out_dir, name + '.log')})")
                continue
            review = {"schema": qg.SCHEMA, "reviewer": name, "git_sha": sha, "level": args.level, **data}
            write_json(os.path.join(out_dir, f"{name}.json"), review)
            reviews.append(review)

    if args.level == "minimal":
        verdict = qg.judge_minimal(reviews, sha, machine)
        summary = {key: verdict[key] for key in ("verdict", "failed", "reasons", "machine")}
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
