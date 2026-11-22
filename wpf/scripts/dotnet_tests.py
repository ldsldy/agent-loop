"""바뀐 테스트 파일에서 돌릴 테스트를 고르고, dotnet으로 돌려 통과·실패를 판정한다. git을 부르지 않는다(테스트 대상).

조각 검사(slice_check.py)가 쓴다. 테스트 파일이 하나도 안 바뀌었으면 빌드만 확인한다.
"""
import os
import re
import subprocess
import xml.etree.ElementTree as ET

ATTRIBUTES = "Test|TestCase|TestCaseSource|Fact|Theory|TestMethod|DataTestMethod|WpfFact|WpfTheory|StaFact|StaTheory"  # NUnit·xUnit·MSTest
TEST_MARK = re.compile(rf"\[(?:{ATTRIBUTES})\b")
NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
CLASS = re.compile(r"^\s*(?:(?:public|internal|private|protected|sealed|abstract|static|partial)\s+)*class\s+(\w+)", re.M)
COMPILE_ERROR = re.compile(r"error (?:CS|MC)\d+")  # C#과 XAML 마크업 컴파일 오류
MAX_LINES = 20  # 실패 출력은 구현 세션의 맥락으로 들어간다. 길게 싣지 않는다.


def fixtures(source):
    """테스트 파일 본문 → 필터에 넣을 테스트 클래스 전체 이름. 테스트 표시가 없는 파일(도우미)은 빈 목록."""
    if not TEST_MARK.search(source):
        return []
    space = NAMESPACE.search(source)
    prefix = f"{space.group(1)}." if space else ""
    return sorted({prefix + name for name in CLASS.findall(source)})


def project(root, path):
    """테스트 파일이 속한 프로젝트(가장 가까운 위 폴더의 .csproj). 없으면 ""(저장소 루트의 솔루션)."""
    folder = os.path.dirname(path)
    while True:
        full = os.path.join(root, folder)
        found = sorted(name for name in os.listdir(full) if name.endswith(".csproj")) if os.path.isdir(full) else []
        if found:
            return f"{folder}/{found[0]}".lstrip("/")
        if not folder:
            return ""
        folder = os.path.dirname(folder)


def plan(root, paths):
    """바뀐 테스트 파일 경로들 → {테스트 프로젝트: [테스트 클래스 전체 이름...]}. 돌릴 테스트가 없으면 빈 dict."""
    chosen = {}
    for path in paths:
        if not path.endswith(".cs"):
            continue
        with open(os.path.join(root, path), encoding="utf-8-sig") as file:
            names = fixtures(file.read())
        if names:
            chosen.setdefault(project(root, path), set()).update(names)
    return {name: sorted(chosen[name]) for name in sorted(chosen)}


def label(target):
    """결과·로그 파일 이름에 쓰는 이름. target이 None이면 빌드만 하는 실행이다."""
    return "build" if target is None else os.path.splitext(os.path.basename(target))[0] or "tests"


def command(root, out, target, names=()):
    """dotnet 명령. target이 None이면 빌드만 확인한다. 저장소 루트를 주면 dotnet이 거기의 솔루션을 찾는다."""
    # shortcut: VSTest 방식의 dotnet test만 안다(--filter, trx 로거), Microsoft.Testing.Platform으로 옮긴 저장소가 생기면 명령을 가른다.
    if target is None:
        return ["dotnet", "build", root, "-nologo"]
    return ["dotnet", "test", os.path.join(root, target) if target else root, "--nologo",
            "--filter", "|".join(f"FullyQualifiedName~{name}." for name in names),
            "--logger", f"trx;LogFileName={label(target)}.trx", "--results-directory", out]


def execute(cmd, log):
    """명령을 돌리고 출력을 log에 남긴다."""
    with open(log, "w", encoding="utf-8") as file:
        return subprocess.run(cmd, stdout=file, stderr=subprocess.STDOUT)


def _compile_errors(log):
    if not os.path.exists(log):
        return []
    with open(log, encoding="utf-8", errors="replace") as file:
        return list(dict.fromkeys(line.strip() for line in file if COMPILE_ERROR.search(line)))


PASS, FAIL, COMPILE, UNKNOWN = "pass", "fail", "compile", "unknown"


def judge(target, code, results, log):
    """실행 한 번의 판정 → (상태, 보고 줄들). results(trx)·log는 그 실행이 남긴 파일 경로다.
    상태: PASS, FAIL(테스트가 실패), COMPILE(컴파일 오류), UNKNOWN(테스트를 돌리지 못했거나 필터에 맞는 것이 없음)."""
    name = "빌드" if target is None else label(target)
    errors = _compile_errors(log)
    if errors:
        return COMPILE, [f"{name}: 컴파일 오류 {len(errors)}건"] + errors[:MAX_LINES]
    if target is None:
        return (PASS, ["빌드 통과"]) if code == 0 else (UNKNOWN, [f"빌드 실패(dotnet 종료 코드 {code})"])
    if not os.path.exists(results):
        return UNKNOWN, [f"{name}: 결과 파일이 없다(dotnet 종료 코드 {code}). 필터에 맞는 테스트가 없거나 돌리지 못했다. 로그를 본다"]
    root = ET.parse(results).getroot()
    counters = root.find(".//{*}Counters")
    total, passed, failed = (int(counters.get(key, 0)) if counters is not None else 0 for key in ("total", "passed", "failed"))
    if total == 0:
        return UNKNOWN, [f"{name}: 필터에 맞는 테스트가 0개다"]
    if code == 0 and passed == total:
        return PASS, [f"{name} 통과 {passed}/{total}"]
    lines = [f"{name} 실패 {total - passed}/{total}(dotnet 종료 코드 {code})"]
    for case in root.findall(".//{*}UnitTestResult"):
        if case.get("outcome") != "Passed":
            message = (case.findtext(".//{*}Message") or "").strip().splitlines()
            lines.append(f"- {case.get('testName')}: {message[0][:300] if message else case.get('outcome')}")
    return FAIL, lines[:MAX_LINES + 1]


def run(root, out, chosen, runner=execute):
    """chosen({테스트 프로젝트: 테스트 클래스들})을 차례로 돌린다. 비어 있으면 빌드만 확인한다. 첫 실패에서 멈춘다 → (상태, 보고 줄들)."""
    lines = []
    for target, names in list(chosen.items()) or [(None, ())]:
        results, log = (os.path.join(out, f"{label(target)}.{kind}") for kind in ("trx", "log"))
        for stale in (results, log):  # 이번 실행이 못 만들면 옛 결과가 통과처럼 보이는 것을 막는다
            if os.path.exists(stale):
                os.remove(stale)
        status, report = judge(target, runner(command(root, out, target, names), log).returncode, results, log)
        lines += report
        if status != PASS:
            return status, lines
    return PASS, lines
