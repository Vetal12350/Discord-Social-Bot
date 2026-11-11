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

    def test_rejects_missing_or_partial_gallery_instead_of_cover(self):
        self.assertIsNone(parse_photo_html(page({
            'id': POST_ID, 'desc': '', 'video': {'cover': SLIDE_A},
        }), POST_ID))
        self.assertIsNone(parse_photo_html(page(photo_item(count=MAX_SLIDES + 1)), POST_ID))
        self.assertIsNone(parse_photo_html(page(photo_item(urls=['https://evil.example/pic.jpg'])), POST_ID))
        self.assertIsNone(parse_photo_html(page(photo_item(urls=['http://p16-sign.tiktokcdn.com/p.jpg'])), POST_ID))

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
        self.assertTrue(post.media_complete)
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
