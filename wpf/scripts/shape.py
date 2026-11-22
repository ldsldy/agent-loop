"""모양 검사: 조각이 새로 만든 C# 파일·타입이 파일·타입·상속·폴더 규칙을 지키는지 본다. 빌드하지 않는다.

    python -I shape.py report --base <rev>    # 루프 끝 보고: 그 커밋 뒤로 파일·타입·폴더가 어떻게 달라졌는지
    python -I shape.py audit [--json]         # 프로젝트 전체를 새 코드로 보고 규칙을 건다. 리팩터링할 곳을 찾을 때 쓴다(loop-refactor 스킬)

조각 검사(slice_check.py)가 빌드하기 전에 부른다. 규칙은 이 조각이 새로 만든 파일과 타입에만 건다(옛 코드는 정리 루프가 맞춘다).
어긴 대상이 조각의 allow({대상: 이유})에 있으면 통과시키고 기록에 남긴다. 대상은 타입 이름, 타입.멤버, 파일 경로, 폴더 이름이다.
설정은 .agent-loop/config.json의 shape: {"roots": [코드 폴더], "max_depth": 3, "folder_names": [쓸 수 있는 폴더 이름]}.
shape가 없으면 검사하지 않는다. folder_names가 없으면 폴더 이름은 보지 않는다.

규칙(근거는 DESIGN.md):
- 새 .cs 파일은 설계가 조각의 files에 적은 것만 만든다.
- enum·struct·delegate 하나만을 위한 파일을 만들지 않는다. 여러 곳이 쓰면 영역의 타입 모음 파일에, 한 클래스만 쓰면 그 클래스 파일에 둔다.
- 인터페이스는 서로 다른 구현이 지금 둘 이상일 때만 만든다.
- 베이스 클래스는 지금 자식이 둘 이상이다. 프로젝트 안 상속 깊이는 max_depth까지다. abstract 클래스 이름은 …Base로 끝난다.
- virtual·abstract 멤버는 재정의하는 곳이 있어야 한다. 상속받지 않는 클래스는 sealed로 닫는다.
- 새 파일의 폴더는 max_depth단 이하이고 이름은 folder_names 안이다.
"""
import collections
import json
import os
import re
import subprocess
import sys

STRIP = re.compile(r'//[^\n]*|/\*.*?\*/|@"(?:[^"]|"")*"|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])+\'', re.S)
MODS = r"(?:(?:public|internal|private|protected|sealed|abstract|static|partial|readonly|ref|new|unsafe)\s+)*"
TYPE = re.compile(rf"^\s*(?:\[[^\]]*\]\s*)*({MODS})(class|struct|interface|enum|record)\s+(\w+)\s*(?:<[^>]*>)?\s*(?::\s*([^{{]*?))?\s*(?:where\b[^{{]*)?(?:\{{.*)?$")
DELEGATE = re.compile(rf"^\s*(?:\[[^\]]*\]\s*)*({MODS})delegate\s+[\w<>\[\],.?]+(?:\s+[\w<>\[\],.?]+)*?\s+(\w+)\s*(?:<[^>]*>)?\s*\(")
NAMESPACE = re.compile(r"^\s*namespace\s+[\w.]+\s*(;?)")
MEMBER = r"[\w<>\[\],.?]+(?:\s+[\w<>\[\],.?]+)*?\s+(\w+)\s*(?:\(|\{|;|=>)"
VIRTUAL = re.compile(rf"\b(?:virtual|abstract)\s+(?!class\b|partial\b){MEMBER}")
OVERRIDE = re.compile(rf"\boverride\s+{MEMBER}")
SMALL_TYPES = ("enum", "struct", "delegate")
SMALL_FILE = 30  # 코드 줄 수. 보고에서 "작은 파일"로 세는 기준


def clean(text):
    """주석과 문자열을 지운다(줄 수는 그대로). 그 안의 중괄호·낱말이 선언으로 읽히지 않게 한다."""
    return STRIP.sub(lambda match: "\n" * match.group().count("\n"), text)


