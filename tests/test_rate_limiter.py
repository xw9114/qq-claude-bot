import asyncio
import unittest
from types import SimpleNamespace

import nonebot


nonebot.init()

from nonebot.matcher import current_event  # noqa: E402

from plugins.rate_limiter import (  # noqa: E402
    OutboundRateLimiter,
    resolve_send_context,
)


class OutboundRateLimiterTest(unittest.TestCase):
    def test_uses_longest_required_delay(self):
        limiter = OutboundRateLimiter(1, 3, 5)
        limiter._last_global_send = 10
        limiter._last_group_send["100"] = 11
        limiter._last_user_send["200"] = 12

        self.assertEqual(limiter.calculate_delay(13, "100", "200"), 4)

    def test_global_queue_serializes_operations(self):
        async def run_test():
            limiter = OutboundRateLimiter(0, 0, 0)
            active_count = 0
            max_active_count = 0
            execution_order: list[str] = []

            async def operation(index: int) -> int:
                nonlocal active_count, max_active_count
                active_count += 1
                max_active_count = max(max_active_count, active_count)
                execution_order.append(f"start-{index}")
                await asyncio.sleep(0.001)
                execution_order.append(f"end-{index}")
                active_count -= 1
                return index

            results = await asyncio.gather(
                *(
                    limiter.submit(
                        lambda index=index: operation(index),
                        str(index),
                        str(index),
                    )
                    for index in range(3)
                )
            )
            await limiter.shutdown()

            self.assertEqual(results, [0, 1, 2])
            self.assertEqual(max_active_count, 1)
            self.assertEqual(
                execution_order,
                ["start-0", "end-0", "start-1", "end-1", "start-2", "end-2"],
            )

        asyncio.run(run_test())

    def test_resolves_ids_from_current_event(self):
        token = current_event.set(SimpleNamespace(group_id=10001, user_id=20001))
        try:
            self.assertEqual(resolve_send_context({}), ("10001", "20001"))
            self.assertEqual(
                resolve_send_context({"group_id": 30001, "user_id": 40001}),
                ("30001", "40001"),
            )
        finally:
            current_event.reset(token)


if __name__ == "__main__":
    unittest.main()
