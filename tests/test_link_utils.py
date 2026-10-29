"""Regression tests for supported social URL normalization and safety."""

import unittest

from link_utils import canonical_url, extract_urls, is_supported_url, to_embed_url


POST_ID = "2115736864338378891"
CANONICAL = f"https://x.com/i/status/{POST_ID}"


class XLinkAliasesTest(unittest.TestCase):
    def test_known_hosts_are_supported_and_canonicalized(self):
        domains = (
            "x.com", "twitter.com", "fixupx.com", "fxtwitter.com",
            "fixvx.com", "vxtwitter.com", "twittpr.com", "pxtwitter.com",
            "www.fixvx.com", "www.vxtwitter.com", "www.twittpr.com",
            "g.fxtwitter.com", "t.fixupx.com", "i.fxtwitter.com",
            "d.fixupx.com", "dl.fxtwitter.com", "m.fixupx.com", "o.fxtwitter.com",
        )
        for domain in domains:
            with self.subTest(domain=domain):
                link = f"https://{domain}/vizitcondoms/status/{POST_ID}?s=20"
                self.assertTrue(is_supported_url(link))
                self.assertEqual(canonical_url(link), CANONICAL)
                self.assertEqual(to_embed_url(link), f"https://fixupx.com/vizitcondoms/status/{POST_ID}")

    def test_fixvx_reported_link_and_deduplication(self):
        link = f"https://fixvx.com/vizitcondoms/status/{POST_ID}"
        self.assertEqual(extract_urls(link), [CANONICAL])
        self.assertEqual(
            extract_urls(f"{link} https://x.com/vizitcondoms/status/{POST_ID}"),
            [CANONICAL],
        )

    def test_x_media_suffixes(self):
        for suffix in ("/video/1", "/photo/2", "/"):
            with self.subTest(suffix=suffix):
                url = f"https://vxtwitter.com/vizitcondoms/status/{POST_ID}{suffix}"
                self.assertTrue(is_supported_url(url))
                self.assertEqual(canonical_url(url), CANONICAL)

    def test_reject_lookalikes_and_non_post_links(self):
        bad = (
            f"http://fixvx.com/u/status/{POST_ID}",
            f"https://fixvx.com.evil.test/u/status/{POST_ID}",
            f"https://evilfixvx.com/u/status/{POST_ID}",
            f"https://fixvx.com@evil.test/u/status/{POST_ID}",
            f"https://evil.test@fixvx.com/u/status/{POST_ID}",
            f"https://fixvx.com:444/u/status/{POST_ID}",
            "https://fixvx.com/settings",
            f"https://fixvx.com/u/status/notanid",
            f"https://random.fxtwitter.com/u/status/{POST_ID}",
            f"https://fxtwitter.com/u/status/{POST_ID}/unknown",
        )
        for link in bad:
            with self.subTest(link=link):
                self.assertFalse(is_supported_url(link))
                with self.assertRaises(ValueError):
                    canonical_url(link)

    def test_existing_tiktok_link_still_supported(self):
        url = "https://www.tiktok.com/@creator/video/7123456789012345678?utm_source=share"
        self.assertTrue(is_supported_url(url))
        self.assertEqual(canonical_url(url), "https://www.tiktok.com/@creator/video/7123456789012345678")
        self.assertEqual(to_embed_url(url), "https://tnktok.com/@creator/video/7123456789012345678")


if __name__ == "__main__":
    unittest.main()
