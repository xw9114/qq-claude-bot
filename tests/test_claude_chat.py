import asyncio
import unittest
from types import SimpleNamespace

import nonebot


nonebot.init()

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

import plugins.claude_chat as claude_chat  # noqa: E402
from plugins.claude_chat import SYSTEM_PROMPT  # noqa: E402
from plugins.claude_chat import active_rule  # noqa: E402
from plugins.claude_chat import build_user_message_content  # noqa: E402
from plugins.claude_chat import build_chat_reply_message  # noqa: E402
from plugins.claude_chat import build_style_prompt  # noqa: E402
from plugins.claude_chat import clear_quiz_state  # noqa: E402
from plugins.claude_chat import cleanup_runtime_state  # noqa: E402
from plugins.claude_chat import active_users  # noqa: E402
from plugins.claude_chat import clear_runtime_session_state  # noqa: E402
from plugins.claude_chat import conversation_key  # noqa: E402
from plugins.claude_chat import exit_roleplay_state  # noqa: E402
from plugins.claude_chat import extract_model_text  # noqa: E402
from plugins.claude_chat import format_user_message  # noqa: E402
from plugins.claude_chat import get_session_lock  # noqa: E402
from plugins.claude_chat import image_cache_last_seen  # noqa: E402
from plugins.claude_chat import memory_update_generations  # noqa: E402
from plugins.claude_chat import memory_update_locks  # noqa: E402
from plugins.claude_chat import memory_update_task_counts  # noqa: E402
from plugins.claude_chat import memory_update_tasks  # noqa: E402
from plugins.claude_chat import quiz_answers  # noqa: E402
from plugins.claude_chat import recent_image_signatures  # noqa: E402
from plugins.claude_chat import schedule_long_term_memory_update  # noqa: E402
from plugins.claude_chat import session_locks  # noqa: E402
from plugins.claude_chat import session_last_seen  # noqa: E402
from plugins.claude_chat import start_quiz_state  # noqa: E402
from plugins.claude_chat import start_role_selection  # noqa: E402
from plugins.claude_chat import update_long_term_memory_safely  # noqa: E402
from plugins.claude_chat import user_history  # noqa: E402
from plugins.claude_chat import user_modes  # noqa: E402
from plugins.claude_chat import user_roles  # noqa: E402
from plugins.user_titles import UserTitleRecord  # noqa: E402


class ClaudeChatReplyTest(unittest.TestCase):
    def test_group_reply_with_title_defaults_to_plain_text(self):
        event = SimpleNamespace(group_id=10000)
        records = [UserTitleRecord(3396024932, "人机", "何以究得物理")]

        message = build_chat_reply_message("这个一听就是群里稳定贡献节目效果的选手", event, records)

        self.assertEqual(
            [(segment.type, segment.data) for segment in message],
            [("text", {"text": "这个一听就是群里稳定贡献节目效果的选手"})],
        )

    def test_group_reply_mentions_when_reply_explicitly_targets_title_user(self):
        event = SimpleNamespace(group_id=10000)
        records = [UserTitleRecord(3396024932, "人机", "何以究得物理")]

        message = build_chat_reply_message("@人机 出来接一下这个锅", event, records)

        self.assertEqual(
            [(segment.type, segment.data) for segment in message],
            [
                ("at", {"qq": "3396024932"}),
                ("text", {"text": " "}),
                ("text", {"text": "出来接一下这个锅"}),
            ],
        )

    def test_group_reply_can_be_only_explicit_title_mention(self):
        event = SimpleNamespace(group_id=10000)
        records = [UserTitleRecord(3396024932, "人机", "何以究得物理")]

        message = build_chat_reply_message("@人机", event, records)

        self.assertEqual(
            [(segment.type, segment.data) for segment in message],
            [("at", {"qq": "3396024932"})],
        )

    def test_ambiguous_title_mention_stays_plain_text(self):
        event = SimpleNamespace(group_id=10000)
        records = [
            UserTitleRecord(10001, "人机", "甲"),
            UserTitleRecord(10002, "人机", "乙"),
        ]

        message = build_chat_reply_message("@人机 出来接一下这个锅", event, records)

        self.assertEqual(
            [(segment.type, segment.data) for segment in message],
            [("text", {"text": "@人机 出来接一下这个锅"})],
        )

    def test_private_reply_does_not_mention(self):
        event = SimpleNamespace()
        records = [UserTitleRecord(3396024932, "人机", "何以究得物理")]

        message = build_chat_reply_message("普通回复", event, records)

        self.assertEqual([(segment.type, segment.data) for segment in message], [
            ("text", {"text": "普通回复"})
        ])


