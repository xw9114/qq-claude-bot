import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg


TITLE_MAX_LENGTH = 30
DISPLAY_NAME_MAX_LENGTH = 50
TITLE_MATCH_LIMIT = 5
DATABASE_PATH = Path("data") / "user_titles.db"

config = get_driver().config
TITLE_ADMINS = {
    str(user_id) for user_id in getattr(config, "title_admins", [])
}


@dataclass(frozen=True, slots=True)
class UserTitleRecord:
    user_id: int
    title: str
    display_name: str | None = None


class UserTitleStore:
    """基于 SQLite 的全局用户称号存储。"""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_titles (
                    user_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    display_name TEXT,
                    updated_by INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(user_titles)")
            }
            if "display_name" not in columns:
                connection.execute(
                    "ALTER TABLE user_titles ADD COLUMN display_name TEXT"
                )

    async def initialize(self) -> None:
        if self._initialized:
            return
        await asyncio.to_thread(self._initialize)
        self._initialized = True

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> UserTitleRecord | None:
        if row is None:
            return None
        display_name = row["display_name"]
        return UserTitleRecord(
            user_id=int(row["user_id"]),
            title=str(row["title"]),
            display_name=str(display_name) if display_name else None,
        )

    def _get_record(self, user_id: int) -> UserTitleRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id, title, display_name FROM user_titles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._row_to_record(row)

    async def get_record(self, user_id: int) -> UserTitleRecord | None:
        await self.initialize()
        return await asyncio.to_thread(self._get_record, user_id)

    async def get_title(self, user_id: int) -> str | None:
        record = await self.get_record(user_id)
        return record.title if record else None

    def _find_titles_in_text(self, text: str, limit: int) -> list[UserTitleRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, title, display_name
                FROM user_titles
                WHERE instr(?, title) > 0
                ORDER BY length(title) DESC, updated_at DESC
                LIMIT ?
                """,
                (text, limit),
            ).fetchall()
        return [
            record
            for record in (self._row_to_record(row) for row in rows)
            if record is not None
        ]

    async def find_titles_in_text(
        self, text: str, limit: int = TITLE_MATCH_LIMIT
    ) -> list[UserTitleRecord]:
        if not text.strip():
            return []
        await self.initialize()
        return await asyncio.to_thread(self._find_titles_in_text, text, limit)

    def _set_title(
        self,
        user_id: int,
        title: str,
        updated_by: int,
        display_name: str | None,
    ) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_titles (
                    user_id, title, display_name, updated_by, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    title = excluded.title,
                    display_name = COALESCE(
                        excluded.display_name,
                        user_titles.display_name
                    ),
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (user_id, title, display_name, updated_by, updated_at),
            )

    async def set_title(
        self,
        user_id: int,
        title: str,
        updated_by: int,
        display_name: str | None = None,
    ) -> None:
        await self.initialize()
        await asyncio.to_thread(
            self._set_title,
            user_id,
            title,
            updated_by,
            display_name,
        )

    def _set_display_name(self, user_id: int, display_name: str) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE user_titles
                SET display_name = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (display_name, updated_at, user_id),
            )

    async def set_display_name(self, user_id: int, display_name: str) -> None:
        await self.initialize()
        await asyncio.to_thread(self._set_display_name, user_id, display_name)

    def _delete_title(self, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM user_titles WHERE user_id = ?", (user_id,)
            )
        return cursor.rowcount > 0

    async def delete_title(self, user_id: int) -> bool:
        await self.initialize()
        return await asyncio.to_thread(self._delete_title, user_id)


title_store = UserTitleStore(DATABASE_PATH)


def normalize_title(title: str) -> str:
    normalized = " ".join(title.split())
    if not normalized:
        raise ValueError("称号不能为空")
    if len(normalized) > TITLE_MAX_LENGTH:
        raise ValueError(f"称号不能超过 {TITLE_MAX_LENGTH} 个字符")
    return normalized


def normalize_display_name(display_name: str | None) -> str | None:
    if display_name is None:
        return None
    normalized = " ".join(display_name.split())
    if not normalized:
        return None
    return normalized[:DISPLAY_NAME_MAX_LENGTH]


def parse_title_payload(payload: str) -> tuple[str, str | None]:
    title_text, separator, display_name = payload.partition("|")
    title = normalize_title(title_text)
    if not separator:
        return title, None
    return title, normalize_display_name(display_name)


def extract_at_user_id(args: Message) -> int | None:
    for segment in args:
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq", ""))
        if qq.isdigit():
            return int(qq)
    return None


def parse_target_and_title(args: Message) -> tuple[int, str, str | None]:
    target_id = extract_at_user_id(args)
    plain_text = args.extract_plain_text().strip()

    if target_id is not None:
        title, display_name = parse_title_payload(plain_text)
        return target_id, title, display_name

    parts = plain_text.split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        raise ValueError(
            "格式：/设置称号 QQ号 称号，或 /设置称号 @用户 称号；"
            "可用“称号 | 显示名”手动指定显示名"
        )
    title, display_name = parse_title_payload(parts[1])
    return int(parts[0]), title, display_name


def parse_target_id(args: Message, default_user_id: int | None = None) -> int:
    if target_id := extract_at_user_id(args):
        return target_id

    plain_text = args.extract_plain_text().strip()
    if plain_text.isdigit():
        return int(plain_text)
    if not plain_text and default_user_id is not None:
        return default_user_id
    raise ValueError("请提供正确的 QQ 号或使用真实的 QQ @")


async def require_title_admin(matcher: Matcher, event: MessageEvent) -> None:
    if str(event.user_id) not in TITLE_ADMINS:
        await matcher.finish("❌ 你没有管理用户称号的权限")


def format_record_label(record: UserTitleRecord) -> str:
    if record.display_name:
        return f"{record.display_name}（QQ {record.user_id}）"
    return f"QQ {record.user_id}"


async def resolve_group_display_name(
    bot: Bot,
    event: MessageEvent,
    user_id: int,
) -> str | None:
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        return None

    try:
        info: dict[str, Any] = await bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
            no_cache=True,
        )
    except Exception as error:
        logger.debug("获取群成员 {} 的显示名失败: {}", user_id, error)
        return None

    return normalize_display_name(
        str(info.get("card") or info.get("nickname") or "")
    )


async def enrich_record_display_name(
    record: UserTitleRecord,
    bot: Bot | None = None,
    event: MessageEvent | None = None,
) -> UserTitleRecord:
    if record.display_name or bot is None or event is None:
        return record

    display_name = await resolve_group_display_name(bot, event, record.user_id)
    if not display_name:
        return record

    await title_store.set_display_name(record.user_id, display_name)
    return replace(record, display_name=display_name)


async def get_user_title(user_id: int) -> str | None:
    return await title_store.get_title(user_id)


async def get_mentioned_title_records(
    message: str,
    bot: Bot | None = None,
    event: MessageEvent | None = None,
) -> list[UserTitleRecord]:
    matched_titles = await title_store.find_titles_in_text(message)
    return [
        await enrich_record_display_name(record, bot, event)
        for record in matched_titles
    ]


async def get_user_title_prompt(
    user_id: int,
    bot: Bot | None = None,
    event: MessageEvent | None = None,
) -> str:
    record = await title_store.get_record(user_id)
    if not record:
        return ""
    record = await enrich_record_display_name(record, bot, event)
    encoded_title = json.dumps(record.title, ensure_ascii=False)
    display_part = (
        f"，显示名是 {json.dumps(record.display_name, ensure_ascii=False)}"
        if record.display_name
        else ""
    )
    return (
        f"\n管理员为当前发言用户设置的称号是 {encoded_title}{display_part}。"
        "该称号是群内昵称/身份标签，可作为称呼、聊天背景和轻度玩笑依据；"
        "不要据此编造现实隐私、真实身份或经历。"
    )


async def get_mentioned_titles_prompt(
    message: str,
    bot: Bot | None = None,
    event: MessageEvent | None = None,
    matched_titles: list[UserTitleRecord] | None = None,
) -> str:
    if matched_titles is None:
        matched_titles = await get_mentioned_title_records(message, bot, event)
    if not matched_titles:
        return ""

    title_lines = []
    for record in matched_titles:
        title = json.dumps(record.title, ensure_ascii=False)
        if record.display_name:
            display_name = json.dumps(record.display_name, ensure_ascii=False)
            title_lines.append(f"- {title} 指的是 {display_name}")
        else:
            title_lines.append(f"- {title} 指的是 QQ {record.user_id}")

    return (
        "\n当前消息提到了以下已登记称号：\n"
        + "\n".join(title_lines)
        + "\n这些称号是管理员设置的群内昵称/身份标签。"
        "当用户用称号提问、聊天、调侃或评价时，可以把称号当作对应用户的代称来理解并自然回复。"
        "可以围绕称号本身做轻度玩笑、吐槽或夸奖；"
        "不要编造该用户的现实隐私、真实身份、经历或敏感信息。"
    )


set_title_cmd = on_command("设置称号", priority=3, block=True)
get_title_cmd = on_command("查看称号", priority=3, block=True)
delete_title_cmd = on_command("删除称号", priority=3, block=True)


@set_title_cmd.handle()
async def handle_set_title(
    matcher: Matcher,
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    await require_title_admin(matcher, event)
    try:
        target_id, title, display_name = parse_target_and_title(args)
    except ValueError as error:
        await matcher.finish(f"❌ {error}")
        return

    display_name = display_name or await resolve_group_display_name(
        bot, event, target_id
    )
    await title_store.set_title(target_id, title, event.user_id, display_name)
    label = format_record_label(UserTitleRecord(target_id, title, display_name))
    await matcher.finish(f"✅ 已将 {label} 的称号设置为「{title}」")


@get_title_cmd.handle()
async def handle_get_title(
    matcher: Matcher,
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    try:
        target_id = parse_target_id(args, event.user_id)
    except ValueError as error:
        await matcher.finish(f"❌ {error}")
        return

    record = await title_store.get_record(target_id)
    if not record:
        await matcher.finish(f"QQ {target_id} 尚未设置称号")
        return
    record = await enrich_record_display_name(record, bot, event)
    await matcher.finish(f"{format_record_label(record)} 的称号是「{record.title}」")


@delete_title_cmd.handle()
async def handle_delete_title(
    matcher: Matcher,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    await require_title_admin(matcher, event)
    try:
        target_id = parse_target_id(args)
    except ValueError as error:
        await matcher.finish(f"❌ {error}")
        return

    deleted = await title_store.delete_title(target_id)
    if not deleted:
        await matcher.finish(f"QQ {target_id} 尚未设置称号")
        return
    await matcher.finish(f"✅ 已删除 QQ {target_id} 的称号")


@get_driver().on_startup
async def initialize_title_store() -> None:
    await title_store.initialize()
    if TITLE_ADMINS:
        logger.info("用户称号功能已启用，共配置 {} 名管理员", len(TITLE_ADMINS))
    else:
        logger.warning("未配置 TITLE_ADMINS，用户称号将无法设置或删除")
