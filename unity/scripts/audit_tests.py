"""테스트 점검: 테스트가 붙잡고 있는 구조와, 확인하는 것이 없는 테스트를 기계로 찾는다. Unity를 띄우지 않고 토큰도 들지 않는다.

    python -I audit_tests.py [--json]

찾는 것은 후보다. 무엇을 지우거나 고칠지는 spec과 대조해 정한다(loop-test-audit 스킬). 코드를 고치지 않는다.
- 테스트만 쓰는 타입: 테스트 아닌 코드 어디서도 쓰지 않는데 테스트가 참조하는 타입. 테스트 때문에 남은 구조일 수 있다.
- 테스트 가짜를 위한 인터페이스: 실제 구현은 하나뿐이고 나머지 구현이 테스트 안에 있는 인터페이스.
- 안을 들여다보는 테스트: 리플렉션으로 비공개 멤버를 읽는 테스트 파일. 내부 구조가 바뀌면 깨진다.
- 확인하는 것이 없는 테스트: 단언이 하나도 없는 테스트 메서드. 단언하는 도우미를 부르면 단언으로 본다.
- 꺼 둔 테스트: [Ignore]·[Explicit].
"""
import collections
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shape  # noqa: E402

# Unity가 이름이나 속성으로 찾아 쓰는 타입. 코드에서 참조가 없어도 쓰이는 것이라 "테스트만 쓴다"고 보지 않는다.
ENGINE_ENTRY = re.compile(r"\[\s*(?:UxmlElement|InitializeOnLoad|RuntimeInitializeOnLoadMethod|MenuItem|CustomEditor|CreateAssetMenu)\b")
ENGINE_BASES = shape.UNITY_OBJECTS | {"AssetPostprocessor", "VisualElement", "PropertyDrawer"}
PEEK = re.compile(r"BindingFlags\.NonPublic|\bGetField\s*\(|\bGetMethod\s*\(|\bGetProperty\s*\(|\bFieldInfo\b|\bMethodInfo\b|\bPrivateObject\b")
TEST_METHOD = re.compile(r"\[\s*(?:Test|UnityTest|TestCase|TestCaseSource)\b[^\]]*\](?:\s*\[[^\]]*\])*\s*(?:public|private|internal|protected)?\s*(?:static\s+)?(?:async\s+)?[\w<>\[\],.?]+\s+(\w+)\s*\(")
# 단언으로 보는 것: Assert·Expect·Should·Verify·Throws가 든 이름(AssertStep 같은 도우미 포함)과 NUnit 제약(Is.·Does.·Has.)
ASSERTS = re.compile(r"\b\w*(?:Assert|Expect|Should|Verify|Throws)\w*\b|\bIs\.|\bDoes\.|\bHas\.|\bReceived\s*\(")
METHOD = re.compile(r"(?:^|[;{}\]])\s*(?:(?:public|private|internal|protected|static|async|override|virtual)\s+)*[\w<>\[\],.?()]+\s+(\w+)\s*\([^;{}]*\)\s*(?=\{|=>)", re.M)
OFF = re.compile(r"(?:\[|,)\s*(Ignore|Explicit)\b")


def method_body(text, start):
    """start(메서드 이름 뒤의 여는 괄호 자리)부터 그 메서드의 본문. 식 본문(=>)은 그 문장까지다."""
    brace, arrow = text.find("{", start), text.find("=>", start)
    if arrow != -1 and (brace == -1 or arrow < brace):
        return text[arrow:text.find(";", arrow) + 1]
    depth = 0
    for position in range(brace, len(text)):
        depth += {"{": 1, "}": -1}.get(text[position], 0)
        if depth == 0:
            return text[brace:position + 1]
    return text[brace:]


def asserting_helpers(texts):
    """단언을 하는 메서드 이름들. 단언하는 메서드를 부르는 메서드도 단언하는 것으로 본다(도우미를 거친 확인)."""
    bodies = {}
    for text in texts:
        for match in METHOD.finditer(text):
            bodies.setdefault(match.group(1), []).append(method_body(text, match.end() - 1))
    helpers = {name for name, found in bodies.items() if any(ASSERTS.search(body) for body in found)}
    while True:
        calls = re.compile(r"\b(?:" + "|".join(map(re.escape, helpers)) + r")\s*\(") if helpers else None
        more = {name for name, found in bodies.items() if name not in helpers and calls and any(calls.search(body) for body in found)}
        if not more:
            return helpers
        helpers |= more


