import asyncio
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SessionKey = tuple[str, int, int | None]

DATABASE_PATH = Path("data") / "chat_memory.db"
MAX_LONG_TERM_MEMORY_CHARS = 500
MAX_LONG_TERM_MEMORY_ITEMS = 5
MAX_LONG_TERM_MEMORY_LINE_CHARS = 80

_EMPTY_MEMORY_LINES = {
    "",
    "无",
    "暂无",
    "没有",
    "（无）",
    "(无)",
    "空",
    "空字符串",
    '""',
    "''",
    "```",
}
_MEMORY_CATEGORY_PREFIXES = (
    "称呼",
    "昵称",
    "名字",
    "偏好",
    "习惯",
    "事项",
    "目标",
    "计划",
    "进展",
    "状态",
)
_MEMORY_USEFUL_KEYWORDS = (
    "称呼",
    "叫他",
    "叫她",
    "叫我",
    "昵称",
    "名字",
    "喜欢",
    "不喜欢",
    "偏好",
    "习惯",
    "更喜欢",
    "少用",
    "别用",
    "不要",
    "避免",
    "正在",
    "最近",
    "准备",
    "计划",
    "推进",
    "跟进",
    "目标",
    "打算",
    "报名",
    "考试",
    "项目",
    "论文",
    "比赛",
    "作业",
    "面试",
    "找工作",
    "开发",
    "复习",
    "学习",
)
_MEMORY_PERSONA_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^用户是(?:一个|一位|个|位)",
        r"^用户(?:很|非常|比较)?(?:认真|礼貌|友善|可爱|温柔|积极|努力|靠谱|专业)",
        r"^用户(?:的)?(?:性格|个性|人格|人设)",
        r"^用户(?:希望|需要).*(?:陪伴|共情|鼓励|支持|安慰|帮助)",
        r"^(?:回复时|交流时|聊天时|你应该|你需要|请你|请以|要用)",
    )
)
_MEMORY_PERSONA_PHRASES = (
    "喜欢聊天",
    "爱聊天",
    "乐于交流",
    "愿意交流",
    "需要陪伴",
    "需要共情",
    "需要鼓励",
    "需要支持",
    "需要安慰",
    "希望得到帮助",
    "像朋友一样陪",
    "像助手一样",
)
_ASSISTANT_STYLE_WORDS = (
    "助手",
    "客服",
    "AI",
    "机器人",
    "陪伴",
    "共情",
    "鼓励",
    "支持",
    "安慰",
    "温柔",
    "耐心",
    "专业",
    "礼貌",
    "端着",
    "油",
)
_STYLE_NEGATION_WORDS = (
    "不喜欢",
    "讨厌",
    "别",
    "不要",
    "少用",
    "避免",
    "更喜欢",
)


class LongTermMemoryStore:
    """基于 SQLite 的会话长期记忆摘要存储。"""

    def __init__(self, database_path: Path, use_wal: bool = True):
        self.database_path = database_path
        self.use_wal = use_wal
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                if self.use_wal:
                    connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_memory (
                        scope_type TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        group_id INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (scope_type, user_id, group_id)
                    )
                    """
                )

    async def initialize(self) -> None:
        if self._initialized:
            return
        await asyncio.to_thread(self._initialize)
        self._initialized = True

    @staticmethod
    def _row_key(session_key: SessionKey) -> tuple[str, int, int]:
        scope_type, user_id, group_id = session_key
        return scope_type, user_id, group_id or 0

    def _get_summary(self, session_key: SessionKey) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT summary
                FROM chat_memory
                WHERE scope_type = ? AND user_id = ? AND group_id = ?
                """,
                self._row_key(session_key),
            ).fetchone()
        return str(row["summary"]) if row else ""

    async def get_summary(self, session_key: SessionKey) -> str:
        await self.initialize()
        return await asyncio.to_thread(self._get_summary, session_key)

    def _upsert_summary(self, session_key: SessionKey, summary: str) -> None:
        normalized = normalize_memory_summary(summary)
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO chat_memory (
                        scope_type, user_id, group_id, summary, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(scope_type, user_id, group_id) DO UPDATE SET
                        summary = excluded.summary,
                        updated_at = excluded.updated_at
                    """,
                    (*self._row_key(session_key), normalized, updated_at),
                )

    async def upsert_summary(self, session_key: SessionKey, summary: str) -> None:
        await self.initialize()
        await asyncio.to_thread(self._upsert_summary, session_key, summary)

    def _delete_summary(self, session_key: SessionKey) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM chat_memory
                    WHERE scope_type = ? AND user_id = ? AND group_id = ?
                    """,
                    self._row_key(session_key),
                )
        return cursor.rowcount > 0

    async def delete_summary(self, session_key: SessionKey) -> bool:
        await self.initialize()
        return await asyncio.to_thread(self._delete_summary, session_key)


