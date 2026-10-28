from html.parser import HTMLParser
from pathlib import Path
import unittest
import re


ROOT = Path(__file__).resolve().parents[1]
APP_HTML = ROOT / "static" / "index.html"
APP_CSS = ROOT / "static" / "app.css"
APP_JS = ROOT / "static" / "app.js"


class _PrototypeContract(HTMLParser):
    def __init__(self):
        super().__init__()
        self.screens = set()
        self.nav_targets = set()
        self.details_classes = []
        self.class_names = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        self.class_names.update(classes)
        if values.get("data-screen"):
            self.screens.add(values["data-screen"])
        if values.get("data-nav"):
            self.nav_targets.add(values["data-nav"])
        if tag == "details":
            self.details_classes.append(classes)


class _ProductionContract(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.smart_ancestors = {}
        self.id_ancestors = {}
        self.classes_by_id = {}
        self.view_ancestors = {}
        self.button_counts_by_ancestor_class = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        node = (tag, classes, values)
        ancestors = [item for item in self.stack]
        if values.get("data-smart"):
            self.smart_ancestors[values["data-smart"]] = ancestors
        if values.get("id"):
            self.id_ancestors[values["id"]] = ancestors
            self.classes_by_id[values["id"]] = classes
        if values.get("data-view"):
            self.view_ancestors[values["data-view"]] = ancestors
        if tag == "button":
            for _, ancestor_classes, _ in ancestors:
                for class_name in ancestor_classes:
                    self.button_counts_by_ancestor_class[class_name] = self.button_counts_by_ancestor_class.get(class_name, 0) + 1
        if tag not in {"input", "img", "link", "meta", "br", "hr", "source"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    @staticmethod
    def has_ancestor_class(ancestors, class_name):
        return any(class_name in classes for _, classes, _ in ancestors)


class RomanticCompactNavigationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(APP_HTML.exists(), "浪漫紧凑版五页应用入口尚未创建")
        self.html = APP_HTML.read_text(encoding="utf-8")
        self.contract = _PrototypeContract()
        self.contract.feed(self.html)

    def test_contains_all_five_surfaces_and_navigation(self):
        expected = {"home", "chat", "trip", "timeline", "life"}
        self.assertEqual(self.contract.screens, expected)
        self.assertEqual(self.contract.nav_targets, expected)

    def test_demonstrates_progressive_disclosure_and_chat_dock(self):
        detail_classes = set().union(*self.contract.details_classes)
        self.assertIn("smart-disclosure", detail_classes)
        self.assertIn("compact-more", detail_classes)
        self.assertIn("chat-compose-dock", self.contract.class_names)


class RomanticCompactProductionTests(unittest.TestCase):
    def setUp(self):
        self.html = APP_HTML.read_text(encoding="utf-8")
        self.css = APP_CSS.read_text(encoding="utf-8")
        self.js = APP_JS.read_text(encoding="utf-8")
        self.contract = _ProductionContract()
        self.contract.feed(self.html)

    def test_smart_surfaces_use_native_progressive_disclosure(self):
        for surface in ("chat", "timeline", "life"):
            with self.subTest(surface=surface):
                ancestors = self.contract.smart_ancestors[surface]
                self.assertTrue(
                    self.contract.has_ancestor_class(ancestors, "smart-disclosure"),
                    f"{surface} 的智能编排尚未放进原生折叠区",
                )

    def test_home_low_frequency_previews_share_more_section(self):
        for element_id in ("weeklyPreview", "datePreview"):
            with self.subTest(element_id=element_id):
                ancestors = self.contract.id_ancestors[element_id]
                self.assertTrue(
                    self.contract.has_ancestor_class(ancestors, "compact-more"),
                    f"{element_id} 尚未移动到更多生活动态",
                )

    def test_chat_has_fixed_compose_dock_and_compact_grouped_bubbles(self):
        ancestors = self.contract.id_ancestors["chatForm"]
        self.assertTrue(self.contract.has_ancestor_class(ancestors, "chat-compose-dock"))
        compact_css = re.sub(r"\s+", " ", self.css)
        self.assertRegex(compact_css, r"\.chat-compose-dock\s*\{[^}]*position:\s*fixed")
        self.assertRegex(compact_css, r"\.chat-content\s*\{[^}]*padding-bottom:\s*(?:1[4-9]\d|[2-9]\d\d)px")
        self.assertRegex(compact_css, r"\.message-row\s*\{[^}]*display:\s*flex")
        self.assertRegex(compact_css, r"\.message-row\s+\.message\s*\{[^}]*max-width:\s*72%")
        self.assertRegex(compact_css, r"\.message-meta\s*\{[^}]*font-size:\s*8px")
        self.assertIn("message-group-start", self.js)
        self.assertIn("message-group-end", self.js)
        self.assertIn('class="message-meta"', self.js)
        self.assertNotIn('<button class="message-save"', self.js)

    def test_bottom_navigation_is_viewport_fixed_and_keyboard_aware(self):
        compact_css = re.sub(r"\s+", " ", self.css)
        self.assertLess(self.html.index('<nav class="bottom-nav"'), self.html.index("</main>"))
        self.assertNotIn("container-type: inline-size", compact_css)
        self.assertRegex(compact_css, r"html\.soft-keyboard-open\s+\.bottom-nav\s*\{[^}]*pointer-events:\s*none")
        self.assertRegex(compact_css, r"html\.soft-keyboard-open\s+\.chat-compose-dock\s*\{[^}]*bottom:")
        self.assertIn("visualViewport", self.js)
        self.assertIn('classList.toggle("soft-keyboard-open"', self.js)
        self.assertIn('document.querySelector(".bottom-nav")?.setAttribute("inert", "")', self.js)

    def test_home_keeps_a_compact_hero_and_only_three_priorities(self):
        compact_css = re.sub(r"\s+", " ", self.css)
        self.assertIn('class="phone-shell romantic-compact"', self.html)
        self.assertRegex(
            compact_css,
            r"\.romantic-compact\s+\.home-hero\s*\{[^}]*height:\s*23[0-9]px",
        )
        self.assertRegex(
            self.js,
            r"assistant\.today\?\.priorities\s*\|\|\s*\[\]\)\.slice\(0,\s*3\)",
        )

    def test_trip_timeline_and_life_keep_secondary_tools_out_of_the_first_view(self):
        compact_css = re.sub(r"\s+", " ", self.css)
        self.assertRegex(compact_css, r"\.romantic-compact\s+\.section-tabs\s*\{[^}]*position:\s*sticky")
        self.assertRegex(compact_css, r"\.romantic-compact\s+\.section-tabs\s*\{[^}]*flex-wrap:\s*nowrap")
        for view in ("report", "private", "trash"):
            with self.subTest(view=view):
                self.assertTrue(
                    self.contract.has_ancestor_class(self.contract.view_ancestors[view], "timeline-more"),
                    f"时光低频视图 {view} 尚未收进更多菜单",
                )
        self.assertEqual(self.contract.button_counts_by_ancestor_class.get("life-core-actions"), 4)
        self.assertGreaterEqual(self.contract.button_counts_by_ancestor_class.get("life-more-actions", 0), 5)


if __name__ == "__main__":
    unittest.main()
