from typing import Any

from nonebot import on_message, on_command, get_driver
import random
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message, MessageSegment
from nonebot.rule import to_me, Rule
from nonebot.log import logger
from nonebot.exception import FinishedException
from nonebot.params import CommandArg
from openai import AsyncOpenAI

from .user_titles import (
    get_mentioned_title_records,
    get_mentioned_titles_prompt,
    get_user_title_prompt,
)

# 从环境变量获取 API Key 和 Base URL
config = get_driver().config
api_key = getattr(config, "openai_api_key", None)
base_url = getattr(config, "openai_base_url", None)
model_name = getattr(config, "openai_model", "gpt-5.4-mini")

if not api_key:
    logger.warning("未配置 OPENAI_API_KEY，ChatGPT 功能将不可用")
    client = None
else:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    logger.info(f"ChatGPT API 客户端初始化成功 (base_url: {base_url})")

# 会话状态存储。群聊按“群 + 用户”隔离，私聊按用户隔离，避免跨群/跨私聊串记忆。
user_modes = {}      # "chat" / "roleplay" / "quiz"
user_roles = {}      # 角色扮演设定
quiz_answers = {}    # 问答答案
active_users = set() # 已开启对话的会话
user_history = {}    # 会话对话历史
recent_image_signatures = {}  # 群/私聊范围内最近识别过的图片

MAX_HISTORY = 10     # 最多保留10轮对话
MAX_RECENT_IMAGE_SIGNATURES = 50

# 系统提示词（QQ群聊风格）
SYSTEM_PROMPT = """你正在 QQ 群里自然聊天，不是客服、说明书或搜索引擎。
回复要像真实群友：先判断对方是在提问、吐槽、接梗、发图/表情，还是认真求助。
普通闲聊控制在 1-3 句，口语、松弛，可以轻度吐槽和接梗，但不要硬玩梗、尬夸或说教。
认真问题直接给可执行答案；信息不足时先指出关键缺口，再给一个最可能的判断。
当用户发送图片时，你可以直接观察图片内容；如果图片无法访问，再根据图片说明和上下文说明限制。
历史消息只属于当前会话里的当前发言用户；不要把某人的“我……”归因给其他人。
避免模板化开场、频繁感叹号和过量 emoji。"""

# 角色预设
ROLES = {
    "1": ("侦探柯南", "你是少年侦探柯南，说话逻辑严谨，偶尔说'真相只有一个！'，回复简短。"),
    "2": ("猫娘", "你是可爱猫娘，句尾加'喵~'，回复简短活泼。"),
    "3": ("古代谋士", "你是古代谋士，说话文绉绉，善用典故，回复简短。"),
    "4": ("毒舌导师", "你是毒舌导师，说话犀利直接，回复简短。"),
}

# ========== 帮助指令 ==========
help_cmd = on_command("help", aliases={"帮助"}, priority=3, block=True)


def conversation_key(event: MessageEvent) -> tuple[str, int, int | None]:
    """同一用户在不同群/私聊中使用独立上下文。"""
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        return ("group", event.user_id, group_id)
    return ("private", event.user_id, None)


def image_cache_key(event: MessageEvent) -> tuple[str, int | None]:
    group_id = getattr(event, "group_id", None)
    return ("group", group_id) if group_id is not None else ("private", event.user_id)

