"""models.json 모양 자체 검사: python -I tests/test_models.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["AGENT_LOOP_ROOT"] = os.path.join(HERE, "fixture")  # ShalomEng 값의 .agent-loop 고정물
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
from loopconfig import MODELS, effort  # noqa: E402

EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class AgentModelsTests(unittest.TestCase):
    def test_every_role_has_a_model_and_a_valid_effort(self):
        self.assertIn(MODELS["default_effort"], EFFORTS)
        sections = [MODELS["orchestrator"], MODELS["design"], MODELS["implement"], MODELS["review"]["minimal"]["subagent"],
                    *(r for level in MODELS["review"].values() for r in level["reviewers"].values())]
        for section in sections:
            self.assertIn(effort(section), EFFORTS)
        self.assertTrue(MODELS["orchestrator"]["model"])
        self.assertTrue({"complex", "medium", "simple"} <= set(MODELS["design"]["by_complexity"]))
        self.assertIn("default", MODELS["implement"]["by_complexity"])

    def test_review_levels_name_reviewers_with_models(self):
        self.assertTrue(MODELS["review"]["minimal"]["subagent"]["model"])
        for level in ("minimal", "detailed"):
            reviewers = MODELS["review"][level]["reviewers"]
            self.assertTrue(reviewers)
            self.assertTrue(all(r["model"] for r in reviewers.values()), level)

    def test_explicit_effort_overrides_the_default(self):
        self.assertEqual(effort({"effort": "high"}), "high")
        self.assertEqual(effort({}), MODELS["default_effort"])


if __name__ == "__main__":
    unittest.main()
