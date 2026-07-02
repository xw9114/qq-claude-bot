"""语音消息插件。

群聊优先走 NapCat 扩展接口 send_group_ai_record（QQ 官方 AI 声聊，
无需本地合成）；私聊或群接口不可用时回退到 edge-tts 在线合成，
以 OneBot record 消息段发送（NapCat 端需要 ffmpeg 做格式转换）。
"""

import base64
import time

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.log import logger
from nonebot.params import CommandArg
from plugins.command_cooldown import SessionCooldown

try:
    import edge_tts
except ImportError:  # 依赖可选：未安装时群聊仍可用 AI 声聊
    edge_tts = None

config = get_driver().config


def _read_str(name: str, default: str | None) -> str | None:
    value = getattr(config, name, default)
    if value is None:
        return default
    value = str(value).strip()
    return value or default


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


DEFAULT_AI_CHARACTER = _read_str("voice_ai_character", None)
DEFAULT_EDGE_VOICE = _read_str("voice_edge_voice", "zh-CN-XiaoxiaoNeural")
MAX_TEXT_LENGTH = _read_int("voice_max_text_len", 200)
VOICE_COMMAND_COOLDOWN = _read_float("voice_command_cooldown", 10.0)

# QQ AI 声聊角色列表是群维度数据，拉取一次后短期复用，避免每条语音都发包
AI_CHARACTER_CACHE_TTL = 10 * 60

# edge-tts 音色全名不好记，提供常用中文音色的短名映射
EDGE_VOICES = {
    "晓晓": "zh-CN-XiaoxiaoNeural",
    "晓伊": "zh-CN-XiaoyiNeural",
    "云希": "zh-CN-YunxiNeural",
    "云健": "zh-CN-YunjianNeural",
    "云扬": "zh-CN-YunyangNeural",
    "东北小贝": "zh-CN-liaoning-XiaobeiNeural",
    "陕西小妮": "zh-CN-shaanxi-XiaoniNeural",
}

group_ai_characters: dict[int, str] = {}   # 群 -> QQ AI 声聊角色 ID
session_edge_voices: dict[tuple, str] = {}  # 会话 -> edge-tts 音色
_ai_character_cache: dict[int, tuple[float, list]] = {}
voice_cooldown = SessionCooldown(VOICE_COMMAND_COOLDOWN)


def voice_session_key(event: MessageEvent) -> tuple:
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        return ("group", group_id)
    return ("private", event.user_id)


def normalize_ai_character_groups(raw: object) -> list[dict]:
    """把 get_ai_characters 的返回整理成 [{type, characters:[{id, name}]}]。

    接口返回结构来自 NapCat 扩展协议，字段缺失时跳过而不是报错，
    避免协议端版本差异直接炸掉整个指令。
    """
    groups = []
    if not isinstance(raw, list):
        return groups
    for item in raw:
        if not isinstance(item, dict):
            continue
        characters = []
        for char in item.get("characters") or []:
            if not isinstance(char, dict):
                continue
            char_id = str(char.get("character_id") or "").strip()
            char_name = str(char.get("character_name") or "").strip()
            if char_id and char_name:
                characters.append({"id": char_id, "name": char_name})
        if characters:
            groups.append({"type": str(item.get("type") or "其他"), "characters": characters})
    return groups


def resolve_ai_character(groups: list[dict], query: str) -> str | None:
    """按名称或 ID 精确匹配 AI 声聊角色，返回角色 ID。"""
    query = query.strip()
    if not query:
        return None
    for group in groups:
        for char in group["characters"]:
            if query in (char["id"], char["name"]):
                return char["id"]
    return None


def resolve_edge_voice(query: str) -> str | None:
    query = query.strip()
    if query in EDGE_VOICES:
        return EDGE_VOICES[query]
    # edge-tts 音色 ID 形如 zh-CN-XiaoxiaoNeural，直接透传给合成端校验
    if query.lower().startswith("zh-") and query.endswith("Neural"):
        return query
    return None


def format_voice_roles(groups: list[dict], in_group: bool) -> str:
    lines = ["🔊 可用语音角色", ""]
    if in_group:
        if groups:
            lines.append("【QQ AI 声聊】（仅群聊，音质好）")
            for group in groups:
                names = "、".join(char["name"] for char in group["characters"])
                lines.append(f"  {group['type']}：{names}")
        else:
            lines.append("【QQ AI 声聊】当前群暂不可用")
        lines.append("")
    lines.append("【在线合成】（群聊/私聊通用）")
    lines.append("  " + "、".join(EDGE_VOICES))
    lines.append("")
    lines.append("发送 /设置语音 [角色名] 切换音色")
    return "\n".join(lines)