@help_cmd.handle()
async def handle_help(event: MessageEvent):
    msg = """🤖 可用指令一览

━━━━━━━━━━━━━━━
💬 AI 对话
  你好         开启对话模式
  再见 / 拜拜  结束对话模式
  （开启后直接发消息即可聊天）

━━━━━━━━━━━━━━━
🎭 角色扮演
  /角色扮演    选择角色（柯南/猫娘/谋士/毒舌）
  /退出角色    退出当前角色

━━━━━━━━━━━━━━━
🧠 问答竞赛
  /问答        出题开始竞赛
  /答案 [内容] 提交你的答案
  /退出问答    结束问答

━━━━━━━━━━━━━━━
🏷 用户称号
  /设置称号 [QQ号/@用户] [称号]  管理员设置称号
  /查看称号 [QQ号/@用户]         查看称号（留空查看自己）
  /删除称号 [QQ号/@用户]         管理员删除称号

━━━━━━━━━━━━━━━
🎮 娱乐
  /抽老婆      随机二次元老婆（每次不同）
  /塔罗        塔罗牌占卜
  /笑话        讲个笑话
  一言         随机一句话

━━━━━━━━━━━━━━━
🖼 表情包 & 梗图
  摸 @某人     生成摸头 GIF
  拍 @某人     生成拍打 GIF
  亲 @某人     生成亲亲 GIF
  膜 @某人     生成膜拜 GIF
  /鲁迅说 [文字]  经典鲁迅梗图
  /举牌 [文字]  举牌表情包
  /头像相关表情包  查看 PetPet 动图指令
  /表情包制作      查看 Memes 梗图模板
  /表情帮助 [关键词] 查看指定梗图详情

━━━━━━━━━━━━━━━
♟️ 棋类游戏（群聊，需 @机器人）
  @我 /五子棋  发起五子棋对局
  @我 /围棋    发起围棋对局
  @我 /黑白棋  发起黑白棋对局
  /落子 A1     落子（字母+数字坐标）
  /悔棋        悔棋
  /显示棋盘    查看当前棋盘
  /结束下棋    结束对局

━━━━━━━━━━━━━━━
❓ /help       显示此帮助"""
    await help_cmd.finish(Message(msg))

# ========== 角色扮演 ==========
roleplay_cmd = on_command("角色扮演", priority=3, block=True)

@roleplay_cmd.handle()
async def handle_roleplay(event: MessageEvent):
    session_key = conversation_key(event)
    user_modes[session_key] = "selecting_role"
    await roleplay_cmd.finish(Message("🎭 选择角色：\n1️⃣ 侦探柯南\n2️⃣ 猫娘\n3️⃣ 古代谋士\n4️⃣ 毒舌导师\n\n回复数字选择"))

exit_role_cmd = on_command("退出角色", priority=3, block=True)

@exit_role_cmd.handle()
async def handle_exit_role(event: MessageEvent):
    session_key = conversation_key(event)
    user_modes.pop(session_key, None)
    user_roles.pop(session_key, None)
    await exit_role_cmd.finish("✅ 已退出角色扮演")

# ========== 问答竞赛 ==========
quiz_cmd = on_command("问答", priority=3, block=True)

@quiz_cmd.handle()
async def handle_quiz(event: MessageEvent):
    if not client:
        await quiz_cmd.finish("❌ API 未配置")
        return
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "出一道有趣的知识问答题，格式：\n题目：xxx\n答案：xxx\n只输出这两行"}]
        )
        text = response.choices[0].message.content
        lines = text.strip().split("\n")
        question = lines[0].replace("题目：", "").strip()
        answer = lines[1].replace("答案：", "").strip()
        session_key = conversation_key(event)
        user_modes[session_key] = "quiz"
        quiz_answers[session_key] = answer
        await quiz_cmd.finish(Message(f"🧠 问答开始！\n\n❓ {question}\n\n用 /答案 [你的答案] 回答"))
    except FinishedException:
        pass
    except Exception as e:
        await quiz_cmd.finish("❌ 出题失败，请重试")

answer_cmd = on_command("答案", priority=3, block=True)

@answer_cmd.handle()
async def handle_answer(event: MessageEvent, args: Message = CommandArg()):
    session_key = conversation_key(event)
    if user_modes.get(session_key) != "quiz":
        await answer_cmd.finish("❌ 未开始问答，发送 /问答 开始")
        return
    user_answer = args.extract_plain_text().strip()
    if not user_answer:
        await answer_cmd.finish("请输入答案：/答案 xxx")
        return
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": f"正确答案'{quiz_answers[session_key]}'，用户答'{user_answer}'，只回复：✅ 回答正确！或 ❌ 错误，答案是xxx"}]
        )
        result = response.choices[0].message.content
        user_modes.pop(session_key, None)
        quiz_answers.pop(session_key, None)
        await answer_cmd.finish(Message(f"{result}\n\n发送 /问答 继续"))
    except FinishedException:
        pass
    except Exception as e:
        await answer_cmd.finish("❌ 判题失败，请重试")

exit_quiz_cmd = on_command("退出问答", priority=3, block=True)