def parse(path, text):
    """파일 하나의 타입 선언들: kind·name·mods·bases·top(최상위인가)·path·body(본문)."""
    found, opened, depth, top_level = [], [], 0, 0
    for line in clean(text).split("\n"):
        namespace = NAMESPACE.match(line)
        if namespace and not namespace.group(1):
            top_level = depth + 1  # 블록 namespace 안의 한 단계가 최상위다
        declared = TYPE.match(line)
        delegate = None if declared else DELEGATE.match(line)
        if declared:
            bases = [re.sub(r"<.*", "", base).strip().split(".")[-1] for base in (declared.group(4) or "").split(",") if base.strip()]
            entry = {"kind": declared.group(2), "name": declared.group(3), "mods": declared.group(1).split(), "bases": bases,
                     "top": depth == top_level, "path": path, "body": [], "at": depth, "seen_brace": False}
            found.append(entry)
            opened.append(entry)
        elif delegate:
            found.append({"kind": "delegate", "name": delegate.group(2), "mods": delegate.group(1).split(), "bases": [],
                          "top": depth == top_level, "path": path, "body": []})
        for entry in opened:
            entry["body"].append(line)
            entry["seen_brace"] = entry["seen_brace"] or "{" in line
        depth += line.count("{") - line.count("}")
        opened = [entry for entry in opened if not (entry["seen_brace"] and depth <= entry["at"])]
    for entry in found:
        entry["body"] = "\n".join(entry["body"])
    return found


class Project:
    """코드 전체의 타입 관계. files = {경로: 본문}(테스트 제외)."""

    def __init__(self, files):
        self.text = {path: clean(text) for path, text in files.items()}
        self.types = [entry for path, text in files.items() for entry in parse(path, text)]
        self.by_name = {entry["name"]: entry for entry in self.types}
        interfaces = {entry["name"] for entry in self.types if entry["kind"] == "interface"}
        self.parent, self.children, self.implementations = {}, collections.Counter(), collections.Counter()
        for entry in self.types:
            if entry["kind"] not in ("class", "struct", "record"):
                continue
            for position, base in enumerate(entry["bases"]):
                external_interface = base not in self.by_name and re.match(r"I[A-Z]", base)
                if base in interfaces or external_interface:
                    self.implementations[base] += 1
                elif position == 0 and entry["kind"] != "struct":
                    self.parent[entry["name"]] = base
                    self.children[base] += 1
        self.overrides = {name for text in self.text.values() for name in OVERRIDE.findall(text)}

    def ancestors(self, name):
        """프로젝트 안의 조상 타입들(가까운 것부터)."""
        chain = []
        while self.parent.get(name) in self.by_name and self.parent[name] not in chain:
            name = self.parent[name]
            chain.append(self.by_name[name])
        return chain

    def users(self, entry):
        """이 타입 이름이 나오는 다른 파일들."""
        word = re.compile(rf"\b{re.escape(entry['name'])}\b")
        return [path for path, text in self.text.items() if path != entry["path"] and word.search(text)]


