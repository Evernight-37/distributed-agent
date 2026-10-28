import unittest
from pathlib import Path


def desktop_source():
    web = Path(__file__).resolve().parents[1] / "assets" / "web"
    order = (
        "core.js",
        "dashboard.js",
        "project.js",
        "settings-components.js",
        "account-settings.js",
        "runtime.js",
    )
    return "\n".join((web / "app" / name).read_text(encoding="utf-8") for name in order)


class ChatComposerUiTests(unittest.TestCase):
    def test_login_page_links_to_the_bundled_beginner_guide(self):
        root = Path(__file__).resolve().parents[1]
        app = desktop_source()
        guide = root / "assets" / "web" / "guide.html"
        self.assertIn('guide.href = "guide.html"', app)
        self.assertIn("第一次使用？打开新手教程", app)
        self.assertTrue(guide.is_file())

    def test_enter_has_one_guarded_submit_path(self):
        source = desktop_source()
        self.assertRegex(source, r"text\.onkeydown\s*=\s*\(e\)\s*=>")
        self.assertIn("composer.requestSubmit(send)", source)
        self.assertRegex(source, r"if\s*\(sending\)\s*return;")
        self.assertIn("compositionstart", source)
        self.assertIn("compositionend", source)
        self.assertNotIn("e.target.form?.requestSubmit()", source)
        self.assertRegex(source, r"client_message_id:\s*submission\.client_message_id")
        self.assertRegex(source, r"client_message_id:\s*messageId\(\)")
        self.assertLess(source.index('await api("message"'), source.index("S.drafts.delete(draftKey)"))
        self.assertNotIn("e.keyCode!==229", source)

    def test_message_selection_guard_is_part_of_the_main_renderer(self):
        web = Path(__file__).resolve().parents[1] / "assets" / "web"
        html = (web / "app.html").read_text(encoding="utf-8")
        css = (web / "styles" / "app.css").read_text(encoding="utf-8")
        source = desktop_source()
        scripts = [
            "app/core.js",
            "app/dashboard.js",
            "app/project.js",
            "app/settings-components.js",
            "app/account-settings.js",
            "app/runtime.js",
        ]
        self.assertEqual(
            [html.index(f'src="{name}"') for name in scripts], sorted(html.index(f'src="{name}"') for name in scripts)
        )
        self.assertEqual(sum(html.count(f'src="{name}"') for name in scripts), len(scripts))
        self.assertNotIn("_renderProject", source)
        self.assertIn('currentMessages = document.querySelector(".messages")', source)
        self.assertIn("currentMessages.contains(selection.anchorNode)", source)
        self.assertIn("S.dirty = true", source)
        self.assertIn("user-select: text", css)


if __name__ == "__main__":
    unittest.main()