class ClaudeChatMessageFormatTest(unittest.TestCase):
    def tearDown(self):
        recent_image_signatures.clear()

    def test_keeps_plain_text_compact(self):
        message = Message("  人机为啥不说话？\n")

        self.assertEqual(format_user_message(message), "人机为啥不说话？")

    def test_describes_image_face_and_mentions(self):
        message = Message(
            [
                MessageSegment.text("你看"),
                MessageSegment(
                    "image",
                    {
                        "summary": "[动画表情]",
                        "file": "3FFDD985.jpg",
                    },
                ),
                MessageSegment.face(14),
                MessageSegment.at(123456),
            ]
        )

        self.assertEqual(
            format_user_message(message),
            "你看 [图片：动画表情] [QQ表情:14] @123456",
        )

    def test_builds_multimodal_content_for_image_url(self):
        message = Message(
            [
                MessageSegment.text("看看这张图"),
                MessageSegment(
                    "image",
                    {
                        "summary": "[截图]",
                        "url": "https://example.com/image.jpg",
                    },
                ),
            ]
        )

        self.assertEqual(
            build_user_message_content(message),
            [
                {"type": "text", "text": "看看这张图"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/image.jpg"},
                },
                {"type": "text", "text": "[图片：截图]"},
            ],
        )

    def test_image_without_url_falls_back_to_text(self):
        message = Message(
            [
                MessageSegment.text("这个表情"),
                MessageSegment("image", {"summary": "[动画表情]"}),
            ]
        )

        self.assertEqual(
            build_user_message_content(message),
            "这个表情 [图片：动画表情]",
        )

    def test_repeated_image_skips_vision_input(self):
        cache_key = ("group", 10000)
        message = Message(
            [
                MessageSegment.text("又来了"),
                MessageSegment(
                    "image",
                    {
                        "summary": "[动画表情]",
                        "file": "same-image.jpg",
                        "url": "https://example.com/image.jpg",
                    },
                ),
            ]
        )

        first_content = build_user_message_content(message, cache_key)
        second_content = build_user_message_content(message, cache_key)

        self.assertIsInstance(first_content, list)
        self.assertIn(
            {
                "type": "image_url",
                "image_url": {"url": "https://example.com/image.jpg"},
            },
            first_content,
        )
        self.assertEqual(
            second_content,
            "又来了 [图片：动画表情]（重复图片，已跳过识别）",
        )


class ClaudeChatModelTextTest(unittest.TestCase):
    def test_extract_model_text_strips_content(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="  好的  "))]
        )

        self.assertEqual(extract_model_text(response, "兜底"), "好的")

    def test_extract_model_text_uses_fallback_for_empty_or_malformed_response(self):
        empty_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
        )

        self.assertEqual(extract_model_text(empty_response, "兜底"), "兜底")
        self.assertEqual(extract_model_text(SimpleNamespace(choices=[]), "兜底"), "兜底")


class ClaudeChatSessionKeyTest(unittest.TestCase):
    def test_group_sessions_are_isolated_by_group_id(self):
        first_group_event = SimpleNamespace(user_id=12345, group_id=10000)
        second_group_event = SimpleNamespace(user_id=12345, group_id=20000)

        self.assertNotEqual(
            conversation_key(first_group_event),
            conversation_key(second_group_event),
        )

    def test_private_session_is_separate_from_group_session(self):
        private_event = SimpleNamespace(user_id=12345)
        group_event = SimpleNamespace(user_id=12345, group_id=10000)

        self.assertNotEqual(conversation_key(private_event), conversation_key(group_event))


class ClaudeChatPromptTest(unittest.TestCase):
    def tearDown(self):
        user_modes.clear()
        user_roles.clear()

    def test_prompt_rejects_assistant_style_tails(self):
        self.assertIn("普通群友", SYSTEM_PROMPT)
        self.assertIn("默认只回 1 句", SYSTEM_PROMPT)
        self.assertIn("如果你愿意", SYSTEM_PROMPT)
        self.assertIn("不复述问题", SYSTEM_PROMPT)

    def test_roleplay_keeps_base_chat_style(self):
        session_key = ("group", 12345, 10000)
        user_modes[session_key] = "roleplay"
        user_roles[session_key] = "说话像古代谋士"

        prompt = build_style_prompt(session_key)

        self.assertIn(SYSTEM_PROMPT, prompt)
        self.assertIn("当前角色设定：说话像古代谋士", prompt)
        self.assertIn("只影响口吻", prompt)


