import unittest
from unittest.mock import patch
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from wechat_mp_articles import (
    WeChatAPIError,
    fetch_public_article,
)


class FakeResponse:
    url = "https://mp.weixin.qq.com/s/test"
    apparent_encoding = "utf-8"
    encoding = "utf-8"
    text = '''<html><head>
    <meta property="og:title" content="测试文章">
    <meta property="og:article:author" content="测试公众号">
    </head><body><div id="js_content"><p>第一段正文。</p><img data-src="https://mmbiz.qpic.cn/test.jpg"><p>第二段正文。</p></div></body></html>'''

    def raise_for_status(self):
        return None


class WeChatArticleTests(unittest.TestCase):
    @patch("wechat_mp_articles.requests.get", return_value=FakeResponse())
    def test_public_url_extracts_article_without_account_api(self, _get):
        article = fetch_public_article("https://mp.weixin.qq.com/s/test")
        self.assertEqual(article["title"], "测试文章")
        self.assertEqual(article["author"], "测试公众号")
        self.assertEqual(article["body_text"], "第一段正文。\n第二段正文。")
        self.assertEqual(article["image_urls"], ["https://mmbiz.qpic.cn/test.jpg"])

    def test_whitelist_error_explains_both_resolutions(self):
        message = str(WeChatAPIError(40164, "invalid ip 203.0.113.8, not in whitelist"))
        self.assertIn("203.0.113.8", message)
        self.assertIn("whitelist", message)
        self.assertIn("'url' command", message)

if __name__ == "__main__":
    unittest.main()
