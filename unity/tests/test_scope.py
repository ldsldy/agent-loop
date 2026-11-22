"""변경 경로 판정 자체 검사: python -I tests/test_scope.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["AGENT_LOOP_ROOT"] = os.path.join(HERE, "fixture")  # ShalomEng 값의 .agent-loop 고정물
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
from scope import tests_only, touches_review  # noqa: E402


class TouchesReviewTests(unittest.TestCase):
    def test_unity_inputs_and_the_test_workflow_count(self):
        for path in ("Assets/FaultTrain/Scripts/A.cs", "Packages/manifest.json", "ProjectSettings/ProjectSettings.asset",
                     ".github/workflows/editmode-tests.yml\n"):
            self.assertTrue(touches_review([path]), path)

    def test_tools_docs_and_agent_settings_do_not(self):
        self.assertFalse(touches_review(["tools/quality/review.py", "docs/agents/design-qa.md", ".claude/skills/x/SKILL.md",
                                        "CLAUDE.md", ".gitignore", ".github/workflows/auto-merge-guard.yml", "AssetsNote.md"]))

    def test_one_unity_file_among_others_is_enough(self):
        self.assertTrue(touches_review(["docs/a.md", "Assets/FaultTrain/UI/Uxml/Main.uxml"]))


class TestsOnlyTests(unittest.TestCase):
    def test_only_test_files_count_as_tests_only(self):
        self.assertTrue(tests_only(["Assets/FaultTrain/UI/Tests/EditMode/A.cs\n", "Assets/FaultTrain/UI/Tests/PlayMode/B.cs"]))

    def test_one_non_test_file_or_an_empty_list_is_not_tests_only(self):
        self.assertFalse(tests_only(["Assets/FaultTrain/UI/Tests/A.cs", "Assets/FaultTrain/Scripts/B.cs"]))
        self.assertFalse(tests_only([]))
        self.assertFalse(tests_only(["\n"]))


if __name__ == "__main__":
    unittest.main()
