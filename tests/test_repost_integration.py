"""Repost pipeline with real parsers/files and a simulated Discord HTTP boundary.

No token, Discord connection, or external network is used. MP4 fixtures model
the track boxes checked by inspect_mp4; they are not playback fixtures.
"""
import asyncio
from collections import OrderedDict
from dataclasses import replace
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from bot import VideoBot
from media import MediaTooLarge
from media_cache import MediaCache
from media_worker import select_bounded_mp4
from social_posts import fetch_social_post, parse_x_data
from test_translation_errors import NOTICE, SOURCE as X_SOURCE, payload as x_payload
from webhook_repost import WebhookFailure, WebhookPoster


SOURCE = 'https://www.tiktok.com/@creator/video/7123456789012345678'
PHOTO = SOURCE.replace('/video/', '/photo/')
ORIGINAL = 'Hello friends, this is our new video #demo @creator'
TRANSLATED = 'Привет, друзья, это наше новое видео #demo @creator'


def box(kind, content):
    return struct.pack('>I4s', len(content) + 8, kind) + content


def mp4(*, audio=True):
    def track(handler, codec):
        stsd = box(b'stsd', b'\0' * 4 + struct.pack('>I', 1) + box(codec, b''))
        return box(b'trak', box(b'mdia', box(b'hdlr', b'\0' * 8 + handler)
                               + box(b'minf', box(b'stbl', stsd))))
    tracks = track(b'vide', b'avc1') + (track(b'soun', b'mp4a') if audio else b'')
    return box(b'ftyp', b'isom\0\0\0\0') + box(b'moov', tracks) + box(b'mdat', b'fixture')


class Response:
    def __init__(self, data, status=200):
        self.status = status
        self.headers = {}
        self.content = self
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.content_length = len(self.body)

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class Session:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.sent = []
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        if '/oembed' in url:
            return Response({'title': ORIGINAL, 'author_name': 'Creator'})
        if 'tnktok.com' in url:
            return Response({}, 404)
        if 'mymemory' in url:
            if self.failure == 'translation':
                return Response({}, 503)
            return Response({'responseStatus': 200, 'responseData': {
                'translatedText': 'Привет, друзья, это наше новое видео ZXQKEEP0ZXQ ZXQKEEP1ZXQ'}})
        if 'tiktokcdn.com' in url:
            if self.failure == 'image' and 'slide2' in url:
                return Response({}, 403)
            return Response(b'\xff\xd8\xfffixture-' + url.rsplit('/', 1)[-1].encode())
        item = {'id': '7123456789012345678', 'desc': ORIGINAL,
                'author': {'nickname': 'Creator', 'uniqueId': 'creator'},
                'imagePost': {'images': [{'imageURL': {'urlList': [
                    f'https://p16-sign.tiktokcdn.com/slide{n}.jpeg']}} for n in range(1, 4)]}}
        payload = {'__DEFAULT_SCOPE__': {'webapp.video-detail': {
            'statusCode': 0, 'itemInfo': {'itemStruct': item}}}}
        return Response(('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
                         + json.dumps(payload) + '</script>').encode())

    def post(self, endpoint, **kwargs):
        # Capture multipart bytes before ExitStack closes the upload files.
        fields = kwargs['data']._fields
        payload = json.loads(next(value for meta, _, value in fields if meta['name'] == 'payload_json'))
        uploads = [(meta['filename'], value.read()) for meta, _, value in fields if 'filename' in meta]
        self.sent.append((payload, uploads, kwargs))
        if self.failure == 'timeout':
            raise TimeoutError()
        if self.failure == 'connection':
            raise aiohttp.ClientConnectionError()
        body = {'id': '123', 'channel_id': '7', 'webhook_id': '88',
                'content': payload['content'],
                'attachments': [{'filename': name} for name, _ in uploads]}
        if self.failure == 'attachments':
            body['attachments'] = []
        if self.failure == 'text':
            body['content'] = 'different'
        if self.failure == 'channel':
            body['channel_id'] = '999'
        if self.failure == 'webhook':
            body['webhook_id'] = '999'
        if self.failure == 'malformed':
            return Response(b'not-json')
        status = {'403': 403, '429': 429, '500': 500}.get(self.failure, 200)
        return Response(body, status)


class RepostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache = MediaCache()
        self.addAsyncCleanup(self.cache.close)

    def bot(self, session):
        bot = SimpleNamespace(
            config=SimpleNamespace(translate=True, source_message_action='delete',
                                   channels=frozenset(), repost_mode='webhook', repost_failure_notice=False),
            media_lock=asyncio.Lock(), media_cache=self.cache, http_session=session,
            user=SimpleNamespace(id=9), webhook_poster=WebhookPoster(),
            seen_messages=OrderedDict(), active_messages=0, auto_active_by_user={}, active_posts=set())
        bot.webhook_poster.get_webhook = AsyncMock(return_value=(
            SimpleNamespace(id=88, token='test-fixture'), 7, None))
        bot.prepare_video = lambda *args: VideoBot.prepare_video(bot, *args)
        bot.repost_message = lambda *args: VideoBot.repost_message(bot, *args)
        return bot

    def message(self, source=SOURCE, number=1):
        rights = SimpleNamespace(send_messages=True, read_message_history=True,
                                 attach_files=True, manage_messages=True)
        author = SimpleNamespace(id=42, bot=False, display_name='Message Author',
                                 display_avatar=SimpleNamespace(url='https://cdn.discordapp.com/avatar.png'))
        current = SimpleNamespace(content=source, author=author, attachments=[],
                                  delete=AsyncMock(), edit=AsyncMock())
        channel = SimpleNamespace(id=7, permissions_for=lambda _: rights,
                                  fetch_message=AsyncMock(return_value=current))
        message = SimpleNamespace(id=number, content=source, author=author, attachments=[],
                                  webhook_id=None, channel=channel,
                                  guild=SimpleNamespace(me=object(), filesize_limit=19 * 1024 * 1024))
        return message, current, rights

    async def metadata(self, session, source=SOURCE):
        # Keep language detection deterministic; exercise the real translation
        # request, token masking/restoration, parser and caption construction.
        with patch('social_posts.detect_source_language', return_value='en'):
            return await fetch_social_post(session, source, translate=True)

    async def worker(self, url, directory, *, download, limit):
        (directory / 'video.mp4').write_bytes(mp4())
        return {}

    async def test_video_translation_identity_and_confirmed_deletion(self):
        session = Session()
        bot = self.bot(session)
        post = await self.metadata(session)
        self.assertEqual(post.original, ORIGINAL)
        self.assertEqual(post.translation, TRANSLATED)
        msg, current, rights = self.message()
        with patch('bot.run_media', side_effect=self.worker):
            await VideoBot.repost_message(bot, msg, post, SOURCE, rights)
        payload, uploads, options = session.sent[0]
        self.assertIn(ORIGINAL, payload['content'])
        self.assertIn(TRANSLATED, payload['content'])
        self.assertEqual(payload['username'], 'Message Author · через Social Bot')
        self.assertEqual(payload['avatar_url'], msg.author.display_avatar.url)
        self.assertEqual(payload['allowed_mentions']['parse'], [])
        self.assertEqual(options['params'], {'wait': 'true'})
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(uploads[0][1], mp4())
        current.delete.assert_awaited_once()

    async def test_oversized_direct_uses_smaller_format_through_webhook(self):
        session = Session()
        bot = self.bot(session)
        post = replace(await self.metadata(session), video_url='https://v.tiktokcdn.com/direct.mp4')
        msg, current, rights = self.message()

        async def direct(session, url, destination, limit):
            destination.write_bytes(b'partial')
            raise MediaTooLarge()

        async def worker(url, directory, *, download, limit):
            self.assertFalse((directory / 'video.mp4').exists())
            choices = [{'ext': 'mp4', 'protocol': 'https', 'vcodec': 'h264', 'acodec': 'aac',
                        'height': height, 'filesize': size} for height, size in
                       ((1080, limit + 1), (720, len(mp4())))]
            self.assertEqual(list(select_bounded_mp4({'formats': choices}, limit)), [choices[1]])
            await self.worker(url, directory, download=download, limit=limit)

        with patch('bot.download_mp4', side_effect=direct), patch('bot.run_media', side_effect=worker):
            await VideoBot.repost_message(bot, msg, post, SOURCE, rights)
        self.assertEqual(len(session.sent), 1)
        current.delete.assert_awaited_once()

    async def test_three_photo_slides_in_order_with_translation_keep_source(self):
        session = Session()
        bot = self.bot(session)
        post = await self.metadata(session, PHOTO)
        self.assertEqual(len(post.image_urls), 3)
        self.assertFalse(post.media_complete)
        msg, current, rights = self.message(PHOTO)
        await VideoBot.repost_message(bot, msg, post, PHOTO, rights)
        payload, uploads, _ = session.sent[0]
        self.assertEqual(len(uploads), 3)
        for index, (_, body) in enumerate(uploads, 1):
            self.assertTrue(body.endswith(f'slide{index}.jpeg'.encode()))
        self.assertIn(ORIGINAL, payload['content'])
        self.assertIn(TRANSLATED, payload['content'])
        current.delete.assert_not_awaited()
        current.edit.assert_not_awaited()

    async def test_four_rapid_links_run_complete_pipeline(self):
        session = Session()
        bot = self.bot(session)
        post = await self.metadata(session)
        bot.get_details = AsyncMock(return_value=post)
        messages = [self.message(SOURCE.replace('678', str(678 + n)), n + 1) for n in range(4)]
        with patch('bot.run_media', side_effect=self.worker), patch('bot.under_pressure', return_value=False):
            await asyncio.gather(*(VideoBot.on_message(bot, msg) for msg, _, _ in messages))
        self.assertEqual(len(session.sent), 4)
        for _, current, _ in messages:
            current.delete.assert_awaited_once()
        self.assertEqual(bot.active_messages, 0)
        self.assertEqual(bot.auto_active_by_user, {})
        self.assertFalse(bot.active_posts)

    async def test_webhook_errors_and_mismatched_receipts_keep_source_without_retry(self):
        for failure in ('timeout', 'connection', '403', '429', '500', 'malformed',
                        'attachments', 'text', 'channel', 'webhook'):
            with self.subTest(failure=failure):
                session = Session(failure=failure)
                bot = self.bot(session)
                post = await self.metadata(session)
                msg, current, rights = self.message()
                with patch('bot.run_media', side_effect=self.worker):
                    try:
                        await VideoBot.repost_message(bot, msg, post, SOURCE, rights)
                    except WebhookFailure:
                        pass
                self.assertEqual(len(session.sent), 1)
                current.delete.assert_not_awaited()
                current.edit.assert_not_awaited()

    async def test_second_slide_failure_posts_nothing_and_keeps_source(self):
        session = Session(failure='image')
        post = await self.metadata(session, PHOTO)
        msg, current, rights = self.message(PHOTO)
        with self.assertRaises(WebhookFailure):
            await VideoBot.repost_message(self.bot(session), msg, post, PHOTO, rights)
        self.assertEqual(session.sent, [])
        current.delete.assert_not_awaited()

    async def test_missing_audio_keeps_video_source(self):
        session = Session()
        post = await self.metadata(session)
        msg, current, rights = self.message()

        async def silent(url, directory, *, download, limit):
            (directory / 'video.mp4').write_bytes(mp4(audio=False))
            return {}

        with patch('bot.run_media', side_effect=silent):
            await VideoBot.repost_message(self.bot(session), msg, post, SOURCE, rights)
        self.assertEqual(len(session.sent), 1)
        current.delete.assert_not_awaited()

    async def test_translation_failure_preserves_original_caption_and_source(self):
        session = Session(failure='translation')
        post = await self.metadata(session)
        self.assertTrue(post.translation_unavailable)
        msg, current, rights = self.message()
        with patch('bot.run_media', side_effect=self.worker):
            await VideoBot.repost_message(self.bot(session), msg, post, SOURCE, rights)
        self.assertIn(ORIGINAL, session.sent[0][0]['content'])
        current.delete.assert_not_awaited()

    async def test_deepl_notice_is_not_sent_and_untranslated_source_is_not_deleted(self):
        session = Session()
        post = parse_x_data(x_payload())
        self.assertIsNone(post.translation)
        self.assertTrue(post.translation_unavailable)
        msg, current, rights = self.message(X_SOURCE)
        await VideoBot.repost_message(self.bot(session), msg, post, X_SOURCE, rights)
        self.assertEqual(len(session.sent), 1)
        self.assertIn(post.original, session.sent[0][0]['content'])
        self.assertNotIn(NOTICE, session.sent[0][0]['content'])
        self.assertNotIn('DeepL', session.sent[0][0]['content'])
        current.delete.assert_not_awaited()

    async def test_third_post_preparation_failure_is_correlated_without_sending_or_deleting(self):
        source = 'https://vt.tiktok.com/ZSp98gAmN'
        session = Session()
        post = await self.metadata(session)
        msg, current, rights = self.message(source)
        with patch('bot.run_media', new_callable=AsyncMock,
                   side_effect=RuntimeError('https://cdn.example/?signature=DO_NOT_LOG')), \
             self.assertLogs('discord-video-bot', level='INFO') as captured:
            with self.assertRaises(WebhookFailure):
                await VideoBot.repost_message(self.bot(session), msg, post, source, rights)
        output = '\n'.join(captured.output)
        self.assertIn('repost_failure stage=video_prepare error_type=RuntimeError', output)
        self.assertIn('source_key=tiktok-short:ZSp98gAmN message_id=1 channel_id=7', output)
        self.assertNotIn('DO_NOT_LOG', output)
        self.assertEqual(session.sent, [])
        current.delete.assert_not_awaited()
        current.edit.assert_not_awaited()

    async def test_preparation_errors_post_nothing_and_keep_source(self):
        for failure in ('worker', 'missing', 'oversized', 'codec'):
            with self.subTest(failure=failure):
                session = Session()
                post = await self.metadata(session)
                msg, current, rights = self.message()

                async def worker(url, directory, *, download, limit):
                    if failure == 'worker':
                        raise RuntimeError('Provider unavailable')
                    if failure == 'missing':
                        return {}
                    if failure == 'oversized':
                        with (directory / 'video.mp4').open('wb') as stream:
                            stream.truncate(limit + 1)
                    else:
                        (directory / 'video.mp4').write_bytes(mp4().replace(b'avc1', b'hvc1'))
                    return {}

                # Avoid cache hits from the successful cases in this class.
                with patch('bot.run_media', side_effect=worker), \
                     patch.object(self.cache, 'get', new_callable=AsyncMock, return_value=None):
                    with self.assertRaises(WebhookFailure):
                        await VideoBot.repost_message(self.bot(session), msg, post, SOURCE, rights)
                self.assertEqual(session.sent, [])
                current.delete.assert_not_awaited()
                current.edit.assert_not_awaited()

    async def test_confirmed_receipt_cannot_delete_changed_or_incomplete_source(self):
        for reason in ('comment', 'edited', 'new_attachment', 'permissions', 'caption_missing', 'truncated'):
            with self.subTest(reason=reason):
                session = Session()
                post = await self.metadata(session)
                msg, current, rights = self.message()
                if reason == 'comment':
                    msg.content = current.content = 'My comment ' + SOURCE
                elif reason == 'edited':
                    current.content = 'Edited while downloading ' + SOURCE
                elif reason == 'new_attachment':
                    current.attachments = [object()]
                elif reason == 'permissions':
                    rights.manage_messages = False
                elif reason == 'caption_missing':
                    post = replace(post, original='', translation=None, caption_known_empty=False)
                elif reason == 'truncated':
                    post = replace(post, original='x' * 2100, translation=None)
                with patch('bot.run_media', side_effect=self.worker):
                    await VideoBot.repost_message(self.bot(session), msg, post, SOURCE, rights)
                self.assertEqual(len(session.sent), 1)
                current.delete.assert_not_awaited()
                current.edit.assert_not_awaited()

    async def test_failed_source_delete_never_reposts_webhook(self):
        session = Session()
        post = await self.metadata(session)
        msg, current, rights = self.message()
        current.delete.side_effect = TimeoutError()
        with patch('bot.run_media', side_effect=self.worker):
            await VideoBot.repost_message(self.bot(session), msg, post, SOURCE, rights)
        self.assertEqual(len(session.sent), 1)
        current.delete.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
