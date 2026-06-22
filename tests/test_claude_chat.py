import unittest
from types import SimpleNamespace

import nonebot


nonebot.init()

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

from plugins.claude_chat import SYSTEM_PROMPT  # noqa: E402
from plugins.claude_chat import build_user_message_content  # noqa: E402
from plugins.claude_chat import build_chat_reply_message  # noqa: E402
from plugins.claude_chat import build_style_prompt  # noqa: E402
from plugins.claude_chat import cleanup_runtime_state  # noqa: E402
from plugins.claude_chat import active_users  # noqa: E402
from plugins.claude_chat import clear_runtime_session_state  # noqa: E402
from plugins.claude_chat import conversation_key  # noqa: E402
from plugins.claude_chat import format_user_message  # noqa: E402
from plugins.claude_chat import image_cache_last_seen  # noqa: E402
from plugins.claude_chat import quiz_answers  # noqa: E402
from plugins.claude_chat import recent_image_signatures  # noqa: E402
from plugins.claude_chat import session_last_seen  # noqa: E402
from plugins.claude_chat import user_history  # noqa: E402
from plugins.claude_chat import user_modes  # noqa: E402
from plugins.claude_chat import user_roles  # noqa: E402
from plugins.user_titles import UserTitleRecord  # noqa: E402


class ClaudeChatReplyTest(unittest.TestCase):
    def test_group_reply_mentions_first_matched_title_user(self):
        event = SimpleNamespace(group_id=10000)
        records = [UserTitleRecord(3396024932, "人机", "何以究得物理")]

        message = build_chat_reply_message("这个一听就是群里稳定贡献节目效果的选手", event, records)

        segments = [(segment.type, segment.data) for segment in message]
        self.assertEqual(segments[0], ("at", {"qq": "3396024932"}))
        self.assertEqual(segments[1], ("text", {"text": " "}))
        self.assertIn("稳定贡献节目效果", segments[2][1]["text"])

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


class ClaudeChatRuntimeCleanupTest(unittest.TestCase):
    def tearDown(self):
        active_users.clear()
        user_modes.clear()
        user_roles.clear()
        quiz_answers.clear()
        user_history.clear()
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


if __name__ == "__main__":
    unittest.main()