def audit(code, tests):
    """code·tests = {경로: 본문} → {목록 이름: [(대상, 설명)]}."""
    project = shape.Project(code)
    test_text = {path: shape.clean(text) for path, text in tests.items()}
    test_types = [entry for path, text in tests.items() for entry in shape.parse(path, text)]
    found = collections.defaultdict(list)

    for entry in project.types:
        name = entry["name"]
        if not entry["top"] or ENGINE_ENTRY.search(code[entry["path"]]):
            continue
        if entry["kind"] == "class" and ({project.engine_root(name), project.parent.get(name)} & ENGINE_BASES):
            continue
        word = re.compile(rf"\b{re.escape(name)}\b")
        own_file_uses = len(word.findall(project.text[entry["path"]])) > 1  # 선언 말고 같은 파일에서 또 쓰는가
        in_tests = [path for path, text in test_text.items() if word.search(text)]
        if in_tests and not project.users(entry) and not own_file_uses:
            found["테스트만 쓰는 타입"].append((name, f"{entry['path']}. 테스트 {len(in_tests)}개 파일만 쓴다: {', '.join(os.path.basename(p) for p in in_tests[:3])}"))
        if entry["kind"] == "interface":
            fakes = [other["name"] for other in test_types if name in other["bases"]]
            if fakes and project.implementations[name] <= 1:
                found["테스트 가짜를 위한 인터페이스"].append((name, f"실제 구현 {project.implementations[name]}개, 테스트 구현 {', '.join(fakes[:3])}. 외부 경계가 아니면 진짜 객체로 테스트한다"))

    helpers = asserting_helpers(test_text.values())
    checks = re.compile(r"\b(?:" + "|".join(map(re.escape, helpers)) + r")\s*\(") if helpers else None
    for path, text in test_text.items():
        peeks = PEEK.findall(text)
        if peeks:
            found["안을 들여다보는 테스트"].append((path, f"리플렉션 {len(peeks)}곳({', '.join(dict.fromkeys(p.strip('( ') for p in peeks))}). 공개된 입구로 확인하도록 고치거나 물린다"))
        for match in TEST_METHOD.finditer(text):
            body = method_body(text, match.end())
            if not ASSERTS.search(body) and not (checks and checks.search(body)):
                found["확인하는 것이 없는 테스트"].append((f"{os.path.basename(path)}:{match.group(1)}", "단언이 없다. 예외가 안 나는 것만 확인한다면 그 뜻을 단언으로 적는다"))
        for kind in OFF.findall(text):
            found["꺼 둔 테스트"].append((path, f"[{kind}]"))
    return dict(found)


def count(tests):
    """테스트의 크기: 파일 수와 테스트 메서드 수."""
    return {"files": len(tests), "tests": sum(len(TEST_METHOD.findall(shape.clean(text))) for text in tests.values())}


def tests_now(root, is_test):
    listed = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files", "*.cs"], cwd=root, capture_output=True, text=True, encoding="utf-8").stdout.split("\n")
    files = {}
    for path in (path for path in listed if path and is_test(path)):
        with open(os.path.join(root, path), encoding="utf-8-sig", errors="replace") as file:
            files[path] = file.read()
    return files


def main(argv):
    from loopconfig import CONFIG, ROOT
    from scope import is_test
    if argv not in ([], ["--json"]):
        sys.exit("사용법: python -I audit_tests.py [--json]")
    if "shape" not in CONFIG:
        sys.exit(".agent-loop/config.json에 shape 설정이 없습니다(코드 폴더 roots가 필요합니다).")
    tests = tests_now(ROOT, is_test)
    found = audit(shape.code_now(ROOT, CONFIG["shape"]["roots"], is_test), tests)
    if argv:
        print(json.dumps({"count": count(tests), "found": found}, ensure_ascii=False, indent=1))
        return 0
    size = count(tests)
    print(f"테스트 파일 {size['files']}개, 테스트 {size['tests']}개")
    for name, items in found.items():
        print(f"## {name} ({len(items)}건)")
        print("\n".join(f"- {subject}: {why}" for subject, why in items))
    print(f"모두 {sum(len(items) for items in found.values())}건")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
