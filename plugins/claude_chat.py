import asyncio
import re
import time
from typing import Any

from nonebot import on_message, on_command, get_driver
import random
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message, MessageSegment
from nonebot.rule import to_me, Rule
from nonebot.log import logger
from nonebot.exception import FinishedException
from nonebot.params import CommandArg
from openai import AsyncOpenAI

from .chat_memory import (
    build_long_term_memory_prompt,
    format_messages_for_memory,
    memory_store,
    normalize_memory_summary,
    trim_history_for_memory,
)
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
session_locks = {}   # 会话串行锁，避免同一会话并发写乱历史
memory_update_locks = {}  # 长期记忆后台更新锁
memory_update_tasks = set()  # 正在运行的长期记忆后台任务
memory_update_task_counts = {}  # 每会话待处理长期记忆任务数
memory_update_generations = {}  # 清除记忆后阻止旧后台任务回写
recent_image_signatures = {}  # 群/私聊范围内最近识别过的图片
session_last_seen = {}  # 会话最近活跃时间
image_cache_last_seen = {}  # 图片缓存最近活跃时间
runtime_cleanup_task = None

MAX_HISTORY = 10     # 最多保留10轮对话
MAX_RECENT_IMAGE_SIGNATURES = 50
MAX_MEMORY_SOURCE_CHARS = 2500
SESSION_STATE_TTL_SECONDS = 6 * 60 * 60
IMAGE_CACHE_TTL_SECONDS = 3 * 60 * 60
RUNTIME_CLEANUP_INTERVAL_SECONDS = 30 * 60

# 系统提示词（QQ群聊风格）
SYSTEM_PROMPT = """你就是 QQ 群里的一个普通群友，不是客服，也不要表现得像 AI 助手。
先接住对方当前这句话，再决定是否展开。

闲聊规则：
- 默认只回 1 句，通常 10-50 字；对方明显在认真提问时才适当展开。
- 用自然口语，顺着对方的用词和情绪说。可以轻吐槽、接梗、自嘲，但别强行搞笑。
- 不复述问题，不做“我理解你的感受”式套路共情，不总结，不主动列清单，不上价值。
- 禁用“如果你愿意”“如果需要”“希望能帮到你”“有问题随时问我”这类助手尾巴。
- 少用“可能、建议、首先、其次、总之”，少用感叹号、emoji 和 Markdown。
- 对方只是在分享近况时，像熟人一样回应；只有明确求助时才给具体办法。

认真问题规则：
- 直接回答核心问题，信息不足就指出缺口；不要为了显得专业而堆术语。
- 不知道就直说不知道，不编造事实或图片细节。

上下文规则：
- 图片能访问时直接看图回应；无法访问时再说明限制，不要假装看见。
- 历史只属于当前会话的当前发言用户；不要把某人的“我……”归因给其他人。"""

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


def build_style_prompt(session_key) -> str:
    role_prompt = user_roles.get(session_key)
    if not role_prompt or user_modes.get(session_key) != "roleplay":
        return SYSTEM_PROMPT
    return (
        f"{SYSTEM_PROMPT}\n\n当前角色设定：{role_prompt}\n"
        "角色设定只影响口吻，不改变上面的聊天节奏和上下文规则。"
    )


def get_session_lock(session_key) -> asyncio.Lock:
    lock = session_locks.get(session_key)
    if lock is None:
        lock = asyncio.Lock()
        session_locks[session_key] = lock
    return lock


def get_memory_update_lock(session_key) -> asyncio.Lock:
    lock = memory_update_locks.get(session_key)
    if lock is None:
        lock = asyncio.Lock()
        memory_update_locks[session_key] = lock
    return lock


def drop_idle_session_lock(session_key) -> None:
    lock = session_locks.get(session_key)
    if lock is not None and not lock.locked():
        session_locks.pop(session_key, None)


def has_runtime_session_state(session_key) -> bool:
    return (
        session_key in active_users
        or session_key in user_modes
        or session_key in user_roles
        or session_key in quiz_answers
        or session_key in user_history
        or session_key in session_last_seen
    )


def cleanup_idle_session_locks() -> int:
    idle_locks = [
        session_key
        for session_key, lock in session_locks.items()
        if not lock.locked() and not has_runtime_session_state(session_key)
    ]
    for session_key in idle_locks:
        session_locks.pop(session_key, None)
    return len(idle_locks)


def bump_memory_update_generation(session_key) -> None:
    memory_update_generations[session_key] = (
        memory_update_generations.get(session_key, 0) + 1
    )


