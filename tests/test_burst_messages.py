"""Rapid public links should not be lost to a per-user timestamp throttle."""
import asyncio
from collections import OrderedDict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import MAX_AUTO_INFLIGHT, MAX_USER_INFLIGHT, VideoBot


def message(number, user=42):
    source = f'https://vt.tiktok.com/ZSgKJho{number:02d}/'
    user_obj = SimpleNamespace(id=user, bot=False)
    permissions = SimpleNamespace(send_messages=True, read_message_history=True)
    channel = SimpleNamespace(
        id=17, permissions_for=lambda member: permissions)
    return SimpleNamespace(
        id=1000 + number, author=user_obj, content=source,
        webhook_id=None, guild=SimpleNamespace(me=object(), filesize_limit=10**7),
        channel=channel)


class BurstTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_rapid_tiktoks_from_same_user_are_processed(self):
        started = []
        release = asyncio.Event()

        async def repost(msg, post, source, permissions):
            started.append(source)
            await release.wait()

        fake = SimpleNamespace(
            config=SimpleNamespace(channels=frozenset(), repost_mode='webhook'),
            user=SimpleNamespace(id=999),
            seen_messages=OrderedDict(),
            active_messages=0,
            auto_active_by_user={},
            active_posts=set(),
            get_details=AsyncMock(return_value=object()),
            repost_message=repost,
        )
        tasks = []
        with patch('bot.under_pressure', return_value=False), \
             patch('bot.time.monotonic', return_value=1000):
            for n in range(4):
                tasks.append(asyncio.create_task(VideoBot.on_message(fake, message(n))))
                await asyncio.sleep(0)
            for _ in range(10):
                if len(started) == 4:
                    break
                await asyncio.sleep(0)
            self.assertEqual(len(started), 4)
            self.assertEqual(fake.active_messages, 4)
            self.assertEqual(fake.auto_active_by_user[42], 4)
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(fake.active_messages, 0)
        self.assertEqual(fake.auto_active_by_user, {})
        self.assertFalse(fake.active_posts)

    async def test_fifth_concurrent_link_is_rejected_but_not_marked_seen(self):
        release = asyncio.Event()

        async def repost(msg, post, source, permissions):
            await release.wait()

        fake = SimpleNamespace(
            config=SimpleNamespace(channels=frozenset(), repost_mode='webhook'),
            user=SimpleNamespace(id=999),
            seen_messages=OrderedDict(),
            active_messages=0,
            auto_active_by_user={},
            active_posts=set(),
            get_details=AsyncMock(return_value=object()),
            repost_message=repost,
        )
        with patch('bot.under_pressure', return_value=False):
            tasks = []
            for n in range(MAX_USER_INFLIGHT):
                tasks.append(asyncio.create_task(VideoBot.on_message(fake, message(n))))
                await asyncio.sleep(0)
            self.assertEqual(fake.active_messages, MAX_USER_INFLIGHT)
            await VideoBot.on_message(fake, message(99))
            self.assertNotIn(1099, fake.seen_messages)
            self.assertEqual(fake.active_messages, MAX_USER_INFLIGHT)
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(fake.active_messages, 0)

    def test_global_limit_is_bounded(self):
        self.assertGreaterEqual(MAX_AUTO_INFLIGHT, MAX_USER_INFLIGHT)
        self.assertLessEqual(MAX_AUTO_INFLIGHT, 8)


if __name__ == '__main__':
    unittest.main()
