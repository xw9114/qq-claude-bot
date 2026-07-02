import unittest

import nonebot


nonebot.init()

import plugins.web_search as web_search  # noqa: E402


class DuckDuckGoUrlTest(unittest.TestCase):
    def test_decodes_redirect_url(self):
        self.assertEqual(
            web_search.clean_duckduckgo_url(
                "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1"
            ),
            "https://example.com/a?x=1",
        )

    def test_build_search_url_percent_encodes_chinese(self):
        self.assertEqual(
            web_search.build_duckduckgo_search_url("Python 最新版本"),
            "https://html.duckduckgo.com/html/?q=Python%20%E6%9C%80%E6%96%B0%E7%89%88%E6%9C%AC",
        )


class DuckDuckGoParserTest(unittest.TestCase):
    def test_parses_title_url_and_snippet(self):
        html = """
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone">
            Example &amp; One
          </a>
          <a class="result__snippet" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fone">
            First <b>snippet</b> text.
          </a>
        </div>
        """

        self.assertEqual(
            web_search.parse_duckduckgo_results(html, limit=3),
            [
                web_search.WebSearchResult(
                    title="Example & One",
                    url="https://example.com/one",
                    snippet="First snippet text.",
                )
            ],
        )

    def test_respects_limit(self):
        html = """
        <a class="result__a" href="https://example.com/one">One</a>
        <a class="result__snippet">First</a>
        <a class="result__a" href="https://example.com/two">Two</a>
        <a class="result__snippet">Second</a>
        """

        self.assertEqual(
            web_search.parse_duckduckgo_results(html, limit=1),
            [
                web_search.WebSearchResult(
                    title="One",
                    url="https://example.com/one",
                    snippet="First",
                )
            ],
        )


class WebSearchCommandParseTest(unittest.TestCase):
    def test_parses_quick_web_search(self):
        self.assertEqual(
            web_search.parse_quick_web_search("联网 Python 最新版本"),
            "Python 最新版本",
        )
        self.assertEqual(
            web_search.parse_quick_web_search("联网搜索 张雪峰"),
            "张雪峰",
        )
        self.assertEqual(
            web_search.parse_quick_web_search("联网搜索张雪峰"),
            "张雪峰",
        )
        self.assertEqual(
            web_search.parse_quick_web_search("联网查一下 今天新闻"),
            "今天新闻",
        )
        self.assertEqual(
            web_search.parse_quick_web_search("查一下 北京天气"),
            "北京天气",
        )

    def test_slash_command_is_left_to_command_handler(self):
        self.assertIsNone(web_search.parse_quick_web_search("/联网 Python"))

    def test_combined_prefix_without_query_is_not_treated_as_query(self):
        self.assertIsNone(web_search.parse_quick_web_search("联网搜索"))
        self.assertIsNone(web_search.QUICK_WEB_SEARCH_PATTERN.fullmatch("联网搜索"))
        self.assertIsNone(web_search.parse_quick_web_search("联网查一下"))
        self.assertIsNone(web_search.QUICK_WEB_SEARCH_PATTERN.fullmatch("联网查一下"))


class WebAnswerPromptTest(unittest.TestCase):
    def test_answer_prompt_requires_grounding_in_results(self):
        messages = web_search.build_web_answer_messages(
            "Python 最新版本",
            [
                web_search.WebSearchResult(
                    title="Python Releases",
                    url="https://www.python.org/downloads/",
                    snippet="The latest Python release is listed here.",
                )
            ],
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("只能使用给定搜索结果", messages[0]["content"])
        self.assertIn("资料不足", messages[0]["content"])
        self.assertIn("Python Releases", messages[1]["content"])
        self.assertIn("https://www.python.org/downloads/", messages[1]["content"])

    def test_trim_reply_adds_ellipsis(self):
        self.assertEqual(web_search.trim_reply("abcdef", max_chars=4), "abc…")


if __name__ == "__main__":
    unittest.main()
