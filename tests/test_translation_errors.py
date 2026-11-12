"""Regression: FxTwitter's real DeepL error must never become a translation."""
import unittest
from unittest.mock import AsyncMock, patch

from social_posts import fetch_social_post, mymemory_translation, parse_x_data
from webhook_repost import caption


SOURCE = 'https://x.com/maruzenskyy/status/2120886007512133929'
NOTICE = ("You're using an older version of DeepL. To continue using the app, "
          "please update to the latest version.\n\n"
          'Sie verwenden eine ältere Version von DeepL. Bitte aktualisieren Sie die App, '
          'um sie weiterhin nutzen zu können.\n\n'
          'Вы используете старую версию DeepL. Чтобы продолжить работу с приложением, '
          'обновите его до последней версии.')


def payload(original='kimi no aiba ga!', translation=NOTICE):
    return {'code': 200, 'status': {'text': original, 'lang': 'en',
            'author': {'name': 'Creator'},
            'translation': {'text': translation, 'source_lang': 'en',
                            'target_lang': 'ru', 'provider': 'deepl'}}}


class TranslationErrorTests(unittest.IsolatedAsyncioTestCase):
    def test_actual_fxtwitter_notice_is_rejected_without_erasing_source(self):
        with self.assertLogs('social_posts', level='INFO') as logs:
            post = parse_x_data(payload())
        self.assertEqual(post.original, 'kimi no aiba ga!')
        self.assertIsNone(post.translation)
        self.assertTrue(post.translation_unavailable)
        self.assertIn('provider=fxtwitter reason=deepl_upgrade_notice', logs.output[0])
        text, complete = caption(post, translate=True)
        self.assertTrue(complete)
        self.assertNotIn('DeepL', text)
        self.assertIn(post.original, text)

    def test_russian_notice_is_also_rejected(self):
        post = parse_x_data(payload(translation=NOTICE.split('\n\n')[-1]))
        self.assertIsNone(post.translation)
        self.assertTrue(post.translation_unavailable)

    def test_valid_deepl_translation_is_not_filtered_by_provider_name(self):
        post = parse_x_data(payload('Hello, friends!', 'Привет, друзья!'))
        self.assertEqual(post.translation, 'Привет, друзья!')
        self.assertFalse(post.translation_unavailable)

    def test_original_post_about_deepl_is_preserved(self):
        post = parse_x_data(payload(original=NOTICE, translation=''))
        self.assertEqual(post.original, NOTICE)

    async def test_existing_fallback_replaces_error_with_actual_translation(self):
        with patch('social_posts.fetch_json', new_callable=AsyncMock, return_value=payload()), \
             patch('social_posts.mymemory_translation', new_callable=AsyncMock,
                   return_value='Твой напарник!') as fallback:
            post = await fetch_social_post(object(), SOURCE)
        fallback.assert_awaited_once_with(unittest.mock.ANY, 'kimi no aiba ga!', 'en', '')
        self.assertEqual(post.translation, 'Твой напарник!')
        self.assertFalse(post.translation_unavailable)

    async def test_failed_fallback_keeps_translation_unavailable_and_source_intact(self):
        with patch('social_posts.fetch_json', new_callable=AsyncMock, return_value=payload()), \
             patch('social_posts.mymemory_translation', new_callable=AsyncMock, return_value=None):
            post = await fetch_social_post(object(), SOURCE)
        self.assertIsNone(post.translation)
        self.assertTrue(post.translation_unavailable)
        self.assertEqual(post.original, 'kimi no aiba ga!')

    async def test_mymemory_http_200_with_same_service_notice_is_not_success(self):
        response = {'responseStatus': 200, 'responseData': {'translatedText': NOTICE}}
        with patch('social_posts.fetch_json', new_callable=AsyncMock, return_value=response):
            self.assertIsNone(await mymemory_translation(object(), 'Hello friends', 'en'))

    async def test_error_in_one_chunk_does_not_publish_partial_translation(self):
        responses = [{'responseStatus': 200, 'responseData': {'translatedText': 'Начало'}},
                     {'responseStatus': 200, 'responseData': {'translatedText': NOTICE}}]
        with patch('social_posts.fetch_json', new_callable=AsyncMock, side_effect=responses):
            self.assertIsNone(await mymemory_translation(object(), 'hello ' * 100, 'en'))
