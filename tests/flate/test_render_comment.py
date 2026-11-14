import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "render_comment", ROOT / "scripts/flate/render_comment.py")
render_comment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_comment)


def deployment(image="app:1", name="web", namespace="apps"):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"template": {"spec": {"containers": [{"name": "app", "image": image}]}}},
    }


def configmap(data, name="settings"):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "apps"}, "data": data}


def crd(versions):
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": "things.example.org"}, "spec": {"versions": versions}}


def run(base, head):
    pairs = render_comment._pairs()
    return render_comment.comment(pairs.pair(base, head, [], []))


class Comment(unittest.TestCase):
    def test_equal_renders_say_so_with_the_object_count(self):
        out = run([deployment()], [deployment()])
        self.assertEqual(out, "No rendered changes. Both renders hold 1 objects.\n")

    def test_a_changed_field_is_shown_as_a_diff_under_the_object(self):
        out = run([deployment("app:1")], [deployment("app:2")])
        self.assertIn("1 rendered objects differ: 1 changed, 0 added, 0 removed.", out)
        self.assertIn("=== Deployment apps/web (changed)", out)
        self.assertIn('-          image: "app:1"', out)
        self.assertIn('+          image: "app:2"', out)

    def test_added_and_removed_objects_are_counted_and_printed(self):
        out = run([deployment(name="old")], [deployment(name="new")])
        self.assertIn("0 changed, 1 added, 1 removed", out)
        self.assertIn("=== Deployment apps/new (added)", out)
        self.assertIn("=== Deployment apps/old (removed)", out)
        self.assertIn('+kind: "Deployment"', out)
        self.assertIn('-kind: "Deployment"', out)

    def test_a_multi_line_value_is_diffed_line_by_line(self):
        out = run([configmap({"app.conf": "a=1\nb=2\nc=3\n"})],
                  [configmap({"app.conf": "a=1\nb=9\nc=3\n"})])
        self.assertIn("-    b=2", out)
        self.assertIn("+    b=9", out)
        self.assertNotIn("a=1\\nb", out)

    def test_a_crd_is_named_without_its_body(self):
        out = run([crd([{"name": "v1"}])], [crd([{"name": "v1"}, {"name": "v2"}])])
        self.assertIn("=== CustomResourceDefinition things.example.org (changed)", out)
        self.assertIn("(CRD body not shown)", out)
        self.assertNotIn("v2", out)

    def test_a_long_object_diff_is_cut_and_says_so(self):
        many = {f"key{i:04d}": str(i) for i in range(400)}
        out = run([configmap({})], [configmap(many)])
        self.assertIn("more lines not shown)", out)
        self.assertLess(len(out.splitlines()), render_comment.OBJECT_LINES + 10)

    def test_rendered_text_cannot_close_the_code_block(self):
        out = run([configmap({"note": "x"})],
                  [configmap({"note": "```\n@someone\n~~~\n\x1b[31m"})])
        self.assertNotIn("```", out)
        self.assertNotIn("~~~", out)
        self.assertNotIn("\x1b", out)

    def test_a_longer_run_of_fence_characters_is_broken_up_whole(self):
        out = run([configmap({"note": "x"})], [configmap({"note": "`````\n~~~~"})])
        self.assertNotIn("```", out)
        self.assertNotIn("~~~", out)
        self.assertIn("` ` `", out)
        self.assertIn("~ ~ ~", out)

    def test_two_copies_of_one_object_are_marked(self):
        out = run([deployment("app:1"), deployment("app:1")],
                  [deployment("app:1"), deployment("app:2")])
        self.assertIn("(two producers render this object)", out)


class Text(unittest.TestCase):
    def test_empty_containers_and_scalars(self):
        self.assertEqual(
            render_comment.text({"a": {}, "b": [], "c": None, "d": True, "e": 3, "f": "s"}),
            ["a: {}", "b: []", "c: null", "d: true", "e: 3", 'f: "s"'])

    def test_lists_of_mappings_are_indented_under_the_dash(self):
        self.assertEqual(
            render_comment.text({"items": [{"name": "a", "value": 1}]}),
            ["items:", "  -", '    name: "a"', "    value: 1"])

    def test_keys_that_are_not_plain_are_quoted(self):
        self.assertEqual(render_comment.text({"a b": 1, "x/y.z": 2}), ['"a b": 1', "x/y.z: 2"])


class Main(unittest.TestCase):
    def call(self, base, head):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name, content in (("base.json", base), ("head.json", head)):
                path = pathlib.Path(tmp) / name
                path.write_text(content)
                paths.append(str(path))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = render_comment.main(paths)
        return code, out.getvalue(), err.getvalue()

    def test_two_renders_give_a_comment(self):
        code, out, _ = self.call(json.dumps([deployment("app:1")]), json.dumps([deployment("app:2")]))
        self.assertEqual(code, 0)
        self.assertIn("=== Deployment apps/web (changed)", out)

    def test_an_unreadable_render_is_an_error_with_no_output(self):
        code, out, err = self.call("not json", json.dumps([deployment()]))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("render comment failed", err)

    def test_an_empty_render_is_an_error_not_an_empty_diff(self):
        code, out, _ = self.call("[]", json.dumps([deployment()]))
        self.assertEqual((code, out), (1, ""))


if __name__ == "__main__":
    unittest.main()
