#!/usr/bin/env python3
"""检查 OpenAI 兼容接口是否可用。"""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI


def main() -> int:
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL") or None

    if not api_key:
        print("错误：未配置 OPENAI_API_KEY，请先复制并编辑 .env.example。")
        return 1

    client = OpenAI(api_key=api_key, base_url=base_url)

    try:
        response = client.chat.completions.create(
            model="gpt-5",
            messages=[{"role": "user", "content": "只回复 OK"}],
        )
    except Exception as error:
        print(f"连接失败：{error}")
        return 1

    reply = response.choices[0].message.content or ""
    print(f"连接成功，模型回复：{reply.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