def type_rules(project, entry, max_depth):
    """새 타입 하나가 어긴 규칙들: (규칙, 대상, 설명)."""
    name, kind, mods = entry["name"], entry["kind"], entry["mods"]
    if kind == "interface":
        count = project.implementations[name]
        if count < 2:
            yield "인터페이스", name, f"구현이 {count}개다. 서로 다른 구현이 지금 둘 이상일 때만 만든다. 외부 경계(네트워크·파일·시간)면 조각의 allow에 이유를 적는다"
    if kind in SMALL_TYPES and entry["top"]:
        home = [other for other in project.types if other["path"] == entry["path"] and other["top"]]
        users = project.users(entry)
        if all(other["kind"] in SMALL_TYPES for other in home) and len(users) == 1:
            yield "한 곳만 쓰는 타입", name, f"쓰는 곳이 {users[0]} 하나뿐이다. 그 파일에 함께 둔다"
    if kind != "class":
        return
    children = project.children[name]
    if "abstract" in mods and not name.endswith("Base"):
        yield "베이스 이름", name, "abstract 클래스 이름은 …Base로 끝낸다"
    if ("abstract" in mods or children == 1) and children < 2:
        yield "베이스 클래스", name, f"자식이 {children}개다. 같은 것의 변형이 지금 둘 이상일 때만 베이스를 만든다"
    chain = project.ancestors(name)
    if len(chain) + 1 > max_depth:
        yield "상속 깊이", name, f"{' → '.join(reversed([name] + [a['name'] for a in chain]))}: 프로젝트 안 {len(chain) + 1}단이다(한도 {max_depth}단)"
    if not children and not {"sealed", "static", "abstract"} & set(mods):
        yield "닫지 않은 클래스", name, "상속받지 않는 클래스는 sealed로 닫는다"
    for member in dict.fromkeys(VIRTUAL.findall(entry["body"])):
        if member not in project.overrides:
            yield "쓰이지 않는 확장점", f"{name}.{member}", "virtual·abstract인데 재정의하는 곳이 없다"


def check(files, before, added, declared, allow, config):
    """조각의 모양 위반과 허용된 예외 → ([(규칙, 대상, 설명)], [(규칙, 대상, 이유)]).
    files는 지금 코드 전체, before는 고친 파일의 이전 본문, added는 새 파일, declared는 조각의 files(없으면 None)."""
    project, max_depth = Project(files), config.get("max_depth", 3)
    found = []
    old_names = {path: {entry["name"] for entry in parse(path, text)} for path, text in before.items()}
    for entry in project.types:
        if entry["path"] in added or (entry["path"] in old_names and entry["name"] not in old_names[entry["path"]]):
            found += list(type_rules(project, entry, max_depth))
    for path in sorted(added):
        if declared is not None and path not in declared:
            found.append(("새 파일", path, "설계가 조각의 files에 적지 않은 새 파일이다. 있는 파일에 넣거나, 필요하면 멈추고 판정을 받는다"))
        tops = [entry for entry in project.types if entry["path"] == path and entry["top"]]
        if len(tops) == 1 and tops[0]["kind"] in SMALL_TYPES:
            found.append(("타입 하나만을 위한 파일", path, f"{tops[0]['kind']} {tops[0]['name']} 하나만 들었다. 여러 곳이 쓰면 영역의 타입 모음 파일에, 한 클래스만 쓰면 그 클래스 파일에 둔다"))
        root = next((r.rstrip("/") + "/" for r in config.get("roots", []) if path.startswith(r.rstrip("/") + "/")), None)
        folders = path[len(root):].split("/")[:-1] if root else []
        if len(folders) > max_depth:
            found.append(("폴더 깊이", path, f"{'/'.join(folders)}: {len(folders)}단이다(한도 {max_depth}단)"))
        for folder in folders if config.get("folder_names") else ():
            if folder not in config["folder_names"]:
                found.append(("폴더 이름", folder, f"정해 둔 폴더 이름이 아니다({path})"))
    found = list(dict.fromkeys(found))
    return ([item for item in found if item[1] not in allow],
            [(rule, subject, allow[subject]) for rule, subject, _ in found if subject in allow])


def measure(files):
    """보고용 숫자: 파일 수, 종류별 타입 수, 작은 파일 수, 폴더들."""
    kinds = collections.Counter(entry["kind"] for path, text in files.items() for entry in parse(path, text))
    small = sum(1 for text in files.values() if sum(1 for line in clean(text).split("\n") if line.strip()) <= SMALL_FILE)
    return {"files": len(files), "types": dict(kinds), "small": small, "folders": sorted({os.path.dirname(path) for path in files})}


# ---- 파일 읽기 ----