def drop_idle_memory_update_state(session_key) -> None:
    if memory_update_task_counts.get(session_key, 0) > 0:
        return

    lock = memory_update_locks.get(session_key)
    if lock is not None and not lock.locked():
        memory_update_locks.pop(session_key, None)
    if not has_runtime_session_state(session_key):
        memory_update_generations.pop(session_key, None)


def finish_memory_update_task(session_key, task: asyncio.Task) -> None:
    memory_update_tasks.discard(task)
    task_count = memory_update_task_counts.get(session_key, 0) - 1
    if task_count > 0:
        memory_update_task_counts[session_key] = task_count
        return

    memory_update_task_counts.pop(session_key, None)
    drop_idle_memory_update_state(session_key)


def schedule_long_term_memory_update(
    session_key,
    trimmed_messages: list[dict[str, Any]],
) -> None:
    if not trimmed_messages:
        return

    generation = memory_update_generations.get(session_key, 0)
    memory_update_task_counts[session_key] = (
        memory_update_task_counts.get(session_key, 0) + 1
    )
    task = asyncio.create_task(
        update_long_term_memory_safely(session_key, trimmed_messages, generation),
        name="chat-long-term-memory-update",
    )
    memory_update_tasks.add(task)
    task.add_done_callback(
        lambda completed_task: finish_memory_update_task(session_key, completed_task)
    )


