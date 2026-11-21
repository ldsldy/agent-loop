"""조각 검사와 Unity 테스트 고르기·판정 자체 검사: python -I tests/test_slice_check.py"""
import contextlib
import io
import json
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


def fake_unity(results=PASSED, code=0, log="", calls=None, touch=None, rewrite=None, then=None, seen=None):
    """Unity 대신 도는 실행기. 명령이 가리키는 결과·로그 파일을 쓰고 종료 코드를 돌려준다.
    then은 두 번째 실행부터 쓸 (결과, 종료 코드)다. seen은 실행 때마다 작업 트리를 들여다보는 함수다."""
    count = [0]

    def runner(cmd):
        count[0] += 1
        text, exit_code = (results, code) if count[0] == 1 or then is None else then
        if calls is not None:
            calls.append(cmd)
        if seen is not None:
            seen(cmd[cmd.index("-projectPath") + 1])
        with open(cmd[cmd.index("-logFile") + 1], "w", encoding="utf-8") as file:
            file.write(log)
        if "-testResults" in cmd and text is not None:
            with open(cmd[cmd.index("-testResults") + 1], "w", encoding="utf-8") as file:
                file.write(text)
        for path in filter(None, (touch, rewrite)):
            write(cmd[cmd.index("-projectPath") + 1], path, "made by unity\n")
        return types.SimpleNamespace(returncode=exit_code)
    return runner


