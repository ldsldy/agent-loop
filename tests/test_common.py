"""두 플러그인(unity, wpf)이 함께 쓰는 파일이 서로 같은지 검사한다: python -I tests/test_common.py

플러그인은 자기 폴더 밖의 파일을 못 써서 공통 파일을 양쪽에 복사해 둔다. 한쪽만 고치면 여기서 실패한다.
일부러 다르게 두는 파일(한쪽에만 있는 파일 포함)은 DIFFERENT에 적는다.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = ("unity", "wpf")
DIFFERENT = {
    ".claude-plugin/plugin.json",
    "scripts/unity_tests.py", "scripts/dotnet_tests.py",  # 스택마다 테스트를 돌리는 방법
    "scripts/slice_check.py", "scripts/shape.py", "scripts/audit_tests.py", "scripts/review.py",
    "skills/loop/SKILL.md", "skills/loop-init/SKILL.md", "skills/loop-refactor/SKILL.md",
    "skills/loop-review/SKILL.md", "skills/loop-test-audit/SKILL.md",
    "templates/agent-loop/config.json",
    "tests/test_slice_check.py", "tests/test_shape.py", "tests/test_audit_tests.py", "tests/test_review.py",
}


def files(plugin):
    found = {}
    for folder, folders, names in os.walk(os.path.join(ROOT, plugin)):
        folders[:] = [name for name in folders if name != "__pycache__"]
        for name in names:
            path = os.path.join(folder, name)
            with open(path, "rb") as file:
                found[os.path.relpath(path, os.path.join(ROOT, plugin)).replace("\\", "/")] = file.read().replace(b"\r\n", b"\n")
    return found


class CommonFileTests(unittest.TestCase):
    def test_every_file_not_listed_as_different_is_the_same_in_both_plugins(self):
        first, second = (files(plugin) for plugin in PLUGINS)
        drifted = sorted(path for path in set(first) | set(second) if path not in DIFFERENT and first.get(path) != second.get(path))
        self.assertEqual(drifted, [], "공통 파일이 한쪽에만 있거나 내용이 다르다. 양쪽을 같게 고치거나 DIFFERENT에 적는다")

    def test_the_different_list_names_only_files_that_exist(self):
        present = set().union(*(files(plugin) for plugin in PLUGINS))
        self.assertEqual(sorted(DIFFERENT - present), [])


if __name__ == "__main__":
    unittest.main()
