import unittest
from types import SimpleNamespace

import nonebot


nonebot.init()

from plugins.claude_chat import build_chat_reply_message  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
