import unittest
from types import SimpleNamespace

import nonebot


nonebot.init()

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

from plugins.claude_chat import build_chat_reply_message  # noqa: E402
from plugins.claude_chat import format_user_message  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