async def fetch_ai_characters(bot: Bot, group_id: int) -> list[dict]:
    now = time.monotonic()
    cached = _ai_character_cache.get(group_id)
    if cached is not None and now - cached[0] < AI_CHARACTER_CACHE_TTL:
        return cached[1]
    raw = await bot.call_api(
        "get_ai_characters", group_id=str(group_id), chat_type=1
    )
    groups = normalize_ai_character_groups(raw)
    _ai_character_cache[group_id] = (now, groups)
    return groups


async def pick_group_character(bot: Bot, group_id: int) -> str | None:
    """确定群里用哪个 AI 声聊角色：群内设置 > 全局配置 > 列表第一个。"""
    character = group_ai_characters.get(group_id) or DEFAULT_AI_CHARACTER
    if character:
        return character
    try:
        groups = await fetch_ai_characters(bot, group_id)
    except (ActionFailed, NetworkError) as error:
        logger.warning("获取 AI 声聊角色列表失败：{}", error)
        return None
    if groups:
        return groups[0]["characters"][0]["id"]
    return None


async def synthesize_edge_audio(text: str, voice: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice=voice)
    buffer = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.extend(chunk["data"])
    if not buffer:
        raise RuntimeError("edge-tts 未返回音频数据")
    return bytes(buffer)


# ========== /语音 ==========
voice_cmd = on_command("语音", priority=3, block=True)


@voice_cmd.handle()
async def handle_voice(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    text = arg.extract_plain_text().strip()
    if not text:
        await voice_cmd.finish("用法：/语音 [要说的话]")
    if len(text) > MAX_TEXT_LENGTH:
        await voice_cmd.finish(f"❌ 文本太长了，最多 {MAX_TEXT_LENGTH} 字")

    retry_after = voice_cooldown.retry_after(voice_session_key(event))
    if retry_after is not None:
        await voice_cmd.finish(f"语音太频繁了，请 {retry_after} 秒后再试")

    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        character = await pick_group_character(bot, group_id)
        if character:
            try:
                await bot.call_api(
                    "send_group_ai_record",
                    group_id=str(group_id),
                    character=character,
                    text=text,
                )
                return
            except (ActionFailed, NetworkError) as error:
                # 群未开通 AI 声聊或协议端不支持时回退在线合成
                logger.warning("AI 声聊发送失败，回退 edge-tts：{}", error)

    if edge_tts is None:
        await voice_cmd.finish("❌ 语音功能未就绪：请安装 edge-tts（pip install edge-tts）")

    voice = session_edge_voices.get(voice_session_key(event), DEFAULT_EDGE_VOICE)
    try:
        audio = await synthesize_edge_audio(text, voice)
    except Exception as error:
        logger.warning("edge-tts 合成失败：{}", error)
        await voice_cmd.finish("❌ 语音合成失败，请稍后重试")
    encoded = base64.b64encode(audio).decode()
    await voice_cmd.finish(MessageSegment.record(f"base64://{encoded}"))


# ========== /语音角色 ==========
voice_roles_cmd = on_command("语音角色", aliases={"语音列表"}, priority=3, block=True)


@voice_roles_cmd.handle()
async def handle_voice_roles(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    groups: list[dict] = []
    if group_id is not None:
        try:
            groups = await fetch_ai_characters(bot, group_id)
        except (ActionFailed, NetworkError) as error:
            logger.warning("获取 AI 声聊角色列表失败：{}", error)
    await voice_roles_cmd.finish(
        Message(format_voice_roles(groups, in_group=group_id is not None))
    )


# ========== /设置语音 ==========
set_voice_cmd = on_command("设置语音", priority=3, block=True)


@set_voice_cmd.handle()
async def handle_set_voice(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    query = arg.extract_plain_text().strip()
    if not query:
        await set_voice_cmd.finish("用法：/设置语音 [角色名]，发送 /语音角色 查看可选项")

    edge_voice = resolve_edge_voice(query)
    if edge_voice is not None:
        session_edge_voices[voice_session_key(event)] = edge_voice
        await set_voice_cmd.finish(f"✅ 在线合成音色已切换为【{query}】")

    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        try:
            groups = await fetch_ai_characters(bot, group_id)
        except (ActionFailed, NetworkError) as error:
            logger.warning("获取 AI 声聊角色列表失败：{}", error)
            groups = []
        character = resolve_ai_character(groups, query)
        if character is not None:
            group_ai_characters[group_id] = character
            await set_voice_cmd.finish(f"✅ 本群 AI 声聊角色已切换为【{query}】")

    await set_voice_cmd.finish("❌ 没找到这个角色，发送 /语音角色 查看可选项")
