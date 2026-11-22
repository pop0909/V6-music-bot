"""我的歌單：不用瀏覽器的 prompt / confirm，改用面板內的詢問對話框（askDialog）
執行：python -m unittest discover -s tests
"""
import os
import re
import unittest

from appjs import read_app_js  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def func(js, start):
    a = js.index(start)
    return js[a:js.index("\n    }\n", a)]


class MylistDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read_app_js()
        cls.ml = read("static", "js", "app", "11-mylist.js")
        cls.html = read("templates", "index.html")

    def test_no_native_prompt_or_confirm_in_mylist(self):
        self.assertIsNone(re.search(r"(?<![\w.])(prompt|confirm|alert)\(", self.ml))

    def test_markup_is_accessible_dialog(self):
        m = re.search(r'<div class="modal-overlay ask-overlay" id="askModal">(.*?)</div>\s*</div>\s*</div>', self.html, re.S)
        self.assertIsNotNone(m)
        box = m.group(0)
        for s in ('role="dialog"', 'aria-modal="true"', 'aria-labelledby="askTitle"', 'id="askInput"',
                  'id="askOk"', 'id="askCancel"', 'id="askClose"', 'aria-label="關閉"', 'role="alert"', '<label for="askInput"'):
            self.assertIn(s, box)

    def test_keyboard_and_focus(self):
        body = self.ml[self.ml.index("const askDialog"):self.ml.index("const ML = ")]
        self.assertIn("e.key === 'Escape'", body)
        self.assertIn("e.key === 'Enter' && !e.isComposing", body)       # 注音選字的 Enter 不送出
        self.assertIn("e.key === 'Tab'", body)                            # Tab 只在對話框裡循環
        self.assertIn("e.stopPropagation()", body)
        self.assertIn("back.focus(", body)                                # 關掉後焦點回原本的按鈕
        self.assertIn("addEventListener('focusin'", body)
        self.assertIn("if (e.target === ov) finish(null)", body)          # 點外面 = 取消

    def test_each_action_uses_dialog(self):
        act = func(self.ml, "async function act(")
        self.assertIn("title: '重新命名歌單'", act)
        self.assertIn("title: '刪除歌單'", act)
        self.assertIn("danger: true", act)
        self.assertIn("加入歌曲到", act)
        self.assertIn("title: '新增歌單'", func(self.ml, "async function create("))
        self.assertIn("title: '匯入歌單'", func(self.ml, "async function importList("))
        self.assertEqual(self.ml.count("await askDialog("), 5)

    def test_shortcuts_ignore_open_dialog(self):
        # 對話框也是 .modal-overlay：鍵盤快捷鍵（空白鍵暫停等）在對話框開著時不作用
        self.assertIn("if (document.querySelector('.modal-overlay.active')) return;", self.js)

    def test_phone_styles(self):
        css = read("static", "css", "style.css")
        i = css.index(".ask-modal .form-input { font-size: 16px")
        self.assertIn("@media (max-width: 600px)", css[i - 400:i])
        self.assertIn(".ask-modal .modal-footer .btn-sm { flex: 1; justify-content: center; min-height: 44px", css)
        self.assertIn(".btn-sm.danger {", css)


if __name__ == "__main__":
    unittest.main()
