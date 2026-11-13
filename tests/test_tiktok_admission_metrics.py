"""Count eligible TikTok inputs before admission, without changing admission."""
from collections import OrderedDict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import MAX_AUTO_INFLIGHT, VideoBot
from test_burst_messages import message


class AdmissionMetricTests(unittest.IsolatedAsyncioTestCase):
    def bot(self):
        return SimpleNamespace(config=SimpleNamespace(channels=frozenset(), repost_mode='webhook'),
            user=SimpleNamespace(id=999), seen_messages=OrderedDict(), active_messages=MAX_AUTO_INFLIGHT,
            auto_active_by_user={}, active_posts=set(), get_details=AsyncMock(), repost_message=AsyncMock())

    async def test_metric_precedes_limit_and_does_not_admit_or_mark_message_seen(self):
        bot = self.bot()
        msg = message(7)
        msg.content += '?secret=DO_NOT_LOG'
        with self.assertLogs('discord-video-bot', level='INFO') as captured:
            await VideoBot.on_message(bot, msg)
        self.assertIn('tiktok_received message_id=1007 channel_id=17 source_keys=tiktok-short:ZSgKJho07', captured.output[0])
        self.assertIn('reason=global_inflight_limit', captured.output[1])
        self.assertNotIn('DO_NOT_LOG', '\n'.join(captured.output))
        bot.get_details.assert_not_awaited()
        bot.repost_message.assert_not_awaited()
        self.assertNotIn(msg.id, bot.seen_messages)
        self.assertEqual(bot.active_messages, MAX_AUTO_INFLIGHT)

    async def test_no_tiktok_metric_for_x_or_excluded_or_seen_messages(self):
        for kind in ('x', 'excluded', 'seen', 'bot', 'webhook', 'no_send'):
            bot = self.bot()
            msg = message(7)
            if kind == 'x':
                msg.content = 'https://x.com/user/status/123'
            elif kind == 'excluded':
                bot.config.channels = frozenset({99999})
            elif kind == 'seen':
                bot.seen_messages[msg.id] = 1
            elif kind == 'bot':
                msg.author.bot = True
            elif kind == 'webhook':
                msg.webhook_id = 123
            else:
                msg.channel.permissions_for = lambda member: SimpleNamespace(send_messages=False)
            with self.subTest(kind=kind), patch('bot.log.info') as info:
                await VideoBot.on_message(bot, msg)
            self.assertFalse(any(call.args and call.args[0].startswith('tiktok_received') for call in info.call_args_list))

    async def test_mixed_message_lists_only_unique_supported_tiktok_sources(self):
        bot = self.bot()
        msg = message(7)
        msg.content = ('https://vt.tiktok.com/ZSpQHULbU/ https://vt.tiktok.com/ZSpQHULbU/?tracking=1 '
                       'https://x.com/user/status/123 https://www.tiktok.com/@user/video/7707467025265757460')
        with self.assertLogs('discord-video-bot', level='INFO') as captured:
            await VideoBot.on_message(bot, msg)
        first = captured.output[0]
        self.assertIn('source_keys=tiktok-short:ZSpQHULbU,tiktok:7707467025265757460', first)
        self.assertNotIn('tracking', first)
        self.assertNotIn('@user', first)

    async def test_same_post_key_in_different_url_forms_is_counted_once_without_changing_processing(self):
        bot = self.bot()
        bot.active_messages = 0
        bot.get_details.return_value = object()
        msg = message(7)
        msg.author.display_name = 'PRIVATE_NAME'
        msg.content = ('PRIVATE_TEXT https://www.tiktok.com/@PRIVATE_HANDLE/video/7707467025265757460?token=SECRET '
                       'https://m.tiktok.com/@PRIVATE_HANDLE/video/7707467025265757460')
        with patch('bot.under_pressure', return_value=False), self.assertLogs('discord-video-bot', level='INFO') as captured:
            await VideoBot.on_message(bot, msg)
            await VideoBot.on_message(bot, msg)  # Same Discord event is ignored after admission.
        inputs = [line for line in captured.output if 'tiktok_received ' in line]
        self.assertEqual(len(inputs), 1)
        self.assertTrue(inputs[0].endswith('source_keys=tiktok:7707467025265757460'))
        self.assertEqual(bot.get_details.await_count, 2)  # Existing per-URL processing is unchanged.
        self.assertEqual(bot.repost_message.await_count, 2)
        self.assertEqual(bot.active_messages, 0)
        self.assertFalse(bot.active_posts)
        for secret in ('PRIVATE_NAME', 'PRIVATE_HANDLE', 'PRIVATE_TEXT', 'SECRET', 'https://'):
            self.assertNotIn(secret, '\n'.join(captured.output))
