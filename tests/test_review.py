"""리뷰 자료 구성 자체 검사: python -I tests/test_review.py"""
import contextlib
import io
import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["AGENT_LOOP_ROOT"] = os.path.join(HERE, "fixture")  # ShalomEng 값의 .agent-loop 고정물
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import review as rv  # noqa: E402

RESULTS = """<test-run result="Passed" total="3" passed="3" failed="0" skipped="0">
  <test-suite type="Assembly" name="FaultTrain.Tests.dll" total="1" passed="1" failed="0" skipped="0">
    <test-suite type="TestSuite" name="FaultTrain">
      <test-suite type="TestFixture" name="UiTests" total="1" passed="1" failed="0" skipped="0" />
    </test-suite>
  </test-suite>
  <test-suite type="Assembly" name="Pkg.Tests.dll" total="2" passed="2" failed="0" skipped="0">
    <test-suite type="TestFixture" name="SecurityTests" total="2" passed="2" failed="0" skipped="0" />
  </test-suite>
</test-run>"""

class ReviewMaterialTests(unittest.TestCase):
    def test_summary_lists_every_assembly_and_fixture_with_counts(self):
        text = rv.test_summary(ET.fromstring(RESULTS))
        self.assertEqual(text.splitlines(), [
            "Passed 통과 3/3, 실패 0, 건너뜀 0",
            "- FaultTrain.Tests.dll: 통과 1/1, 실패 0, 건너뜀 0",
            "  - UiTests: 통과 1/1, 실패 0, 건너뜀 0",
            "- Pkg.Tests.dll: 통과 2/2, 실패 0, 건너뜀 0",
            "  - SecurityTests: 통과 2/2, 실패 0, 건너뜀 0",
        ])

    def test_latest_artifact_picks_the_newest_attempt_of_that_name(self):
        artifacts = [
            {"name": "unity-test-results", "created_at": "2026-11-17T08:00:00Z", "id": 1},
            {"name": "unity-test-results", "created_at": "2026-11-17T09:00:00Z", "id": 2},
            {"name": "unity-test-results", "created_at": "2026-11-17T10:00:00Z", "id": 3, "expired": True},
            {"name": "other", "created_at": "2026-11-17T11:00:00Z", "id": 4},
        ]
        self.assertEqual(rv.latest_artifact(artifacts, "unity-test-results")["id"], 2)
        self.assertIsNone(rv.latest_artifact(artifacts, "missing"))

    @staticmethod
    @contextlib.contextmanager
    def repo_with_changes():
        """임시 저장소: base 커밋 뒤에 코드·데이터·패키지·설정을 바꾸고 파일 하나는 이름을 바꾼다. (base, head)를 준다."""
        import subprocess
        import tempfile

        def run(*args):
            return subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True, text=True).stdout.strip()

        def write(path, text):
            os.makedirs(os.path.dirname(os.path.join(tmp, path)), exist_ok=True)
            with open(os.path.join(tmp, path), "w", encoding="utf-8") as f:
                f.write(text)

        with tempfile.TemporaryDirectory() as tmp:
            run("init", "-q")
            run("config", "user.email", "t@example.com")
            run("config", "user.name", "t")
            for path in ("Assets/A.cs", "Assets/Data/T.asset", "Packages/manifest.json", "ProjectSettings/ProjectSettings.asset",
                         "Assets/Old.uss", "Assets/UI/Tests/EditMode/FooTests.cs", "tools/x.py", "docs/X.md"):
                write(path, "before\n" * 3)
            run("add", "-A")
            run("commit", "-qm", "base")
            base = run("rev-parse", "HEAD")
            for path in ("Assets/A.cs", "Assets/Data/T.asset", "Packages/manifest.json", "ProjectSettings/ProjectSettings.asset",
                         "Assets/UI/Tests/EditMode/FooTests.cs", "tools/x.py", "docs/X.md"):
                write(path, "after-" + os.path.basename(path) + "\n")
            run("mv", "Assets/Old.uss", "Assets/New.uss")
            run("add", "-A")
            run("commit", "-qm", "change")
            original, rv.ROOT = rv.ROOT, tmp
            try:
                yield base, run("rev-parse", "HEAD")
            finally:
                rv.ROOT = original

    def evidence_with(self, *commands):
        original = rv.CONFIG.get("evidence")
        rv.CONFIG["evidence"] = list(commands)
        try:
            with self.repo_with_changes() as (base, head):
                return rv.evidence_part(base, head), base, head
        finally:
            rv.CONFIG["evidence"] = original

    def test_evidence_commands_get_base_sha_and_tree_and_json_hard_fails_count(self):
        text_cmd = {"title": "텍스트", "cmd": ["python", "-c", "import sys;print('base='+sys.argv[1]+' sha='+sys.argv[2])", "{base}", "{sha}"]}
        tree_cmd = {"title": "트리", "cmd": ["python", "-c",
                                            "import json,os,sys;print(json.dumps({'cs': os.path.exists(os.path.join(sys.argv[1],'Assets','A.cs')),"
                                            "'hard_fails': ['순환']}))", "{tree}"]}
        (parts, hard_fails), base, head = self.evidence_with(text_cmd, tree_cmd)
        self.assertEqual(parts[0], f"### 텍스트\nbase={base} sha={head}")
        self.assertIn('"cs": true', parts[1], "the tree is the target commit")
        self.assertEqual(hard_fails, ["트리: 순환"])

    def test_failing_evidence_command_stops_the_review(self):
        with self.assertRaises(SystemExit):
            self.evidence_with({"title": "실패", "cmd": ["python", "-c", "raise SystemExit(3)"]})

    def test_no_evidence_commands_means_no_parts(self):
        self.assertEqual(self.evidence_with()[0], ([], []))

    def test_detailed_diff_keeps_code_packages_and_settings_but_only_stats_for_data_files(self):
        with self.repo_with_changes() as (base, head):
            text = "\n".join(rv.diff_part(base, head))
        self.assertIn("+after-A.cs", text)
        self.assertIn("+after-manifest.json", text, "Packages/ is shown in full")
        self.assertIn("+after-ProjectSettings.asset", text, "ProjectSettings/ is shown in full")
        self.assertNotIn("+after-T.asset", text, "data files show only stats")
        self.assertIn("Assets/Data/T.asset", text)

    def test_detailed_diff_shows_only_stats_for_tests_tools_and_docs(self):
        with self.repo_with_changes() as (base, head):
            text = "\n".join(rv.diff_part(base, head))
        for name in ("FooTests.cs", "x.py", "X.md"):
            self.assertNotIn("+after-" + name, text, f"{name} shows only stats")
        self.assertIn("Assets/UI/Tests/EditMode/FooTests.cs", text)
        self.assertIn("docs/X.md", text)

    def test_changed_paths_include_both_sides_of_a_rename(self):
        with self.repo_with_changes() as (base, head):
            changed = rv.changed_paths(base, head)
        self.assertIn("Assets/Old.uss", changed)
        self.assertIn("Assets/New.uss", changed)
        self.assertIn("Packages/manifest.json", changed)

    def test_detailed_review_without_spec_is_refused_before_anything_runs(self):
        for argv in (["--level", "detailed", "--base", "origin/develop"], ["--level", "detailed", "--base", "origin/develop", "--spec"]):
            with self.assertRaises(SystemExit) as stop, contextlib.redirect_stderr(io.StringIO()):
                rv.main(argv)
            self.assertEqual(stop.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