class ClaudeChatMemorySummaryTest(unittest.TestCase):
    def test_summarize_memory_reaudits_old_summary_and_filters_model_output(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = []

            async def create(self, model, messages):
                self.calls.append({"model": model, "messages": messages})
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=(
                                    "用户是一个认真温柔的人\n"
                                    "偏好：喜欢短回复\n"
                                    "回复时要温柔专业地鼓励他"
                                )
                            )
                        )
                    ]
                )

        async def run_test():
            completions = FakeCompletions()
            fake_client = SimpleNamespace(
                chat=SimpleNamespace(completions=completions)
            )
            original_client = claude_chat.client
            claude_chat.client = fake_client
            try:
                summary = await claude_chat.summarize_long_term_memory(
                    "用户是一个认真温柔的人\n事项：最近在准备面试",
                    [
                        {"role": "user", "content": "我最近在改简历"},
                        {"role": "assistant", "content": "用户需要温柔鼓励"},
                    ],
                )
            finally:
                claude_chat.client = original_client

            self.assertEqual(summary, "偏好：喜欢短回复")
            self.assertEqual(len(completions.calls), 1)

            messages = completions.calls[0]["messages"]
            self.assertIn("不能写人设、评价或回复策略", messages[0]["content"])
            self.assertIn("最多 5 条", messages[0]["content"])
            self.assertIn("事项：最近在准备面试", messages[1]["content"])
            self.assertNotIn("认真温柔", messages[1]["content"])
            self.assertIn("用户: 我最近在改简历", messages[1]["content"])
            self.assertNotIn("用户需要温柔鼓励", messages[1]["content"])

        asyncio.run(run_test())


class ClaudeChatStateTransitionTest(unittest.TestCase):
    def tearDown(self):
        active_users.clear()
        user_modes.clear()
        user_roles.clear()
        quiz_answers.clear()
        session_last_seen.clear()

    def test_role_selection_activates_session_for_numeric_reply(self):
        event = SimpleNamespace(
            user_id=12345,
            group_id=10000,
            get_plaintext=lambda: "1",
        )
        session_key = conversation_key(event)

        start_role_selection(session_key)

        self.assertIn(session_key, active_users)
        self.assertEqual(user_modes[session_key], "selecting_role")
        self.assertTrue(asyncio.run(active_rule(event)))

    def test_exit_roleplay_state_keeps_chat_active_but_clears_role(self):
        session_key = ("group", 12345, 10000)
        active_users.add(session_key)
        user_modes[session_key] = "roleplay"
        user_roles[session_key] = "role prompt"

        exit_roleplay_state(session_key)

        self.assertIn(session_key, active_users)
        self.assertNotIn(session_key, user_modes)
        self.assertNotIn(session_key, user_roles)
        self.assertIn(session_key, session_last_seen)

    def test_quiz_state_lifecycle(self):
        session_key = ("group", 12345, 10000)

        start_quiz_state(session_key, "正确答案")

        self.assertEqual(user_modes[session_key], "quiz")
        self.assertEqual(quiz_answers[session_key], "正确答案")
        self.assertIn(session_key, session_last_seen)

        clear_quiz_state(session_key)

        self.assertNotIn(session_key, user_modes)
        self.assertNotIn(session_key, quiz_answers)
        self.assertIn(session_key, session_last_seen)


