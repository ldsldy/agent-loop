"""바뀐 Unity 테스트 파일에서 돌릴 테스트를 고르고, batchmode로 돌려 통과·실패를 판정한다. git을 부르지 않는다(테스트 대상).

조각 검사(slice_check.py)가 쓴다. 테스트 파일이 하나도 안 바뀌었으면 컴파일만 확인한다.
"""
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET

PLATFORMS = ("EditMode", "PlayMode")
TEST_MARK = re.compile(r"\[(?:Test|UnityTest|TestCase|TestCaseSource)\b")
NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
CLASS = re.compile(r"^\s*(?:(?:public|internal|private|protected|sealed|abstract|static|partial)\s+)*class\s+(\w+)", re.M)
COMPILE_ERROR = re.compile(r"error CS\d+")
MAX_LINES = 20  # 실패 출력은 구현 세션의 맥락으로 들어간다. 길게 싣지 않는다.


def fixtures(source):
    """테스트 파일 본문 → -testFilter에 넣을 fixture 전체 이름. 테스트 표시가 없는 파일(도우미)은 빈 목록."""
    if not TEST_MARK.search(source):
        return []
    space = NAMESPACE.search(source)
    prefix = f"{space.group(1)}." if space else ""
    return sorted({prefix + name for name in CLASS.findall(source)})


def platform(root, path):
    """테스트 파일이 속한 asmdef로 EditMode·PlayMode를 가른다. includePlatforms가 Editor뿐이면 EditMode다."""
    folder = os.path.dirname(path)
    while folder:
        full = os.path.join(root, folder)
        for name in sorted(os.listdir(full)) if os.path.isdir(full) else []:
            if name.endswith(".asmdef"):
                with open(os.path.join(full, name), encoding="utf-8-sig") as file:
                    return "EditMode" if json.load(file).get("includePlatforms") == ["Editor"] else "PlayMode"
        folder = os.path.dirname(folder)
    return "EditMode" if "/Editor/" in f"/{path}" else "PlayMode"


def plan(root, paths):
    """바뀐 테스트 파일 경로들 → {플랫폼: [fixture 전체 이름...]}. 돌릴 테스트가 없으면 빈 dict."""
    chosen = {}
    for path in paths:
        if not path.endswith(".cs"):
            continue
        with open(os.path.join(root, path), encoding="utf-8-sig") as file:
            names = fixtures(file.read())
        if names:
            chosen.setdefault(platform(root, path), set()).update(names)
    return {name: sorted(chosen[name]) for name in PLATFORMS if name in chosen}


def editor(root):
    """프로젝트가 쓰는 버전의 Unity 실행 파일."""
    # shortcut: Unity Hub 기본 설치 경로(Windows)만 본다, 다른 곳에 깐 PC가 생기면 설정으로 뺀다.
    with open(os.path.join(root, "ProjectSettings", "ProjectVersion.txt"), encoding="utf-8") as file:
        version = re.search(r"m_EditorVersion:\s*(\S+)", file.read()).group(1)
    return rf"C:\Program Files\Unity\Hub\Editor\{version}\Editor\Unity.exe"


def command(unity, root, out, mode, names=()):
    """batchmode 명령. mode가 None이면 컴파일만 확인한다. UI 테스트가 Editor 창을 띄우므로 -nographics를 쓰지 않는다."""
    base = [unity, "-batchmode", "-projectPath", root, "-logFile", os.path.join(out, f"{mode or 'compile'}.log")]
    if mode is None:
        return base + ["-quit"]
    return base + ["-runTests", "-testPlatform", mode, "-testFilter", ";".join(names), "-testResults", os.path.join(out, f"{mode}.xml")]


def _compile_errors(log):
    if not os.path.exists(log):
        return []
    with open(log, encoding="utf-8", errors="replace") as file:
        return list(dict.fromkeys(line.strip() for line in file if COMPILE_ERROR.search(line)))


def judge(mode, code, results, log):
    """실행 한 번의 판정 → (통과 여부, 보고 줄들). results·log는 그 실행이 남긴 파일 경로다."""
    label = mode or "컴파일"
    errors = _compile_errors(log)
    if errors:
        return False, [f"{label}: 컴파일 오류 {len(errors)}건"] + errors[:MAX_LINES]
    if mode is None:
        return code == 0, [f"컴파일 {'통과' if code == 0 else f'실패(Unity 종료 코드 {code})'}"]
    if not os.path.exists(results):
        return False, [f"{label}: 결과 파일이 없다(Unity 종료 코드 {code}). 로그를 본다"]
    root = ET.parse(results).getroot()
    total, failed = int(root.get("total", 0)), int(root.get("failed", 0))
    if total == 0:
        return False, [f"{label}: 필터에 맞는 테스트가 0개다"]
    if code == 0 and failed == 0:
        return True, [f"{label} 통과 {root.get('passed')}/{total}"]
    lines = [f"{label} 실패 {failed}/{total}(Unity 종료 코드 {code})"]
    for case in root.iter("test-case"):
        if case.get("result") == "Failed":
            message = (case.findtext("failure/message") or "").strip().splitlines()
            lines.append(f"- {case.get('fullname')}: {message[0][:300] if message else ''}")
    return False, lines[:MAX_LINES + 1]


def run(root, out, chosen, runner=subprocess.run):
    """chosen({플랫폼: fixture들})을 차례로 돌린다. 비어 있으면 컴파일만 확인한다. 첫 실패에서 멈춘다 → (통과 여부, 보고 줄들).
    같은 프로젝트를 두 Unity가 함께 열 수 없어 한 번에 하나만 돈다."""
    unity = editor(root)
    lines = []
    for mode, names in list(chosen.items()) or [(None, ())]:
        results, log = (os.path.join(out, f"{mode or 'compile'}.{kind}") for kind in ("xml", "log"))
        for stale in (results, log):  # 이번 실행이 못 만들면 옛 결과가 통과처럼 보이는 것을 막는다
            if os.path.exists(stale):
                os.remove(stale)
        ok, report = judge(mode, runner(command(unity, root, out, mode, names)).returncode, results, log)
        lines += report
        if not ok:
            return False, lines
    return True, lines
