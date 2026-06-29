import asyncio
import tempfile
import unittest
from pathlib import Path

from plugins.chat_memory import (
    LongTermMemoryStore,
    build_long_term_memory_prompt,
    format_messages_for_memory,
    normalize_memory_summary,
    trim_history_for_memory,
)


class ChatMemoryHelperTest(unittest.TestCase):
    def test_normalizes_memory_summary(self):
        self.assertEqual(
            normalize_memory_summary("  用户准备考试  \n\n 喜欢简短回答 "),
            "用户准备考试\n喜欢简短回答",
        )

    def test_normalizes_memory_summary_filters_persona_and_style_noise(self):
        summary = normalize_memory_summary(
            "\n".join(
                [
                    "- 用户是一个认真温柔的人",
                    "- 称呼：叫他小周",
                    "- 偏好：不喜欢 AI 助手腔，喜欢短回复",
                    "- 用户喜欢聊天，愿意交流",
                    "- 回复时要温柔专业地鼓励他",
                    "- 事项：最近在推进数学建模论文",
                    "- 昨天发了个表情包",
                ]
            )
        )

        self.assertEqual(
            summary,
            "\n".join(
                [
                    "称呼：叫他小周",
                    "偏好：不喜欢 AI 助手腔，喜欢短回复",
                    "事项：最近在推进数学建模论文",
                ]
            ),
        )

    def test_builds_prompt_only_when_summary_exists(self):
        self.assertEqual(build_long_term_memory_prompt(""), "")

        prompt = build_long_term_memory_prompt("用户最近要考试")

        self.assertIn("当前会话的长期记忆摘要", prompt)
        self.assertIn("用户最近要考试", prompt)
        self.assertIn("不要把摘要里的信息归因给其他人", prompt)

    def test_builds_prompt_as_facts_not_persona_or_style_instruction(self):
        prompt = build_long_term_memory_prompt(
            "用户是一个认真温柔的人\n称呼：叫他小周"
        )

        self.assertIn("称呼：叫他小周", prompt)
        self.assertNotIn("认真温柔", prompt)
        self.assertIn("不是人设、评价或回复风格指令", prompt)
        self.assertIn("不相关就忽略", prompt)

    def test_trims_history_and_returns_overflow_messages(self):
        history = [
            {"role": "user", "content": f"用户消息 {index}"}
            for index in range(5)
        ]

        kept, trimmed = trim_history_for_memory(history, 3)

        self.assertEqual([message["content"] for message in kept], [
            "用户消息 2",
            "用户消息 3",
            "用户消息 4",
        ])
        self.assertEqual([message["content"] for message in trimmed], [
            "用户消息 0",
            "用户消息 1",
        ])

    def test_formats_user_messages_for_memory(self):
        text = format_messages_for_memory(
            [
                {"role": "user", "content": "我要考试了"},
                {"role": "assistant", "content": "那先别熬夜"},
                {"role": "user", "content": "我喜欢短回复"},
            ]
        )

        self.assertEqual(text, "用户: 我要考试了\n用户: 我喜欢短回复")


class LongTermMemoryStoreTest(unittest.TestCase):
    def test_persists_summary_by_session_key(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                store = LongTermMemoryStore(
                    Path(temp_dir) / "memory.db",
                    use_wal=False,
                )
                group_session = ("group", 12345, 10000)
                private_session = ("private", 12345, None)

                await store.upsert_summary(group_session, "用户在 A 群准备考试")
                await store.upsert_summary(private_session, "用户私聊喜欢短回复")

                self.assertEqual(
                    await store.get_summary(group_session),
                    "用户在 A 群准备考试",
                )
                self.assertEqual(
                    await store.get_summary(private_session),
                    "用户私聊喜欢短回复",
                )
                self.assertTrue(await store.delete_summary(group_session))
                self.assertEqual(await store.get_summary(group_session), "")
                self.assertFalse(await store.delete_summary(group_session))

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
