"""TikTok photo mode: exact post matching and complete slide-only delivery."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from link_utils import is_supported_url
from social_posts import fetch_social_post
from tiktok_photos import MAX_SLIDES, parse_photo_html


PHOTO = 'https://www.tiktok.com/@creator/photo/7123456789012345678'
SHORT = 'https://vt.tiktok.com/ZSpPCjqce/'
POST_ID = '7123456789012345678'
SLIDE_A = 'https://p16-sign.tiktokcdn.com/some/image1.jpeg'
SLIDE_B = 'https://p16-sign.tiktokcdn.com/some/image2.jpeg'


def page(item, *, kind='universal'):
    if kind == 'universal':
        payload = {
            '__DEFAULT_SCOPE__': {
                'webapp.video-detail': {
                    'statusCode': 0,
                    'itemInfo': {'itemStruct': item},
                }
            }
        }
        return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">' + json.dumps(payload) + '</script>'
    return '<script type="application/json" id="SIGI_STATE">' + json.dumps({'ItemModule': {POST_ID: item}}) + '</script>'


def photo_item(urls=None, count=2):
    urls = [SLIDE_A, SLIDE_B] if urls is None else urls
    return {
        'id': POST_ID,
        'desc': 'Слайды с текстом #test',
        'author': {'nickname': 'Creator', 'uniqueId': 'creator'},
        'imagePost': {'images': [
            {'imageURL': {'urlList': [urls[n % len(urls)]]}} for n in range(count)
        ]},
        'video': {'cover': 'https://p16-sign.tiktokcdn.com/cover.jpeg'},
    }


class PhotoParsingTests(unittest.TestCase):
    def test_strict_order_and_post_match(self):
        result = parse_photo_html(page(photo_item()), POST_ID)
        self.assertEqual(result['image_urls'], (SLIDE_A, SLIDE_B))
        self.assertEqual(result['description'], 'Слайды с текстом #test')
        self.assertEqual(result['author'], 'Creator')
        self.assertFalse(result['caption_known_empty'])
        self.assertIsNone(parse_photo_html(page(photo_item()), '999'))

    def test_supports_legacy_embedded_state(self):
        self.assertEqual(parse_photo_html(page(photo_item(), kind='sigi'), POST_ID)['image_urls'],
                         (SLIDE_A, SLIDE_B))

    def test_explicit_empty_caption_is_distinct_from_missing_metadata(self):
        item = photo_item()
        item['desc'] = ''
        self.assertTrue(parse_photo_html(page(item), POST_ID)['caption_known_empty'])
        del item['desc']
        self.assertFalse(parse_photo_html(page(item), POST_ID)['caption_known_empty'])

    def test_rejects_missing_or_partial_gallery_instead_of_cover(self):
        self.assertIsNone(parse_photo_html(page({
            'id': POST_ID, 'desc': '', 'video': {'cover': SLIDE_A},
        }), POST_ID))
        self.assertIsNone(parse_photo_html(page(photo_item(count=MAX_SLIDES + 1)), POST_ID))
        self.assertIsNone(parse_photo_html(page(photo_item(urls=['https://evil.example/pic.jpg'])), POST_ID))
        self.assertIsNone(parse_photo_html(page(photo_item(urls=['http://p16-sign.tiktokcdn.com/p.jpg'])), POST_ID))

    def test_keeps_up_to_two_vetted_mirrors_per_slide(self):
        mirror = SLIDE_A.replace('p16-sign', 'p19-sign')
        item = photo_item(count=1)
        item['imagePost']['images'][0]['imageURL']['urlList'] = [
            'https://evil.example/a.jpeg', SLIDE_A, SLIDE_A, mirror,
            SLIDE_B, SLIDE_B.replace('p16', 'p77')]
        result = parse_photo_html(page(item), POST_ID)
        self.assertEqual(result['image_urls'], (SLIDE_A,))
        self.assertEqual(result['image_alternates'], ((mirror, SLIDE_B),))
        self.assertEqual(parse_photo_html(page(photo_item()), POST_ID)['image_alternates'], ((), ()))

    def test_no_html_script_execution_and_wrong_status(self):
        body = page(photo_item()).replace('"statusCode": 0', '"statusCode": 10204')
        self.assertIsNone(parse_photo_html(body, POST_ID))
        self.assertIsNone(parse_photo_html('<script>alert(1)</script>', POST_ID))


class PhotoRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_link_photo_uses_images_without_embed_or_ytdlp(self):
        self.assertTrue(is_supported_url(SHORT))
        metadata = {'description': 'Слайды #tag', 'author': 'Creator',
                    'handle': 'creator', 'image_urls': (SLIDE_A, SLIDE_B),
                    'caption_known_empty': False}
        with patch('social_posts.resolve_tiktok_url', new_callable=AsyncMock,
                   return_value=PHOTO), \
             patch('social_posts.fetch_tiktok_photo_post', new_callable=AsyncMock,
                   return_value=metadata) as photos, \
             patch('social_posts.fetch_json', new_callable=AsyncMock) as json_fetch, \
             patch('social_posts.tiktok_ytdlp_metadata', new_callable=AsyncMock) as ytdlp:
            post = await fetch_social_post(None, SHORT, translate=False)
        photos.assert_awaited_once_with(None, PHOTO)
        json_fetch.assert_not_awaited()
        ytdlp.assert_not_awaited()
        self.assertEqual(post.platform, 'TikTok')
        self.assertEqual(post.image_urls, (SLIDE_A, SLIDE_B))
        self.assertFalse(post.has_video)
        # The gallery photos are complete, but the original soundtrack isn't
        # transferred. Never authorize deletion of the original post.
        self.assertFalse(post.media_complete)
        self.assertEqual(post.original, 'Слайды #tag')

    async def test_no_photo_images_means_no_repost(self):
        with patch('social_posts.resolve_tiktok_url', new_callable=AsyncMock,
                   return_value=PHOTO), \
             patch('social_posts.fetch_tiktok_photo_post', new_callable=AsyncMock,
                   return_value=None):
            post = await fetch_social_post(None, SHORT, translate=False)
        self.assertIsNone(post)


if __name__ == '__main__':
    unittest.main()


class MirrorDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def run_candidates(self, outcomes):
        from pathlib import Path
        import media
        calls = []

        async def fake(session, url, destination, limit):
            calls.append(url)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return Path(outcome)

        with patch('media.download_image', side_effect=fake):
            try:
                result = await media.download_image_candidates(None, ('a', 'b', 'c'), Path('x'), 10)
            except BaseException as exc:  # noqa: BLE001 - returned for assertions
                return calls, exc
        return calls, result

    async def test_only_403_and_404_move_to_next_mirror(self):
        from media import ImageUnavailable, MediaTooLarge
        calls, result = await self.run_candidates([ImageUnavailable(404), ImageUnavailable(403), 'ok.jpg'])
        self.assertEqual((calls, str(result)), (['a', 'b', 'c'], 'ok.jpg'))
        for stop in (ImageUnavailable(500), ImageUnavailable(429), MediaTooLarge('big'),
                     ValueError('html'), RuntimeError('Image destination rejected'), TimeoutError()):
            with self.subTest(stop=type(stop).__name__):
                calls, result = await self.run_candidates([stop, 'never.jpg', 'never.jpg'])
                self.assertEqual(calls, ['a'])
                self.assertIs(result, stop)

    async def test_last_mirror_failure_is_reported(self):
        from media import ImageUnavailable
        calls, result = await self.run_candidates([ImageUnavailable(404)] * 3)
        self.assertEqual(calls, ['a', 'b', 'c'])
        self.assertIsInstance(result, ImageUnavailable)
        self.assertIsInstance(result, RuntimeError)
