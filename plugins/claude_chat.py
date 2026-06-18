from nonebot import on_message, on_command, get_driver
import random
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message
from nonebot.rule import to_me, Rule
from nonebot.log import logger
from nonebot.exception import FinishedException
from nonebot.params import CommandArg
from openai import OpenAI

# 从环境变量获取 API Key 和 Base URL
config = get_driver().config
api_key = getattr(config, "openai_api_key", None)
base_url = getattr(config, "openai_base_url", None)

if not api_key:
    logger.warning("未配置 OPENAI_API_KEY，ChatGPT 功能将不可用")
    client = None
else:
    client = OpenAI(api_key=api_key, base_url=base_url)
    logger.info(f"ChatGPT API 客户端初始化成功 (base_url: {base_url})")

# 用户状态存储
user_modes = {}      # "chat" / "roleplay" / "quiz"
user_roles = {}      # 角色扮演设定
quiz_answers = {}    # 问答答案
active_users = set() # 已开启对话的用户
user_history = {}    # 用户对话历史

MAX_HISTORY = 10     # 最多保留10轮对话

# 系统提示词（简洁风格）
SYSTEM_PROMPT = "你是一个简洁的助手，回复尽量控制在100字以内，直接给出答案，不要废话。"

# 角色预设
ROLES = {
    "1": ("侦探柯南", "你是少年侦探柯南，说话逻辑严谨，偶尔说'真相只有一个！'，回复简短。"),
    "2": ("猫娘", "你是可爱猫娘，句尾加'喵~'，回复简短活泼。"),
    "3": ("古代谋士", "你是古代谋士，说话文绉绉，善用典故，回复简短。"),
    "4": ("毒舌导师", "你是毒舌导师，说话犀利直接，回复简短。"),
}

# ========== 帮助指令 ==========
help_cmd = on_command("help", aliases={"帮助"}, priority=3, block=True)

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
  鲁迅说 [文字]  经典鲁迅梗图
  举牌 [文字]  举牌表情包
  petpet帮助   查看全部 50+ 种表情
  表情帮助     查看全部 100+ 种梗图模板

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
    user_modes[event.user_id] = "selecting_role"
    await roleplay_cmd.finish(Message("🎭 选择角色：\n1️⃣ 侦探柯南\n2️⃣ 猫娘\n3️⃣ 古代谋士\n4️⃣ 毒舌导师\n\n回复数字选择"))

exit_role_cmd = on_command("退出角色", priority=3, block=True)

@exit_role_cmd.handle()
async def handle_exit_role(event: MessageEvent):
    user_modes.pop(event.user_id, None)
    user_roles.pop(event.user_id, None)
    await exit_role_cmd.finish("✅ 已退出角色扮演")

# ========== 问答竞赛 ==========
quiz_cmd = on_command("问答", priority=3, block=True)

@quiz_cmd.handle()
async def handle_quiz(event: MessageEvent):
    if not client:
        await quiz_cmd.finish("❌ API 未配置")
        return
    try:
        response = client.chat.completions.create(
            model="gpt-5",
            messages=[{"role": "user", "content": "出一道有趣的知识问答题，格式：\n题目：xxx\n答案：xxx\n只输出这两行"}]
        )
        text = response.choices[0].message.content
        lines = text.strip().split("\n")
        question = lines[0].replace("题目：", "").strip()
        answer = lines[1].replace("答案：", "").strip()
        user_modes[event.user_id] = "quiz"
        quiz_answers[event.user_id] = answer
        await quiz_cmd.finish(Message(f"🧠 问答开始！\n\n❓ {question}\n\n用 /答案 [你的答案] 回答"))
    except FinishedException:
        pass
    except Exception as e:
        await quiz_cmd.finish("❌ 出题失败，请重试")

answer_cmd = on_command("答案", priority=3, block=True)

@answer_cmd.handle()
async def handle_answer(event: MessageEvent, args: Message = CommandArg()):
    user_id = event.user_id
    if user_modes.get(user_id) != "quiz":
        await answer_cmd.finish("❌ 未开始问答，发送 /问答 开始")
        return
    user_answer = args.extract_plain_text().strip()
    if not user_answer:
        await answer_cmd.finish("请输入答案：/答案 xxx")
        return
    try:
        response = client.chat.completions.create(
            model="gpt-5",
            messages=[{"role": "user", "content": f"正确答案'{quiz_answers[user_id]}'，用户答'{user_answer}'，只回复：✅ 回答正确！或 ❌ 错误，答案是xxx"}]
        )
        result = response.choices[0].message.content
        user_modes.pop(user_id, None)
        quiz_answers.pop(user_id, None)
        await answer_cmd.finish(Message(f"{result}\n\n发送 /问答 继续"))
    except FinishedException:
        pass
    except Exception as e:
        await answer_cmd.finish("❌ 判题失败，请重试")

