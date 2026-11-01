"""Ensure X/TikTok captions preserve the original post text."""
import unittest
from types import SimpleNamespace

from social_posts import parse_x_data, parse_tiktok_data, x_original_text
from webhook_repost import caption, units


def post(original, translation=None, *, language='en', platform='X / Twitter'):
    return SimpleNamespace(original=original, translation=translation,
                           language=language, platform=platform)


class CaptionTests(unittest.TestCase):
    def test_x_original_and_translation_both_visible(self):
        text, complete = caption(post('Hello world!', 'Привет, мир!'), translate=True)
        self.assertTrue(complete)
        self.assertIn('**Оригинал:**', text)
        self.assertIn('Hello world!', text)
        self.assertIn('**Перевод:**', text)
        self.assertIn('Привет, мир!', text)
        self.assertLessEqual(units(text), 2000)

    def test_tiktok_original_and_translation_both_visible(self):
        text, complete = caption(post('Look at this!', 'Посмотрите на это!',
                                      platform='TikTok', language='en'), translate=True)
        self.assertTrue(complete)
        self.assertIn('Look at this!', text)
        self.assertIn('Посмотрите на это!', text)

    def test_translation_disabled_retains_original(self):
        text, complete = caption(post('Hello world!', 'Привет, мир!'), translate=False)
        self.assertEqual(text, '**Текст поста:**\nHello world!')
        self.assertTrue(complete)

    def test_translation_unavailable_retains_original(self):
        text, complete = caption(post('Original caption', None), translate=True)
        self.assertEqual(text, '**Текст поста:**\nOriginal caption')
        self.assertTrue(complete)

    def test_russian_original_not_replaced(self):
        text, complete = caption(post('Привет, друзья!', 'Hello, friends!', language='ru'), translate=True)
        self.assertEqual(text, '**Текст поста:**\nПривет, друзья!')
        self.assertTrue(complete)

    def test_empty_caption_does_not_create_empty_headers(self):
        for platform in ('TikTok', 'X / Twitter'):
            text, complete = caption(post('', 'unexpected translation', platform=platform), translate=True)
            self.assertEqual(text, '')
            self.assertTrue(complete)

    def test_long_translation_does_not_hide_original(self):
        text, complete = caption(post('Original complete', 'Перевод' * 400), translate=True)
        self.assertEqual(text, '**Текст поста:**\nOriginal complete')
        self.assertTrue(complete)

    def test_overlong_original_is_marked_incomplete(self):
        text, complete = caption(post('a' * 2100, 'b' * 25), translate=True)
        self.assertFalse(complete)
        self.assertIn('a', text)
        self.assertLessEqual(units(text), 2000)

    def test_tiktok_description_is_source_caption(self):
        p = parse_tiktok_data(
            {'title': 'Creator on TikTok', 'description': 'Real caption #tag',
             'author_name': 'Creator'}, 'https://www.tiktok.com/@creator/video/7123456789012345678')
        self.assertEqual(p.original, 'Real caption #tag')
        text, complete = caption(p, translate=True)
        self.assertIn('Real caption #tag', text)
        self.assertTrue(complete)


class XRawTextTests(unittest.TestCase):
    def test_recover_text_before_media_link(self):
        raw = 'Hello from X! https://t.co/abc'
        start = len('Hello from X! ')
        original = x_original_text({
            'text': '', 'raw_text': {'text': raw,
                                    'facets': [{'type': 'media',
                                                'indices': [start, len(raw)]}]}})
        self.assertEqual(original, 'Hello from X!')

    def test_only_media_url_is_not_caption(self):
        raw = 'https://t.co/abc'
        original = x_original_text({
            'text': '', 'raw_text': {'text': raw,
                                    'facets': [{'type': 'media',
                                                'indices': [0, len(raw)]}]}})
        self.assertEqual(original, '')

    def test_surrogate_pair_offsets(self):
        raw = 'Hi 😃 https://t.co/abc'
        begin = len('Hi 😃 '.encode('utf-16-le')) // 2
        end = len(raw.encode('utf-16-le')) // 2
        self.assertEqual(x_original_text({
            'text': '', 'raw_text': {'text': raw,
                                    'facets': [{'type': 'media',
                                                'indices': [begin, end]}]}}), 'Hi 😃')

    def test_malformed_media_indices_fail_closed(self):
        self.assertEqual(x_original_text({'text': '',
                         'raw_text': {'text': 'Hello https://t.co/abc',
                                      'facets': [{'type': 'media',
                                                  'indices': [999, 1000]}]}}), '')

    def test_status_display_text_preferred(self):
        self.assertEqual(x_original_text({'text': 'Actual author text',
                                          'raw_text': {'text': 't.co/blah',
                                                       'facets': []}}), 'Actual author text')

    def test_parser_uses_recovered_original(self):
        status = {'text': '', 'author': {'screen_name': 'user'},
                  'raw_text': {'text': 'Caption https://t.co/abc',
                               'facets': [{'type': 'media', 'indices': [8, 24]}]},
                  'media': {'photos': [{'url': 'https://pbs.twimg.com/media/example.jpg'}]}}
        parsed = parse_x_data({'code': 200, 'status': status})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.original, 'Caption')


if __name__ == '__main__':
    unittest.main()
