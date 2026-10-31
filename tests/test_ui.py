"""纯 XML 夹具测试，不是微信 Android 界面验收。"""

import unittest
from xml.sax.saxutils import quoteattr

from wechatauto_android.errors import AmbiguousTarget, SelectorNotFound, ValidationError
from wechatauto_android.ui import Hierarchy, Selector


def xml(text="你好", extra="", bounds="[0,0][100,100]"):
    return (
        "<hierarchy><node text="
        + quoteattr(text)
        + ' package="com.tencent.mm" resource-id="id" class="android.widget.TextView" enabled="true" clickable="true" bounds='
        + quoteattr(bounds)
        + " "
        + extra
        + "/></hierarchy>"
    )


class HierarchyTests(unittest.TestCase):
    def test_round_trip_selector(self):
        selected = Selector(text="你好", enabled=True, clickable=True, resource_id="id")
        self.assertEqual(Selector.from_dict(selected.to_dict()), selected)
        self.assertEqual(Hierarchy.parse(xml()).one(selected).center, (50, 50))

    def test_empty_hierarchy(self):
        self.assertEqual(Hierarchy.parse("<hierarchy/> ").nodes, ())

    def test_description_and_tree(self):
        tree = Hierarchy.parse(
            '<hierarchy><node bounds="[0,0][100,100]"><node text="x" content-desc="描述" bounds="[1,1][5,5]"/></node></hierarchy>'
        )
        self.assertEqual(tree.nodes[0].children[0].description, "描述")
        self.assertEqual(tree.one(Selector(description="描述")).text, "x")
        with self.assertRaises(TypeError):
            tree.nodes[0].attrs["text"] = "mutation"

    def test_selector_nonmatching_and_duplicate(self):
        tree = Hierarchy.parse(xml())
        with self.assertRaises(SelectorNotFound):
            tree.one(Selector(text="你"))
        duplicate = xml().replace("</hierarchy>", xml()[11:])
        with self.assertRaises(AmbiguousTarget):
            Hierarchy.parse(duplicate).one(Selector(text="你好"))

    def test_invalid_selector_fields(self):
        invalid = [
            {},
            {"enabled": True},
            {"text": 5},
            {"text": "x", "enabled": 1},
            {"text": "x", "clickable": "true"},
            {"text": "\x00"},
            {"text": "\ud800"},
            {"text": "x" * 16385},
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValidationError):
                Selector.from_dict(value)
        for value in (None, [], "text", {"textContains": "x"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Selector.from_dict(value)

    def test_selector_empty_text_can_be_qualified(self):
        self.assertEqual(
            Hierarchy.parse(xml(text="")).one(Selector(text="", resource_id="id")).text, ""
        )

    def test_invalid_xml_inputs(self):
        invalid = [
            None,
            b"<hierarchy/>",
            "",
            "<node/>",
            "<hierarchy><foo/></hierarchy>",
            "<hierarchy",
            "\ud800",
            "<hierarchy/>" + " " * (2 * 1024 * 1024),
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValidationError):
                Hierarchy.parse(value)

    def test_bounds_limits(self):
        for bounds in (
            "garbage",
            "[10,10][1,1]",
            "[0,0][1000001,1]",
            "[0,0][1.1,4]",
            "[0,0][1,1]trailing",
            "[0,0][" + "1" * 5000 + ",1]",
        ):
            with self.subTest(bounds=bounds), self.assertRaises(ValidationError):
                Hierarchy.parse(xml(bounds=bounds))
        self.assertEqual(Hierarchy.parse(xml(bounds="[-1,0][1,1]")).nodes[0].bounds, (-1, 0, 1, 1))

    def test_invalid_booleans(self):
        for key in ("enabled", "clickable", "visible-to-user", "long-clickable", "scrollable"):
            value = xml().replace(f'{key}="true"', "")
            value = value.replace("/>", f'{key}="1"/>')
            with self.subTest(key=key), self.assertRaises(ValidationError):
                Hierarchy.parse(value)

    def test_depth_limit(self):
        with self.assertRaises(ValidationError):
            Hierarchy.parse("<hierarchy>" + "<node>" * 129 + "</node>" * 129 + "</hierarchy>")

    def test_node_limit(self):
        with self.assertRaises(ValidationError):
            Hierarchy.parse("<hierarchy>" + "<node/>" * 20_001 + "</hierarchy>")

    def test_find_requires_selector(self):
        with self.assertRaises(ValidationError):
            Hierarchy.parse(xml()).find_all({"text": "你好"})

    def test_fingerprint_exact_input(self):
        a = Hierarchy.parse(xml())
        self.assertEqual(a.fingerprint, Hierarchy.parse(xml()).fingerprint)
        self.assertNotEqual(a.fingerprint, Hierarchy.parse(xml() + " ").fingerprint)


if __name__ == "__main__":
    unittest.main()