@exit_quiz_cmd.handle()
async def handle_exit_quiz(event: MessageEvent):
    session_key = conversation_key(event)
    user_modes.pop(session_key, None)
    quiz_answers.pop(session_key, None)
    await exit_quiz_cmd.finish("✅ 已退出问答")

# ========== 塔罗牌占卜 ==========
TAROT_CARDS = [
    "愚者", "魔术师", "女祭司", "女皇", "皇帝", "教皇", "恋人",
    "战车", "力量", "隐者", "命运之轮", "正义", "倒吊人", "死神",
    "节制", "恶魔", "塔", "星星", "月亮", "太阳", "审判", "世界"
]

tarot_cmd = on_command("塔罗", aliases={"占卜", "抽牌"}, priority=3, block=True)

@tarot_cmd.handle()
async def handle_tarot(event: MessageEvent):
    if not client:
        await tarot_cmd.finish("❌ API 未配置")
        return
    card = random.choice(TAROT_CARDS)
    position = "逆位" if random.choice([True, False]) else "正位"
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": f"塔罗牌【{card}】{position}，神秘语气解读，50字以内"}]
        )
        await tarot_cmd.finish(Message(f"🎴【{card}】{position}\n\n{response.choices[0].message.content}"))
    except FinishedException:
        pass
    except Exception as e:
        await tarot_cmd.finish("❌ 占卜失败，请重试")

# ========== 讲笑话 ==========
joke_cmd = on_command("笑话", aliases={"joke"}, priority=3, block=True)

@joke_cmd.handle()
async def handle_joke(event: MessageEvent):
    if not client:
        await joke_cmd.finish("❌ API 未配置")
        return
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "讲一个简短笑话，有反转结尾，不超过80字"}]
        )
        await joke_cmd.finish(Message(f"😄 {response.choices[0].message.content}"))
    except FinishedException:
        pass
    except Exception as e:
        await joke_cmd.finish("❌ 失败，请重试")

# ========== 普通聊天 ==========
# 结束对话词（优先检查）
async def bye_rule(event: MessageEvent) -> bool:
    msg = event.get_plaintext().strip()
    return any(kw in msg for kw in ["再见", "拜拜", "bye", "goodbye"])

# 触发词开启对话（且不是结束词）
async def greet_rule(event: MessageEvent) -> bool:
    msg = event.get_plaintext().strip()
    if await bye_rule(event):
        return False
    return any(kw in msg for kw in ["你好", "hello", "hi"])

# 聊天模式不应接管的插件指令。部分插件允许省略命令前缀，
# 因此这里同时保留帮助中公开的裸指令入口。
BLOCKED_KEYWORDS = {
    # 娱乐
    "抽老婆", "随机老婆", "抽wife", "随机wife", "今日wife", "今日老婆",
    "塔罗", "占卜", "抽牌", "笑话", "joke", "一言", "一句",
    # 表情包与梗图
    "摸", "拍", "亲", "膜",
    # 棋类游戏
    "五子棋", "围棋", "黑白棋", "奥赛罗", "落子", "悔棋",
    "显示棋盘", "显示棋局", "查看棋盘", "查看棋局",
    "结束下棋", "结束游戏", "结束象棋", "跳过", "跳过回合",
    "重载棋局", "恢复棋局",
}


def is_plugin_command(message: str) -> bool:
    """判断消息是否应交给命令或功能插件处理。"""
    msg = message.strip()
    if msg.startswith("/"):
        return True

    for keyword in BLOCKED_KEYWORDS:
        if not msg.startswith(keyword):
            continue
        suffix = msg[len(keyword):]
        if not suffix or suffix[0].isspace():
            return True

    # 棋类开局指令支持紧跟执棋顺序，例如“五子棋后手”。
    return any(
        msg == f"{game}{order}"
        for game in ("五子棋", "围棋", "黑白棋", "奥赛罗")
        for order in ("先手", "执白", "后手", "执黑")
    )

async def active_rule(event: MessageEvent) -> bool:
    if conversation_key(event) not in active_users:
        return False
    if await greet_rule(event) or await bye_rule(event):
        return False
    return not is_plugin_command(event.get_plaintext())

