"""모양 검사 자체 검사: python -I tests/test_shape.py"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import shape  # noqa: E402

CONFIG = {"roots": ["Assets/Scripts"], "max_depth": 3, "folder_names": ["Parts", "UI", "Types", "Network"]}
ROOT = "Assets/Scripts/"


def rules(files, added, declared=None, allow=None, before=None, config=CONFIG):
    """files = {루트 아래 경로: 본문}. 어긴 (규칙, 대상)과 허용된 (규칙, 대상)을 돌려준다."""
    files = {ROOT + path: text for path, text in files.items()}
    violations, allowed = shape.check(files, {ROOT + p: t for p, t in (before or {}).items()}, {ROOT + path for path in added},
                                      None if declared is None else [ROOT + path for path in declared], allow or {}, config)
    return sorted((rule, subject) for rule, subject, _ in violations), sorted((rule, subject) for rule, subject, _ in allowed)


def wrap(body):
    return "namespace Demo\n{\n" + body + "\n}\n"


USER_A = wrap("public sealed class Lamp { Mode mode; }")
USER_B = wrap("public sealed class Gauge { Mode mode; }")


class ParsingTests(unittest.TestCase):
    def test_declarations_carry_kind_bases_and_whether_they_are_top_level(self):
        found = shape.parse("a.cs", wrap("""
    public delegate void Changed(int value);
    public sealed class Foo : FooBase, IThing
    {
        private enum Inner { X }
        // class NotReal { }
        string text = "class AlsoNotReal { }";
    }"""))
        self.assertEqual([(t["kind"], t["name"], t["top"]) for t in found],
                         [("delegate", "Changed", True), ("class", "Foo", True), ("enum", "Inner", False)])
        self.assertEqual(found[1]["bases"], ["FooBase", "IThing"])

    def test_file_scoped_namespace_and_no_namespace_are_top_level_too(self):
        self.assertTrue(shape.parse("a.cs", "namespace Demo;\npublic enum Mode { A }\n")[0]["top"])
        self.assertTrue(shape.parse("a.cs", "public struct Size { public int W; }\n")[0]["top"])


class FileRuleTests(unittest.TestCase):
    def test_file_for_a_single_small_type_is_refused(self):
        files = {"Parts/Mode.cs": wrap("public enum Mode { A, B }"), "Parts/Lamp.cs": USER_A, "Parts/Gauge.cs": USER_B}
        self.assertIn(("타입 하나만을 위한 파일", ROOT + "Parts/Mode.cs"), rules(files, ["Parts/Mode.cs"])[0])

    def test_shared_small_types_gathered_in_one_types_file_pass(self):
        files = {"Types/PartTypes.cs": wrap("public enum Mode { A }\n    public struct Size { public int W; }"),
                 "Parts/Lamp.cs": USER_A + "// Size", "Parts/Gauge.cs": USER_B.replace("Mode mode;", "Mode mode; Size size;"),
                 "Parts/Dial.cs": wrap("public sealed class Dial { Size size; }")}
        self.assertEqual(rules(files, ["Types/PartTypes.cs"])[0], [])

    def test_small_type_used_by_one_class_belongs_in_that_class_file(self):
        files = {"Types/PartTypes.cs": wrap("public enum Mode { A }\n    public enum Other { B }"),
                 "Parts/Lamp.cs": USER_A, "Parts/Gauge.cs": wrap("public sealed class Gauge { Other o; }"),
                 "Parts/Dial.cs": wrap("public sealed class Dial { Other o; }")}
        self.assertEqual(rules(files, ["Types/PartTypes.cs"])[0], [("한 곳만 쓰는 타입", "Mode")])
        beside_its_user = {"Parts/Lamp.cs": wrap("public enum Mode { A }\n    public sealed class Lamp { Mode mode; }")}
        self.assertEqual(rules(beside_its_user, ["Parts/Lamp.cs"])[0], [])

    def test_new_code_file_must_be_declared_by_the_design(self):
        files = {"Parts/Lamp.cs": wrap("public sealed class Lamp { }")}
        self.assertEqual(rules(files, ["Parts/Lamp.cs"], declared=["Parts/Gauge.cs"])[0], [("새 파일", ROOT + "Parts/Lamp.cs")])
        self.assertEqual(rules(files, ["Parts/Lamp.cs"], declared=["Parts/Lamp.cs"])[0], [])
        self.assertEqual(rules(files, ["Parts/Lamp.cs"])[0], [], "계약이 없는 조각은 보지 않는다")

    def test_folder_depth_and_names_are_limited(self):
        files = {"UI/Parts/Types/Network/Deep.cs": wrap("public sealed class Deep { }"), "Stuff/Odd.cs": wrap("public sealed class Odd { }")}
        found = rules(files, list(files))[0]
        self.assertIn(("폴더 깊이", ROOT + "UI/Parts/Types/Network/Deep.cs"), found)
        self.assertIn(("폴더 이름", "Stuff"), found)

    def test_only_what_the_slice_added_is_judged(self):
        old = wrap("public enum Mode { A }")
        files = {"Parts/Mode.cs": old, "Parts/Lamp.cs": USER_A, "Parts/Open.cs": wrap("public class Open { }")}
        self.assertEqual(rules(files, [])[0], [])
        grown = wrap("public class Open { }\n    public class Fresh { }")
        self.assertEqual(rules({**files, "Parts/Open.cs": grown}, [], before={"Parts/Open.cs": files["Parts/Open.cs"]})[0],
                         [("닫지 않은 클래스", "Fresh")], "고친 파일에서는 새로 생긴 타입만 본다")


class AbstractionRuleTests(unittest.TestCase):
    def test_interface_needs_two_implementations_unless_the_slice_gives_a_reason(self):
        files = {"Network/ILink.cs": wrap("public interface ILink { void Send(); }"),
                 "Network/Link.cs": wrap("public sealed class Link : ILink { public void Send() { } }")}
        self.assertEqual(rules(files, list(files))[0], [("인터페이스", "ILink")])
        allowed = rules(files, list(files), allow={"ILink": "외부 경계: 네트워크"})
        self.assertEqual(allowed, ([], [("인터페이스", "ILink")]))
        two = {**files, "Network/FakeLink.cs": wrap("public sealed class FakeLink : ILink { public void Send() { } }")}
        self.assertEqual(rules(two, list(two))[0], [])

    def test_base_class_needs_two_children_and_a_base_name(self):
        base = wrap("public abstract class Part { public abstract void Draw(); }")
        child = wrap("public sealed class Lamp : Part { public override void Draw() { } }")
        found = rules({"Parts/Part.cs": base, "Parts/Lamp.cs": child}, ["Parts/Part.cs", "Parts/Lamp.cs"])[0]
        self.assertEqual(found, [("베이스 이름", "Part"), ("베이스 클래스", "Part")])
        good = {"Parts/PartBase.cs": base.replace("class Part", "class PartBase"), "Parts/Lamp.cs": child.replace(": Part", ": PartBase"),
                "Parts/Gauge.cs": child.replace("Lamp : Part", "Gauge : PartBase")}
        self.assertEqual(rules(good, list(good))[0], [])

    def test_inheritance_deeper_than_the_limit_is_refused(self):
        files = {"Parts/ABase.cs": wrap("public abstract class ABase { }"), "Parts/A2.cs": wrap("public sealed class A2 : ABase { }"),
                 "Parts/B.cs": wrap("public class B : ABase { }"), "Parts/B2.cs": wrap("public sealed class B2 : B { }"),
                 "Parts/C.cs": wrap("public class C : B { }"), "Parts/C2.cs": wrap("public sealed class C2 : C { }"),
                 "Parts/D.cs": wrap("public sealed class D : C { }")}
        self.assertEqual([found for found in rules(files, list(files))[0] if found[0] == "상속 깊이"], [("상속 깊이", "C2"), ("상속 깊이", "D")])

    def test_extension_points_nobody_uses_and_unsealed_leaf_classes_are_refused(self):
        files = {"Parts/Lamp.cs": wrap("public class Lamp { protected virtual void OnLit() { } }")}
        self.assertEqual(rules(files, list(files))[0], [("닫지 않은 클래스", "Lamp"), ("쓰이지 않는 확장점", "Lamp.OnLit")])


class AuditTests(unittest.TestCase):
    def test_audit_judges_the_whole_project_and_groups_by_rule_with_the_most_frequent_first(self):
        files = {ROOT + "Parts/Mode.cs": wrap("public enum Mode { A }"), ROOT + "Parts/Lamp.cs": wrap("public class Lamp { Mode mode; }"),
                 ROOT + "Parts/Gauge.cs": wrap("public class Gauge { Mode mode; }")}
        found = shape.audit(files, CONFIG)
        self.assertEqual([(rule, len(items)) for rule, items in found.items()], [("닫지 않은 클래스", 2), ("타입 하나만을 위한 파일", 1)])
        self.assertEqual(found["타입 하나만을 위한 파일"][0][0], ROOT + "Parts/Mode.cs")


class ReportTests(unittest.TestCase):
    def test_measure_counts_files_types_small_files_and_folders(self):
        numbers = shape.measure({"Assets/Scripts/Parts/Lamp.cs": USER_A, "Assets/Scripts/Types/T.cs": wrap("public enum Mode { A }\n    public struct S { }")})
        self.assertEqual((numbers["files"], numbers["small"]), (2, 2))
        self.assertEqual(numbers["types"], {"class": 1, "enum": 1, "struct": 1})
        self.assertEqual(numbers["folders"], ["Assets/Scripts/Parts", "Assets/Scripts/Types"])

    def test_build_output_folders_are_not_read(self):
        with tempfile.TemporaryDirectory() as root:
            for path in ("src/App/Lamp.cs", "src/App/obj/Debug/Lamp.g.cs", "src/App/bin/Copy.cs"):
                os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
                with open(os.path.join(root, path), "w", encoding="utf-8") as file:
                    file.write(USER_A)
            self.assertEqual(list(shape.code_now(root, ["src"], lambda path: False)), ["src/App/Lamp.cs"])


if __name__ == "__main__":
    unittest.main()
