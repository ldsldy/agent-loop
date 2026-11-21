"""조각 검사와 Unity 테스트 고르기·판정 자체 검사: python -I tests/test_slice_check.py"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import slice_check as sc  # noqa: E402
import unity_tests as ut  # noqa: E402

TEST_SOURCE = """using NUnit.Framework;
namespace FaultTrain.Tests
{
    public sealed class FooTests
    {
        [Test] public void Works() {}
        private class Helper {}
    }
}
"""
PASSED = '<test-run result="Passed" total="2" passed="2" failed="0" skipped="0"/>'
FAILED = """<test-run result="Failed" total="2" passed="1" failed="1" skipped="0">
  <test-case fullname="FaultTrain.Tests.FooTests.Works" result="Failed"><failure><message><![CDATA[Expected 1
But was 2]]></message></failure></test-case>
  <test-case fullname="FaultTrain.Tests.FooTests.Other" result="Passed"/>
</test-run>"""
EMPTY = '<test-run result="Passed" total="0" passed="0" failed="0" skipped="0"/>'


def is_test(path):
    return "/Tests/" in path


def write(root, path, text):
    os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
    with open(os.path.join(root, path), "w", encoding="utf-8") as file:
        file.write(text)


def fake_unity(results=PASSED, code=0, log="", calls=None, touch=None, rewrite=None):
    """Unity 대신 도는 실행기. 명령이 가리키는 결과·로그 파일을 쓰고 종료 코드를 돌려준다."""
    def runner(cmd):
        if calls is not None:
            calls.append(cmd)
        with open(cmd[cmd.index("-logFile") + 1], "w", encoding="utf-8") as file:
            file.write(log)
        if "-testResults" in cmd and results is not None:
            with open(cmd[cmd.index("-testResults") + 1], "w", encoding="utf-8") as file:
                file.write(results)
        for path in filter(None, (touch, rewrite)):
            write(cmd[cmd.index("-projectPath") + 1], path, "made by unity\n")
        return types.SimpleNamespace(returncode=code)
    return runner


@contextlib.contextmanager
def slice_repo(branch="task/demo-s1"):
    """임시 Unity 저장소: origin/loop/demo에서 갈라진 조각 브랜치. EditMode·PlayMode 테스트 폴더와 루프 설정이 있다."""
    with tempfile.TemporaryDirectory() as tmp:
        def run(*args):
            return subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True, text=True).stdout.strip()
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        write(tmp, ".agent-loop/config.json", "{}")
        write(tmp, "ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.6.8f1\n")
        write(tmp, "Assets/Tests/EditMode/E.asmdef", '{"name": "E", "includePlatforms": ["Editor"]}')
        write(tmp, "Assets/Tests/PlayMode/P.asmdef", '{"name": "P", "includePlatforms": []}')
        write(tmp, "Assets/Tests/EditMode/OldTests.cs", TEST_SOURCE.replace("FooTests", "OldTests"))
        write(tmp, "Assets/A.cs", "class A {}\n")
        run("add", "-A")
        run("commit", "-qm", "base")
        run("update-ref", "refs/remotes/origin/loop/demo", "HEAD")
        run("switch", "-qc", branch)
        yield tmp, run


def quiet(call, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        code = call(*args, **kwargs)
    return code, out.getvalue() + err.getvalue()


class ChoosingTests(unittest.TestCase):
    def test_fixture_names_carry_the_namespace_and_every_class(self):
        self.assertEqual(ut.fixtures(TEST_SOURCE), ["FaultTrain.Tests.FooTests", "FaultTrain.Tests.Helper"])

    def test_file_without_a_test_attribute_gives_nothing_to_run(self):
        self.assertEqual(ut.fixtures("namespace X { public class Builder {} }"), [])

    def test_asmdef_decides_edit_or_play_mode(self):
        with slice_repo() as (root, _):
            write(root, "Assets/Tests/EditMode/Sub/FooTests.cs", TEST_SOURCE)
            write(root, "Assets/Tests/PlayMode/BarTests.cs", TEST_SOURCE.replace("FooTests", "BarTests"))
            chosen = ut.plan(root, ["Assets/Tests/EditMode/Sub/FooTests.cs", "Assets/Tests/PlayMode/BarTests.cs", "Assets/Tests/x.json"])
        self.assertEqual(chosen, {"EditMode": ["FaultTrain.Tests.FooTests", "FaultTrain.Tests.Helper"],
                                  "PlayMode": ["FaultTrain.Tests.BarTests", "FaultTrain.Tests.Helper"]})


class JudgingTests(unittest.TestCase):
    def judged(self, mode, code, results=None, log=""):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [os.path.join(tmp, name) for name in ("r.xml", "r.log")]
            for path, text in zip(paths, (results, log)):
                if text is not None:
                    with open(path, "w", encoding="utf-8") as file:
                        file.write(text)
            return ut.judge(mode, code, *paths)

    def test_all_passed(self):
        self.assertEqual(self.judged("EditMode", 0, PASSED), (True, ["EditMode 통과 2/2"]))

    def test_failed_case_is_named_with_the_first_line_of_its_message(self):
        ok, lines = self.judged("EditMode", 2, FAILED)
        self.assertFalse(ok)
        self.assertEqual(lines[1], "- FaultTrain.Tests.FooTests.Works: Expected 1")

    def test_filter_matching_no_test_is_a_failure(self):
        self.assertFalse(self.judged("EditMode", 0, EMPTY)[0])

    def test_missing_results_is_a_failure(self):
        self.assertFalse(self.judged("PlayMode", 1)[0])

    def test_compile_error_in_the_log_fails_and_is_listed_once(self):
        log = "Assets/A.cs(1,1): error CS1002: ; expected\nnoise\nAssets/A.cs(1,1): error CS1002: ; expected\n"
        ok, lines = self.judged(None, 0, log=log)
        self.assertFalse(ok)
        self.assertEqual(lines, ["컴파일: 컴파일 오류 1건", "Assets/A.cs(1,1): error CS1002: ; expected"])

    def test_compile_only_passes_on_clean_exit(self):
        self.assertEqual(self.judged(None, 0), (True, ["컴파일 통과"]))


class TreeTests(unittest.TestCase):
    def test_clean_work_tree_hashes_to_the_commit_tree(self):
        with slice_repo() as (root, run):
            self.assertEqual(sc.work_tree(root), run("rev-parse", "HEAD^{tree}"))

    def test_untracked_file_changes_the_hash_and_leaves_the_index_alone(self):
        with slice_repo() as (root, run):
            write(root, "Assets/New.cs.meta", "guid: 1\n")
            self.assertNotEqual(sc.work_tree(root), run("rev-parse", "HEAD^{tree}"))
            self.assertEqual(run("diff", "--cached", "--name-only"), "")

    def test_same_size_edit_in_the_same_second_as_the_last_index_write_still_changes_the_hash(self):
        with slice_repo() as (root, run):
            path, index = os.path.join(root, "Assets/A.cs"), os.path.join(root, ".git", "index")
            before = os.stat(path)
            os.utime(index, (int(before.st_mtime), int(before.st_mtime)))  # 인덱스를 파일과 같은 초에 쓴 것으로 둔다
            write(root, "Assets/A.cs", "class B {}\n")  # 크기가 같다
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # 같은 초에 고친 것처럼 수정 시각도 같다
            self.assertNotEqual(sc.work_tree(root), run("rev-parse", "HEAD^{tree}"))

    def test_loop_base_is_the_longest_loop_name_the_task_branch_starts_with(self):
        with slice_repo("task/demo-ui-s2") as (root, run):
            run("update-ref", "refs/remotes/origin/loop/demo-ui", "HEAD")
            self.assertEqual(sc.loop_base(root, "task/demo-ui-s2"), "origin/loop/demo-ui")
            self.assertEqual(sc.loop_base(root, "task/demo-s1"), "origin/loop/demo")
            self.assertIsNone(sc.loop_base(root, "task/other-s1"))
            self.assertIsNone(sc.loop_base(root, "fix/demo-s1"))


class CheckTests(unittest.TestCase):
    def test_changed_tests_run_by_fixture_and_the_committed_tree_is_recorded(self):
        with slice_repo() as (root, run):
            write(root, "Assets/Tests/EditMode/FooTests.cs", TEST_SOURCE)
            write(root, "Assets/A.cs", "class A { int x; }\n")
            calls = []
            code, _ = quiet(sc.check, root, is_test, fake_unity(calls=calls))
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][calls[0].index("-testPlatform") + 1], "EditMode")
            self.assertEqual(calls[0][calls[0].index("-testFilter") + 1], "FaultTrain.Tests.FooTests;FaultTrain.Tests.Helper")
            run("add", "-A")
            run("commit", "-qm", "slice")
            self.assertEqual(quiet(sc.has, root, "HEAD")[0], 0)
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(calls=calls))[0], 0)
            self.assertEqual(len(calls), 1, "통과한 트리는 Unity를 다시 띄우지 않는다")
            write(root, "Assets/A.cs", "class A { int y; }\n")
            run("commit", "-qam", "more")
            self.assertEqual(quiet(sc.has, root, "HEAD")[0], 1, "검사 뒤에 바꾼 커밋은 기록이 없다")

    def test_no_changed_test_checks_compile_only_and_skips_deleted_tests(self):
        with slice_repo() as (root, run):
            write(root, "Assets/A.cs", "class A { int x; }\n")
            os.remove(os.path.join(root, "Assets/Tests/EditMode/OldTests.cs"))
            calls = []
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(calls=calls))[0], 0)
            self.assertEqual(len(calls), 1)
            self.assertIn("-quit", calls[0])
            self.assertNotIn("-runTests", calls[0])

    def test_failing_test_leaves_no_record_and_reports_the_case(self):
        with slice_repo() as (root, run):
            write(root, "Assets/Tests/EditMode/FooTests.cs", TEST_SOURCE)
            code, text = quiet(sc.check, root, is_test, fake_unity(FAILED, code=2))
            self.assertEqual(code, 1)
            self.assertIn("FaultTrain.Tests.FooTests.Works", text)
            run("add", "-A")
            run("commit", "-qm", "slice")
            self.assertEqual(quiet(sc.has, root, "HEAD")[0], 1)

    def test_edit_mode_failure_stops_before_play_mode(self):
        with slice_repo() as (root, _):
            write(root, "Assets/Tests/EditMode/FooTests.cs", TEST_SOURCE)
            write(root, "Assets/Tests/PlayMode/BarTests.cs", TEST_SOURCE.replace("FooTests", "BarTests"))
            calls = []
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(FAILED, code=2, calls=calls))[0], 1)
            self.assertEqual(len(calls), 1)

    def test_file_made_during_the_run_fails_the_check_and_is_named(self):
        with slice_repo() as (root, _):
            write(root, "Assets/Tests/EditMode/FooTests.cs", TEST_SOURCE)
            code, text = quiet(sc.check, root, is_test, fake_unity(touch="Assets/Tests/EditMode/FooTests.cs.meta"))
            self.assertEqual(code, 1)
            self.assertIn("FooTests.cs.meta", text)

    def test_tracked_file_rewritten_during_the_run_is_put_back_and_the_check_still_passes(self):
        with slice_repo() as (root, run):
            write(root, "Assets/A.cs", "class A { int x; }\n")
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(rewrite="ProjectSettings/ProjectVersion.txt"))[0], 0)
            with open(os.path.join(root, "ProjectSettings/ProjectVersion.txt"), encoding="utf-8") as file:
                self.assertEqual(file.read(), "m_EditorVersion: 6000.6.8f1\n")
            with open(os.path.join(root, "Assets/A.cs"), encoding="utf-8") as file:
                self.assertEqual(file.read(), "class A { int x; }\n", "커밋하지 않은 조각의 변경은 그대로다")
            run("add", "-A")
            run("commit", "-qm", "slice")
            self.assertEqual(quiet(sc.has, root, "HEAD")[0], 0)

    def test_outside_a_slice_branch_it_refuses(self):
        with slice_repo("fix/x") as (root, _):
            with self.assertRaises(SystemExit):
                quiet(sc.check, root, is_test, fake_unity())


class GuardTests(unittest.TestCase):
    def push(self, root, command="git push -u origin HEAD"):
        return quiet(sc.guard, {"tool_input": {"command": command}, "cwd": root})

    def test_push_without_a_passing_record_is_blocked_with_the_command_to_run(self):
        with slice_repo() as (root, _):
            write(root, "Assets/A.cs", "class A { int x; }\n")
            code, text = self.push(root)
            self.assertEqual(code, 2)
            self.assertIn("slice_check.py", text)

    def test_push_after_a_passing_check_goes_through_even_before_the_commit(self):
        with slice_repo() as (root, _):
            write(root, "Assets/A.cs", "class A { int x; }\n")
            quiet(sc.check, root, is_test, fake_unity())
            self.assertEqual(self.push(root, "git add -A && git commit -m slice && git push")[0], 0)

    def test_other_commands_branches_and_repositories_are_left_alone(self):
        with slice_repo() as (root, run):
            self.assertEqual(self.push(root, 'git commit -m "push the fix"')[0], 0)
            self.assertEqual(self.push(root, "gh pr create")[0], 0)
            run("switch", "-qc", "fix/x")
            self.assertEqual(self.push(root)[0], 0)
            run("switch", "-q", "task/demo-s1")
            os.remove(os.path.join(root, ".agent-loop", "config.json"))
            self.assertEqual(self.push(root)[0], 0)
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(self.push(plain)[0], 0)

    def test_unity_batchmode_is_blocked_only_while_this_pc_runs_ci(self):
        command = {"tool_input": {"command": '& "C:\\Unity.exe" -batchmode -runTests'}}
        with slice_repo() as (root, _):
            write(root, ".agent-loop/config.json", '{"runner": {"task": "Runner"}}')
            self.assertEqual(quiet(sc.guard, {**command, "cwd": root}, ci_busy=lambda: True)[0], 2)
            self.assertEqual(quiet(sc.guard, {**command, "cwd": root}, ci_busy=lambda: False)[0], 0)
            write(root, ".agent-loop/config.json", "{}")
            self.assertEqual(quiet(sc.guard, {**command, "cwd": root}, ci_busy=lambda: True)[0], 0, "실행기를 쓰지 않는 저장소")

    def test_push_is_recognised_with_git_options_before_it(self):
        self.assertTrue(sc.PUSH.search("git -C wt push origin task/x"))
        self.assertTrue(sc.PUSH.search("cd wt; git push"))
        self.assertFalse(sc.PUSH.search("git log --grep push"))


if __name__ == "__main__":
    unittest.main()