async def not_blocked(event: MessageEvent) -> bool:
    return not is_plugin_command(event.get_plaintext())

bye_chat = on_message(rule=Rule(bye_rule), priority=4, block=True)
greet_chat = on_message(rule=Rule(greet_rule), priority=5, block=True)
chat_at = on_message(rule=to_me() & Rule(not_blocked), priority=15, block=True)
active_chat = on_message(rule=Rule(active_rule), priority=16, block=True)


def build_chat_reply_message(
    reply: str,
    event: MessageEvent,
    mentioned_title_records,
) -> Message:
    reply_message = Message(reply)
    if getattr(event, "group_id", None) is None or not mentioned_title_records:
        return reply_message

    target_id = mentioned_title_records[0].user_id
    return MessageSegment.at(target_id) + " " + reply_message


def normalize_segment_text(text: Any) -> str:
    return str(text).strip()


def compact_segment_text(text: Any) -> str:
    """压缩 CQ 元数据空白，避免无意义换行污染模型上下文。"""
    return " ".join(str(text).split())


def normalize_segment_summary(summary: Any) -> str:
    text = compact_segment_text(summary)
    if len(text) >= 2 and text.startswith("[") and text.endswith("]"):
        return text[1:-1].strip()
    return text


def describe_non_text_segment(segment: MessageSegment) -> str:
    data = segment.data
    segment_type = segment.type

    if segment_type == "at":
        qq = str(data.get("qq", "")).strip()
        return "@全体成员" if qq == "all" else f"@{qq}" if qq else "[@某人]"

    if segment_type == "face":
        face_id = str(data.get("id", "")).strip()
        return f"[QQ表情:{face_id}]" if face_id else "[QQ表情]"

    if segment_type == "image":
        summary = normalize_segment_summary(data.get("summary", ""))
        return f"[图片：{summary}]" if summary else "[图片]"

    if segment_type == "record":
        return "[语音消息]"

    if segment_type == "video":
        return "[视频消息]"

    if segment_type == "file":
        name = compact_segment_text(data.get("name", ""))
        return f"[文件：{name}]" if name else "[文件]"

    if segment_type == "reply":
        message_id = str(data.get("id", "")).strip()
        return f"[回复消息:{message_id}]" if message_id else "[回复消息]"

    if segment_type in {"json", "xml"}:
        return "[卡片消息]"

    return f"[{segment_type}消息]"


def get_image_url(segment: MessageSegment) -> str | None:
    if segment.type != "image":
        return None

    url = str(segment.data.get("url", "")).strip()
    return url or None


def get_image_signature(segment: MessageSegment) -> str | None:
    if segment.type != "image":
        return None

    for key in ("file_unique", "file", "md5", "url"):
        value = str(segment.data.get(key, "")).strip()
        if value:
            return f"{key}:{value}"

    summary = normalize_segment_summary(segment.data.get("summary", ""))
    return f"summary:{summary}" if summary else None


def is_recent_image(cache_key: tuple[str, int | None], signature: str) -> bool:
    signatures = recent_image_signatures.setdefault(cache_key, [])
    if signature in signatures:
        return True

    signatures.append(signature)
    if len(signatures) > MAX_RECENT_IMAGE_SIGNATURES:
        del signatures[: len(signatures) - MAX_RECENT_IMAGE_SIGNATURES]
    return False


def format_user_message(message: Message) -> str:
    """把 OneBot 消息段转成模型可理解的聊天文本。"""
    parts: list[str] = []
    for segment in message:
        if segment.type == "text":
            text = normalize_segment_text(segment.data.get("text", ""))
            if text:
                parts.append(text)
            continue

        parts.append(describe_non_text_segment(segment))

    return " ".join(part for part in parts if part).strip()


