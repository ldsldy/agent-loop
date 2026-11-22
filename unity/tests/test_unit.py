"""큰 단위 상태·다음 행동 규칙 자체 검사: python -I tests/test_unit.py (GitHub 없이 돈다)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["AGENT_LOOP_ROOT"] = os.path.join(HERE, "fixture")  # ShalomEng 값의 .agent-loop 고정물
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import unit as u  # noqa: E402


def piece(piece_id, depends_on=(), complexity="simple"):
    return {"id": piece_id, "title": piece_id, "issues": [114], "acceptance": ["a"], "files": ["f"],
            "complexity": complexity, "depends_on": list(depends_on), "kind": "keep", "guards": [], "retire": {}}


def unit_with(*pieces):
    state = u.new_unit({"number": 5, "title": "코드 정리"}, "loop/code-cleanup")
    return u.add_slices(state, list(pieces), improvement=False)


def actions(state, prs=None, live=()):
    return [a["action"] for a in u.next_actions(state, prs or {}, set(live), max_improvements=2)]


class NewUnitTests(unittest.TestCase):
    def test_loop_branch_must_be_named(self):
        with self.assertRaises(ValueError):
            u.new_unit({"number": 1, "title": "x"}, "task/x")
        self.assertEqual(u.task_branch(unit_with(piece("s1")), piece("s1")), "task/code-cleanup-s1")


class SliceErrorTests(unittest.TestCase):
    def test_missing_field_bad_complexity_duplicate_unknown_dep_and_cycle(self):
        self.assertTrue(u.slice_errors([{"id": "s1"}]))
        self.assertTrue(u.slice_errors([piece("s1", complexity="huge")]))

    def test_proof_contract_needs_a_kind_reasons_for_retired_tests_and_no_test_on_both_lists(self):
        self.assertEqual(u.slice_errors([{**piece("s1"), "kind": "change", "guards": ["a.cs"], "retire": {"b.cs": "옛 구조"}}]), [])
        self.assertTrue(u.slice_errors([{**piece("s1"), "kind": "refactor"}]))
        self.assertTrue(u.slice_errors([{**piece("s1"), "retire": {"b.cs": " "}}]))
        self.assertTrue(u.slice_errors([{**piece("s1"), "guards": ["a.cs"], "retire": {"a.cs": "옛 구조"}}]))
        self.assertTrue(u.slice_errors([{**piece("s1"), "retire": ["b.cs"]}]))
        self.assertEqual(u.slice_errors([{**piece("s1"), "allow": {"ILink": "외부 경계: 네트워크"}}]), [])
        self.assertTrue(u.slice_errors([{**piece("s1"), "allow": {"ILink": ""}}]))
        self.assertTrue(u.slice_errors([piece("s1"), piece("s1")]))
        self.assertTrue(u.slice_errors([piece("s1", ["s9"])]))
        self.assertIn("의존이 순환한다", u.slice_errors([piece("a", ["b"]), piece("b", ["a"])]))
        self.assertEqual(u.slice_errors([piece("a"), piece("b", ["a"])]), [])

    def test_appended_slices_may_depend_on_existing_ones_but_not_reuse_ids(self):
        existing = [piece("s1")]
        self.assertEqual(u.slice_errors([piece("s2", ["s1"])], existing), [])
        self.assertTrue(u.slice_errors([piece("s1")], existing))


class NextActionTests(unittest.TestCase):
    def test_no_slices_means_design_first(self):
        state = u.new_unit({"number": 5, "title": "x"}, "loop/x")
        self.assertEqual(actions(state), ["design"])

    def test_implement_only_slices_whose_dependencies_are_merged(self):
        state = unit_with(piece("s1"), piece("s2", ["s1"]))
        self.assertEqual(actions(state), ["implement"])
        merged = {"task/code-cleanup-s1": "MERGED"}
        result = u.next_actions(state, merged, set(), 2)
        self.assertEqual([(a["action"], a.get("slice")) for a in result], [("implement", "s2")])

    def test_open_pr_means_review_and_leftover_merged_branch_means_cleanup(self):
        state = unit_with(piece("s1"), piece("s2"))
        prs = {"task/code-cleanup-s1": "MERGED", "task/code-cleanup-s2": "OPEN"}
        self.assertEqual(actions(state, prs, live={"task/code-cleanup-s1"}), ["review", "cleanup"])

    def test_detailed_review_after_all_merged_then_improve_or_stop_or_notify(self):
        state = unit_with(piece("s1"))
        merged = {"task/code-cleanup-s1": "MERGED"}
        self.assertEqual(actions(state, merged), ["detailed-review"])
        u.record_detailed(state, "failed", "v.json")
        self.assertEqual(actions(state, merged), ["improve"])
        u.add_slices(state, [piece("fix1")], improvement=True)
        self.assertEqual(state["improvements"], 1)
        self.assertEqual(actions(state, merged), ["implement"])
        merged["task/code-cleanup-fix1"] = "MERGED"
        self.assertEqual(actions(state, merged), ["detailed-review"])
        again = u.next_actions(state, merged, set(), max_improvements=2)[0]
        self.assertEqual(again["previous"], "v.json", "재리뷰는 미달이던 리뷰에서 이어 간다")
        state["improvements"] = 2
        u.record_detailed(state, "failed", "v.json")
        self.assertEqual(actions(state, merged), ["stop"])
        u.record_detailed(state, "passed", "v.json")
        self.assertEqual(actions(state, merged), ["notify-user"])

    def test_implement_model_follows_complexity(self):
        state = unit_with(piece("hard", complexity="complex"), piece("easy"))
        found = {a["slice"]: a["model"] for a in u.next_actions(state, {}, set(), 2)}
        self.assertEqual(found["hard"], u.MODELS["implement"]["by_complexity"]["complex"])
        self.assertEqual(found["easy"], u.MODELS["implement"]["by_complexity"]["default"])


if __name__ == "__main__":
    unittest.main()
