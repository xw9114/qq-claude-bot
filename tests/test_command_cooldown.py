import unittest

from plugins.command_cooldown import SessionCooldown


class SessionCooldownTest(unittest.TestCase):
    def test_allows_first_call_and_blocks_until_cooldown_expires(self):
        now = [100.0]
        cooldown = SessionCooldown(10.0, clock=lambda: now[0])

        self.assertIsNone(cooldown.retry_after(("group", 1)))
        self.assertEqual(cooldown.retry_after(("group", 1)), 10)

        now[0] = 105.1
        self.assertEqual(cooldown.retry_after(("group", 1)), 5)

        now[0] = 110.0
        self.assertIsNone(cooldown.retry_after(("group", 1)))

    def test_tracks_keys_independently(self):
        cooldown = SessionCooldown(10.0, clock=lambda: 100.0)

        self.assertIsNone(cooldown.retry_after(("group", 1)))
        self.assertIsNone(cooldown.retry_after(("group", 2)))
        self.assertEqual(cooldown.retry_after(("group", 1)), 10)

    def test_zero_seconds_disables_cooldown(self):
        cooldown = SessionCooldown(0.0, clock=lambda: 100.0)

        self.assertIsNone(cooldown.retry_after(("group", 1)))
        self.assertIsNone(cooldown.retry_after(("group", 1)))


if __name__ == "__main__":
    unittest.main()