class ClaudeChatRuntimeCleanupTest(unittest.TestCase):
    def tearDown(self):
        active_users.clear()
        user_modes.clear()
        user_roles.clear()
        quiz_answers.clear()
        user_history.clear()
        session_locks.clear()
        session_last_seen.clear()
        recent_image_signatures.clear()
        image_cache_last_seen.clear()

    def test_clear_runtime_session_state_removes_short_term_state(self):
        session_key = ("group", 12345, 10000)
        active_users.add(session_key)
        user_modes[session_key] = "roleplay"
        user_roles[session_key] = "role prompt"
        user_history[session_key] = [{"role": "user", "content": "我要考试了"}]
        session_last_seen[session_key] = 1.0

        clear_runtime_session_state(session_key)

        self.assertNotIn(session_key, active_users)
        self.assertNotIn(session_key, user_modes)
        self.assertNotIn(session_key, user_roles)
        self.assertNotIn(session_key, user_history)
        self.assertNotIn(session_key, session_last_seen)

    def test_cleanup_runtime_state_removes_expired_entries(self):
        expired_session = ("group", 12345, 10000)
        fresh_session = ("group", 12345, 20000)
        expired_cache = ("group", 10000)
        fresh_cache = ("group", 20000)

        active_users.update({expired_session, fresh_session})
        user_history[expired_session] = [{"role": "user", "content": "旧消息"}]
        user_history[fresh_session] = [{"role": "user", "content": "新消息"}]
        session_last_seen[expired_session] = 0.0
        session_last_seen[fresh_session] = 100000.0
        recent_image_signatures[expired_cache] = ["old-image"]
        recent_image_signatures[fresh_cache] = ["new-image"]
        image_cache_last_seen[expired_cache] = 0.0
        image_cache_last_seen[fresh_cache] = 100000.0

        expired_sessions, expired_image_caches = cleanup_runtime_state(100000.0)

        self.assertEqual(expired_sessions, 1)
        self.assertEqual(expired_image_caches, 1)
        self.assertNotIn(expired_session, active_users)
        self.assertIn(fresh_session, active_users)
        self.assertNotIn(expired_cache, recent_image_signatures)
        self.assertIn(fresh_cache, recent_image_signatures)

    def test_session_lock_is_reused_and_cleaned_when_idle(self):
        session_key = ("group", 12345, 10000)

        first_lock = get_session_lock(session_key)
        second_lock = get_session_lock(session_key)
        session_last_seen[session_key] = 0.0

        expired_sessions, _ = cleanup_runtime_state(100000.0)

        self.assertIs(first_lock, second_lock)
        self.assertEqual(expired_sessions, 1)
        self.assertNotIn(session_key, session_locks)

    def test_locked_session_lock_survives_cleanup(self):
        async def run_test():
            session_key = ("group", 12345, 10000)
            lock = get_session_lock(session_key)
            session_last_seen[session_key] = 0.0

            async with lock:
                cleanup_runtime_state(100000.0)

            self.assertIn(session_key, session_locks)
            cleanup_runtime_state(100001.0)
            self.assertNotIn(session_key, session_locks)

        asyncio.run(run_test())


class ClaudeChatMemoryUpdateTest(unittest.TestCase):
    def tearDown(self):
        memory_update_generations.clear()
        memory_update_locks.clear()
        memory_update_task_counts.clear()
        memory_update_tasks.clear()

    def test_schedule_long_term_memory_update_tracks_and_cleans_task(self):
        async def run_test():
            session_key = ("group", 12345, 10000)
            trimmed_messages = [{"role": "user", "content": "我要考试了"}]
            calls = []
            original_update = claude_chat.update_long_term_memory_safely

            async def fake_update(session_key_arg, messages_arg, generation_arg):
                calls.append((session_key_arg, messages_arg, generation_arg))

            claude_chat.update_long_term_memory_safely = fake_update
            try:
                schedule_long_term_memory_update(session_key, trimmed_messages)
                self.assertEqual(memory_update_task_counts[session_key], 1)

                await asyncio.gather(*list(memory_update_tasks))

                self.assertEqual(calls, [(session_key, trimmed_messages, 0)])
                self.assertNotIn(session_key, memory_update_task_counts)
            finally:
                claude_chat.update_long_term_memory_safely = original_update

        asyncio.run(run_test())

    def test_memory_generation_change_skips_stale_background_write(self):
        class FakeMemoryStore:
            def __init__(self):
                self.writes = []

            async def get_summary(self, session_key):
                return "旧摘要"

            async def upsert_summary(self, session_key, summary):
                self.writes.append((session_key, summary))

        async def run_test():
            session_key = ("group", 12345, 10000)
            store = FakeMemoryStore()
            original_store = claude_chat.memory_store
            original_summarize = claude_chat.summarize_long_term_memory

            async def fake_summarize(old_summary, trimmed_messages):
                memory_update_generations[session_key] = 2
                return "新摘要"

            claude_chat.memory_store = store
            claude_chat.summarize_long_term_memory = fake_summarize
            memory_update_generations[session_key] = 1
            try:
                await update_long_term_memory_safely(
                    session_key,
                    [{"role": "user", "content": "我要考试了"}],
                    1,
                )

                self.assertEqual(store.writes, [])
            finally:
                claude_chat.memory_store = original_store
                claude_chat.summarize_long_term_memory = original_summarize

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
