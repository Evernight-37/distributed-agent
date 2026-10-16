import unittest
from pathlib import Path


class ChatComposerUiTests(unittest.TestCase):
    def test_enter_has_one_guarded_submit_path(self):
        html=(Path(__file__).resolve().parents[1]/'assets'/'web'/'app.html').read_text(encoding='utf-8')
        self.assertIn("text.onkeydown=e=>",html)
        self.assertIn("composer.requestSubmit(send)",html)
        self.assertIn("if(sending)return;",html)
        self.assertIn("compositionstart",html)
        self.assertIn("compositionend",html)
        self.assertNotIn("e.target.form?.requestSubmit()",html)
        self.assertIn("client_message_id:submission.client_message_id",html)
        self.assertIn("client_message_id:messageId()",html)
        self.assertLess(html.index("await api('message'"),html.index("S.drafts.delete(draftKey)"))
        self.assertNotIn("e.keyCode!==229",html)


if __name__=='__main__':unittest.main()
