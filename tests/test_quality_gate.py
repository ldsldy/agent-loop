"""quality_gate 판정 규칙 자체 검사: python -I tests/test_quality_gate.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["AGENT_LOOP_ROOT"] = os.path.join(HERE, "fixture")  # ShalomEng 값의 .agent-loop 고정물
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import quality_gate as qg  # noqa: E402


def review(grade=4, gate="pass", **overrides):
    data = {
        "schema": qg.SCHEMA,
        "ratings": {item: {"quant": grade, "qual": grade, "evidence": "f.cs:1"}
                    for group in qg.RUBRIC.values() for item, *_ in group},
        "gates": {g: {"status": gate, "evidence": "CI run 1"} for g, *_ in qg.GATES},
        "critical": 0, "criticalEvidence": "검토함", "penalty": 0,
    }
    data.update(overrides)
    return data


class JudgeTests(unittest.TestCase):
    def test_item_score_follows_the_60_40_formula(self):
        self.assertEqual(qg.item_score(20, {"quant": 4, "qual": 2}), 20 * (0.6 + 0.2))
        self.assertIsNone(qg.item_score(20, {"quant": 4}))

    def test_full_marks_with_evidence_and_gates_is_approved(self):
        self.assertEqual(qg.judge(review())["verdict"], "approved")

    def test_each_group_must_pass_on_its_own(self):
        data = review()
        for item, *_ in qg.RUBRIC["b"]:
            data["ratings"][item] = {"quant": 3, "qual": 2, "evidence": "x"}  # B = 65
        result = qg.judge(data)
        self.assertEqual(result["verdict"], "revise")
        self.assertEqual(result["a"]["score"], 100)

    def test_unchecked_gate_or_missing_rating_is_incomplete_not_passed(self):
        self.assertEqual(qg.judge(review(gate="unchecked"))["verdict"], "incomplete")
        data = review()
        del data["ratings"]["b2"]
        self.assertEqual(qg.judge(data)["verdict"], "incomplete")

    def test_na_is_allowed_only_on_marked_gates(self):
        data = review()
        data["gates"]["state"] = {"status": "na", "evidence": "네트워크 변경 없음"}
        self.assertEqual(qg.judge(data)["verdict"], "approved")
        data["gates"]["tests"] = {"status": "na", "evidence": "없음"}
        self.assertEqual(qg.judge(data)["verdict"], "incomplete")

    def test_failed_gate_or_critical_blocks_regardless_of_score(self):
        self.assertEqual(qg.judge(review(gate="fail"))["verdict"], "blocked")
        self.assertEqual(qg.judge(review(critical=1))["verdict"], "blocked")

    def test_penalty_lowers_a_and_needs_a_reason(self):
        self.assertEqual(qg.judge(review(penalty=5, penaltyReason="연쇄 래퍼"))["a"]["score"], 95)
        self.assertEqual(qg.judge(review(penalty=5))["verdict"], "incomplete")

    def test_other_schema_is_rejected(self):
        with self.assertRaises(ValueError):
            qg.judge(review(schema="v0"))

    def test_critical_review_is_required(self):
        self.assertEqual(qg.judge(review(criticalEvidence=""))["verdict"], "incomplete")
        self.assertEqual(qg.judge(review(critical=None))["verdict"], "incomplete")


def signed(name, sha="abc", **overrides):
    return {**review(**overrides), "reviewer": name, "git_sha": sha}


class JudgeAllTests(unittest.TestCase):
    def test_every_required_reviewer_must_pass_on_the_same_commit(self):
        reviews = [signed(name) for name in qg.REQUIRED_REVIEWERS]
        self.assertEqual(qg.judge_all(reviews, "abc")["verdict"], "approved")

    def test_missing_reviewer_or_other_commit_is_incomplete(self):
        self.assertEqual(qg.judge_all([signed("claude")], "abc")["verdict"], "incomplete")
        reviews = [signed("claude"), signed("codex", sha="old")]
        result = qg.judge_all(reviews, "abc")
        self.assertEqual(result["verdict"], "incomplete")
        self.assertTrue(any("old" in reason for reason in result["reasons"]))

    def test_one_low_reviewer_fails_the_gate_and_marks_disagreement(self):
        low = signed("codex")
        for item, *_ in qg.RUBRIC["b"]:
            low["ratings"][item] = {"quant": 3, "qual": 2, "evidence": "x"}
        result = qg.judge_all([signed("claude"), low], "abc")
        self.assertEqual(result["verdict"], "revise")
        self.assertTrue(result["disagreement"])

    def test_failed_ci_blocks_regardless_of_scores(self):
        reviews = [signed(name) for name in qg.REQUIRED_REVIEWERS]
        self.assertEqual(qg.judge_all(reviews, "abc", "failure")["verdict"], "blocked")

    def test_any_blocked_reviewer_blocks(self):
        result = qg.judge_all([signed("claude"), signed("codex", critical=1)], "abc")
        self.assertEqual(result["verdict"], "blocked")


class RubricTests(unittest.TestCase):
    def test_each_group_weighs_100(self):
        for group, items in qg.RUBRIC.items():
            self.assertEqual(sum(weight for _, _, weight, _ in items), 100, group)

    def test_review_schema_is_strict(self):
        def check(node):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for child in node["properties"].values():
                    check(child)
            if "items" in node:
                check(node["items"])
        check(qg.review_schema())
        self.assertIn("b1", qg.review_schema()["properties"]["ratings"]["properties"])
        self.assertIn("gates", qg.review_schema()["properties"])


def minimal(status="pass", sha="abc", **checks):
    data = {"schema": qg.SCHEMA, "reviewer": "claude", "git_sha": sha,
            "checks": {item: {"status": status, "evidence": "x"} for item, *_ in qg.MINIMAL_CHECKS}}
    for item, value in checks.items():
        data["checks"][item] = {"status": value, "evidence": "x"}
    return data


OK_MACHINE = {"hard_fails": []}


class JudgeMinimalTests(unittest.TestCase):
    def test_all_checks_pass_without_ci(self):
        self.assertEqual(qg.judge_minimal([minimal()], "abc", OK_MACHINE)["verdict"], "pass")

    def test_ci_result_is_not_part_of_the_minimal_verdict(self):
        # 조각 PR은 CI를 돌리지 않는다(2026-11-19). 실패한 CI 값이 들어와도 판정에 쓰지 않는다.
        self.assertEqual(qg.judge_minimal([minimal()], "abc", {"ci": "failure", "hard_fails": []})["verdict"], "pass")

    def test_any_failed_check_or_machine_fact_fails(self):
        self.assertEqual(qg.judge_minimal([minimal(m2="fail")], "abc", OK_MACHINE)["verdict"], "fail")
        self.assertEqual(qg.judge_minimal([minimal()], "abc", {"hard_fails": ["순환"]})["verdict"], "fail")

    def test_unknown_check_missing_reviewer_or_other_commit_is_incomplete(self):
        self.assertEqual(qg.judge_minimal([minimal(m1="unknown")], "abc", OK_MACHINE)["verdict"], "incomplete")
        self.assertEqual(qg.judge_minimal([], "abc", OK_MACHINE)["verdict"], "incomplete")
        self.assertEqual(qg.judge_minimal([minimal(sha="old")], "abc", OK_MACHINE)["verdict"], "incomplete")

    def test_minimal_schema_is_strict(self):
        schema = qg.minimal_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(sorted(schema["properties"]["checks"]["required"]), sorted(i for i, *_ in qg.MINIMAL_CHECKS))


if __name__ == "__main__":
    unittest.main()
