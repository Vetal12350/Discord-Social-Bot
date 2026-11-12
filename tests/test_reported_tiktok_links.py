"""Offline regressions from the three real public posts examined on OVHcloud.

Only public IDs, canonical URLs and format properties are saved. No signed
CDN URLs, user content, credentials or downloaded media are fixtures.
Network availability and Discord playback are separate live checks.
"""
import json
from pathlib import Path
import unittest

from link_utils import canonical_url, extract_urls, is_supported_url
from media_worker import select_bounded_mp4
from social_posts import resolve_tiktok_url
from test_tiktok_fallback import Response, Session


CASES = json.loads(Path(__file__).with_name('fixtures').joinpath('reported_tiktok_formats.json').read_text())
LIMIT = 19 * 1024 * 1024


class ReportedTikTokLinksTests(unittest.IsolatedAsyncioTestCase):
    def test_all_reported_short_links_are_recognized_and_deduplicated(self):
        for case in CASES:
            source = case['link']
            with self.subTest(source=source):
                self.assertTrue(is_supported_url(source))
                self.assertEqual(extract_urls(source + ' ' + source + '?tracking=1'),
                                 [canonical_url(source)])

    async def test_observed_redirects_resolve_to_matching_video_posts(self):
        for case in CASES:
            with self.subTest(source=case['link']):
                target = case['canonical']
                session = Session([Response(301, target + '?tracking=1'), Response(200)])
                resolved = await resolve_tiktok_url(session, case['link'])
                self.assertEqual(canonical_url(resolved), target)
                self.assertTrue(all(not kwargs['allow_redirects'] for _, kwargs in session.requests))

    def test_observed_h264_aac_formats_fit_and_are_selected(self):
        for case in CASES:
            with self.subTest(source=case['link']):
                chosen = list(select_bounded_mp4({'formats': case['formats']}, LIMIT))
                self.assertEqual(len(chosen), 1)
                self.assertEqual(chosen[0]['vcodec'], 'h264')
                self.assertEqual(chosen[0]['acodec'], 'aac')
                self.assertEqual(chosen[0]['filesize'], case['download_bytes'])
                self.assertLessEqual(chosen[0]['filesize'], LIMIT)

    def test_observed_formats_do_not_bypass_a_smaller_upload_limit(self):
        for case in CASES:
            with self.subTest(source=case['link']):
                # Exclude unknown-sized streams in this strict-size test.
                known = [fmt for fmt in case['formats'] if fmt.get('filesize')]
                self.assertEqual(list(select_bounded_mp4({'formats': known}, 1)), [])
