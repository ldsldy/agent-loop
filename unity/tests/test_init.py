"""초기화와 씨앗 템플릿 자체 검사: python -I tests/test_init.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
import init  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.write(text)


class InitTests(unittest.TestCase):
    def test_first_run_copies_everything_and_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            first = init.plan(root, update=False)
            self.assertEqual({action for _, action in first}, {"copy"})
            init.apply(root, first)
            second = dict(init.plan(root, update=False))
            self.assertEqual(second[".github/scripts/auto_merge_guard.py"], "same")
            self.assertEqual(second[".agent-loop/config.json"], "keep")

    def test_repo_files_are_never_overwritten_but_stale_plugin_files_are_on_update(self):
        with tempfile.TemporaryDirectory() as root:
            init.apply(root, init.plan(root, update=False))
            config, guard = os.path.join(root, ".agent-loop", "config.json"), os.path.join(root, ".github", "scripts", "auto_merge_guard.py")
            write(config, '{"mine": true}')
            write(guard, "# old")
            self.assertEqual(dict(init.plan(root, update=False))[".github/scripts/auto_merge_guard.py"], "stale")
            init.apply(root, init.plan(root, update=True))
            self.assertEqual(open(config, encoding="utf-8").read(), '{"mine": true}')
            self.assertNotEqual(open(guard, encoding="utf-8").read(), "# old")

    def test_crlf_checkout_of_a_plugin_file_counts_as_the_same(self):
        with tempfile.TemporaryDirectory() as root:
            init.apply(root, init.plan(root, update=False))
            guard = os.path.join(root, ".github", "scripts", "auto_merge_guard.py")
            with open(guard, "rb") as file:
                data = file.read()
            with open(guard, "wb") as file:
                file.write(data.replace(b"\n", b"\r\n"))
            self.assertEqual(dict(init.plan(root, update=False))[".github/scripts/auto_merge_guard.py"], "same")


    def test_check_fails_on_a_stale_plugin_file_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q", root], check=True)
            init.apply(root, init.plan(root, update=False))

            def check():
                return subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "init.py"), "--check"], cwd=root,
                                      capture_output=True, text=True, encoding="utf-8")

            self.assertEqual(check().returncode, 0)
            guard = os.path.join(root, ".github", "scripts", "auto_merge_guard.py")
            write(guard, "# old")
            done = check()
            self.assertEqual(done.returncode, 1)
            self.assertIn(".github/scripts/auto_merge_guard.py: 플러그인 것과 다름", done.stdout)
            self.assertEqual(open(guard, encoding="utf-8").read(), "# old")


class SeedTests(unittest.TestCase):
    def test_seed_config_loads_through_every_plugin_module(self):
        """씨앗만 넣은 저장소에서 플러그인 스크립트가 모두 설정을 읽는다(빠진 키·배점 오류가 없다)."""
        with tempfile.TemporaryDirectory() as root:
            init.apply(root, init.plan(root, update=False))
            code = "import orchestrator, review, scope, quality_gate as qg; print(qg.PASS_SCORE, len(qg.GATES))"
            done = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {SCRIPTS!r}); {code}"],
                                  env={**os.environ, "AGENT_LOOP_ROOT": root}, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.split(), ["85", "6"])

    def test_seed_policy_has_every_key_the_guard_reads(self):
        with open(os.path.join(init.TEMPLATES, "github", "auto-merge-policy.json"), encoding="utf-8") as file:
            policy = json.load(file)
        self.assertTrue({"label", "required_checks", "deny", "allow", "test_glob", "test_disable_markers"} <= set(policy))


if __name__ == "__main__":
    unittest.main()