def plan_slice(root, **contract):
    """상태 파일에 이 조각(task/demo-s1)의 증명 계약을 적는다."""
    path = sc.state_path(root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump({"loop_branch": "loop/demo", "slices": [{"id": "s1", **contract}]}, file)


def read(root, path):
    with open(os.path.join(root, path), encoding="utf-8") as file:
        return file.read()


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
        self.assertEqual(self.judged("EditMode", 0, PASSED), (ut.PASS, ["EditMode 통과 2/2"]))

    def test_failed_case_is_named_with_the_first_line_of_its_message(self):
        status, lines = self.judged("EditMode", 2, FAILED)
        self.assertEqual(status, ut.FAIL)
        self.assertEqual(lines[1], "- FaultTrain.Tests.FooTests.Works: Expected 1")

    def test_filter_matching_no_test_is_a_failure(self):
        self.assertEqual(self.judged("EditMode", 0, EMPTY)[0], ut.UNKNOWN)

    def test_missing_results_is_a_failure(self):
        self.assertEqual(self.judged("PlayMode", 1)[0], ut.UNKNOWN)

    def test_compile_error_in_the_log_fails_and_is_listed_once(self):
        log = "Assets/A.cs(1,1): error CS1002: ; expected\nnoise\nAssets/A.cs(1,1): error CS1002: ; expected\n"
        status, lines = self.judged(None, 0, log=log)
        self.assertEqual(status, ut.COMPILE)
        self.assertEqual(lines, ["컴파일: 컴파일 오류 1건", "Assets/A.cs(1,1): error CS1002: ; expected"])

    def test_compile_only_passes_on_clean_exit(self):
        self.assertEqual(self.judged(None, 0), (ut.PASS, ["컴파일 통과"]))


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


OLD_TEST = "Assets/Tests/EditMode/OldTests.cs"
NEW_TEST = "Assets/Tests/EditMode/FooTests.cs"
NEW_CODE = "Assets/New.cs"


class ContractTests(unittest.TestCase):
    """조각의 증명 계약(kind·guards·retire)과 멈춤 예산."""

    def change_slice(self, root):
        plan_slice(root, kind="change", guards=[], retire={})
        write(root, NEW_TEST, TEST_SOURCE)
        write(root, NEW_CODE, "class New {}\n")
        write(root, "Assets/A.cs", "class A { int x; }\n")

    def test_change_slice_passes_when_its_test_fails_on_the_code_before_it(self):
        with slice_repo() as (root, run):
            self.change_slice(root)
            before = []
            unity = fake_unity(then=(FAILED, 2), seen=lambda at: before.append(
                (read(at, "Assets/A.cs"), os.path.exists(os.path.join(at, NEW_CODE)), os.path.exists(os.path.join(at, NEW_TEST)))))
            code, text = quiet(sc.check, root, is_test, unity)
            self.assertEqual(code, 0, text)
            self.assertIn("빨강에서 초록", text)
            self.assertEqual(before[0], ("class A { int x; }\n", True, True), "첫 실행은 구현이 있는 지금 트리다")
            self.assertEqual(before[1], ("class A {}\n", False, True), "둘째 실행은 구현 전 코드에 테스트만 얹은 것이다")
            self.assertEqual((read(root, "Assets/A.cs"), read(root, NEW_CODE)), ("class A { int x; }\n", "class New {}\n"), "끝나면 구현이 돌아와 있다")
            run("add", "-A")
            run("commit", "-qm", "slice")
            self.assertEqual(quiet(sc.has, root, "HEAD")[0], 0)

    def test_test_that_also_passes_without_the_change_proves_nothing(self):
        with slice_repo() as (root, _):
            self.change_slice(root)
            code, text = quiet(sc.check, root, is_test, fake_unity())
            self.assertEqual(code, 1)
            self.assertIn("구현 전 코드에서도 통과", text)
            self.assertEqual(read(root, NEW_CODE), "class New {}\n")

    def test_code_that_does_not_compile_without_the_change_counts_as_red(self):
        with slice_repo() as (root, _):
            self.change_slice(root)
            unity = fake_unity(then=(None, 1), log="")
            calls = []

            def runner(cmd):  # 둘째 실행(구현 전)은 컴파일 오류를 로그에 남긴다
                calls.append(cmd)
                done = unity(cmd)
                if len(calls) == 2:
                    with open(cmd[cmd.index("-logFile") + 1], "w", encoding="utf-8") as file:
                        file.write("Assets/Tests/EditMode/FooTests.cs(3,1): error CS0246: New\n")
                return done
            code, text = quiet(sc.check, root, is_test, runner)
            self.assertEqual(code, 0, text)
            self.assertIn("컴파일되지 않고", text)

    def test_change_slice_without_a_test_fails_before_unity_starts(self):
        with slice_repo() as (root, _):
            plan_slice(root, kind="change", guards=[], retire={})
            write(root, "Assets/A.cs", "class A { int x; }\n")
            calls = []
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(calls=calls))[0], 1)
            self.assertEqual(calls, [])

    def test_existing_test_outside_the_retire_list_cannot_be_touched(self):
        with slice_repo() as (root, _):
            plan_slice(root, kind="keep", guards=[], retire={})
            write(root, OLD_TEST, TEST_SOURCE.replace("FooTests", "OldTests").replace("Works", "WorksNow"))
            calls = []
            code, text = quiet(sc.check, root, is_test, fake_unity(calls=calls))
            self.assertEqual((code, calls), (1, []))
            self.assertIn(OLD_TEST, text)
            self.assertIn("판정을 요청", text)

    def test_retired_test_may_be_deleted_and_is_named_in_the_record(self):
        with slice_repo() as (root, run):
            plan_slice(root, kind="keep", guards=[], retire={OLD_TEST: "없앤 Helper 클래스를 검사한다"})
            os.remove(os.path.join(root, OLD_TEST))
            write(root, "Assets/A.cs", "class A { int x; }\n")
            code, text = quiet(sc.check, root, is_test, fake_unity())
            self.assertEqual(code, 0, text)
            self.assertIn("물러난 테스트: " + OLD_TEST, text)

    def test_guard_tests_run_untouched_and_cannot_be_changed(self):
        with slice_repo() as (root, _):
            plan_slice(root, kind="keep", guards=[OLD_TEST], retire={})
            write(root, "Assets/A.cs", "class A { int x; }\n")
            calls = []
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(calls=calls))[0], 0)
            self.assertIn("FaultTrain.Tests.OldTests", calls[0][calls[0].index("-testFilter") + 1])
            write(root, OLD_TEST, TEST_SOURCE)
            code, text = quiet(sc.check, root, is_test, fake_unity(calls=calls))
            self.assertEqual((code, len(calls)), (1, 1))
            self.assertIn("지킴이", text)

    def test_shape_rules_fail_before_unity_and_an_allowed_exception_is_recorded(self):
        shape_config = {"roots": ["Assets"], "max_depth": 3}
        with slice_repo() as (root, _):
            plan_slice(root, kind="keep", guards=[], retire={}, files=["Assets/ILink.cs", "Assets/Link.cs"])
            write(root, "Assets/ILink.cs", "public interface ILink { void Send(); }\n")
            write(root, "Assets/Link.cs", "public sealed class Link : ILink { public void Send() { } }\n")
            calls = []
            code, text = quiet(sc.check, root, is_test, fake_unity(calls=calls), shape_config=shape_config)
            self.assertEqual((code, calls), (1, []))
            self.assertIn("[인터페이스] ILink", text)
            plan_slice(root, kind="keep", guards=[], retire={}, files=["Assets/ILink.cs", "Assets/Link.cs"], allow={"ILink": "외부 경계: 네트워크"})
            code, text = quiet(sc.check, root, is_test, fake_unity(calls=calls), shape_config=shape_config)
            self.assertEqual(code, 0, text)
            self.assertIn("허용된 모양 예외: ILink", text)

    def test_changing_the_guid_of_an_existing_asset_fails(self):
        with slice_repo() as (root, run):
            write(root, "Assets/A.cs.meta", "fileFormatVersion: 2\nguid: aaaa1111\n")
            run("add", "-A")
            run("commit", "-qm", "meta")
            run("update-ref", "refs/remotes/origin/loop/demo", "HEAD")
            write(root, "Assets/A.cs.meta", "fileFormatVersion: 2\nguid: bbbb2222\n")
            calls = []
            code, text = quiet(sc.check, root, is_test, fake_unity(calls=calls))
            self.assertEqual((code, calls), (1, []))
            self.assertIn("GUID", text)
            write(root, "Assets/A.cs.meta", "fileFormatVersion: 2\nguid: aaaa1111\nMonoImporter:\n")
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(calls=calls))[0], 0, "GUID가 그대로면 다른 줄은 바뀌어도 된다")

    def test_after_the_budget_of_failures_it_stops_until_told_to_go_again(self):
        with slice_repo() as (root, _):
            write(root, NEW_TEST, TEST_SOURCE)
            calls = []
            for _ in range(sc.BUDGET):
                self.assertEqual(quiet(sc.check, root, is_test, fake_unity(FAILED, 2, calls=calls))[0], 1)
            code, text = quiet(sc.check, root, is_test, fake_unity(calls=calls))
            self.assertEqual((code, len(calls)), (1, sc.BUDGET), "예산을 다 쓰면 Unity를 띄우지 않는다")
            self.assertIn("멈춘다", text)
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(calls=calls), again=True)[0], 0)
            write(root, NEW_TEST, TEST_SOURCE + "// more\n")
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(FAILED, 2, calls=calls))[0], 1, "통과하면 예산이 다시 찬다")
            self.assertEqual(len(calls), sc.BUDGET + 2)

    def test_check_cut_off_in_the_before_state_is_undone_by_the_next_run(self):
        with slice_repo() as (root, run):
            self.change_slice(root)
            tree = sc.work_tree(root)
            marker = os.path.join(root, ".git", "agent-loop", "restore-tree")
            with open(marker, "w", encoding="utf-8") as file:  # 폴더는 plan_slice가 만든 상태 파일 옆이다
                file.write(tree)
            os.remove(os.path.join(root, NEW_CODE))  # 구현 전 상태에서 끊긴 것처럼
            write(root, "Assets/A.cs", "class A {}\n")
            write(root, "Assets/Stray.cs.meta", "made by unity\n")
            self.assertEqual(quiet(sc.check, root, is_test, fake_unity(then=(FAILED, 2)))[0], 0)
            self.assertEqual((read(root, "Assets/A.cs"), read(root, NEW_CODE)), ("class A { int x; }\n", "class New {}\n"))
            self.assertFalse(os.path.exists(os.path.join(root, "Assets/Stray.cs.meta")))


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
