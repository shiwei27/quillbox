#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""正文清洗的回归测试（离线，纳入回归套件）。

起因：一封只有 text/html 的通知邮件，正文整段显示成
`.vmail-p { mso-line-height-rule:exactly; } ...`。

根因是 `_clean_body` 只做 `re.sub(r"<[^>]+>", " ")` —— 去掉标签，
但 `<style>` / `<script>` 的**内容本身不是标签**，被当成正文留下。

这个缺陷潜伏很久：`body` 取 `text_body or html_body`，绝大多数邮件都带
text/plain 分段，走不到 html 那条路，所以一直没暴露。
把它钉在测试里，免得以后有人"优化"清洗逻辑时又踩回去。
"""
import os as _os; ROOT = _os.path.dirname(_os.path.abspath(__file__))  # repo root (tests live at top level)
import os
import sys
import unittest

os.chdir(ROOT)
sys.path.insert(0, ROOT)

from core.pop3_extractor import _clean_body        # noqa: E402


class TestStripsStyleAndScript(unittest.TestCase):
    """style/script 的内容必须整块去掉，不能当正文留下。"""

    def test_paired_style(self):
        out = _clean_body('<style>.a { font-size: 16px; color:#000; }</style>'
                          '<p>真正的正文</p>')
        self.assertIn("真正的正文", out)
        self.assertNotIn("font-size", out)
        self.assertNotIn("{", out)

    def test_paired_script(self):
        out = _clean_body('<script>var x = 1; function f(){ return 2; }</script>'
                          '<p>正文</p>')
        self.assertIn("正文", out)
        self.assertNotIn("var x", out)
        self.assertNotIn("function", out)

    def test_case_insensitive(self):
        out = _clean_body('<STYLE type="text/css">.b { margin: 8px 0; }</STYLE>'
                          '<div>正文</div>')
        self.assertIn("正文", out)
        self.assertNotIn("margin", out)

    def test_style_with_attributes(self):
        out = _clean_body('<style media="screen" type="text/css">'
                          '.c { padding: 2px; }</style><p>正文</p>')
        self.assertIn("正文", out)
        self.assertNotIn("padding", out)

    def test_unclosed_style_dropped_to_end(self):
        """未闭合的 <style>：后面本来就是 CSS，丢到结尾。"""
        out = _clean_body('<p>正文在前</p><style>.d { color: red; }')
        self.assertIn("正文在前", out)
        self.assertNotIn("color: red", out)

    def test_html_comment_hiding_css(self):
        """Outlook 的条件注释里常藏整段 CSS。"""
        out = _clean_body('<!--[if mso]><style>.e { color: red; }</style><![endif]-->'
                          '<p>正文</p>')
        self.assertIn("正文", out)
        self.assertNotIn("color: red", out)

    def test_multiple_style_blocks(self):
        out = _clean_body('<style>.f{a:1}</style><p>甲</p><style>.g{b:2}</style>'
                          '<p>乙</p>')
        self.assertIn("甲", out)
        self.assertIn("乙", out)
        self.assertNotIn("a:1", out)
        self.assertNotIn("b:2", out)

    def test_real_world_sample(self):
        """用户实际报告的那封邮件的形态。"""
        html = ('<html><head><style>.vmail-p { mso-line-height-rule:exactly; }\n'
                '.vmail-ol, .vmail-ul { list-style-position: inside; padding-left: 0; }\n'
                '.vmail-li { line-height: 1.5; font-family: Microsoft YaHei; }'
                '</style></head><body><p>尊敬的示例云用户: 您好！</p>'
                '<p>示例区域01节点将进行割接。</p></body></html>')
        out = _clean_body(html)
        self.assertIn("尊敬的示例云用户", out)
        self.assertIn("割接", out)
        for bad in ("mso-line-height-rule", "list-style-position", "padding-left",
                    "Microsoft YaHei", "{"):
            self.assertNotIn(bad, out, "CSS 残留: %s" % bad)


class TestStillWorksNormally(unittest.TestCase):
    """修复不能把原有行为改坏。"""

    def test_plain_text_untouched(self):
        self.assertEqual(_clean_body("就是一段普通正文"), "就是一段普通正文")

    def test_ordinary_tags_removed(self):
        out = _clean_body("<p>甲</p><b>乙</b>")
        self.assertIn("甲", out)
        self.assertIn("乙", out)
        self.assertNotIn("<p>", out)
        self.assertNotIn("<b>", out)

    def test_entities_unescaped(self):
        self.assertIn("&", _clean_body("<p>A &amp; B</p>"))

    def test_empty_input(self):
        self.assertEqual(_clean_body(""), "")
        self.assertEqual(_clean_body(None), "")

    def test_whitespace_collapsed(self):
        out = _clean_body("<p>甲</p>\n\n\n\n<p>乙</p>")
        self.assertNotIn("\n\n\n", out)

    def test_attachment_only_mail_yields_empty(self):
        """只有 <style> 没有正文文字的邮件 → 空正文。
        这是正确答案：留着 CSS 反而更糟（库里真有这样一封）。"""
        self.assertEqual(_clean_body('<html><head><style>font{line-height:1.6}'
                                     '</style></head><body></body></html>').strip(), "")

    def test_style_word_in_prose_not_eaten(self):
        """正文里正常提到 style 这个词，不该被误删。"""
        out = _clean_body("<p>请参考 style 指南</p>")
        self.assertIn("style 指南", out)


class TestAllBackendsShareIt(unittest.TestCase):
    def test_three_backends_use_same_cleaner(self):
        """三条后端必须共用这一份清洗实现，否则修了一处漏两处。"""
        import io
        import re
        for mod in ("core/pop3_extractor.py", "core/imap_extractor.py",
                    "core/ews_extractor.py"):
            with io.open(mod, encoding="utf-8") as f:
                src = f.read()
            self.assertTrue(re.search(r"_clean_body", src),
                            "%s 没用共用的 _clean_body" % mod)


if __name__ == "__main__":
    unittest.main(verbosity=2)