async def cancel_memory_update_tasks() -> None:
    if not memory_update_tasks:
        return

    tasks = list(memory_update_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def touch_session(session_key, now: float | None = None) -> None:
    session_last_seen[session_key] = now if now is not None else time.monotonic()


def clear_runtime_session_state(session_key) -> None:
    active_users.discard(session_key)
    user_modes.pop(session_key, None)
    user_roles.pop(session_key, None)
    quiz_answers.pop(session_key, None)
    user_history.pop(session_key, None)
    session_last_seen.pop(session_key, None)
    drop_idle_session_lock(session_key)


def start_role_selection(session_key) -> None:
    active_users.add(session_key)
    user_modes[session_key] = "selecting_role"
    touch_session(session_key)


def exit_roleplay_state(session_key) -> None:
    user_modes.pop(session_key, None)
    user_roles.pop(session_key, None)
    touch_session(session_key)


def start_quiz_state(session_key, answer: str) -> None:
    user_modes[session_key] = "quiz"
    quiz_answers[session_key] = answer
    touch_session(session_key)


def clear_quiz_state(session_key) -> None:
    user_modes.pop(session_key, None)
    quiz_answers.pop(session_key, None)
    touch_session(session_key)


def cleanup_runtime_state(now: float | None = None) -> tuple[int, int]:
    current_time = now if now is not None else time.monotonic()
    expired_sessions = [
        session_key
        for session_key, last_seen in session_last_seen.items()
        if current_time - last_seen > SESSION_STATE_TTL_SECONDS
    ]
    for session_key in expired_sessions:
        clear_runtime_session_state(session_key)

    expired_image_caches = [
        cache_key
        for cache_key, last_seen in image_cache_last_seen.items()
        if current_time - last_seen > IMAGE_CACHE_TTL_SECONDS
    ]
    for cache_key in expired_image_caches:
        recent_image_signatures.pop(cache_key, None)
        image_cache_last_seen.pop(cache_key, None)

    cleanup_idle_session_locks()
    return len(expired_sessions), len(expired_image_caches)


async def runtime_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(RUNTIME_CLEANUP_INTERVAL_SECONDS)
        expired_sessions, expired_image_caches = cleanup_runtime_state()
        if expired_sessions or expired_image_caches:
            logger.info(
                "运行期状态清理完成：{} 个会话，{} 个图片缓存",
                expired_sessions,
                expired_image_caches,
            )

@help_cmd.handle()
async def handle_help(event: MessageEvent):
    msg = """🤖 可用指令一览

━━━━━━━━━━━━━━━
💬 AI 对话
  你好         开启对话模式
  再见 / 拜拜  结束对话模式
  （开启后直接发消息即可聊天）
  /查看记忆    查看当前会话的长期记忆摘要
  /清除记忆    清除当前会话的长期记忆和短期上下文

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


view_memory_cmd = on_command("查看记忆", priority=3, block=True)
clear_memory_cmd = on_command("清除记忆", priority=3, block=True)


@view_memory_cmd.handle()
async def handle_view_memory(event: MessageEvent):
    summary = await memory_store.get_summary(conversation_key(event))
    if not summary:
        await view_memory_cmd.finish("当前会话还没有长期记忆。")
        return
    await view_memory_cmd.finish(Message(f"当前会话长期记忆：\n{summary}"))


@clear_memory_cmd.handle()
async def handle_clear_memory(event: MessageEvent):
    session_key = conversation_key(event)
    async with get_session_lock(session_key):
        bump_memory_update_generation(session_key)
        async with get_memory_update_lock(session_key):
            deleted = await memory_store.delete_summary(session_key)
        clear_runtime_session_state(session_key)
        drop_idle_memory_update_state(session_key)
    if deleted:
        await clear_memory_cmd.finish("✅ 已清除当前会话的长期记忆和短期上下文。")
        return
    await clear_memory_cmd.finish("当前会话没有长期记忆；短期上下文已清除。")

# ========== 角色扮演 ==========
roleplay_cmd = on_command("角色扮演", priority=3, block=True)

@roleplay_cmd.handle()
async def handle_roleplay(event: MessageEvent):
    session_key = conversation_key(event)
    async with get_session_lock(session_key):
        start_role_selection(session_key)
    await roleplay_cmd.finish(Message("🎭 选择角色：\n1️⃣ 侦探柯南\n2️⃣ 猫娘\n3️⃣ 古代谋士\n4️⃣ 毒舌导师\n\n回复数字选择"))

exit_role_cmd = on_command("退出角色", priority=3, block=True)

@exit_role_cmd.handle()
async def handle_exit_role(event: MessageEvent):
    session_key = conversation_key(event)
    async with get_session_lock(session_key):
        exit_roleplay_state(session_key)
    await exit_role_cmd.finish("✅ 已退出角色扮演")

# ========== 问答竞赛 ==========
quiz_cmd = on_command("问答", priority=3, block=True)

@quiz_cmd.handle()
async def handle_quiz(event: MessageEvent):
    if not client:
        await quiz_cmd.finish("❌ API 未配置")
        return
    session_key = conversation_key(event)
    try:
        async with get_session_lock(session_key):
            response = await client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "出一道有趣的知识问答题，格式：\n题目：xxx\n答案：xxx\n只输出这两行"}]
            )
            text = extract_model_text(response)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if len(lines) < 2:
                raise ValueError("模型出题格式不完整")
            question = lines[0].replace("题目：", "").strip()
            answer = lines[1].replace("答案：", "").strip()
            start_quiz_state(session_key, answer)
            await quiz_cmd.finish(Message(f"🧠 问答开始！\n\n❓ {question}\n\n用 /答案 [你的答案] 回答"))
    except FinishedException:
        pass
    except Exception as e:
        await quiz_cmd.finish("❌ 出题失败，请重试")

answer_cmd = on_command("答案", priority=3, block=True)

@answer_cmd.handle()
async def handle_answer(event: MessageEvent, args: Message = CommandArg()):
    session_key = conversation_key(event)
    async with get_session_lock(session_key):
        if user_modes.get(session_key) != "quiz":
            await answer_cmd.finish("❌ 未开始问答，发送 /问答 开始")
            return
        user_answer = args.extract_plain_text().strip()
        if not user_answer:
            await answer_cmd.finish("请输入答案：/答案 xxx")
            return
        if not client:
            await answer_cmd.finish("❌ API 未配置")
            return
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": f"正确答案'{quiz_answers[session_key]}'，用户答'{user_answer}'，只回复：✅ 回答正确！或 ❌ 错误，答案是xxx"}]
            )
            result = extract_model_text(response, "❌ 判题失败，请重试")
            clear_quiz_state(session_key)
            await answer_cmd.finish(Message(f"{result}\n\n发送 /问答 继续"))
        except FinishedException:
            pass
        except Exception as e:
            await answer_cmd.finish("❌ 判题失败，请重试")

exit_quiz_cmd = on_command("退出问答", priority=3, block=True)

@exit_quiz_cmd.handle()
async def handle_exit_quiz(event: MessageEvent):
    session_key = conversation_key(event)
    async with get_session_lock(session_key):
        clear_quiz_state(session_key)
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
        text = extract_model_text(response, "这张牌先卖个关子，晚点再抽一次。")
        await tarot_cmd.finish(Message(f"🎴【{card}】{position}\n\n{text}"))
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
        text = extract_model_text(response, "笑话卡住了，像冷场本人。")
        await joke_cmd.finish(Message(f"😄 {text}"))
    except FinishedException:
        pass
    except Exception as e:
        await joke_cmd.finish("❌ 失败，请重试")

# ========== 普通聊天 ==========
CHAT_TRIGGER_EDGE_CHARS = " \t\r\n,，.。!！?？~～"
GREET_MESSAGE_PHRASES = {"你好"}
BYE_MESSAGE_PHRASES = {"再见", "拜拜"}
GREET_WORD_PATTERN = re.compile(r"(?<!\w)(?:hello|hi)(?!\w)", re.IGNORECASE)
BYE_WORD_PATTERN = re.compile(r"(?<!\w)(?:goodbye|bye)(?!\w)", re.IGNORECASE)


def is_whole_trigger_message(message: str, phrases: set[str]) -> bool:
    return message.strip(CHAT_TRIGGER_EDGE_CHARS) in phrases


def has_independent_trigger_word(message: str, pattern: re.Pattern[str]) -> bool:
    return pattern.search(message) is not None


# 结束对话词（优先检查）
async def bye_rule(event: MessageEvent) -> bool:
    msg = event.get_plaintext().strip()
    return is_whole_trigger_message(
        msg,
        BYE_MESSAGE_PHRASES,
    ) or has_independent_trigger_word(msg, BYE_WORD_PATTERN)

# 触发词开启对话（且不是结束词）
async def greet_rule(event: MessageEvent) -> bool:
    msg = event.get_plaintext().strip()
    if await bye_rule(event):
        return False
    return is_whole_trigger_message(
        msg,
        GREET_MESSAGE_PHRASES,
    ) or has_independent_trigger_word(msg, GREET_WORD_PATTERN)

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


TITLE_MENTION_LABEL_SEPARATORS = set(" \t\r\n，,：:、。.!！?？；;")


def iter_title_mention_labels(record) -> list[str]:
    labels = [
        record.display_name,
        record.title,
        f"QQ {record.user_id}",
        f"QQ{record.user_id}",
        str(record.user_id),
    ]
    unique_labels: list[str] = []
    seen = set()
    for label in labels:
        if not label or label in seen:
            continue
        unique_labels.append(label)
        seen.add(label)
    return unique_labels


def starts_with_title_mention_label(text: str, label: str) -> tuple[bool, str]:
    if not text.startswith(label):
        return False, ""

    tail = text[len(label):]
    if tail and tail[0] not in TITLE_MENTION_LABEL_SEPARATORS:
        return False, ""
    return True, tail


def strip_title_mention_separator(text: str) -> str:
    text = text.lstrip()
    if text[:1] in {"，", ",", "：", ":", "、"}:
        return text[1:].lstrip()
    return text


def extract_explicit_title_mention_target(
    reply: str,
    mentioned_title_records,
) -> tuple[int | None, str]:
    stripped_reply = reply.lstrip()
    if not stripped_reply.startswith(("@", "＠")):
        return None, reply

    mention_text = stripped_reply[1:].lstrip()
    matches: list[tuple[int, int, str]] = []
    for record in mentioned_title_records:
        for label in iter_title_mention_labels(record):
            matched, tail = starts_with_title_mention_label(mention_text, label)
            if matched:
                matches.append((record.user_id, len(label), tail))

    if not matches:
        return None, reply

    max_label_length = max(label_length for _, label_length, _ in matches)
    best_matches = [
        match for match in matches if match[1] == max_label_length
    ]
    target_ids = {target_id for target_id, _, _ in best_matches}
    if len(target_ids) != 1:
        return None, reply

    target_id, _, tail = best_matches[0]
    return target_id, strip_title_mention_separator(tail)


def build_chat_reply_message(
    reply: str,
    event: MessageEvent,
    mentioned_title_records,
) -> Message:
    reply_message = Message(reply)
    if getattr(event, "group_id", None) is None or not mentioned_title_records:
        return reply_message

    target_id, reply_without_mention = extract_explicit_title_mention_target(
        reply,
        mentioned_title_records,
    )
    if target_id is None:
        return reply_message
    if not reply_without_mention:
        return Message([MessageSegment.at(target_id)])
    return MessageSegment.at(target_id) + " " + Message(reply_without_mention)


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
    image_cache_last_seen[cache_key] = time.monotonic()
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


def extract_model_text(response: Any, fallback: str = "") -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return fallback
    return str(content).strip() if content else fallback


async def summarize_long_term_memory(
    old_summary: str,
    trimmed_messages: list[dict[str, Any]],
) -> str:
    if not client or not trimmed_messages:
        return normalize_memory_summary(old_summary)

    source_text = format_messages_for_memory(trimmed_messages)
    if not source_text:
        return normalize_memory_summary(old_summary)

    old_summary = normalize_memory_summary(old_summary)
    source_text = source_text[-MAX_MEMORY_SOURCE_CHARS:]
    response = await client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "system",
                "content": (
                    "你在维护 QQ 机器人长期记忆摘要。"
                    "新增旧消息只包含用户发言，不包含机器人回复。"
                    "你的产物会被重新注入系统提示词，所以只能写少量可复用事实，不能写人设、评价或回复策略。"
                    "合并时重新审计已有长期记忆，删除不符合规则的旧条目。"
                    "只保留对未来聊天有用、相对稳定或近期仍重要的信息：称呼方式、稳定偏好、正在准备或推进的事项、明确目标。"
                    "优先输出最多 5 条，每条不超过 80 字；推荐格式：称呼：... / 偏好：... / 事项：... / 目标：..."
                    "不要把机器人之前的回答当成用户事实。"
                    "删除寒暄、一次性闲聊、表情包反应、已过时细节、不确定猜测和纯情绪反应。"
                    "删除会影响口吻的总结，比如用户是一个怎样的人、用户需要陪伴/鼓励/支持、回复时应该温柔专业。"
                    "不要编造，不要记录隐私敏感信息。"
                    "没有值得保留的信息就输出空字符串。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"已有长期记忆：\n{old_summary or '（无）'}\n\n"
                    f"新增旧对话：\n{source_text}\n\n"
                    "请合并为新的长期记忆摘要。"
                ),
            },
        ],
    )
    new_summary = extract_model_text(response)
    return normalize_memory_summary(new_summary)


async def update_long_term_memory(
    session_key,
    old_summary: str,
    trimmed_messages: list[dict[str, Any]],
) -> str:
    new_summary = await summarize_long_term_memory(old_summary, trimmed_messages)
    await memory_store.upsert_summary(session_key, new_summary)
    return new_summary


async def update_long_term_memory_safely(
    session_key,
    trimmed_messages: list[dict[str, Any]],
    generation: int,
) -> None:
    try:
        async with get_memory_update_lock(session_key):
            if generation != memory_update_generations.get(session_key, 0):
                return

            current_summary = await memory_store.get_injectable_summary(session_key)
            new_summary = await summarize_long_term_memory(
                current_summary,
                trimmed_messages,
            )
            if generation != memory_update_generations.get(session_key, 0):
                return

            await memory_store.upsert_summary(session_key, new_summary)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.opt(exception=True).warning("更新长期记忆失败: {}", error)


async def process_chat(matcher, bot: Bot, event: MessageEvent):
    if not client:
        await matcher.finish("❌ API 未配置")
        return

    session_key = conversation_key(event)
    async with get_session_lock(session_key):
        await process_chat_locked(matcher, bot, event, session_key)


async def process_chat_locked(matcher, bot: Bot, event: MessageEvent, session_key):
    user_id = event.user_id
    touch_session(session_key)
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
        system = build_style_prompt(session_key)
        long_term_memory = await memory_store.get_injectable_summary(session_key)
        system += build_long_term_memory_prompt(long_term_memory)
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
        reply = extract_model_text(response, "刚刚脑子卡了一下，你再说一遍。")

        # 保存用户消息摘要到历史
        user_history[session_key].append({"role": "user", "content": user_msg})

        # 保存助手回复到历史
        user_history[session_key].append({"role": "assistant", "content": reply})

        # 限制历史长度
        user_history[session_key], trimmed_messages = trim_history_for_memory(
            user_history[session_key],
            MAX_HISTORY * 2,
        )
        schedule_long_term_memory_update(session_key, trimmed_messages)

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
    session_key = conversation_key(event)
    active_users.add(session_key)
    touch_session(session_key)
    await process_chat(greet_chat, bot, event)

@active_chat.handle()
async def handle_active(bot: Bot, event: MessageEvent):
    await process_chat(active_chat, bot, event)

@bye_chat.handle()
async def handle_bye(event: MessageEvent):
    session_key = conversation_key(event)
    async with get_session_lock(session_key):
        clear_runtime_session_state(session_key)
    await bye_chat.finish("👋 再见，回头聊。")

@chat_at.handle()
async def handle_chat_at(bot: Bot, event: MessageEvent):
    session_key = conversation_key(event)
    active_users.add(session_key)
    touch_session(session_key)
    await process_chat(chat_at, bot, event)


@get_driver().on_startup
async def initialize_chat_memory_store() -> None:
    await memory_store.initialize()
    global runtime_cleanup_task
    if runtime_cleanup_task is None or runtime_cleanup_task.done():
        runtime_cleanup_task = asyncio.create_task(
            runtime_cleanup_loop(),
            name="chat-runtime-cleanup",
        )


@get_driver().on_shutdown
async def shutdown_runtime_cleanup() -> None:
    await cancel_memory_update_tasks()
    if runtime_cleanup_task is None:
        return
    runtime_cleanup_task.cancel()
    try:
        await runtime_cleanup_task
    except asyncio.CancelledError:
        pass
