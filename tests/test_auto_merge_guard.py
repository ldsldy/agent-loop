"""자동 머지 가드 판정 자체 검사: python -I tests/test_auto_merge_guard.py (정책은 ShalomEng 고정물)"""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "templates", "github", "scripts"))
from auto_merge_guard import evaluate  # noqa: E402

POLICY = json.load(open(os.path.join(HERE, "fixture", ".github", "auto-merge-policy.json"), encoding="utf-8"))
T = "Assets/FaultTrain/UI/Tests/EditMode/FooTests.cs"
S = "Assets/FaultTrain/Scripts/Hotspots/HotspotProjection.cs"


def f(name, status="modified", **kw):
    return {"filename": name, "status": status, "patch": kw.pop("patch", "+x"), **kw}


def blocked(*files):
    return evaluate(list(files), POLICY)


class Guard(unittest.TestCase):
    def test_docs_and_internal_code_pass(self):
        self.assertEqual(blocked(f("README.md"), f("docs/a.md", "added"), f(S)), [])

    def test_new_test_with_meta_passes(self):
        self.assertEqual(blocked(f(T, "added"), f(T + ".meta", "added")), [])

    def test_existing_test_modified_or_removed_blocks(self):
        self.assertTrue(blocked(f(T, "modified")))
        self.assertTrue(blocked(f(T, "removed")))

    def test_disabling_marker_in_new_test_blocks(self):
        self.assertTrue(blocked(f(T, "added", patch="+[Ignore(\"x\")]")))

    def test_denied_paths_block(self):
        for p in ["Assets/FaultTrain/Scripts/Network/NetworkManager.cs", "Assets/FaultTrain/Scripts/Core/TrainSession.cs",
                  "Assets/FaultTrain/UI/Main.uxml", "Assets/FaultTrain/Scenes/Boot.unity", "Packages/manifest.json",
                  "ProjectSettings/ProjectSettings.asset", ".github/workflows/editmode-tests.yml", "CLAUDE.md",
                  "Assets/FaultTrain/Scripts/FaultTrain.asmdef"]:
            self.assertTrue(blocked(f(p)), p)

    def test_unlisted_file_blocks_by_default(self):
        self.assertTrue(blocked(f("tools/design_qa.py")))

    def test_rename_and_delete_block(self):
        self.assertTrue(blocked(f(S, "renamed", previous_filename="Assets/FaultTrain/Scripts/Hotspots/Old.cs")))
        self.assertTrue(blocked(f(S, "removed")))

    def test_meta_without_added_pair_blocks(self):
        self.assertTrue(blocked(f(S + ".meta", "modified")))
        self.assertTrue(blocked(f(T + ".meta", "added")))

    def test_one_bad_file_blocks_everything(self):
        self.assertTrue(blocked(f("README.md"), f("Packages/manifest.json")))

    def test_missing_patch_or_empty_is_unanalyzable(self):
        self.assertTrue(blocked(f(T, "added", patch=None)))
        self.assertTrue(blocked())


if __name__ == "__main__":
    unittest.main()
