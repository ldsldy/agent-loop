"""오케스트레이터 판정 자체 검사: python -I tests/test_orchestrator.py (GitHub 없이 돈다)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["AGENT_LOOP_ROOT"] = os.path.join(HERE, "fixture")  # ShalomEng 값의 .agent-loop 고정물
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import orchestrator as o  # noqa: E402


def issue(number, labels=("ready-for-agent",), body=""):
    return {"number": number, "title": f"이슈 {number}", "labels": [{"name": n} for n in labels], "body": body}


class BlockersTests(unittest.TestCase):
    def test_reads_every_number_on_blocked_by_lines(self):
        self.assertEqual(o.blockers("앞\nBlocked by: #7\n뒤"), [7])
        self.assertEqual(o.blockers("Blocked by #19, #30"), [19, 30])
        self.assertEqual(o.blockers("관련 #4"), [])


class IssueStateTests(unittest.TestCase):
    def test_human_label_wins(self):
        self.assertEqual(o.issue_state(issue(5, ("human", "ready-for-agent")), set(), set())[0], "human")

    def test_open_blocker_outside_the_milestone_blocks(self):
        self.assertEqual(o.issue_state(issue(18, body="Blocked by: #7"), {7, 18}, {18}), ("blocked", [7]))

    def test_closed_or_same_milestone_blocker_does_not_block(self):
        self.assertEqual(o.issue_state(issue(18, body="Blocked by: #7"), {18}, {18})[0], "ready")
        self.assertEqual(o.issue_state(issue(18, body="Blocked by: #17"), {17, 18}, {17, 18})[0], "ready")

    def test_without_ready_label_it_is_not_ready(self):
        self.assertEqual(o.issue_state(issue(114, ("needs-triage",)), {114}, {114})[0], "not-ready")


class PickTests(unittest.TestCase):
    def test_a_milestone_starts_only_when_every_open_issue_is_ready(self):
        ready = o.milestone_report({"number": 5, "title": "코드 정리"}, [issue(114)], {114})
        mixed = o.milestone_report({"number": 1, "title": "UI"}, [issue(17), issue(18, body="Blocked by: #7")], {7, 17, 18})
        empty = o.milestone_report({"number": 2, "title": "빈 것"}, [], set())
        self.assertTrue(ready["startable"])
        self.assertFalse(mixed["startable"])
        self.assertFalse(empty["startable"])
        self.assertEqual(o.pick([mixed, ready, empty])["number"], 5)

    def test_lowest_numbered_startable_milestone_first_or_none(self):
        a = o.milestone_report({"number": 3, "title": "a"}, [issue(1)], {1})
        b = o.milestone_report({"number": 2, "title": "b"}, [issue(2)], {2})
        self.assertEqual(o.pick([a, b])["number"], 2)
        self.assertIsNone(o.pick([]))


class CleanupTests(unittest.TestCase):
    def test_only_merged_task_branches_are_removed(self):
        branches = ["task/a", "task/b", "loop/x", "develop", "main", "claude/c"]
        merged = {"task/a", "loop/x", "claude/c"}
        self.assertEqual(o.removable(branches, merged), ["task/a"])


if __name__ == "__main__":
    unittest.main()
