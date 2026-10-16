import unittest
from pathlib import Path


class ChatComposerUiTests(unittest.TestCase):
    def test_enter_has_one_guarded_submit_path(self):
        html = (Path(__file__).resolve().parents[1] / "assets" / "web" / "app.html").read_text(encoding="utf-8")
        self.assertRegex(html, r"text\.onkeydown\s*=\s*\(e\)\s*=>")
        self.assertIn("composer.requestSubmit(send)", html)
        self.assertRegex(html, r"if\s*\(sending\)\s*return;")
        self.assertIn("compositionstart", html)
        self.assertIn("compositionend", html)
        self.assertNotIn("e.target.form?.requestSubmit()", html)
        self.assertRegex(html, r"client_message_id:\s*submission\.client_message_id")
        self.assertRegex(html, r"client_message_id:\s*messageId\(\)")
        self.assertLess(html.index('await api("message"'), html.index("S.drafts.delete(draftKey)"))
        self.assertNotIn("e.keyCode!==229", html)

    def test_message_selection_guard_is_part_of_the_main_renderer(self):
        html = (Path(__file__).resolve().parents[1] / "assets" / "web" / "app.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("<script>"), 1)
        self.assertNotIn("_renderProject", html)
        self.assertIn('currentMessages = document.querySelector(".messages")', html)
        self.assertIn("currentMessages.contains(selection.anchorNode)", html)
        self.assertIn("S.dirty = true", html)
        self.assertIn("user-select: text", html)


if __name__ == "__main__":
    unittest.main()
