import unittest
from types import SimpleNamespace

import nonebot


nonebot.init()

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

from plugins.claude_chat import build_user_message_content  # noqa: E402
from plugins.claude_chat import build_chat_reply_message  # noqa: E402
from plugins.claude_chat import conversation_key  # noqa: E402
from plugins.claude_chat import format_user_message  # noqa: E402
from plugins.claude_chat import recent_image_signatures  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
