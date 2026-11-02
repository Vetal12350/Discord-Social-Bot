"""TikTok short-link regressions: safe redirects and metadata fallback."""
import unittest
from unittest.mock import AsyncMock, patch

from link_utils import extract_urls, is_supported_url
from social_posts import fetch_social_post, resolve_tiktok_url


SHORT = "https://vt.tiktok.com/ZSgKJhojp/"
POST = "https://www.tiktok.com/@creator/video/7123456789012345678"


class Response:
    def __init__(self, status, location=None):
        self.status = status
        self.headers = {'Location': location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, typ, value, tb):
        return False


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class TikTokFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_short_url_is_recognized_and_extracted(self):
        self.assertTrue(is_supported_url(SHORT))
        self.assertEqual(extract_urls(SHORT), [SHORT.rstrip('/')])
        self.assertTrue(is_supported_url(POST))

    async def test_intermediate_first_party_redirect(self):
        intermediate = "https://www.tiktok.com/redirect?target=post"
        session = Session([Response(302, intermediate), Response(302, POST), Response(200)])
        self.assertEqual(await resolve_tiktok_url(session, SHORT), POST)
        self.assertEqual(len(session.requests), 3)
        self.assertTrue(all(request[1]['allow_redirects'] is False for request in session.requests))

    async def test_off_site_redirect_is_rejected_without_following(self):
        session = Session([Response(302, "https://evil.example/steal")])
        self.assertIsNone(await resolve_tiktok_url(session, SHORT))
        self.assertEqual(len(session.requests), 1)

    async def test_missing_location_is_rejected(self):
        self.assertIsNone(await resolve_tiktok_url(Session([Response(302)]), SHORT))

    async def test_short_url_403_uses_ytdlp_metadata_fallback(self):
        session = Session([Response(403)])
        details = {'title': 'Original text #tag', 'uploader': 'Author', 'id': '12345'}
        with patch('social_posts.tiktok_ytdlp_metadata', new_callable=AsyncMock, return_value=details) as extractor, \
             patch('social_posts.fetch_json', new_callable=AsyncMock) as fetch_json:
            post = await fetch_social_post(session, SHORT, translate=False)
        self.assertIsNotNone(post)
        self.assertEqual(post.platform, 'TikTok')
        self.assertEqual(post.original, 'Original text #tag')
        self.assertTrue(post.has_video)
        extractor.assert_awaited_once_with(SHORT)
        fetch_json.assert_not_awaited()

    async def test_shortlink_fallback_no_metadata_returns_none(self):
        session = Session([Response(403)])
        with patch('social_posts.tiktok_ytdlp_metadata', new_callable=AsyncMock, return_value=None):
            self.assertIsNone(await fetch_social_post(session, SHORT, translate=False))

    async def test_unresolved_shortcode_does_not_request_other_hosts(self):
        session = Session([Response(302, 'https://attacker.example@www.tiktok.com:444/post')])
        self.assertIsNone(await resolve_tiktok_url(session, SHORT))
        self.assertEqual(len(session.requests), 1)


if __name__ == '__main__':
    unittest.main()