exit_quiz_cmd = on_command("退出问答", priority=3, block=True)

@exit_quiz_cmd.handle()
async def handle_exit_quiz(event: MessageEvent):
    user_modes.pop(event.user_id, None)
    quiz_answers.pop(event.user_id, None)
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
        response = client.chat.completions.create(
            model="gpt-5",
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
        response = client.chat.completions.create(
            model="gpt-5",
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

# 已开启对话且不是触发词、不是结束词
BLOCKED_KEYWORDS = {
    "抽老婆", "随机老婆", "抽wife", "随机wife", "今日wife", "今日老婆",
    "五子棋", "围棋", "黑白棋",
}

async def active_rule(event: MessageEvent) -> bool:
    if event.user_id not in active_users:
        return False
    if await greet_rule(event) or await bye_rule(event):
        return False
    msg = event.get_plaintext().strip()
    if any(msg.lstrip("/").startswith(kw) for kw in BLOCKED_KEYWORDS):
        return False
    return True

async def not_blocked(event: MessageEvent) -> bool:
    msg = event.get_plaintext().strip()
    # 以 / 开头的是命令，不应由聊天 AI 接管（让对应命令插件处理或静默忽略）
    if msg.startswith("/"):
        return False
    return not any(msg.startswith(kw) for kw in BLOCKED_KEYWORDS)

bye_chat = on_message(rule=Rule(bye_rule), priority=4, block=True)
greet_chat = on_message(rule=Rule(greet_rule), priority=5, block=True)
chat_at = on_message(rule=to_me() & Rule(not_blocked), priority=15, block=True)
active_chat = on_message(rule=Rule(active_rule), priority=16, block=True)

async def process_chat(matcher, bot: Bot, event: MessageEvent):
    if not client:
        await matcher.finish("❌ API 未配置")
        return

    user_id = event.user_id
    user_msg = event.get_plaintext().strip()

    if not user_msg:
        return

    # 处理角色选择
    if user_modes.get(user_id) == "selecting_role":
        if user_msg in ROLES:
            role_name, role_prompt = ROLES[user_msg]
            user_roles[user_id] = role_prompt
            user_modes[user_id] = "roleplay"
            await matcher.finish(f"🎭 已切换为【{role_name}】，发送 /退出角色 可退出")
        else:
            await matcher.finish("请回复 1-4 选择角色")
        return

    try:
        # 构建系统提示
        system = SYSTEM_PROMPT
        if user_modes.get(user_id) == "roleplay" and user_id in user_roles:
            system = user_roles[user_id]

        # 初始化历史
        if user_id not in user_history:
            user_history[user_id] = []

        # 添加用户消息到历史
        user_history[user_id].append({"role": "user", "content": user_msg})

        # 构建完整消息列表
        messages = [{"role": "system", "content": system}] + user_history[user_id]

        response = client.chat.completions.create(model="gpt-5", messages=messages)
        reply = response.choices[0].message.content

        # 保存助手回复到历史
        user_history[user_id].append({"role": "assistant", "content": reply})

        # 限制历史长度
        if len(user_history[user_id]) > MAX_HISTORY * 2:
            user_history[user_id] = user_history[user_id][-MAX_HISTORY * 2:]

        logger.info(f"回复: {reply[:80]}...")
        await matcher.finish(Message(reply))
    except FinishedException:
        pass
    except Exception as e:
        logger.error(f"调用 API 失败: {e}", exc_info=True)
        await matcher.finish(f"❌ 调用失败: {str(e)}")

@greet_chat.handle()
async def handle_greet(bot: Bot, event: MessageEvent):
    active_users.add(event.user_id)
    await process_chat(greet_chat, bot, event)

@active_chat.handle()
async def handle_active(bot: Bot, event: MessageEvent):
    await process_chat(active_chat, bot, event)

@bye_chat.handle()
async def handle_bye(event: MessageEvent):
    active_users.discard(event.user_id)
    user_modes.pop(event.user_id, None)
    user_roles.pop(event.user_id, None)
    user_history.pop(event.user_id, None)
    await bye_chat.finish("👋 再见！有需要随时说'你好'找我")

@chat_at.handle()
async def handle_chat_at(bot: Bot, event: MessageEvent):
    active_users.add(event.user_id)
    await process_chat(chat_at, bot, event)
