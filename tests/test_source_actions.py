"""Regression checks for common X/TikTok source handling."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from webhook_repost import Receipt, caption, delete_source_if_safe


def make_message(url):
    user = SimpleNamespace(id=42)
    original = SimpleNamespace(content=url, author=user, attachments=[],
                               stickers=[], poll=None, reference=None, webhook_id=None,
                               edit=AsyncMock(), delete=AsyncMock())
    rights = SimpleNamespace(manage_messages=True, read_message_history=True)
    channel = SimpleNamespace(id=7, fetch_message=AsyncMock(return_value=original),
                              permissions_for=lambda _: rights)
    msg = SimpleNamespace(id=11, content=url, author=user, attachments=[],
                          stickers=[], poll=None, reference=None, webhook_id=None,
                          guild=SimpleNamespace(me=object()), channel=channel)
    return msg, original


class TestSourceActions(unittest.IsolatedAsyncioTestCase):
    async def test_source_modes_both_platforms(self):
        links = ('https://x.com/a/status/2116393850717737216',
                 'https://www.tiktok.com/@a/video/7123456789012345678')
        for link in links:
            msg, original = make_message(link)
            success = await delete_source_if_safe(
                msg, link, Receipt(55, True), media_complete=False,
                text_complete=True, caption_available=False, action='keep_link')
            self.assertTrue(success)
            original.edit.assert_awaited_once_with(suppress=True)
            original.delete.assert_not_awaited()
            msg, original = make_message(link)
            success = await delete_source_if_safe(
                msg, link, Receipt(55, True), media_complete=False,
                text_complete=True, caption_available=False, action='delete')
            self.assertFalse(success)
            original.delete.assert_not_awaited()
            original.edit.assert_not_awaited()

    async def test_failures_keep_original(self):
        link = 'https://www.tiktok.com/@a/video/7123456789012345678'
        msg, original = make_message(link)
        await delete_source_if_safe(
            msg, link, Receipt(55, False), media_complete=True,
            text_complete=True, caption_available=True, action='keep_link')
        original.edit.assert_not_awaited()
        msg, original = make_message(link)
        msg.content = 'see ' + link
        await delete_source_if_safe(
            msg, link, Receipt(55, True), media_complete=True,
            text_complete=True, caption_available=True, action='keep_link')
        original.edit.assert_not_awaited()

    async def test_keep_all_no_edits(self):
        link = 'https://x.com/a/status/2116393850717737216'
        msg, original = make_message(link)
        await delete_source_if_safe(
            msg, link, Receipt(55, True), media_complete=True,
            text_complete=True, caption_available=True, action='keep_all')
        original.edit.assert_not_awaited()
        original.delete.assert_not_awaited()

    def test_empty_captions(self):
        for platform in ('TikTok', 'X / Twitter'):
            post = SimpleNamespace(platform=platform, original='',
                                   translation=None, language='')
            self.assertEqual(caption(post, translate=True), ('', True))
