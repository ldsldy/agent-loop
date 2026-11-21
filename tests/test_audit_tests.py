"""테스트 점검 자체 검사: python -I tests/test_audit_tests.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import audit_tests as at  # noqa: E402


def wrap(body):
    return "namespace Demo\n{\n" + body + "\n}\n"


def fixture(body):
    return wrap("public sealed class LampTests\n    {\n" + body + "\n    }")


def found(code, tests):
    return {name: sorted(subject for subject, _ in items) for name, items in at.audit(code, tests).items()}


class TestOnlyStructureTests(unittest.TestCase):
    def test_type_only_tests_use_is_listed_and_a_type_the_code_uses_is_not(self):
        code = {"Lamp.cs": wrap("public sealed class Lamp { LampState state; }"), "LampState.cs": wrap("public enum LampState { On }"),
                "LampProbe.cs": wrap("public sealed class LampProbe { }")}
        tests = {"Tests/LampTests.cs": fixture("[Test] public void A() { Assert.NotNull(new LampProbe()); var s = LampState.On; }")}
        self.assertEqual(found(code, tests), {"테스트만 쓰는 타입": ["LampProbe"]})

    def test_types_unity_finds_by_itself_are_not_called_test_only(self):
        code = {"Screen.cs": wrap("public sealed class Screen : MonoBehaviour { }"),
                "Chip.cs": wrap("[UxmlElement]\n    public sealed partial class Chip { }")}
        tests = {"Tests/T.cs": fixture("[Test] public void A() { Assert.NotNull(typeof(Screen)); Assert.NotNull(typeof(Chip)); }")}
        self.assertEqual(found(code, tests), {})

    def test_interface_kept_alive_by_a_test_fake_is_listed(self):
        code = {"ILink.cs": wrap("public interface ILink { void Send(); }"), "Link.cs": wrap("public sealed class Link : ILink { public void Send() { } }"),
                "Sender.cs": wrap("public sealed class Sender { ILink link; }")}
        tests = {"Tests/T.cs": wrap("public sealed class FakeLink : ILink { public void Send() { } }")}
        self.assertEqual(found(code, tests), {"테스트 가짜를 위한 인터페이스": ["ILink"]})
        two = {**code, "Udp.cs": wrap("public sealed class Udp : ILink { public void Send() { } }")}
        self.assertEqual(found(two, tests), {}, "실제 구현이 둘이면 테스트 때문에 있는 것이 아니다")


class WeakTestTests(unittest.TestCase):
    def test_tests_that_read_private_members_are_listed(self):
        tests = {"Tests/T.cs": fixture('[Test] public void A() { var f = typeof(Lamp).GetField("x", BindingFlags.NonPublic | BindingFlags.Instance); Assert.NotNull(f); }')}
        self.assertEqual(found({}, tests), {"안을 들여다보는 테스트": ["Tests/T.cs"]})

    def test_test_without_any_assertion_is_listed_by_name(self):
        tests = {"Tests/T.cs": fixture("""
        [Test] public void Checks() { Assert.AreEqual(1, 1); }
        [Test] public void OnlyRuns() { new object(); }
        [UnityTest] public IEnumerator Waits() { yield return null; }
        [Test] public void Arrow() => Assert.That(1, Is.EqualTo(1));
        [TestCase(1)] public void Fluent(int x) { Assert.That(x, Is.GreaterThan(0)); }""")}
        self.assertEqual(found({}, tests), {"확인하는 것이 없는 테스트": ["T.cs:OnlyRuns", "T.cs:Waits"]})

    def test_assertion_made_through_a_helper_counts_even_two_calls_deep(self):
        tests = {"Tests/T.cs": fixture("""
        [Test] public void ThroughHelper() { AssertStep(1); }
        [Test] public void TwoDeep() { Fails(2); }
        [Test] public void OnlyBuilds() { Build(3); }
        static void AssertStep(int x) { Assert.AreEqual(1, x); }
        static void Fails(int x) { Rejected(x); }
        static void Rejected(int x) { Assert.Throws<System.Exception>(() => Build(x)); }
        static object Build(int x) { return new object(); }""")}
        self.assertEqual(found({}, tests), {"확인하는 것이 없는 테스트": ["T.cs:OnlyBuilds"]})

    def test_switched_off_tests_are_listed_and_counts_are_reported(self):
        tests = {"Tests/T.cs": fixture('[Test, Ignore("나중에")] public void A() { Assert.Pass(); }\n        [Test] public void B() { Assert.Pass(); }')}
        self.assertEqual(found({}, tests), {"꺼 둔 테스트": ["Tests/T.cs"]})
        self.assertEqual(at.count(tests), {"files": 1, "tests": 2})


if __name__ == "__main__":
    unittest.main()
