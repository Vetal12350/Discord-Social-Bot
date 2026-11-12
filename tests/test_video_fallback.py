"""Oversized direct TikTok media can still have a fitting extractor variant."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import VideoBot
from media import MediaTooLarge


class VideoFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_tiktok_direct_url_tries_bounded_worker(self):
        limit = 1024
        post = SimpleNamespace(platform='TikTok', video_url='https://v.tiktokcdn.com/direct.mp4')
        source = 'https://www.tiktok.com/@creator/video/7123456789012345678'
        cache = SimpleNamespace(get=AsyncMock(return_value=None), put=AsyncMock())
        bot = SimpleNamespace(media_cache=cache, http_session=object())
        info = SimpleNamespace(compatible=True)

        async def oversized(session, url, destination, budget):
            # A streaming rejection can leave a partial file. The worker uses
            # overwrites=False, so that file must be removed before fallback.
            destination.write_bytes(b'partial')
            raise MediaTooLarge('Direct variant exceeds upload budget')

        async def fitting(url, directory, *, download, limit):
            self.assertFalse((directory / 'video.mp4').exists())
            (directory / 'video.mp4').write_bytes(b'fitting MP4')
            return {}

        with tempfile.TemporaryDirectory() as folder, \
             patch('bot.download_mp4', side_effect=oversized), \
             patch('bot.run_media', side_effect=fitting) as worker, \
             patch('bot.inspect_mp4', return_value=info):
            directory = Path(folder)
            media, actual_info = await VideoBot.prepare_video(bot, post, source, directory, limit)
            self.assertEqual(media.read_bytes(), b'fitting MP4')
            self.assertIs(actual_info, info)
            worker.assert_awaited_once_with(source, directory, download=True, limit=limit)
            cache.put.assert_awaited_once()

    async def test_oversized_x_direct_url_keeps_existing_size_failure(self):
        post = SimpleNamespace(platform='X', video_url='https://video.twimg.com/direct.mp4')
        cache = SimpleNamespace(get=AsyncMock(return_value=None), put=AsyncMock())
        bot = SimpleNamespace(media_cache=cache, http_session=object())
        with tempfile.TemporaryDirectory() as folder, \
             patch('bot.download_mp4', side_effect=MediaTooLarge('Too large')), \
             patch('bot.run_media', new_callable=AsyncMock) as worker:
            with self.assertRaises(MediaTooLarge):
                await VideoBot.prepare_video(
                    bot, post, 'https://x.com/creator/status/123', Path(folder), 1024)
            worker.assert_not_awaited()
            cache.put.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
