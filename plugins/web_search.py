"""联网搜索插件。

显式触发时访问搜索引擎，提取搜索结果摘要，再交给 OpenAI 兼容模型
基于资料回答。普通聊天不会自动联网，避免无意外发外部请求。
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from nonebot import get_driver, on_command, on_regex
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.log import logger
from nonebot.params import CommandArg, RegexGroup
from openai import AsyncOpenAI
from plugins.command_cooldown import SessionCooldown

config = get_driver().config


def _read_int(name: str, default: int) -> int:
    value = getattr(config, name, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        logger.warning("配置 {}={} 无效，使用默认值 {}", name.upper(), value, default)
        return default


def _read_float(name: str, default: float) -> float:
    value = getattr(config, name, default)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        logger.warning("配置 {}={} 无效，使用默认值 {}", name.upper(), value, default)
        return default


api_key = getattr(config, "openai_api_key", None)
base_url = getattr(config, "openai_base_url", None)
model_name = getattr(config, "openai_model", "gpt-5.4-mini")

client = AsyncOpenAI(api_key=api_key, base_url=base_url) if api_key else None

WEB_SEARCH_MAX_RESULTS = _read_int("web_search_max_results", 5)
WEB_SEARCH_COMMAND_COOLDOWN = _read_float("web_search_command_cooldown", 15.0)
WEB_SEARCH_REPLY_MAX_CHARS = _read_int("web_search_reply_max_chars", 900)
WEB_SEARCH_TIMEOUT = httpx.Timeout(15.0)
WEB_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

QUICK_WEB_SEARCH_PREFIXES = (
    "联网搜索",
    "联网查一下",
    "联网查一查",
    "帮我搜一下",
    "查一下",
    "查一查",
    "搜一下",
    "联网",
    "搜索",
)
QUICK_WEB_SEARCH_REGEX = (
    r"^(?:联网搜索|联网查一下|联网查一查|帮我搜一下|查一下|查一查|搜一下|联网(?!搜索|查一下|查一查)|搜索)\s*(\S.*)$"
)
QUICK_WEB_SEARCH_PATTERN = re.compile(QUICK_WEB_SEARCH_REGEX)
web_search_cooldown = SessionCooldown(WEB_SEARCH_COMMAND_COOLDOWN)


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str


def compact_text(text: Any) -> str:
    return " ".join(str(text).split())


def clean_duckduckgo_url(href: str) -> str:
    href = href.strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    elif href.startswith("/"):
        href = f"https://duckduckgo.com{href}"

    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target).strip()
    return href


def get_attr(attrs: list[tuple[str, str | None]], name: str) -> str:
    for attr_name, value in attrs:
        if attr_name == name and value:
            return value
    return ""


def has_class(attrs: list[tuple[str, str | None]], class_name: str) -> bool:
    return class_name in get_attr(attrs, "class").split()


class DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.results: list[WebSearchResult] = []
        self._title_parts: list[str] | None = None
        self._title_href = ""
        self._snippet_parts: list[str] | None = None
        self._pending_title = ""
        self._pending_url = ""

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if len(self.results) >= self.limit:
            return
        if tag == "a" and has_class(attrs, "result__a"):
            self._flush_pending()
            self._title_parts = []
            self._title_href = get_attr(attrs, "href")
            return
        if has_class(attrs, "result__snippet"):
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
            return
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._title_parts is not None and tag == "a":
            self._pending_title = compact_text("".join(self._title_parts))
            self._pending_url = clean_duckduckgo_url(self._title_href)
            self._title_parts = None
            self._title_href = ""
            return

        if self._snippet_parts is not None and tag in {"a", "div"}:
            snippet = compact_text("".join(self._snippet_parts))
            self._snippet_parts = None
            self._append_pending(snippet)

    def close(self) -> None:
        super().close()
        self._flush_pending()

    def _append_pending(self, snippet: str) -> None:
        if self._pending_title and self._pending_url:
            self.results.append(
                WebSearchResult(
                    title=self._pending_title,
                    url=self._pending_url,
                    snippet=snippet,
                )
            )
        self._pending_title = ""
        self._pending_url = ""

    def _flush_pending(self) -> None:
        if self._pending_title and self._pending_url:
            self._append_pending("")


def parse_duckduckgo_results(html: str, limit: int) -> list[WebSearchResult]:
    parser = DuckDuckGoHTMLParser(limit)
    parser.feed(html)
    parser.close()
    return parser.results[:limit]


def build_duckduckgo_search_url(query: str) -> str:
    return f"https://html.duckduckgo.com/html/?q={quote(query, safe='')}"


def parse_quick_web_search(text: str) -> str | None:
    message = text.strip()
    for prefix in QUICK_WEB_SEARCH_PREFIXES:
        if not message.startswith(prefix):
            continue
        query = message[len(prefix):].strip()
        return query or None
    return None


def web_search_session_key(event: MessageEvent) -> tuple:
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        return ("group", group_id)
    return ("private", event.user_id)


async def search_web(
    http_client: httpx.AsyncClient,
    query: str,
    limit: int = WEB_SEARCH_MAX_RESULTS,
) -> list[WebSearchResult]:
    response = await http_client.get(build_duckduckgo_search_url(query))
    response.raise_for_status()
    return parse_duckduckgo_results(response.text, limit)


def build_web_answer_messages(
    query: str,
    results: list[WebSearchResult],
) -> list[dict[str, str]]:
    search_context = "\n\n".join(
        (
            f"[{index}] {result.title}\n"
            f"URL：{result.url}\n"
            f"摘要：{result.snippet or '（无摘要）'}"
        )
        for index, result in enumerate(results, start=1)
    )
    return [
        {
            "role": "system",
            "content": (
                "你在 QQ 群里基于联网搜索结果回答问题。"
                "只能使用给定搜索结果，不要编造未出现的事实；资料不足就直说没搜到可靠信息。"
                "回答要短，通常 2-5 句，口语自然，不要 Markdown 表格。"
                "涉及新闻、价格、版本、政策等时效信息时，提醒结果来自当前搜索。"
                "末尾用一行写来源，格式：来源：1 标题；2 标题。"
            ),
        },
        {
            "role": "user",
            "content": f"问题：{query}\n\n搜索结果：\n{search_context}",
        },
    ]


def format_search_results(results: list[WebSearchResult]) -> str:
    lines = ["搜到了这些："]
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. {result.title}\n{result.url}")
    return "\n".join(lines)


def trim_reply(text: str, max_chars: int = WEB_SEARCH_REPLY_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


async def answer_with_web_results(
    query: str,
    results: list[WebSearchResult],
) -> str:
    if client is None:
        return format_search_results(results)

    response = await client.chat.completions.create(
        model=model_name,
        messages=build_web_answer_messages(query, results),
    )
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        content = ""
    answer = str(content).strip() if content else format_search_results(results)
    return trim_reply(answer)


async def handle_web_search_request(matcher, event: MessageEvent, query: str) -> None:
    query = query.strip()
    if not query:
        await matcher.finish("用法：/联网 [要查的问题]，也可以直接说“联网 Python 最新版本”")

    retry_after = web_search_cooldown.retry_after(web_search_session_key(event))
    if retry_after is not None:
        await matcher.finish(f"联网查询太频繁了，请 {retry_after} 秒后再试")

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=WEB_SEARCH_HEADERS,
            timeout=WEB_SEARCH_TIMEOUT,
        ) as http_client:
            results = await search_web(http_client, query)
    except httpx.HTTPError as error:
        logger.warning("联网搜索失败：{}", error)
        await matcher.finish("❌ 联网搜索失败，请稍后重试")

    if not results:
        await matcher.finish("没搜到可用结果，换个关键词试试")

    try:
        answer = await answer_with_web_results(query, results)
    except Exception as error:
        logger.opt(exception=True).warning("联网回答生成失败：{}", error)
        answer = format_search_results(results)

    await matcher.finish(Message(answer))


web_search_cmd = on_command("联网", aliases={"搜索"}, priority=3, block=True)
quick_web_search_cmd = on_regex(QUICK_WEB_SEARCH_REGEX, priority=4, block=True)


@web_search_cmd.handle()
async def handle_web_search(event: MessageEvent, arg: Message = CommandArg()):
    await handle_web_search_request(web_search_cmd, event, arg.extract_plain_text())


@quick_web_search_cmd.handle()
async def handle_quick_web_search(
    event: MessageEvent,
    matched: tuple[str, ...] = RegexGroup(),
):
    query = matched[0] if matched else ""
    await handle_web_search_request(quick_web_search_cmd, event, query)