def build_user_message_content(
    message: Message,
    cache_key: tuple[str, int | None] | None = None,
) -> str | list[dict[str, Any]]:
    """构造 OpenAI 兼容的多模态用户消息内容。"""
    content: list[dict[str, Any]] = []
    text_parts: list[str] = []

    def flush_text_parts() -> None:
        if not text_parts:
            return
        text = " ".join(text_parts).strip()
        if text:
            content.append({"type": "text", "text": text})
        text_parts.clear()

    for segment in message:
        if segment.type == "text":
            text = normalize_segment_text(segment.data.get("text", ""))
            if text:
                text_parts.append(text)
            continue

        image_url = get_image_url(segment)
        image_signature = get_image_signature(segment)
        if (
            image_url
            and image_signature
            and cache_key is not None
            and is_recent_image(cache_key, image_signature)
        ):
            text_parts.append(f"{describe_non_text_segment(segment)}（重复图片，已跳过识别）")
            continue

        if image_url:
            flush_text_parts()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                }
            )
            summary = describe_non_text_segment(segment)
            if summary != "[图片]":
                text_parts.append(summary)
            continue

        text_parts.append(describe_non_text_segment(segment))

    flush_text_parts()

    if not content:
        return ""
    if len(content) == 1 and content[0]["type"] == "text":
        return str(content[0]["text"])
    return content


async def process_chat(matcher, bot: Bot, event: MessageEvent):
    if not client:
        await matcher.finish("❌ API 未配置")
        return

    user_id = event.user_id
    session_key = conversation_key(event)
    plain_user_msg = event.get_plaintext().strip()
    event_message = event.get_message()
    user_msg = format_user_message(event_message)
    current_user_content = build_user_message_content(
        event_message,
        image_cache_key(event),
    )

    if not user_msg or not current_user_content:
        return

    # 处理角色选择
    if user_modes.get(session_key) == "selecting_role":
        if plain_user_msg in ROLES:
            role_name, role_prompt = ROLES[plain_user_msg]
            user_roles[session_key] = role_prompt
            user_modes[session_key] = "roleplay"
            await matcher.finish(f"🎭 已切换为【{role_name}】，发送 /退出角色 可退出")
        else:
            await matcher.finish("请回复 1-4 选择角色")
        return

    try:
        # 构建系统提示
        system = SYSTEM_PROMPT
        if user_modes.get(session_key) == "roleplay" and session_key in user_roles:
            system = user_roles[session_key]
        system += await get_user_title_prompt(user_id, bot, event)
        mentioned_title_records = await get_mentioned_title_records(
            user_msg, bot, event
        )
        system += await get_mentioned_titles_prompt(
            user_msg, bot, event, mentioned_title_records
        )

        # 初始化历史
        if session_key not in user_history:
            user_history[session_key] = []

        # 当前轮可携带图片；历史只保留文字摘要，避免旧图片反复进入上下文。
        messages = (
            [{"role": "system", "content": system}]
            + user_history[session_key]
            + [{"role": "user", "content": current_user_content}]
        )

        response = await client.chat.completions.create(
            model=model_name, messages=messages
        )
        reply = response.choices[0].message.content

        # 保存用户消息摘要到历史
        user_history[session_key].append({"role": "user", "content": user_msg})

        # 保存助手回复到历史
        user_history[session_key].append({"role": "assistant", "content": reply})

        # 限制历史长度
        if len(user_history[session_key]) > MAX_HISTORY * 2:
            user_history[session_key] = user_history[session_key][-MAX_HISTORY * 2:]

        logger.info(f"回复: {reply[:80]}...")
        await matcher.finish(
            build_chat_reply_message(reply, event, mentioned_title_records)
        )
    except FinishedException:
        pass
    except Exception as e:
        logger.opt(exception=True).error("调用 API 失败: {}", e)
        await matcher.finish("❌ 调用失败，请稍后重试")

@greet_chat.handle()
async def handle_greet(bot: Bot, event: MessageEvent):
    active_users.add(conversation_key(event))
    await process_chat(greet_chat, bot, event)

@active_chat.handle()
async def handle_active(bot: Bot, event: MessageEvent):
    await process_chat(active_chat, bot, event)

@bye_chat.handle()
async def handle_bye(event: MessageEvent):
    session_key = conversation_key(event)
    active_users.discard(session_key)
    user_modes.pop(session_key, None)
    user_roles.pop(session_key, None)
    user_history.pop(session_key, None)
    await bye_chat.finish("👋 再见！有需要随时说'你好'找我")

@chat_at.handle()
async def handle_chat_at(bot: Bot, event: MessageEvent):
    active_users.add(conversation_key(event))
    await process_chat(chat_at, bot, event)