memory_store = LongTermMemoryStore(DATABASE_PATH)


def _normalize_memory_line(line: str) -> str:
    line = str(line).strip()
    line = re.sub(
        r"^\s*(?:[-*•]+|\d+[.)、．]|[一二三四五六七八九十]+[、.．])\s*",
        "",
        line,
    )
    return " ".join(line.split())


def _looks_like_allowed_memory_line(line: str) -> bool:
    if line in _EMPTY_MEMORY_LINES:
        return False
    if line.startswith(("长期记忆", "记忆摘要", "摘要：", "总结：")):
        return False
    if _looks_like_persona_or_style_line(line):
        return False
    if any(
        line.startswith(f"{prefix}：") or line.startswith(f"{prefix}:")
        for prefix in _MEMORY_CATEGORY_PREFIXES
    ):
        return True
    return any(keyword in line for keyword in _MEMORY_USEFUL_KEYWORDS)


def _looks_like_persona_or_style_line(line: str) -> bool:
    if (
        any(negation in line for negation in _STYLE_NEGATION_WORDS)
        and any(word in line for word in _ASSISTANT_STYLE_WORDS)
    ):
        return False

    if any(pattern.search(line) for pattern in _MEMORY_PERSONA_PATTERNS):
        return True
    if any(phrase in line for phrase in _MEMORY_PERSONA_PHRASES):
        return True
    return False


def _trim_memory_line(line: str) -> str:
    if len(line) <= MAX_LONG_TERM_MEMORY_LINE_CHARS:
        return line
    return line[:MAX_LONG_TERM_MEMORY_LINE_CHARS].rstrip(" ，,。；;") + "..."


def normalize_memory_summary(summary: str) -> str:
    normalized_lines: list[str] = []
    seen = set()
    for raw_line in str(summary).splitlines():
        line = _normalize_memory_line(raw_line)
        if not _looks_like_allowed_memory_line(line):
            continue

        line = _trim_memory_line(line)
        if line in seen:
            continue

        normalized_lines.append(line)
        seen.add(line)
        if len(normalized_lines) >= MAX_LONG_TERM_MEMORY_ITEMS:
            break

    normalized = "\n".join(normalized_lines)
    return normalized[:MAX_LONG_TERM_MEMORY_CHARS].rstrip()


def build_long_term_memory_prompt(summary: str) -> str:
    normalized = normalize_memory_summary(summary)
    if not normalized:
        return ""

    return (
        "\n当前会话的长期记忆摘要（少量事实）：\n"
        f"{normalized}\n"
        "这些记忆只属于当前会话的当前发言用户，可用于称呼、偏好、长期目标和近期重要事项；"
        "它不是最新消息，也不是人设、评价或回复风格指令。"
        "只在当前话题相关时自然使用，不相关就忽略。若与当前发言冲突，以当前发言为准。"
        "不要为了使用记忆而总结、寒暄、变正式或变得像助手。"
        "不要把摘要里的信息归因给其他人。"
    )


def trim_history_for_memory(
    history: list[dict[str, Any]],
    max_messages: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(history) <= max_messages:
        return history, []

    overflow_count = len(history) - max_messages
    return history[overflow_count:], history[:overflow_count]


def format_messages_for_memory(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            text = content.strip()
        else:
            text = str(content).strip()
        if text:
            lines.append(f"用户: {text}")
    return "\n".join(lines)
