"""Shorts links: video-only automatic reposts, no descriptions or translation."""
import unittest
from unittest.mock import AsyncMock, patch

from bot import VideoBot
from link_utils import canonical_url, extract_urls, is_supported_url, to_embed_url
from media_worker import network_guard
from safe_http import allowed_url
from webhook_repost import Receipt, caption, delete_source_if_safe
from tests.test_source_actions import make_message


SHORT_ID = 'dQw4w9WgXcQ'
SHORT = f'https://www.youtube.com/shorts/{SHORT_ID}'


class ShortsLinkTests(unittest.TestCase):
    def test_short_links_only_and_canonicalized(self):
        links = [
            f'https://youtube.com/shorts/{SHORT_ID}',
            f'https://m.youtube.com/shorts/{SHORT_ID}/',
            SHORT + '?feature=share&si=abc',
        ]
        for link in links:
            with self.subTest(link=link):
                self.assertTrue(is_supported_url(link))
                self.assertEqual(canonical_url(link), SHORT)
                self.assertEqual(to_embed_url(link), SHORT)
        self.assertEqual(extract_urls('watch this ' + links[2]), [SHORT])

    def test_deny_non_shorts_and_lookalikes(self):
        invalid = [
            f'http://www.youtube.com/shorts/{SHORT_ID}',
            f'https://www.youtube.com/watch?v={SHORT_ID}',
            f'https://youtu.be/{SHORT_ID}',
            f'https://www.youtube.com/live/{SHORT_ID}',
            'https://www.youtube.com/shorts/short',
            f'https://www.youtube.com/shorts/{SHORT_ID}/extra',
            f'https://www.youtube.com:444/shorts/{SHORT_ID}',
            f'https://www.youtube.com.evil.test/shorts/{SHORT_ID}',
            f'https://www.youtube.com@evil.test/shorts/{SHORT_ID}',
            f'https://www.youtube.com/shorts/{SHORT_ID}\\x',
        ]
        for link in invalid:
            with self.subTest(link=link):
                self.assertFalse(is_supported_url(link))

    def test_youtube_requests_only_public_official_https_hosts(self):
        for url in [SHORT, 'https://www.youtube.com/youtubei/v1/player',
                    'https://youtubei.googleapis.com/youtubei/v1/player',
                    'https://rr4---sn-abcd.googlevideo.com/videoplayback']:
            with self.subTest(url=url):
                self.assertTrue(allowed_url(url, media=True))
                network_guard('urllib.Request', (url,))
        for url in ['https://googlevideo.com.evil.test/video',
                    'https://accounts.google.com/login',
                    'http://www.youtube.com/watch',
                    'https://127.0.0.1/private',
                    'https://evil.example/youtube']:
            with self.subTest(url=url):
                self.assertFalse(allowed_url(url, media=True))
                with self.assertRaises(OSError):
                    network_guard('urllib.Request', (url,))


class ShortsDownloadOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_shorts_bypasses_all_metadata_and_translation(self):
        with patch('bot.fetch_social_post', new_callable=AsyncMock) as fetch:
            # Shortcuts precede HTTP-session checks. No external metadata calls.
            post = await VideoBot.get_details(None, SHORT)
            fetch.assert_not_awaited()
        self.assertEqual(post.platform, 'YouTube Shorts')
        self.assertTrue(post.has_video)
        self.assertTrue(post.has_media)
        self.assertEqual(post.original, '')
        self.assertIsNone(post.translation)
        self.assertFalse(post.caption_known_empty)  # Do not infer a blank source caption.
        self.assertFalse(post.media_complete)
        self.assertEqual(caption(post, translate=True), ('', True))

    async def test_original_link_retained_when_delete_requested(self):
        msg, original = make_message(SHORT)
        result = await delete_source_if_safe(
            msg, SHORT, Receipt(55, True),
            media_complete=False, text_complete=True,
            caption_available=False, action='delete')
        self.assertFalse(result)
        original.delete.assert_not_awaited()

    async def test_original_link_suppressed_only_after_confirmed_upload(self):
        msg, original = make_message(SHORT)
        await delete_source_if_safe(
            msg, SHORT, Receipt(55, True),
            media_complete=False, text_complete=True,
            caption_available=False, action='keep_link')
        original.edit.assert_awaited_once_with(suppress=True)
        msg, original = make_message(SHORT)
        await delete_source_if_safe(
            msg, SHORT, Receipt(55, False),
            media_complete=False, text_complete=True,
            caption_available=False, action='keep_link')
        original.edit.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