def read_under(root, roots, ending, keep=lambda path: True):
    """roots 아래에서 이름이 ending으로 끝나는 파일들 → {경로: 본문}."""
    files = {}
    for top in roots:
        for folder, folders, names in os.walk(os.path.join(root, top)):
            folders[:] = [name for name in folders if name not in ("bin", "obj")]  # 빌드 산출물(생성된 .g.cs 포함)은 보지 않는다
            for name in names:
                path = os.path.relpath(os.path.join(folder, name), root).replace("\\", "/")
                if name.endswith(ending) and keep(path):
                    with open(os.path.join(folder, name), encoding="utf-8-sig", errors="replace") as file:
                        files[path] = file.read()
    return files


def code_now(root, roots, is_test):
    return read_under(root, roots, ".cs", lambda path: not is_test(path))


def code_at(root, rev, paths):
    """그 커밋의 파일 본문들. 없는 파일은 뺀다."""
    files = {}
    for path in paths:
        done = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=root, capture_output=True)
        if done.returncode == 0:
            files[path] = done.stdout.decode("utf-8-sig", errors="replace")
    return files


def slice_report(root, fork, rows, piece, config, is_test):
    """조각 검사가 부른다 → (위반 줄들, 허용된 예외 줄들). rows는 갈라진 지점부터 바뀐 (상태, 경로)."""
    def code(path):
        return path.endswith(".cs") and not is_test(path) and any(path.startswith(r.rstrip("/") + "/") for r in config["roots"])
    added = {path for status, path in rows if status == "A" and code(path)}
    before = code_at(root, fork, [path for status, path in rows if status == "M" and code(path)])
    violations, allowed = check(code_now(root, config["roots"], is_test), before, added,
                                piece["files"] if "files" in piece else None, piece.get("allow", {}), config)
    return ([f"- [{rule}] {subject}: {why}" for rule, subject, why in violations],
            [f"{subject}({rule}: {why})" for rule, subject, why in allowed])


def report(root, base, config, is_test):
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", base, "--", *config["roots"]], cwd=root, capture_output=True, text=True, encoding="utf-8").stdout.split("\n")
    old = measure(code_at(root, base, [path for path in listed if path.endswith(".cs") and not is_test(path)]))
    new = measure(code_now(root, config["roots"], is_test))
    kinds = sorted(set(old["types"]) | set(new["types"]))
    lines = [f"파일 {old['files']} → {new['files']}, {SMALL_FILE}줄 이하 파일 {old['small']} → {new['small']}",
             "타입 " + ", ".join(f"{kind} {old['types'].get(kind, 0)} → {new['types'].get(kind, 0)}" for kind in kinds)]
    lines += [f"새 폴더: {folder}" for folder in new["folders"] if folder not in old["folders"]]
    lines += [f"없어진 폴더: {folder}" for folder in old["folders"] if folder not in new["folders"]]
    return lines


def audit(files, config):
    """코드 전체를 새 코드로 보고 모양 규칙을 건다 → {규칙: [(대상, 설명)]}. 새 파일 선언 규칙은 걸지 않는다(조각이 없다)."""
    violations, _ = check(files, {}, set(files), None, {}, config)
    grouped = collections.defaultdict(list)
    for rule, subject, why in violations:
        grouped[rule].append((subject, why))
    return dict(sorted(grouped.items(), key=lambda item: -len(item[1])))


def main(argv):
    from loopconfig import CONFIG, ROOT
    from scope import is_test
    if "shape" not in CONFIG:
        sys.exit(".agent-loop/config.json에 shape 설정이 없습니다.")
    if argv in (["audit"], ["audit", "--json"]):
        found = audit(code_now(ROOT, CONFIG["shape"]["roots"], is_test), CONFIG["shape"])
        if "--json" in argv:
            print(json.dumps(found, ensure_ascii=False, indent=1))
        else:
            for rule, items in found.items():
                print(f"## {rule} ({len(items)}건)")
                print("\n".join(f"- {subject}: {why}" for subject, why in items))
            print(f"모두 {sum(len(items) for items in found.values())}건")
        return 0
    if len(argv) != 3 or argv[:2] != ["report", "--base"]:
        sys.exit("사용법: python -I shape.py report --base <rev> | audit [--json]")
    print("\n".join(report(ROOT, argv[2], CONFIG["shape"], is_test)))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main(sys.argv[1:]))
