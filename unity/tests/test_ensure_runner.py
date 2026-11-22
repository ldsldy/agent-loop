"""실행기를 켤지 판정하는 함수 자체 검사: python -I tests/test_ensure_runner.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["AGENT_LOOP_ROOT"] = os.path.join(HERE, "fixture")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
from ensure_runner import busy, runner_task, should_start  # noqa: E402

HOST = "DESKTOP-Q3BJVJJ"


def runner(name, status):
    return {"name": name, "status": status, "busy": False}


class ShouldStartTests(unittest.TestCase):
    def test_other_pc_offline_does_not_start_when_this_pc_is_online(self):
        listed = [runner(HOST, "online"), runner("DESKTOP-OB7KNB1", "offline")]
        self.assertFalse(should_start(listed, HOST, running=False))

    def test_listener_process_running_does_not_start_even_if_listed_offline(self):
        self.assertFalse(should_start([runner(HOST, "offline")], HOST, running=True))

    def test_this_pc_offline_and_no_listener_starts(self):
        listed = [runner(HOST, "offline"), runner("DESKTOP-OB7KNB1", "online")]
        self.assertTrue(should_start(listed, HOST, running=False))

    def test_this_pc_not_registered_and_no_listener_starts(self):
        self.assertTrue(should_start([runner("DESKTOP-OB7KNB1", "online")], HOST, running=False))
        self.assertTrue(should_start([], HOST, running=False))

    def test_host_name_compare_ignores_case(self):
        self.assertFalse(should_start([runner(HOST, "online")], HOST.lower(), running=False))


class BusyTests(unittest.TestCase):
    def test_only_this_pc_running_a_job_counts_as_busy(self):
        self.assertTrue(busy([{"name": HOST.lower(), "status": "online", "busy": True}], HOST))
        self.assertFalse(busy([runner(HOST, "online"), {"name": "DESKTOP-OB7KNB1", "status": "online", "busy": True}], HOST))
        self.assertFalse(busy([], HOST))


class RunnerTaskTests(unittest.TestCase):
    def test_task_comes_from_the_repository_config(self):
        self.assertEqual(runner_task(), "GitHubActionsRunner-Unity")


if __name__ == "__main__":
    unittest.main()
